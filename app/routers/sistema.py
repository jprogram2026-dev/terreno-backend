"""Endpoints de saúde, métricas e debug."""
from fastapi import APIRouter

from app.core.cache import cache_stats, clear_cache
from app.services.ibge import discover_sidra_metadata
from app.services import rais

router = APIRouter(prefix="/api", tags=["sistema"])


@router.get("/health")
async def health():
    """Status simples para monitoramento."""
    return {"status": "ok"}


@router.get("/info")
async def info():
    """Estado interno: metadados SIDRA descobertos, estatísticas de cache."""
    meta = await discover_sidra_metadata()
    cempre_meta = meta.get("cempre") or {}
    pib_meta = meta.get("pib_munic") or {}
    return {
        "cache": cache_stats(),
        "sidra_metadata": {
            "cempre_variavel_empresas": cempre_meta.get("var_empresas"),
            "cempre_variavel_pessoal": cempre_meta.get("var_pessoal"),
            "cempre_classif_cnae_id": (cempre_meta.get("classif_cnae") or {}).get("id"),
            "pib_variavel": pib_meta.get("var_pib"),
            "pib_percapita_variavel": pib_meta.get("var_pib_percap"),
        },
        "rais_bigquery_available": rais.is_available(),
    }


@router.post("/cache/clear")
async def cache_clear():
    """Limpa todo o cache. Útil para testes ou após atualização de dados."""
    removed = clear_cache()
    return {"removed_keys": removed}
