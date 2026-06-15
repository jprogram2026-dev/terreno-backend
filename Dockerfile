FROM python:3.12-slim

WORKDIR /app

# Dependências de sistema mínimas (pytrends precisa de SSL atualizado)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python primeiro para cache de camada
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY app/ ./app/

# Render/Fly.io vão sobrescrever isso; mas é bom default
ENV PORT=8000
EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
