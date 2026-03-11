from __future__ import annotations

import html
import json
import os
import re
import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
import uvicorn

try:
    import webview
except ImportError:
    webview = None


SOURCE_URL = "https://substack.com/@slavojzizek"
PUBLICATION_URL = "https://slavoj.substack.com/p"
FEED_URL = "https://slavoj.substack.com/feed"
AUTHOR_NAME = "Slavoj Zizek"
DOWNLOAD_ROOT = Path(r"C:\Users\Amin\OneDrive\Documents\Online Articles")
AUTHOR_DIR = DOWNLOAD_ROOT / AUTHOR_NAME
STATE_PATH = Path("state.json")
SETTINGS_PATH = Path("settings.json")
PLAYWRIGHT_PROFILE_DIR = Path(".playwright-substack-profile")
LOGIN_URL = "https://substack.com/sign-in?redirect=%2F%40slavojzizek"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 20
MIN_VALID_HTML_BYTES = 256
login_browser_lock = threading.Lock()
login_browser_running = False


@dataclass
class Article:
    title: str
    url: str
    published: str | None = None


app = FastAPI(title="Article Collector")
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_published_for_display(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%b %d, %Y")
    except ValueError:
        return value


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "article"


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def get_substack_cookie() -> str | None:
    cookie = os.environ.get("SUBSTACK_COOKIE", "").strip()
    if cookie:
        return cookie
    settings = load_settings()
    cookie = str(settings.get("substack_cookie", "")).strip()
    return cookie or None


def request_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    cookie = get_substack_cookie()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def session_get(url: str, **kwargs: Any) -> requests.Response:
    headers = request_headers()
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        headers.update(extra_headers)
    return session.get(url, headers=headers, **kwargs)


def wait_for_server(url: str, timeout_seconds: float = 15.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=1.5)
            if response.ok:
                return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for local server at {url}") from last_error


def has_playwright_profile() -> bool:
    return PLAYWRIGHT_PROFILE_DIR.exists() and any(PLAYWRIGHT_PROFILE_DIR.iterdir())


def mark_login_browser_stopped() -> None:
    global login_browser_running
    with login_browser_lock:
        login_browser_running = False


def launch_login_browser() -> None:
    global login_browser_running
    with login_browser_lock:
        if login_browser_running:
            raise HTTPException(status_code=409, detail="Login browser is already open.")
        login_browser_running = True

    def worker() -> None:
        async def runner() -> None:
            PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(PLAYWRIGHT_PROFILE_DIR),
                    headless=False,
                    user_agent=USER_AGENT,
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT_SECONDS * 1000)
                    while any(not current_page.is_closed() for current_page in context.pages):
                        await asyncio.sleep(1)
                finally:
                    await context.close()

        try:
            asyncio.run(runner())
        finally:
            mark_login_browser_stopped()

    threading.Thread(target=worker, name="substack-login-browser", daemon=True).start()


