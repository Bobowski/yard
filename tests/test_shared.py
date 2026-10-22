"""Shared values: the error shape, name and env checks, atomic writes."""

import json
import stat
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.shared import (
    FORGE_HOSTS,
    YardError,
    age,
    atomic_write,
    check_app_name,
    check_domain,
    check_name,
    check_ref,
    check_repo,
    check_trigger,
    ensure_file,
    explain,
    parse_env,
)


def test_error_text_matches_stario() -> None:
    exc = YardError(
        400,
        "bad_name",
        "name must be a short lowercase word",
        context={"name": "Yard"},
        help_text="Start with a letter.",
        example="heldso",
    )
    assert str(exc) == (
        "name must be a short lowercase word\n  Context: name='Yard'\n  Help: Start with a letter.\n  Example:\nheldso"
    )
    body = {"error": exc.payload()}
    assert body["error"]["code"] == "bad_name"
    assert body["error"]["help"] == "Start with a letter."
    assert explain(json.dumps(body)) == str(exc)


def test_plain_text_stays_plain() -> None:
    assert explain("podman is down") == "podman is down"


def _names(*reject: str):
    banned = set(reject)

    def ok(name: str) -> bool:
        if name in banned:
            return False
        try:
            check_name(name)
        except YardError:
            return False
        return True

    return st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True).filter(ok)


def _hostnames():
    label = st.from_regex(r"[a-z][a-z0-9]{0,6}", fullmatch=True)
    return st.lists(label, min_size=1, max_size=3).map(".".join)


def _env_keys():
    return st.from_regex(r"[A-Z][A-Z0-9_]{1,12}", fullmatch=True).filter(lambda key: not key.startswith("PROJECT_"))


def _env_values():
    return st.from_regex(r"[A-Za-z0-9./:@_-]{0,16}", fullmatch=True)


def _bad_names():
    return st.one_of(
        st.just(""),
        st.just("yard-sampler"),
        st.just("Heldso"),
        st.from_regex(r"[A-Z][A-Za-z0-9]{0,6}", fullmatch=True),
        st.from_regex(r"yard-[a-z]{1,6}", fullmatch=True),
        st.from_regex(r"[0-9][a-z0-9]{0,6}", fullmatch=True),
    )


@example("heldso")
@example("yard")
@example("a")
@example("a-")
@given(_names())
def test_repo_names_pass(name: str) -> None:
    check_name(name)


@example("heldso")
@example("yard")
@given(_names("caddy"))
def test_app_names_pass(name: str) -> None:
    check_app_name(name)


@example("")
@example("yard-sampler")
@example("Heldso")
@given(_bad_names())
def test_repo_names_fail(name: str) -> None:
    with pytest.raises(YardError) as caught:
        check_name(name)
    assert caught.value.code == "bad_name"


def test_caddy_is_a_repo_name_and_not_an_app_name() -> None:
    check_name("caddy")
    with pytest.raises(YardError) as caught:
        check_app_name("caddy")
    assert caught.value.code == "bad_name"


_REF = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,16}", fullmatch=True).filter(
    lambda ref: ".." not in ref and not ref.startswith("/")
)


@example("main")
@example("feature/login")
@given(_REF)
def test_refs_pass(ref: str) -> None:
    check_ref(ref)


@example("")
@example("../x")
@example("/main")
@example("a b")
@example("a\tb")
@example("a\\b")
@given(
    st.one_of(
        st.just(""),
        st.just("../x"),
        st.just("/main"),
        st.just("a b"),
        st.from_regex(r"/[a-z]{1,8}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4}\.\.[a-z]{0,4}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4} [a-z]{0,4}", fullmatch=True),
    )
)
def test_refs_fail(ref: str) -> None:
    with pytest.raises(YardError) as caught:
        check_ref(ref)
    assert caught.value.code == "bad_ref"


_REPO_SIDE = st.from_regex(r"[A-Za-z0-9._-]+", fullmatch=True).filter(lambda text: text not in {".", ".."})


