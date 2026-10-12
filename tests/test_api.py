import json
import os
import stat
import subprocess
from pathlib import Path

from app import deploy
from app.podman import Podman
from app.store import Store
from tests.conftest import auth, local


async def test_health(yard):
    client, _token, _root = yard
    res = await client.get("/health")
    assert res.status_code == 200


async def test_slot_name_is_required(yard):
    client, token, _root = yard
    made = await client.post("/api/v1/repos", json={"name": "base"}, headers=auth(token))
    assert made.status_code == 200
    for body in (
        {"name": "", "repo": "base"},
        {"name": " ", "repo": "base"},
        {"repo": "base"},
    ):
        res = await client.post("/api/v1/slots", json=body, headers=auth(token))
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_json"


async def test_repo_and_slot(yard):
    client, token, root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    bare = root / "repos" / "heldso.git"
    hook = (bare / "hooks" / "reference-transaction").read_text()
    assert "/api/v1/hooks/push" in hook
    assert "committed" in hook
    assert not (bare / "hooks" / "post-receive").exists()
    assert not (bare / "hooks" / "pre-receive").exists()
    assert res.json()["name"] == "heldso"
    head = subprocess.check_output(["git", "-C", str(bare), "symbolic-ref", "HEAD"], text=True)
    assert head.strip() == "refs/heads/main"

    res = await client.post(
        "/api/v1/slots",
        json={"name": "heldso", "repo": "heldso", "domain": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    slot = res.json()["slot"]
    assert slot["ref"] == "main"
    assert slot["domain"] == "heldso.example.com"
    assert res.json()["deploy"]["status"] == "waiting"

    res = await client.post(
        "/api/v1/slots",
        json={"name": "other", "repo": "heldso", "domain": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 409
    assert "domain_taken" in res.text

    res = await client.post(
        "/api/v1/slots",
        json={"name": "yard-sampler", "repo": "heldso"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.post("/api/v1/repos", json={"name": "caddy"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "caddy", "repo": "caddy"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.patch(
        "/api/v1/slots/heldso",
        json={"domain": "app.example.com", "ref": "release"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["domain"] == "app.example.com"
    assert res.json()["slot"]["ref"] == "release"

    res = await client.delete("/api/v1/repos/heldso", headers=auth(token))
    assert res.status_code == 409
    assert "repo_in_use" in res.text

    refused = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "heldso", "new": "abc", "ref": "refs/heads/release"},
        headers=auth(token),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "local_only"
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "heldso", "new": "abc", "ref": "refs/heads/release"},
        headers=local(token),
    )
    assert res.status_code == 200
    assert "deploy skipped" in res.text
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "heldso", "new": "abc", "ref": "refs/heads/main"},
        headers=local(token),
    )
    assert "no slot watches" in res.text

    res = await client.get("/api/v1/slots")
    assert res.status_code == 401
    assert "unauthorized" in res.text


async def test_slot_trigger(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "backup", "repo": "heldso", "trigger": "tomorrow"},
        headers=auth(token),
    )
    assert res.status_code == 400
    res = await client.post(
        "/api/v1/slots",
        json={"name": "backup", "repo": "heldso", "trigger": "every 5m"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["trigger"] == "every 5m"
    res = await client.patch("/api/v1/slots/backup", json={"trigger": ""}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["slot"]["trigger"] == ""


async def test_slot_env_round_trip(yard):
    client, token, root = yard
    assert (await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))).status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "heldso", "repo": "heldso", "domain": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["env"] == {}
    vals = (await client.get("/api/v1/slots/heldso", headers=auth(token))).json()
    assert vals["name"] == "heldso"
    assert vals["domain"] == "heldso.example.com"

    # The slot env is a file on the host, closed to other users. The CLI quoting rule applies.
    env_file = root / "env" / "heldso.env"
    assert env_file.read_text() == ""
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    res = await client.patch(
        "/api/v1/slots/heldso",
        json={"ref": "login", "env": {"API_TOKEN": "secret", "GREETING": "hi there"}},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["env"] == {"API_TOKEN": "secret", "GREETING": "hi there"}
    assert env_file.read_text() == 'API_TOKEN=secret\nGREETING="hi there"\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    vals = (await client.get("/api/v1/slots/heldso", headers=auth(token))).json()
    assert vals["ref"] == "login"
    assert vals["repo"] == "heldso"
    assert vals["domain"] == "heldso.example.com"
    assert vals["env"] == {"API_TOKEN": "secret", "GREETING": "hi there"}

    # The list hides the env; only the single GET shows it.
    listed = (await client.get("/api/v1/slots", headers=auth(token))).json()
    assert listed[0]["name"] == "heldso"
    assert "env" not in listed[0]

    # Deleting the slot removes the file.
    assert (await client.delete("/api/v1/slots/heldso", headers=auth(token))).status_code == 200
    assert not env_file.exists()


async def test_repo_create_is_safe(yard):
    client, token, root = yard
    junk = root / "repos" / "heldso.git"
    junk.mkdir(parents=True)
    (junk / "nope").write_text("x")
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 409
    assert "--force" in res.text
    assert (junk / "nope").is_file()
    res = await client.post("/api/v1/repos", json={"name": "heldso", "force": True}, headers=auth(token))
    assert res.status_code == 200
    assert (junk / "hooks" / "reference-transaction").is_file()
    assert not (junk / "hooks" / "post-receive").exists()
    assert not (junk / "nope").exists()
    res = await client.post("/api/v1/repos", json={"name": "heldso", "force": True}, headers=auth(token))
    assert res.status_code == 409
    assert "already exists" in res.text
    assert (junk / "hooks" / "reference-transaction").is_file()


async def test_slot_deploys_existing_revision(yard):
    client, token, root = yard
    assert (await client.post("/api/v1/repos", json={"name": "app"}, headers=auth(token))).status_code == 200
    sha = _commit_empty(root / "repos" / "app.git")
    res = await client.post(
        "/api/v1/slots",
        json={"name": "app", "repo": "app", "ref": "main"},
        headers=auth(token),
    )
    assert res.status_code == 200
    deploy = res.json()["deploy"]
    assert deploy["status"] == "skipped"
    assert sha in deploy["image"]
    vals = (await client.get("/api/v1/slots/app", headers=auth(token))).json()
    assert vals["sha"] == ""
    again = await client.post("/api/v1/slots/app/deploy", headers=auth(token))
    assert again.status_code == 200
    assert again.json()["deploy"]["status"] == "skipped"
    assert sha in again.text
    bad = await client.get("/api/v1/logs", params={"role": "nope"}, headers=auth(token))
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "bad_role"


async def test_tokens(yard):
    client, admin, _root = yard
    res = await client.post("/api/v1/tokens", json={"name": "ada"}, headers=auth(admin))
    assert res.status_code == 200
    ada = res.json()["token"]
    res = await client.get("/api/v1/tokens", headers=auth(admin))
    names = {row["name"] for row in res.json()}
    assert names == {"admin", "ada"}
    assert (await client.get("/api/v1/repos", headers=auth(ada))).status_code == 200

    res = await client.delete("/api/v1/tokens/admin", headers=auth(admin))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"
    res = await client.delete("/api/v1/tokens/nobody", headers=auth(admin))
    assert res.status_code == 404

    res = await client.delete("/api/v1/tokens/ada", headers=auth(admin))
    assert res.status_code == 200
    assert (await client.get("/api/v1/repos", headers=auth(ada))).status_code == 401
    assert (await client.get("/api/v1/repos", headers=auth(admin))).status_code == 200


async def test_slot_leaves_when_branch_merges(yard):
    client, token, root = yard
    assert (await client.post("/api/v1/repos", json={"name": "shop"}, headers=auth(token))).status_code == 200
    bare = root / "repos" / "shop.git"
    sha = _seed_merged_feature(bare)
    assert (
        await client.post(
            "/api/v1/slots",
            json={"name": "shop", "repo": "shop", "ref": "main"},
            headers=auth(token),
        )
    ).status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "shop-wip", "repo": "shop", "ref": "feature", "domain": "wip.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["domain"] == "wip.example.com"
    assert (
        await client.post(
            "/api/v1/slots",
            json={"name": "shop-also", "repo": "shop", "ref": "feature"},
            headers=auth(token),
        )
    ).status_code == 200
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "shop", "new": sha, "ref": "refs/heads/main"},
        headers=local(token),
    )
    assert res.status_code == 200
    assert "slot shop-wip removed: branch feature is in main" in res.text
    assert "slot shop-also removed" in res.text
    assert (await client.get("/api/v1/slots/shop-wip", headers=auth(token))).status_code == 404
    assert (await client.get("/api/v1/slots/shop-also", headers=auth(token))).status_code == 404
    assert (await client.get("/api/v1/slots/shop", headers=auth(token))).status_code == 200

    assert (
        await client.post(
            "/api/v1/slots",
            json={"name": "shop-wip", "repo": "shop", "ref": "feature"},
            headers=auth(token),
        )
    ).status_code == 200
    zeros = "0" * 40
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "shop", "new": zeros, "ref": "refs/heads/feature"},
        headers=local(token),
    )
    assert res.status_code == 200
    assert "slot shop-wip removed: branch feature was deleted" in res.text
    assert (await client.get("/api/v1/slots/shop-wip", headers=auth(token))).status_code == 404
    assert (await client.get("/api/v1/slots/shop", headers=auth(token))).status_code == 200


async def test_repo_git_reads_a_commit_and_fetch_needs_origin(yard):
    client, token, root = yard
    made = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert made.status_code == 200
    sha = _commit_empty(root / "repos" / "heldso.git")
    res = await client.post(
        "/api/v1/repos/heldso/git",
        json={"args": ["log", "-1", "--oneline"]},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert sha[:7] in res.json()["output"]
    refused = await client.post("/api/v1/repos/heldso/git", json={"args": ["fetch"]}, headers=auth(token))
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "git_failed"


async def test_container_inspect_rejects_a_bad_name(yard):
    client, token, _root = yard
    res = await client.get("/api/v1/containers/heldso")
    assert res.status_code == 401
    res = await client.get("/api/v1/containers/bad%20name", headers=auth(token))
    assert res.status_code == 400


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    name = env.get("GIT_AUTHOR_NAME") or _git_value("user.name")
    email = env.get("GIT_AUTHOR_EMAIL") or _git_value("user.email")
    if not name:
        name = "Adam Bobowski"
    if not email:
        email = "bobowskiadam@gmail.com"
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    if not env.get("GIT_COMMITTER_NAME"):
        env["GIT_COMMITTER_NAME"] = name
    if not env.get("GIT_COMMITTER_EMAIL"):
        env["GIT_COMMITTER_EMAIL"] = email
    return env


def _git_value(key: str) -> str:
    found = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
    if found.returncode != 0:
        return ""
    return found.stdout.strip()


def _commit_empty(directory: Path) -> str:
    tree = subprocess.check_output(["git", "-C", str(directory), "hash-object", "-t", "tree", "--stdin"], input=b"")
    commit = subprocess.check_output(
        ["git", "-C", str(directory), "commit-tree", tree.decode().strip(), "-m", "init"],
        text=True,
        env=_git_env(),
    )
    sha = commit.strip()
    subprocess.check_call(["git", "-C", str(directory), "update-ref", "refs/heads/main", sha])
    return sha


def _seed_merged_feature(bare: Path) -> str:
    hook = bare / "hooks" / "reference-transaction"
    off = hook.with_name("reference-transaction.off")
    hook.rename(off)
    try:
        work = bare.parent / "work"
        work.mkdir()
        _git(work, "init", "-b", "main")
        (work / "readme.txt").write_text("one\n")
        _git(work, "add", "readme.txt")
        _git(work, "commit", "-m", "one")
        _git(work, "remote", "add", "origin", str(bare))
        _git(work, "push", "origin", "main")
        _git(work, "checkout", "-b", "feature")
        (work / "readme.txt").write_text("two\n")
        _git(work, "add", "readme.txt")
        _git(work, "commit", "-m", "two")
        _git(work, "push", "origin", "feature")
        _git(work, "checkout", "main")
        _git(work, "merge", "-m", "merge", "feature")
        _git(work, "push", "origin", "main")
        return subprocess.check_output(["git", "-C", str(bare), "rev-parse", "main"], text=True).strip()
    finally:
        off.rename(hook)


def _git(directory: Path, *args: str) -> None:
    subprocess.check_call(["git", "-C", str(directory), *args], env=_git_env())


async def test_slot_status_and_doctor(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, timeout=None, env=None):
        if args and args[0] == "ps":
            labels = {"com.docker.compose.project": "heldso", "com.docker.compose.service": "app"}
            return json.dumps([{"Names": ["heldso-app-1"], "State": "running", "Labels": labels}])
        if args and args[0] == "stats":
            return "[]"
        if args and args[0] == "info":
            return '{"host":{"cpus":4,"memTotal":1000},"version":{"Version":"5.3.1"}}'
        if args and args[0] == "logs":
            return "2026-10-05T12:00:00Z one\n"
        return ""

    from app.podman import Podman

    monkeypatch.setattr(Podman, "run", fake_run)
    res = await client.post("/api/v1/repos", json={"name": "app"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "heldso", "repo": "app", "ref": "main", "domain": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200

    page = (await client.get("/api/v1/slots/heldso/status", headers=auth(token))).json()
    assert page["slot"]["name"] == "heldso"
    assert "env" not in page["slot"]
    assert page["route"] == {"host": "heldso.example.com", "dial": "heldso:8000"}
    assert [row["container"] for row in page["rows"]] == ["heldso-app-1"]
    assert page["rows"][0]["answers"] is None
    assert "one" in page["log"]
    res = await client.get("/api/v1/slots/nope/status", headers=auth(token))
    assert res.status_code == 404

    checks = {check["name"]: check for check in (await client.get("/api/v1/doctor", headers=auth(token))).json()}
    assert checks["podman"] == {"name": "podman", "ok": True, "detail": "podman 5.3.1"}
    assert checks["hook token"]["ok"]
    assert checks["caddy"]["detail"] == "skipped: no host"


async def test_slot_run(yard, monkeypatch):
    client, token, root = yard
    res = await client.post("/api/v1/repos", json={"name": "app"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"name": "nightly", "repo": "app", "trigger": "manual"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["slot"]["trigger"] == "manual"

    async def no_ps(self, *args, **kw):
        return "[]"

    monkeypatch.setattr(Podman, "run", no_ps)
    res = await client.post("/api/v1/slots/nightly/run", headers=auth(token))
    assert res.status_code == 409

    store = Store.open(root / "data" / "yard")
    try:
        store.set_image("nightly", "localhost/app:abc")
    finally:
        store.close()

    monkeypatch.setattr(deploy, "run_job", lambda *a, **k: _fake_job())
    res = await client.post("/api/v1/slots/nightly/run", headers=auth(token))
    assert res.status_code == 200
    assert res.json()["output"] == "ran nightly\n"


async def _fake_job():
    return "ran nightly\n"


async def test_prune(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, **kw):
        if args and args[0] == "images":
            return "[]"
        return ""

    monkeypatch.setattr(Podman, "run", fake_run)
    res = await client.post("/api/v1/prune", json={"days": 0}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_json"

    res = await client.post("/api/v1/prune", json={"days": 3, "dry": True}, headers=auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["days"] == 3
    assert body["dry"]
    assert body["checkouts"] == []
    assert body["images"] == []
