"""
Service RAIS via Base dos Dados (BigQuery público).

Acessa microdados RAIS através do datalake público mantido pela ONG
Base dos Dados (basedosdados.org), que disponibiliza ~250GB de RAIS
tratada em BigQuery.

CONFIGURAÇÃO:
1. Criar projeto gratuito em https://console.cloud.google.com
2. Ativar API BigQuery
3. Criar service account com role "BigQuery User"
4. Baixar credenciais JSON e apontar GOOGLE_APPLICATION_CREDENTIALS no .env
5. (alternativa) Para projetos pessoais, login via gcloud:
   `gcloud auth application-default login`

Free tier: 1 TB de query por mês. Nossas queries usam ~100 MB cada (filtradas
por município+UF+CNAE+ano), então cabe muito uso no free tier.

Se as credenciais não estiverem configuradas, o service degrada graciosamente:
get_emprego_setor() retorna None e o resto do relatório segue normalmente.
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.core.cache import cached

log = logging.getLogger(__name__)

# Import condicional — a dependência é opcional
try:
    from google.cloud import bigquery
    BQ_AVAILABLE = True
except ImportError:
    BQ_AVAILABLE = False
    bigquery = None
    log.info("google-cloud-bigquery não instalado — RAIS desativada")

# Executor dedicado para chamadas síncronas BigQuery.
# 2 workers — BigQuery aguenta paralelismo bem.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bigquery")

_bq_client: Optional["bigquery.Client"] = None


def _get_client() -> Optional["bigquery.Client"]:
    """Cria cliente BigQuery sob demanda. Retorna None se não configurado."""
    global _bq_client
    if not BQ_AVAILABLE:
        return None
    if _bq_client is not None:
        return _bq_client
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    project = os.getenv("GCP_PROJECT_ID")
    if not (creds_path or project):
        log.info("RAIS: GOOGLE_APPLICATION_CREDENTIALS ou GCP_PROJECT_ID não setados")
        return None
    try:
        _bq_client = bigquery.Client(project=project) if project else bigquery.Client()
        log.info("BigQuery cliente inicializado (projeto: %s)", _bq_client.project)
        return _bq_client
    except Exception as e:
        log.warning("Falha ao inicializar BigQuery: %s", e)
        return None


def is_available() -> bool:
    """Indica se RAIS está utilizável (lib instalada + credenciais configuradas)."""
    return _get_client() is not None


# ============================================================================
# Query principal: série histórica de emprego no setor por município
# ============================================================================

# Query parametrizada — filtra cedo para minimizar bytes processados.
# A tabela é particionada por ano e sigla_uf, então esses filtros são essenciais.
# Na tabela microdados_estabelecimentos da Base dos Dados, cada linha = 1 estabelecimento
# em 1 ano, então COUNT(*) já dá o número de estabelecimentos.
_QUERY_EMPREGO = """
SELECT
  ano,
  SUM(CAST(quantidade_vinculos_ativos AS INT64)) AS vinculos_ativos,
  COUNT(*) AS estabelecimentos
FROM `basedosdados.br_me_rais.microdados_estabelecimentos`
WHERE sigla_uf = @sigla_uf
  AND id_municipio = @id_municipio
  AND SUBSTR(cnae_2_subclasse, 1, 4) = @cnae_classe
  AND ano BETWEEN @ano_inicio AND @ano_fim
  AND quantidade_vinculos_ativos IS NOT NULL
GROUP BY ano
ORDER BY ano
"""


def _run_query_sync(sigla_uf: str, muni_id: int, cnae_classe: str,
                    ano_inicio: int, ano_fim: int) -> list[dict]:
    """Versão síncrona da query — roda no executor."""
    client = _get_client()
    if client is None:
        raise RuntimeError("BigQuery não configurado")

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("sigla_uf", "STRING", sigla_uf),
            bigquery.ScalarQueryParameter("id_municipio", "STRING", str(muni_id)),
            bigquery.ScalarQueryParameter("cnae_classe", "STRING", cnae_classe),
            bigquery.ScalarQueryParameter("ano_inicio", "INT64", ano_inicio),
            bigquery.ScalarQueryParameter("ano_fim", "INT64", ano_fim),
        ],
        # Limite de bytes para evitar surpresa de cobrança.
        # A tabela RAIS tem ~1GB por query mesmo filtrada — bem dentro do free tier (1TB/mês).
        maximum_bytes_billed=2 * 1024 * 1024 * 1024,  # 2 GB
        use_query_cache=True,
    )

    log.debug("RAIS query: uf=%s muni=%s cnae=%s anos=%d-%d",
              sigla_uf, muni_id, cnae_classe, ano_inicio, ano_fim)

    rows = list(client.query(_QUERY_EMPREGO, job_config=job_config).result())
    return [
        {
            "ano": int(r.ano),
            "vinculos_ativos": int(r.vinculos_ativos or 0),
            "estabelecimentos": int(r.estabelecimentos or 0),
        }
        for r in rows
    ]


@cached("rais:emprego", ttl=86400)  # 24h — dados anuais, sem necessidade de refresh frequente
async def get_emprego_setor(
    sigla_uf: str,
    muni_id: int,
    cnae_classe: str,
    ano_inicio: int = 2018,
    ano_fim: int = 2023,
) -> Optional[dict]:
    """
    Série histórica de empregos formais no setor para o município.

    Retorna {
        anos: [{ano, vinculos_ativos, estabelecimentos}, ...],
        variacao_pct: float (variação total no período),
        ano_inicial: int,
        ano_final: int
    }
    ou None se BigQuery não estiver configurado / falhar.
    """
    if not is_available():
        return None

    try:
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            _executor, _run_query_sync,
            sigla_uf, muni_id, cnae_classe, ano_inicio, ano_fim
        )
    except Exception as e:
        log.warning("RAIS query falhou: %s", e)
        return None

    if not rows:
        return None

    variacao = None
    if len(rows) >= 2 and rows[0]["vinculos_ativos"] > 0:
        variacao = round(
            ((rows[-1]["vinculos_ativos"] - rows[0]["vinculos_ativos"])
             / rows[0]["vinculos_ativos"]) * 100, 1
        )

    return {
        "anos": rows,
        "variacao_pct": variacao,
        "ano_inicial": rows[0]["ano"],
        "ano_final": rows[-1]["ano"],
    }


async def shutdown() -> None:
    """Encerra o executor no shutdown."""
    _executor.shutdown(wait=False, cancel_futures=True)
