FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
RUN pip install --no-cache-dir --upgrade pip uv
RUN uv pip install --system --no-cache .

COPY app.py /app/app.py
COPY frontend /app/frontend
COPY workflows /app/workflows
COPY scripts /app/scripts

EXPOSE 8091

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8091"]
