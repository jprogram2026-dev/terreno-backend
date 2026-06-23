"""
Orchestrator do relatório.

Combina todas as fontes em uma única chamada (paralela) e aplica a lógica
de cálculo de indicadores derivados que antes ficava no front.

Vantagens de centralizar aqui:
- Frontend faz UMA chamada em vez de 6
- Cache mais eficaz (chave por município+CNAE)
- Lógica de negócio em Python (testável, versionável)
- Fácil migrar para SSE/WebSocket para progressivamente entregar campos
"""
import asyncio
import logging
import statistics
from typing import Any, Optional

from app.core.cache import cached
from app.services import bcb, ibge, rais, trends

log = logging.getLogger(__name__)

# Mapeamento de categorias de negócio → CNAE classe (4 dígitos da CNAE 2.0)
# e termo de busca pro Google Trends. cliente_alvo é a proporção da população
# que consome esse tipo de negócio com frequência relevante (estimativa).
# Em produção, isso viraria uma tabela no banco. Para acadêmico, dict basta.
CNAE_CATALOG: dict[str, dict] = {
    # --- ALIMENTAÇÃO ---
    "cafeteria":       {"nome": "Cafeteria",                "cnae_classe": "5611", "termo": "cafeteria",         "cliente_alvo": 0.22},
    "restaurante":     {"nome": "Restaurante",              "cnae_classe": "5611", "termo": "restaurante",       "cliente_alvo": 0.55},
    "lanchonete":      {"nome": "Lanchonete/Hamburgueria",  "cnae_classe": "5611", "termo": "lanchonete",        "cliente_alvo": 0.70},
    "pizzaria":        {"nome": "Pizzaria",                 "cnae_classe": "5611", "termo": "pizzaria",          "cliente_alvo": 0.50},
    "padaria":         {"nome": "Padaria/Confeitaria",      "cnae_classe": "4721", "termo": "padaria",           "cliente_alvo": 0.75},
    "sorveteria":      {"nome": "Sorveteria/Açaiteria",     "cnae_classe": "5611", "termo": "sorveteria",        "cliente_alvo": 0.35},
    "buffet":          {"nome": "Buffet/Eventos",           "cnae_classe": "5620", "termo": "buffet",            "cliente_alvo": 0.05},
    "mercado":         {"nome": "Mini-mercado",             "cnae_classe": "4712", "termo": "mercado",           "cliente_alvo": 0.95},

    # --- BELEZA E ESTÉTICA ---
    "salao":           {"nome": "Salão/Barbearia",          "cnae_classe": "9602", "termo": "salão beleza",      "cliente_alvo": 0.90},
    "estetica":        {"nome": "Estética/Depilação",       "cnae_classe": "9602", "termo": "estética",          "cliente_alvo": 0.40},
    "massoterapia":    {"nome": "Massoterapia/Spa",         "cnae_classe": "9609", "termo": "massagem",          "cliente_alvo": 0.10},
    "cosmeticos":      {"nome": "Cosméticos/Perfumaria",    "cnae_classe": "4772", "termo": "cosméticos",        "cliente_alvo": 0.65},

    # --- SAÚDE ---
    "odontologia":     {"nome": "Clínica Odontológica",     "cnae_classe": "8630", "termo": "dentista",          "cliente_alvo": 0.55},
    "farmacia":        {"nome": "Farmácia/Drogaria",        "cnae_classe": "4771", "termo": "farmácia",          "cliente_alvo": 0.85},
    "otica":           {"nome": "Ótica",                    "cnae_classe": "4774", "termo": "ótica",             "cliente_alvo": 0.30},

    # --- EDUCAÇÃO ---
    "idiomas":         {"nome": "Escola de Idiomas",        "cnae_classe": "8593", "termo": "escola de inglês",  "cliente_alvo": 0.06},
    "cursos":          {"nome": "Cursos Profissionalizantes","cnae_classe": "8599", "termo": "curso técnico",     "cliente_alvo": 0.05},
    "escolinha":       {"nome": "Escolinha Esportiva",      "cnae_classe": "8591", "termo": "escolinha de futebol","cliente_alvo": 0.08},

    # --- FITNESS E BEM-ESTAR ---
    "academia":        {"nome": "Academia/Fitness",         "cnae_classe": "9313", "termo": "academia",          "cliente_alvo": 0.18},
    "pilates":         {"nome": "Pilates/Yoga",             "cnae_classe": "9313", "termo": "pilates",           "cliente_alvo": 0.10},

    # --- PETS ---
    "petshop":         {"nome": "Petshop",                  "cnae_classe": "4789", "termo": "petshop",           "cliente_alvo": 0.46},
    "veterinaria":     {"nome": "Clínica Veterinária",      "cnae_classe": "7500", "termo": "veterinário",       "cliente_alvo": 0.40},

    # --- VAREJO ---
    "moda":            {"nome": "Loja de Roupas",           "cnae_classe": "4781", "termo": "loja roupas",       "cliente_alvo": 0.85},
    "calcados":        {"nome": "Loja de Calçados",         "cnae_classe": "4782", "termo": "loja de calçados",  "cliente_alvo": 0.50},
    "floricultura":    {"nome": "Floricultura",             "cnae_classe": "4789", "termo": "floricultura",      "cliente_alvo": 0.15},
    "papelaria":       {"nome": "Papelaria/Livraria",       "cnae_classe": "4761", "termo": "papelaria",         "cliente_alvo": 0.40},

    # --- SERVIÇOS ---
    "lavanderia":      {"nome": "Lavanderia",               "cnae_classe": "9601", "termo": "lavanderia",        "cliente_alvo": 0.20},
    "oficina":         {"nome": "Oficina Mecânica",         "cnae_classe": "4520", "termo": "oficina mecânica",  "cliente_alvo": 0.40},
    "contabilidade":   {"nome": "Escritório de Contabilidade","cnae_classe": "6920", "termo": "contador",          "cliente_alvo": 0.05},
    "celular":         {"nome": "Assistência Técnica Celular","cnae_classe": "9521", "termo": "assistência celular","cliente_alvo": 0.35},

    # --- HOSPEDAGEM ---
    "pousada":         {"nome": "Pousada/Hotel Pequeno",    "cnae_classe": "5510", "termo": "pousada",           "cliente_alvo": 0.20},
}

