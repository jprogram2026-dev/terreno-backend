"""
Cache TTL em memória usando cachetools.

Decisão arquitetural: em vez de Redis, usamos TTLCache em processo.
Para o volume de um projeto acadêmico (centenas de requests/dia), isso é mais
que suficiente, elimina infra externa e não compromete a defesa do projeto.

Em produção real com múltiplas instâncias, troque para Redis (interface compatível).
"""
import hashlib
import json
import logging
from functools import wraps
from typing import Any, Callable

from cachetools import TTLCache

from app.core.config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()
_cache: TTLCache = TTLCache(
    maxsize=_settings.CACHE_MAX_SIZE,
    ttl=_settings.CACHE_TTL_SECONDS,
)


def make_cache_key(prefix: str, *args, **kwargs) -> str:
    """
    Gera chave de cache estável a partir do prefixo e argumentos.
    Hash MD5 garante chaves curtas mesmo para argumentos longos.
    """
    raw = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def cached(prefix: str, ttl: int | None = None):
    """
    Decorator para cachear o resultado de uma função async.
    Uso:
        @cached("ibge:pop")
        async def get_population(muni_id: str): ...
    """
    def decorator(fn: Callable):
        @wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            key = make_cache_key(prefix, *args, **kwargs)
            if key in _cache:
                log.debug("Cache HIT: %s", key)
                return _cache[key]
            log.debug("Cache MISS: %s", key)
            result = await fn(*args, **kwargs)
            if result is not None:
                _cache[key] = result
            return result
        return wrapper
    return decorator


def clear_cache() -> int:
    """Limpa todo o cache. Retorna número de chaves removidas."""
    count = len(_cache)
    _cache.clear()
    return count


def cache_stats() -> dict:
    """Estatísticas do cache para endpoint de health/debug."""
    return {
        "size": len(_cache),
        "max_size": _cache.maxsize,
        "ttl_seconds": _cache.ttl,
    }
