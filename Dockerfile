FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock .python-version /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN uv sync --frozen --no-dev --no-install-project

RUN /opt/venv/bin/python -m playwright install --with-deps chromium

COPY . /app

ENV PATH="/opt/venv/bin:$PATH"

CMD ["python", "main.py"]
