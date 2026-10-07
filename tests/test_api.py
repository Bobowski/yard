import subprocess
from pathlib import Path

from stario.testing import TestClient

from app.main import bootstrap
from tests.conftest import auth


async def test_health(yard):
    client, _token, _root = yard
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.text == ""


async def test_slot_name_is_required(yard):
    client, token, _root = yard
    made = await client.post("/api/v1/repos", json={"name": "base"}, headers=auth(token))
    assert made.status_code == 200
    for body in (
        {"SLOT_NAME": "", "SLOT_REPO": "base"},
        {"SLOT_NAME": " ", "SLOT_REPO": "base"},
        {"SLOT_NAME": "base", "SLOT_REPO": ""},
    ):
        res = await client.post("/api/v1/slots", json=body, headers=auth(token))
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_slot"


async def test_repo_and_slot(yard):
    client, token, root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    bare = root / "repos" / "heldso.git"
    hook = (bare / "hooks" / "post-receive").read_text()
    assert "/api/v1/hooks/push" in hook
    assert not (bare / "hooks" / "pre-receive").exists()
    assert res.json()["name"] == "heldso"
    head = subprocess.check_output(["git", "-C", str(bare), "symbolic-ref", "HEAD"], text=True)
    assert head.strip() == "refs/heads/main"

    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "SLOT_DOMAIN": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    body = res.text
    values = res.json()["values"]
    assert values["SLOT_BRANCH"] == "main"
    assert values["SLOT_DOMAIN"] == "heldso.example.com"
    assert '"status":"waiting"' in body
    assert "no revision" in body

    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "other", "SLOT_REPO": "heldso", "SLOT_DOMAIN": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 409
    assert "domain_taken" in res.text

    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "yard-sampler", "SLOT_REPO": "heldso"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.post("/api/v1/repos", json={"name": "caddy"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "caddy", "SLOT_REPO": "caddy"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.put(
        "/api/v1/slots/heldso",
        json={"SLOT_DOMAIN": "app.example.com", "SLOT_BRANCH": "release"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert "app.example.com" in res.text
    assert "release" in res.text

    res = await client.delete("/api/v1/repos/heldso", headers=auth(token))
    assert res.status_code == 409
    assert "repo_in_use" in res.text

    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "heldso", "old": "000", "new": "abc", "ref": "refs/heads/release"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert "deploy skipped" in res.text
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "heldso", "old": "000", "new": "abc", "ref": "refs/heads/main"},
        headers=auth(token),
    )
    assert "no slot watches" in res.text

    res = await client.get("/api/v1/slots")
    assert res.status_code == 401
    assert "unauthorized" in res.text


async def test_slot_every(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "backup", "SLOT_REPO": "heldso", "SLOT_INTERVAL": "tomorrow"},
        headers=auth(token),
    )
    assert res.status_code == 400
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "backup", "SLOT_REPO": "heldso", "SLOT_INTERVAL": "every 5m"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert '"SLOT_INTERVAL":"5m"' in res.text
    res = await client.put("/api/v1/slots/backup", json={"SLOT_INTERVAL": ""}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["values"]["SLOT_INTERVAL"] == ""


async def test_slot_file(yard):
    client, token, _root = yard
    assert (await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))).status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "SLOT_DOMAIN": "heldso.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    res = await client.get("/api/v1/slots/heldso", headers=auth(token))
    vals = res.json()
    assert res.status_code == 200
    assert vals["SLOT_NAME"] == "heldso"
    assert vals["SLOT_DOMAIN"] == "heldso.example.com"
    res = await client.put(
        "/api/v1/slots/heldso",
        json={"SLOT_BRANCH": "login", "SLOT_REPO": "other", "API_TOKEN": "secret"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert "SLOT_REPO stays" in res.text
    assert "API_TOKEN" in res.text
    vals = (await client.get("/api/v1/slots/heldso", headers=auth(token))).json()
    assert vals["SLOT_BRANCH"] == "login"
    assert vals["SLOT_REPO"] == "heldso"
    assert vals["SLOT_DOMAIN"] == "heldso.example.com"
    assert vals["API_TOKEN"] == "secret"


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
    assert (junk / "hooks" / "post-receive").is_file()
    assert not (junk / "nope").exists()
    res = await client.post("/api/v1/repos", json={"name": "heldso", "force": True}, headers=auth(token))
    assert res.status_code == 409
    assert "already exists" in res.text
    assert (junk / "hooks" / "post-receive").is_file()


async def test_slot_deploys_existing_revision(yard):
    client, token, root = yard
    assert (await client.post("/api/v1/repos", json={"name": "app"}, headers=auth(token))).status_code == 200
    sha = _commit_empty(root / "repos" / "app.git")
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "app", "SLOT_REPO": "app", "SLOT_BRANCH": "main"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert sha in res.text
    assert '"status":"skipped"' in res.text
    vals = (await client.get("/api/v1/slots/app", headers=auth(token))).json()
    assert vals["DEPLOY_SHA"] == ""


async def test_experiment(yard):
    client, token, _root = yard
    one = {
        "title": "Fix the login cookie",
        "body": "The cookie is set on the wrong host.",
        "labels": [{"key": "repo", "value": "shop"}],
    }
    two = {
        "title": "Slow the health probe",
        "body": "The probe runs every second.",
        "labels": [{"key": "repo", "value": "yard"}],
    }
    res = await client.post("/api/v1/experiments", json=one, headers=auth(token))
    assert res.status_code == 200
    assert '"id":1' in res.text
    res = await client.post("/api/v1/experiments", json=two, headers=auth(token))
    assert res.status_code == 200
    assert '"id":2' in res.text
    res = await client.get("/api/v1/experiments", params={"repo": "shop"}, headers=auth(token))
    assert res.status_code == 200
    assert "Fix the login cookie" in res.text
    assert "Slow the health probe" not in res.text
    edited = {
        "title": "Fix the login cookie",
        "body": "The cookie is set on the wrong host.\nThe probe runs every second.",
        "labels": [{"key": "repo", "value": "shop"}],
    }
    res = await client.put("/api/v1/experiments/1", json=edited, headers=auth(token))
    assert res.status_code == 200
    assert "The probe runs every second." in res.text
    res = await client.delete("/api/v1/experiments/2", headers=auth(token))
    assert res.status_code == 200
    assert '"id":2' in res.text
    res = await client.get("/api/v1/experiments", headers=auth(token))
    assert "Slow the health probe" not in res.text
    assert "The probe runs every second." in res.text
    res = await client.put("/api/v1/experiments/99", json=one, headers=auth(token))
    assert res.status_code == 404
    res = await client.get("/api/v1/experiments/1", headers=auth(token))
    assert res.status_code == 200
    assert "Fix the login cookie" in res.text
    assert "Nope" not in res.text


async def test_token_tree(yard):
    client, admin, _root = yard
    res = await client.post("/api/v1/tokens", json={"name": "ada"}, headers=auth(admin))
    assert res.status_code == 200
    assert '"parent":"admin"' in res.text
    ada = res.json()["token"]
    res = await client.post("/api/v1/tokens", json={"name": "bot"}, headers=auth(ada))
    assert res.status_code == 200
    assert '"parent":"ada"' in res.text
    bot = res.json()["token"]
    assert (await client.get("/api/v1/repos", headers=auth(bot))).status_code == 200
    res = await client.delete("/api/v1/tokens/admin", headers=auth(bot))
    assert res.status_code == 403
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "shop", "old": "0", "new": "abcdef", "ref": "refs/heads/main", "actor": "ada"},
        headers=auth(admin),
    )
    assert res.status_code == 200
    assert (await client.delete("/api/v1/tokens/ada", headers=auth(admin))).status_code == 200
    assert (await client.get("/api/v1/repos", headers=auth(ada))).status_code == 401
    assert (await client.get("/api/v1/repos", headers=auth(bot))).status_code == 401
    assert (await client.get("/api/v1/repos", headers=auth(admin))).status_code == 200
    res = await client.get("/api/v1/tokens", headers=auth(admin))
    assert '"name":"bot"' in res.text
    assert '"revoked":true' in res.text


