"""Drive the Waiting on me demo in headless Chrome and save screenshots.

Needs `pip install playwright` and a Chrome or Chromium binary. Start a demo
first (`python -m homelab.waiting_cli demo --port 8099`), then run:

    ./.venv/bin/python scripts/waiting_browser_check.py --url http://127.0.0.1:8099 --out /tmp/waiting-shots

It prints a JSON report and exits 1 when a check fails. The checks cover the
live indicator, a new card arriving, the polling fallback when the event
stream is blocked, and sending feedback. Against a demo started with
`--no-cursor`, pass `--feedback-off` to check the feedback-off state instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright


DESKTOP = {"width": 1280, "height": 900}
MOBILE = {"width": 390, "height": 844}


async def signed_in_page(browser: Browser, url: str, token: str, *, viewport, scheme: str, block_events=False) -> Page:
    context = await browser.new_context(
        viewport=viewport,
        color_scheme=scheme,
        device_scale_factor=2 if viewport is MOBILE else 1,
    )
    page = await context.new_page()
    if block_events:
        await page.route("**/waiting/events", lambda route: route.abort())
    await page.goto(f"{url}/waiting")
    await page.fill("#token", token)
    await page.click("button[type=submit]")
    await page.wait_for_selector(".item")
    return page


async def live_label(page: Page) -> str:
    return (await page.text_content("#live .live-label") or "").strip()


async def run(url: str, token: str, out: Path, chrome: str | None, *, feedback_off: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        if feedback_off:
            try:
                page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
                notice = await page.text_content(".notice.info")
                toggles = page.locator(".feedback-toggle")
                disabled = await toggles.evaluate_all("buttons => buttons.every(button => button.disabled)")
                check("feedback off: notice names CURSOR_API_KEY", "CURSOR_API_KEY" in (notice or ""), notice)
                check("feedback off: every feedback button is disabled", disabled, await toggles.count())
                await page.screenshot(path=str(out / "feedback-off.png"), full_page=False)
            finally:
                await browser.close()
            return {"ok": all(entry["ok"] for entry in checks), "url": url, "out": str(out), "checks": checks}
        try:
            for viewport, scheme, name in (
                (DESKTOP, "light", "overview-desktop-light"),
                (DESKTOP, "dark", "overview-desktop-dark"),
                (MOBILE, "light", "overview-mobile-light"),
                (MOBILE, "dark", "overview-mobile-dark"),
            ):
                page = await signed_in_page(browser, url, token, viewport=viewport, scheme=scheme)
                await page.locator('#live[data-mode="live"]').wait_for(timeout=10000)
                overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                check(f"{name}: live with no horizontal overflow", not overflow, await live_label(page))
                await page.screenshot(path=str(out / f"{name}.png"), full_page=True)
                await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
            before = await page.locator(".item").count()
            await page.wait_for_selector(".item.is-new", timeout=40000)
            after = await page.locator(".item").count()
            new_title = await page.text_content(".item.is-new .title")
            check("a new pull request arrives live and is highlighted", after > before, {"before": before, "after": after, "new": new_title})
            await page.locator(".item.is-new").evaluate("card => card.scrollIntoView({block: 'center'})")
            await page.wait_for_timeout(400)
            await page.screenshot(path=str(out / "live.png"), full_page=False)
            await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light", block_events=True)
            await page.locator('#live[data-mode="polling"]').wait_for(timeout=20000)
            check("blocked event stream falls back to polling", True, await live_label(page))
            await page.screenshot(path=str(out / "polling-fallback.png"), full_page=False)
            await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
            card = page.locator(".item").first
            await card.locator(".feedback-toggle").click()
            await card.locator("textarea").fill("Rename the route variable to match homelab-config, then rerun the route check.")
            await card.locator(".send").click()
            await card.locator(".form-status.ok").wait_for(timeout=10000)
            status = await card.locator(".form-status").text_content()
            await card.locator(".agent:not([hidden])").wait_for(timeout=10000)
            await card.locator(".agent-pr:not([hidden])").wait_for(timeout=30000)
            agent_text = " ".join((await card.locator(".agent").inner_text()).split())
            check(
                "feedback starts one agent and the card links its pull request",
                "Cursor agent" in agent_text and "Feedback PR #" in agent_text,
                {"status": status, "agent": agent_text},
            )
            await page.screenshot(path=str(out / "feedback.png"), full_page=False)
            await page.context.close()
        finally:
            await browser.close()
    return {"ok": all(entry["ok"] for entry in checks), "url": url, "out": str(out), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8099")
    parser.add_argument("--token", default="demo-token-not-a-secret")
    parser.add_argument("--out", type=Path, default=Path("/tmp/waiting-shots"))
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium"))
    parser.add_argument("--feedback-off", action="store_true", help="Check a demo started with --no-cursor.")
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run(args.url.rstrip("/"), args.token, args.out, args.chrome, feedback_off=args.feedback_off)
        )
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
