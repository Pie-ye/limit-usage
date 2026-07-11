FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=50048 \
    DATABASE_PATH=/app/data/usage.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data

EXPOSE 50048

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "50048"]
