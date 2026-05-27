#!/usr/bin/env python3
"""
Udemy Business Course External Link Extractor
=============================================
Extracts every external URL and tool reference from a Udemy (Business) course:
  - Lecture descriptions / instructor notes
  - Supplementary resource links
  - Article lecture bodies
  - Practice activity descriptions

Results are organized in course order (section → lecture) and annotated with
the source location.  Both a human-readable report and a JSON file are written.

Usage
-----
    pip install -r requirements.txt
    playwright install chromium

    python udemy_extractor.py --course-url "https://learning.udemy.com/course/generative-ai-in-software-testing"

Options
-------
    --course-url    Udemy (Business) course URL          [required]
    --output        Path for the .txt report             [default: <slug>_links.txt]
    --headless      Run the browser without a window     [default: False]
    --delay         Seconds between API calls            [default: 0.8]
    --include-bare  Also extract raw URLs from plain text (not just <a> tags)
    --no-qa         Skip Q&A scraping (faster)

Notes
-----
  - A Chromium window opens so you can log into Udemy once.
    Your session is saved to udemy_browser_session/ for future runs.
  - Requires a valid Udemy (Business) enrolment for the course.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Constants ──────────────────────────────────────────────────────────────────

SESSION_DIR = Path("udemy_browser_session")
API_BASE_TEMPLATE = "https://{host}/api-2.0"

# These are treated as "internal" and excluded from results
INTERNAL_PATTERNS = [
    r"udemy\.com",
    r"udemycdn\.com",
    r"^#",
    r"^javascript:",
    r"^mailto:",
    r"^tel:",
    r"^data:",
]

# Domain → human-readable tool name (longest-match wins)
KNOWN_TOOLS: dict[str, str] = {
    # Version control / code hosting
    "github.com": "GitHub",
    "gist.github.com": "GitHub Gist",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
    # AI / ML platforms
    "openai.com": "OpenAI",
    "platform.openai.com": "OpenAI Platform",
    "anthropic.com": "Anthropic / Claude",
    "claude.ai": "Claude",
    "gemini.google.com": "Google Gemini",
    "aistudio.google.com": "Google AI Studio",
    "cohere.com": "Cohere",
    "huggingface.co": "Hugging Face",
    "replicate.com": "Replicate",
    "together.ai": "Together AI",
    "groq.com": "Groq",
    "mistral.ai": "Mistral AI",
    # Notebooks / compute
    "colab.research.google.com": "Google Colab",
    "kaggle.com": "Kaggle",
    "replit.com": "Replit",
    "deepnote.com": "Deepnote",
    "databricks.com": "Databricks",
    # Cloud providers
    "aws.amazon.com": "AWS",
    "console.aws.amazon.com": "AWS Console",
    "cloud.google.com": "Google Cloud",
    "portal.azure.com": "Azure",
    "azure.microsoft.com": "Azure",
    # Containers / DevOps
    "docker.com": "Docker",
    "hub.docker.com": "Docker Hub",
    "kubernetes.io": "Kubernetes",
    # Testing / QA tools
    "selenium.dev": "Selenium",
    "playwright.dev": "Playwright",
    "cypress.io": "Cypress",
    "jestjs.io": "Jest",
    "pytest.org": "pytest",
    "testng.org": "TestNG",
    "junit.org": "JUnit",
    "appium.io": "Appium",
    "katalon.com": "Katalon",
    "browserstack.com": "BrowserStack",
    "saucelabs.com": "Sauce Labs",
    "lambdatest.com": "LambdaTest",
    "postman.com": "Postman",
    "insomnia.rest": "Insomnia",
    # Project management / collaboration
    "notion.so": "Notion",
    "trello.com": "Trello",
    "jira.atlassian.com": "Jira",
    "atlassian.com": "Atlassian",
    "confluence.atlassian.com": "Confluence",
    "slack.com": "Slack",
    "discord.com": "Discord",
    "discord.gg": "Discord",
    "zoom.us": "Zoom",
    "miro.com": "Miro",
    "figma.com": "Figma",
    # Data / analytics
    "tableau.com": "Tableau",
    "powerbi.microsoft.com": "Power BI",
    "grafana.com": "Grafana",
    "snowflake.com": "Snowflake",
    "mongodb.com": "MongoDB",
    "postgresql.org": "PostgreSQL",
    "redis.io": "Redis",
    # Dev resources / docs
    "developer.mozilla.org": "MDN Web Docs",
    "docs.python.org": "Python Docs",
    "npmjs.com": "npm",
    "pypi.org": "PyPI",
    "w3schools.com": "W3Schools",
    "stackoverflow.com": "Stack Overflow",
    # Research / papers
    "arxiv.org": "arXiv",
    "paperswithcode.com": "Papers with Code",
    # Social / content
    "medium.com": "Medium",
    "dev.to": "Dev.to",
    "substack.com": "Substack",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
}


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Link:
    url: str
    text: str                   # anchor text or empty
    source_type: str            # "description" | "resource" | "article" | "practice"
    section_index: int
    section_title: str
    lecture_index: int          # object_index inside section
    lecture_title: str
    tool_name: Optional[str] = None

    def course_order_key(self) -> tuple:
        return (self.section_index, self.lecture_index)

    def location_label(self) -> str:
        return (
            f"Section {self.section_index}: {self.section_title}  ›  "
            f"Lecture {self.lecture_index}: {self.lecture_title}  [{self.source_type}]"
        )


# ── URL helpers ────────────────────────────────────────────────────────────────

def is_external(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    for pat in INTERNAL_PATTERNS:
        if re.search(pat, url, re.IGNORECASE):
            return False
    return True


def tool_name_for(url: str) -> Optional[str]:
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        # Longest-match: prefer subdomain-specific entries
        for domain in sorted(KNOWN_TOOLS, key=len, reverse=True):
            if host == domain or host.endswith("." + domain):
                return KNOWN_TOOLS[domain]
    except Exception:
        pass
    return None


def links_from_html(html: str) -> list[tuple[str, str]]:
    """Return (url, anchor_text) pairs from <a href> tags in HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if is_external(href):
            results.append((href, a.get_text(strip=True)[:200]))
    return results


