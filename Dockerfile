FROM mirror.gcr.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY src/ src/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "ai_agent_monitoring.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