async def export_article_from_browser(article: dict[str, Any]) -> str:
    PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(PLAYWRIGHT_PROFILE_DIR),
            headless=True,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 2200},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(article["url"], wait_until="domcontentloaded", timeout=TIMEOUT_SECONDS * 1000)
            except PlaywrightTimeoutError as exc:
                raise HTTPException(status_code=504, detail="Timed out while loading the article in the browser profile.") from exc

            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass

            await page.add_style_tag(
                content="""
                [data-testid*='subscribe'],
                [class*='subscribe'],
                [class*='subscription'],
                [class*='paywall'],
                [class*='modal'],
                [class*='popup'],
                [class*='banner'],
                form,
                button[aria-label*='Share'],
                button[aria-label*='share'] {
                  display: none !important;
                }
                [style*='position: fixed'],
                [style*='position: sticky'],
                [style*='position:sticky'] {
                  display: none !important;
                }
                """
            )
            await page.evaluate(
                """
                () => {
                  const badText = [
                    "type your email",
                    "subscribe",
                    "become a paid subscriber",
                    "download the substack app"
                  ];
                  for (const node of Array.from(document.querySelectorAll("section, div, aside, footer"))) {
                    const text = (node.innerText || "").trim().toLowerCase();
                    if (!text) continue;
                    if (badText.some(value => text.includes(value)) && node.querySelector("input, form, button")) {
                      node.remove();
                    }
                  }
                }
                """
            )
            await page.evaluate(
                """
                (articleUrl) => {
                  let base = document.querySelector("base");
                  if (!base) {
                    base = document.createElement("base");
                    document.head.prepend(base);
                  }
                  base.href = articleUrl;
                }
                """,
                article["url"],
            )

            body_text = await page.locator("body").inner_text()
            paragraph_count = await page.locator("article p, .body.markup p, .available-content p, .post-content p").count()
            preview_markers = (
                "subscribe to continue reading",
                "become a paid subscriber",
                "this post is for paid subscribers",
            )
            if paragraph_count < 5 and any(marker in body_text.lower() for marker in preview_markers):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "The browser profile still sees only the paid preview. Click Login, sign in to Substack "
                        "in the opened browser window, close that window, then try Save HTML again."
                    ),
                )

            html_content = await page.content()
        finally:
            await context.close()

    if len(html_content.encode("utf-8")) < MIN_VALID_HTML_BYTES:
        raise HTTPException(status_code=502, detail="Saved HTML was empty after browser rendering.")
    return html_content


def article_filename(article: Article) -> str:
    date_part = ""
    if article.published:
        try:
            parsed = datetime.fromisoformat(article.published.replace("Z", "+00:00"))
            date_part = parsed.strftime("%Y-%m-%d") + " - "
        except ValueError:
            date_part = ""
    return f"{date_part}{sanitize_filename(article.title)}.html"


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "articles": {},
            "last_checked_at": None,
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ensure_within_author_dir(path: Path) -> Path:
    resolved = path.resolve()
    author_dir = AUTHOR_DIR.resolve()
    if author_dir not in resolved.parents and resolved != author_dir:
        raise HTTPException(status_code=400, detail="Invalid PDF path.")
    return resolved


def normalize_article(article: Article) -> dict[str, Any]:
    html_path = AUTHOR_DIR / article_filename(article)
    return {
        "title": article.title,
        "url": article.url,
        "published": article.published,
        "is_new": False,
        "has_html": html_path.exists(),
        "html_path": str(html_path),
    }


def refresh_articles() -> list[dict[str, Any]]:
    state = load_state()
    fetched = fetch_articles()
    existing = state.get("articles", {})
    latest: dict[str, Any] = {}
    for article in fetched:
        article_data = normalize_article(article)
        previous = existing.get(article.url)
        if previous:
            article_data["first_seen_at"] = previous.get("first_seen_at")
            article_data["seen_at"] = previous.get("seen_at")
            article_data["is_new"] = previous.get("seen_at") is None
        else:
            article_data["first_seen_at"] = utc_now_iso()
            article_data["seen_at"] = None
            article_data["is_new"] = True
        latest[article.url] = article_data
    state["articles"] = latest
    state["last_checked_at"] = utc_now_iso()
    save_state(state)
    return list(latest.values())


def mark_article_seen(url: str) -> dict[str, Any]:
    state = load_state()
    article = state.get("articles", {}).get(url)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    if article.get("seen_at") is None:
        article["seen_at"] = utc_now_iso()
        article["is_new"] = False
        save_state(state)
    return article


