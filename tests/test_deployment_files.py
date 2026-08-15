from pathlib import Path

import yaml

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
    assert app["environment"]["RUNBOW007_TMS_DOWNLOAD_TIMEOUT_SECONDS"] == "1200"


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
        assert "TimeoutStartSec=75min" in service
        assert "SuccessExitStatus=3" in service
        assert "OnFailure=runbow007-failure@%n.service" in service

    failure_unit = (timer_dir / "runbow007-failure@.service").read_text(
        encoding="utf-8"
    )
    assert "notify-failure-alinux3.sh %i" in failure_unit
    assert "RUNBOW007_ENABLE_SENDING" in failure_script
    assert "notify-failure" in failure_script
    assert "runbow007-failure@.service" in deploy_script


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

