import asyncio
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from app import compose
from app.compose import APPLY_DIR, DEFAULT_COMPOSE, built_stamp, check, image_tag, prepare, run_env, saved_project
from app.config import Config
from app.store import Slot

ALIASED = "services:\n  app:\n    networks:\n      yard:\n        aliases:\n          - ${SLOT_NAME}\n"
FULL = ALIASED + "    env_file:\n      - ${APP_ENV}\n    volumes:\n      - ${DATA_DIR}:/data\n"


def _config(root: Path) -> Config:
    return Config(
        root=root,
        caddy_sock=None,
        podman_sock="/run/podman.sock",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=True,
    )


def _slot(name: str, trigger: str = "", image: str = "") -> Slot:
    return Slot(name, "heldso", "main", "", "", image, trigger, "")


def _prepare(config: Config, slot: Slot, checkout: Path, image: str, sha: str) -> tuple[compose.Project, list[str]]:
    return asyncio.run(prepare(config, slot, checkout, image, sha))


def _checkout(root: Path, files: dict[str, str]) -> Path:
    directory = root / "checkouts" / "heldso" / "abc"
    directory.mkdir(parents=True)
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return directory


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _doc(aliases: dict[str, list[str]]) -> dict[str, Any]:
    """A resolved compose document: each service with its yard aliases."""
    return {"services": {name: {"networks": {"yard": {"aliases": found}}} for name, found in aliases.items()}}


def _resolved(doc: dict[str, Any]):
    async def fake(compose_file: Path, env: dict[str, str]) -> dict[str, Any]:
        return doc

    return fake


def test_run_env_is_the_interpolation_env(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    env = run_env(config, _slot("heldso"), checkout, "localhost/heldso:abc", "abc")

    assert env == {
        "SLOT_NAME": "heldso",
        "IMAGE": "localhost/heldso:abc",
        "SHA": "abc",
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(tmp_path / "data" / "heldso"),
        "APP_ENV": str(tmp_path / "env" / "heldso.env"),
        "DOCKERFILE": "Containerfile",
    }
    assert (tmp_path / "data" / "heldso").is_dir()
    assert not any(key.startswith("STARIO") for key in env)


def test_prepare_with_the_default_compose(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    project, warnings = _prepare(config, _slot("heldso"), checkout, "localhost/heldso:abc", "abc")

    assert project.files == [DEFAULT_COMPOSE]
    assert warnings == []
    assert project.env["DOCKERFILE"] == "Containerfile"
    assert project.env["APP_ENV"] == str(tmp_path / "env" / "heldso.env")

    # The env file exists for compose, closed to other users. Nothing else is written.
    env_file = tmp_path / "env" / "heldso.env"
    assert env_file.read_text() == ""
    assert _mode(env_file) == 0o600
    assert not (tmp_path / "run").exists()


def test_prepare_needs_a_build_source(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"README": "hi\n"})
    with pytest.raises(RuntimeError, match="no compose file or Containerfile"):
        _prepare(config, _slot("heldso"), checkout, "img", "abc")


def test_prepare_with_a_repo_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"compose.yml": FULL})
    monkeypatch.setattr(compose, "resolved", _resolved(_doc({"app": ["heldso"], "db": []})))
    project, warnings = _prepare(config, _slot("heldso"), checkout, "localhost/heldso:abc", "abc")

    # The repo file runs as it is: no override, no DOCKERFILE.
    assert project.files == [str(checkout / "compose.yml")]
    assert warnings == []
    assert "DOCKERFILE" not in project.env
    assert not (tmp_path / "run").exists()


def test_prepare_fails_without_the_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"compose.yml": "services:\n  app:\n    networks: [yard]\n"})
    monkeypatch.setattr(compose, "resolved", _resolved({"services": {"app": {"networks": {"yard": None}}}}))
    with pytest.raises(RuntimeError) as failed:
        _prepare(config, _slot("heldso"), checkout, "localhost/heldso:abc", "abc")
    assert str(failed.value) == (
        "compose: no service on network yard with alias heldso; the health probe dials http://heldso:8000/"
    )

    # A job slot is not dialed, so it needs no alias.
    _project, warnings = _prepare(config, _slot("nightly", trigger="every 5m"), checkout, "img", "abc")
    assert warnings == [
        "warning: compose: no service attaches env_file ${APP_ENV}; the slot env is not passed",
        "warning: compose: no service mounts ${DATA_DIR}; the slot has no /data",
    ]


def test_check_warns_on_the_soft_rules(tmp_path: Path):
    doc = _doc({"app": ["heldso"]})
    assert check(_slot("heldso"), doc, FULL) == []
    assert check(_slot("heldso"), doc, ALIASED) == [
        "warning: compose: no service attaches env_file ${APP_ENV}; the slot env is not passed",
        "warning: compose: no service mounts ${DATA_DIR}; the slot has no /data",
    ]
    assert check(_slot("heldso"), doc, ALIASED + "    env_file: [${APP_ENV}]\n") == [
        "warning: compose: no service mounts ${DATA_DIR}; the slot has no /data",
    ]

    # The alias may sit on any service. A document without services has none.
    assert check(_slot("heldso"), _doc({"web": ["heldso", "www"]}), FULL) == []
    with pytest.raises(RuntimeError, match="alias heldso"):
        check(_slot("heldso"), _doc({"app": ["other"]}), FULL)
    with pytest.raises(RuntimeError, match="alias heldso"):
        check(_slot("heldso"), {}, FULL)


