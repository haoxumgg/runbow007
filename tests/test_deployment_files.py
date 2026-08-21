from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_batch_job_has_no_inbound_ports_and_persists_state():
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


def test_compose_serves_the_manual_upload_page_as_a_long_running_service():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    web = compose["services"]["web"]

    assert web["command"] == ["--config", "/app/config.yaml", "web"]
    assert web["restart"] == "unless-stopped"
    assert web["ports"] == ["${RUNBOW007_WEB_PORT:-8080}:8080"]
    # 上传页和定时任务共用同一个数据库和下载目录，去重记录才连得上。
    volumes = set(web["volumes"])
    assert "./data:/app/data" in volumes
    assert "./downloads:/app/downloads" in volumes
    # 页面不开浏览器，内存上限要给 app 留出余量：整台机器只有 2 GiB。
    assert web["mem_limit"] == "600m"


def test_manual_upload_service_is_installed_and_started_by_the_deploy():
    unit = (ROOT / "deploy" / "systemd" / "runbow007-web.service").read_text(
        encoding="utf-8"
    )
    deploy_script = (ROOT / "scripts" / "deploy-alinux3.sh").read_text(encoding="utf-8")
    web_script = (ROOT / "scripts" / "web-alinux3.sh").read_text(encoding="utf-8")

    assert "RemainAfterExit=yes" in unit
    assert "web-alinux3.sh start" in unit
    assert "web-alinux3.sh stop" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "runbow007-web.service /etc/systemd/system/" in deploy_script
    assert "systemctl enable runbow007-web.service" in deploy_script
    # 必须让 systemd 自己拉起来：直接调脚本会让 unit 停在 inactive，
    # 之后 `systemctl stop` 静默失效，容器还在跑。
    assert "systemctl restart runbow007-web.service" in deploy_script
    assert "./scripts/web-alinux3.sh start" not in deploy_script
    assert "compose up -d web" in web_script
    assert 'source "$runtime_file"' in web_script


def test_tms_download_timers_stay_off_until_switched_on_by_hand():
    """自动下载经常取不到数据，定时器默认必须是关的。

    只是"不启用"不够：上一次部署留下的 enabled 状态会原样活下来，所以部署脚本
    要主动 disable，再由 timers-alinux3.sh 手动控制开关。
    """
    deploy_script = (ROOT / "scripts" / "deploy-alinux3.sh").read_text(encoding="utf-8")
    actions_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )
    toggle = (ROOT / "scripts" / "timers-alinux3.sh").read_text(encoding="utf-8")

    assert (
        "systemctl disable --now runbow007-hourly.timer runbow007-arrival.timer"
        in deploy_script
    )
    assert "./scripts/timers-alinux3.sh on" in actions_script
    assert "./scripts/timers-alinux3.sh off" in actions_script
    assert 'systemctl enable --now "${timers[@]}"' in toggle
    assert 'systemctl disable --now "${timers[@]}"' in toggle


def test_systemd_timers_are_persistent_and_use_shanghai_timezone():
    timer_dir = ROOT / "deploy" / "systemd"
    hourly = (timer_dir / "runbow007-hourly.timer").read_text(encoding="utf-8")
    arrival = (timer_dir / "runbow007-arrival.timer").read_text(encoding="utf-8")

    assert "*:05:00 Asia/Shanghai" in hourly
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
        # 必须小于每小时一次的调度间隔：卡死的一轮要在下一个整点之前被收掉，
        # 否则下一轮只会撞上文件锁然后静默跳过。
        assert "TimeoutStartSec=55min" in service
        assert "SuccessExitStatus=3" in service
        assert "OnFailure=runbow007-failure@%n.service" in service

    failure_unit = (timer_dir / "runbow007-failure@.service").read_text(
        encoding="utf-8"
    )
    assert "notify-failure-alinux3.sh %i" in failure_unit
    assert "RUNBOW007_ENABLE_SENDING" in failure_script
    assert "notify-failure" in failure_script
    assert "runbow007-failure@.service" in deploy_script


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
    assert "RUNBOW007_WEB_PORT=8080" in runtime


def test_github_deploy_is_manual_and_safe_by_default():
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    trigger = workflow["on"]
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert set(trigger) == {"workflow_dispatch"}
    # TMS 下载不稳定，部署不该被一次取不到数的演练拖垮。
    assert inputs["run_smoke_test"]["default"] == "false"
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