def fetch_articles_from_feed() -> list[Article]:
    response = session_get(FEED_URL, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    parsed = feedparser.parse(response.text)
    articles: list[Article] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        published = None
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            published = datetime(*published_parsed[:6], tzinfo=timezone.utc).isoformat()
        articles.append(Article(title=title.strip(), url=link, published=published))
    return articles


def fetch_articles_from_page() -> list[Article]:
    response = session_get(PUBLICATION_URL, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    articles: list[Article] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href*='/p/']"):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        url = urljoin(PUBLICATION_URL, href)
        if url in seen_urls or "/publish/" in url:
            continue
        title = anchor.get_text(" ", strip=True)
        if not title or len(title) < 4:
            continue
        seen_urls.add(url)
        articles.append(Article(title=title, url=url))
    return articles


def fetch_articles() -> list[Article]:
    try:
        articles = fetch_articles_from_feed()
        if articles:
            return articles
    except requests.RequestException:
        pass
    try:
        articles = fetch_articles_from_page()
        if articles:
            return articles
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Substack: {exc}") from exc
    raise HTTPException(status_code=502, detail="No articles found on the source website.")


async def save_article_as_html(article: dict[str, Any]) -> Path:
    AUTHOR_DIR.mkdir(parents=True, exist_ok=True)
    output_path = AUTHOR_DIR / article_filename(
        Article(
            title=article["title"],
            url=article["url"],
            published=article.get("published"),
        )
    )
    if output_path.exists() and output_path.stat().st_size >= MIN_VALID_HTML_BYTES:
        return output_path
    if output_path.exists():
        output_path.unlink()

    html_content = await export_article_from_browser(article)
    output_path.write_text(html_content, encoding="utf-8")

    if output_path.stat().st_size < MIN_VALID_HTML_BYTES:
        output_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="Saved HTML was empty after browser export.")

    return output_path


INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Article Collector</title>
  <style>
    :root {
      --bg: #eef2f5;
      --bg-alt: #dfe8ec;
      --window: rgba(252, 253, 255, 0.82);
      --panel: rgba(255, 255, 255, 0.88);
      --line: rgba(21, 53, 75, 0.12);
      --line-strong: rgba(21, 53, 75, 0.22);
      --ink: #163042;
      --muted: #647786;
      --accent: #0d6c74;
      --accent-strong: #094f57;
      --accent-soft: rgba(13, 108, 116, 0.1);
      --warm: #ba5e35;
      --new: #fff4dc;
      --shadow: 0 24px 60px rgba(18, 40, 56, 0.14);
      --radius-lg: 28px;
      --radius-md: 18px;
      --radius-sm: 12px;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at 0% 0%, rgba(255, 255, 255, 0.92), transparent 28%),
        radial-gradient(circle at 100% 15%, rgba(116, 171, 179, 0.24), transparent 22%),
        linear-gradient(145deg, var(--bg) 0%, var(--bg-alt) 100%);
    }
    .shell {
      max-width: 1240px;
      margin: 0 auto;
      padding: 28px 18px 36px;
    }
    .window {
      border: 1px solid rgba(255, 255, 255, 0.55);
      border-radius: var(--radius-lg);
      background: var(--window);
      box-shadow: var(--shadow);
      backdrop-filter: blur(20px);
      overflow: hidden;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 22px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,0.8), rgba(247,250,252,0.58));
    }
    .traffic {
      display: flex;
      gap: 8px;
    }
    .traffic span {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      display: inline-block;
    }
    .traffic span:nth-child(1) { background: #f17f74; }
    .traffic span:nth-child(2) { background: #e8b860; }
    .traffic span:nth-child(3) { background: #5fc07c; }
    .titlebar {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }
    .titlecopy {
      min-width: 0;
    }
    .eyebrow {
      margin: 0 0 2px;
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-weight: 700;
    }
    h1 {
      margin: 0;
      font-size: clamp(1.5rem, 3vw, 2.5rem);
      line-height: 1.05;
      letter-spacing: -0.04em;
    }
    .subtext {
      margin-top: 8px;
      color: var(--muted);
      max-width: 62ch;
      font-size: 0.96rem;
    }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 12px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: rgba(248, 251, 252, 0.72);
    }
    .search {
      width: 100%;
      min-height: 46px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.95);
      padding: 0 18px;
      color: var(--ink);
      font: inherit;
      outline: none;
    }
    .search:focus {
      border-color: rgba(13, 108, 116, 0.45);
      box-shadow: 0 0 0 4px rgba(13, 108, 116, 0.08);
    }
    .toolbar-group {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .segmented {
      display: inline-flex;
      padding: 4px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
    }
    .segmented button {
      min-width: 84px;
    }
    .button {
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.14s ease, background-color 0.14s ease, border-color 0.14s ease;
    }
    .button:hover {
      transform: translateY(-1px);
    }
    .button[disabled] {
      opacity: 0.62;
      cursor: wait;
      transform: none;
    }
    .button.primary {
      background: var(--accent);
      color: #f8feff;
    }
    .button.primary:hover {
      background: var(--accent-strong);
    }
    .button.secondary {
      background: rgba(255,255,255,0.72);
      border-color: var(--line);
      color: var(--ink);
    }
    .button.secondary:hover,
    .button.secondary.active {
      border-color: rgba(13, 108, 116, 0.22);
      background: var(--accent-soft);
      color: var(--accent-strong);
    }
    .content {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 0;
      min-height: 68vh;
    }
    .sidebar {
      padding: 22px;
      border-right: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(247,250,252,0.72), rgba(242,247,248,0.48));
    }
    .stat-grid {
      display: grid;
      gap: 12px;
    }
    .stat {
      padding: 16px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: var(--panel);
    }
    .stat-label {
      color: var(--muted);
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }
    .stat-value {
      margin-top: 6px;
      font-size: 1.6rem;
      font-weight: 800;
      letter-spacing: -0.04em;
    }
    .stat-note {
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.45;
    }
    .feed-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      margin-top: 18px;
      text-decoration: none;
    }
    .main {
      padding: 22px;
    }
    .list-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }
    .list-title {
      font-size: 1.1rem;
      font-weight: 800;
      letter-spacing: -0.03em;
    }
    .list-subtitle {
      color: var(--muted);
      font-size: 0.94rem;
    }
    .status {
      min-height: 22px;
      color: var(--muted);
      font-size: 0.92rem;
      text-align: right;
    }
    .list {
      display: grid;
      gap: 14px;
    }
    .empty {
      padding: 28px;
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius-md);
      background: rgba(255,255,255,0.66);
      color: var(--muted);
      text-align: center;
    }
    .card {
      display: block;
      align-items: center;
      padding: 18px;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: 0 12px 26px rgba(19, 42, 58, 0.05);
    }
    .card.new {
      background: linear-gradient(180deg, rgba(255, 248, 232, 0.96), rgba(255,255,255,0.9));
      border-color: rgba(186, 94, 53, 0.18);
    }
    .card-head {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.76rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .pill.new {
      background: rgba(186, 94, 53, 0.12);
      color: var(--warm);
    }
    .pill.saved {
      background: rgba(13, 108, 116, 0.1);
      color: var(--accent-strong);
    }
    .title {
      color: var(--ink);
      text-decoration: none;
      font-size: 1.18rem;
      line-height: 1.4;
      font-weight: 700;
    }
    .title:hover {
      color: var(--accent-strong);
    }
    .title.new {
      font-weight: 800;
    }
    .meta {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.92rem;
    }
    @media (max-width: 980px) {
      .toolbar {
        grid-template-columns: 1fr;
      }
      .content {
        grid-template-columns: 1fr;
      }
      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
    }
    @media (max-width: 760px) {
      .topbar,
      .list-header,
      .card {
        grid-template-columns: 1fr;
      }
      .topbar,
      .list-header {
        display: grid;
      }
      .titlebar {
        align-items: flex-start;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="window">
      <header class="topbar">
        <div class="titlebar">
          <div class="traffic" aria-hidden="true">
            <span></span><span></span><span></span>
          </div>
          <div class="titlecopy">
            <div class="eyebrow">Article Collector</div>
            <h1>Slavoj Zizek Feed Monitor</h1>
            <div class="subtext">A cleaner control panel for unread posts, original links, and local file capture.</div>
          </div>
        </div>
        <button class="button primary" id="refreshButton" type="button">Refresh</button>
      </header>

      <section class="toolbar">
        <input class="search" id="searchInput" type="search" placeholder="Search by title...">
        <div class="toolbar-group segmented" role="tablist" aria-label="Article filters">
          <button class="button secondary active" data-filter="all" type="button">All</button>
          <button class="button secondary" data-filter="new" type="button">New</button>
        </div>
        <div class="toolbar-group">
          <button class="button secondary" id="notifyButton" type="button">Notifications</button>
          <a class="button secondary" href="https://substack.com/@slavojzizek" target="_blank" rel="noreferrer">Open Source</a>
        </div>
      </section>

      <section class="content">
        <aside class="sidebar">
          <div class="stat-grid">
            <div class="stat">
              <div class="stat-label">Articles</div>
              <div class="stat-value" id="count">0</div>
              <div class="stat-note">Total articles currently visible in the feed.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Unread</div>
              <div class="stat-value" id="newCount">0</div>
              <div class="stat-note">Unread items stay bold until you open them.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Last Check</div>
              <div class="stat-note" id="checked">Checking source...</div>
            </div>
          </div>
          <a class="button primary feed-link" href="https://substack.com/@slavojzizek" target="_blank" rel="noreferrer">Visit Substack</a>
        </aside>

        <section class="main">
          <div class="list-header">
            <div>
              <div class="list-title">Article Window</div>
              <div class="list-subtitle">Open originals, save local copies, and filter the list.</div>
            </div>
            <div class="status" id="status">Ready.</div>
          </div>
          <section class="list" id="articles"></section>
        </section>
      </section>
    </section>
  </main>

  <script>
    let knownNewUrls = new Set();
    let allArticles = [];
    let currentFilter = "all";
    let searchQuery = "";
    let lastCheckedAt = null;

    function formatDate(value) {
      if (!value) return "Date unavailable";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }

    function setStatus(message) {
      document.getElementById("status").textContent = message;
    }

    function updateStats(articles, checkedAt) {
      const unread = articles.filter(article => article.is_new).length;
      document.getElementById("count").textContent = String(articles.length);
      document.getElementById("newCount").textContent = String(unread);
      document.getElementById("checked").textContent = checkedAt ? formatDate(checkedAt) : "Not checked yet";
    }

    async function fetchJson(url, options = {}) {
      const response = await fetch(url, options);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || "Request failed");
      }
      return response.json();
    }

    async function markSeen(url) {
      await fetchJson("/api/articles/seen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url })
      });
    }

    function notifyForNewArticles(articles) {
      const fresh = articles.filter(article => article.is_new && !knownNewUrls.has(article.url));
      for (const article of fresh) {
        knownNewUrls.add(article.url);
        if (window.Notification && Notification.permission === "granted") {
          new Notification("New article", { body: article.title });
        }
      }
    }

    function applyFilters(articles) {
      return articles.filter(article => {
        const matchesSearch = article.title.toLowerCase().includes(searchQuery);
        const matchesFilter =
          currentFilter === "all" ||
          (currentFilter === "new" && article.is_new);
        return matchesSearch && matchesFilter;
      });
    }

    function makeMeta(text) {
      const node = document.createElement("span");
      node.textContent = text;
      return node;
    }

    function renderArticles(articles, checkedAt) {
      lastCheckedAt = checkedAt;
      allArticles = articles;
      notifyForNewArticles(articles);
      updateStats(articles, checkedAt);

      const filtered = applyFilters(articles);
      const container = document.getElementById("articles");
      container.innerHTML = "";

      if (!filtered.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No articles match the current filter.";
        container.append(empty);
        return;
      }

      for (const article of filtered) {
        const card = document.createElement("article");
        card.className = article.is_new ? "card new" : "card";

        const left = document.createElement("div");
        const head = document.createElement("div");
        head.className = "card-head";
        if (article.is_new) {
          const newPill = document.createElement("span");
          newPill.className = "pill new";
          newPill.textContent = "Unread";
          head.append(newPill);
        }

        const link = document.createElement("a");
        link.className = article.is_new ? "title new" : "title";
        link.href = article.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = article.title;
        link.addEventListener("click", async () => {
          try {
            await markSeen(article.url);
            article.is_new = false;
            renderArticles(allArticles, lastCheckedAt);
          } catch (error) {
            console.error(error);
          }
        });

        const meta = document.createElement("div");
        meta.className = "meta";
        meta.append(
          makeMeta(formatDate(article.published)),
          makeMeta(article.is_new ? "Unread" : "Opened")
        );

        left.append(head, link, meta);
        card.append(left);
        container.append(card);
      }
    }

    async function refresh() {
      const refreshButton = document.getElementById("refreshButton");
      refreshButton.disabled = true;
      setStatus("Refreshing articles...");
      try {
        const data = await fetchJson("/api/articles");
        renderArticles(data.articles, data.last_checked_at);
        setStatus("Ready.");
      } catch (error) {
        setStatus("Refresh failed.");
        throw error;
      } finally {
        refreshButton.disabled = false;
      }
    }

    function syncFilterButtons() {
      document.querySelectorAll("[data-filter]").forEach(button => {
        button.classList.toggle("active", button.dataset.filter === currentFilter);
      });
    }

    async function enableNotifications() {
      if (!window.Notification) {
        setStatus("This browser does not support notifications.");
        return;
      }
      if (Notification.permission === "granted") {
        setStatus("Notifications are already enabled.");
        return;
      }
      const permission = await Notification.requestPermission();
      setStatus(
        permission === "granted"
          ? "Notifications enabled."
          : "Notifications were not enabled."
      );
    }

    async function boot() {
      document.getElementById("refreshButton").addEventListener("click", refresh);
      document.getElementById("notifyButton").addEventListener("click", enableNotifications);
      document.getElementById("searchInput").addEventListener("input", event => {
        searchQuery = event.target.value.trim().toLowerCase();
        renderArticles(allArticles, lastCheckedAt);
      });
      document.querySelectorAll("[data-filter]").forEach(button => {
        button.addEventListener("click", () => {
          currentFilter = button.dataset.filter;
          syncFilterButtons();
          renderArticles(allArticles, lastCheckedAt);
        });
      });

      syncFilterButtons();
      if (window.Notification && Notification.permission === "default") {
        Notification.requestPermission().catch(() => {});
      }
      await refresh();
      setInterval(refresh, 120000);
    }

    boot().catch(error => {
      document.getElementById("count").textContent = "0";
      document.getElementById("checked").textContent = "Failed";
      setStatus(error.message);
    });
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/articles")
async def get_articles() -> dict[str, Any]:
    articles = refresh_articles()
    state = load_state()
    return {
        "articles": articles,
        "last_checked_at": state.get("last_checked_at"),
    }


