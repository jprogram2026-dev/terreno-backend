"""
Configuração central da aplicação.
Carrega variáveis de ambiente e expõe constantes usadas em toda a app.
"""
import os
import logging
from functools import lru_cache
from typing import List

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Configurações da aplicação carregadas do .env."""

    ALLOWED_ORIGINS: List[str] = [
        o.strip() for o in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:3000,http://localhost:8000,http://127.0.0.1:5500"
        ).split(",") if o.strip()
    ]

    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "21600"))
    CACHE_MAX_SIZE: int = int(os.getenv("CACHE_MAX_SIZE", "1024"))

    PYTRENDS_PROXY: str = os.getenv("PYTRENDS_PROXY", "")

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # URLs das APIs externas (centralizado para fácil manutenção)
    IBGE_LOCALIDADES = "https://servicodados.ibge.gov.br/api/v1/localidades"
    IBGE_SIDRA = "https://servicodados.ibge.gov.br/api/v3/agregados"
    BCB_API = "https://api.bcb.gov.br/dados/serie"
    IPEA_API = "http://www.ipeadata.gov.br/api/odata4"

    # Timeouts em segundos para chamadas externas
    HTTP_TIMEOUT: float = 15.0
    HTTP_TIMEOUT_HEAVY: float = 30.0  # Para SIDRA (lenta) e pytrends


@lru_cache
def get_settings() -> Settings:
    """Singleton de configurações."""
    return Settings()


def setup_logging() -> None:
    """Configura logging com formato consistente."""
    s = get_settings()
    logging.basicConfig(
        level=getattr(logging, s.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # pytrends é verboso demais por padrão
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
