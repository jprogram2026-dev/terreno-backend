"""
Cliente HTTP async compartilhado.

Centralizamos httpx aqui para:
- Reutilizar pool de conexões (performance)
- Aplicar timeout padrão consistente
- Logging unificado de erros
"""
import logging
from typing import Any, Optional

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)
_settings = get_settings()

# Cliente global criado no startup do FastAPI
_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    """Retorna o cliente global, criando se necessário."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_settings.HTTP_TIMEOUT, connect=5.0),
            headers={"User-Agent": "Terreno/0.2 (academic project)"},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    """Fecha o cliente no shutdown do FastAPI."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


async def fetch_json(
    url: str,
    params: dict | None = None,
    timeout: float | None = None,
    method: str = "GET",
    data: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """
    Requisição HTTP → JSON com tratamento de erro consistente.
    Por padrão faz GET; aceita POST quando method='POST' (usado por APIs como
    Overpass que recebem queries no body).

    Levanta httpx.HTTPStatusError em códigos 4xx/5xx.
    """
    client = await get_client()
    t = timeout if timeout is not None else _settings.HTTP_TIMEOUT
    log.debug("%s %s params=%s", method, url, params)
    if method.upper() == "POST":
        res = await client.post(url, params=params, data=data, headers=headers, timeout=t)
    else:
        res = await client.get(url, params=params, headers=headers, timeout=t)
    res.raise_for_status()
    return res.json()
