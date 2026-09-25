FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Cache package installation in a dedicated layer
COPY routing-policy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root application user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

COPY --chown=appuser:appuser routing-policy/app ./app
COPY --chown=appuser:appuser routing-policy/policy ./policy
COPY --chown=appuser:appuser catalog ./catalog

USER appuser

EXPOSE 50100

CMD ["python", "-m", "app.main"]
