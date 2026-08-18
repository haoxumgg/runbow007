FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

# 浏览器这一层必须排在 COPY src 之前。放在源码后面的话，改一行代码就让它失效，
# 每次部署都要重新从 Google CDN 拉约 300MB（chromium + headless shell + ffmpeg）。
# 服务器并不总能拉得动：2026-08-18 一次构建在这里重试了 68 分钟，最后被工作流
# 的 75 分钟上限打死，当天另有三次同样的挂起。分层之后源码变更只会重跑下面那条
# 便宜的 pip install。
#
# 版本必须跟 pyproject.toml 里的精确锁保持一致，否则后面的 pip install . 会把
# playwright 升级掉，而已经下好的浏览器是按版本存放的，运行时才会报"可执行文件
# 不存在"。下面装完项目后有一条断言把这种不一致变成构建期的显式失败。
ARG PLAYWRIGHT_VERSION=1.62.0
RUN python -m pip install --upgrade pip \
    && python -m pip install "playwright==${PLAYWRIGHT_VERSION}" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install . \
    && python -c "from importlib.metadata import version; v = version('playwright'); assert v == '${PLAYWRIGHT_VERSION}', f'pyproject 解析出 playwright {v}，与 Dockerfile 的 ARG PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION} 不一致；已下载的浏览器会对不上号'" \
    && useradd --create-home --user-group --uid 10001 \
        --shell /usr/sbin/nologin runbow007 \
    && install -d -o 10001 -g 10001 \
        /app/data /app/downloads /app/logs /app/browser-profile

USER runbow007

ENTRYPOINT ["runbow007"]
CMD ["--config", "/app/config.yaml", "check-config"]
