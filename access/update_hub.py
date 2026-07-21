from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
SOURCES_FILE = BASE_DIR / "sources.json"

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DISCOVERY_MODEL = os.environ.get("GEMINI_DISCOVERY_MODEL", "gemini-3.5-flash").strip()
CURATION_MODEL = os.environ.get("GEMINI_CURATOR_MODEL", "gemini-3.5-flash").strip()

MAX_FINAL_ITEMS = int(os.environ.get("MAX_FINAL_ITEMS", "100"))
MIN_GOOD_RUN_ITEMS = int(os.environ.get("MIN_GOOD_RUN_ITEMS", "1"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "14"))
MAX_SOURCE_WORKERS = int(os.environ.get("MAX_SOURCE_WORKERS", "8"))
MAX_VERIFY_WORKERS = int(os.environ.get("MAX_VERIFY_WORKERS", "14"))
MAX_VERIFY_CANDIDATES = int(os.environ.get("MAX_VERIFY_CANDIDATES", "260"))
MAX_DIRECT_SOURCES = int(os.environ.get("MAX_DIRECT_SOURCES", "24"))
MAX_LISTING_PAGES_PER_SOURCE = int(os.environ.get("MAX_LISTING_PAGES_PER_SOURCE", "3"))
CURRENT_YEAR = datetime.now(timezone.utc).year
TODAY = datetime.now(timezone.utc).date()
NOW_ISO = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

CATEGORY_ORDER = ("Event",)
CATEGORY_TARGET_WEIGHTS = {"Event": 1.0}

ALLOWED_EVENT_SUBCATEGORIES = {
    "Accelerator", "Incubator", "Venture Studio", "Founder Residency",
    "Fellowship", "Hackathon", "Competition / Challenge", "Pitch Competition",
    "Demo Day", "Startup Conference", "Founder Event", "Entrepreneurship Event",
    "Startup Program", "Founder Program", "Bootcamp",
}

# Only opportunity-specific feeds belong here. Generic startup-news feeds are deliberately absent.
RSS_FEEDS: list[tuple[str, str, int]] = []

DISCOVERY_JOBS = {
    "Event": (
        "Find only currently open startup accelerators, incubators, founder programs, founder residencies, "
        "entrepreneurship fellowships, startup hackathons, startup competitions, pitch competitions, demo days, "
        "startup conferences, founder summits and other entrepreneurial events worldwide."
    ),
}

CATEGORY_TERMS = {
    "Event": (
        "accelerator", "incubator", "venture studio", "founder residency", "startup residency",
        "founder fellowship", "startup fellowship", "hackathon", "startup competition",
        "innovation challenge", "pitch competition", "startup pitch", "demo day",
        "startup conference", "founder conference", "entrepreneurship conference",
        "startup summit", "founder summit", "entrepreneurship summit", "founder event",
        "startup event", "entrepreneurship event", "startup bootcamp", "founder bootcamp",
        "startup program", "founder program", "accelerator program", "incubator program",
    ),
}

STRONG_ACTION_PHRASES = (
    "apply now", "apply today", "apply here", "start application", "submit application",
    "applications open", "applications are open", "now accepting applications", "open for applications",
    "register now", "registration open", "submit your project", "enter now", "join the cohort",
    "join now", "enroll now", "open enrollment", "book a mentor", "book a call", "claim credits",
    "claim your credits", "get started", "sign up", "download now", "access the course",
)

OPEN_PHRASES = (
    "rolling applications", "rolling intake", "applications are open", "apply now",
    "now accepting", "open enrollment", "currently available", "registration open",
    "open call", "accepting applications", "enrolling now", "available now",
)

CLOSED_PHRASES = (
    "applications closed", "application closed", "closed for applications", "deadline passed",
    "expired", "no longer accepting", "submissions closed", "registration closed",
    "call closed", "applications are now closed", "not accepting applications",
)

# News and corporate-announcement language. These patterns are checked before Gemini and again
# after Gemini, so a model cannot force a news article into deterministic backfill.
NEWS_TITLE_PATTERNS = (
    r"\braises?\s+[€$£]?\d", r"\braised\s+[€$£]?\d", r"\bsecures?\s+[€$£]?\d",
    r"\bfunding round\b", r"\bseries\s+[a-z]\b", r"\bseed round\b",
    r"\bvalued at\b", r"\bvaluation\b", r"\bacquires?\b", r"\bacquired by\b",
    r"\bacquisition\b", r"\bmerger\b", r"\bexits? to\b", r"\bexit to\b",
    r"\blaunches?\b", r"\bunveils?\b", r"\bannounces? partnership\b",
    r"\bcommits?\s+(?:at least\s+)?[€$£]?\d", r"\binvests?\s+[€$£]?\d",
    r"\bopens?\s+(?:a\s+)?new office\b", r"\bexpands?\s+to\b",
    r"\bappoints?\b", r"\bhires?\b", r"\brevenue reaches\b", r"\bprofits?\b",
)
NEWS_BODY_PATTERNS = (
    "funding round", "has raised", "announced today that", "acquired by", "acquisition of",
    "exit to", "company valuation", "venture capital round", "press release", "startup news",
    "opens new office", "commits at least", "investment announcement", "expands into",
)
NEWS_SCHEMA_TYPES = {"article", "newsarticle", "reportagenewsarticle", "blogposting"}

BLOCKED_DOMAINS = {
    "producthunt.com", "www.producthunt.com", "techcrunch.com",
    "news.ycombinator.com",
}
NEWS_HEAVY_DOMAINS = {
    "eu-startups.com", "www.eu-startups.com",
}
BLOCKED_URL_PARTS = (
    "/news/", "/blog/", "/press/", "/press-release", "/stories/", "/story/",
    "/jobs/", "/careers/", "/company/", "/people/", "/portfolio/", "/about/",
    "/privacy", "/terms", "/login", "/sign-in", "/signin", "/contact",
)
BLOCKED_PAGE_PHRASES = (
    "access denied", "verify you are human", "captcha", "enable javascript to continue",
    "temporarily unavailable", "page not found", "404 not found", "log in to continue",
    "sign in to continue",
)
LISTING_NAV_TERMS = (
    "programs", "accelerators", "incubators", "challenges", "hackathons", "competitions",
    "events", "grants", "funding", "opportunities", "open calls", "mentors", "courses",
    "perks", "resources", "next", "older", "page 2",
)

DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y",
    "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y", "%d %B %Y",
)

_thread_local = threading.local()
_snapshot_cache: dict[str, dict[str, Any] | None] = {}
_snapshot_lock = threading.Lock()


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "AccessByEntreprenote/4.0 (+https://entreprenote.com/access/)",
            "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        _thread_local.session = session
    return session


