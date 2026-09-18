"""Capability discovery for a llama.cpp HTTP server.

The chat API deliberately stays on the shared OpenAI-compatible adapter. Discovery does not: the
OpenAI model list has no portable place for the server's context size, tool template, concurrency or
sleep state, while llama.cpp publishes those facts at ``/props`` beside its ``/v1`` API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


@dataclass(slots=True)
class LlamaCppDiscovery:
    """What one server says about itself; absent facts stay ``None`` instead of being guessed."""

    reachable: bool = False
    base_url: str | None = None
    model_id: str | None = None
    context_window: int | None = None
    tools: bool | None = None
    images: bool | None = None
    slots: int | None = None
    build: str | None = None
    sleeping: bool | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _roots(base_url: str) -> tuple[str, str]:
    base = (base_url or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    if parsed.path.rstrip("/").endswith("/v1"):
        server_root = base[:-3].rstrip("/")
        return server_root, base
    return base, base + "/v1"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _images(modalities: Any) -> bool | None:
    if isinstance(modalities, dict):
        values = [modalities.get(name) for name in ("vision", "image", "images") if name in modalities]
        return any(value is True for value in values) if values else None
    if isinstance(modalities, list):
        names = {str(value).lower() for value in modalities}
        return bool(names & {"vision", "image", "images"})
    return None


def _model_facts(payload: dict[str, Any]) -> tuple[str | None, int | None, bool | None, bool | None]:
    data = payload.get("data")
    openai_rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    llama_rows_raw = payload.get("models")
    llama_rows = [row for row in llama_rows_raw if isinstance(row, dict)] if isinstance(llama_rows_raw, list) else []
    row = openai_rows[0] if openai_rows else {}
    llama = llama_rows[0] if llama_rows else {}
    model_id = str(row.get("id") or llama.get("model") or llama.get("name") or "").strip() or None
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    context = _positive_int(row.get("context_length") or meta.get("n_ctx"))
    capabilities = llama.get("capabilities")
    names = {str(value).lower() for value in capabilities} if isinstance(capabilities, list) else set()
    tools = True if names & {"tool", "tools", "tool_calls", "function_calling"} else None
    images = True if names & {"vision", "image", "images"} else None
    return model_id, context, tools, images


def _said(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    text = str(message or "").strip()
    return f" ({text[:160]})" if text else ""


async def discover_llamacpp(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> LlamaCppDiscovery:
    """Read ``/props`` and ``/v1/models`` without making either endpoint mandatory.

    Older servers and API-only reverse proxies have no ``/props``. Their model list is still useful,
    and any capability that list does not actually state stays unset so the operator's typed value
    wins. A completely unreadable server is returned as data with a reason, which lets the API,
    doctor and startup log give the same diagnosis.
    """
    try:
        server_root, api_root = _roots(base_url)
    except ValueError as exc:
        return LlamaCppDiscovery(reason=str(exc))
    result = LlamaCppDiscovery(base_url=api_root)
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    owns_client = client is None
    if client is None:
        # Redirects are not followed because these requests may carry an operator-supplied key. A
        # redirect to another origin is a configuration error, not permission to send the key there.
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    errors: list[str] = []
    props: dict[str, Any] | None = None
    models: dict[str, Any] | None = None
    try:
        for label, url in (("props", server_root + "/props"), ("models", api_root + "/models")):
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                errors.append(f"{label}: {exc.__class__.__name__}")
                continue
            if response.status_code != 200:
                errors.append(f"{label}: HTTP {response.status_code}{_said(response)}")
                continue
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{label}: not JSON")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{label}: JSON is not an object")
                continue
            if label == "props":
                known = {"default_generation_settings", "model_alias", "model_path", "chat_template_caps", "modalities", "total_slots", "is_sleeping", "build_info"}
                if not known.intersection(payload):
                    errors.append("props: no llama.cpp properties in the response")
                    continue
                props = payload
            else:
                data = payload.get("data")
                llama_models = payload.get("models")
                if not isinstance(data, list) and not isinstance(llama_models, list):
                    errors.append("models: no model list in the response")
                    continue
                model_id, _context, _tools, _images_value = _model_facts(payload)
                if not model_id:
                    errors.append("models: the model list has no id")
                    continue
                models = payload
    finally:
        if owns_client:
            await client.aclose()

    if props is not None:
        defaults = props.get("default_generation_settings")
        defaults = defaults if isinstance(defaults, dict) else {}
        params = defaults.get("params") if isinstance(defaults.get("params"), dict) else {}
        caps = props.get("chat_template_caps")
        caps = caps if isinstance(caps, dict) else {}
        result.model_id = str(props.get("model_alias") or "").strip() or None
        result.context_window = _positive_int(params.get("n_ctx"))
        tool_values = [caps.get(name) for name in ("supports_tools", "supports_tool_calls") if name in caps]
        result.tools = any(value is True for value in tool_values) if tool_values else None
        result.images = _images(props.get("modalities"))
        result.slots = _positive_int(props.get("total_slots"))
        result.build = str(props.get("build_info") or "").strip() or None
        result.sleeping = _boolean(props.get("is_sleeping"))
    if models is not None:
        model_id, context, tools, images = _model_facts(models)
        result.model_id = model_id or result.model_id
        result.context_window = result.context_window or context
        result.tools = result.tools if result.tools is not None else tools
        result.images = result.images if result.images is not None else images
    result.reachable = props is not None or models is not None
    if not result.reachable:
        result.reason = "; ".join(errors[:4]) or "the server returned no readable response"
    return result


def describe_discovery(result: LlamaCppDiscovery) -> str:
    """One compact sentence shared by the doctor and startup log."""
    if not result.reachable:
        return f"unreachable: {result.reason}"
    facts = ["reachable"]
    for label, value in (
        ("model", result.model_id),
        ("context", result.context_window),
        ("tools", result.tools),
        ("images", result.images),
        ("slots", result.slots),
        ("build", result.build),
        ("sleeping", result.sleeping),
    ):
        if value is not None:
            facts.append(f"{label}={str(value).lower() if isinstance(value, bool) else value}")
    return ", ".join(facts)


__all__ = ["LlamaCppDiscovery", "describe_discovery", "discover_llamacpp"]
