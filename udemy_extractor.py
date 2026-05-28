#!/usr/bin/env python3
"""
Udemy Course External Link Extractor

Opens a browser, walks every lecture in a Udemy (Business) course, and collects
all external links from descriptions and resource sections. Results are saved in
course order showing exactly where each link came from.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium
    python udemy_extractor.py --course-url "https://learning.udemy.com/course/YOUR-COURSE"

Options:
    --course-url    Udemy course URL                  [required]
    --output        Report file path                  [default: <slug>_links.txt]
    --headless      Hide the browser window           [default: False]
    --delay         Seconds to wait per lecture       [default: 2.0]
"""

import argparse
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ─────────────────────────────────────────────────────────────────────

SESSION_DIR = Path("udemy_session")

UDEMY_HOSTS = {"udemy.com", "learning.udemy.com", "udemycdn.com"}

KNOWN_TOOLS = {
    "github.com": "GitHub",
    "gist.github.com": "GitHub Gist",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
    "openai.com": "OpenAI",
    "platform.openai.com": "OpenAI Platform",
    "anthropic.com": "Anthropic",
    "claude.ai": "Claude",
    "huggingface.co": "Hugging Face",
    "colab.research.google.com": "Google Colab",
    "kaggle.com": "Kaggle",
    "replit.com": "Replit",
    "databricks.com": "Databricks",
    "docker.com": "Docker",
    "hub.docker.com": "Docker Hub",
    "selenium.dev": "Selenium",
    "playwright.dev": "Playwright",
    "cypress.io": "Cypress",
    "appium.io": "Appium",
    "browserstack.com": "BrowserStack",
    "saucelabs.com": "Sauce Labs",
    "lambdatest.com": "LambdaTest",
    "postman.com": "Postman",
    "aws.amazon.com": "AWS",
    "cloud.google.com": "Google Cloud",
    "azure.microsoft.com": "Azure",
    "stackoverflow.com": "Stack Overflow",
    "developer.mozilla.org": "MDN",
    "pypi.org": "PyPI",
    "npmjs.com": "npm",
    "medium.com": "Medium",
    "dev.to": "Dev.to",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "arxiv.org": "arXiv",
    "paperswithcode.com": "Papers with Code",
}

_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_chrome() -> Optional[str]:
    import os
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    return next((p for p in _CHROME_PATHS if Path(p).exists()), None)


def is_external(url: str) -> bool:
    if not url.startswith("http"):
        return False
    host = urlparse(url).netloc.lower().lstrip("www.")
    return not any(host == h or host.endswith("." + h) for h in UDEMY_HOSTS)


def tool_label(url: str) -> Optional[str]:
    host = urlparse(url).netloc.lower().lstrip("www.")
    for domain, name in sorted(KNOWN_TOOLS.items(), key=lambda x: -len(x[0])):
        if host == domain or host.endswith("." + domain):
            return name
    return None


def links_from_html(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        (a["href"].strip(), a.get_text(strip=True)[:150])
        for a in soup.find_all("a", href=True)
        if is_external(a["href"].strip())
    ]


# ── Curriculum ─────────────────────────────────────────────────────────────────