def clean_text(value: object, limit: int = 1200) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def clean_url(value: object) -> str:
    try:
        raw = str(value or "").strip()
        markdown = re.fullmatch(r"\[(https?://[^\]]+)\]\((https?://[^)]+)\)", raw)
        if markdown:
            raw = markdown.group(1)
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        if parsed.netloc.lower() in {"google.com", "www.google.com"} and parsed.path == "/search":
            target = dict(parse_qsl(parsed.query)).get("q", "")
            return clean_url(target) if target.startswith(("http://", "https://")) else ""
        query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid"}
        ]
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        path = path.rstrip("/") or "/"
        return urlunparse((
            parsed.scheme.lower(), parsed.netloc.lower(), path, "",
            urlencode(query, doseq=True), "",
        ))
    except Exception:
        return ""


def allowed_url(url: str) -> bool:
    parsed = urlparse(clean_url(url))
    host = parsed.netloc.lower()
    return bool(host) and host not in BLOCKED_DOMAINS


def normalize_title_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value, 240).lower()).strip()


def parse_date_value(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = clean_text(value, 120)
    if not raw:
        return None
    raw = re.sub(
        r"\b(?:deadline|closing|close date|opening|open date|published|apply by|ends?|starts?)\s*:\s*",
        "", raw, flags=re.I,
    ).strip(" .")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    match = re.search(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},\s+\d{4}\b",
        raw, flags=re.I,
    )
    if match:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(match.group(0), fmt).date()
            except ValueError:
                continue
    return None


def extract_labeled_date(text: str, labels: tuple[str, ...]) -> date | None:
    if not text:
        return None
    label_pattern = "|".join(re.escape(label) for label in labels)
    date_pattern = (
        r"\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
        r"\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*-\d{4}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}"
    )
    match = re.search(rf"(?:{label_pattern})\s*:?\s*({date_pattern})", text, flags=re.I)
    return parse_date_value(match.group(1)) if match else None


def candidate_dates(item: dict) -> tuple[date | None, date | None, date | None]:
    text = clean_text(
        item.get("page_text") or item.get("summary") or item.get("description"),
        8000,
    )
    deadline = (
        parse_date_value(item.get("deadline"))
        or parse_date_value(item.get("close_date"))
        or extract_labeled_date(text, ("deadline", "closing", "close date", "apply by", "applications close", "ends"))
    )
    opening = (
        parse_date_value(item.get("opening_date"))
        or parse_date_value(item.get("open_date"))
        or extract_labeled_date(text, ("opening", "open date", "opens", "applications open", "starts"))
    )
    published = (
        parse_date_value(item.get("published_at"))
        or parse_date_value(item.get("updated_at"))
        or parse_date_value(item.get("first_seen"))
    )
    return opening, deadline, published


def iso_or_empty(value: date | None) -> str:
    return value.isoformat() if value else ""


def load_sources() -> list[dict]:
    if not SOURCES_FILE.exists():
        raise RuntimeError(f"Missing source catalog: {SOURCES_FILE}")
    payload = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("sources.json must contain an array")
    output: list[dict] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        url = clean_url(str(raw.get("url", "")).replace("{year}", str(CURRENT_YEAR)))
        if not allowed_url(url) or url in seen:
            continue
        categories = [c for c in raw.get("categories", []) if c in CATEGORY_ORDER]
        if not categories:
            continue
        item = dict(raw)
        item["url"] = url
        item["categories"] = categories
        item["mode"] = "direct" if item.get("mode") == "direct" else "search"
        item["confidence"] = item.get("confidence") if item.get("confidence") in {"high", "medium", "low"} else "medium"
        item["max_links"] = max(2, min(int(item.get("max_links", 6)), 15))
        output.append(item)
        seen.add(url)
    return output


SOURCES = load_sources()
SOURCE_ROOTS = {clean_url(source["url"]): source for source in SOURCES}
SOURCE_HOSTS: dict[str, list[dict]] = {}
for _source in SOURCES:
    SOURCE_HOSTS.setdefault(urlparse(_source["url"]).netloc.lower(), []).append(_source)


def source_for_url(url: str) -> dict | None:
    host = urlparse(clean_url(url)).netloc.lower()
    candidates = SOURCE_HOSTS.get(host, [])
    if not candidates:
        return None
    path = urlparse(clean_url(url)).path.rstrip("/")
    return max(candidates, key=lambda source: len(urlparse(source["url"]).path.rstrip("/")) if path.startswith(urlparse(source["url"]).path.rstrip("/")) else 0)


def source_confidence_for_url(url: str) -> str:
    source = source_for_url(url)
    if source:
        return source.get("confidence", "medium")
    host = urlparse(clean_url(url)).netloc.lower()
    if host.endswith(".gov") or ".gov." in host or host.endswith(".europa.eu"):
        return "high"
    return "medium"


def is_source_root(url: str) -> bool:
    return clean_url(url) in SOURCE_ROOTS


def fallback_category(item: dict) -> str:
    suggested = clean_text(item.get("suggested_category") or item.get("category"), 30).title()
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} "
        f"{item.get('primary_text', '')}",
        4000,
    ).lower()
    if suggested in {"Event", "Events"} and any(term in text for term in CATEGORY_TERMS["Event"]):
        return "Event"
    if any(term in text for term in CATEGORY_TERMS["Event"]):
        return "Event"
    return "Reject"


def fallback_subcategory(item: dict, category: str) -> str:
    suggested = clean_text(item.get("suggested_subcategory") or item.get("subcategory"), 50)
    normalized = {
        "Competition": "Competition / Challenge", "Challenge": "Competition / Challenge",
        "Conference": "Startup Conference", "Summit": "Startup Conference",
        "Program": "Startup Program", "Residency": "Founder Residency",
    }.get(suggested, suggested)
    if normalized in ALLOWED_EVENT_SUBCATEGORIES:
        return normalized
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} "
        f"{item.get('primary_text', '')}",
        4000,
    ).lower()
    rules = [
        ("accelerator", "Accelerator"), ("incubator", "Incubator"),
        ("venture studio", "Venture Studio"), ("residency", "Founder Residency"),
        ("fellowship", "Fellowship"), ("hackathon", "Hackathon"),
        ("pitch competition", "Pitch Competition"), ("demo day", "Demo Day"),
        ("competition", "Competition / Challenge"), ("challenge", "Competition / Challenge"),
        ("startup conference", "Startup Conference"), ("founder conference", "Startup Conference"),
        ("entrepreneurship conference", "Startup Conference"), ("summit", "Startup Conference"),
        ("founder event", "Founder Event"), ("entrepreneurship event", "Entrepreneurship Event"),
        ("startup event", "Founder Event"), ("bootcamp", "Bootcamp"),
        ("founder program", "Founder Program"), ("startup program", "Startup Program"),
    ]
    for keyword, label in rules:
        if keyword in text:
            return label
    return "Startup Program"