# População estimada do Brasil para cálculo de ratio nacional
POP_BR_REFERENCIA = 213_000_000


def _compute_indicators(
    population: int,
    cnae_cfg: dict,
    cempre_compare: Optional[dict],
    pib_data: Optional[dict],
    trend_data: Optional[dict],
    macro: Optional[dict],
    rais_data: Optional[dict] = None,
) -> dict:
    """Calcula indicadores derivados a partir das fontes brutas."""
    out: dict[str, Any] = {
        "data_real_cempre": False,
    }

    # Estabelecimentos
    estab = None
    pessoal = None
    if cempre_compare and cempre_compare.get("municipio"):
        mun = cempre_compare["municipio"]
        estab = mun.get("empresas")
        pessoal = mun.get("pessoal")
        out["data_real_cempre"] = estab is not None
        out["cempre_ano"] = mun.get("ano")

    if estab is None:
        # Fallback de último recurso — não devia acontecer com a tabela 9418 disponível
        log.warning("CEMPRE sem dado, fallback para zero")
        estab = 0
        pessoal = 0

    hab_por_estab = round(population / estab) if estab > 0 else 0
    mercado_potencial = round(population * cnae_cfg["cliente_alvo"])
    density_ratio = (estab / population) * 10_000 if population > 0 else 0

    # Densidade competitiva: comparar ratio do município com ratio nacional
    density_level = "Moderada"
    density_pct = 50.0
    if cempre_compare and cempre_compare.get("brasil"):
        empresas_br = cempre_compare["brasil"].get("empresas") or 0
        if empresas_br > 0:
            ratio_br = (empresas_br / POP_BR_REFERENCIA) * 10_000
            intensity = density_ratio / ratio_br if ratio_br > 0 else 1
            if intensity >= 1.4:
                density_level = "Alta"
                density_pct = min(90.0, 50 + (intensity - 1) * 40)
            elif intensity >= 0.7:
                density_level = "Moderada"
                density_pct = 50 + (intensity - 1) * 30
            else:
                density_level = "Baixa"
                density_pct = max(15.0, intensity * 50)

    # Aderência demográfica
    if population < 20_000:    demog_score = 42
    elif population < 50_000:  demog_score = 58
    elif population < 100_000: demog_score = 68
    elif population < 500_000: demog_score = 78
    elif population < 1e6:     demog_score = 84
    else:                      demog_score = 89

    # Tendência e sazonalidade derivadas do Google Trends
    trend_level = "Indisponível"
    trend_growth = None
    sazon_level = "Indisponível"
    sazon_cv = None
    if trend_data and trend_data.get("points"):
        values = [p["value"] for p in trend_data["points"]]
        if len(values) >= 6 and values[0] > 0:
            trend_growth = round(((values[-1] - values[0]) / values[0]) * 100, 1)
            if trend_growth > 15:    trend_level = "Crescente"
            elif trend_growth > -5:  trend_level = "Estável"
            else:                    trend_level = "Declinante"

            mean = statistics.mean(values)
            if mean > 0:
                stdev = statistics.pstdev(values)
                sazon_cv = round(stdev / mean, 3)
                if sazon_cv >= 0.25:   sazon_level = "Alta"
                elif sazon_cv >= 0.10: sazon_level = "Moderada"
                else:                  sazon_level = "Baixa"

    # Tendência de emprego no setor (RAIS)
    emprego_level = "Indisponível"
    emprego_variacao = None
    if rais_data and rais_data.get("variacao_pct") is not None:
        emprego_variacao = rais_data["variacao_pct"]
        if emprego_variacao > 10:    emprego_level = "Setor em expansão"
        elif emprego_variacao > -5:  emprego_level = "Setor estável"
        else:                        emprego_level = "Setor em retração"

    out.update({
        "populacao": population,
        "empresas_municipio": estab,
        "pessoal_municipio": pessoal,
        "habitantes_por_empresa": hab_por_estab,
        "mercado_potencial": mercado_potencial,
        "density_ratio_local": round(density_ratio, 2),
        "density_level": density_level,
        "density_pct": round(density_pct, 1),
        "demog_score": demog_score,
        "trend_level": trend_level,
        "trend_growth_pct": trend_growth,
        "sazon_level": sazon_level,
        "sazon_cv": sazon_cv,
        "emprego_level": emprego_level,
        "emprego_variacao_pct": emprego_variacao,
    })

    # Geração de sinais (oportunidades e riscos) a partir das condições reais.
    # Cada sinal só aparece se a condição correspondente foi satisfeita pelos dados.
    out["oportunidades"], out["riscos"] = _generate_signals(out, cempre_compare, pib_data, macro, rais_data)

    return out


