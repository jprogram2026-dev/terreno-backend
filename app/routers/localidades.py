"""Endpoints para localidades brasileiras (proxy IBGE)."""
from fastapi import APIRouter, HTTPException

from app.services import ibge

router = APIRouter(prefix="/api/localidades", tags=["localidades"])


@router.get("/estados")
async def get_estados():
    """Lista os 27 estados brasileiros."""
    try:
        return {"estados": await ibge.list_states()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IBGE indisponível: {e}")


@router.get("/estados/{uf_id}/municipios")
async def get_municipios(uf_id: int):
    """Lista municípios de um estado pelo ID IBGE."""
    try:
        return {"uf_id": uf_id, "municipios": await ibge.list_municipalities(uf_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IBGE indisponível: {e}")
