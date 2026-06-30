"""
Service IBGE — consolida todas as integrações com APIs do IBGE.

Cobre:
- Localidades (estados, municípios)
- População residente estimada (SIDRA tabela 6579)
- CEMPRE — número de empresas e pessoal ocupado por município+CNAE (tabela 9418)
- PIB municipal (tabela 5938)
- Auto-descoberta de IDs de variáveis e classificações via /metadados

Toda função pesada é cacheada com TTL para reduzir carga nos servidores do IBGE.
"""
import asyncio
import logging
import re
from typing import Any, Optional

from app.core.cache import cached
from app.core.config import get_settings
from app.core.http import fetch_json

log = logging.getLogger(__name__)
_settings = get_settings()


# ============================================================================
# Localidades
# ============================================================================

@cached("ibge:estados", ttl=86400)  # 24h — muda raramente
async def list_states() -> list[dict]:
    """Lista os 27 estados brasileiros."""
    data = await fetch_json(
        f"{_settings.IBGE_LOCALIDADES}/estados",
        params={"orderBy": "nome"},
    )
    return [
        {"id": s["id"], "sigla": s["sigla"], "nome": s["nome"]}
        for s in data
    ]


@cached("ibge:municipios", ttl=86400)
async def list_municipalities(uf_id: int) -> list[dict]:
    """Lista municípios de um estado, ordenados por nome."""
    data = await fetch_json(
        f"{_settings.IBGE_LOCALIDADES}/estados/{uf_id}/municipios"
    )
    return [
        {"id": m["id"], "nome": m["nome"]}
        for m in sorted(data, key=lambda x: x["nome"])
    ]


@cached("ibge:muni")
async def get_municipio(muni_id: int) -> dict:
    """
    Detalhes de um município específico por ID — usado pra resolver nome e UF
    quando temos só o código (ex: 5103403 → Cuiabá, MT). Cacheado porque dado
    estático que não muda.
    """
    data = await fetch_json(
        f"{_settings.IBGE_LOCALIDADES}/municipios/{muni_id}"
    )
    if not data:
        return {}
    uf_obj = (data.get("microrregiao", {})
                  .get("mesorregiao", {})
                  .get("UF", {}))
    return {
        "id": data.get("id"),
        "nome": data.get("nome"),
        "uf_sigla": uf_obj.get("sigla"),
        "uf_nome": uf_obj.get("nome"),
    }


# ============================================================================
# População (SIDRA 6579)
# ============================================================================

@cached("ibge:pop")
async def get_population(muni_id: int) -> dict:
    """
    População residente estimada (SIDRA tabela 6579, variável 9324).
    Retorna {population: int, year: str}.
    """
    url = f"{_settings.IBGE_SIDRA}/6579/periodos/-1/variaveis/9324"
    data = await fetch_json(url, params={"localidades": f"N6[{muni_id}]"})
    series = data[0]["resultados"][0]["series"][0]["serie"]
    last_period = max(series.keys())
    value = int(series[last_period])
    return {"population": value, "year": last_period}


# ============================================================================
# Auto-descoberta de metadados das tabelas SIDRA
# ============================================================================

# Cache em módulo (não TTL) — descoberto uma vez por boot do servidor
_sidra_meta_cache: dict[str, Any] = {"cempre": None, "pib_munic": None, "ready": False}


async def _fetch_agregado_meta(agregado_id: int) -> dict:
    """Busca metadados de um agregado SIDRA."""
    return await fetch_json(f"{_settings.IBGE_SIDRA}/{agregado_id}/metadados")


def _find_variavel(meta: dict, *keywords: str) -> Optional[dict]:
    """Encontra a primeira variável cujo nome contém todas as keywords."""
    for v in meta.get("variaveis", []):
        nome = (v.get("nome") or "").lower()
        if all(k.lower() in nome for k in keywords):
            return v
    return None


def _find_classificacao(meta: dict, *keywords: str) -> Optional[dict]:
    for c in meta.get("classificacoes", []):
        nome = (c.get("nome") or "").lower()
        if all(k.lower() in nome for k in keywords):
            return c
    return None


