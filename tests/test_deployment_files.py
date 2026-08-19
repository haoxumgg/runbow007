import re
from pathlib import Path

import yaml

from runbow007.config import TmsConfig
from runbow007.downloader import TmsDownloader

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_no_inbound_ports_and_persists_state():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    app = compose["services"]["app"]

    assert "ports" not in app
    assert app["init"] is True
    assert app["ipc"] == "host"
    volumes = set(app["volumes"])
    assert "./data:/app/data" in volumes
    assert "./downloads:/app/downloads" in volumes
    assert "./browser-profile:/app/browser-profile" in volumes
    assert app["environment"]["RUNBOW007_TMS_DOWNLOAD_TIMEOUT_SECONDS"] == "600"
    # 服务器只有 2 GiB：卡死的 Chromium 必须先撑爆容器，而不是整台机器。
    assert app["mem_limit"] == "1200m"


def test_browsers_come_from_the_playwright_image_not_the_zip_cdn():
    """浏览器必须来自 Playwright 官方镜像，不能再走 zip 下载。

    2026-08-19 实测：cdn.playwright.dev 会 307 到 Azure 签名 URL，从广州拉经常
    断，而 Playwright 自己的 socket 超时只有 30 秒（NET_DEFAULT_TIMEOUT = 3e4），
    抖一下就整个中止；唯一的国内镜像 npmmirror 在 1.62.0 锁定的 revision 1234 上
    只同步了 arm64，x64 是 404。改从官方镜像按 Docker 分层取，那条通道在目标服务
    器上是通的。
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY --from=browsers /ms-playwright /ms-playwright" in dockerfile
    # 只装系统依赖，绝不能再触发浏览器下载。
    assert "playwright install-deps chromium" in dockerfile
    assert "playwright install --with-deps" not in dockerfile
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in dockerfile
    # firefox/webkit 必须在来源层就删掉：COPY 是独立分层，下游删减不掉体积。
    firefox_at = dockerfile.index("rm -rf /ms-playwright/firefox-*")
    copy_at = dockerfile.index("COPY --from=browsers")
    assert firefox_at < copy_at, "必须在 COPY 之前裁掉用不到的浏览器"
    # 构建期就验证浏览器真的能被解析到，别等到线上才发现。
    assert "executable_path" in dockerfile


def test_build_sources_are_overridable_mirrors():
    """构建源必须是可覆盖的 build arg，不能写死。

    2026-08-19 广州的 ECS 上直连 pypi.org 拉一个 2.5 kB 的 metadata 要 12 秒，
    构建每次都死在半路；换成阿里云 ECS 内网源后同一批请求是 0.1-0.2 秒。但把
    镜像写死会让阿里云之外（别人的 CI、本地开发机）根本构建不出来，所以三个源
    都留成可覆盖参数，默认值只写在 Dockerfile 一处。
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    build_args = compose["services"]["app"]["build"]["args"]

    for arg in ("PLAYWRIGHT_IMAGE", "APT_MIRROR", "PIP_INDEX"):
        assert f"ARG {arg}" in dockerfile, f"Dockerfile 缺少 {arg}"
        assert arg in build_args, f"compose 没有透传 {arg}"
        # 留空表示「有同名环境变量就透传」，默认值不能在 compose 里再写一遍。
        assert build_args[arg] is None, f"{arg} 的默认值只应写在 Dockerfile 里"

    # 内网源只有 http，缺了 trusted-host pip 会直接拒绝。
    assert "--trusted-host" in dockerfile


def test_systemd_timers_are_persistent_and_use_shanghai_timezone():
    timer_dir = ROOT / "deploy" / "systemd"
    hourly = (timer_dir / "runbow007-hourly.timer").read_text(encoding="utf-8")
    arrival = (timer_dir / "runbow007-arrival.timer").read_text(encoding="utf-8")

    assert "*:05,35:00 Asia/Shanghai" in hourly
    assert "13:30:00 Asia/Shanghai" in arrival
    assert "Persistent=true" in hourly
    assert "Persistent=true" in arrival


