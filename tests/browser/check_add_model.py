"""Does "Add a model" record the model the operator ended on, and nothing of the one before it?

Step 2 offers a list and a text field. Picking from the list fills step 3 in from the catalogue —
the price, the label, the window, whether the model sees pictures — and typing an id afterwards is
how an operator reaches a model the endpoint did not list. Nothing on the screen says which of the
two the form now describes, so the check is that the requests say it: pick an expensive model with
a very large window, type a cheap small one, save, and read what went to the API.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8101 /tmp/app-root &
    APP_URL=http://127.0.0.1:8101/app python3 tests/browser/check_add_model.py

Exit 0 when the picked model's description does not reach the typed model.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import stub  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8101/app")
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

PICKED = "Claude Opus 5"  # $5.00/$25.00 in, a 1M window, sees pictures
TYPED = "z-ai/glm-5.3-flash"  # $0.15/$0.50, 200k, text only


def run() -> int:
    sent: list[tuple[str, dict]] = []
    failures: list[str] = []
    stub.fresh = True  # type: ignore[attr-defined]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.route("**/api/**", stub)
        page.on("request", lambda r: sent.append((r.url.split("/api/", 1)[1], json.loads(r.post_data or "{}"))) if r.method == "PUT" else None)
        page.goto(f"{BASE}/agents?token=t")
        page.wait_for_selector(".addmodel", timeout=15000)
        page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
        page.wait_for_selector(".modelgrid .pick", timeout=15000)
        page.locator(".modelgrid .pick", has_text=PICKED).first.click()
        page.wait_for_timeout(200)
        page.locator("input.field.mono").fill(TYPED)
        page.wait_for_timeout(200)
        page.locator(".addmodel-foot .btn.primary").click()
        page.wait_for_timeout(600)
        context.close()
        browser.close()
    stub.fresh = False  # type: ignore[attr-defined]

    if not sent:
        print("nothing was sent: the flow did not reach the save")
        return 1
    for path, body in sent:
        print("PUT", path, json.dumps(body, sort_keys=True))
    prices = {k: v for path, body in sent for k, v in (body.get("pricing") or {}).items()}
    presets = [body for path, body in sent if path.startswith("presets/")]
    if prices:
        failures.append(f"a price was recorded for a model that was typed, not picked: {prices}")
    if not presets:
        failures.append("no preset was written")
    for preset in presets:
        if preset.get("model") != TYPED:
            failures.append(f"the preset is for {preset.get('model')!r}, not the model that was typed")
        if preset.get("label"):
            failures.append(f"the preset carries a label from the picked model: {preset['label']!r}")
        if preset.get("images"):
            failures.append("the preset says the typed model sees pictures, which is the picked model's answer")
        if preset.get("context_window") != 128000:
            failures.append(f"the preset carries a window from the picked model: {preset.get('context_window')}")
    for line in failures:
        print("FAIL:", line)
    print("ok" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
