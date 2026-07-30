# SRJ Wikidata MCP Server — Docker image for Render deploy.
# Mirrors srj-mcp: python 3.12-slim, deps first for layer caching, single worker.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src

ENV PORT=8000
EXPOSE 8000

# Workers=1 keeps both the in-memory OAuth provider and the in-memory plan store
# coherent. Scaling horizontally would silently break batch approval: a plan
# created on one worker would not exist on another.
CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT} --workers 1"]
