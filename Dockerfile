FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PRISM_DB_PATH=/app/data/prism.db
ENV PORT=8000

EXPOSE 8000

# Gunicorn with multiple sync workers; each worker is a separate process so
# SQLite WAL + BEGIN IMMEDIATE (see app/core/db.py) is what keeps rate-limit
# and budget admission race-free across workers, not just across threads.
# --threads gives per-worker concurrency for streaming responses, which hold
# a connection open for the duration of the SSE stream.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "4", \
     "--timeout", "60", "app.main:app"]
