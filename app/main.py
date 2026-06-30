"""
Terreno — Backend FastAPI.

Ponto de entrada da aplicação. Para rodar localmente:

    pip install -r requirements.txt
    cp .env.example .env
    uvicorn app.main:app --reload --port 8000

Documentação interativa em http://localhost:8000/docs
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings, setup_logging
from app.core.http import close_client
from app.routers import localidades, mapa, relatorio, sistema
from app.services import rais, trends
from app.services.ibge import discover_sidra_metadata

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hooks de startup e shutdown."""
    log.info("Iniciando Terreno backend…")
    try:
        # Descobre metadados SIDRA em background (não bloqueia startup)
        await discover_sidra_metadata()
        log.info("Metadados SIDRA descobertos com sucesso")
    except Exception as e:
        log.warning("Falha ao descobrir metadados SIDRA no startup: %s", e)

    yield

    log.info("Encerrando Terreno backend…")
    await close_client()
    await trends.shutdown()
    await rais.shutdown()


app = FastAPI(
    title="Terreno API",
    description=(
        "Backend da plataforma Terreno — agrega dados públicos brasileiros "
        "(IBGE, BCB, Google Trends) para estudo de viabilidade de negócios."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

settings = get_settings()

# CORS para o frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Registra routers
app.include_router(sistema.router)
app.include_router(localidades.router)
app.include_router(relatorio.router)
app.include_router(mapa.router)


@app.get("/", include_in_schema=False)
async def root():
    """Raiz — aponta para a documentação."""
    return {
        "name": "Terreno API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "/api/health",
            "/api/info",
            "/api/localidades/estados",
            "/api/localidades/estados/{uf_id}/municipios",
            "/api/relatorio?cnae=cafeteria&muni_id=3550308&uf_id=35",
            "/api/relatorio/catalogo",
        ],
    }
