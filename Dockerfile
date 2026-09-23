FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/dp.db

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data && \
    groupadd --system app && \
    useradd --system --gid app --home-dir /app app && \
    chown -R app:app /data /app
USER app

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('APP_PORT','8000')+'/health').read()" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]