def _generate_signals(
    ind: dict,
    cempre_compare: Optional[dict],
    pib_data: Optional[dict],
    macro: Optional[dict],
    rais_data: Optional[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Gera listas dinâmicas de oportunidades e riscos baseadas nos dados reais.
    Cada item: {titulo: str, descricao: str}.
    """
    oportunidades: list[dict] = []
    riscos: list[dict] = []

    # ---- Densidade competitiva ----
    if ind["density_level"] == "Baixa":
        oportunidades.append({
            "titulo": "Mercado pouco saturado.",
            "descricao": (
                f"Apenas {ind['empresas_municipio']} estabelecimentos no setor, "
                f"com {ind['habitantes_por_empresa']:,} habitantes por empresa "
                f"(densidade {ind['density_ratio_local']}/10 mil hab)."
            ).replace(",", "."),
        })
    elif ind["density_level"] == "Alta":
        riscos.append({
            "titulo": "Alta saturação setorial.",
            "descricao": (
                f"{ind['empresas_municipio']} estabelecimentos já competem no setor, "
                f"com apenas {ind['habitantes_por_empresa']:,} habitantes por empresa. "
                f"Diferenciação será fator crítico."
            ).replace(",", "."),
        })

    # ---- Comparação com média nacional ----
    if cempre_compare and cempre_compare.get("brasil") and cempre_compare["brasil"].get("empresas"):
        pop_br = 213_000_000
        ratio_br = (cempre_compare["brasil"]["empresas"] / pop_br) * 10_000
        ratio_local = ind.get("density_ratio_local") or 0
        if ratio_br > 0:
            ratio_relativo = ratio_local / ratio_br
            if ratio_relativo < 0.7:
                pct_menor = round((1 - ratio_relativo) * 100)
                oportunidades.append({
                    "titulo": "Setor subatendido vs. média nacional.",
                    "descricao": (
                        f"A densidade local do setor é {pct_menor}% inferior à média do Brasil, "
                        f"indicando possível demanda não atendida."
                    ),
                })
            elif ratio_relativo > 1.4:
                pct_maior = round((ratio_relativo - 1) * 100)
                riscos.append({
                    "titulo": "Mercado mais disputado que a média nacional.",
                    "descricao": (
                        f"A densidade local é {pct_maior}% superior à média do Brasil — "
                        f"competição mais intensa que em outras regiões."
                    ),
                })

    # ---- Tendência de busca (Google Trends) ----
    if ind.get("trend_growth_pct") is not None:
        g = ind["trend_growth_pct"]
        if g > 20:
            oportunidades.append({
                "titulo": "Interesse de busca em alta.",
                "descricao": f"Buscas pelo termo cresceram {g:+.0f}% nos últimos 12 meses no Google Trends.",
            })
        elif g < -15:
            riscos.append({
                "titulo": "Interesse de busca em queda.",
                "descricao": f"Buscas pelo termo recuaram {g:.0f}% nos últimos 12 meses no Google Trends — atenção à demanda.",
            })

    # ---- Sazonalidade ----
    if ind.get("sazon_level") == "Alta":
        riscos.append({
            "titulo": "Setor com sazonalidade elevada.",
            "descricao": (
                f"Coeficiente de variação de {(ind['sazon_cv'] or 0) * 100:.0f}% nos pageviews mensais — "
                f"prepare fluxo de caixa para meses de baixa."
            ),
        })

    # ---- Tendência de emprego no setor (RAIS) ----
    if ind.get("emprego_variacao_pct") is not None:
        v = ind["emprego_variacao_pct"]
        if v > 15:
            oportunidades.append({
                "titulo": "Setor em expansão de empregos.",
                "descricao": f"Vínculos formais cresceram {v:+.1f}% no período (RAIS) — sinal de setor aquecido localmente.",
            })
        elif v < -10:
            riscos.append({
                "titulo": "Retração de empregos no setor.",
                "descricao": f"Vínculos formais caíram {v:.1f}% no período (RAIS) — setor pode estar encolhendo na região.",
            })

    # ---- Contexto macro (BCB) ----
    if macro:
        selic = macro.get("selic")
        ipca = macro.get("ipca12m")
        if selic is not None and selic >= 13:
            riscos.append({
                "titulo": "Juros elevados encarecem o crédito.",
                "descricao": f"Selic em {selic:.2f}% — financiamento de capital de giro tende a comprometer mais a margem.",
            })
        elif selic is not None and selic < 8:
            oportunidades.append({
                "titulo": "Custo do crédito favorável.",
                "descricao": f"Selic em {selic:.2f}% — janela para financiar investimento inicial com taxas mais baixas.",
            })
        if ipca is not None and ipca >= 6:
            riscos.append({
                "titulo": "Inflação pressiona insumos e aluguel.",
                "descricao": f"IPCA acumulado de {ipca:.2f}% em 12 meses — repasse de preços e reajustes contratuais sob pressão.",
            })

    # ---- Aderência demográfica ----
    if ind.get("demog_score", 0) >= 80:
        oportunidades.append({
            "titulo": "Porte da cidade favorece o setor.",
            "descricao": (
                f"Município com {ind['populacao']:,} habitantes oferece base ampla de mercado potencial "
                f"({ind['mercado_potencial']:,} clientes potenciais estimados)."
            ).replace(",", "."),
        })
    elif ind.get("demog_score", 0) <= 50:
        riscos.append({
            "titulo": "Mercado local pequeno.",
            "descricao": (
                f"População de {ind['populacao']:,} habitantes limita escala — "
                f"considere atração de demanda regional ou modelo de operação enxuto."
            ).replace(",", "."),
        })

    # ---- PIB per capita ----
    if pib_data and pib_data.get("pib_percap"):
        pc = pib_data["pib_percap"]
        # Referência: PIB per capita do Brasil ~R$ 50.000 (2021)
        if pc >= 60_000:
            oportunidades.append({
                "titulo": "Renda local acima da média nacional.",
                "descricao": (
                    f"PIB per capita de R$ {pc:,.0f} (IBGE) sugere maior poder de consumo, "
                    f"compatível com ticket médio mais elevado."
                ).replace(",", "."),
            })
        elif pc < 25_000:
            riscos.append({
                "titulo": "Renda local abaixo da média nacional.",
                "descricao": (
                    f"PIB per capita de R$ {pc:,.0f} (IBGE) limita ticket médio possível — "
                    f"considere posicionamento de preço acessível."
                ).replace(",", "."),
            })

    return oportunidades, riscos


@cached("report", ttl=21600)  # 6h
async def build_report(cnae_key: str, muni_id: int, uf_id: int) -> dict:
    """
    Endpoint principal: dispara todas as fontes em paralelo, agrega, retorna JSON pronto.
    """
    if cnae_key not in CNAE_CATALOG:
        raise ValueError(f"CNAE desconhecido: {cnae_key}")

    cnae_cfg = CNAE_CATALOG[cnae_key]

    # Precisamos da sigla UF para a query RAIS. Buscamos do IBGE em paralelo.
    estados = await ibge.list_states()
    uf_match = next((s for s in estados if s["id"] == uf_id), None)
    sigla_uf = uf_match["sigla"] if uf_match else None

    # Dispara todas as fontes em paralelo (RAIS só roda se BigQuery estiver configurado)
    rais_coro = (
        rais.get_emprego_setor(sigla_uf, muni_id, cnae_cfg["cnae_classe"])
        if (sigla_uf and rais.is_available())
        else asyncio.sleep(0, result=None)
    )

    results = await asyncio.gather(
        ibge.get_population(muni_id),
        ibge.get_cempre_comparison(cnae_cfg["cnae_classe"], muni_id, uf_id),
        ibge.get_pib_municipal(muni_id),
        trends.get_trend(cnae_cfg["termo"], uf_id),
        bcb.get_macro_indicators(),
        rais_coro,
        return_exceptions=True,
    )
    pop_r, cempre_r, pib_r, trend_r, macro_r, rais_r = results

    # População IBGE é obrigatória
    if isinstance(pop_r, Exception):
        raise RuntimeError(f"IBGE população indisponível: {pop_r}")

    # Resto é opcional — logamos e seguimos
    rais_status = "ok" if not isinstance(rais_r, Exception) else f"err: {rais_r}"
    if rais_r is None and rais_status == "ok":
        rais_status = "skipped (BigQuery não configurado)"
    sources_status = {
        "ibge_pop": "ok",
        "ibge_cempre": "ok" if not isinstance(cempre_r, Exception) else f"err: {cempre_r}",
        "ibge_pib": "ok" if not isinstance(pib_r, Exception) else f"err: {pib_r}",
        "google_trends": "ok" if not isinstance(trend_r, Exception) else f"err: {trend_r}",
        "bcb_macro": "ok" if not isinstance(macro_r, Exception) else f"err: {macro_r}",
        "rais_bigquery": rais_status,
    }
    for k, v in sources_status.items():
        if not v.startswith("ok"):
            log.info("Fonte %s: %s", k, v)

    cempre_compare = cempre_r if not isinstance(cempre_r, Exception) else None
    pib_data = pib_r if not isinstance(pib_r, Exception) else None
    trend_data = trend_r if not isinstance(trend_r, Exception) else None
    macro = macro_r if not isinstance(macro_r, Exception) else None
    rais_data = rais_r if not isinstance(rais_r, Exception) else None

    indicators = _compute_indicators(
        pop_r["population"], cnae_cfg, cempre_compare, pib_data, trend_data, macro, rais_data,
    )

    return {
        "cnae": cnae_cfg,
        "populacao_ano": pop_r["year"],
        "sigla_uf": sigla_uf,
        "indicators": indicators,
        "cempre": cempre_compare,
        "pib": pib_data,
        "trend": trend_data,
        "macro": macro,
        "rais": rais_data,
        "sources_status": sources_status,
    }