def test_systemd_jobs_allow_full_download_window_and_alert_on_failure():
    timer_dir = ROOT / "deploy" / "systemd"
    deploy_script = (ROOT / "scripts" / "deploy-alinux3.sh").read_text(
        encoding="utf-8"
    )
    failure_script = (ROOT / "scripts" / "notify-failure-alinux3.sh").read_text(
        encoding="utf-8"
    )

    for name in ("runbow007-hourly.service", "runbow007-arrival.service"):
        service = (timer_dir / name).read_text(encoding="utf-8")
        # 必须小于 30 分钟的调度间隔：卡死的一轮要在下一个 slot 之前被收掉，
        # 否则下一轮只会撞上文件锁然后静默跳过。
        assert "TimeoutStartSec=28min" in service
        assert "SuccessExitStatus=3" in service
        assert "OnFailure=runbow007-failure@%n.service" in service

    failure_unit = (timer_dir / "runbow007-failure@.service").read_text(
        encoding="utf-8"
    )
    assert "notify-failure-alinux3.sh %i" in failure_unit
    assert "RUNBOW007_ENABLE_SENDING" in failure_script
    assert "notify-failure" in failure_script
    assert "runbow007-failure@.service" in deploy_script


def test_run_budget_and_unit_timeout_stay_inside_the_timer_interval():
    """必须始终满足：单轮下载预算 < systemd 超时 < 调度间隔。

    这三个数分别写在 downloader.py、.service 和 .timer 里，改任何一个都可能
    让慢的一轮盖住下一个 slot。而第二轮撞上文件锁只会以退出码 3 静默跳过
    （SuccessExitStatus=3），监控上完全看不出来，所以在这里锁死。
    """
    timer = (ROOT / "deploy" / "systemd" / "runbow007-hourly.timer").read_text(
        encoding="utf-8"
    )
    fire_minutes = re.search(r"OnCalendar=\*-\*-\* \*:([\d,]+):00", timer).group(1)
    minutes = sorted(int(value) for value in fire_minutes.split(","))
    assert len(minutes) == 2, "每小时两次触发才构成 30 分钟间隔"
    interval_seconds = (minutes[1] - minutes[0]) * 60
    assert interval_seconds == 30 * 60

    assert TmsDownloader._RUN_BUDGET_SECONDS < interval_seconds

    for name in ("runbow007-hourly.service", "runbow007-arrival.service"):
        service = (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")
        timeout = int(re.search(r"TimeoutStartSec=(\d+)min", service).group(1)) * 60
        assert TmsDownloader._RUN_BUDGET_SECONDS < timeout < interval_seconds


def test_retry_schedule_cannot_overrun_the_run_budget():
    """重试次数 × 单次硬上限 + 退避总和不能超过单轮预算。

    超了的话看门狗还没来得及打断，预算检查就已经把重试全砍掉，配置里写的
    重试次数就成了假的。
    """
    attempts = len(TmsDownloader._RETRY_DELAYS)
    backoff = sum(TmsDownloader._RETRY_DELAYS)
    worst_case = attempts * TmsConfig().attempt_timeout_seconds + backoff

    assert worst_case <= TmsDownloader._RUN_BUDGET_SECONDS


def test_deploy_reclaims_build_cache_without_failing_the_deploy():
    """每次部署都构建一次镜像，buildx 缓存从不自动回收。

    2026-08-17 实测：镜像本身 1.9GB，构建缓存却堆到 27.3GB，39GB 的盘用到 80%。
    清理必须跟在 build 后面，且自身失败不能让整个部署挂掉。
    """
    deploy_script = (ROOT / "scripts" / "deploy-alinux3.sh").read_text(encoding="utf-8")

    assert "docker builder prune" in deploy_script
    build_at = deploy_script.index("docker compose --project-directory")
    prune_at = deploy_script.index("docker builder prune")
    assert build_at < prune_at, "构建缓存清理必须在 build 之后"

    prune_block = deploy_script[prune_at : deploy_script.index("\n\n", prune_at)]
    assert "|| true" in prune_block, "清理失败不能中断部署"


def test_linux_runtime_defaults_to_dry_run():
    runtime = (ROOT / "deploy" / "runtime.env.example").read_text(encoding="utf-8")

    assert "RUNBOW007_ENABLE_SENDING=false" in runtime


def test_github_deploy_is_manual_and_safe_by_default():
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    trigger = workflow["on"]
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert set(trigger) == {"workflow_dispatch"}
    assert inputs["run_smoke_test"]["default"] == "true"
    assert inputs["enable_timers"]["default"] == "false"
    assert inputs["enable_sending"]["default"] == "false"
    assert inputs["feishu_test_rule"]["default"] == "R3"
    assert inputs["feishu_test_rule"]["options"] == [
        "R1",
        "R2",
        "R3",
        "R4",
        "R1,R2,R3,R4",
    ]
    assert inputs["feishu_test_orders"]["default"] == "0"
    assert inputs["feishu_test_orders"]["options"] == ["0", "3", "5", "all"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["deploy"]["timeout-minutes"] == "75"


def test_github_deploy_pins_host_key_and_never_enables_sending():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert "StrictHostKeyChecking=yes" in workflow
    assert "ServerAliveInterval=30" in workflow
    assert "ServerAliveCountMax=40" in workflow
    assert "DEPLOY_KNOWN_HOSTS" in workflow
    assert "workflow_dispatch" in workflow
    assert "RUNBOW007_ENABLE_SENDING=true" not in workflow
    assert "RUNBOW007_ENABLE_SENDING=true" not in remote_script


def test_github_deploy_requires_explicit_sending_and_keeps_smoke_dry():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert "inputs.enable_sending" in workflow
    assert 'enable_sending="${6:-false}"' in remote_script
    assert "RUNBOW007_RUNTIME_FILE=/dev/null" in remote_script
    assert "RUNBOW007_ENABLE_SENDING=$enable_sending" in remote_script
    assert "定时任务飞书发送开关已设置为" in remote_script


def test_feishu_manual_send_can_reuse_latest_excel_and_validates_options():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )
    send_script = (ROOT / "scripts" / "send-smoke-alinux3.sh").read_text(
        encoding="utf-8"
    )

    assert "feishu_test_orders" in workflow
    assert "复用服务器最新一次成功下载的 Excel" in remote_script
    assert '"$feishu_test_orders" != "3"' in remote_script
    assert '"$feishu_test_orders" != "5"' in remote_script
    assert '"$feishu_test_orders" != "all"' in remote_script
    assert '"$feishu_test_rule" != "R1"' in remote_script
    assert '"$feishu_test_rule" != "R1,R2,R3,R4"' in remote_script
    assert "RUNBOW007_RUNTIME_FILE" in send_script
    assert 'source "$runtime_file"' in send_script
    assert '--rules "$rule_code" --send --force-send' in send_script
    assert 'echo "使用最新测试文件: $latest_file"' in send_script
    assert 'if [[ "$order_count" != "all" ]]' in send_script


def test_actions_deploy_can_bootstrap_docker_on_alinux3():
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert '"${ID:-}" == "alinux"' in remote_script
    assert '"${VERSION_ID%%.*}" == "3"' in remote_script
    assert "dnf-plugin-releasever-adapter --repo alinux3-plus" in remote_script
    assert "docker-ce docker-ce-cli containerd.io" in remote_script
    assert "docker-buildx-plugin docker-compose-plugin" in remote_script
    assert "systemctl enable --now docker" in remote_script


def test_actions_deploy_can_bootstrap_docker_on_ubuntu_2404():
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert '"${ID:-}" == "ubuntu"' in remote_script
    assert '"${VERSION_ID:-}" == "24.04"' in remote_script
    assert "https://download.docker.com/linux/ubuntu/gpg" in remote_script
    assert "/etc/apt/sources.list.d/docker.sources" in remote_script
    assert "docker-buildx-plugin docker-compose-plugin" in remote_script

