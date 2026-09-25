"""Measure one terminal under a long flood: throughput, frames, the main thread, and a click during it.

Sixty-four megabytes of numbered lines go to a terminal in the session dock over a real loopback
socket (``terminal_flood.py``), as fast as the page's acknowledgements let them, once at full speed
and once at a 4x CPU throttle. Reported per run:

- rendered MB/s: bytes acknowledged (xterm.js acknowledges only what it has parsed) over the time;
- frames per second while the flood runs;
- long tasks: how many, and the worst;
- DOM nodes and JS heap at the end;
- the time from a click on the dock's Find button, halfway through the flood, to the search bar
  being in the page;
- the most output ever unacknowledged.

The claims checked: no long task over 200 ms, the click answered within 150 ms, and never more
unacknowledged than the window plus one batch. The machine's load average is printed with the
numbers; above about 8 the frame rate and the timings say more about the machine than the page, and
the long-task count and the DOM node count are the evidence that does not depend on it.

    cd miniapp && npm run build && cd ..
    python3 tests/browser/perf_terminal.py [--mib 64] [--rates 1,4] [--json out.json]

Exit 1 when a claim fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import S1, stub  # noqa: E402
from terminal_flood import BATCH, PROBE, Flood, FloodServer, numbered_lines  # noqa: E402
from terminal_stub import DEBUG, WINDOW, TerminalStub, dock_state, wait_live  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("APP_DIST", ROOT / "miniapp" / "dist"))
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
FLOOD = "perfflood000"
LONG_TASK_LIMIT_MS = 200
CLICK_LIMIT_MS = 150

# Records when the search bar enters the page, measured against the click's own pointerdown.
SEARCH_WATCH = """
() => {
  window.__perf.searchAt = null;
  window.__perf.clickAt = null;
  const seen = () => { if (document.querySelector('.term-search') && window.__perf.searchAt === null) window.__perf.searchAt = performance.now(); };
  new MutationObserver(seen).observe(document.body, { childList: true, subtree: true });
}
"""


def load() -> str:
    return " ".join(Path("/proc/loadavg").read_text().split()[:3])


def measure(browser, server: FloodServer, data: bytes, rate: int) -> dict:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    term.add(FLOOD, title="yes")
    flood = server.floods[FLOOD] = Flood(data=data)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    context.add_init_script(PROBE)
    context.add_init_script(dock_state(S1, [FLOOD], height=420))
    page = context.new_page()
    page.route("**/api/**", stub)
    page.route("**/api/terminals**", term.route)
    page.goto(f"{server.base}/agents/{S1}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".chat-scroll .timeline", timeout=20000)
    wait_live(page, FLOOD)
    page.wait_for_timeout(500)
    cdp = context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
    page.evaluate("() => { window.__perf.longTasks = []; window.__perf.frames = 0; }")
    page.evaluate(SEARCH_WATCH)
    load_before = load()
    started = time.monotonic()
    flood.go.set()
    clicked = False
    while flood.acked < len(data) and time.monotonic() - started < 600:
        if not clicked and flood.acked >= len(data) // 2:
            page.locator(".term-tools button[aria-label='Find']").click()
            clicked = True
        page.wait_for_timeout(100)
    took = (flood.finished_at or time.monotonic()) - started
    frames = page.evaluate("() => window.__perf.frames")
    perf = page.evaluate("() => ({ longTasks: window.__perf.longTasks, clickAt: window.__perf.clickAt, searchAt: window.__perf.searchAt })")
    renderer = page.evaluate("(id) => window.__terminals.renderer(id)", FLOOD)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
    counters = cdp.send("Memory.getDOMCounters")
    heap = cdp.send("Runtime.getHeapUsage")
    context.close()
    long_tasks = perf["longTasks"]
    click_ms = (perf["searchAt"] - perf["clickAt"]) if perf["clickAt"] is not None and perf["searchAt"] is not None else None
    return {
        "cpu_throttle": rate,
        "bytes": len(data),
        "finished": flood.acked >= len(data),
        "seconds": round(took, 2),
        "rendered_mb_s": round(flood.acked / took / 1e6, 2),
        "fps": round(frames / took, 1),
        "long_tasks": len(long_tasks),
        "worst_long_task_ms": round(max(long_tasks), 1) if long_tasks else 0,
        "click_to_search_ms": round(click_ms, 1) if click_ms is not None else None,
        "dom_nodes": counters["nodes"],
        "js_heap_mb": round(heap["usedSize"] / 1e6, 1),
        "max_unacked": flood.max_unacked,
        "renderer": renderer,
        "load_before": load_before,
        "load_after": load(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mib", type=int, default=64)
    parser.add_argument("--rates", default="1,4")
    parser.add_argument("--json")
    args = parser.parse_args()
    data, _ = numbered_lines(args.mib * 1024 * 1024)
    results = []
    problems: list[str] = []
    with FloodServer(DIST) as server, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
        for rate in (int(r) for r in args.rates.split(",")):
            result = measure(browser, server, data, rate)
            results.append(result)
            print(json.dumps(result))
            tag = f"at {rate}x"
            if not result["finished"]:
                problems.append(f"{tag}: the flood did not finish")
            if result["worst_long_task_ms"] > LONG_TASK_LIMIT_MS:
                problems.append(f"{tag}: a long task took {result['worst_long_task_ms']} ms (limit {LONG_TASK_LIMIT_MS})")
            if result["click_to_search_ms"] is None:
                problems.append(f"{tag}: the Find click during the flood never opened the search bar")
            elif result["click_to_search_ms"] > CLICK_LIMIT_MS:
                problems.append(f"{tag}: the Find click took {result['click_to_search_ms']} ms (limit {CLICK_LIMIT_MS})")
            if result["max_unacked"] > WINDOW + BATCH:
                problems.append(f"{tag}: {result['max_unacked']} bytes unacknowledged, past the window and one batch")
        browser.close()
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
