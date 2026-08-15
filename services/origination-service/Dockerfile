FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Build context is the REPO ROOT (docker-compose.yml), not this directory, so every COPY
# source below is repo-relative. The context moved so the image can carry rag_eval/ — see
# the COPY at the bottom.
COPY services/origination-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/origination-service/app ./app

# rag_eval/ is a repo-root package (retrieval eval harness, ADR 0007) and this is its only
# in-platform consumer. WORKDIR is /app and cwd is on sys.path, so landing it at /app/rag_eval
# is what makes `import rag_eval` resolve — no PYTHONPATH, no repo-wide venv. Copied rather
# than bind-mounted because it is code: a mount would make the import work under compose only.
# It adds no dependency — the package is stdlib-only, and boto3 is imported lazily inside
# BedrockEmbedder. Never close this by copying the modules into app/ instead: that reproduces
# the per-service redactor duplication `redactor-drift` exists to police.
COPY rag_eval ./rag_eval

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
