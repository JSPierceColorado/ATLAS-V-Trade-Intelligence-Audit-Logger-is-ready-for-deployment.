FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 atlas
COPY main.py strategy_analytics.py audit_intelligence.py ./
USER atlas

# Long-running worker, NOT a Railway cron job.
CMD ["python", "main.py"]
