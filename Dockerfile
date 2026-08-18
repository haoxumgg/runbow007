FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

# 浏览器单独成层，且放在 COPY src 之前——这一层只随 playwright 版本变化。
#
# 之前 COPY src 排在 playwright install 前面，于是改一行代码就要把 114 MB 的
# Chrome Headless Shell 重下一遍。网络快的时候（2026-08-17 实测 6 秒、约 20 MB/s）
# 没人察觉；8-18 服务器到 cdn.playwright.dev 掉到约 107 KB/s，光这一步就跑了
# 64 分钟，连着两次部署都撞穿工作流 75 分钟超时，新代码一次都没上线。
#
# 版本必须和 pyproject.toml 里的钉子一致，test_deployment_files.py 会守着这一点：
# 不一致的话下面 pip install . 会换掉 playwright，浏览器就和库对不上了。
RUN python -m pip install --upgrade pip \
    && python -m pip install "playwright==1.62.0" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install . \
    && useradd --create-home --user-group --uid 10001 \
        --shell /usr/sbin/nologin runbow007 \
    && install -d -o 10001 -g 10001 \
        /app/data /app/downloads /app/logs /app/browser-profile

USER runbow007

ENTRYPOINT ["runbow007"]
CMD ["--config", "/app/config.yaml", "check-config"]
