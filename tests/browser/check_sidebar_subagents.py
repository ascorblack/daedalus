"""The desktop sidebar stays flat, its menus escape the sidebar, and subagents live in the header."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def centres(page, button: str) -> float:  # type: ignore[no-untyped-def]
    return page.locator(button).first.evaluate(
        """button => {
          const box = button.getBoundingClientRect();
          const icon = button.querySelector('svg').getBoundingClientRect();
          return Math.max(
            Math.abs((box.left + box.width / 2) - (icon.left + icon.width / 2)),
            Math.abs((box.top + box.height / 2) - (icon.top + icon.height / 2)),
          );
        }"""
    )


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)

        page.goto(f"{BASE}/agents?token=t&lang=en&scheme=dark", wait_until="networkidle")
        expect(page.locator(".pagehead-actions .iconbtn.primary")).to_be_visible()
        main_plus_error = centres(page, ".pagehead-actions .iconbtn.primary")
        if main_plus_error > 1:
            problems.append(f"the main new-agent plus is {main_plus_error:.1f}px off centre")

        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&scheme=dark&panel=details", wait_until="networkidle")
        expect(page.locator(".sidebar-new-agent")).to_be_visible()
        project = page.locator('.sidebar [data-project="9f3c2a1b7d40"]')
        expect(project.locator(".erow")).to_have_count(3)
        folder_height = project.locator(".folder-top").evaluate("node => node.getBoundingClientRect().height")
        row_height = project.locator(".erow").first.evaluate("node => node.getBoundingClientRect().height")
        if folder_height > 44 or row_height > 44:
            problems.append(f"the sidebar is still card-dense: folder={folder_height:.0f}px row={row_height:.0f}px")

        project.locator(".folder-top").hover()
        expect(project.locator(".folder-add")).to_be_visible()
        sidebar_plus_error = centres(page, '.sidebar [data-project="9f3c2a1b7d40"] .folder-add')
        if sidebar_plus_error > 1:
            problems.append(f"the project plus is {sidebar_plus_error:.1f}px off centre")

        row_menu = project.locator(".erow").first.locator(".session-row-menu button")
        row_menu.click()
        menu = page.locator(".menu[role='menu']")
        expect(menu).to_be_visible()
        geometry = menu.evaluate(
            """menu => {
              const box = menu.getBoundingClientRect();
              const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
              return { width: box.width, left: box.left, right: box.right, sidebarRight: sidebar.right,
                       menuZ: Number(getComputedStyle(menu).zIndex), sidebarZ: Number(getComputedStyle(document.querySelector('.sidebar')).zIndex) };
            }"""
        )
        if geometry["width"] > 300:
            problems.append(f"the session menu is {geometry['width']:.0f}px wide")
        if geometry["menuZ"] <= geometry["sidebarZ"]:
            problems.append(f"the session menu is behind the sidebar ({geometry['menuZ']} <= {geometry['sidebarZ']})")
        page.get_by_role("menuitem", name="Rename").click()
        expect(page.get_by_role("dialog", name="Rename")).to_be_visible()
        page.get_by_role("dialog").get_by_role("button", name="Close").click()

        trigger = page.locator(".subagents-trigger")
        expect(trigger).to_be_visible()
        trigger.click()
        popover = page.locator(".subagents-popover")
        expect(popover).to_be_visible()
        expect(popover).to_contain_text("link-check")
        popover_box = popover.bounding_box()
        assert popover_box
        if popover_box["width"] > 360 or popover_box["x"] + popover_box["width"] > 1432:
            problems.append(f"the subagent window is misplaced: {popover_box}")
        expect(page.locator('.details [id$="-info-subagents"]')).to_have_count(0)

        if os.environ.get("SHOTS"):
            page.screenshot(path="/tmp/daedalus-sidebar-subagents.png", full_page=True)
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.route("**/api/**", stub)
        phone.goto(f"{BASE}/agents?token=t&lang=en&scheme=dark", wait_until="networkidle")
        expect(phone.locator(".folder-add").first).to_be_visible()
        phone_plus_error = centres(phone, ".folder-add")
        if phone_plus_error > 1:
            problems.append(f"the phone project plus is {phone_plus_error:.1f}px off centre")
        medium = browser.new_page(viewport={"width": 688, "height": 900}, is_mobile=True, has_touch=True)
        medium.route("**/api/**", stub)
        medium.goto(f"{BASE}/agents?token=t&lang=en&scheme=dark", wait_until="networkidle")
        medium_add = medium.locator(".folder-add").first
        expect(medium_add).to_be_visible()
        expect(medium_add.locator("span")).to_have_count(0)
        medium_plus_error = centres(medium, ".folder-add")
        if medium_plus_error > 1:
            problems.append(f"the 688px project plus is {medium_plus_error:.1f}px off centre")
        medium.close()
        phone.goto(f"{BASE}/agents/{S1}?token=t&lang=en&scheme=dark", wait_until="networkidle")
        phone.locator(".subagents-trigger").click()
        phone_popover = phone.locator(".subagents-popover")
        expect(phone_popover).to_be_visible()
        phone_box = phone_popover.bounding_box()
        assert phone_box
        if phone_box["x"] < 8 or phone_box["x"] + phone_box["width"] > 382:
            problems.append(f"the phone subagent window runs off screen: {phone_box}")
        phone.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    raise SystemExit(run())
