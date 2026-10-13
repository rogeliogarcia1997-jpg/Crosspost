FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
USER app
EXPOSE 8000
# Override the command for the worker: ["python", "-m", "crosspost.cli", "worker"]
# PORT is set by platforms such as Render; defaults to 8000 elsewhere.
CMD ["sh", "-c", "exec uvicorn crosspost.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
