FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
# ONE worker on purpose: each worker would run its own scanner and send duplicate alerts.
CMD gunicorn --workers 1 --threads 8 --bind 0.0.0.0:${PORT} server:app