def get_curriculum(page) -> list[dict]:
    """Return [{title, href, section}] in sidebar order."""

    # Expand every collapsed section — try several selector patterns
    page.evaluate("""() => {
        const selectors = [
            '[data-purpose="section-panel-toggler"][aria-expanded="false"]',
            '[aria-expanded="false"][class*="section"]',
            '[aria-expanded="false"]'
        ];
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(b => { try { b.click(); } catch(e) {} });
        });
    }""")
    time.sleep(1.5)

    # Strategy 1 — data-purpose attributes (classic Udemy / Udemy Business)
    items = page.evaluate("""() => {
        const items = [];
        let section = "Introduction";
        const nodes = document.querySelectorAll('[data-purpose="section-panel-toggler"], [data-purpose="curriculum-item-link"]');
        for (const node of nodes) {
            if (node.dataset.purpose === "section-panel-toggler") {
                const spans = Array.from(node.querySelectorAll("span")).map(s => s.textContent.trim()).filter(t => t.length > 3);
                if (spans.length) section = spans[0];
            } else {
                const titleEl = node.querySelector('[data-purpose="item-title"]');
                const title = (titleEl || node).textContent.trim().slice(0, 120);
                const href = node.getAttribute("href");
                if (href && title) items.push({ title, href, section });
            }
        }
        return items;
    }""") or []

    if items:
        return items

    # Strategy 2 — find all lecture links by URL pattern (/learn/lecture/ or /learn/v4/)
    items = page.evaluate("""() => {
        const items = [];
        const links = Array.from(document.querySelectorAll('a[href*="/learn/lecture/"], a[href*="/learn/v4/"]'));
        for (const link of links) {
            const href = link.getAttribute("href");
            const title = (link.querySelector('[class*="title"]') || link).textContent.trim().slice(0, 120) || href;

            // Walk up to find section heading
            let section = "Course Content";
            let el = link.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!el) break;
                const heading = el.querySelector('h2, h3, h4, [class*="section-title"], [class*="chapter-title"]');
                if (heading) { section = heading.textContent.trim().slice(0, 100); break; }
                el = el.parentElement;
            }
            if (href) items.push({ title, href, section });
        }
        return items;
    }""") or []

    if items:
        return items

    # Strategy 3 — any sidebar link that looks like a lecture
    items = page.evaluate("""() => {
        const items = [];
        const links = Array.from(document.querySelectorAll('aside a[href], nav a[href], [class*="sidebar"] a[href], [class*="curriculum"] a[href]'));
        for (const link of links) {
            const href = link.getAttribute("href");
            if (!href || href === "#" || href.startsWith("javascript")) continue;
            const title = link.textContent.trim().slice(0, 120);
            if (title) items.push({ title, href, section: "Course Content" });
        }
        return items;
    }""") or []

    return items


# ── Per-lecture scraping ────────────────────────────────────────────────────────

def scrape_lecture(page, base_url: str, lecture: dict, delay: float) -> list[dict]:
    url = urljoin(base_url, lecture["href"])
    results = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        time.sleep(delay)
    except PlaywrightTimeout:
        print("timeout")
        return results

    def add_links(html: str, source: str):
        for link_url, text in links_from_html(html):
            results.append({
                "url": link_url,
                "text": text,
                "source": source,
                "section": lecture["section"],
                "lecture": lecture["title"],
                "tool": tool_label(link_url),
            })

    # Overview / description tab
    for sel in ['[data-purpose="overview-tab"]', 'button:has-text("Overview")',
                'button:has-text("Description")', '[role="tab"]:has-text("Overview")']:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                time.sleep(0.8)
                break
        except Exception:
            pass

    for sel in ['[data-purpose="lecture-description"]', '[class*="description--content"]',
                '[class*="description"]', '.ud-component--course-taking--tab-overview']:
        el = page.query_selector(sel)
        if el:
            add_links(el.inner_html(), "description")
            break

    # Resources tab
    for sel in ['[data-purpose="resources-tab"]', 'button:has-text("Resources")',
                '[role="tab"]:has-text("Resources")']:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                time.sleep(0.8)
                break
        except Exception:
            pass

    for sel in ['[data-purpose="lecture-resources"]', '[class*="resources--content"]',
                '[class*="resources"]']:
        el = page.query_selector(sel)
        if el:
            add_links(el.inner_html(), "resource")
            break

    return results


# ── Report ──────────────────────────────────────────────────────────────────────