def contains_category_evidence(text: str, category: str) -> bool:
    if category != "Event":
        return False
    lower = text.lower()
    return any(term in lower for term in CATEGORY_TERMS["Event"])


def extract_action_evidence(text: str) -> str:
    lower = text.lower()
    for phrase in STRONG_ACTION_PHRASES:
        if phrase in lower:
            return phrase
    standalone = re.search(r"\b(apply|register|submit|enroll|join|book|claim|download|start)\b", lower)
    return standalone.group(1) if standalone else ""


def is_news_like(title: str, text: str, url: str, schema_types: set[str] | None = None) -> bool:
    title_lower = clean_text(title, 260).lower()
    body_lower = clean_text(text, 5000).lower()
    parsed = urlparse(clean_url(url))
    path = parsed.path.lower()
    host = parsed.netloc.lower()

    if schema_types and any(schema_type.lower() in NEWS_SCHEMA_TYPES for schema_type in schema_types):
        return True
    if any(re.search(pattern, title_lower, flags=re.I) for pattern in NEWS_TITLE_PATTERNS):
        return True
    if any(pattern in body_lower for pattern in NEWS_BODY_PATTERNS) and not extract_action_evidence(body_lower):
        return True
    if any(part in path for part in BLOCKED_URL_PARTS):
        return True
    if host in NEWS_HEAVY_DOMAINS and re.search(r"/20\d{2}/\d{2}/", path):
        return True
    return False


def is_current_or_open(item: dict) -> bool:
    category = fallback_category(item)
    if category != "Event":
        return False
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} "
        f"{item.get('page_text', '')} {item.get('action_evidence', '')} "
        f"{item.get('deadline_evidence', '')} {item.get('status', '')}",
        9000,
    ).lower()
    status = clean_text(item.get("status"), 80).lower()
    if any(phrase in text for phrase in CLOSED_PHRASES):
        return False
    opening, deadline, published = candidate_dates(item)
    if deadline:
        return deadline >= TODAY
    if status == "forecasted":
        return bool(opening and TODAY <= opening <= TODAY + timedelta(days=365))
    if status in {"open", "active", "available", "rolling", "enrolling", "posted"}:
        if category in {"Event", "Funding"}:
            return bool(extract_action_evidence(text) or status == "posted")
        return True
    if any(phrase in text for phrase in OPEN_PHRASES):
        return True
    # A publication date alone never proves an Event/Funding article is actionable.
    if category in {"Event", "Funding"}:
        return False
    return bool(published and published.year >= CURRENT_YEAR and extract_action_evidence(text))


def basic_publishable(item: dict) -> bool:
    url = clean_url(item.get("link") or item.get("url"))
    if not allowed_url(url):
        return False
    category = fallback_category(item)
    if category != "Event":
        return False
    full_text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} "
        f"{item.get('page_text', '')} {item.get('action_evidence', '')} {item.get('deadline_evidence', '')}",
        9000,
    )
    primary_text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} "
        f"{item.get('primary_text', '')}",
        5000,
    )
    if is_news_like(item.get("title", ""), full_text, url, set(item.get("schema_types", []))):
        return False
    if not contains_category_evidence(primary_text, "Event"):
        return False
    subcategory = fallback_subcategory(item, "Event")
    if subcategory not in ALLOWED_EVENT_SUBCATEGORIES:
        return False
    if is_source_root(url):
        source = SOURCE_ROOTS[url]
        if source.get("source_type") == "directory":
            return False
    return is_current_or_open({**item, "category": "Event", "subcategory": subcategory})


def fetch_snapshot(url: str) -> dict[str, Any] | None:
    url = clean_url(url)
    if not allowed_url(url):
        return None
    with _snapshot_lock:
        if url in _snapshot_cache:
            return _snapshot_cache[url]
    snapshot: dict[str, Any] | None = None
    try:
        response = get_session().get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        final_url = clean_url(response.url)
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and "xml" not in content_type and "json" not in content_type:
            raise ValueError(f"Unsupported content type: {content_type}")
        content = response.content[:2_500_000]
        if "json" in content_type:
            text = clean_text(response.text, 20000)
            snapshot = {
                "url": final_url, "title": "", "description": "", "text": text,
                "links": [], "schema_types": set(), "status_code": response.status_code,
            }
        else:
            soup = BeautifulSoup(content, "html.parser")
            schema_types: set[str] = set()
            for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    payload = json.loads(script.string or script.get_text() or "{}")
                except Exception:
                    continue
                stack = payload if isinstance(payload, list) else [payload]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        kind = node.get("@type")
                        if isinstance(kind, str):
                            schema_types.add(kind.lower())
                        elif isinstance(kind, list):
                            schema_types.update(str(value).lower() for value in kind)
                        stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
                    elif isinstance(node, list):
                        stack.extend(node)
            title = clean_text(soup.title.get_text(" ") if soup.title else "", 240)
            h1 = clean_text(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "", 300)
            description = ""
            meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
            if meta:
                description = clean_text(meta.get("content"), 500)
            canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
            if canonical and canonical.get("href"):
                candidate_canonical = clean_url(urljoin(final_url, canonical.get("href")))
                if allowed_url(candidate_canonical):
                    final_url = candidate_canonical
            for node in soup(["script", "style", "noscript", "svg", "canvas"]):
                node.decompose()
            main = soup.find("main") or soup.find("article") or soup.body or soup
            text = clean_text(main.get_text(" ", strip=True), 24000)
            links: list[tuple[str, str]] = []
            for anchor in soup.find_all("a", href=True):
                href = clean_url(urljoin(final_url, anchor.get("href")))
                label = clean_text(anchor.get_text(" ", strip=True) or anchor.get("aria-label") or anchor.get("title"), 180)
                if allowed_url(href):
                    links.append((href, label))
            snapshot = {
                "url": final_url, "title": title, "description": description, "text": text,
                "primary_text": clean_text(f"{title} {h1} {description} {text[:1800]}", 3200),
                "links": links, "schema_types": schema_types, "status_code": response.status_code,
            }
    except Exception as exc:
        print(f"[DEBUG] Fetch failed {url}: {exc}")
        snapshot = None
    with _snapshot_lock:
        _snapshot_cache[url] = snapshot
    return snapshot


