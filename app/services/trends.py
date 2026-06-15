"""
Service Google Trends via pytrends.

ATENÇÃO: pytrends faz scraping não-oficial da interface Google Trends.
Características operacionais:
- Sem API key
- Sujeito a bloqueio temporário (HTTP 429) se exceder ~10 requests/minuto
- A lib quebra periodicamente quando o Google muda o frontend
- Roda síncrona — envolvemos em run_in_executor para não bloquear o loop async

Para projeto acadêmico isso é aceitável. Em produção séria, considerar SerpAPI ou DataForSEO.

A indireção via backend é justamente o que permite usar pytrends — do navegador
não funciona por CORS e por rate limit (Google bloquearia rápido).
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

from pytrends.request import TrendReq

from app.core.cache import cached
from app.core.config import get_settings

log = logging.getLogger(__name__)
_settings = get_settings()

# Mapeamento de UF para o código geográfico do Google Trends (formato BR-XX)
UF_TO_GEO = {
    11: "BR-RO", 12: "BR-AC", 13: "BR-AM", 14: "BR-RR", 15: "BR-PA",
    16: "BR-AP", 17: "BR-TO", 21: "BR-MA", 22: "BR-PI", 23: "BR-CE",
    24: "BR-RN", 25: "BR-PB", 26: "BR-PE", 27: "BR-AL", 28: "BR-SE",
    29: "BR-BA", 31: "BR-MG", 32: "BR-ES", 33: "BR-RJ", 35: "BR-SP",
    41: "BR-PR", 42: "BR-SC", 43: "BR-RS", 50: "BR-MS", 51: "BR-MT",
    52: "BR-GO", 53: "BR-DF",
}

# Executor dedicado para chamadas síncronas pytrends.
# 1 worker = serializa chamadas → evita rate limit do Google.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pytrends")


def _build_pytrends() -> TrendReq:
    """Instancia TrendReq com proxy opcional."""
    kwargs: dict[str, Any] = {
        "hl": "pt-BR",
        "tz": 180,  # GMT-3 em minutos
        "timeout": (10, 25),
        "retries": 2,
        "backoff_factor": 0.5,
    }
    if _settings.PYTRENDS_PROXY:
        kwargs["proxies"] = [_settings.PYTRENDS_PROXY]
    return TrendReq(**kwargs)


def _fetch_trend_sync(termo: str, uf_id: Optional[int]) -> dict:
    """
    Versão síncrona — roda no executor.
    Retorna {points: [{month, value}, ...], geo, related: [...]}.
    """
    pytrends = _build_pytrends()
    geo = UF_TO_GEO.get(uf_id, "BR") if uf_id else "BR"

    # build_payload define o que vai ser consultado
    # timeframe "today 12-m" = últimos 12 meses
    pytrends.build_payload([termo], timeframe="today 12-m", geo=geo)

    # Interesse ao longo do tempo (DataFrame com index=data, colunas=termos)
    df = pytrends.interest_over_time()
    if df.empty:
        raise ValueError(f"Google Trends sem dados para '{termo}' em {geo}")

    # Converte index datetime → mês YYYYMM e pega o valor da coluna do termo
    points = []
    for ts, row in df.iterrows():
        if "isPartial" in df.columns and bool(row.get("isPartial", False)):
            continue
        valor = int(row[termo])
        points.append({
            "month": ts.strftime("%Y%m"),
            "value": valor,
            "iso_date": ts.strftime("%Y-%m-%d"),
        })

    # Consultas relacionadas — informação extra útil para o usuário
    related: list[str] = []
    try:
        related_queries = pytrends.related_queries()
        top = related_queries.get(termo, {}).get("top")
        if top is not None and not top.empty:
            related = top.head(5)["query"].tolist()
    except Exception as e:
        log.debug("Related queries falhou para '%s': %s", termo, e)

    return {
        "termo": termo,
        "geo": geo,
        "points": points,
        "related_queries": related,
    }


@cached("trends", ttl=43200)  # 12h — Google Trends atualiza com latência
async def get_trend(termo: str, uf_id: Optional[int] = None) -> dict:
    """
    Busca série de interesse de busca para um termo nos últimos 12 meses.
    Cacheado por 12h porque o Google Trends já tem latência intrínseca de 1-2 dias.

    Args:
        termo: palavra-chave (ex. "cafeteria", "padaria")
        uf_id: id IBGE da UF para filtro regional. Se None, busca nacional.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_trend_sync, termo, uf_id)


async def shutdown() -> None:
    """Encerra o executor no shutdown."""
    _executor.shutdown(wait=False, cancel_futures=True)