@app.post("/api/articles/seen")
async def seen_article(payload: dict[str, str]) -> dict[str, Any]:
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing article URL.")
    article = mark_article_seen(url)
    return {"ok": True, "article": article}


@app.post("/api/session/login")
async def login_session() -> dict[str, str]:
    launch_login_browser()
    return {
        "detail": "Login browser opened. Sign in to Substack there, then close that browser window."
    }


@app.post("/api/articles/html")
async def create_html(payload: dict[str, str]) -> dict[str, str]:
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing article URL.")

    state = load_state()
    article = state.get("articles", {}).get(url)
    if not article:
        refresh_articles()
        state = load_state()
        article = state.get("articles", {}).get(url)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")

    output_path = await save_article_as_html(article)
    article["has_html"] = True
    article["html_path"] = str(output_path)
    state["articles"][url] = article
    save_state(state)
    return {"path": str(output_path)}


@app.get("/api/articles/html/open")
async def open_html(path: str) -> HTMLResponse:
    file_path = ensure_within_author_dir(Path(path))
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return HTMLResponse(file_path.read_text(encoding="utf-8"))


def run_server() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    url = "http://127.0.0.1:8000"
    if webview is None:
        raise SystemExit(
            "pywebview is required for the Windows app window. Install it with: "
            "python -m pip install pywebview"
        )

    server_thread = threading.Thread(target=run_server, name="article-collector-server", daemon=True)
    server_thread.start()
    wait_for_server(url)
    window = webview.create_window("Article Collector", url, width=1320, height=920)
    webview.start()
