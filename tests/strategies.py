"""Generators for Yard names, durations, and slot files."""

import string

from hypothesis import strategies as st

from app.errors import YardError
from app.store import check_name
from app.text import ORDER

_WORD = string.ascii_lowercase + string.digits + "-"


def names(*reject: str):
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


def slot_names():
    return names("caddy")


def repo_names():
    return names()


def hostnames():
    label = st.from_regex(r"[a-z][a-z0-9]{0,6}", fullmatch=True)
    return st.lists(label, min_size=1, max_size=3).map(".".join)


def rel_dirs():
    part = st.from_regex(r"[A-Za-z][A-Za-z0-9._-]{0,6}", fullmatch=True).filter(lambda text: text not in {".", ".."})
    return st.lists(part, min_size=1, max_size=3).map("/".join)


def env_keys():
    return st.from_regex(r"[A-Z][A-Z0-9_]{1,12}", fullmatch=True).filter(
        lambda key: key not in ORDER and not key.startswith(("SLOT_", "DEPLOY_"))
    )


def env_values():
    return st.from_regex(r"[A-Za-z0-9./:@_-]{0,16}", fullmatch=True)


def words():
    return st.from_regex(r"[A-Za-z0-9._:]{1,12}", fullmatch=True).filter(lambda word: not word.startswith("-"))


def bad_repos():
    return st.one_of(
        st.just(""),
        st.just("yard"),
        st.just("yard-sampler"),
        st.just("Heldso"),
        st.from_regex(r"[A-Z][A-Za-z0-9]{0,6}", fullmatch=True),
        st.from_regex(r"yard-[a-z]{1,6}", fullmatch=True),
        st.from_regex(r"[0-9][a-z0-9]{0,6}", fullmatch=True),
    )


def bad_slots():
    return st.one_of(bad_repos(), st.just("caddy"))


def bad_every():
    return st.from_regex(r"[a-z]{5,8}", fullmatch=True)


def titles():
    return st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,24}", fullmatch=True)


def body_lines():
    line = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 .,:@-]{0,30}", fullmatch=True)
    return line.filter(lambda text: text != "#" and not text.startswith("# "))


def exp_labels():
    key = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True).filter(lambda text: text != "id")
    value = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9.:_-]{0,12}", fullmatch=True)
    return st.tuples(key, value)
