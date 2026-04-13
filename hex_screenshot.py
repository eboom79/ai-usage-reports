#!/usr/bin/env python3
"""
Take a screenshot of a Hex app URL by connecting to the user's already-running
Chrome browser via Chrome DevTools Protocol (CDP).

The screenshot is returned as raw PNG bytes — nothing is ever written to disk.

Requirements
------------
- pip install playwright && playwright install chromium
- Chrome launched with:  google-chrome --remote-debugging-port=9222

Usage (standalone test)
-----------------------
    python3 hex_screenshot.py <hex_url>
"""

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

def _default_chrome_bin() -> str:
    env_bin = os.getenv("CHROME_BIN")
    if env_bin:
        return env_bin

    candidates = []
    if platform.system() == "Darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
        ])
    else:
        candidates.extend([
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ])

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return shutil.which("google-chrome") or shutil.which("chromium") or candidates[0]


def _default_chrome_user_data() -> str:
    env_dir = os.getenv("CHROME_USER_DATA")
    if env_dir:
        return env_dir

    home = os.path.expanduser("~")
    if platform.system() == "Darwin":
        return os.path.join(home, "Library", "Application Support", "AI Usage Chrome Debug")
    return "/tmp/chrome-debug-hex"


CHROME_DEBUG_PORT = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
CHROME_BIN        = _default_chrome_bin()
CHROME_USER_DATA  = _default_chrome_user_data()
# Extra seconds to wait after page load so Hex JS charts finish rendering
RENDER_WAIT_MS = int(os.getenv("HEX_RENDER_WAIT_MS", "6000"))

HEX_LOGIN_URL = "https://app.hex.tech/redis/app/AI-usage-032EARbPw0YYLaJdMcBxfj/latest"


class HexLoginRequired(Exception):
    """Raised when Hex redirects to the login page instead of showing a report."""


def launch_chrome_to_login(port: int = CHROME_DEBUG_PORT) -> None:
    """Launch Chrome with remote debugging positioned off-screen.

    Placing the window at y=-10000 means it never appears visually and never
    steals focus — no timing race, no flicker.  Call _focus_chrome_window()
    only when login is actually required.
    """
    cmd = [
        CHROME_BIN,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CHROME_USER_DATA}",
        "--no-first-run",
        "--no-default-browser-check",
        HEX_LOGIN_URL,
    ]
    if platform.system() != "Darwin":
        cmd.insert(-1, "--window-position=0,-10000")   # off-screen on Linux
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Launched Chrome off-screen on port %d", port)