_BARE_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}]+[^\s\"'<>()\[\]{}.,;:!?)]", re.I)


def bare_urls_from_text(text: str) -> list[str]:
    """Extract raw URLs embedded in plain text (not inside anchor tags)."""
    return [m.group() for m in _BARE_URL_RE.finditer(text) if is_external(m.group())]


# ── Browser: login + cookie extraction ────────────────────────────────────────

# Common Chrome/Chromium locations to try if Playwright's own build is missing
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def _find_chrome() -> Optional[str]:
    import os
    # Honour explicit env var first
    env = os.environ.get("CHROME_PATH") or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env and Path(env).exists():
        return env
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def get_cookies_via_browser(course_url: str, headless: bool) -> tuple[list[dict], Optional[int], Optional[str]]:
    """
    Open Chromium, let the user log in, then return:
      (cookies, course_id_or_None, course_title_or_None)
    Course ID is extracted from the page while the browser is open,
    which is more reliable than later API calls.
    """
    SESSION_DIR.mkdir(exist_ok=True)

    chrome_path = _find_chrome()
    launch_kwargs: dict = {
        "user_data_dir": str(SESSION_DIR),
        "headless": headless,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if chrome_path:
        print(f"  Using browser: {chrome_path}")
        launch_kwargs["executable_path"] = chrome_path

    course_id: Optional[int] = None
    course_title: Optional[str] = None

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"  Opening {course_url} …")
        try:
            page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            print("  ⚠ Page load timed out; continuing with whatever loaded.")
        time.sleep(3)

        # Detect login wall — Udemy Business may use SSO so always confirm
        needs_login = bool(
            page.query_selector('input[name="email"]')
            or page.query_selector('[data-purpose="header-login"]')
            or "login" in page.url.lower()
            or "sso" in page.url.lower()
        )
        if needs_login:
            print("\n  ⚠  Not logged in. Please log into Udemy Business in the browser window.")
            input("     Press ENTER once you are fully logged in and can see the course page … ")
            try:
                page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeout:
                pass
            time.sleep(3)

        # Extract course ID from page JavaScript while browser is open
        html = page.content()
        for pattern in [
            r'"courseId"\s*:\s*(\d+)',
            r'data-course-id=["\'](\d+)["\']',
            r'"id"\s*:\s*(\d+)\s*,\s*"[^"]*title',
            r'/learn/v4/(\d+)/',
        ]:
            m = re.search(pattern, html)
            if m:
                course_id = int(m.group(1))
                break

        # Also try navigating to the learn URL to get course ID from redirect
        if not course_id:
            slug_m = re.search(r"/course/([^/?#]+)", course_url)
            if slug_m:
                slug = slug_m.group(1)
                host = urlparse(course_url).netloc
                try:
                    page.goto(
                        f"https://{host}/course/{slug}/learn/",
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    time.sleep(2)
                    m = re.search(r"/learn/v4/(\d+)/", page.url)
                    if m:
                        course_id = int(m.group(1))
                    if not course_id:
                        m = re.search(r'"courseId"\s*:\s*(\d+)', page.content())
                        if m:
                            course_id = int(m.group(1))
                except Exception:
                    pass

        # Extract course title from page
        title_m = re.search(r'"title"\s*:\s*"([^"]{5,})"', html)
        if title_m:
            course_title = title_m.group(1)

        if course_id:
            print(f"  Course ID extracted from browser: {course_id}")
        else:
            print("  ⚠ Could not extract course ID from page — will try API fallbacks.")

        cookies = ctx.cookies()
        ctx.close()

    print(f"  Captured {len(cookies)} cookies.")
    return cookies, course_id, course_title


# ── Requests session from cookies ──────────────────────────────────────────────

def build_session(cookies: list[dict], host: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://{host}/",
    })
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    # CSRF token
    csrf = s.cookies.get("csrftoken", "")
    if csrf:
        s.headers["X-CSRFToken"] = csrf

    # Udemy Business uses a Bearer token — check common cookie names
    bearer = None
    for name in ("access_token", "ud-access-token", "ud_access_token", "bearer_token"):
        val = s.cookies.get(name, "")
        if val:
            bearer = val
            break
    if bearer:
        s.headers["Authorization"] = f"Bearer {bearer}"
        print(f"  Bearer token found in cookie '{name}'")
    else:
        print("  ⚠ No bearer token cookie found — API calls will use session cookies only.")

    return s