def _find_cnae_categoria(classificacao: dict, codigo4dig: str) -> Optional[dict]:
    """
    Encontra categoria CNAE no SIDRA a partir do código de classe (4 dígitos).

    O CEMPRE no SIDRA expõe a CNAE em diferentes níveis de agregação dependendo da
    tabela e do município (omitindo dados para preservar sigilo estatístico em
    municípios pequenos). Estratégia de fallback:
      1) Match exato pelos 4 dígitos (classe)
      2) Match pelos 3 primeiros dígitos (grupo)
      3) Match pelos 2 primeiros dígitos (divisão)
      4) Retorna a primeira categoria como último recurso

    Categorias do SIDRA têm nomes como "56.1 Restaurantes e outros" ou "5611-2 ..."
    ou "56 Alimentação" — precisamos lidar com várias formatações.
    """
    categorias = classificacao.get("categorias", [])
    if not categorias:
        return None

    def code_at_start(nome: str) -> str:
        """Extrai a sequência inicial de dígitos (ignorando pontos), até 4 chars."""
        m = re.match(r"^(\d[\d.]*)", nome.strip())
        if not m:
            return ""
        # Remove pontos: "56.1" → "561", "5611-2" → "5611"
        return m.group(1).replace(".", "").replace("-", "")[:4]

    target_classe = codigo4dig            # 5611
    target_grupo = codigo4dig[:3]         # 561
    target_divisao = codigo4dig[:2]       # 56

    # 1) classe
    for cat in categorias:
        code = code_at_start(str(cat.get("nome") or ""))
        if code == target_classe:
            return cat

    # 2) grupo
    for cat in categorias:
        code = code_at_start(str(cat.get("nome") or ""))
        if code == target_grupo:
            log.info("CEMPRE: classe %s não disponível, usando grupo %s (%s)",
                     target_classe, target_grupo, cat.get("nome"))
            return cat

    # 3) divisão
    for cat in categorias:
        code = code_at_start(str(cat.get("nome") or ""))
        if code == target_divisao:
            log.info("CEMPRE: classe %s não disponível, usando divisão %s (%s)",
                     target_classe, target_divisao, cat.get("nome"))
            return cat

    # 4) última tentativa: ID matching (alguns metadados expõem o código no campo id)
    for cat in categorias:
        cid = str(cat.get("id") or "")
        if cid.startswith(codigo4dig) or cid == codigo4dig:
            return cat

    log.warning("CEMPRE: nenhuma categoria encontrada para CNAE %s "
                "(testei classe/grupo/divisão %s/%s/%s)",
                codigo4dig, target_classe, target_grupo, target_divisao)
    return None


async def discover_sidra_metadata() -> dict:
    """
    Descobre IDs de variáveis e classificações das tabelas SIDRA que usamos.
    Chamado uma vez no startup. Os IDs ficam disponíveis durante toda a vida do processo.
    """
    if _sidra_meta_cache["ready"]:
        return _sidra_meta_cache

    cempre_res, pib_res = await asyncio.gather(
        _fetch_agregado_meta(9418),  # CEMPRE
        _fetch_agregado_meta(5938),  # PIB Municipal
        return_exceptions=True,
    )

    if not isinstance(cempre_res, Exception):
        var_emp = _find_variavel(cempre_res, "empresas") or _find_variavel(cempre_res, "organizações")
        var_pes = _find_variavel(cempre_res, "pessoal", "ocupado", "total")
        classif = _find_classificacao(cempre_res, "cnae") or _find_classificacao(cempre_res, "atividade")
        _sidra_meta_cache["cempre"] = {
            "var_empresas": var_emp["id"] if var_emp else None,
            "var_pessoal": var_pes["id"] if var_pes else None,
            "classif_cnae": classif,
        }
        log.info("CEMPRE meta: emp=%s pes=%s classif=%s",
                 var_emp and var_emp["id"],
                 var_pes and var_pes["id"],
                 classif and classif["id"])
    else:
        log.warning("Falha ao descobrir CEMPRE: %s", cempre_res)

    if not isinstance(pib_res, Exception):
        var_pib = _find_variavel(pib_res, "produto interno bruto") or _find_variavel(pib_res, "pib")
        var_pc = _find_variavel(pib_res, "per capita") or _find_variavel(pib_res, "percapita")
        _sidra_meta_cache["pib_munic"] = {
            "var_pib": var_pib["id"] if var_pib else None,
            "var_pib_percap": var_pc["id"] if var_pc else None,
        }
        log.info("PIB meta: pib=%s pc=%s",
                 var_pib and var_pib["id"],
                 var_pc and var_pc["id"])
    else:
        log.warning("Falha ao descobrir PIB: %s", pib_res)

    _sidra_meta_cache["ready"] = True
    return _sidra_meta_cache


# ============================================================================
# CEMPRE (SIDRA 9418) — empresas e pessoal ocupado por CNAE+local
# ============================================================================

