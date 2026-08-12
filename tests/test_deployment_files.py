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


def test_systemd_timers_are_persistent_and_use_shanghai_timezone():
    timer_dir = ROOT / "deploy" / "systemd"
    hourly = (timer_dir / "runbow007-hourly.timer").read_text(encoding="utf-8")
    arrival = (timer_dir / "runbow007-arrival.timer").read_text(encoding="utf-8")

    assert "*:05:00 Asia/Shanghai" in hourly
    assert "13:30:00 Asia/Shanghai" in arrival
    assert "Persistent=true" in hourly
    assert "Persistent=true" in arrival


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
    assert workflow["permissions"] == {"contents": "read"}


def test_github_deploy_pins_host_key_and_never_enables_sending():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert "StrictHostKeyChecking=yes" in workflow
    assert "DEPLOY_KNOWN_HOSTS" in workflow
    assert "workflow_dispatch" in workflow
    assert "RUNBOW007_ENABLE_SENDING=true" not in workflow
    assert "RUNBOW007_ENABLE_SENDING=true" not in remote_script


def test_actions_deploy_can_bootstrap_docker_only_on_alinux3():
    remote_script = (ROOT / "scripts" / "deploy-from-actions.sh").read_text(
        encoding="utf-8"
    )

    assert '"${ID:-}" != "alinux"' in remote_script
    assert '"${VERSION_ID%%.*}" != "3"' in remote_script
    assert "dnf-plugin-releasever-adapter --repo alinux3-plus" in remote_script
    assert "docker-ce docker-ce-cli containerd.io" in remote_script
    assert "docker-buildx-plugin docker-compose-plugin" in remote_script
    assert "systemctl enable --now docker" in remote_script

