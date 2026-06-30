"""
Endpoint para mapa de calor de empresas — consulta OpenStreetMap.
"""
import logging

from fastapi import APIRouter, HTTPException, Query

from app.services import ibge, osm
from app.services.report import CNAE_CATALOG

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mapa-empresas", tags=["mapa"])


@router.get("")
async def get_mapa_empresas(
    uf_id: int = Query(..., description="Código IBGE da UF"),
    muni_id: int = Query(..., description="Código IBGE do município"),
    cnae: str = Query(..., description="Chave do CNAE (ex: cafeteria, restaurante)"),
):
    """
    Retorna estabelecimentos do CNAE no município, com coordenadas, pra
    visualização em mapa de calor no frontend.

    Fonte: OpenStreetMap (Nominatim + Overpass API). Cobertura varia por região
    — o frontend exibe aviso quando há poucos pontos.

    Response:
        {
          "places":  [{"lat": ..., "lon": ..., "nome": ...}, ...],
          "center":  [lat, lon],
          "bbox":    [s, w, n, e],
          "total":   42,
          "coverage_note": null | "..."
        }
    """
    # Valida CNAE
    cnae_info = CNAE_CATALOG.get(cnae)
    if not cnae_info:
        raise HTTPException(status_code=404, detail=f"CNAE '{cnae}' não cadastrado")

    # Resolve nome do município
    try:
        muni = await ibge.get_municipio(muni_id)
    except Exception as e:
        log.warning("Falha ao buscar município %s: %s", muni_id, e)
        raise HTTPException(status_code=502, detail="Erro ao consultar IBGE")

    if not muni or not muni.get("nome"):
        raise HTTPException(status_code=404, detail=f"Município {muni_id} não encontrado no IBGE")

    # Consulta OSM
    result = await osm.fetch_places(
        muni["nome"],
        muni.get("uf_sigla") or "",
        cnae_info["cnae_classe"],
    )

    return {
        "municipio": muni["nome"],
        "uf": muni.get("uf_sigla"),
        "cnae_classe": cnae_info["cnae_classe"],
        "cnae_nome": cnae_info["nome"],
        **result,
    }
