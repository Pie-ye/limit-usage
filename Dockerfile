FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=50048 \
    DATABASE_PATH=/app/data/usage.db

# upower + dbus client so /api/host/ups can query host upowerd via the
# mounted system bus socket (see docker-compose.yml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends upower dbus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data

EXPOSE 50048

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:50048/api/health')"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "50048"]
