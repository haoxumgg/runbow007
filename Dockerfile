FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --user-group --uid 10001 \
        --shell /usr/sbin/nologin runbow007 \
    && install -d -o 10001 -g 10001 \
        /app/data /app/downloads /app/logs /app/browser-profile

USER runbow007

ENTRYPOINT ["runbow007"]
CMD ["--config", "/app/config.yaml", "check-config"]
