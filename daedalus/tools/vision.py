"""``ImageView`` — look at an image with a small vision model and report what the agent asked."""

from __future__ import annotations

import mimetypes

from protocore.contracts.llm import LLMRequest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for

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
    if not target.is_file():
        return error(context, f"no such file: {target}")
    mime = mimetypes.guess_type(target.name)[0] or ""
    if mime not in SUPPORTED:
        return error(context, f"unsupported image type {mime or 'unknown'}; supported: {sorted(SUPPORTED)}")
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return error(context, f"image is {size} bytes; downscale it first (limit {MAX_IMAGE_BYTES})")
    vision = services.extra.get("vision")
    if not vision:
        return error(context, "no vision model is configured (set OPENROUTER_API_KEY or [vision] in the config)")
    provider, model, blobs, tenant = vision
    manager = services.extra.get("manager")
    max_out = int(getattr(getattr(getattr(manager, "config", None), "vision", None), "max_output_tokens", 2000))
    accepts = getattr(provider, "accepts_images", None)
    if accepts is None or not accepts(model):
        return error(context, "the vision preset is not marked as image-capable; enable 'images' on it in Settings → Models")
    meta = await blobs.put(tenant, target.read_bytes(), content_type=mime)
    instruction = (
        "You are the eyes of another AI agent. Look at the image and answer its request precisely. "
        "Quote text verbatim when asked to read; give numbers when asked about data; say clearly "
        "when something is not visible. "
    )
    instruction += "Be exhaustive and structured." if detail == "full" else "Be concise and specific."
    request = LLMRequest(
        model=model,
        messages=[
            Message(role=MessageRole.system, content_blocks=[TextBlock(text=instruction)]),
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
        return error(context, f"vision model failed: {exc}")
    text = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
    return ok(context, text or "(the vision model returned nothing)", model=model, image=str(target))


TOOLS = [image_view]

__all__ = ["TOOLS"]
