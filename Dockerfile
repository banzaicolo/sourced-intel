# ---------------------------------------------------------------------------
# Sourced Intel image
#
# Build:
#   docker compose build
#
# For a faster build behind a slow PyPI connection, override the index:
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# ---------------------------------------------------------------------------
FROM python:3.14-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first: this layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY src ./src
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8501 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
