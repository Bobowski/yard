"""One API failure.

The text follows StarioError: a short message, then context, help, and an example.
"""

import json
from typing import Any

JsonScalar = str | int | float | bool | None


class YardError(Exception):
    """status and code identify the failure. message is the short reason."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        exit_code: int | None = None,
        *,
        context: dict[str, JsonScalar] | None = None,
        help_text: str | None = None,
        example: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.context = dict(context) if context else {}
        self.help_text = help_text
        self.example = example

    def shown_context(self) -> dict[str, JsonScalar]:
        found = dict(self.context)
        if self.exit_code is not None:
            found.setdefault("exit_code", self.exit_code)
        return found

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        shown = self.shown_context()
        if shown:
            body["context"] = shown
        if self.help_text:
            body["help"] = self.help_text
        if self.example:
            body["example"] = self.example
        return body

    def __str__(self) -> str:
        parts = [self.message]
        shown = self.shown_context()
        if shown:
            ctx = ", ".join(f"{key}={value!r}" for key, value in shown.items())
            parts.append(f"  Context: {ctx}")
        if self.help_text:
            parts.append(f"  Help: {self.help_text}")
        if self.example:
            parts.append(f"  Example:\n{self.example}")
        return "\n".join(parts)


def explain(text: str) -> str:
    """Turn an API error body into the same text as str(YardError)."""

    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return text
    raw_context = error.get("context")
    context: dict[str, JsonScalar] | None = None
    exit_code = None
    if isinstance(raw_context, dict):
        context = {}
        for key, value in raw_context.items():
            if not isinstance(key, str):
                continue
            if value is not None and not isinstance(value, str | int | float | bool):
                continue
            if key == "exit_code" and isinstance(value, int):
                exit_code = value
                continue
            context[key] = value
    help_text = error.get("help")
    example = error.get("example")
    return str(
        YardError(
            0,
            str(error.get("code") or ""),
            error["message"],
            exit_code,
            context=context,
            help_text=help_text if isinstance(help_text, str) else None,
            example=example if isinstance(example, str) else None,
        )
    )