def _focus_chrome_window() -> None:
    """Move Chrome back on-screen and raise it so the user can log in."""
    if platform.system() == "Darwin":
        for script in (
            'tell application "Google Chrome" to activate',
            'tell application "Google Chrome" to reopen',
        ):
            try:
                subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                break
        log.info("Chrome activated for login on macOS")
        return

    # Move to a visible position first, then activate
    for cmd in (
        ["wmctrl", "-r", "Google Chrome", "-e", "0,100,100,1280,800"],
        ["wmctrl", "-r", "Google Chrome", "-b", "remove,hidden"],
        ["wmctrl", "-a", "Google Chrome"],
    ):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            break   # wmctrl not available, try xdotool
    try:
        subprocess.run(
            ["xdotool", "search", "--name", "Google Chrome",
             "windowmove", "100", "100", "windowraise", "windowfocus"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass
    log.info("Chrome window moved on-screen and raised for login")


async def _extract_cookies_async(port: int) -> list:
    """Connect to Chrome just long enough to grab session cookies, then disconnect."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        log.info("Connecting to Chrome on port %d to extract session cookies …", port)
        cdp_browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
        cdp_context = cdp_browser.contexts[0] if cdp_browser.contexts else None
        cookies = await cdp_context.cookies() if cdp_context else []
        log.info("Extracted %d cookies from Chrome session", len(cookies))
    return cookies


def extract_cookies(port: int = CHROME_DEBUG_PORT) -> list:
    """Synchronous wrapper — call once and share the result across threads."""
    return asyncio.run(_extract_cookies_async(port))


async def _open_hex_login_async(port: int = CHROME_DEBUG_PORT) -> None:
    """Raise Chrome, navigate to the Hex login page, and bring it into focus."""
    from playwright.async_api import async_playwright
    _focus_chrome_window()   # un-minimize and raise the window
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx and ctx.pages:
                page = ctx.pages[0]
                await page.bring_to_front()
                await page.goto(HEX_LOGIN_URL, wait_until="load", timeout=30_000)
                log.info("Opened Hex login page in real Chrome")
        except Exception as exc:
            log.warning("Could not open Hex login page in Chrome: %s", exc)


def open_hex_login(port: int = CHROME_DEBUG_PORT) -> None:
    """Synchronous wrapper for _open_hex_login_async."""
    asyncio.run(_open_hex_login_async(port))


async def _screenshot_one(p, url: str, cookies: list) -> bytes:
    """Take a single screenshot using an already-running Playwright instance *p*."""
    from io import BytesIO
    from PIL import Image

    # Keep report generation headless so it never steals focus from the user.
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(viewport={"width": 1600, "height": 900})
    if cookies:
        await context.add_cookies(cookies)
        log.info("[%s] Injected %d cookies", url[:60], len(cookies))

    page = await context.new_page()
    try:
        log.info("Navigating to: %s", url)
        await page.goto(url, wait_until="load", timeout=60_000)

        async def _looks_like_login() -> bool:
            return await page.evaluate("""
                () => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    return text.includes('log in with sso')
                        || text.includes('logging in')
                        || text.includes('sign up for hex')
                        || text.includes('log in to another workspace');
                }
            """)

        # Detect login state before doing any waiting
        if "/login" in page.url or "/auth/" in page.url or await _looks_like_login():
            raise HexLoginRequired(
                f"Hex is showing a login page ({page.url}). "
                "Please log in to Hex in Chrome first."
            )

        # Hex is a heavy JS app — wait for content to finish rendering.
        # The spinner appears a few seconds AFTER navigation, so we must:
        #   Phase 1 – initial pause to let the page start loading data
        #   Phase 2 – wait for the spinner to appear (so we know loading started)
        #   Phase 3 – wait for the spinner to disappear (loading finished)
        #   Phase 4 – wait for scroll height to stabilise

        SPINNER_JS = "() => document.querySelectorAll('[class*=\"HexSpinner__SpinnerWrapper\"]').length"
        GET_HEIGHT_JS = """
            () => {
                const el = document.getElementById('cellScrollParent-app')
                          || Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                              const style = window.getComputedStyle(el);
                              const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                                    style.overflowY === 'auto' || style.overflowY === 'scroll');
                              return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                          }, document.body);
                return el ? el.scrollHeight : 0;
            }
        """

        # Phase 1: initial pause (spinner hasn't appeared yet at 2 s)
        log.info("Phase 1: initial 5 s pause …")
        await page.wait_for_timeout(5000)

        if await _looks_like_login():
            raise HexLoginRequired(
                f"Hex is showing a login page after load ({page.url}). "
                "Please log in to Hex in Chrome first."
            )

        # Phase 2: wait for spinner to appear (up to 15 s)
        log.info("Phase 2: waiting for spinner to appear …")
        for attempt in range(15):
            count = await page.evaluate(SPINNER_JS)
            log.info("  spinner check %d: %d spinner(s)", attempt + 1, count)
            if count > 0:
                log.info("  spinner appeared — moving to phase 3")
                break
            await page.wait_for_timeout(1000)

        # Phase 3: wait for spinner to disappear (up to 90 s)
        log.info("Phase 3: waiting for spinner to clear …")
        for attempt in range(45):
            count = await page.evaluate(SPINNER_JS)
            log.info("  spinner poll %d: %d spinner(s) visible", attempt + 1, count)
            if count == 0:
                log.info("  spinner gone after %d polls", attempt + 1)
                break
            await page.wait_for_timeout(2000)
        else:
            log.warning("Spinner did not clear within timeout — proceeding anyway")

        # Phase 4: wait for scroll height to stabilise
        log.info("Phase 4: waiting for scroll height to stabilise …")
        stable_count = 0
        last_height = 0
        for attempt in range(10):  # max 20 s
            await page.wait_for_timeout(2000)
            height = await page.evaluate(GET_HEIGHT_JS)
            log.info("  height poll %d: scrollHeight=%d", attempt + 1, height)
            if height > 0 and height == last_height:
                stable_count += 1
                if stable_count >= 2:
                    log.info("Height stable at %d px", height)
                    break
            else:
                stable_count = 0
            last_height = height

        if await _looks_like_login():
            raise HexLoginRequired(
                f"Hex is showing a login page before screenshot ({page.url}). "
                "Please log in to Hex in Chrome first."
            )

        # Find the tallest scrollable container (Hex uses a custom scroll div)
        scroll_info = await page.evaluate("""
            () => {
                const el = Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                    const style = window.getComputedStyle(el);
                    const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                          style.overflowY === 'auto' || style.overflowY === 'scroll');
                    return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                }, document.body);
                return { scrollHeight: el.scrollHeight, tag: el.tagName, id: el.id, className: el.className.slice(0, 60) };
            }
        """)
        log.info("Scroll container: %s (scrollHeight=%d)", scroll_info, scroll_info['scrollHeight'])

        total_height = scroll_info['scrollHeight']

        # Get the scroll container's bounding rect so we can clip screenshots
        # to just the content area, excluding the fixed Hex toolbar above it.
        container_rect = await page.evaluate("""
            () => {
                const el = document.getElementById('cellScrollParent-app')
                          || Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                              const style = window.getComputedStyle(el);
                              const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                                    style.overflowY === 'auto' || style.overflowY === 'scroll');
                              return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                          }, document.body);
                const r = el.getBoundingClientRect();
                return { x: Math.round(r.x), y: Math.round(r.y),
                         width: Math.round(r.width), height: Math.round(r.height) };
            }
        """)
        log.info("Scroll container rect: %s", container_rect)

        # ── Diagnose inner scrollable tables ────────────────────────────────
        diag = await page.evaluate("""
            () => {
                const main = document.getElementById('cellScrollParent-app')
                          || Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                              const style = window.getComputedStyle(el);
                              const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                                    style.overflowY === 'auto' || style.overflowY === 'scroll');
                              return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                          }, document.body);
                const results = [];
                document.querySelectorAll('*').forEach(el => {
                    if (el === main) return;
                    const style = window.getComputedStyle(el);
                    const scrollable = style.overflow === 'auto'   || style.overflow === 'scroll' ||
                                       style.overflowY === 'auto'  || style.overflowY === 'scroll' ||
                                       style.overflowX === 'auto'  || style.overflowX === 'scroll';
                    if (scrollable && el.scrollHeight > el.clientHeight + 5) {
                        const rows = el.querySelectorAll('tr, [role=\"row\"]').length;
                        results.push({
                            tag:          el.tagName,
                            id:           el.id || '(none)',
                            cls:          el.className.slice(0, 80),
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight,
                            rowsInDOM:    rows,
                            childCount:   el.children.length,
                        });
                    }
                });
                results.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                return results.slice(0, 20);
            }
        """)
        log.info("── Inner scrollable elements (top overflowing) ──")
        for i, d in enumerate(diag):
            log.info(
                "  [%d] <%s> id=%s  scrollH=%d clientH=%d  rowsInDOM=%d children=%d  cls=%.80s",
                i, d['tag'], d['id'], d['scrollHeight'], d['clientHeight'],
                d['rowsInDOM'], d['childCount'], d['cls']
            )
        if not diag:
            log.info("  (no inner scrollable overflow found — page may use virtual scrolling or not yet rendered)")

        # ── Expand inner scrollable tables so every row is visible ──────────
        expanded = await page.evaluate("""
            () => {
                const main = document.getElementById('cellScrollParent-app')
                          || Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                              const style = window.getComputedStyle(el);
                              const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                                    style.overflowY === 'auto' || style.overflowY === 'scroll');
                              return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                          }, document.body);
                if (!main) return 0;
                let count = 0;
                document.querySelectorAll('*').forEach(el => {
                    if (el === main || !main.contains || !main.contains(el)) return;
                    const style = window.getComputedStyle(el);
                    const scrollable = style.overflow === 'auto'   || style.overflow === 'scroll' ||
                                       style.overflowY === 'auto'  || style.overflowY === 'scroll' ||
                                       style.overflowX === 'auto'  || style.overflowX === 'scroll';
                    if (scrollable && el.scrollHeight > el.clientHeight + 2) {
                        const extra = el.scrollHeight - el.clientHeight;
                        let hexCell = el;
                        while (hexCell.parentElement && hexCell.parentElement !== main) {
                            hexCell = hexCell.parentElement;
                        }
                        const cellH = parseFloat(window.getComputedStyle(hexCell).height);
                        if (!isNaN(cellH)) {
                            hexCell.style.height    = (cellH + extra) + 'px';
                            hexCell.style.maxHeight = 'none';
                        }
                        el.style.height    = el.scrollHeight + 'px';
                        el.style.maxHeight = 'none';
                        el.style.overflow  = 'visible';
                        let parent = el.parentElement;
                        while (parent && parent !== hexCell) {
                            parent.style.overflow  = 'visible';
                            parent.style.maxHeight = 'none';
                            parent = parent.parentElement;
                        }
                        count++;
                    }
                });
                return count;
            }
        """)
        log.info("Expanded %d inner scrollable element(s); Hex cells grown to match", expanded)

        post_diag = await page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('*').forEach(el => {
                    const rows = el.querySelectorAll('tr, [role=\"row\"]').length;
                    if (rows > 0) {
                        results.push({
                            tag: el.tagName,
                            id:  el.id || '(none)',
                            cls: el.className.slice(0, 80),
                            rows: rows,
                        });
                    }
                });
                const seen = new Set();
                return results.filter(r => {
                    if (seen.has(r.rows)) return false;
                    seen.add(r.rows);
                    return true;
                }).sort((a, b) => b.rows - a.rows).slice(0, 10);
            }
        """)
        log.info("── Row counts in DOM after expansion ──")
        for d in post_diag:
            log.info("  <%s> id=%s  rows=%d  cls=%.80s", d['tag'], d['id'], d['rows'], d['cls'])
        if not post_diag:
            log.info("  (no tr/[role=row] elements found at all)")

        await page.wait_for_timeout(400)
        new_scroll = await page.evaluate("""
            () => {
                const el = document.getElementById('cellScrollParent-app');
                return el ? el.scrollHeight : document.body.scrollHeight;
            }
        """)
        if new_scroll != total_height:
            log.info("Outer scrollHeight updated: %d → %d px", total_height, new_scroll)
            total_height = new_scroll

        clip_x      = container_rect['x']
        clip_y      = container_rect['y']
        clip_width  = container_rect['width']
        clip_height = container_rect['height']

        log.info("Capturing %d px in chunks (clip_height=%d) …", total_height, clip_height)

        scroll_js = """
            (target) => {
                const el = document.getElementById('cellScrollParent-app')
                          || Array.from(document.querySelectorAll('*')).reduce((best, el) => {
                              const style = window.getComputedStyle(el);
                              const isScrollable = (style.overflow === 'auto' || style.overflow === 'scroll' ||
                                                    style.overflowY === 'auto' || style.overflowY === 'scroll');
                              return (isScrollable && el.scrollHeight > best.scrollHeight) ? el : best;
                          }, document.body);
                el.scrollTop = target;
                return el.scrollTop;
            }
        """

        chunks = []
        prev_actual_top = 0
        y = 0
        while y < total_height:
            actual_top = await page.evaluate(scroll_js, y)
            await page.wait_for_timeout(600)

            chunk_bytes = await page.screenshot(clip={
                "x": clip_x,
                "y": clip_y,
                "width": clip_width,
                "height": clip_height,
            })
            img = Image.open(BytesIO(chunk_bytes))

            overlap_px = (prev_actual_top + clip_height) - actual_top
            if overlap_px > 0 and chunks:
                log.info("  chunk y=%d actual_top=%d overlap=%d px — cropping top",
                         y, actual_top, overlap_px)
                img = img.crop((0, overlap_px, img.width, img.height))

            if img.height > 0:
                chunks.append(img)

            prev_actual_top = actual_top
            y += clip_height

        total_width = chunks[0].width
        stitched_height = sum(c.height for c in chunks)
        stitched = Image.new("RGB", (total_width, stitched_height))
        offset = 0
        for chunk in chunks:
            stitched.paste(chunk, (0, offset))
            offset += chunk.height

        buf = BytesIO()
        stitched.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        log.info("Full screenshot captured: %d bytes (%d chunks)", len(png_bytes), len(chunks))
        return png_bytes
    finally:
        if page:
            await page.close()
        await browser.close()


