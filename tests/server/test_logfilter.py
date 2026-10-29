"""LogFilter.parse/select and the follow-side Held, with a caller clock."""

import pytest
from stario import json

from app.server.logfilter import _HOLD_SECONDS, Held, LogFilter
from app.shared import YardError

HEALTH_CHILD = json.dumps(
    {"name": "render", "trace_id": "t-health", "span_id": "s2", "parent_id": "s1", "status": "ok", "duration_ns": 100}
)
HEALTH_ROOT = json.dumps(
    {
        "name": "GET /health",
        "trace_id": "t-health",
        "span_id": "s1",
        "status": "ok",
        "duration_ns": 5000,
        "attributes": {"request.path": "/health"},
    }
)
NOTES_CHILD = json.dumps(
    {
        "name": "db.query",
        "trace_id": "t-notes",
        "span_id": "s4",
        "parent_id": "s3",
        "status": "error",
        "duration_ns": 700,
    }
)
NOTES_ROOT = json.dumps(
    {
        "name": "GET /notes",
        "trace_id": "t-notes",
        "span_id": "s3",
        "status": "ok",
        "duration_ns": 9000,
        "attributes": {"request.path": "/notes"},
    }
)
TEXT = "boom traceback"
PLAIN = json.dumps({"name": "tick", "status": "ok", "duration_ns": 50})
PAGE = [HEALTH_CHILD, HEALTH_ROOT, NOTES_CHILD, TEXT, NOTES_ROOT, PLAIN]


def _filter(hide: list[str] | None = None, only: list[str] | None = None) -> LogFilter:
    return LogFilter.parse(hide or [], only or []) or LogFilter([], [])


def test_parse_returns_none_without_expressions():
    assert LogFilter.parse([], []) is None


def test_parse_rejects_a_bad_number_and_an_empty_expr():
    for expr in ("duration_ns>slow", "status<", "", "   "):
        with pytest.raises(YardError) as caught:
            LogFilter.parse([expr], [])
        assert caught.value.status == 400
        assert caught.value.code == "bad_filter"


def test_field_rules_glob_and_compare():
    messages = [json.dumps({"status": "error", "duration_ns": 700, "ok": True, "nested": {"a": 1}})]
    assert _filter(only=["status=err*"]).select(messages) == [True]
    assert _filter(only=["status=warn*"]).select(messages) == [False]
    assert _filter(only=["status!=warn*"]).select(messages) == [True]
    assert _filter(only=["duration_ns>600"]).select(messages) == [True]
    assert _filter(only=["duration_ns<600"]).select(messages) == [False]
    assert _filter(only=["ok=true"]).select(messages) == [True]
    # A dict value and a missing key match no op, not even !=.
    assert _filter(only=["nested!=x"]).select(messages) == [False]
    assert _filter(only=["missing!=x"]).select(messages) == [False]
    assert _filter(hide=["missing!=x"]).select(messages) == [True]


def test_lookup_reads_the_top_level_before_attributes():
    record = json.dumps({"status": "ok", "attributes": {"status": "error"}})
    assert _filter(only=["status=error"]).select([record]) == [False]
    assert _filter(only=["status=ok"]).select([record]) == [True]


def test_select_decides_a_trace_as_one_group():
    # The /health lines carry no request.path except the root's attributes.
    assert _filter(hide=["request.path=/health"]).select(PAGE) == [
        False,
        False,
        True,
        True,
        True,
        True,
    ]
    # The notes child's error keeps the whole trace, root included.
    assert _filter(only=["status=error"]).select(PAGE) == [
        False,
        False,
        True,
        True,
        True,
        False,
    ]


def test_select_text_rules_apply_to_every_line():
    assert _filter(hide=["boom"]).select(PAGE) == [True, True, True, False, True, True]
    # A field rule ignores a text line entirely: it neither hides nor requires.
    assert _filter(only=["status=error"]).select([TEXT]) == [True]
    assert _filter(hide=["status=error"]).select([TEXT]) == [True]


def test_select_number_rules_read_group_lines():
    assert _filter(only=["duration_ns>800"]).select(PAGE) == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]


def test_held_waits_for_the_root_then_decides():
    held: Held[str] = Held(_filter())
    assert held.push("c1", HEALTH_CHILD, 0.0) == []
    assert held.push("r1", HEALTH_ROOT, 0.5) == ["c1", "r1"]
    assert held.drain() == []


def test_held_expires_a_rootless_trace():
    held: Held[str] = Held(_filter())
    held.push("c1", HEALTH_CHILD, 0.0)
    assert held.expire(_HOLD_SECONDS) == []
    assert held.expire(_HOLD_SECONDS + 0.5) == ["c1"]


def test_held_remembers_the_decision_for_late_children():
    held: Held[str] = Held(_filter(hide=["name=GET*"]))
    assert held.push("r1", NOTES_ROOT, 0.0) == []
    # The trace was hidden at its root; a late child follows that decision.
    assert held.push("c1", NOTES_CHILD, 1.0) == []
    # A kept trace keeps its late children too, even ones a rule would match.
    held = Held(_filter(hide=["db.query"]))
    assert held.push("r1", NOTES_ROOT, 0.0) == ["r1"]
    assert held.push("c1", NOTES_CHILD, 1.0) == ["c1"]


def test_held_decides_non_trace_lines_at_once():
    held: Held[str] = Held(_filter(hide=["boom"]))
    assert held.push("t1", TEXT, 0.0) == []
    assert held.push("p1", PLAIN, 0.0) == ["p1"]


def test_held_drain_flushes_what_it_has():
    held: Held[str] = Held(_filter())
    held.push("a", HEALTH_CHILD, 0.0)
    held.push("b", NOTES_CHILD, 0.1)
    assert held.drain() == ["a", "b"]
    assert held.drain() == []