def link_score(source: dict, href: str, label: str) -> int:
    href = clean_url(href)
    if not allowed_url(href) or href == clean_url(source["url"]):
        return -999
    parsed = urlparse(href)
    path = parsed.path.lower()
    label_lower = label.lower()
    text = f"{label_lower} {path.replace('-', ' ').replace('_', ' ')}"
    if any(part in path for part in BLOCKED_URL_PARTS):
        return -999
    if is_news_like(label, "", href):
        return -999
    score = 0
    for category in source["categories"]:
        score += 3 * sum(1 for term in CATEGORY_TERMS[category] if term in text)
    score += 5 * sum(1 for phrase in STRONG_ACTION_PHRASES if phrase in text)
    if any(term in text for term in ("apply", "application", "register", "open", "deadline", "cohort")):
        score += 4
    if parsed.netloc.lower() != urlparse(source["url"]).netloc.lower():
        score += 2 if any(term in text for term in ("apply", "register", "application", "challenge", "program")) else -4
    if len(label_lower) < 3:
        score -= 2
    return score


def listing_navigation_links(source: dict, snapshot: dict[str, Any]) -> list[str]:
    root_host = urlparse(source["url"]).netloc.lower()
    output: list[tuple[int, str]] = []
    for href, label in snapshot.get("links", []):
        parsed = urlparse(href)
        if parsed.netloc.lower() != root_host:
            continue
        lower = f"{label} {parsed.path}".lower()
        score = sum(1 for term in LISTING_NAV_TERMS if term in lower)
        if score and href != source["url"] and not any(part in parsed.path.lower() for part in BLOCKED_URL_PARTS):
            output.append((score, href))
    output.sort(key=lambda pair: (-pair[0], pair[1]))
    seen: set[str] = set()
    return [href for _, href in output if not (href in seen or seen.add(href))][: MAX_LISTING_PAGES_PER_SOURCE - 1]


def category_for_link(source: dict, label: str, href: str) -> str:
    text = f"{label} {urlparse(href).path}".lower()
    for category in source["categories"]:
        if contains_category_evidence(text, category):
            return category
    return source["categories"][0]


def crawl_source(source: dict) -> list[dict]:
    candidates: list[dict] = []
    root = fetch_snapshot(source["url"])
    if not root:
        return candidates
    pages = [root]
    for nav_url in listing_navigation_links(source, root):
        nav_snapshot = fetch_snapshot(nav_url)
        if nav_snapshot:
            pages.append(nav_snapshot)
    scored: list[tuple[int, str, str]] = []
    for page in pages:
        for href, label in page.get("links", []):
            score = link_score(source, href, label)
            if score > 0:
                scored.append((score, href, label))
    scored.sort(key=lambda row: (-row[0], row[1]))
    seen: set[str] = set()
    for _, href, label in scored:
        if href in seen:
            continue
        seen.add(href)
        category = category_for_link(source, label, href)
        candidates.append({
            "title": label or href.rstrip("/").split("/")[-1].replace("-", " ").title(),
            "summary": f"Discovered from the {source['name']} opportunity directory.",
            "link": href,
            "source": source["name"],
            "method": "direct-crawl",
            "status": "",
            "opening_date": "",
            "deadline": "",
            "suggested_category": category,
            "suggested_subcategory": "",
            "source_confidence": source.get("confidence", "medium"),
        })
        if len(candidates) >= source.get("max_links", 8):
            break

    # A single official program hub can itself be an actionable listing. Directory roots cannot.
    if source.get("source_type") == "official program hub":
        root_text = f"{root.get('title', '')} {root.get('description', '')} {root.get('primary_text', '')}"
        category = source["categories"][0]
        if extract_action_evidence(root_text) and contains_category_evidence(root_text, category):
            candidates.append({
                "title": root.get("title") or source["name"],
                "summary": root.get("description") or clean_text(root.get("text"), 900),
                "link": root.get("url") or source["url"],
                "source": source["name"],
                "method": "direct-crawl",
                "status": "open",
                "opening_date": "",
                "deadline": "",
                "suggested_category": category,
                "suggested_subcategory": "",
                "source_confidence": source.get("confidence", "medium"),
            })
    return candidates


