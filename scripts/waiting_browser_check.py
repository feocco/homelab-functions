"""Drive the Waiting on me demo in headless Chrome and save screenshots.

Needs `pip install playwright` and a Chrome or Chromium binary. Start a demo
first (`python -m homelab.waiting_cli demo --port 8099`), then run:

    ./.venv/bin/python scripts/waiting_browser_check.py --url http://127.0.0.1:8099 --out /tmp/waiting-shots

It prints a JSON report and exits 1 when a check fails. The checks cover the
seven category headings, one primary action and a source link per card, the
copy control, live arrival and removal, the polling fallback, the merge gate,
and sending feedback. Against a demo started with `--no-cursor`, pass
`--feedback-off` to check the feedback-off state instead.
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
CATEGORY_LABELS = [
    "PR approve/merge",
    "Deploy approval",
    "Secret or key needed",
    "DNS or infra decision",
    "Question to answer",
    "Copy/paste value or command",
    "Evidence to review",
]


async def signed_in_page(browser: Browser, url: str, token: str, *, viewport, scheme: str, block_events=False) -> Page:
    context = await browser.new_context(
        viewport=viewport,
        color_scheme=scheme,
        device_scale_factor=2 if viewport is MOBILE else 1,
    )
    await context.grant_permissions(["clipboard-read", "clipboard-write"], origin=url)
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


async def card_shape(page: Page) -> list[dict]:
    return await page.locator(".item").evaluate_all(
        """cards => cards.map(card => ({
            id: card.dataset.id,
            category: card.dataset.category,
            need: card.querySelector('.need').textContent.trim(),
            why: card.querySelector('.why').textContent.trim(),
            primary: [...card.querySelectorAll('.action-open, .action-copy')].filter(el => !el.hidden).map(el => el.textContent.trim()),
            source: card.querySelector('.source').href,
            done: !card.querySelector('.done').hidden,
        }))"""
    )


async def run(url: str, token: str, out: Path, chrome: str | None, *, feedback_off: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        try:
            if feedback_off:
                page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
                notice = await page.text_content(".notice.info")
                disabled = await page.locator(".feedback-toggle").evaluate_all(
                    "buttons => buttons.every(button => button.disabled)"
                )
                check("feedback off: notice names CURSOR_API_KEY", "CURSOR_API_KEY" in (notice or ""), notice)
                check("feedback off: every feedback button is disabled", disabled)
                await page.screenshot(path=str(out / "feedback-off.png"), full_page=False)
                return {"ok": all(entry["ok"] for entry in checks), "url": url, "out": str(out), "checks": checks}

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
                if name == "overview-desktop-light":
                    headings = [
                        text.strip()
                        for text in await page.locator(".section:not([hidden]) .section-label").all_text_contents()
                    ]
                    check("headings are the seven categories, in order", headings == CATEGORY_LABELS, headings)
                    cards = await card_shape(page)
                    bad = [
                        card
                        for card in cards
                        if not card["need"]
                        or not card["why"]
                        or len(card["primary"]) != 1
                        or not card["source"].startswith("https://github.com/")
                    ]
                    check("every card shows need, why, one primary action, and a source link", not bad, bad or len(cards))
                    merge_cards = [card for card in cards if card["category"] == "approve_merge"]
                    check(
                        "approve/merge cards only link to GitHub and have no Done button",
                        merge_cards and all(not card["done"] and card["primary"] == ["Review and merge on GitHub"] for card in merge_cards),
                        merge_cards,
                    )
                await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
            copy_card = page.locator('.item[data-category="copy"]').first
            value = (await copy_card.locator(".copy-value").text_content() or "").strip()
            await copy_card.locator(".action-copy").click()
            await copy_card.locator(".action-copy", has_text="Copied").wait_for(timeout=5000)
            clipboard = await page.evaluate("navigator.clipboard.readText()")
            check("copy puts the exact value on the clipboard in one click", clipboard == value and bool(value), clipboard)

            before = await page.locator(".item").count()
            await page.wait_for_selector(".item.is-new", timeout=70000)
            new_need = await page.text_content(".item.is-new .need")
            check("a new item arrives live and is highlighted", await page.locator(".item").count() > before, new_need)
            await page.locator(".item.is-new").evaluate("card => card.scrollIntoView({block: 'center'})")
            await page.wait_for_timeout(400)
            await page.screenshot(path=str(out / "live.png"), full_page=False)

            secret = page.locator('.item[data-category="secret"]')
            await secret.locator(".done").click()
            await secret.wait_for(state="detached", timeout=5000)
            check("Done removes the item without a reload", await page.locator('.item[data-category="secret"]').count() == 0)
            await page.locator('.item[data-category="question"]').wait_for(state="detached", timeout=60000)
            check("an ask ticked on GitHub leaves the inbox live", True, await live_label(page))
            await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light", block_events=True)
            await page.locator('#live[data-mode="polling"]').wait_for(timeout=20000)
            check("blocked event stream falls back to polling", True, await live_label(page))
            await page.screenshot(path=str(out / "polling-fallback.png"), full_page=False)
            await page.context.close()

            page = await signed_in_page(browser, url, token, viewport=DESKTOP, scheme="light")
            card = page.locator('.item[data-category="deploy"]').first
            await card.locator(".feedback-toggle").click()
            await card.locator("textarea").fill("Roll out after 10pm so nobody is listening to the radio.")
            await card.locator(".send").click()
            await card.locator(".form-status.ok").wait_for(timeout=10000)
            status = await card.locator(".form-status").text_content()
            still_there = await page.locator('.item[data-category="deploy"]').count()
            check(
                "feedback starts one agent and never clears or approves the item",
                "Cursor agent started" in (status or "") and still_there == 1,
                status,
            )
            await card.evaluate("card => card.scrollIntoView({block: 'center'})")
            await page.wait_for_timeout(300)
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