@cached("ibge:cempre")
async def get_cempre(cnae_classe: str, nivel: str, localidade_id: int | str) -> dict:
    """
    Busca dados do CEMPRE para um CNAE classe (4 dígitos) e localidade.

    Args:
        cnae_classe: ex. "5611" (Restaurantes)
        nivel: "N6" (município), "N3" (UF), "N1" (Brasil)
        localidade_id: id IBGE ou "all" se nivel=N1

    Retorna {empresas: int|None, pessoal: int|None, ano: str|None, categoria_usada: str}.
    Campos podem vir None se o IBGE omitir o dado por sigilo estatístico.
    """
    meta = await discover_sidra_metadata()
    cempre = meta.get("cempre")
    if not cempre or not cempre.get("classif_cnae"):
        raise RuntimeError("Metadados CEMPRE indisponíveis")

    categoria = _find_cnae_categoria(cempre["classif_cnae"], cnae_classe)
    if not categoria:
        raise ValueError(f"CNAE classe {cnae_classe} não encontrado no CEMPRE")

    categoria_nome = str(categoria.get("nome") or "")

    vars_ids = "|".join(str(v) for v in [cempre["var_empresas"], cempre["var_pessoal"]] if v)
    classif = f"{cempre['classif_cnae']['id']}[{categoria['id']}]"
    loc = "N1[all]" if nivel == "N1" else f"{nivel}[{localidade_id}]"

    url = f"{_settings.IBGE_SIDRA}/9418/periodos/-1/variaveis/{vars_ids}"
    data = await fetch_json(
        url,
        params={"localidades": loc, "classificacao": classif},
        timeout=_settings.HTTP_TIMEOUT_HEAVY,
    )

    out = {
        "empresas": None,
        "pessoal": None,
        "ano": None,
        "categoria_usada": categoria_nome,
    }

    # Marcadores de sigilo/omissão do SIDRA:
    # ".." = não disponível, "-" = zero, "..." = sigilo estatístico, "X" = sigilo
    SIGILO_MARKERS = {"..", "-", "...", "X", ""}

    for item in data:
        serie = item.get("resultados", [{}])[0].get("series", [{}])[0].get("serie", {})
        if not serie:
            continue
        # Pega o último período com valor numérico utilizável
        for periodo in sorted(serie.keys(), reverse=True):
            raw = str(serie[periodo]).strip()
            if raw in SIGILO_MARKERS:
                continue
            try:
                valor = int(float(raw))
            except (ValueError, TypeError):
                continue
            if str(item.get("id")) == str(cempre["var_empresas"]):
                out["empresas"] = valor
                out["ano"] = periodo
            elif str(item.get("id")) == str(cempre["var_pessoal"]):
                out["pessoal"] = valor
            break

    if out["empresas"] is None:
        log.info("CEMPRE: dados omitidos para CNAE %s em %s[%s] (provável sigilo estatístico)",
                 cnae_classe, nivel, localidade_id)
    return out


async def get_cempre_comparison(cnae_classe: str, muni_id: int, uf_id: int) -> dict:
    """
    Busca CEMPRE em 3 níveis simultaneamente: município, estado e Brasil.
    Permite comparar densidade do setor local com benchmarks.
    """
    results = await asyncio.gather(
        get_cempre(cnae_classe, "N6", muni_id),
        get_cempre(cnae_classe, "N3", uf_id),
        get_cempre(cnae_classe, "N1", "all"),
        return_exceptions=True,
    )
    keys = ["municipio", "estado", "brasil"]
    return {
        k: (None if isinstance(r, Exception) else r)
        for k, r in zip(keys, results)
    }


# ============================================================================
# PIB Municipal (SIDRA 5938)
# ============================================================================

@cached("ibge:pib")
async def get_pib_municipal(muni_id: int) -> dict:
    """
    PIB total e PIB per capita do município.
    Retorna {pib: float, pib_percap: float, ano: str}.
    Valores do SIDRA vêm em R$ × 1000 para PIB total.
    """
    meta = await discover_sidra_metadata()
    pib_meta = meta.get("pib_munic")
    if not pib_meta:
        raise RuntimeError("Metadados PIB indisponíveis")

    vars_ids = "|".join(str(v) for v in [pib_meta["var_pib"], pib_meta["var_pib_percap"]] if v)
    url = f"{_settings.IBGE_SIDRA}/5938/periodos/-1/variaveis/{vars_ids}"
    data = await fetch_json(
        url,
        params={"localidades": f"N6[{muni_id}]"},
        timeout=_settings.HTTP_TIMEOUT_HEAVY,
    )

    out = {"pib": None, "pib_percap": None, "ano": None}
    for item in data:
        serie = item.get("resultados", [{}])[0].get("series", [{}])[0].get("serie", {})
        if not serie:
            continue
        ultimo = max(serie.keys())
        try:
            valor = float(serie[ultimo])
        except (ValueError, TypeError):
            continue
        if str(item.get("id")) == str(pib_meta["var_pib"]):
            out["pib"] = valor
            out["ano"] = ultimo
        elif str(item.get("id")) == str(pib_meta["var_pib_percap"]):
            out["pib_percap"] = valor
    return out
