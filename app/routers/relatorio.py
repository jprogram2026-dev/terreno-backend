"""Endpoint principal: gera o relatório completo."""
from fastapi import APIRouter, HTTPException, Query

from app.services import report

router = APIRouter(prefix="/api/relatorio", tags=["relatorio"])


@router.get("")
async def gerar_relatorio(
    cnae: str = Query(..., description="Chave do CNAE (cafeteria, padaria, ...)"),
    muni_id: int = Query(..., description="ID IBGE do município"),
    uf_id: int = Query(..., description="ID IBGE do estado"),
):
    """
    Gera relatório completo combinando todas as fontes em uma única chamada.

    Internamente dispara em paralelo:
    - IBGE: população, CEMPRE (município/UF/Brasil), PIB
    - BCB: Selic, IPCA, USD
    - Google Trends: interesse de busca regional

    Resposta é cacheada por 6 horas.
    """
    try:
        return await report.build_report(cnae, muni_id, uf_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")


@router.get("/catalogo")
async def get_catalogo():
    """Lista as categorias de negócio suportadas pela plataforma."""
    return {
        "categorias": [
            {"key": k, "nome": v["nome"], "cnae_classe": v["cnae_classe"]}
            for k, v in report.CNAE_CATALOG.items()
        ]
    }