# ── Udemy API helpers ──────────────────────────────────────────────────────────

def resolve_course(
    session: requests.Session,
    course_url: str,
    host: str,
    browser_course_id: Optional[int] = None,
    browser_course_title: Optional[str] = None,
) -> tuple[int, str]:
    """Return (course_id, course_title)."""
    api_base = API_BASE_TEMPLATE.format(host=host)
    slug_m = re.search(r"/course/([^/?#]+)", course_url)
    if not slug_m:
        raise ValueError(f"Cannot extract slug from URL: {course_url}")
    slug = slug_m.group(1)

    # Strategy 0: use ID extracted directly from browser page (most reliable)
    if browser_course_id:
        # Verify it works with the API and get a proper title
        try:
            r = session.get(
                f"{api_base}/courses/{browser_course_id}/",
                params={"fields[course]": "id,title"},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                return data["id"], data.get("title", browser_course_title or slug)
        except Exception:
            pass
        return browser_course_id, browser_course_title or slug

    # Strategy 1: API search by slug
    r1 = session.get(
        f"{api_base}/courses/",
        params={"slug": slug, "fields[course]": "id,title"},
        timeout=15,
    )
    print(f"  Strategy 1 (slug search): HTTP {r1.status_code}")
    if r1.status_code == 200:
        results = r1.json().get("results", [])
        if results:
            return results[0]["id"], results[0]["title"]

    # Strategy 2: Scrape course landing page HTML for embedded JS data
    r2 = session.get(f"https://{host}/course/{slug}/", timeout=15)
    print(f"  Strategy 2 (landing page scrape): HTTP {r2.status_code}")
    if r2.status_code == 200:
        html = r2.text
        for pattern in [
            r'data-course-id=["\'](\d+)["\']',
            r'"courseId"\s*:\s*(\d+)',
            r'"id"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, html)
            if m:
                course_id = int(m.group(1))
                title_m = re.search(r'"title"\s*:\s*"([^"]{5,})"', html)
                title = title_m.group(1) if title_m else slug
                return course_id, title

    # Strategy 3: Follow redirect on learn URL
    r3 = session.get(
        f"https://{host}/course/{slug}/learn/", allow_redirects=True, timeout=15
    )
    print(f"  Strategy 3 (learn redirect): HTTP {r3.status_code}, final URL: {r3.url}")
    m = re.search(r"/learn/v4/(\d+)/", r3.url)
    if m:
        return int(m.group(1)), slug

    raise RuntimeError(
        f"Could not resolve course ID for '{slug}'.\n"
        f"  • Make sure you are enrolled in the course.\n"
        f"  • Try deleting udemy_browser_session/ and re-running so you can log in fresh.\n"
        f"  • HTTP status codes above indicate whether requests are reaching Udemy."
    )


def fetch_curriculum(session: requests.Session, course_id: int, host: str) -> list[dict]:
    """Return every curriculum item (chapters + lectures + practices) in order."""
    api_base = API_BASE_TEMPLATE.format(host=host)
    items: list[dict] = []
    page = 1
    while True:
        r = session.get(
            f"{api_base}/courses/{course_id}/cached-subscriber-curriculum-items/",
            params={
                "page_size": 200,
                "page": page,
                "fields[lecture]": "id,title,object_index,supplementary_assets,asset",
                "fields[chapter]": "id,title,object_index",
                "fields[practice]": "id,title,object_index",
                "fields[quiz]": "id,title,object_index",
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(f"  ⚠ Curriculum API: HTTP {r.status_code}")
            break
        data = r.json()
        items.extend(data.get("results", []))
        if not data.get("next"):
            break
        page += 1
    return items


def fetch_lecture_detail(
    session: requests.Session, course_id: int, lecture_id: int, host: str
) -> dict:
    api_base = API_BASE_TEMPLATE.format(host=host)
    r = session.get(
        f"{api_base}/users/me/subscribed-courses/{course_id}/lectures/{lecture_id}/",
        params={
            "fields[lecture]": "id,title,description,supplementary_assets,asset",
            "fields[asset]": "id,asset_type,title,body,external_url",
        },
        timeout=15,
    )
    return r.json() if r.status_code == 200 else {}


# ── Main extraction walk ───────────────────────────────────────────────────────

def extract_all_links(
    session: requests.Session,
    course_id: int,
    curriculum: list[dict],
    host: str,
    delay: float,
    include_bare: bool,
) -> list[Link]:
    all_links: list[Link] = []
    section = {"index": 0, "title": "Preamble"}
    lecture_items = [i for i in curriculum if i.get("_class") in ("lecture", "practice", "quiz")]
    total = len(lecture_items)

    for item in curriculum:
        cls = item.get("_class", "")

        # ── Chapter / section header ──────────────────────────────────────────
        if cls == "chapter":
            section = {
                "index": item.get("object_index", 0),
                "title": item.get("title", "Unnamed Section"),
            }
            print(f"\n  ┌── Section {section['index']}: {section['title']}")
            continue

        if cls not in ("lecture", "practice", "quiz"):
            continue

        lec_id = item.get("id")
        lec_title = item.get("title", "Untitled")
        lec_index = item.get("object_index", 0)
        src_type = "practice" if cls == "practice" else ("quiz" if cls == "quiz" else "lecture")

        processed = lecture_items.index(item) + 1
        print(f"  │  [{processed:>3}/{total}] {lec_title[:60]} …", end="  ", flush=True)

        def make(url: str, text: str, kind: str) -> Link:
            return Link(
                url=url, text=text, source_type=kind,
                section_index=section["index"], section_title=section["title"],
                lecture_index=lec_index, lecture_title=lec_title,
                tool_name=tool_name_for(url),
            )

        found = 0

        # ── Fetch detail from API ─────────────────────────────────────────────
        detail = fetch_lecture_detail(session, course_id, lec_id, host)

        # 1. Lecture description HTML
        desc_html = detail.get("description") or ""
        if desc_html:
            for url, text in links_from_html(desc_html):
                all_links.append(make(url, text, f"{src_type}:description"))
                found += 1
            if include_bare:
                plain = BeautifulSoup(desc_html, "html.parser").get_text(" ")
                seen_urls = {l.url for l in all_links[-20:]}
                for url in bare_urls_from_text(plain):
                    if url not in seen_urls:
                        all_links.append(make(url, "", f"{src_type}:description"))
                        seen_urls.add(url)
                        found += 1

        # 2. Supplementary assets (external link resources)
        for asset in detail.get("supplementary_assets") or []:
            atype = asset.get("asset_type", "")
            if atype == "ExternalLink":
                ext = (asset.get("external_url") or "").strip()
                if is_external(ext):
                    all_links.append(make(ext, asset.get("title", ""), f"{src_type}:resource"))
                    found += 1

        # 3. Main asset — Article body or External link
        asset = detail.get("asset") or {}
        atype = asset.get("asset_type", "")
        if atype == "Article":
            body = asset.get("body") or ""
            if body:
                for url, text in links_from_html(body):
                    all_links.append(make(url, text, f"{src_type}:article"))
                    found += 1
                if include_bare:
                    plain = BeautifulSoup(body, "html.parser").get_text(" ")
                    seen_urls = {l.url for l in all_links[-30:]}
                    for url in bare_urls_from_text(plain):
                        if url not in seen_urls:
                            all_links.append(make(url, "", f"{src_type}:article"))
                            seen_urls.add(url)
                            found += 1
        elif atype == "ExternalLink":
            ext = (asset.get("external_url") or "").strip()
            if is_external(ext):
                all_links.append(make(ext, asset.get("title", ""), f"{src_type}:resource"))
                found += 1

        print(f"{found} link{'s' if found != 1 else ''}")
        time.sleep(delay)

    return all_links


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(
    links: list[Link],
    course_title: str,
    course_url: str,
    output_path: Path,
) -> None:
    # Sort into course order
    ordered_links = sorted(links, key=lambda l: (l.section_index, l.lecture_index, l.url))

    # Deduplicate: keep first occurrence, collect all locations
    seen: dict[str, dict] = {}
    deduped: list[dict] = []

    for lnk in ordered_links:
        if lnk.url not in seen:
            entry = {
                "url": lnk.url,
                "text": lnk.text,
                "tool_name": lnk.tool_name,
                "section_index": lnk.section_index,
                "section_title": lnk.section_title,
                "lecture_index": lnk.lecture_index,
                "lecture_title": lnk.lecture_title,
                "source_type": lnk.source_type,
                "also_in": [],
            }
            seen[lnk.url] = entry
            deduped.append(entry)
        else:
            loc = lnk.location_label()
            if loc not in seen[lnk.url]["also_in"]:
                seen[lnk.url]["also_in"].append(loc)

    # ── Text report ─────────────────────────────────────────────────────────
    W = 80
    lines: list[str] = [
        "=" * W,
        "  UDEMY COURSE — EXTERNAL LINKS & TOOLS REPORT",
        f"  Course : {course_title}",
        f"  URL    : {course_url}",
        f"  Date   : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Links  : {len(deduped)} unique external references",
        "=" * W,
    ]

    current_section_key = None
    for idx, entry in enumerate(deduped, 1):
        sec_key = (entry["section_index"], entry["section_title"])
        if sec_key != current_section_key:
            current_section_key = sec_key
            lines.append("")
            lines.append("─" * W)
            lines.append(f"  SECTION {entry['section_index']}:  {entry['section_title']}")
            lines.append("─" * W)

        tag = f"  [{entry['tool_name']}]" if entry["tool_name"] else ""
        lines.append(f"\n  {idx:>3}.{tag}")
        lines.append(f"        URL      : {entry['url']}")
        if entry["text"]:
            lines.append(f"        Text     : {entry['text']}")
        lines.append(
            f"        Location : Lecture {entry['lecture_index']}: {entry['lecture_title']}"
            f"  [{entry['source_type']}]"
        )
        for loc in entry["also_in"]:
            lines.append(f"        Also in  : {loc}")

    # Tools summary
    tools_summary: dict[str, list[str]] = {}
    for entry in deduped:
        if entry["tool_name"]:
            tools_summary.setdefault(entry["tool_name"], []).append(entry["url"])

    lines += ["", "=" * W, "  TOOLS & PLATFORMS REFERENCED", "=" * W]
    if tools_summary:
        for tool, urls in sorted(tools_summary.items()):
            lines.append(f"  • {tool:<30} ({len(urls)} link{'s' if len(urls) > 1 else ''})")
    else:
        lines.append("  (No recognised tools detected)")
    lines.append("=" * W)

    output_path.write_text("\n".join(lines), encoding="utf-8")

    # ── JSON output ──────────────────────────────────────────────────────────
    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "course_title": course_title,
                "course_url": course_url,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "total_unique_links": len(deduped),
                "tools_referenced": {
                    t: len(u) for t, u in sorted(tools_summary.items())
                },
                "links": deduped,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"\n{'=' * 60}")
    print(f"  Done!  {len(deduped)} unique external links found.")
    print(f"  Report  → {output_path}")
    print(f"  JSON    → {json_path}")
    if tools_summary:
        print(f"\n  Tools referenced:")
        for tool in sorted(tools_summary):
            print(f"    • {tool}")
    print("=" * 60)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract external URLs and tools from a Udemy Business course.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--course-url", required=True, help="Udemy course URL")
    ap.add_argument("--output", default=None, help="Output .txt path (default: <slug>_links.txt)")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="Run browser without a visible window")
    ap.add_argument("--delay", type=float, default=0.8,
                    help="Seconds between API requests (default: 0.8)")
    ap.add_argument("--include-bare", action="store_true", default=False,
                    help="Also extract raw URLs from plain text (not just hyperlinks)")
    args = ap.parse_args()

    host = urlparse(args.course_url).netloc or "learning.udemy.com"

    # Derive output path from slug
    if args.output:
        output_path = Path(args.output)
    else:
        slug_m = re.search(r"/course/([^/?#]+)", args.course_url)
        slug = slug_m.group(1) if slug_m else "udemy_course"
        output_path = Path(f"{slug}_links.txt")

    # ── 1. Browser auth ───────────────────────────────────────────────────────
    print("\n[1/5] Browser authentication")
    cookies, browser_course_id, browser_course_title = get_cookies_via_browser(
        args.course_url, args.headless
    )

    # ── 2. Build API session ──────────────────────────────────────────────────
    print("\n[2/5] Building API session")
    session = build_session(cookies, host)

    # ── 3. Resolve course ─────────────────────────────────────────────────────
    print("\n[3/5] Resolving course")
    try:
        course_id, course_title = resolve_course(
            session, args.course_url, host, browser_course_id, browser_course_title
        )
    except Exception as e:
        print(f"  ✗ {e}")
        sys.exit(1)
    print(f"  ID    : {course_id}")
    print(f"  Title : {course_title}")

    # ── 4. Fetch curriculum ───────────────────────────────────────────────────
    print("\n[4/5] Fetching curriculum")
    curriculum = fetch_curriculum(session, course_id, host)
    n_sections = sum(1 for i in curriculum if i.get("_class") == "chapter")
    n_lectures = sum(1 for i in curriculum if i.get("_class") == "lecture")
    n_other = sum(1 for i in curriculum if i.get("_class") in ("practice", "quiz"))
    print(f"  {n_sections} sections, {n_lectures} lectures, {n_other} practices/quizzes")

    # ── 5. Extract links ──────────────────────────────────────────────────────
    print("\n[5/5] Extracting links\n")
    all_links = extract_all_links(
        session, course_id, curriculum, host, args.delay, args.include_bare
    )

    write_report(all_links, course_title, args.course_url, output_path)


if __name__ == "__main__":
    main()