def crawl_direct_sources() -> list[dict]:
    sources = [
        source for source in SOURCES
        if not source.get("requires_login")
        and (
            source.get("mode") == "direct"
            or (
                source.get("confidence") == "high"
                and source.get("direct_application_links")
                and (source.get("has_open_filter") or source.get("source_type") == "official program hub")
            )
        )
    ]
    sources.sort(key=lambda source: (source.get("confidence") != "high", source["name"].lower()))
    sources = sources[:MAX_DIRECT_SOURCES]
    output: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_SOURCE_WORKERS) as executor:
        futures = {executor.submit(crawl_source, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                rows = future.result()
                output.extend(rows)
                print(f"[OK] Direct crawl {source['name']}: {len(rows)} candidate links")
            except Exception as exc:
                print(f"[WARN] Direct crawl {source['name']} failed: {exc}")
    return output


def rss_published_date(entry: object) -> date | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            try:
                return date(value.tm_year, value.tm_mon, value.tm_mday)
            except Exception:
                pass
    for key in ("published", "updated", "created"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            try:
                return parsedate_to_datetime(str(value)).date()
            except Exception:
                parsed = parse_date_value(value)
                if parsed:
                    return parsed
    return None


def fetch_rss() -> list[dict]:
    output: list[dict] = []
    for source, url, limit in RSS_FEEDS:
        try:
            response = get_session().get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            accepted = 0
            for entry in feed.entries[:limit]:
                title = clean_text(entry.get("title"), 180)
                summary = clean_text(entry.get("summary") or entry.get("description"), 1200)
                link = clean_url(entry.get("link"))
                published = rss_published_date(entry)
                item = {
                    "title": title, "summary": summary, "link": link, "source": source,
                    "method": "rss", "published_at": iso_or_empty(published), "status": "",
                    "opening_date": "", "deadline": "", "suggested_category": "",
                    "suggested_subcategory": "", "source_confidence": "medium",
                }
                if is_news_like(title, summary, link):
                    continue
                action = extract_action_evidence(f"{title} {summary}")
                if action:
                    item["status"] = "open"
                if not basic_publishable(item):
                    continue
                output.append(item)
                accepted += 1
            print(f"[OK] RSS {source}: {accepted} strict candidates")
        except Exception as exc:
            print(f"[WARN] RSS {source} failed: {exc}")
    return output


def fetch_grants_gov() -> list[dict]:
    endpoint = "https://api.grants.gov/v1/api/search2"
    output: list[dict] = []
    for keyword in ("small business", "entrepreneurship", "innovation", "startup"):
        try:
            response = get_session().post(
                endpoint,
                json={"keyword": keyword, "oppStatuses": "posted|forecasted", "rows": 20},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            hits = (((response.json().get("data") or {}).get("oppHits")) or [])
            for hit in hits:
                opportunity_id = str(hit.get("id") or "").strip()
                title = clean_text(hit.get("title"), 180)
                if not opportunity_id or not title:
                    continue
                opening = parse_date_value(hit.get("openDate"))
                deadline = parse_date_value(hit.get("closeDate"))
                status = clean_text(hit.get("oppStatus"), 40).lower()
                item = {
                    "title": title,
                    "summary": clean_text(
                        f"Agency: {hit.get('agencyName', '')}. Opening: {hit.get('openDate', '')}. "
                        f"Closing: {hit.get('closeDate', '')}. Status: {status}.",
                        900,
                    ),
                    "link": f"https://www.grants.gov/search-results-detail/{opportunity_id}",
                    "source": "Grants.gov", "method": "api", "opening_date": iso_or_empty(opening),
                    "deadline": iso_or_empty(deadline), "status": status,
                    "suggested_category": "Funding", "suggested_subcategory": "Grant",
                    "source_confidence": "high",
                }
                if basic_publishable(item):
                    output.append(item)
        except Exception as exc:
            print(f"[WARN] Grants.gov '{keyword}' failed: {exc}")
    print(f"[OK] Grants.gov: {len(output)} strict candidates")
    return output


def find_first_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for value in payload.values():
        found = find_first_list(value)
        if found:
            return found
    return []


def fetch_sbir() -> list[dict]:
    endpoint = "https://api.www.sbir.gov/public/api/solicitations?open=1&rows=60"
    output: list[dict] = []
    try:
        response = get_session().get(endpoint, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        for row in find_first_list(response.json()):
            title = clean_text(row.get("solicitation_title") or row.get("title"), 180)
            link = clean_url(row.get("solicitation_agency_url") or row.get("url"))
            opening = parse_date_value(row.get("open_date") or row.get("release_date"))
            deadline = parse_date_value(row.get("close_date"))
            if not deadline:
                values = row.get("application_due_date") or []
                if not isinstance(values, list):
                    values = [values]
                future = [parsed for parsed in (parse_date_value(value) for value in values) if parsed and parsed >= TODAY]
                deadline = min(future) if future else None
            item = {
                "title": title,
                "summary": clean_text(
                    f"{row.get('agency', '')} {row.get('program', '')} {row.get('phase', '')} "
                    f"{row.get('solicitation_number', '')}",
                    900,
                ),
                "link": link, "source": "SBIR / STTR", "method": "api",
                "opening_date": iso_or_empty(opening), "deadline": iso_or_empty(deadline),
                "status": clean_text(row.get("current_status"), 40).lower() or "open",
                "suggested_category": "Funding", "suggested_subcategory": "SBIR / STTR",
                "source_confidence": "high",
            }
            if basic_publishable(item):
                output.append(item)
        print(f"[OK] SBIR / STTR: {len(output)} strict candidates")
    except Exception as exc:
        print(f"[WARN] SBIR / STTR API unavailable: {exc}")
    return output


def extract_json_array(text: str) -> list[dict]:
    value = (text or "").replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass
    start, end = value.find("["), value.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(value[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pass
    return []


def source_batches(category: str, batch_size: int = 15) -> list[list[dict]]:
    sources = [source for source in SOURCES if category in source["categories"]]
    sources.sort(key=lambda source: (source.get("confidence") != "high", source["name"].lower()))
    return [sources[index:index + batch_size] for index in range(0, len(sources), batch_size)]


def generate_grounded_json(client: genai.Client, prompt: str) -> list[dict]:
    search_tool = types.Tool(google_search=types.GoogleSearch())
    try:
        response = client.models.generate_content(
            model=DISCOVERY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[search_tool], response_mime_type="application/json",
                temperature=0.1, max_output_tokens=16000,
            ),
        )
    except Exception as first_error:
        print(f"[DEBUG] Structured grounded call failed, retrying plain grounded JSON: {first_error}")
        response = client.models.generate_content(
            model=DISCOVERY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(tools=[search_tool], temperature=0.1),
        )
    return extract_json_array(response.text or "")


def discover_with_google_search(client: genai.Client) -> list[dict]:
    output: list[dict] = []
    for category, request_text in DISCOVERY_JOBS.items():
        for batch_number, batch in enumerate(source_batches(category), start=1):
            sources_text = "\n".join(
                f"- {source['name']}: {source['url']} ({source.get('source_type', 'directory')})"
                for source in batch
            )
            prompt = f"""
Today is {TODAY.isoformat()}.

{request_text}

Use these pages as SOURCE DIRECTORIES. Open them, inspect their category/list pages, and follow their
program links to the exact application, registration, booking, claim, enrollment or resource page:
{sources_text}

A result is valid only when the answer to this test is YES:
"Can a founder open the returned URL and take the promised action now?"

NON-NEGOTIABLE RULES:
1. Return the exact actionable subpage, not a directory homepage and not a generic article.
2. Reject all startup news, fundraising news, acquisitions, exits, product launches, partnerships,
   opinion pieces, company profiles, press releases and funding-round announcements.
3. Example that MUST be rejected: an article titled "Prior Labs raises €1 billion and exits to SAP".
   It contains funding words but provides no grant, accelerator, competition or application.
4. Every result must be an accelerator, incubator, founder/startup program, hackathon, startup competition,
   pitch competition, demo day, startup conference, founder summit or entrepreneurship event and require at least one of:
   - a deadline on or after today; or
   - explicit text saying applications/registration are open or rolling; or
   - a visible Apply/Register/Submit action for a currently active program.
5. Reject grants-only pages, investor news, funding announcements, tools, courses, mentor directories and generic resources.
6. Reject corporate announcements such as investments, office openings, partnerships or expansion news even if they mention startups.
7. Never infer openness from the article publication year. Never invent a URL, deadline or status.
8. Prefer official organizer pages. Hosted application pages such as Devpost, F6S, Agorize or official
   government portals are acceptable.

Return ONLY a JSON array of 4 to 12 objects with exactly:
- title
- summary
- link
- source
- opening_date: YYYY-MM-DD or empty string
- deadline: YYYY-MM-DD or empty string
- status: open, rolling, active, available, enrolling, posted, forecasted, or empty string
- suggested_category: exactly {category}
- suggested_subcategory: concise type
- page_kind: application, registration, program, resource, booking, course, tool, or claim
- action_evidence: short exact phrase seen on the page, such as "Apply now"
- deadline_evidence: short exact deadline phrase or empty string
- is_news: false

Return an empty array rather than a doubtful result. Do not use markdown.
"""
            try:
                found = generate_grounded_json(client, prompt)
                accepted = 0
                for raw in found:
                    if raw.get("is_news") is not False:
                        continue
                    action_evidence = clean_text(raw.get("action_evidence"), 160)
                    page_kind = clean_text(raw.get("page_kind"), 40).lower()
                    if page_kind not in {"application", "registration", "program", "resource", "booking", "course", "tool", "claim"}:
                        continue
                    item = {
                        "title": clean_text(raw.get("title"), 180),
                        "summary": clean_text(raw.get("summary"), 1200),
                        "link": clean_url(raw.get("link")),
                        "source": clean_text(raw.get("source"), 120),
                        "method": "gemini-search",
                        "opening_date": iso_or_empty(parse_date_value(raw.get("opening_date"))),
                        "deadline": iso_or_empty(parse_date_value(raw.get("deadline"))),
                        "status": clean_text(raw.get("status"), 40).lower(),
                        "suggested_category": category,
                        "suggested_subcategory": clean_text(raw.get("suggested_subcategory"), 50),
                        "action_evidence": action_evidence,
                        "deadline_evidence": clean_text(raw.get("deadline_evidence"), 160),
                        "page_kind": page_kind,
                        "source_confidence": source_confidence_for_url(raw.get("link", "")),
                    }
                    if not item["title"] or not action_evidence or not basic_publishable(item):
                        continue
                    output.append(item)
                    accepted += 1
                print(f"[OK] Gemini Search {category} batch {batch_number}: {accepted} strict candidates")
            except Exception as exc:
                print(f"[WARN] Gemini Search {category} batch {batch_number} failed: {exc}")
    return output


def deduplicate(records: list[dict]) -> list[dict]:
    output: list[dict] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    for raw in records:
        item = dict(raw)
        link = clean_url(item.get("link") or item.get("url"))
        title = clean_text(item.get("title"), 180)
        title_key = normalize_title_key(title)
        if not allowed_url(link) or not title_key:
            continue
        if link in seen_links or title_key in seen_titles:
            continue
        item["link"] = link
        item["title"] = title
        seen_links.add(link)
        seen_titles.add(title_key)
        output.append(item)
    return output


def candidate_priority(item: dict) -> int:
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('subcategory', '')} "
        f"{item.get('suggested_subcategory', '')}",
        2200,
    ).lower()
    if "accelerator" in text or "incubator" in text:
        return 0
    if "venture studio" in text or "residency" in text or "fellowship" in text:
        return 1
    if "hackathon" in text or "competition" in text or "challenge" in text:
        return 2
    if "grant" in text or "funding" in text or "sbir" in text or "sttr" in text:
        return 3
    return 4


def build_balanced_candidate_pool(records: list[dict], limit: int) -> list[dict]:
    buckets = {category: [] for category in CATEGORY_ORDER}
    for item in records:
        category = fallback_category(item)
        if category not in buckets:
            continue
        buckets[category].append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (
            candidate_priority(item),
            0 if item.get("method") == "api" else 1,
            0 if item.get("source_confidence") == "high" else 1,
            item.get("deadline") or "9999-12-31",
        ))
    output: list[dict] = []
    while len(output) < limit:
        added = False
        for category in CATEGORY_ORDER:
            if buckets[category]:
                output.append(buckets[category].pop(0))
                added = True
                if len(output) >= limit:
                    break
        if not added:
            break
    return output


def verification_score(item: dict, snapshot: dict[str, Any], action: str, deadline: date | None) -> int:
    score = 45
    if item.get("source_confidence") == "high":
        score += 12
    elif item.get("source_confidence") == "medium":
        score += 5
    if action:
        score += 16
    if deadline and deadline >= TODAY:
        score += 18
    if item.get("method") == "api":
        score += 8
    if snapshot.get("schema_types") and not any(kind in NEWS_SCHEMA_TYPES for kind in snapshot["schema_types"]):
        score += 3
    if is_source_root(snapshot.get("url", "")):
        score -= 15
    return max(0, min(score, 100))


def verify_candidate(item: dict) -> dict | None:
    item = dict(item)
    url = clean_url(item.get("link"))
    category = fallback_category(item)
    snapshot = fetch_snapshot(url)

    # Official APIs with explicit, current dates are already strong evidence and may survive a page outage.
    if not snapshot:
        _, deadline, _ = candidate_dates(item)
        if item.get("method") == "api" and deadline and deadline >= TODAY and basic_publishable(item):
            item["category"] = category
            item["subcategory"] = fallback_subcategory(item, category)
            item["verified_at"] = NOW_ISO
            item["verification_score"] = 80
            item["verification_method"] = "official-api"
            item["action_evidence"] = item.get("action_evidence") or "Official open API record"
            return item
        return None

    page_url = clean_url(snapshot.get("url") or url)
    page_title = clean_text(snapshot.get("title"), 240)
    page_description = clean_text(snapshot.get("description"), 600)
    page_text = clean_text(snapshot.get("text"), 24000)
    primary_text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {page_title} {page_description} "
        f"{snapshot.get('primary_text', '')}",
        6000,
    )
    combined = clean_text(f"{primary_text} {page_text}", 26000)
    if is_news_like(page_title or item.get("title", ""), combined, page_url, snapshot.get("schema_types")):
        return None
    if any(phrase in combined.lower() for phrase in BLOCKED_PAGE_PHRASES):
        return None
    if any(phrase in combined.lower() for phrase in CLOSED_PHRASES):
        return None
    if category != "Event" or not contains_category_evidence(primary_text, "Event"):
        return None

    action = extract_action_evidence(combined)
    opening, deadline, _ = candidate_dates({**item, "page_text": combined})
    status = clean_text(item.get("status"), 40).lower()
    open_text = any(phrase in combined.lower() for phrase in OPEN_PHRASES)

    if category in {"Event", "Funding"}:
        if is_source_root(page_url):
            source = SOURCE_ROOTS[page_url]
            if source.get("source_type") == "directory":
                return None
        if deadline and deadline < TODAY:
            return None
        if item.get("method") != "api" and not action:
            return None
        if not deadline and not (action and (open_text or status in {"open", "rolling", "posted", "active", "enrolling"})):
            return None
    else:
        if not action and status not in {"open", "active", "available", "rolling", "enrolling"}:
            return None

    score = verification_score(item, snapshot, action, deadline)
    if score < 60:
        return None

    title = clean_text(item.get("title") or page_title, 180)
    if len(title) < 8 or title.lower() in {"apply", "learn more", "program", "opportunity"}:
        title = page_title or title
    description = clean_text(item.get("summary") or page_description or page_text, 240)
    item.update({
        "title": title,
        "summary": description,
        "description": description,
        "primary_text": primary_text,
        "link": page_url,
        "category": category,
        "subcategory": fallback_subcategory(item, category),
        "opening_date": iso_or_empty(opening),
        "deadline": iso_or_empty(deadline),
        "status": status or ("open" if action or open_text else ""),
        "action_evidence": action or clean_text(item.get("action_evidence"), 160),
        "verification_excerpt": clean_text(page_description or page_text, 360),
        "verified_at": NOW_ISO,
        "verification_score": score,
        "verification_method": "page-fetch",
        "source_confidence": item.get("source_confidence") or source_confidence_for_url(page_url),
    })
    return item if basic_publishable(item) else None


def verify_candidates(records: list[dict]) -> list[dict]:
    pool = build_balanced_candidate_pool(deduplicate(records), MAX_VERIFY_CANDIDATES)
    output: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_VERIFY_WORKERS) as executor:
        futures = {executor.submit(verify_candidate, item): item for item in pool}
        for future in as_completed(futures):
            try:
                verified = future.result()
                if verified:
                    output.append(verified)
            except Exception as exc:
                print(f"[DEBUG] Verification failed: {exc}")
    verified = deduplicate(output)
    print(f"[INFO] Verified {len(verified)} / {len(pool)} candidate pages")
    return verified


def generate_curated_json(client: genai.Client, prompt: str) -> list[dict]:
    try:
        response = client.models.generate_content(
            model=CURATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.0, max_output_tokens=24000,
            ),
        )
        return extract_json_array(response.text or "")
    except Exception as exc:
        print(f"[WARN] Gemini curation failed: {exc}")
        return []


