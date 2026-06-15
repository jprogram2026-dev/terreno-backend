"""
Service Banco Central (SGS).

Séries usadas:
- 432: Meta da taxa Selic definida pelo Copom (% a.a.)
- 433: Variação mensal do IPCA (%)
- 1:   Taxa de câmbio livre - Dólar americano (venda)
"""
import asyncio
import logging
from typing import Any

from app.core.cache import cached
from app.core.config import get_settings
from app.core.http import fetch_json

log = logging.getLogger(__name__)
_settings = get_settings()


async def _fetch_serie(codigo: int, n: int = 1) -> list[dict]:
    """Busca os últimos N valores de uma série do SGS."""
    url = f"{_settings.BCB_API}/bcdata.sgs.{codigo}/dados/ultimos/{n}"
    return await fetch_json(url, params={"formato": "json"})


@cached("bcb:macro", ttl=3600)  # 1h — Selic muda raramente, IPCA mensal, USD diário
async def get_macro_indicators() -> dict:
    """
    Busca os 3 indicadores macro em paralelo.
    Retorna {selic: float, ipca12m: float, usd: float}.
    Cada um pode ser None se a série falhar.
    """
    selic_res, ipca_res, usd_res = await asyncio.gather(
        _fetch_serie(432, 1),
        _fetch_serie(433, 12),
        _fetch_serie(1, 1),
        return_exceptions=True,
    )

    out: dict[str, Any] = {"selic": None, "ipca12m": None, "usd": None}

    if not isinstance(selic_res, Exception) and selic_res:
        try:
            out["selic"] = float(selic_res[0]["valor"])
        except (KeyError, ValueError, IndexError):
            pass

    if not isinstance(ipca_res, Exception) and ipca_res:
        try:
            # IPCA acumulado 12 meses: produto dos fatores mensais
            fator = 1.0
            for m in ipca_res:
                fator *= 1 + float(m["valor"]) / 100
            out["ipca12m"] = round((fator - 1) * 100, 2)
        except (KeyError, ValueError):
            pass

    if not isinstance(usd_res, Exception) and usd_res:
        try:
            out["usd"] = float(usd_res[0]["valor"])
        except (KeyError, ValueError, IndexError):
            pass

    return out
