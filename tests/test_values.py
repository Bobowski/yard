"""Validators, durations, and env parsing. Properties for the pieces that parse."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.errors import YardError
from app.values import (
    check_domain,
    check_name,
    check_ref,
    check_slot_name,
    clean_dir,
    parse_duration,
    parse_env,
    parse_trigger,
)


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


def _rel_dirs():
    part = st.from_regex(r"[A-Za-z][A-Za-z0-9._-]{0,6}", fullmatch=True).filter(lambda text: text not in {".", ".."})
    return st.lists(part, min_size=1, max_size=3).map("/".join)


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
def test_slot_names_pass(name: str) -> None:
    check_slot_name(name)


@example("")
@example("yard-sampler")
@example("Heldso")
@given(_bad_names())
def test_repo_names_fail(name: str) -> None:
    with pytest.raises(YardError) as caught:
        check_name(name)
    assert caught.value.code == "bad_name"


def test_caddy_is_a_repo_name_and_not_a_slot_name() -> None:
    check_name("caddy")
    with pytest.raises(YardError) as caught:
        check_slot_name("caddy")
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


@example("")
@example(".")
@example("./")
@given(st.sampled_from(["", ".", "./", " . "]))
def test_blank_directories_clear(path: str) -> None:
    assert clean_dir(path) == ""


@example("projects/mono")
@given(_rel_dirs())
def test_directories_stay_inside_the_repo(path: str) -> None:
    got = clean_dir(path)
    assert clean_dir(got) == got
    assert not got.startswith("/")
    assert ".." not in got.split("/")


@example("..")
@example("../x")
@example("/etc")
@example("a/../../b")
@given(
    st.one_of(
        st.just(".."),
        st.just("/etc"),
        st.from_regex(r"\.\./[a-z]{1,6}", fullmatch=True),
        st.from_regex(r"/[a-z]{1,6}", fullmatch=True),
    )
)
def test_directories_reject_an_escape(path: str) -> None:
    with pytest.raises(YardError) as caught:
        clean_dir(path)
    assert caught.value.code == "bad_dir"


def test_parse_duration() -> None:
    assert parse_duration("5m") == 300
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2d") == 172800
    assert parse_duration("2s") == 2
    with pytest.raises(ValueError):
        parse_duration("")
    with pytest.raises(ValueError):
        parse_duration("tomorrow")
    with pytest.raises(ValueError):
        parse_duration("5x")
    with pytest.raises(ValueError):
        parse_duration("500ms")


@given(st.integers(1, 5000))
def test_trigger_every_seconds(n: int) -> None:
    if n % 3600 == 0:
        expect = f"{n // 3600}h"
    elif n % 60 == 0:
        expect = f"{n // 60}m"
    else:
        expect = f"{n}s"
    assert parse_trigger(f"every {n}s") == f"every {expect}"
    assert parse_trigger(f"every {expect}") == f"every {expect}"


def test_trigger_every_spellings() -> None:
    assert parse_trigger("every:5m") == "every 5m"
    assert parse_trigger("every  5m") == "every 5m"
    assert parse_trigger("Every 1H") == "every 1h"
    assert parse_trigger("every 90s") == "every 90s"
    assert parse_trigger("every 2d") == "every 2d"
    assert parse_trigger("every 172800s") == "every 2d"


def test_trigger_push() -> None:
    for text in ("push", "on push", "on_push", "PUSH"):
        assert parse_trigger(text) == "push"


def test_trigger_manual() -> None:
    for text in ("manual", "MANUAL"):
        assert parse_trigger(text) == "manual"


@example("")
@example("off")
@example("-")
@example("service")
@example("OFF")
@given(st.sampled_from(["", "off", "-", "service", "OFF", "Service", "  -  "]))
def test_trigger_clears(text: str) -> None:
    assert parse_trigger(text) == ""


@example("every")
@example("every 0s")
@example("every 500ms")
@example("weekly")
@example("5m")
@example("every tomorrow")
@given(st.sampled_from(["nightly", "weekly", "every night", "cron 5m", "on commit"]))
def test_trigger_rejects(text: str) -> None:
    with pytest.raises(YardError) as caught:
        parse_trigger(text)
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