def curate_with_gemini(client: genai.Client, verified_records: list[dict]) -> list[dict]:
    candidate_pool = build_balanced_candidate_pool(
        verified_records,
        min(MAX_VERIFY_CANDIDATES, max(140, MAX_FINAL_ITEMS * 2)),
    )
    compact = []
    for index, item in enumerate(candidate_pool):
        compact.append({
            "index": index,
            "title": item.get("title", ""),
            "description": item.get("description") or item.get("summary", ""),
            "link": item.get("link", ""),
            "source": item.get("source", ""),
            "deadline": item.get("deadline", ""),
            "status": item.get("status", ""),
            "category": item.get("category") or fallback_category(item),
            "subcategory": item.get("subcategory") or fallback_subcategory(item, fallback_category(item)),
            "action_evidence": item.get("action_evidence", ""),
            "verification_excerpt": item.get("verification_excerpt", ""),
            "verification_score": item.get("verification_score", 0),
        })

    prompt = f"""
You are the final safety curator for Access by Entreprenote. Today is {TODAY.isoformat()}.

Every candidate has already been fetched and deterministically verified, but you must still reject any
record that is news, a company announcement, a generic directory homepage, expired, duplicated or not
something a founder can act on now.

THE DEFINING TEST:
A kept record must let a founder apply, register, submit, enroll, book, claim, download or start using the
resource from the linked page now. Mere relevance to startups is not enough.

ABSOLUTE REJECTIONS:
- fundraising/funding-round news (for example, "Prior Labs raises €1 billion and exits to SAP");
- acquisitions, exits, launches, partnerships, press releases, company profiles and opinion articles;
- directory/list/article pages when a more specific actionable page is required;
- expired or closed applications;
- vague records without action_evidence.

ONLY ALLOWED CONTENT:
- accelerators and accelerator cohorts;
- incubators and incubator programs;
- founder/startup programs, residencies and fellowships;
- hackathons, startup competitions and innovation challenges;
- pitch competitions and demo days;
- startup/founder/entrepreneurship conferences, summits and events.

Reject every funding-only, investor, tool, mentor, course, ordinary business event, corporate news or general startup article.
Prioritize accelerators and incubators, then hackathons and startup competitions. Never alter links.

Return ONLY a JSON array with up to {MAX_FINAL_ITEMS} objects containing exactly:
- source_index: integer
- title: concise factual title
- category: exactly Event
- subcategory: concise type
- description: factual founder-focused summary, maximum 220 characters
- keep: boolean

Candidates:
{json.dumps(compact, ensure_ascii=False)}
"""
    decisions = generate_curated_json(client, prompt)
    output: list[dict] = []
    used: set[int] = set()
    for decision in decisions:
        try:
            index = int(decision.get("source_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(candidate_pool) or index in used or decision.get("keep") is not True:
            continue
        raw = dict(candidate_pool[index])
        if not basic_publishable(raw) or int(raw.get("verification_score", 0)) < 60:
            continue
        category = clean_text(decision.get("category"), 30).title()
        if category != "Event":
            category = fallback_category(raw)
        if category != "Event":
            continue
        raw.update({
            "title": clean_text(decision.get("title") or raw.get("title"), 180),
            "category": category,
            "subcategory": clean_text(decision.get("subcategory"), 50) or fallback_subcategory(raw, category),
            "description": clean_text(decision.get("description") or raw.get("description"), 240),
        })
        output.append(raw)
        used.add(index)

    # Fail-safe backfill uses ONLY pages that already passed deterministic page verification.
    selected_links = {item["link"] for item in output}
    if len(output) < MAX_FINAL_ITEMS:
        print(f"[INFO] Gemini kept {len(output)} records; filling from verified-only candidates")
        for raw in candidate_pool:
            if len(output) >= MAX_FINAL_ITEMS:
                break
            if raw["link"] in selected_links or not basic_publishable(raw):
                continue
            if int(raw.get("verification_score", 0)) < 70:
                continue
            output.append(dict(raw))
            selected_links.add(raw["link"])
    return deduplicate(output)


def load_existing() -> list[dict]:
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def existing_candidates() -> list[dict]:
    output: list[dict] = []
    for raw in load_existing():
        item = dict(raw)
        item["summary"] = item.get("summary") or item.get("description", "")
        item["method"] = "existing"
        item["suggested_category"] = item.get("category", "")
        item["suggested_subcategory"] = item.get("subcategory", "")
        item["source_confidence"] = item.get("source_confidence") or source_confidence_for_url(item.get("link", ""))
        link = clean_url(item.get("link") or item.get("url"))
        text = clean_text(f"{item.get('title', '')} {item.get('summary', '')}", 5000)
        if not allowed_url(link) or is_news_like(item.get("title", ""), text, link):
            continue
        if fallback_category(item) != "Event" or not contains_category_evidence(text, "Event"):
            continue
        _, deadline, _ = candidate_dates(item)
        if deadline and deadline < TODAY:
            continue
        item["link"] = link
        output.append(item)
    return output


def preserve_history(items: list[dict]) -> list[dict]:
    previous = {
        clean_url(item.get("link") or item.get("url")): item
        for item in load_existing()
        if clean_url(item.get("link") or item.get("url"))
    }
    output: list[dict] = []
    for raw in items:
        item = dict(raw)
        old = previous.get(clean_url(item.get("link")), {})
        item["first_seen"] = old.get("first_seen") or old.get("updated_at") or NOW_ISO
        item["last_seen"] = NOW_ISO
        item["updated_at"] = NOW_ISO
        output.append(item)
    return output


def scaled_category_targets(limit: int) -> dict[str, int]:
    targets = {category: int(limit * weight) for category, weight in CATEGORY_TARGET_WEIGHTS.items()}
    remainder = limit - sum(targets.values())
    for category in CATEGORY_ORDER:
        if remainder <= 0:
            break
        targets[category] += 1
        remainder -= 1
    return targets


def record_sort_key(item: dict) -> tuple:
    confidence = {"high": 0, "medium": 1, "low": 2}.get(str(item.get("source_confidence", "medium")).lower(), 1)
    score = -int(item.get("verification_score", 0))
    deadline = parse_date_value(item.get("deadline")) or date.max
    return (candidate_priority(item), score, confidence, deadline, item.get("title", "").lower())


def select_final_records(records: list[dict], limit: int) -> list[dict]:
    buckets = {category: [] for category in CATEGORY_ORDER}
    for raw in deduplicate(records):
        item = dict(raw)
        category = fallback_category(item)
        if category != "Event":
            continue
        item["category"] = "Event"
        item["subcategory"] = item.get("subcategory") or fallback_subcategory(item, category)
        if basic_publishable(item) and int(item.get("verification_score", 0)) >= 60:
            buckets[category].append(item)
    for bucket in buckets.values():
        bucket.sort(key=record_sort_key)

    targets = scaled_category_targets(limit)
    selected: list[dict] = []
    selected_links: set[str] = set()
    for category in CATEGORY_ORDER:
        for item in buckets[category][:targets[category]]:
            selected.append(item)
            selected_links.add(item["link"])
    leftovers = [
        item for category in CATEGORY_ORDER for item in buckets[category]
        if item["link"] not in selected_links
    ]
    leftovers.sort(key=record_sort_key)
    for item in leftovers:
        if len(selected) >= limit:
            break
        selected.append(item)
        selected_links.add(item["link"])
    return selected[:limit]


def validate_final(records: list[dict]) -> None:
    for index, item in enumerate(records):
        required = {
            "title", "category", "subcategory", "description", "link", "source",
            "first_seen", "last_seen", "verified_at", "verification_score", "action_evidence",
        }
        missing = required - set(item)
        if missing:
            raise RuntimeError(f"Record {index} missing fields: {sorted(missing)}")
        if item["category"] != "Event":
            raise RuntimeError(f"Record {index} invalid category: {item['category']}")
        if item.get("subcategory") not in ALLOWED_EVENT_SUBCATEGORIES:
            raise RuntimeError(f"Record {index} invalid event subtype: {item.get('subcategory')}")
        if int(item.get("verification_score", 0)) < 60:
            raise RuntimeError(f"Record {index} insufficient verification score")
        if not basic_publishable(item):
            raise RuntimeError(f"Record {index} failed final safety validation: {item['title']}")


def run_self_tests() -> None:
    bad = {
        "title": "Germany's Prior Labs raises €1 billion and exits to SAP 18 months after being founded",
        "summary": "The startup has raised a funding round and completed an exit.",
        "link": "https://www.eu-startups.com/2026/07/germanys-prior-labs-raises-e1-billion-and-exits-to-sap-18-months-after-being-founded",
        "suggested_category": "Funding", "status": "open", "deadline": "",
    }
    assert is_news_like(bad["title"], bad["summary"], bad["link"])
    assert not basic_publishable(bad)

    lockheed = {
        "title": "US investor Lockheed Martin Ventures commits at least €87 million to Europe as it opens new office in the UK",
        "summary": "A corporate investment and office-opening announcement.",
        "link": "https://example-news.com/2026/07/lockheed-martin-ventures-opens-new-office",
        "suggested_category": "Event", "status": "open",
    }
    assert is_news_like(lockheed["title"], lockheed["summary"], lockheed["link"])
    assert not basic_publishable(lockheed)

    good = {
        "title": "Global Startup Accelerator 2026 Applications Open",
        "summary": "Applications are open. Apply now to join the accelerator cohort.",
        "link": "https://example.org/accelerator/apply",
        "suggested_category": "Event", "status": "open", "deadline": f"{CURRENT_YEAR}-12-31",
    }
    assert basic_publishable(good)
    print("[OK] Safety self-tests passed")


def main() -> None:
    run_self_tests()
    if "--self-test" in sys.argv:
        return
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from GitHub Actions secrets")

    client = genai.Client(api_key=API_KEY)

    raw: list[dict] = []
    raw.extend(crawl_direct_sources())
    raw.extend(discover_with_google_search(client))
    # Event-only product: no generic RSS, grants, tools, advisors or learning feeds.
    raw.extend(existing_candidates())
    raw = [item for item in deduplicate(raw) if allowed_url(item.get("link", ""))]

    candidate_counts = Counter(fallback_category(item) for item in raw)
    print(f"[INFO] Raw candidate categories: {dict(candidate_counts)}")
    print(f"[INFO] Raw unique candidates: {len(raw)}")

    verified = verify_candidates(raw)
    verified_counts = Counter(fallback_category(item) for item in verified)
    print(f"[INFO] Verified categories: {dict(verified_counts)}")

    curated = curate_with_gemini(client, verified)
    final = preserve_history(select_final_records(curated, MAX_FINAL_ITEMS))
    validate_final(final)

    final_counts = Counter(item["category"] for item in final)
    print(f"[INFO] Final categories: {dict(final_counts)}")
    print(f"[INFO] Final records: {len(final)} / {MAX_FINAL_ITEMS}")

    if len(final) < MIN_GOOD_RUN_ITEMS:
        raise RuntimeError(f"Only {len(final)} safe event records were produced; refusing to publish stale or mixed data")

    DATA_FILE.write_text(
        json.dumps(final, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"[OK] Wrote {len(final)} verified autonomous Access records")


if __name__ == "__main__":
    main()
