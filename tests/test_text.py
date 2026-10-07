import pytest

from app.errors import YardError
from app.store import Slot, parse_every
from app.text import apply_slot_file, format_blocks, format_values, parse_blocks, parse_env, slot_values
from app.yard import git_repo


def test_parse_every():
    assert parse_every("5m") == "5m"
    assert parse_every("every 1h") == "1h"
    assert parse_every("off") == ""
    with pytest.raises(YardError):
        parse_every("tomorrow")


def test_parse_and_format_env():
    env = parse_env('# comment\n\nexport SECRET_KEY="ab c"\nAPI_URL=https://x\n')
    assert env == {"SECRET_KEY": "ab c", "API_URL": "https://x"}
    assert format_values(env).count("API_URL=https://x\n") == 1
    text = "\n".join(f"{key}={env[key]}" for key in sorted(env)) + "\n"
    assert text == "API_URL=https://x\nSECRET_KEY=ab c\n"
    for bad in ("not a line", "1BAD=x", "A=1\nA=2", "BAD KEY=x"):
        with pytest.raises(YardError):
            parse_env(bad)


def test_slot_file_round_trip():
    slot = Slot(
        "heldso",
        "heldso",
        "main",
        "",
        "heldso.example.com",
        "localhost/heldso:abc",
        "5m",
        {"API_TOKEN": "secret", "PORT": "9000"},
        "",
    )
    text = format_values(slot_values(slot))
    for line in (
        "SLOT_NAME=heldso",
        "SLOT_BRANCH=main",
        "SLOT_INTERVAL=5m",
        "DEPLOY_SHA=abc",
        "PORT=9000",
        "API_TOKEN=secret",
    ):
        assert line + "\n" in text
    edit = apply_slot_file(slot, text)
    assert edit.ref is None
    assert edit.domain is None
    assert edit.every is None
    assert edit.warn == []
    assert edit.env["API_TOKEN"] == "secret"
    assert edit.env["PORT"] == "9000"
    assert "STARIO_HOST" not in edit.env


def test_slot_file_keeps_read_only_keys():
    slot = Slot("heldso", "heldso", "main", "", "", "", "", {}, "")
    text = format_values(slot_values(slot))
    text = text.replace("SLOT_REPO=heldso", "SLOT_REPO=other", 1)
    text = text.replace("SLOT_DOMAIN=", "SLOT_DOMAIN=app.example.com", 1)
    text = text.replace("SLOT_INTERVAL=", "SLOT_INTERVAL=off", 1)
    edit = apply_slot_file(slot, text)
    assert edit.warn == ["SLOT_REPO stays heldso"]
    assert edit.domain == "app.example.com"
    assert edit.every == "off"


def test_slot_file_leaves_app_names_alone():
    slot = Slot("heldso", "heldso", "main", "", "heldso.example.com", "", "", {}, "")
    text = format_values(slot_values(slot)) + "DOMAIN=internal\nREF=other\n"
    edit = apply_slot_file(slot, text)
    assert edit.domain is None
    assert edit.ref is None
    assert edit.env["DOMAIN"] == "internal"
    assert edit.env["REF"] == "other"


def test_parse_blocks_round_trip():
    text = """# Fix the login cookie
@ id: 17
@ repo: shop
@ repo: yard

The cookie is set on the wrong host.
The host: the apex domain.

# Slow the health probe
@ id: 18
@ repo: yard

The probe runs every second.
"""
    blocks = parse_blocks(text)
    assert len(blocks) == 2
    assert blocks[0].id == 17
    assert blocks[0].has_id
    assert blocks[0].title == "Fix the login cookie"
    assert len(blocks[0].labels) == 2
    assert blocks[0].labels[1].value == "yard"
    assert "The host: the apex domain." in blocks[0].body
    assert blocks[1].id == 18
    assert blocks[1].title == "Slow the health probe"
    again = parse_blocks(format_blocks(blocks))
    assert again == blocks


def test_parse_body_keeps_hash_text():
    blocks = parse_blocks("# Title\n\nSee ## still a heading.\n@ id: 4\n")
    assert len(blocks) == 1
    assert not blocks[0].has_id
    assert "## still a heading." in blocks[0].body
    assert "@ id: 4" in blocks[0].body


def test_parse_block_errors():
    for text in (
        "#\n\nbody\n",
        "no title\n",
        "# Title\n@ repo\n\nbody\n",
        "# Title\n@ id: no\n\nbody\n",
        "# One\n@ id: 1\n\n# Two\n@ id: 1\n\n",
    ):
        with pytest.raises(YardError):
            parse_blocks(text)
    assert parse_blocks(" \n\n") == []


def test_git_repo():
    assert git_repo("/git/heldso.git/info/refs") == "heldso"
    assert git_repo("/git/../x.git") == ""
