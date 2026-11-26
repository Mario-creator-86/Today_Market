FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 LOG_FORMAT=json
WORKDIR /app
COPY pyproject.toml ./
COPY backend ./backend
# Web Push and OpenTelemetry export are optional extras; both stay inactive until configured in .env.
RUN pip install --no-cache-dir ".[notifications,otel]"
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
# The app installs JSON logging on import and routes uvicorn's loggers through it.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