@example("https://origin.cursor.com/org/heldso")
@example("https://github.com/Bobowski/yard")
@given(
    st.builds(
        lambda host, a, b: f"https://{host}/{a}/{b}",
        st.sampled_from(FORGE_HOSTS),
        _REPO_SIDE,
        _REPO_SIDE,
    )
)
def test_forge_repos_pass(repo: str) -> None:
    assert check_repo(repo) == repo


@given(
    st.builds(
        lambda host, a, b, tail: f"https://{host}/{a}/{b}{tail}",
        st.sampled_from(FORGE_HOSTS),
        _REPO_SIDE,
        _REPO_SIDE,
        st.sampled_from(["/", ".git", ".git/"]),
    )
)
def test_forge_repo_trailers_normalize(host_and_repo: str) -> None:
    text = host_and_repo
    expected = text[:-1] if text.endswith("/") else text
    if expected.endswith(".git"):
        expected = expected[:-4]
    assert check_repo(text) == expected


@example("")
@example("noslash")
@example("org/heldso")
@example("gitlab.com/org/heldso")
@example("https://github.com/a/b/c")
@example("https://github.com/a b/c")
@given(
    st.sampled_from(
        [
            "",
            "noslash",
            "org/heldso",
            "gitlab.com/org/heldso",
            "https://github.com/a/b/c",
            "https://github.com//heldso",
            "https://github.com/org/",
            "https://github.com/a b/c",
            "a b",
        ]
    )
)
def test_forge_repos_fail(repo: str) -> None:
    with pytest.raises(YardError) as caught:
        check_repo(repo)
    assert caught.value.code == "bad_repo"


@example("")
@example("heldso.example.com")
@given(st.one_of(st.just(""), _hostnames()))
def test_domains_pass(domain: str) -> None:
    check_domain(domain)


@example("..")
@example("heldso..com")
@example("-bad")
@example("Bad.com")
@given(
    st.one_of(
        st.just(".."),
        st.just("heldso..com"),
        st.from_regex(r"[A-Z][A-Za-z]{1,6}", fullmatch=True),
        st.from_regex(r"-[a-z]{1,6}", fullmatch=True),
        _hostnames().map(lambda host: host + ".."),
    )
)
def test_domains_fail(domain: str) -> None:
    with pytest.raises(YardError) as caught:
        check_domain(domain)
    assert caught.value.code == "bad_domain"


def test_trigger_push() -> None:
    for text in ("push", "PUSH", ""):
        assert check_trigger(text) == "push"


def test_trigger_manual() -> None:
    for text in ("manual", "MANUAL"):
        assert check_trigger(text) == "manual"


@example("every")
@example("every 5m")
@example("weekly")
@example("5m")
@example("every tomorrow")
@given(st.sampled_from(["nightly", "weekly", "every 5m", "cron 5m", "on commit", "off", "service", "on push"]))
def test_trigger_rejects(text: str) -> None:
    with pytest.raises(YardError) as caught:
        check_trigger(text)
    assert caught.value.code == "bad_trigger"


@example({"API_URL": "https://x", "SECRET_KEY": "abc"})
@given(st.dictionaries(_env_keys(), _env_values(), max_size=5))
def test_env_lines_round_trip(env: dict[str, str]) -> None:
    text = "".join(f"{key}={env[key]}\n" for key in sorted(env))
    assert parse_env(text) == env


@example("ab c")
@given(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 ]{0,12}", fullmatch=True))
def test_quoted_env_values(value: str) -> None:
    assert parse_env(f'SECRET="{value}"\n') == {"SECRET": value}


def test_atomic_write_replaces_the_file(tmp_path: Path):
    path = tmp_path / "dir" / "state.json"
    atomic_write(path, "one\n")
    assert path.read_text() == "one\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    atomic_write(path, "two\n")
    assert path.read_text() == "two\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in path.parent.iterdir()] == ["state.json"]


def test_ensure_file_keeps_an_existing_file(tmp_path: Path):
    path = tmp_path / "env"
    ensure_file(path)
    assert path.read_text() == ""
    path.write_text("A=1\n")
    ensure_file(path)
    assert path.read_text() == "A=1\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_age():
    assert age(0.5) == "now"
    assert age(30) == "30s"
    assert age(300) == "5m"
    assert age(3 * 3600) == "3h"
    assert age(15 * 86400) == "15d"
