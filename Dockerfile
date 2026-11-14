FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY skills /app/skills

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

EXPOSE 8080

CMD ["uvicorn", "src.telegram_server:app", "--host", "0.0.0.0", "--port", "8080"]