async def _screenshot_async(url: str, cookies: list) -> bytes:
    """Convenience wrapper: owns the Playwright instance for a single URL."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await _screenshot_one(p, url, cookies)


async def _screenshot_all_async(leaders: list[dict], cookies: list) -> dict:
    """Run all screenshots sequentially under a single Playwright instance.

    Sequential is required because concurrent requests sharing the same Hex
    session cookies cause the server to return the same cached state for all.
    """
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        for leader in leaders:
            name = leader["name"]
            log.info("Generating report for %s …", name)
            try:
                results[name] = await _screenshot_one(p, leader["hex_url"], cookies)
            except Exception as exc:
                log.error("Failed to screenshot for %s: %s", name, exc)
                results[name] = exc
    return results


def screenshot_hex_url(url: str, port: int = CHROME_DEBUG_PORT, cookies: Optional[list] = None) -> bytes:
    """
    Screenshot *url* in a headless browser using session cookies from Chrome.

    Pass pre-extracted *cookies* (from extract_cookies()) to avoid contacting
    Chrome more than once.

    Raises:
        ConnectionError  – if Chrome is not reachable on *port*
        TimeoutError     – if the page does not load within 90 s
    """
    try:
        if cookies is None:
            cookies = extract_cookies(port)
        return asyncio.run(_screenshot_async(url, cookies))
    except Exception as exc:
        if "connect" in str(exc).lower() or "ECONNREFUSED" in str(exc):
            raise ConnectionError(
                f"Cannot connect to Chrome on port {port}. "
                "Make sure Chrome is running with --remote-debugging-port="
                f"{port}"
            ) from exc
        raise


def screenshot_all(leaders: list[dict], port: int = CHROME_DEBUG_PORT) -> dict[str, bytes]:
    """
    Screenshot multiple Hex reports concurrently under a single Playwright instance.

    *leaders* is a list of dicts with at least 'name' and 'hex_url' keys.
    Returns a dict mapping name → PNG bytes (or Exception on failure).
    """
    cookies = extract_cookies(port)
    return asyncio.run(_screenshot_all_async(leaders, cookies))


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <hex_url>")
        sys.exit(1)

    url = sys.argv[1]
    png_bytes = screenshot_hex_url(url)

    out_path = "/tmp/hex_test_screenshot.png"
    with open(out_path, "wb") as f:
        f.write(png_bytes)
    print(f"Screenshot saved to {out_path}  ({len(png_bytes):,} bytes)")
