"""Inline answer media stays in the prose, opens above the app, and remains usable on a phone."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, GATES, Unhandled, expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
UNHANDLED = Unhandled()
SESSION = "media-session"
ALBUM = "11111111-1111-4111-8111-111111111111"
VIDEO = "22222222-2222-4222-8222-222222222222"
AUDIO = "33333333-3333-4333-8333-333333333333"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def item(item_id: str, kind: str, filename: str, *, alt: str = "", caption: str = "") -> dict:
    return {"id": item_id, "kind": kind, "mime_type": "image/png" if kind == "image" else f"{kind}/mp4" if kind == "video" else "audio/mpeg", "filename": filename, "byte_size": len(PNG), "width": 640 if kind == "image" else None, "height": 480 if kind == "image" else None, "alt": alt, "caption": caption}


MEDIA = [
    {"id": ALBUM, "layout": "album", "items": [
        item("image-one", "image", "one.png", alt="First chart", caption="Before"),
        item("image-two", "image", "two.png", alt="Second chart", caption="After"),
        item("image-three", "image", "three.png", alt="Third chart", caption="Detail"),
        item("image-four", "image", "four.png", alt="Fourth chart", caption="Result"),
    ]},
    {"id": VIDEO, "layout": "single", "items": [item("video-one", "video", "walkthrough.mp4", caption="Walkthrough")]},
    {"id": AUDIO, "layout": "single", "items": [item("audio-one", "audio", "summary.mp3", caption="Audio summary")]},
]
ANSWER = f"Before the album.\n\n![Charts](daedalus-media:{ALBUM})\n\nBetween the album and video.\n\n![Video](daedalus-media:{VIDEO})\n\n![Audio](daedalus-media:{AUDIO})\n\nAfter every attachment."
DETAIL = {
    "id": SESSION, "title": "Media answer", "status": "idle", "run_id": None,
    "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
    "workspace_sessions": [], "project": {"id": "p", "name": "Project", "root": "/workspace", "settings": {"snapshots": True}},
    "pending": None, "model": "some-model", "mode": "", "brief": "", "tools_off": [], "loop": None,
    "services": [], "subagents": [], "usage": {}, "context": {"tokens": 30, "window": 100000, "messages": 2, "summaries": 0, "operator_turns": 1},
    "messages": [
        {"role": "user", "summary": False, "internal": False, "origin": "operator", "seq": 10, "compaction": None, "archived": None, "headline": "", "text": "Show the result", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-21T00:00:00+00:00", "media": []},
        {"role": "assistant", "summary": False, "internal": False, "origin": "", "seq": 11, "compaction": None, "archived": None, "headline": "", "text": ANSWER, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-21T00:00:02+00:00", "media": MEDIA},
    ],
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    path = url.split("?", 1)[0]
    if path.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "/media/" in path and path.endswith("/content"):
        return route.fulfill(status=200, content_type="image/png", body=PNG)
    if path.endswith("/api/media/access"):
        return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url:
        body = json.dumps(DETAIL)
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps({"sessions": [], "projects": []})
    else:
        rel = path[path.index("/api/"):] if "/api/" in path else ""
        if rel in GATES:
            body = json.dumps(GATES[rel])
        else:
            if rel:
                UNHANDLED.record(rel)
            body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in (("phone", 390, 844), ("desktop", 1440, 900)):
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone", permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
            page.wait_for_selector(".inline-media.album img", timeout=15000)
            page.wait_for_function("() => [...document.images].filter(i => i.closest('.inline-media')).every(i => i.complete && i.naturalWidth > 0)")
            album_images = page.locator(".inline-media.album img").count()
            players = (page.locator(".inline-media video").count(), page.locator(".inline-media audio").count())
            order = page.locator(".answer-with-media").evaluate("el => [...el.children].map(x => x.className.includes('inline-media') ? 'media' : x.textContent.trim()).filter(Boolean)")
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            geometry = page.locator(".inline-media.album").evaluate("el => { const box=el.getBoundingClientRect(), answer=el.closest('.answer').getBoundingClientRect(), rail=el.querySelector('.inline-media-track'), caption=el.querySelector('figcaption'); return { centre: Math.abs((box.left+box.right)/2-(answer.left+answer.right)/2), scroll: rail.scrollWidth-rail.clientWidth, caption: getComputedStyle(caption).position } }")
            print(f"{name}: album={album_images}, players={players}, overflow={overflow}, geometry={geometry}, order={order}")
            if album_images != 4 or players != (1, 1):
                problems.append(f"{name}: expected four album images, video and audio; got {album_images}, {players}")
            if order[:3] != ["Before the album.", "media", "Between the album and video."] or order[-1] != "After every attachment.":
                problems.append(f"{name}: media did not retain its position in prose ({order})")
            if overflow > 1:
                problems.append(f"{name}: media made the page {overflow}px wider than the viewport")
            if geometry["centre"] > 2 or geometry["scroll"] <= 0 or geometry["caption"] != "absolute":
                problems.append(f"{name}: album is not centred, swipeable and overlaid ({geometry})")

            page.locator(".inline-media.album .inline-media-image").first.click()
            page.wait_for_selector(".media-viewer")
            page.wait_for_timeout(300)
            viewer = page.locator(".media-viewer").bounding_box()
            controls = page.locator(".media-viewer-tools .btn").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
            page.locator(".media-viewer-stage").dblclick()
            zoom = page.locator(".media-viewer-tools span").last.text_content()
            print(f"{name}: viewer={viewer}, controls={controls}, zoom={zoom}")
            if not viewer or viewer["width"] < width * 0.9 or viewer["height"] < height * 0.8:
                problems.append(f"{name}: viewer is not a full overlay ({viewer})")
            if any(value < 44 for value in controls):
                problems.append(f"{name}: viewer has a touch target below 44px ({controls})")
            if zoom != "200%":
                problems.append(f"{name}: double click did not zoom the image ({zoom})")
            page.keyboard.press("Escape")

            page.locator(".turn").last.hover()
            page.locator(".msg-actions").last.get_by_role("button", name="Copy", exact=True).click()
            copied = page.evaluate("() => navigator.clipboard.readText()")
            if "daedalus-media:" in copied or not all(label in copied for label in ("Before", "After", "Detail", "Result", "Walkthrough", "Audio summary")):
                problems.append(f"{name}: copy exposed internal references or lost labels ({copied!r})")
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
