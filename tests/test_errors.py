import json

from app.errors import YardError, explain


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
