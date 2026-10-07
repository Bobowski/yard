"""Yard. HTTP API and the yard command."""

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    try:
        return version("staryard")
    except PackageNotFoundError:
        return "0.1.0"
