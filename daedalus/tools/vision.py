"""``ImageView`` — look at an image with a small vision model and report what the agent asked."""

from __future__ import annotations

import mimetypes
from typing import Any

from protocore.contracts.llm import LLMRequest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, refuse_protected, services_for

MAX_IMAGE_BYTES = 20_000_000
SUPPORTED = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@tool(
    name="ImageView",
    description=(
        "Look at an image file and answer a question about it, using a separate vision model so "
        "the image never enters your own context. Give the path and what you want to know "
        "(task): e.g. 'transcribe all text', 'describe the UI and any error messages', "
        "'what does this chart show, with numbers'. Use detail='full' for a long, exhaustive "
        "description; the default is a focused answer to the task."
    ),
)
async def image_view(context: ToolContext, path: str, task: str, detail: str = "focused") -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if refusal := refuse_protected(context, services, target, "read"):
        return refusal
    if not target.is_file():
        return error(context, f"no such file: {target}")
    mime = mimetypes.guess_type(target.name)[0] or ""
    if mime not in SUPPORTED:
        return error(context, f"unsupported image type {mime or 'unknown'}; supported: {sorted(SUPPORTED)}")
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return error(context, f"image is {size} bytes; downscale it first (limit {MAX_IMAGE_BYTES})")
    try:
        text, model = await look(services.extra.get("vision"), services.extra.get("manager"), target.read_bytes(), mime, task, detail=detail)
    except VisionUnavailable as exc:
        return error(context, str(exc))
    return ok(context, text, model=model, image=str(target))


class VisionUnavailable(Exception):
    """No vision model can look now; the message says what to change."""


async def look(vision: Any, manager: Any, data: bytes, mime: str, task: str, *, detail: str = "focused", instruction: str = "") -> tuple[str, str]:
    """Ask the configured vision model about an image; returns its answer and the model's name.

    The one path pixels take to a model: the image goes to a separate vision model and only its
    words come back, so no agent's own context ever carries an image. ``ImageView`` and the browser's
    ``BrowserLook`` both come through here. ``instruction`` replaces the opening words of the request
    for a caller that must say more about what it shows (a web page is not to be obeyed).
    """
    if not vision:
        raise VisionUnavailable("no vision model is configured (set OPENROUTER_API_KEY or [vision] in the config)")
    provider, model, blobs, tenant = vision
    max_out = int(getattr(getattr(getattr(manager, "config", None), "vision", None), "max_output_tokens", 2000))
    accepts = getattr(provider, "accepts_images", None)
    if accepts is None or not accepts(model):
        raise VisionUnavailable("the vision preset is not marked as image-capable; enable 'images' on it in Settings → Models")
    meta = await blobs.put(tenant, data, content_type=mime)
    text = instruction or (
        "You are the eyes of another AI agent. Look at the image and answer its request precisely. "
        "Quote text verbatim when asked to read; give numbers when asked about data; say clearly "
        "when something is not visible. "
    )
    text += "Be exhaustive and structured." if detail == "full" else "Be concise and specific."
    request = LLMRequest(
        model=model,
        messages=[
            Message(role=MessageRole.system, content_blocks=[TextBlock(text=text)]),
            Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text=f"Request: {task}")],
                metadata={"image_refs": [{"ref": meta.ref, "mime": mime}]},
            ),
        ],
        max_tokens=max_out if detail == "full" else min(800, max_out),
        temperature=0.1,
    )
    try:
        response = await provider.complete_text(request)
    except Exception as exc:  # noqa: BLE001
        raise VisionUnavailable(f"vision model failed: {exc}") from exc
    answer = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
    return answer or "(the vision model returned nothing)", str(model)


TOOLS = [image_view]

__all__ = ["TOOLS", "VisionUnavailable", "look"]
