"""Drive the composer in a real browser and refuse what does not behave.

The composer's one circle has a unit test for what it means (miniapp/src/composer.test.ts); this is
the part that only exists once it is drawn against a host: a message goes out and the primary is
Send; while a run is on an empty pill is Stop and a written one queues a steer, which appears as a
card above the pill and is withdrawn with its ×; a tool call the policy refused is a dock above the
pill whose Allow once spends the key; the agent's question is a dock whose answer the circle sends
as Reply; the model list opens from inside the pill and a pick reaches the host; and while another
model stands in the selector says so and offers the way back.

The host is a stub in this file with the state a real one would hold — the session's status, the
queue, what was posted — so every step can be read back as the request the app made.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_composer.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, GATES, Unhandled, expect_app  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSION = "sess-1"
CONFIGURED = "claude-opus-5"
STANDBY = "deepseek-flash"

PRESETS = {
    "opus": {"provider": "claude", "model": CONFIGURED, "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000},
    "flash": {"provider": "deepseek", "model": STANDBY, "label": "DeepSeek Flash", "thinking": False, "reasoning_effort": "", "images": False, "context_window": 128000, "max_output_tokens": 16384},
}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-09-18T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """What the invented host holds, and what the app asked of it."""

    def __init__(self) -> None:
        self.status = "idle"
        self.queue: list[dict] = []
        self.posted: list[tuple[str, str, dict | None]] = []
        self.pending: dict | None = None
        self.fallback: dict | None = None
        self.messages = [message(101, "user", "Check the run and tell me what the log says."), message(102, "assistant", "One slow query on the events table; the plan is below.")]
        self.steer_route = True
        self.n = 0

    def detail(self) -> dict:
        return {
            "id": SESSION, "title": "A session", "status": self.status, "run_id": "r1" if self.status == "running" else None, "workspace": "/workspace",
            "workspace_name": "ws", "workspace_own": True, "workspace_sessions": [], "pending": self.pending, "model": "Claude Opus 5", "provider": "claude",
            "configured_model": CONFIGURED, "effective_model": STANDBY if self.fallback else CONFIGURED, "fallback": self.fallback, "mode": "", "brief": "",
            "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": {"tokens": 42000, "window": 200000, "messages": 38, "summaries": 1, "operator_turns": 6},
            "messages": self.messages,
        }

    def refuse(self, key: str) -> None:
        self.messages = self.messages + [
            message(103, "assistant", "", tool_calls=[{"id": "c_rm", "name": "Exec", "arguments": {"command": "rm -rf build"}}]),
            message(104, "tool", "", tool_results=[{"id": "c_rm", "content": f"refused by policy: destructive command.\nApproval key: {key}", "is_error": True}]),
        ]


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    url = req.url
    path = url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    body: object
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if req.method != "GET":
        data = json.loads(req.post_data) if req.post_data else None
        HOST.posted.append((req.method, rel, data))
        if rel == f"/api/sessions/{SESSION}/messages":
            if HOST.status == "running":
                HOST.n += 1
                HOST.queue.append({"id": f"q_{HOST.n:04d}", "text": str((data or {}).get("text", "")), "queued_at": "2026-09-18T12:01:00+00:00"})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"run_id": "r2"}))
        if rel.startswith(f"/api/sessions/{SESSION}/steer/"):
            sid = rel.rsplit("/", 1)[1]
            before = len(HOST.queue)
            HOST.queue = [q for q in HOST.queue if q["id"] != sid]
            if len(HOST.queue) == before:
                return route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "that message has already reached the agent"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"deleted": True}))
        if rel == f"/api/sessions/{SESSION}/stop":
            HOST.status = "idle"
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if rel == f"/api/sessions/{SESSION}/answer":
            HOST.pending = None
            HOST.status = "running"
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if rel == f"/api/sessions/{SESSION}/model":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"model": "DeepSeek Flash"}))
        return route.fulfill(status=200, content_type="application/json", body="{}")
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{SESSION}/steer":
        if not HOST.steer_route:
            return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "Not Found"}))
        body = HOST.queue
    elif rel == f"/api/sessions/{SESSION}":
        body = HOST.detail()
    elif rel.startswith(f"/api/sessions/{SESSION}/"):
        body = []
    elif rel == "/api/settings":
        body = {"model": {"preset": "opus"}, "presets": PRESETS}
    elif rel == "/api/sessions":
        body = {"sessions": [{"id": SESSION, "title": "A session", "status": HOST.status, "created_at": "2026-09-18T12:00:00+00:00", "last_message_at": "2026-09-18T12:00:04+00:00", "run_id": None}], "projects": [], "free": {"total": 1, "active": 0, "loops": 0, "last_message_at": ""}}
    elif rel.startswith("/api/usage/provider/"):
        body = {"provider": "claude", "today": {"calls": 4}, "subscription": None, "balance": None}
    elif rel in GATES:
        body = GATES[rel]
    else:
        UNHANDLED.record(rel)
        body = []
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def open_page(context, phone: bool = False) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_timeout(400)
    return page


def primary(page: Page) -> str:
    return page.locator(".composer .roundbtn.primary").get_attribute("data-action") or ""


def field(page: Page):  # type: ignore[no-untyped-def]
    return page.locator(".composer textarea")


def posts(kind: str) -> list[tuple[str, str, dict | None]]:
    return [p for p in HOST.posted if p[1].endswith(kind)]


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = open_page(context)
    # Nothing left over from an earlier run: the draft this check writes is the one it reads back.
    page.evaluate("() => localStorage.removeItem('daedalus.draft.sess-1')")

    # Idle: the circle is Send, disabled until there is something to send.
    print("idle primary:", primary(page), "disabled:", page.locator(".composer .roundbtn.primary").is_disabled())
    if primary(page) != "send" or not page.locator(".composer .roundbtn.primary").is_disabled():
        problems.append("at rest the circle is not a disabled Send")
    box = page.locator(".composer-box").bounding_box()
    print("pill at rest:", box and round(box["height"]))
    if not box or box["height"] > 52:
        problems.append(f"the pill at rest is {box and round(box['height'])} px, not one row")
    if not page.locator(".composer .model-select").count() or "Opus" not in page.locator(".composer .model-select").inner_text():
        problems.append("the model selector is not in the pill")
    if not page.locator(".composer .ctx-ring").count():
        problems.append("the context ring is not in the pill")
    ring = page.locator(".composer .ctx-ring").get_attribute("title") or ""
    if "21%" not in ring or "38" not in ring:
        problems.append(f"the ring's tooltip does not carry the numbers ({ring!r})")

    # Typing: Shift+Enter is a new line, Enter sends, the draft is remembered while it is being written.
    field(page).click()
    field(page).type("first line")
    page.keyboard.press("Shift+Enter")
    field(page).type("second line")
    if field(page).input_value() != "first line\nsecond line":
        problems.append(f"Shift+Enter did not insert a line ({field(page).input_value()!r})")
    if primary(page) != "send" or page.locator(".composer .roundbtn.primary").is_disabled():
        problems.append("with a draft the circle is not an enabled Send")
    two = page.locator(".composer-box").bounding_box()
    if not two or two["height"] <= box["height"]:
        problems.append("the pill did not grow with a second line")
    page.wait_for_timeout(500)
    stored = page.evaluate("() => localStorage.getItem('daedalus.draft.sess-1')")
    if stored != "first line\nsecond line":
        problems.append(f"the draft was not remembered ({stored!r})")
    page.reload()
    page.wait_for_selector(".composer textarea", timeout=15000)
    page.wait_for_timeout(300)
    if field(page).input_value() != "first line\nsecond line":
        problems.append(f"the draft did not come back after a reload ({field(page).input_value()!r})")
    field(page).click()
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    sent = posts("/messages")
    print("sent:", sent)
    if len(sent) != 1 or sent[0][2] != {"text": "first line\nsecond line"}:
        problems.append(f"Enter did not send the draft as a message ({sent})")
    if field(page).input_value() != "":
        problems.append("the field was not cleared after sending")
    if page.evaluate("() => localStorage.getItem('daedalus.draft.sess-1')"):
        problems.append("the stored draft was not cleared after sending")

    # A run is on: an empty pill is Stop; a written one queues a steer, which becomes a card with a ×.
    HOST.status = "running"
    page.reload()
    page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']", timeout=15000)
    print("running primary:", primary(page))
    field(page).click()
    field(page).type("also look at the log")
    if primary(page) != "queue":
        problems.append(f"a draft during a run is {primary(page)!r}, not queue")
    hint = page.locator(".composer-foot").inner_text() if page.locator(".composer-foot").count() else ""
    if "next step" not in hint:
        problems.append(f"the queue state carries no hint ({hint!r})")
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_selector(".composer .steer", timeout=5000)
    steered = posts("/messages")[-1]
    print("steered:", steered)
    if steered[2] != {"text": "also look at the log", "steer": True}:
        problems.append(f"the steer was not posted as one ({steered})")
    card = page.locator(".composer .steer")
    if card.count() != 1 or "also look at the log" not in card.inner_text():
        problems.append("the queued steer is not a card above the pill")
    if primary(page) != "stop":
        problems.append(f"after queuing, the circle is {primary(page)!r}, not stop")
    card.locator(".steer-x").click()
    page.wait_for_timeout(500)
    if page.locator(".composer .steer").count():
        problems.append("the card stayed after its × was pressed")
    deleted = [p for p in HOST.posted if p[0] == "DELETE"]
    print("withdrawn:", deleted)
    if not deleted or not deleted[-1][1].endswith("/steer/q_0001"):
        problems.append(f"the × did not DELETE the steer ({deleted})")

    # Ctrl+Shift+S stops the run, after the confirm.
    field(page).click()
    page.keyboard.press("Control+Shift+S")
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(400)
    if not posts("/stop"):
        problems.append("the stop shortcut did not reach the host")
    HOST.status = "idle"

    # A host without the route: no cards, no complaints.
    HOST.steer_route = False
    HOST.status = "running"
    page.reload()
    page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']", timeout=15000)
    page.wait_for_timeout(400)
    if page.locator(".composer .steer").count():
        problems.append("a host without the steer route still draws cards")
    HOST.steer_route = True
    HOST.status = "idle"

    # A refused tool call: the dock offers it once; Allow once spends the key.
    HOST.refuse("0123456789ab")
    page.reload()
    page.wait_for_selector(".composer .dock.approval", timeout=15000)
    dock = page.locator(".composer .dock.approval")
    print("approval dock:", dock.inner_text().replace("\n", " | "))
    if "Exec" not in dock.inner_text() or "rm -rf build" not in dock.inner_text():
        problems.append("the dock does not name the refused call")
    page.keyboard.press("y")
    page.wait_for_timeout(500)
    granted = posts("/policy/grant")
    print("granted:", granted)
    if not granted or granted[-1][2] != {"key": "0123456789ab"}:
        problems.append(f"the approval did not spend the key ({granted})")
    if page.locator(".composer .dock.approval").count():
        problems.append("the dock stayed after the key was spent")

    # The agent's question: a dock with the options; the circle reads Reply and sends the answer.
    HOST.messages = HOST.messages[:2]
    HOST.status = "waiting"
    HOST.pending = {"questions": [{"question": "Keep it to the usual 8?", "header": "Length", "options": [{"label": "Top 8", "description": "the usual"}, {"label": "All 14"}], "allow_custom": True}]}
    page.reload()
    page.wait_for_selector(".composer .dock.question", timeout=15000)
    print("waiting primary:", primary(page))
    if primary(page) != "reply":
        problems.append(f"with a question open the circle is {primary(page)!r}, not reply")
    if page.locator(".composer .dock.question .btn.option").count() != 2:
        problems.append("the question's options are not buttons in the dock")
    page.locator(".composer .dock.question .btn.option", has_text="Top 8").click()
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_timeout(500)
    answered = posts("/answer")
    print("answered:", answered)
    if not answered or answered[-1][2] != {"answers": [{"question": "Keep it to the usual 8?", "selected": ["Top 8"], "custom": None}]}:
        problems.append(f"Reply did not send the chosen answer ({answered})")
    HOST.status = "idle"

    # The model list: opens from the pill, names the presets with their kind, a pick reaches the host.
    page.reload()
    page.wait_for_selector(".composer .model-select", timeout=15000)
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-list .model-row", timeout=5000)
    rows = page.locator(".model-list .model-row").all_inner_texts()
    print("model rows:", [r.replace("\n", " ") for r in rows])
    if not any("Claude Opus 5" in r for r in rows) or not any("DeepSeek Flash" in r for r in rows):
        problems.append("the list does not name the presets")
    kinds = page.locator(".model-list .model-kind").evaluate_all("(els) => els.map((el) => el.className)")
    if not any("thinking" in k for k in kinds) or not any("fast" in k for k in kinds):
        problems.append(f"the presets carry no fast/thinking marker ({kinds})")
    if not page.locator(".model-list .model-row.on", has_text="Claude Opus 5").count():
        problems.append("the current model is not marked in the list")
    page.locator(".model-list .model-row", has_text="DeepSeek Flash").click()
    page.wait_for_timeout(400)
    picked = posts("/model")
    print("picked:", picked)
    if not picked or picked[-1][2] != {"preset": "flash"}:
        problems.append(f"the pick did not reach the host ({picked})")
    if page.locator(".model-list").count():
        problems.append("the list stayed open after a pick")
    field(page).click()
    page.keyboard.press("Control+M")
    page.wait_for_selector(".model-list", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".model-list").count():
        problems.append("Escape did not close the model list")

    # The fallback state: the button is amber and names both models; the list offers the way back.
    HOST.fallback = {"from": CONFIGURED, "to": STANDBY, "reason": "rate_limit"}
    page.reload()
    page.wait_for_selector(".composer .model-select.attn", timeout=15000)
    label = page.locator(".composer .model-select").inner_text()
    print("fallback label:", label)
    if "flash" not in label or "opus" not in label:
        problems.append(f"the selector does not say which model stands in for which ({label!r})")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-list .model-row.restore", timeout=5000)
    page.locator(".model-list .model-row.restore").click()
    page.wait_for_timeout(400)
    restored = posts("/model")[-1]
    print("restored:", restored)
    if restored[2] != {"preset": "opus"}:
        problems.append(f"the way back did not pick the configured preset ({restored})")
    HOST.fallback = None
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, phone=True)
    pill = page.locator(".composer-box").bounding_box()
    if not pill or pill["x"] < 0 or pill["x"] + pill["width"] > 391:
        problems.append(f"phone: the pill is outside the viewport ({pill})")
    if not page.locator(".composer .model-select").count():
        problems.append("phone: the model selector is not in the pill")
    fs = page.evaluate("() => getComputedStyle(document.querySelector('.composer textarea')).fontSize")
    if fs != "16px":
        problems.append(f"phone: the field is {fs}, which Safari would zoom into")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".sheet .model-list", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".sheet .model-list").count():
        problems.append("phone: the model sheet did not close on Escape")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser)
        problems += phone(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
