# Terreno — Backend

Backend FastAPI da plataforma Terreno. Agrega dados públicos brasileiros (IBGE, BCB, Google Trends) e expõe um endpoint único de relatório para o frontend.

## Por que backend?

O frontend HTML estático já conseguia integrar IBGE e BCB diretamente (CORS aberto). O backend foi criado para:

1. **Google Trends** — `pytrends` é scraping server-side, impossível direto do navegador
2. **RAIS via BigQuery** — credenciais Google não devem ficar no front
3. **Cache centralizado** — evita martelar APIs públicas a cada acesso
4. **Lógica de negócio em um lugar** — cálculo de indicadores derivados em Python (testável)
5. **Preparado para extensão** — facilita adicionar mais fontes (dump CNPJ, etc.)

## Fontes integradas

| Fonte | Granularidade | Status |
|---|---|---|
| **IBGE Localidades** | Estados, municípios | ✓ Sempre disponível |
| **IBGE SIDRA 6579** (População) | Município | ✓ Sempre disponível |
| **IBGE SIDRA 9418** (CEMPRE) | Município + CNAE classe | ✓ Sempre disponível |
| **IBGE SIDRA 5938** (PIB) | Município | ✓ Sempre disponível |
| **BCB SGS** (Selic, IPCA, USD) | Nacional | ✓ Sempre disponível |
| **Google Trends** | UF + termo | ✓ Sempre disponível (pytrends, sujeito a quebra) |
| **RAIS via Base dos Dados** | Município + CNAE classe, série histórica | ✓ Opcional (requer GCP) |

## Rodando local

```bash
# 1. Dependências
pip install -r requirements.txt

# 2. Variáveis de ambiente
cp .env.example .env
# Edite .env se necessário (CORS, TTL de cache, etc.)

# 3. Servidor de desenvolvimento
uvicorn app.main:app --reload --port 8000
```

Documentação interativa em `http://localhost:8000/docs` (Swagger UI).

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| GET | `/api/health` | Status simples |
| GET | `/api/info` | Estado interno, cache, metadados SIDRA |
| GET | `/api/localidades/estados` | Lista 27 estados |
| GET | `/api/localidades/estados/{uf_id}/municipios` | Municípios de um estado |
| GET | `/api/relatorio/catalogo` | Categorias de negócio suportadas |
| GET | `/api/relatorio?cnae=cafeteria&muni_id=3550308&uf_id=35` | Relatório completo |
| POST | `/api/cache/clear` | Limpa cache (útil em dev) |

### Exemplo de chamada do relatório

```bash
curl 'http://localhost:8000/api/relatorio?cnae=padaria&muni_id=3550308&uf_id=35' | jq
```

Resposta inclui:
- `indicators`: indicadores prontos para renderizar (densidade, tendência, sazonalidade, demog_score)
- `cempre`: empresas e pessoal ocupado nos 3 níveis (município/UF/Brasil)
- `pib`: PIB e PIB per capita do município
- `trend`: série de 12 meses do Google Trends + queries relacionadas
- `macro`: Selic, IPCA 12m, USD
- `sources_status`: status de cada fonte (ok ou erro)

## Arquitetura

```
app/
├── main.py              # Entrypoint FastAPI + CORS + lifespan
├── core/
│   ├── config.py        # Settings via .env
│   ├── http.py          # httpx client compartilhado
│   └── cache.py         # TTLCache (em memória)
├── services/
│   ├── ibge.py          # Localidades, população, CEMPRE, PIB
│   ├── bcb.py           # Indicadores macro
│   ├── trends.py        # Google Trends (pytrends)
│   └── report.py        # Orchestrator
└── routers/
    ├── sistema.py       # /api/health, /api/info
    ├── localidades.py   # /api/localidades/*
    └── relatorio.py     # /api/relatorio
```

### Cache

TTLCache em processo, decorator `@cached(prefix, ttl)`. TTLs definidos:

- Estados/municípios: 24h (raramente mudam)
- CEMPRE / PIB: 6h
- BCB macro: 1h
- Google Trends: 12h
- Relatório agregado: 6h

Para produção com múltiplas instâncias, troque por Redis (interface compatível).

## Deploy

### Render.com (recomendado para acadêmico — free tier)

1. Push do código para GitHub
2. Render → New Web Service → conecte o repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Adicione variável `ALLOWED_ORIGINS` com o domínio do seu frontend

⚠️ Free tier hiberna após 15min sem tráfego — primeira requisição depois demora ~30s.

### Fly.io (sem hibernação)

```bash
fly launch  # detecta o Dockerfile
fly deploy
```

### Docker local

```bash
docker build -t terreno-backend .
docker run -p 8000:8000 --env-file .env terreno-backend
```

## Integrando o frontend

No arquivo `terreno-plataforma.html`, substituir as constantes de URL por:

```js
const API_BASE = "http://localhost:8000/api";  // ou URL pública após deploy

// Antes:
// fetch("https://servicodados.ibge.gov.br/api/v1/localidades/estados")
// Depois:
fetch(`${API_BASE}/localidades/estados`)

// Antes: 6 chamadas paralelas a IBGE/BCB/Wikipedia
// Depois: 1 chamada agregada
fetch(`${API_BASE}/relatorio?cnae=${cnae}&muni_id=${muni}&uf_id=${uf}`)
```

## Configurando RAIS (opcional)

A integração RAIS via Base dos Dados/BigQuery é opcional. Sem ela, o relatório continua funcionando normalmente, apenas sem o indicador de "Tendência de emprego no setor" (5 anos de série histórica).

### Passos para ativar

1. **Criar projeto GCP gratuito** em https://console.cloud.google.com (sem cartão de crédito)
2. **Ativar API BigQuery** no projeto criado
3. **Autenticar** — duas opções:

**Opção A — Application Default Credentials (mais simples, uso local):**
```bash
# Instale o gcloud CLI primeiro
gcloud auth application-default login
```
E no `.env`:
```
GCP_PROJECT_ID=seu-projeto-id
```

**Opção B — Service Account (recomendado para deploy):**
- Crie uma service account com role "BigQuery User"
- Baixe o JSON de credenciais
- No `.env`:
```
GOOGLE_APPLICATION_CREDENTIALS=/caminho/para/credentials.json
```

### Quanto custa?

Free tier do BigQuery = 1 TB de query/mês. Cada consulta da Terreno processa ~100 MB (filtrada por município + UF + CNAE). Suporta ~10.000 relatórios/mês no free tier — muito além do necessário para um projeto acadêmico.

A query usa `maximum_bytes_billed=500MB` como segurança contra surpresas de cobrança.

## Extensões futuras

### Dump CNPJ da Receita Federal

Use o projeto open-source [cnpj-sqlite](https://github.com/rictom/cnpj-sqlite) ou [cnpj-postgres](https://github.com/cnpj-brasil/cnpj-postgres) para ingestão. Depois exponha endpoints como `/api/cnpj/by-cnae?cnae=5611203&municipio=3550308`.

Requer ~50 GB de storage e ~3 horas de ingestão inicial. Pode ser feito num PostgreSQL grátis (Neon ou Supabase).

## Limitações conhecidas

- **pytrends quebra periodicamente** quando o Google muda o frontend. Atualize a lib (`pip install -U pytrends`) ou troque por SerpAPI.
- **Cache em memória não compartilha entre instâncias**. Para deploy com múltiplas réplicas, troque por Redis.
- **CEMPRE tem granularidade de classe CNAE (4 dígitos)** — não distingue subclasses como "cafeteria" vs "restaurante" (ambas estão em 5611).
