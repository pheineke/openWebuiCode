"""Provider backends by type.  Adding a service means adding one module here and registering it."""
from .base import Backend, ProxyError
from .deepseek import DeepSeek
from .openai_compat import OpenAICompat
from .openwebui import OpenWebUI

BACKENDS: dict[str, Backend] = {b.type: b for b in (OpenWebUI(), DeepSeek(), OpenAICompat())}


def get(kind: str | None) -> Backend | None:
    return BACKENDS.get(kind or "")


def of(p: dict) -> Backend:
    b = BACKENDS.get(p.get("type") or "")
    if not b:
        raise ProxyError(f"Unbekannter Anbieter-Typ: {p.get('type')}", 400)
    return b


def types() -> dict:
    return {k: b.info() for k, b in BACKENDS.items()}