def test_the_yard_slot_gets_its_host_paths_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"ops/compose.yml": FULL})
    monkeypatch.setattr(compose, "resolved", _resolved(_doc({"yard": ["yard"], "caddy": []})))
    project, _warnings = _prepare(config, _slot("yard"), checkout, "localhost/yard:abc", "abc")

    assert project.env["YARD_ROOT"] == str(tmp_path)
    assert project.env["YARD_PODMAN_SOCK"] == "/run/podman.sock"
    assert project.files == [str(checkout / "ops" / "compose.yml")]

    # Another slot does not see the host paths.
    other, _warnings = _prepare(config, _slot("nightly", trigger="every 5m"), checkout, "localhost/nightly:abc", "abc")
    assert "YARD_PODMAN_SOCK" not in other.env


def test_saved_project_rebuilds_from_the_live_image(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    live = _slot("heldso", image="localhost/heldso:abc")
    found = saved_project(config, live)
    assert found is not None
    assert found.files == [DEFAULT_COMPOSE]
    assert found.env["CHECKOUT"] == str(checkout)
    assert found.env["IMAGE"] == "localhost/heldso:abc"
    assert found.env["SHA"] == "abc"

    # A repo compose is the one file.
    (checkout / "compose.yml").write_text(FULL)
    found = saved_project(config, live)
    assert found is not None
    assert found.files == [str(checkout / "compose.yml")]

    # No live image or a missing checkout means nothing to run.
    assert saved_project(config, _slot("heldso")) is None
    assert saved_project(config, _slot("heldso", image="localhost/heldso:zzz")) is None


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
        {"DATA_DIR": f"{tmp_path}/data/heldso", "IMAGE": "localhost/heldso:abc", "SLOT_NAME": "heldso"},
    )

    code, text = asyncio.run(
        compose.apply(
            config,
            "heldso",
            "up",
            project,
            previous="localhost/heldso:old9",
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
        "yard-apply-heldso",
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
        "PREVIOUS=localhost/heldso:old9",
        "-e",
        f"DATA_DIR={tmp_path}/data/heldso",
        "-e",
        "IMAGE=localhost/heldso:abc",
        "-e",
        "SLOT_NAME=heldso",
        "localhost/yard-apply",
        "up",
    ]
    assert env["CONTAINER_HOST"] == "unix:///run/podman.sock"
    assert env["DOCKER_HOST"] == "unix:///run/podman.sock"

    # The job command gets the -job container name.
    asyncio.run(compose.apply(config, "nightly", "job", project))
    argv, _env = calls[-1]
    assert argv[-2:] == ["localhost/yard-apply", "job"]
    assert "yard-apply-nightly-job" in argv

    # The yard slot detaches.
    asyncio.run(compose.apply(config, "yard", "up", project, detach=True))
    argv, _env = calls[-1]
    assert argv[-3:] == ["-d", "localhost/yard-apply", "up"]


def test_install_sh_runs_the_first_apply():
    # install.sh prepares the host, then starts the first apply container by hand.
    spec = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    for part in ("git clone --bare", "localhost/yard-apply", "yard doctor", "admin.token"):
        assert part in spec
    for gone in ("prepare_source", "autosave", "HEALTH_URL", "yard:bootstrap", "app.main bootstrap"):
        assert gone not in spec


def test_install_sh_and_apply_pass_the_same_env_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # install.sh starts the yard apply container by hand; apply() must pass the same -e names.
    spec = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
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
        run_env(config, _slot("yard"), checkout, "localhost/yard:abc", "abc"),
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
    ops = Path(__file__).resolve().parents[1] / "ops"
    text = (ops / "apply" / "default.compose.yml").read_text()
    assert "- ${SLOT_NAME}" in text
    assert "- ${APP_ENV}" in text
    assert "- ${DATA_DIR}:/data" in text
    for line in (
        "SLOT_NAME: ${SLOT_NAME}",
        "SLOT_DATA_DIR: /data",
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
    assert "yard." not in own
    assert "path: ${APP_ENV}" in own
    assert "required: false" in own
    files = sorted(path.name for path in (ops / "apply").iterdir())
    assert files == ["Containerfile", "apply.sh", "default.compose.yml"]


def test_built_stamp_reads_the_script_line():
    log = "== build localhost/app:abc\nbuilt 2026-10-05T10:00:00Z\n== ready localhost/app:abc\n"
    assert built_stamp(log) == "2026-10-05T10:00:00Z"
    assert built_stamp("== build failed\n") == ""


def test_image_tag():
    assert image_tag("mono", "", "abc") == "localhost/mono:abc"
    assert image_tag("mono", "projects/Mono_App", "abc") == "localhost/mono-projects-mono-app:abc"
