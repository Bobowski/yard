import asyncio
import os
import re
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.server import compose
from app.server.compose import APPLY_DIR, DEFAULT_COMPOSE, image_tag, prepare, run_env, saved_project
from app.server.store import App, Config
from app.shared import YardError

ON_YARD = "services:\n  app:\n    networks:\n      - yard\n"
FULL = ON_YARD + "    env_file:\n      - ${APP_ENV}\n    volumes:\n      - ${DATA_DIR}:/data\n"


def _config(root: Path) -> Config:
    return Config(
        root=root,
        podman_sock="/run/podman.sock",
        podman_bin="podman",
        skip_deploy=True,
        boot=False,
        remote=True,
        journal_dir=root / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=root / "no-journal.sock",
    )


def _app(name: str, trigger: str = "") -> App:
    return App(name, "https://origin.cursor.com/org/heldso", "main", trigger or "push", "")


def _prepare(config: Config, app: App, checkout: Path, image: str, sha: str) -> compose.Project:
    return prepare(config, app, checkout, image, sha)


def _checkout(root: Path, files: dict[str, str]) -> Path:
    directory = root / "src" / "heldso" / "abc"
    directory.mkdir(parents=True)
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return directory


def _git(checkout: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "-C", str(checkout), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-m", "one"], check=True, capture_output=True, env=env)


def _git_out(checkout: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_run_env_is_the_interpolation_env(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    env = run_env(config, _app("heldso"), checkout, "localhost/heldso:abc", "abc")

    assert env == {
        "PROJECT_NAME": "heldso",
        "IMAGE": "localhost/heldso:abc",
        "SHA": "abc",
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(tmp_path / "apps" / "heldso" / "data"),
        "APP_ENV": str(tmp_path / "apps" / "heldso" / "env"),
        "DOCKERFILE": "Containerfile",
    }
    assert (tmp_path / "apps" / "heldso" / "data").is_dir()
    assert not any(key.startswith("STARIO") for key in env)


def test_prepare_with_the_default_compose(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    project = _prepare(config, _app("heldso"), checkout, "localhost/heldso:abc", "abc")

    assert project.files == [DEFAULT_COMPOSE]
    assert project.env["DOCKERFILE"] == "Containerfile"
    assert project.env["APP_ENV"] == str(tmp_path / "apps" / "heldso" / "env")

    # The env file exists for compose, closed to other users. Nothing else is written.
    env_file = tmp_path / "apps" / "heldso" / "env"
    assert env_file.read_text() == ""
    assert _mode(env_file) == 0o600
    assert not (tmp_path / "run").exists()


def test_prepare_needs_a_build_source(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"README": "hi\n"})
    with pytest.raises(YardError, match="no compose file or Containerfile"):
        _prepare(config, _app("heldso"), checkout, "img", "abc")


def test_prepare_with_a_repo_compose(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"compose.yml": FULL})
    project = _prepare(config, _app("heldso"), checkout, "localhost/heldso:abc", "abc")

    # The repo file runs as it is: no override, no DOCKERFILE.
    assert project.files == [str(checkout / "compose.yml")]
    assert "DOCKERFILE" not in project.env
    assert not (tmp_path / "run").exists()


def test_the_yard_app_gets_its_host_paths_from_config(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"ops/compose.yml": FULL})
    project = _prepare(config, _app("yard"), checkout, "localhost/yard:abc", "abc")

    assert project.env["YARD_ROOT"] == str(tmp_path)
    assert project.env["YARD_PODMAN_SOCK"] == "/run/podman.sock"
    # The directory is mounted so a host podman.socket restart stays reachable.
    assert project.env["YARD_PODMAN_DIR"] == "/run"
    default = _prepare(replace(config, podman_sock=""), _app("yard"), checkout, "localhost/yard:abc", "abc")
    assert default.env["YARD_PODMAN_SOCK"] == "/run/podman/podman.sock"
    assert default.env["YARD_PODMAN_DIR"] == "/run/podman"
    assert project.files == [str(checkout / "ops" / "compose.yml")]

    # Another app does not see the host paths.
    other = _prepare(config, _app("nightly", trigger="manual"), checkout, "localhost/nightly:abc", "abc")
    assert "YARD_PODMAN_SOCK" not in other.env


def test_saved_project_rebuilds_from_the_clone(tmp_path: Path):
    config = _config(tmp_path)
    checkout = config.clone("heldso")
    checkout.mkdir(parents=True)
    (checkout / "Containerfile").write_text("FROM scratch\n")
    _git(checkout)
    sha = _git_out(checkout, "rev-parse", "HEAD")
    live = _app("heldso")
    found = saved_project(config, live)
    assert found is not None
    assert found.files == [DEFAULT_COMPOSE]
    assert found.env["CHECKOUT"] == str(checkout)
    assert found.env["IMAGE"] == image_tag("heldso", sha)
    assert found.env["SHA"] == sha

    # A repo compose is the one file.
    (checkout / "compose.yml").write_text(FULL)
    found = saved_project(config, live)
    assert found is not None
    assert found.files == [str(checkout / "compose.yml")]

    # A missing clone means nothing to run.
    assert saved_project(config, _app("other")) is None


def test_apply_runs_one_podman_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run(argv, env):
        calls.append((list(argv), dict(env)))
        return 0, "== build\n"

    monkeypatch.setattr(compose, "_run", fake_run)
    project = compose.Project(
        "heldso",
        ["/checkout/compose.yml"],
        {"DATA_DIR": f"{tmp_path}/data/heldso", "IMAGE": "localhost/heldso:abc", "PROJECT_NAME": "heldso"},
    )

    code, text = asyncio.run(
        compose.apply(
            config,
            "heldso",
            "up",
            project,
        )
    )
    assert code == 0
    assert text == "== build\n"
    argv, env = calls[0]
    assert argv == [
        "podman",
        "--remote",
        "run",
        "--replace",
        "--name",
        "deploy-heldso",
        "--network",
        "yard",
        "-v",
        "/run/podman.sock:/run/podman/podman.sock",
        "-v",
        f"{tmp_path}:{tmp_path}",
        "-e",
        "COMPOSE_PROJECT_NAME=heldso",
        "-e",
        "COMPOSE_FILE=/checkout/compose.yml",
        "-e",
        f"DATA_DIR={tmp_path}/data/heldso",
        "-e",
        "IMAGE=localhost/heldso:abc",
        "-e",
        "PROJECT_NAME=heldso",
        "localhost/yard-apply",
        "up",
    ]
    assert env["CONTAINER_HOST"] == "unix:///run/podman.sock"
    assert env["DOCKER_HOST"] == "unix:///run/podman.sock"

    # The yard app detaches.
    asyncio.run(compose.apply(config, "yard", "up", project, detach=True))
    argv, _env = calls[-1]
    assert argv[-3:] == ["-d", "localhost/yard-apply", "up"]


def test_install_sh_runs_the_first_apply():
    # install.sh prepares the host, then starts the first apply container by hand.
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    for part in (
        'git clone --depth 1 "$origin" "$src"',
        "apps/yard/repo",
        "localhost/yard-apply",
        "yard forge setup origin",
        "admin.token",
    ):
        assert part in spec
    for gone in ("prepare_source", "autosave", "HEALTH_URL", "yard:bootstrap", "clone --bare", "worktree", "repos/"):
        assert gone not in spec


def test_install_sh_configures_journald():
    # Container logs live in the journal so they survive a container recreate.
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    for part in (
        "/etc/systemd/journald.conf.d/yard.conf",
        "Storage=persistent",
        "SystemMaxUse=2G",
        "RateLimitBurst=20000",
        "mkdir -p /var/log/journal",
        "systemd-tmpfiles --create --prefix /var/log/journal",
        "systemctl restart systemd-journald",
        "/home/yard/.config/containers/containers.conf",
        'log_driver = "journald"',
        "[engine]",
        'events_logger = "journald"',
        "systemctl --user restart podman.socket",
    ):
        assert part in spec
    # Each file writes only when its bytes differ; the restarts stay inside the guards.
    assert spec.count('!= "$journald_conf"') == 1
    assert spec.count('!= "$containers_conf"') == 1


def test_install_sh_uses_the_distro_podman():
    # Ubuntu 26.04 LTS packages Podman 5.x; the static bundle is gone.
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    assert "apt-get install -y nftables git ca-certificates sudo iproute2 procps uidmap acl podman" in spec
    assert "VERSION_ID" in spec
    assert "26.04" in spec
    assert "Yard needs Podman 5 or newer" in spec
    # The only podman-static reference left is the drop-in cleanup for 0.6.0 hosts.
    assert spec.count("podman-static.conf") == 1
    for gone in ("podman_sha256", "wget -q", "podman-linux-amd64", "/usr/local/bin/podman"):
        assert gone not in spec


def test_the_images_pin_the_host_podman_remote():
    # Both images carry the podman-remote that matches the host's Podman 5.7.
    ops = Path(__file__).resolve().parents[2] / "ops"
    sha = "0d1759e3fc42b0e3a4496176dabc7b65f0f334a0ab2b22eaed7c36afeece8b0c"
    yard = (ops / "Containerfile").read_text()
    apply = (ops / "apply" / "Containerfile").read_text()
    assert "download/v5.7.0/podman-remote-static-linux_amd64.tar.gz" in yard
    assert f"{sha}  /tmp/podman-remote.tar.gz" in yard
    assert "PODMAN_VERSION=5.7.0" in apply
    assert f"PODMAN_SHA256={sha}" in apply
    assert "v${PODMAN_VERSION}/podman-remote-static-linux_amd64.tar.gz" in apply


def test_install_sh_and_apply_pass_the_same_env_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # install.sh starts the yard apply container by hand; apply() must pass the same -e names.
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    block = spec.split("podman run --replace", 1)[1].split("localhost/yard-apply", 1)[0]
    shell_names = set(re.findall(r"-e ([A-Z_]+)=", block))

    config = _config(tmp_path)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run(argv, env):
        calls.append((list(argv), dict(env)))
        return 0, ""

    monkeypatch.setattr(compose, "_run", fake_run)
    checkout = _checkout(tmp_path, {"compose.yml": FULL})
    project = compose.Project(
        "yard",
        [f"{checkout}/compose.yml"],
        run_env(config, _app("yard"), checkout, "localhost/yard:abc", "abc"),
    )
    asyncio.run(compose.apply(config, "yard", "up", project))

    argv, _env = calls[0]
    python_names = {value.split("=", 1)[0] for flag, value in zip(argv, argv[1:], strict=False) if flag == "-e"}
    assert python_names == shell_names


def test_build_apply_builds_the_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run(argv, env):
        calls.append((list(argv), dict(env)))
        return 0, ""

    monkeypatch.setattr(compose, "_run", fake_run)
    code, _ = asyncio.run(compose.build_apply(config))
    assert code == 0
    argv, env = calls[0]
    assert argv == [
        "podman",
        "--remote",
        "build",
        "-t",
        "localhost/yard-apply",
        "-f",
        f"{APPLY_DIR}/Containerfile",
        APPLY_DIR,
    ]
    assert env["CONTAINER_HOST"] == "unix:///run/podman.sock"
    assert env["DOCKER_HOST"] == "unix:///run/podman.sock"


def test_the_default_compose_meets_the_contract():
    # The file Yard ships passes its own check, and no app container carries yard labels.
    ops = Path(__file__).resolve().parents[2] / "ops"
    text = (ops / "apply" / "default.compose.yml").read_text()
    assert "aliases:" not in text
    assert "- yard" in text
    assert "- ${APP_ENV}" in text
    assert "- ${DATA_DIR}:/data" in text
    for line in (
        "PROJECT_NAME: ${PROJECT_NAME}",
        "PROJECT_DATA_DIR: /data",
        "SHA: ${SHA}",
        'PORT: "8000"',
        "STARIO_HOST: 0.0.0.0",
        'STARIO_PORT: "8000"',
        "STARIO_TRACER: json",
    ):
        assert line in text
    assert "restart: always" in text
    assert "      IMAGE:" not in text
    assert "yard." not in text

    own = (ops / "compose.yml").read_text()
    assert "labels:" not in own
    # The socket directory is mounted, not the socket file itself.
    assert "${YARD_PODMAN_DIR}:/run/podman" in own
    assert ":/run/podman/podman.sock" not in own
    # The app service keeps the yard alias: a self-deploy probe dials it by name.
    assert "aliases:" in own
    assert "path: ${APP_ENV}" in own
    assert "required: false" in own
    files = sorted(path.name for path in (ops / "apply").iterdir())
    assert files == ["Containerfile", "apply.sh", "default.compose.yml"]


def test_image_tag():
    assert image_tag("mono", "abc1234def") == "localhost/mono:abc1234def"
    assert image_tag("yard", "abc") == "localhost/yard:abc"
