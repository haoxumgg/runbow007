FROM python:3.12-slim-bookworm

# 构建期镜像源。服务器在广州，直连 deb.debian.org / pypi.org 慢到会把构建拖垮：
# 2026-08-19 实测从 pypi.org 拉一个 2.5 kB 的 wheel metadata 要 12 秒，构建每次都
# 死在半路。改走阿里云 ECS 内网源后同一批请求是 0.1–0.2 秒（本项目就部署在阿里云
# 上，内网源还不消耗公网带宽）。
# 在阿里云之外构建时把对应参数覆盖成空字符串即可回到上游，例如：
#   docker compose build --build-arg APT_MIRROR= --build-arg PIP_INDEX= app
ARG APT_MIRROR=mirrors.cloud.aliyuncs.com
# 内网源只有 http，所以下面会自动为它补一个 --trusted-host，否则 pip 直接拒绝。
ARG PIP_INDEX=http://mirrors.cloud.aliyuncs.com/pypi/simple/
# Playwright 的浏览器二进制默认走上游 cdn.playwright.dev（这也是驱动里写死的默认
# 值）。阿里云不镜像它；npmmirror 虽然有 1.62.0 锁定的 revision 1234 目录，但
# 2026-08-19 实测里面只同步了 arm64，x64 的 chromium-linux.zip 是 404，
# chromium-headless-shell/1234/ 干脆是空目录。所以这里不设默认镜像——宁可慢，也不
# 给一个必然 404 的默认值。将来确认某个镜像站同步全了，用 PLAYWRIGHT_MIRROR=<host>
# 覆盖即可（会被设成 PLAYWRIGHT_DOWNLOAD_HOST）。
ARG PLAYWRIGHT_MIRROR=
# Playwright 下载浏览器时的 socket 超时，单位毫秒。它自己的默认值是
# NET_DEFAULT_TIMEOUT = 3e4，也就是连续 30 秒没收到数据就中止整个下载——从广州拉
# Azure CDN 抖动一下就会超，表现正是「卡在 playwright 下载」然后失败。放宽到 5 分钟，
# 慢没关系，别因为一次抖动前功尽弃。ARG 会自动作为环境变量暴露给下面的 RUN。
ARG PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=300000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        for source in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
            [ -f "$source" ] || continue; \
            sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
                "$source"; \
        done; \
    fi; \
    pip_args=""; \
    if [ -n "$PIP_INDEX" ]; then \
        pip_host="$(printf '%s' "$PIP_INDEX" | sed -E 's#^[a-z]+://##; s#/.*##')"; \
        pip_args="--index-url $PIP_INDEX --trusted-host $pip_host"; \
    fi; \
    python -m pip install --upgrade $pip_args pip; \
    python -m pip install $pip_args .; \
    if [ -n "$PLAYWRIGHT_MIRROR" ]; then \
        export PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_MIRROR"; \
    fi; \
    python -m playwright install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --user-group --uid 10001 \
        --shell /usr/sbin/nologin runbow007; \
    install -d -o 10001 -g 10001 \
        /app/data /app/downloads /app/logs /app/browser-profile

USER runbow007

ENTRYPOINT ["runbow007"]
CMD ["--config", "/app/config.yaml", "check-config"]
