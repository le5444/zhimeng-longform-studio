"""Decode public pages and render client-side content in a fresh browser context."""

from __future__ import annotations

import codecs
import re
import time
from dataclasses import dataclass


def decode_web_text(raw: bytes, content_type: str) -> str:
    candidates: list[str] = []
    if raw.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    header = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    if header:
        candidates.append(header.group(1))
    # Older Chinese pages declare their encoding in HTML rather than HTTP headers.
    for meta in re.findall(rb"<meta\b[^>]*>", raw[:8192], re.I):
        match = re.search(rb"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", meta, re.I)
        if match:
            candidates.append(match.group(1).decode("ascii"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def is_verification_page(title: str | None, text: str) -> bool:
    if re.fullmatch(r"\s*(just a moment[.!…]*|请稍候[.!。…]*|security verification)\s*", title or "", re.I):
        return True
    return bool(re.search(
        r"checking your browser|performing security verification|verify you are human|"
        r"enable javascript and cookies to continue|正在验证.*浏览器",
        text[:1000], re.I,
    ))


def needs_browser(source: str, text: str) -> bool:
    if not re.search(r"<script\b", source, re.I):
        return False
    explicit = re.search(r"(?:enable|turn on|need to enable) javascript|请启用\s*javascript", text, re.I)
    runtime = re.search(
        r"<script\b[^>]*\bsrc\s*=|<script\b[^>]*\btype\s*=\s*[\"']module|"
        r"document\.(?:write|getElementById|querySelector|createElement)|\.innerHTML\s*=",
        source, re.I,
    )
    return bool(explicit or (runtime and len(text.strip()) < 1000))


@dataclass(frozen=True)
class RenderedWebPage:
    url: str
    status_code: int
    content_type: str
    title: str | None
    text: str
    truncated: bool = False


def render_web_page(url: str, *, max_characters: int) -> RenderedWebPage:
    """Use an installed browser without importing the user's cookies or profile."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise RuntimeError("网页浏览器依赖未安装，需安装运行时依赖 playwright。") from exc

    with sync_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True, timeout=5000)
                break
            except Error:
                continue
        if browser is None:
            raise RuntimeError("没有可用的 Edge、Chrome 或 Playwright Chromium 浏览器。")
        try:
            context = browser.new_context(accept_downloads=False, service_workers="block")
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # Network-idle never arrives on many sites. Instead wait a bounded time
            # for rendered text, then for it to stop changing (at least 2 seconds).
            start = time.monotonic()
            previous = ""
            stable = 0
            text = ""
            while time.monotonic() - start < 5:
                text = page.locator("body").inner_text(timeout=2000)
                stable = stable + 1 if text == previous else 0
                if text.strip() and stable >= 2 and time.monotonic() - start >= 2:
                    break
                previous = text
                page.wait_for_timeout(500)
            return RenderedWebPage(
                url=page.url,
                status_code=response.status if response else 0,
                content_type=response.headers.get("content-type", "") if response else "",
                title=page.title() or None,
                text=text[:max_characters],
                truncated=len(text) > max_characters,
            )
        except Error as exc:
            raise RuntimeError(f"浏览器加载网页失败：{exc}") from exc
        finally:
            browser.close()
