# 浏览器的来源。Playwright 自己那套 zip 下载在国内不可用：cdn.playwright.dev 会
# 307 到 Azure 签名 URL，从广州拉经常断；唯一的国内镜像 npmmirror 在 1.62.0 锁定的
# revision 1234 上只同步了 arm64，x64 的 chromium-linux.zip 是 404、
# chromium-headless-shell/1234/ 更是空目录（2026-08-19 实测）。
#
# 所以改走 Docker registry：官方镜像里已经装好了和 1.62.0 完全匹配的
# chromium(1234)、chromium-headless-shell(1234) 和 ffmpeg(1011)。这条通道在目标
# 服务器上已经验证可用（python:3.12-slim-bookworm 就是这么拉下来的），而且分层
# 可续传、不受 Playwright 那个 30 秒 socket 超时影响。
# 需要换国内 registry 镜像时覆盖这个参数即可，例如 --build-arg PLAYWRIGHT_IMAGE=<mirror>/playwright/python:v1.62.0-noble
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.62.0-noble

FROM ${PLAYWRIGHT_IMAGE} AS browsers
# 官方镜像还带着 firefox 和 webkit，本项目只用 chromium。必须在这一层就删掉：
# COPY 是独立分层，等复制过去再删是减不掉体积的。
RUN rm -rf /ms-playwright/firefox-* /ms-playwright/webkit-*

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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=browsers /ms-playwright /ms-playwright

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
    python -m playwright install-deps chromium; \
    python -c "import playwright.sync_api as api; pw = api.sync_playwright().start(); print('chromium ->', pw.chromium.executable_path); pw.stop()"; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --user-group --uid 10001 \
        --shell /usr/sbin/nologin runbow007; \
    install -d -o 10001 -g 10001 \
        /app/data /app/downloads /app/logs /app/browser-profile

USER runbow007

ENTRYPOINT ["runbow007"]
CMD ["--config", "/app/config.yaml", "check-config"]
