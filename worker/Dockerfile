FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "celery -A celery_app worker --loglevel=info --concurrency=${CELERY_CONCURRENCY:-2} --max-tasks-per-child=${MAX_TASKS_PER_CHILD:-10}"]
