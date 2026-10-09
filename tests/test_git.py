import pytest

from app.errors import YardError
from app.gitcmd import prepare_git


def test_log_is_allowed():
    call = prepare_git(["log", "-1", "--oneline", "main"])
    assert call.argv == ["log", "-1", "--oneline", "main"]
    assert call.delete_branch == ""


def test_fetch_uses_one_depth():
    call = prepare_git(["fetch"])
    assert call.argv == ["fetch", "--depth", "1", "origin", "+refs/heads/*:refs/remotes/origin/*"]
    assert prepare_git(["fetch", "origin"]).argv == call.argv
    with pytest.raises(YardError) as caught:
        prepare_git(["fetch", "--all"])
    assert caught.value.code == "bad_git"


@pytest.mark.parametrize(
    "args",
    [
        ["merge", "main"],
        ["commit", "-m", "x"],
        ["checkout", "main"],
        ["rebase", "main"],
        ["push", "origin", "main"],
        ["config", "user.name"],
        ["update-ref", "refs/heads/main", "abc"],
        ["branch", "feature"],
        ["remote", "add", "origin", "https://github.com/org/heldso"],
        ["log", "--output", "out.txt"],
        ["gc", "--aggressive"],
    ],
)
def test_bare_repo_rejects_other_git_commands(args: list[str]):
    with pytest.raises(YardError) as caught:
        prepare_git(args)
    assert caught.value.code == "bad_git"


def test_branch_delete_is_recorded():
    call = prepare_git(["branch", "-D", "feature"])
    assert call.argv == ["branch", "-D", "feature"]
    assert call.delete_branch == "feature"


def test_remote_set_url_keeps_an_http_address():
    call = prepare_git(["remote", "set-url", "origin", "https://github.com/org/heldso"])
    assert call.argv[-1] == "https://github.com/org/heldso"