def write_report(links: list[dict], course_url: str, output_path: Path):
    # Deduplicate, preserving first-seen order; collect extra locations
    seen: dict[str, dict] = {}
    deduped: list[dict] = []
    for lnk in links:
        if lnk["url"] not in seen:
            entry = {**lnk, "also_in": []}
            seen[lnk["url"]] = entry
            deduped.append(entry)
        else:
            loc = f"{lnk['section']} › {lnk['lecture']} [{lnk['source']}]"
            if loc not in seen[lnk["url"]]["also_in"]:
                seen[lnk["url"]]["also_in"].append(loc)

    W = 78
    lines = [
        "=" * W,
        "  UDEMY COURSE — EXTERNAL LINKS",
        f"  {course_url}",
        f"  {len(deduped)} unique links",
        "=" * W,
    ]

    cur_section = None
    for i, lnk in enumerate(deduped, 1):
        if lnk["section"] != cur_section:
            cur_section = lnk["section"]
            lines += ["", f"  ── {cur_section} ──"]

        tag = f" [{lnk['tool']}]" if lnk["tool"] else ""
        lines.append(f"\n  {i}.{tag}")
        lines.append(f"     {lnk['url']}")
        if lnk["text"]:
            lines.append(f"     \"{lnk['text']}\"")
        lines.append(f"     → {lnk['lecture']} [{lnk['source']}]")
        for loc in lnk["also_in"]:
            lines.append(f"     → also: {loc}")

    tools: dict[str, int] = {}
    for lnk in deduped:
        if lnk["tool"]:
            tools[lnk["tool"]] = tools.get(lnk["tool"], 0) + 1

    if tools:
        lines += ["", "=" * W, "  TOOLS REFERENCED"]
        for tool, count in sorted(tools.items()):
            lines.append(f"  • {tool}  ({count})")
    lines.append("=" * W)

    output_path.write_text("\n".join(lines), encoding="utf-8")

    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps({"course_url": course_url, "total": len(deduped), "links": deduped},
                   indent=2),
        encoding="utf-8",
    )
    print(f"\n  {len(deduped)} links  →  {output_path}  |  {json_path}")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Extract external links from a Udemy course")
    ap.add_argument("--course-url", required=True)
    ap.add_argument("--output", default=None)
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--delay", type=float, default=2.0,
                    help="Seconds to wait after loading each lecture (default: 2.0)")
    ap.add_argument("--debug", action="store_true", default=False,
                    help="Save page HTML to debug_page.html if curriculum is empty")
    args = ap.parse_args()

    host = urlparse(args.course_url).netloc
    base_url = f"https://{host}"
    slug = re.search(r"/course/([^/?#]+)", args.course_url)
    output_path = Path(args.output) if args.output else Path(f"{slug.group(1) if slug else 'course'}_links.txt")

    SESSION_DIR.mkdir(exist_ok=True)
    chrome = find_chrome()
    launch_kw: dict = {
        "user_data_dir": str(SESSION_DIR),
        "headless": args.headless,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if chrome:
        print(f"Browser: {chrome}")
        launch_kw["executable_path"] = chrome

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ── Login ──────────────────────────────────────────────────────────────
        print(f"\n[1/4] Opening course …")
        page.goto(args.course_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)

        if "login" in page.url or "sso" in page.url or page.query_selector('input[name="email"]'):
            print("  Please log in, then press ENTER …")
            input()
            page.goto(args.course_url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

        # Navigate to the course player if still on landing page
        if "/learn/" not in page.url:
            page.goto(args.course_url.rstrip("/") + "/learn/",
                      wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

        # ── Curriculum ─────────────────────────────────────────────────────────
        print("[2/4] Reading curriculum …")
        curriculum = get_curriculum(page)
        print(f"  {len(curriculum)} lectures found")

        if not curriculum:
            if args.debug:
                debug_file = Path("debug_page.html")
                debug_file.write_text(page.content(), encoding="utf-8")
                print(f"  Saved page HTML → {debug_file}  (open in a browser to inspect)")
            print("  No lectures found — make sure you're enrolled and the sidebar is visible.")
            ctx.close()
            return

        # ── Scrape each lecture ────────────────────────────────────────────────
        print("[3/4] Scanning lectures …\n")
        all_links: list[dict] = []
        cur_section = None

        for i, lecture in enumerate(curriculum, 1):
            if lecture["section"] != cur_section:
                cur_section = lecture["section"]
                print(f"\n  ── {cur_section} ──")
            print(f"  [{i}/{len(curriculum)}] {lecture['title'][:55]} …", end="  ", flush=True)
            found = scrape_lecture(page, base_url, lecture, args.delay)
            all_links.extend(found)
            print(f"{len(found)} link{'s' if len(found) != 1 else ''}")

        ctx.close()

    # ── Report ─────────────────────────────────────────────────────────────────
    print("\n[4/4] Writing report …")
    write_report(all_links, args.course_url, output_path)


if __name__ == "__main__":
    main()
