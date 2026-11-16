"""Small dependency helpers keep route modules independent of concrete services."""

from typing import Any

from fastapi import Request


def state(request: Request) -> Any:
    return request.app.state


def setting(settings: Any, path: str, default: Any) -> Any:
    value = settings
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            value = getattr(value, part, default)
        if value is default:
            break
    return value