async def test_slot_leaves_when_branch_merges(yard):
    client, token, root = yard
    assert (await client.post("/api/v1/repos", json={"name": "shop"}, headers=auth(token))).status_code == 200
    bare = root / "repos" / "shop.git"
    sha = _seed_merged_feature(bare)
    assert (
        await client.post(
            "/api/v1/slots",
            json={"SLOT_NAME": "shop", "SLOT_REPO": "shop", "SLOT_BRANCH": "main"},
            headers=auth(token),
        )
    ).status_code == 200
    res = await client.post(
        "/api/v1/slots",
        json={"SLOT_NAME": "shop-wip", "SLOT_REPO": "shop", "SLOT_BRANCH": "feature", "SLOT_DOMAIN": "wip.example.com"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert '"SLOT_DOMAIN":"wip.example.com"' in res.text
    assert (
        await client.post(
            "/api/v1/slots",
            json={"SLOT_NAME": "shop-also", "SLOT_REPO": "shop", "SLOT_BRANCH": "feature"},
            headers=auth(token),
        )
    ).status_code == 200
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "shop", "old": "000", "new": sha, "ref": "refs/heads/main"},
        headers=auth(token),
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
            json={"SLOT_NAME": "shop-wip", "SLOT_REPO": "shop", "SLOT_BRANCH": "feature"},
            headers=auth(token),
        )
    ).status_code == 200
    zeros = "0" * 40
    res = await client.post(
        "/api/v1/hooks/push",
        json={"repo": "shop", "old": sha, "new": zeros, "ref": "refs/heads/feature"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert "slot shop-wip removed: branch feature was deleted" in res.text
    assert (await client.get("/api/v1/slots/shop-wip", headers=auth(token))).status_code == 404
    assert (await client.get("/api/v1/slots/shop", headers=auth(token))).status_code == 200


async def test_self_image_is_a_registry_reference(yard):
    client, token, _root = yard
    res = await client.put(
        "/api/v1/settings",
        json={"self_image": "ghcr.io/bobowski/yard:latest"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["self_image"] == "ghcr.io/bobowski/yard:latest"
    got = await client.get("/api/v1/settings", headers=auth(token))
    assert got.json()["self_image"] == "ghcr.io/bobowski/yard:latest"

    res = await client.put("/api/v1/settings", json={"self_image": "yard:latest"}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_image"

    made = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert made.status_code == 200
    res = await client.put(
        "/api/v1/settings",
        json={"self_repo": "heldso", "self_image": "ghcr.io/bobowski/yard:latest"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "one_source"

    res = await client.put("/api/v1/settings", json={"self_repo": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["self_repo"] == "heldso"
    assert res.json()["self_image"] == ""
    res = await client.put("/api/v1/settings", json={"self_repo": ""}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["self_repo"] == ""
    assert res.json()["self_image"] == "ghcr.io/bobowski/yard:latest"


async def test_boot_keeps_the_registry_image(tmp_path, monkeypatch):
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_HOOK_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_SELF_IMAGE", "ghcr.io/bobowski/yard:latest")
    async with TestClient(bootstrap) as client:
        token = (tmp_path / "data" / "yard" / "admin.token").read_text().strip()
        res = await client.get("/api/v1/settings", headers=auth(token))
    assert res.status_code == 200
    assert res.json()["self_image"] == "ghcr.io/bobowski/yard:latest"


async def test_container_inspect_rejects_a_bad_name(yard):
    client, token, _root = yard
    res = await client.get("/api/v1/containers/heldso")
    assert res.status_code == 401
    res = await client.get("/api/v1/containers/bad%20name", headers=auth(token))
    assert res.status_code == 400


def _commit_empty(directory: Path) -> str:
    tree = subprocess.check_output(["git", "-C", str(directory), "hash-object", "-t", "tree", "--stdin"], input=b"")
    commit = subprocess.check_output(
        ["git", "-C", str(directory), "commit-tree", tree.decode().strip(), "-m", "init"],
        text=True,
    )
    sha = commit.strip()
    subprocess.check_call(["git", "-C", str(directory), "update-ref", "refs/heads/main", sha])
    return sha


def _seed_merged_feature(bare: Path) -> str:
    hook = bare / "hooks" / "post-receive"
    off = hook.with_name("post-receive.off")
    hook.rename(off)
    try:
        work = bare.parent / "work"
        work.mkdir()
        _git(work, "init", "-b", "main")
        _git(work, "config", "user.email", "yard@example.com")
        _git(work, "config", "user.name", "Yard")
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
    subprocess.check_call(["git", "-C", str(directory), *args])
