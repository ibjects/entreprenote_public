from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DISCOVERY_MODEL = os.environ.get(
    "GEMINI_DISCOVERY_MODEL",
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
).strip()
CURATION_MODEL = os.environ.get(
    "GEMINI_CURATOR_MODEL",
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
).strip()

MAX_FINAL_ITEMS = int(os.environ.get("MAX_FINAL_ITEMS", "100"))
MIN_GOOD_RUN_ITEMS = int(os.environ.get("MIN_GOOD_RUN_ITEMS", "10"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "25"))
CURRENT_YEAR = datetime.now(timezone.utc).year
TODAY = datetime.now(timezone.utc).date()

# Existing undated event/funding records are retained briefly when a source misses them
# during one run. Evergreen resources get a longer grace period.
STALE_DAYS = {
    "Event": 45,
    "Funding": 45,
    "Advisor": 180,
    "Learning": 180,
    "Tool": 180,
}

# Target mix at 100 records. Targets scale automatically if MAX_FINAL_ITEMS changes.
CATEGORY_TARGET_WEIGHTS = {
    "Event": 0.35,
    "Funding": 0.30,
    "Tool": 0.15,
    "Advisor": 0.10,
    "Learning": 0.10,
}
CATEGORY_ORDER = ("Event", "Funding", "Tool", "Advisor", "Learning")

# Opportunity feeds. Generic startup-news feeds are intentionally excluded.
RSS_FEEDS = [
    ("Opportunity Desk", "https://opportunitydesk.org/feed/", 20),
    ("Youth Opportunities", "https://www.youthop.com/feed/", 20),
    ("fundsforNGOs", "https://www2.fundsforngos.org/feed/", 18),
    ("fundsforNGOs Listings", "https://www2.fundsforngos.org/category/listing/feed/", 18),
    ("AlphaGamma Opportunities", "https://www.alphagamma.eu/category/opportunities/feed/", 18),
    ("EU-Startups", "https://www.eu-startups.com/feed/", 15),
]

# Cleaned source catalog built from the user's researched source list.
# These pages are search hints, not automatically trusted listings. Gemini must find a
# current official application/registration/resource page before an item is published.
SOURCE_CATALOG = [
    {"name": "Devpost", "url": "https://devpost.com/hackathons", "categories": ["Event"], "confidence": "high"},
    {"name": "TAIKAI", "url": "https://taikai.network/en/hackathons", "categories": ["Event"], "confidence": "high"},
    {"name": "Major League Hacking", "url": "https://mlh.io/seasons/2026/events", "categories": ["Event"], "confidence": "high"},
    {"name": "HackerEarth", "url": "https://www.hackerearth.com/challenges/", "categories": ["Event"], "confidence": "high"},
    {"name": "Unstop", "url": "https://unstop.com/hackathons", "categories": ["Event"], "confidence": "high"},
    {"name": "Kaggle Competitions", "url": "https://www.kaggle.com/competitions", "categories": ["Event"], "confidence": "high"},
    {"name": "Hackathon.com", "url": "https://www.hackathon.com/", "categories": ["Event"], "confidence": "medium"},
    {"name": "Agorize", "url": "https://www.agorize.com/en/challenges", "categories": ["Event"], "confidence": "high"},
    {"name": "ChallengeRocket", "url": "https://challengerocket.com/", "categories": ["Event"], "confidence": "medium"},
    {"name": "MindSumo", "url": "https://www.mindsumo.com/challenges", "categories": ["Event"], "confidence": "high"},
    {"name": "Vestbee", "url": "https://www.vestbee.com/programs", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Gust Programs", "url": "https://gust.com/search/programs", "categories": ["Event"], "confidence": "medium"},
    {"name": "Failory Accelerator Directory", "url": "https://www.failory.com/accelerators", "categories": ["Event"], "confidence": "medium"},
    {"name": "SOSV", "url": "https://sosv.com/programs/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Founders Factory", "url": "https://foundersfactory.com/accelerator/", "categories": ["Event"], "confidence": "high"},
    {"name": "EIT Digital", "url": "https://www.eitdigital.eu/accelerator/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Alchemist Accelerator", "url": "https://www.alchemistaccelerator.com/", "categories": ["Event"], "confidence": "high"},
    {"name": "Village Global", "url": "https://www.villageglobal.vc/accelerator", "categories": ["Event", "Funding"], "confidence": "medium"},
    {"name": "Startup Chile", "url": "https://startupchile.org/en/programs/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Founders Network", "url": "https://foundersnetwork.com/", "categories": ["Advisor", "Event"], "confidence": "medium"},
    {"name": "EIC Accelerator", "url": "https://eic.ec.europa.eu/eic-funding-opportunities/eic-accelerator_en", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Startus Insights", "url": "https://www.startus-insights.com/innovators-guide/", "categories": ["Event", "Funding"], "confidence": "medium"},
    {"name": "EU-Startups Directory", "url": "https://www.eu-startups.com/directory/", "categories": ["Event"], "confidence": "high"},
    {"name": "FounderHub", "url": "https://founderhub.io/", "categories": ["Event", "Advisor"], "confidence": "medium"},
    {"name": "StartupBlink", "url": "https://www.startupblink.com/accelerators", "categories": ["Event"], "confidence": "high"},
    {"name": "OpenGrants", "url": "https://www.opengrants.io/", "categories": ["Funding"], "confidence": "high"},
    {"name": "SBIR / STTR", "url": "https://www.sbir.gov/solicitation-listing/open", "categories": ["Funding"], "confidence": "high"},
    {"name": "Innovate UK Funding", "url": "https://apply-for-innovation-funding.service.gov.uk/competition/search", "categories": ["Funding"], "confidence": "high"},
    {"name": "EU Funding and Tenders", "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/programmes/horizon", "categories": ["Funding"], "confidence": "high"},
    {"name": "Instrumentl", "url": "https://www.instrumentl.com/grants", "categories": ["Funding"], "confidence": "medium"},
    {"name": "Global Innovation Fund", "url": "https://www.globalinnovation.fund/apply/process/", "categories": ["Funding"], "confidence": "high"},
    {"name": "Hello Tomorrow", "url": "https://hello-tomorrow.org/global-challenge/", "categories": ["Funding", "Event"], "confidence": "high"},
    {"name": "Skip", "url": "https://helloskip.com/grants", "categories": ["Funding"], "confidence": "medium"},
    {"name": "Sky's the Limit", "url": "https://www.skysthelimit.org/", "categories": ["Advisor", "Funding"], "confidence": "high"},
    {"name": "GrowthMentor", "url": "https://www.growthmentor.com/", "categories": ["Advisor"], "confidence": "medium"},
    {"name": "Clarity", "url": "https://clarity.fm/", "categories": ["Advisor"], "confidence": "medium"},
    {"name": "MentorCruise", "url": "https://mentorcruise.com/", "categories": ["Advisor"], "confidence": "medium"},
    {"name": "Luma Tech Events", "url": "https://lu.ma/explore", "categories": ["Event"], "confidence": "high"},
    {"name": "Eventbrite Startups", "url": "https://www.eventbrite.com/d/online/startup/", "categories": ["Event"], "confidence": "medium"},
    {"name": "Meetup Startups", "url": "https://www.meetup.com/topics/startup/", "categories": ["Event"], "confidence": "medium"},
    {"name": "Startup Digest", "url": "https://www.startupdigest.com/", "categories": ["Event"], "confidence": "medium"},
    {"name": "Web Summit", "url": "https://websummit.com/", "categories": ["Event"], "confidence": "high"},
    {"name": "Secret", "url": "https://www.joinsecret.com/", "categories": ["Tool"], "confidence": "high"},
    {"name": "Startup Basecamp", "url": "https://startupbasecamp.org/startup-tools/", "categories": ["Tool", "Learning"], "confidence": "high"},
    {"name": "FounderPass", "url": "https://www.founderpass.com/", "categories": ["Tool"], "confidence": "high"},
    {"name": "Stripe Atlas Perks", "url": "https://stripe.com/atlas/perks", "categories": ["Tool"], "confidence": "medium"},
    {"name": "GitHub for Startups", "url": "https://github.com/enterprise/startups", "categories": ["Tool"], "confidence": "high"},
    {"name": "YC Startup School", "url": "https://www.startupschool.org/", "categories": ["Learning", "Event"], "confidence": "high"},
    {"name": "Coursera Entrepreneurship", "url": "https://www.coursera.org/browse/business/entrepreneurship", "categories": ["Learning"], "confidence": "high"},
    {"name": "edX Entrepreneurship", "url": "https://www.edx.org/learn/entrepreneurship", "categories": ["Learning"], "confidence": "medium"},
    {"name": "HubSpot Academy", "url": "https://academy.hubspot.com/", "categories": ["Learning"], "confidence": "high"},
    {"name": "SCORE", "url": "https://www.score.org/", "categories": ["Learning", "Advisor"], "confidence": "high"},
]

# Existing strong sources retained from the earlier version.
SOURCE_CATALOG.extend([
    {"name": "F6S Programs", "url": "https://www.f6s.com/programs", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Techstars", "url": "https://www.techstars.com/accelerators", "categories": ["Event"], "confidence": "high"},
    {"name": "MassChallenge", "url": "https://masschallenge.org/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Founder Institute", "url": "https://fi.co/enrolling", "categories": ["Event"], "confidence": "high"},
    {"name": "Antler", "url": "https://www.antler.co/apply", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Antler Cohorts", "url": "https://www.antler.co/cohort-start-dates", "categories": ["Event"], "confidence": "high"},
    {"name": "Startupbootcamp", "url": "https://startupbootcamp.org/", "categories": ["Event"], "confidence": "high"},
    {"name": "Seedstars", "url": "https://www.seedstars.com/community/entrepreneurs/programs/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Y Combinator", "url": "https://www.ycombinator.com/apply", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "500 Global", "url": "https://500.co/", "categories": ["Event", "Funding"], "confidence": "high"},
    {"name": "Plug and Play", "url": "https://www.plugandplaytechcenter.com/", "categories": ["Event"], "confidence": "high"},
    {"name": "Google for Startups", "url": "https://startup.google.com/programs/", "categories": ["Event", "Tool"], "confidence": "high"},
    {"name": "AWS Startups", "url": "https://aws.amazon.com/startups/", "categories": ["Tool", "Learning"], "confidence": "high"},
    {"name": "Microsoft for Startups", "url": "https://www.microsoft.com/startups", "categories": ["Tool", "Event"], "confidence": "high"},
])

DISCOVERY_JOBS = {
    "Event": (
        "Find currently open startup accelerators, incubators, venture studios, founder residencies, fellowships, "
        "hackathons, startup competitions, pitch programs, demo-day applications and founder events worldwide."
    ),
    "Funding": (
        "Find currently open startup grants, non-dilutive funding, innovation prizes, equity-free competitions, "
        "SBIR/STTR calls and founder funding application channels."
    ),
    "Advisor": (
        "Find currently accessible founder mentorship programs, startup advisor office hours, legal clinics, "
        "finance advice, product mentoring and growth mentoring."
    ),
    "Learning": (
        "Find currently accessible practical entrepreneurship courses, workshops, bootcamps, playbooks, templates "
        "and founder education programs. Prefer free or low-cost resources."
    ),
    "Tool": (
        "Find currently accessible startup tools, cloud credits, founder discounts, software perks, legal services, "
        "accounting resources, marketing tools and development tools for founders."
    ),
}

ACTION_WORDS = (
    "apply", "applications open", "deadline", "grant", "funding", "accelerator",
    "incubator", "venture studio", "residency", "hackathon", "competition",
    "challenge", "prize", "award", "fellowship", "bootcamp", "pitch",
    "demo day", "mentor", "mentorship", "advisor", "office hours", "workshop",
    "webinar", "course", "program", "register", "open call", "tool", "software",
    "platform", "startup credits", "cloud credits", "discount", "service",
    "template", "playbook", "guide", "sign up", "get started", "book", "download",
)
CLOSED_WORDS = (
    "applications closed", "application closed", "closed for applications",
    "deadline passed", "expired", "no longer accepting", "submissions closed",
    "registration closed", "call closed",
)
OPEN_STATUS_WORDS = (
    "open", "accepting", "rolling", "ongoing", "active", "applications open",
    "available", "enrolling",
)
BLOCKED_DOMAINS = {
    "producthunt.com", "www.producthunt.com", "techcrunch.com",
    "news.ycombinator.com",
}
DIRECTORY_URLS = {urlparse(source["url"]).netloc.lower() + urlparse(source["url"]).path.rstrip("/") for source in SOURCE_CATALOG}

DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y",
    "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y", "%d %B %Y",
)

session = requests.Session()
session.headers.update({
    "User-Agent": "AccessByEntreprenote/3.0 (+https://entreprenote.com/access/)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
})


def clean_text(value: object, limit: int = 900) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def unwrap_markdown_url(value: object) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"\[(https?://[^\]]+)\]\((https?://[^)]+)\)", raw)
    return match.group(1) if match else raw


def clean_url(value: object) -> str:
    try:
        raw = unwrap_markdown_url(value)
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""

        # Gemini sometimes returns a Google search wrapper instead of the destination.
        if parsed.netloc.lower() in {"google.com", "www.google.com"} and parsed.path == "/search":
            target = dict(parse_qsl(parsed.query)).get("q", "")
            if target.startswith(("http://", "https://")):
                return clean_url(target)

        query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "ref", "source"}
        ]
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((
            parsed.scheme.lower(), parsed.netloc.lower(), path, "",
            urlencode(query, doseq=True), "",
        ))
    except Exception:
        return ""


def allowed_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return bool(url) and host not in BLOCKED_DOMAINS


def looks_actionable(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(word in text for word in ACTION_WORDS)


def parse_date_value(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = clean_text(value, 100)
    if not raw:
        return None
    raw = re.sub(
        r"\b(?:deadline|closing|close date|opening|open date|published|apply by)\s*:\s*",
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


def candidate_dates(item: dict) -> tuple[date | None, date | None, date | None]:
    summary = clean_text(item.get("summary") or item.get("description"), 1600)
    deadline = (
        parse_date_value(item.get("deadline"))
        or parse_date_value(item.get("close_date"))
        or extract_labeled_date(summary, ("deadline", "closing", "close date", "apply by"))
    )
    opening = (
        parse_date_value(item.get("opening_date"))
        or parse_date_value(item.get("open_date"))
        or extract_labeled_date(summary, ("opening", "open date", "opens"))
    )
    published = (
        parse_date_value(item.get("published_at"))
        or parse_date_value(item.get("updated_at"))
        or parse_date_value(item.get("first_seen"))
    )
    return opening, deadline, published


def is_current_or_open(item: dict) -> bool:
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} "
        f"{item.get('description', '')} {item.get('status', '')}",
        2200,
    ).lower()
    status = clean_text(item.get("status"), 80).lower()

    if any(word in text for word in CLOSED_WORDS):
        return False

    opening, deadline, published = candidate_dates(item)
    if deadline:
        return deadline >= TODAY

    if any(word in status for word in OPEN_STATUS_WORDS) or any(
        phrase in text
        for phrase in (
            "rolling applications", "rolling intake", "applications are open",
            "apply now", "now accepting", "open enrollment", "currently available",
        )
    ):
        return True

    category = fallback_category(item)
    if category in {"Tool", "Advisor", "Learning"} and item.get("method") == "gemini-search":
        # Evergreen resources are allowed only when grounded search explicitly returned them
        # as currently accessible.
        return status in {"open", "active", "available", "rolling"}

    if status in {"posted", "forecasted"}:
        anchor = opening or published
        return bool(anchor and anchor.year >= CURRENT_YEAR)

    anchor = opening or published
    return bool(anchor and anchor.year >= CURRENT_YEAR)


def iso_or_empty(value: date | None) -> str:
    return value.isoformat() if value else ""


def source_confidence_for_url(url: str) -> str:
    host = urlparse(clean_url(url)).netloc.lower()
    for source in SOURCE_CATALOG:
        if urlparse(source["url"]).netloc.lower() == host:
            return source["confidence"]
    if host.endswith(".gov") or ".gov." in host or host.endswith(".europa.eu"):
        return "high"
    return "medium"


def fetch_rss() -> list[dict]:
    records: list[dict] = []
    for source, url, limit in RSS_FEEDS:
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            added = 0
            for entry in feed.entries[:limit]:
                title = clean_text(entry.get("title"), 180)
                link = clean_url(entry.get("link"))
                summary = clean_text(entry.get("summary") or entry.get("description") or "", 1000)
                if not title or not allowed_url(link) or not looks_actionable(title, summary):
                    continue

                published = rss_published_date(entry)
                candidate = {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                    "method": "rss",
                    "published_at": iso_or_empty(published),
                    "status": "open" if any(x in f"{title} {summary}".lower() for x in ("applications open", "apply now", "open call")) else "",
                    "source_confidence": "medium",
                }
                opening, deadline, _ = candidate_dates(candidate)
                candidate["opening_date"] = iso_or_empty(opening)
                candidate["deadline"] = iso_or_empty(deadline)
                if not is_current_or_open(candidate):
                    continue
                records.append(candidate)
                added += 1
            print(f"[OK] {source}: {added} current/open RSS records")
        except Exception as exc:
            print(f"[WARN] {source} failed: {exc}")
    return records


def fetch_grants_gov() -> list[dict]:
    endpoint = "https://api.grants.gov/v1/api/search2"
    records: list[dict] = []
    for keyword in ("small business", "entrepreneurship", "innovation", "startup"):
        try:
            response = session.post(
                endpoint,
                json={"keyword": keyword, "oppStatuses": "posted|forecasted", "rows": 15},
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
                candidate = {
                    "title": title,
                    "summary": clean_text(
                        f"Agency: {hit.get('agencyName', '')}. Opening: {hit.get('openDate', '')}. "
                        f"Closing: {hit.get('closeDate', '')}. Status: {status}.",
                        900,
                    ),
                    "link": f"https://www.grants.gov/search-results-detail/{opportunity_id}",
                    "source": "Grants.gov",
                    "method": "api",
                    "opening_date": iso_or_empty(opening),
                    "deadline": iso_or_empty(deadline),
                    "status": status,
                    "suggested_category": "Funding",
                    "suggested_subcategory": "Grant",
                    "source_confidence": "high",
                }
                if is_current_or_open(candidate):
                    records.append(candidate)
        except Exception as exc:
            print(f"[WARN] Grants.gov '{keyword}' failed: {exc}")
    print(f"[OK] Grants.gov: {len(records)} current/open records")
    return records


def find_first_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "data", "solicitations", "response"):
        value = payload.get(key)
        found = find_first_list(value)
        if found:
            return found
    return []


def fetch_sbir() -> list[dict]:
    """Fetch the official SBIR open-soliciation API; fail safely during API maintenance."""
    endpoint = "https://api.www.sbir.gov/public/api/solicitations?open=1&rows=50"
    records: list[dict] = []
    try:
        response = session.get(endpoint, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        rows = find_first_list(response.json())
        for row in rows:
            title = clean_text(row.get("solicitation_title") or row.get("title"), 180)
            link = clean_url(row.get("solicitation_agency_url") or row.get("url"))
            if not title or not allowed_url(link):
                continue
            opening = parse_date_value(row.get("open_date") or row.get("release_date"))
            deadline = parse_date_value(row.get("close_date"))
            if not deadline:
                due_dates = row.get("application_due_date") or []
                if isinstance(due_dates, list):
                    future_dates = [d for d in (parse_date_value(x) for x in due_dates) if d and d >= TODAY]
                    deadline = min(future_dates) if future_dates else None
                else:
                    deadline = parse_date_value(due_dates)
            status = clean_text(row.get("current_status"), 40).lower() or "open"
            candidate = {
                "title": title,
                "summary": clean_text(
                    f"{row.get('agency', '')} {row.get('program', '')} {row.get('phase', '')} "
                    f"{row.get('solicitation_number', '')}",
                    900,
                ),
                "link": link,
                "source": "SBIR / STTR",
                "method": "api",
                "opening_date": iso_or_empty(opening),
                "deadline": iso_or_empty(deadline),
                "status": status,
                "suggested_category": "Funding",
                "suggested_subcategory": "SBIR / STTR",
                "source_confidence": "high",
            }
            if is_current_or_open(candidate):
                records.append(candidate)
        print(f"[OK] SBIR / STTR: {len(records)} current/open records")
    except Exception as exc:
        print(f"[WARN] SBIR / STTR API unavailable: {exc}")
    return records


def extract_json_array(text: str) -> list[dict]:
    value = (text or "").replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass

    start = value.find("[")
    end = value.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(value[start:end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Last-resort extraction for responses containing multiple standalone JSON objects.
    decoder = json.JSONDecoder()
    output: list[dict] = []
    index = 0
    while index < len(value):
        brace = value.find("{", index)
        if brace < 0:
            break
        try:
            item, consumed = decoder.raw_decode(value[brace:])
            if isinstance(item, dict):
                output.append(item)
            index = brace + consumed
        except json.JSONDecodeError:
            index = brace + 1
    return output


def source_batches(category: str, batch_size: int = 10) -> list[list[dict]]:
    sources = [source for source in SOURCE_CATALOG if category in source["categories"]]
    sources.sort(key=lambda source: (source["confidence"] != "high", source["name"].lower()))
    return [sources[index:index + batch_size] for index in range(0, len(sources), batch_size)]


def grounding_fallback_candidates(response: object, category: str, source_names: str) -> list[dict]:
    records: list[dict] = []
    response_text = clean_text(getattr(response, "text", ""), 1200)
    lower_text = response_text.lower()
    status = "open" if any(word in lower_text for word in ("open", "rolling", "apply now", "available")) else ""
    try:
        candidate = response.candidates[0]
        metadata = getattr(candidate, "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) or []
    except Exception:
        chunks = []

    for chunk in chunks:
        web = getattr(chunk, "web", None)
        title = clean_text(getattr(web, "title", ""), 180)
        link = clean_url(getattr(web, "uri", ""))
        if not title or not allowed_url(link) or not looks_actionable(title, response_text):
            continue
        record = {
            "title": title,
            "summary": response_text,
            "link": link,
            "source": source_names,
            "method": "gemini-search",
            "opening_date": "",
            "deadline": "",
            "status": status,
            "suggested_category": category,
            "suggested_subcategory": "",
            "source_confidence": source_confidence_for_url(link),
        }
        if is_current_or_open(record):
            records.append(record)
    return records


def discover_with_google_search(client: genai.Client) -> list[dict]:
    records: list[dict] = []
    search_tool = types.Tool(google_search=types.GoogleSearch())

    for category, request_text in DISCOVERY_JOBS.items():
        for batch_number, batch in enumerate(source_batches(category), start=1):
            source_lines = "\n".join(f"- {item['name']}: {item['url']}" for item in batch)
            source_names = ", ".join(item["name"] for item in batch)
            prompt = f"""
Today is {TODAY.isoformat()}.

{request_text}

Search these source pages first and follow their links to official application, registration,
booking, claim or resource pages:
{source_lines}

Strict publication rules:
- Return only opportunities/resources currently open, rolling, enrolling, available, or with a deadline on or after today.
- For Event or Funding, do not return an expired cohort, old article, generic directory article, or directory homepage as the listing.
- Prefer the official organizer page; an active F6S/Devpost/competition-host application page is acceptable.
- For Advisor, Learning and Tool, an evergreen page is valid only when a founder can access, book, enroll, claim or start it now.
- Exclude jobs, ordinary scholarships, news, funding-round announcements, opinion articles and product-launch articles.
- Never invent a URL or deadline.

Return ONLY a JSON array with 6 to 12 objects. Every object must contain:
- title
- summary
- link
- source
- opening_date: YYYY-MM-DD or empty string
- deadline: YYYY-MM-DD or empty string
- status: open, rolling, active, available, posted, forecasted, or empty string
- suggested_category: exactly {category}
- suggested_subcategory: a concise type such as Accelerator, Incubator, Hackathon, Competition / Challenge,
  Conference, Grant, Mentorship, Course, Workshop, Startup Tool or Founder Perk

Do not use markdown.
"""
            try:
                response = client.models.generate_content(
                    model=DISCOVERY_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(tools=[search_tool]),
                )
                found = extract_json_array(response.text or "")
                accepted = 0
                for item in found:
                    title = clean_text(item.get("title"), 180)
                    summary = clean_text(item.get("summary"), 1000)
                    link = clean_url(item.get("link"))
                    if not title or not allowed_url(link) or not looks_actionable(title, summary):
                        continue

                    status = clean_text(item.get("status"), 40).lower()
                    candidate = {
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "source": clean_text(item.get("source"), 120) or source_names,
                        "method": "gemini-search",
                        "opening_date": iso_or_empty(parse_date_value(item.get("opening_date"))),
                        "deadline": iso_or_empty(parse_date_value(item.get("deadline"))),
                        "status": status,
                        "suggested_category": category,
                        "suggested_subcategory": clean_text(item.get("suggested_subcategory"), 50),
                        "source_confidence": source_confidence_for_url(link),
                    }
                    if not is_current_or_open(candidate):
                        continue
                    records.append(candidate)
                    accepted += 1

                if not accepted:
                    fallback = grounding_fallback_candidates(response, category, source_names)
                    records.extend(fallback)
                    accepted = len(fallback)

                print(f"[OK] Gemini Search {category} batch {batch_number}: {accepted} records")
            except Exception as exc:
                print(f"[WARN] Gemini Search {category} batch {batch_number} failed: {exc}")

    return records


def deduplicate(records: list[dict]) -> list[dict]:
    output: list[dict] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    for item in records:
        link = clean_url(item.get("link") or item.get("url"))
        title = clean_text(item.get("title"), 180)
        title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        if not allowed_url(link) or not title_key:
            continue
        if link in seen_links or title_key in seen_titles:
            continue
        item = dict(item)
        item["link"] = link
        item["title"] = title
        seen_links.add(link)
        seen_titles.add(title_key)
        output.append(item)
    return output


def fallback_category(item: dict) -> str:
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')}".lower()
    suggested = clean_text(item.get("suggested_category") or item.get("category"), 30).title()
    mapping = {"Events": "Event", "Advisors": "Advisor", "Learn": "Learning", "Services": "Tool"}
    suggested = mapping.get(suggested, suggested)
    if suggested in CATEGORY_ORDER:
        return suggested

    # Specific founder-program terms should win before broad words like funding/program.
    if any(word in text for word in (
        "accelerator", "incubator", "venture studio", "founder residency",
        "hackathon", "competition", "challenge", "fellowship", "pitch event",
        "demo day", "conference", "summit",
    )):
        return "Event"
    if any(word in text for word in ("mentor", "advisor", "office hours", "legal clinic")):
        return "Advisor"
    if any(word in text for word in ("course", "workshop", "webinar", "training", "playbook", "template")):
        return "Learning"
    if any(word in text for word in ("startup credits", "cloud credits", "software", "tool", "discount", "perk")):
        return "Tool"
    if any(word in text for word in ("grant", "funding", "fund", "prize", "award", "investment", "sbir", "sttr")):
        return "Funding"
    return "Tool"


def fallback_subcategory(item: dict, category: str) -> str:
    suggested = clean_text(item.get("suggested_subcategory") or item.get("subcategory"), 50)
    if suggested:
        return suggested
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')}".lower()
    rules = [
        ("accelerator", "Accelerator"), ("incubator", "Incubator"),
        ("venture studio", "Venture Studio"), ("residency", "Founder Residency"),
        ("hackathon", "Hackathon"), ("fellowship", "Fellowship"),
        ("competition", "Competition / Challenge"), ("challenge", "Competition / Challenge"),
        ("conference", "Conference"), ("summit", "Conference"),
        ("mentor", "Mentorship"), ("office hours", "Office Hours"),
        ("workshop", "Workshop"), ("webinar", "Webinar"), ("course", "Course"),
        ("sbir", "SBIR / STTR"), ("sttr", "SBIR / STTR"), ("grant", "Grant"),
        ("credit", "Founder Perk"), ("discount", "Founder Perk"), ("template", "Template"),
    ]
    for keyword, label in rules:
        if keyword in text:
            return label
    return {
        "Funding": "Grant", "Event": "Program", "Advisor": "Mentorship",
        "Learning": "Learning Program", "Tool": "Startup Tool",
    }[category]


def candidate_priority(item: dict) -> int:
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('subcategory', '')} {item.get('suggested_subcategory', '')}".lower()
    if "accelerator" in text or "incubator" in text:
        return 0
    if "venture studio" in text or "residency" in text or "fellowship" in text:
        return 1
    if "hackathon" in text or "competition" in text or "challenge" in text:
        return 2
    if "grant" in text or "funding" in text or "sbir" in text or "sttr" in text:
        return 3
    return 4


def build_balanced_candidate_pool(records: list[dict], limit: int = 180) -> list[dict]:
    buckets = {category: [] for category in CATEGORY_ORDER}
    for item in records:
        buckets[fallback_category(item)].append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (candidate_priority(item), item.get("deadline") or "9999-12-31"))

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


def curate_with_gemini(client: genai.Client, raw_records: list[dict]) -> list[dict]:
    candidate_pool = build_balanced_candidate_pool(raw_records, min(180, max(120, MAX_FINAL_ITEMS * 2)))
    compact = []
    for index, item in enumerate(candidate_pool):
        compact.append({
            "index": index,
            "title": item.get("title", ""),
            "summary": item.get("summary", "")[:700],
            "link": item.get("link", ""),
            "source": item.get("source", ""),
            "opening_date": item.get("opening_date", ""),
            "deadline": item.get("deadline", ""),
            "status": item.get("status", ""),
            "suggested_category": item.get("suggested_category", ""),
            "suggested_subcategory": item.get("suggested_subcategory", ""),
        })

    prompt = f"""
You are the autonomous curator for Access by Entreprenote.
Today is {TODAY.isoformat()}.

Select only actionable, currently accessible founder opportunities and resources.
Reject expired, closed, irrelevant, inaccessible, duplicated or news-only records.
Do not alter or invent links. Use source_index to refer to the supplied candidate.

Category definitions:
- Event: accelerators, incubators, venture studios, residencies, fellowships, hackathons,
  competitions, conferences, webinars and pitch programs.
- Funding: grants, non-dilutive funding, prizes, SBIR/STTR and open funding channels.
- Advisor: mentorship, office hours, legal, finance, product and growth advice.
- Learning: practical courses, workshops, templates, playbooks and founder education.
- Tool: startup software, credits, perks and practical professional services.

Aim for a strong balanced directory rather than allowing one source/category to dominate.
When enough candidates exist, prioritize accelerators and incubators and include all five categories.
Return up to {MAX_FINAL_ITEMS} decisions.

Return ONLY a JSON array. Every object must contain exactly:
- source_index: integer
- title: concise factual title
- category: Funding, Event, Advisor, Learning, or Tool
- subcategory: concise type
- description: factual founder-focused summary, maximum 220 characters
- keep: boolean

Candidates:
{json.dumps(compact, ensure_ascii=False)}
"""
    try:
        response = client.models.generate_content(
            model=CURATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=24000,
            ),
        )
        decisions = extract_json_array(response.text or "")
    except Exception as exc:
        print(f"[WARN] Gemini curation failed: {exc}")
        decisions = []

    final: list[dict] = []
    used_indexes: set[int] = set()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for decision in decisions:
        try:
            index = int(decision.get("source_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(candidate_pool) or index in used_indexes:
            continue
        if decision.get("keep") is False:
            continue
        raw = candidate_pool[index]
        if not is_current_or_open(raw):
            continue

        category = clean_text(decision.get("category"), 30).title()
        if category not in CATEGORY_ORDER:
            category = fallback_category(raw)
        opening, deadline, _ = candidate_dates(raw)
        final.append({
            "title": clean_text(decision.get("title") or raw.get("title"), 180),
            "category": category,
            "subcategory": clean_text(decision.get("subcategory"), 50) or fallback_subcategory(raw, category),
            "description": clean_text(decision.get("description") or raw.get("summary"), 240),
            "link": raw["link"],
            "source": raw.get("source", ""),
            "opening_date": iso_or_empty(opening),
            "deadline": iso_or_empty(deadline),
            "status": clean_text(raw.get("status"), 40),
            "source_confidence": raw.get("source_confidence") or source_confidence_for_url(raw["link"]),
            "updated_at": now,
        })
        used_indexes.add(index)

    # Deterministic backfill makes the product resilient to short/invalid Gemini output.
    if len(final) < MAX_FINAL_ITEMS:
        print(f"[INFO] Gemini selected {len(final)} records; filling remaining capacity deterministically")
        existing_links = {item["link"] for item in final}
        for raw in candidate_pool:
            if len(final) >= MAX_FINAL_ITEMS:
                break
            if raw["link"] in existing_links or not is_current_or_open(raw):
                continue
            category = fallback_category(raw)
            opening, deadline, _ = candidate_dates(raw)
            final.append({
                "title": clean_text(raw.get("title"), 180),
                "category": category,
                "subcategory": fallback_subcategory(raw, category),
                "description": clean_text(raw.get("summary"), 240),
                "link": raw["link"],
                "source": raw.get("source", ""),
                "opening_date": iso_or_empty(opening),
                "deadline": iso_or_empty(deadline),
                "status": clean_text(raw.get("status"), 40),
                "source_confidence": raw.get("source_confidence") or source_confidence_for_url(raw["link"]),
                "updated_at": now,
            })
            existing_links.add(raw["link"])

    return deduplicate(final)


def load_existing() -> list[dict]:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_iso_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def retain_existing(item: dict) -> bool:
    category = fallback_category(item)
    text = clean_text(f"{item.get('title', '')} {item.get('description', '')} {item.get('status', '')}", 1600).lower()
    if any(word in text for word in CLOSED_WORDS):
        return False
    _, deadline, _ = candidate_dates(item)
    if deadline:
        return deadline >= TODAY

    last_seen = parse_iso_datetime(item.get("last_seen") or item.get("updated_at") or item.get("first_seen"))
    if not last_seen:
        return False
    age = datetime.now(timezone.utc) - last_seen
    return age <= timedelta(days=STALE_DAYS.get(category, 45))


def merge_with_existing(new_items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing = deduplicate(load_existing())
    existing_by_link = {clean_url(item.get("link") or item.get("url")): item for item in existing}

    merged: list[dict] = []
    rediscovered_links: set[str] = set()
    for item in new_items:
        item = dict(item)
        link = clean_url(item.get("link"))
        previous = existing_by_link.get(link, {})
        item["link"] = link
        item["first_seen"] = previous.get("first_seen") or previous.get("updated_at") or now
        item["last_seen"] = now
        item["updated_at"] = now
        merged.append(item)
        rediscovered_links.add(link)

    retained = 0
    for old in existing:
        link = clean_url(old.get("link") or old.get("url"))
        if not link or link in rediscovered_links or not retain_existing(old):
            continue
        old = dict(old)
        old["link"] = link
        old.setdefault("first_seen", old.get("updated_at") or now)
        old.setdefault("last_seen", old.get("updated_at") or old["first_seen"])
        old.setdefault("source_confidence", source_confidence_for_url(link))
        merged.append(old)
        retained += 1

    print(f"[INFO] Retained {retained} still-valid records not rediscovered in this run")
    return deduplicate(merged)


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
    confidence_rank = {"high": 0, "medium": 1, "low": 2}.get(str(item.get("source_confidence", "medium")).lower(), 1)
    deadline = parse_date_value(item.get("deadline")) or date.max
    first_seen = parse_iso_datetime(item.get("first_seen"))
    newest_rank = -(first_seen.timestamp() if first_seen else 0)
    return (candidate_priority(item), confidence_rank, deadline, newest_rank, item.get("title", "").lower())


def select_final_records(records: list[dict], limit: int) -> list[dict]:
    buckets = {category: [] for category in CATEGORY_ORDER}
    for item in records:
        category = fallback_category(item)
        item = dict(item)
        item["category"] = category
        item["subcategory"] = item.get("subcategory") or fallback_subcategory(item, category)
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
        item
        for category in CATEGORY_ORDER
        for item in buckets[category]
        if item["link"] not in selected_links
    ]
    leftovers.sort(key=record_sort_key)
    for item in leftovers:
        if len(selected) >= limit:
            break
        selected.append(item)
        selected_links.add(item["link"])

    # The frontend's "Recently discovered" control uses first_seen, so file order can focus
    # on quality and category diversity.
    return selected[:limit]


def validate_final(records: list[dict]) -> None:
    allowed = set(CATEGORY_ORDER)
    for index, item in enumerate(records):
        required = {"title", "category", "subcategory", "description", "link", "source", "first_seen", "last_seen"}
        missing = required - set(item)
        if missing:
            raise RuntimeError(f"Record {index} is missing fields: {sorted(missing)}")
        if item["category"] not in allowed:
            raise RuntimeError(f"Record {index} has invalid category: {item['category']}")
        if not allowed_url(clean_url(item["link"])):
            raise RuntimeError(f"Record {index} has invalid link")


def main() -> None:
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from GitHub Actions secrets")

    client = genai.Client(api_key=API_KEY)

    raw: list[dict] = []
    raw.extend(discover_with_google_search(client))
    raw.extend(fetch_rss())
    raw.extend(fetch_grants_gov())
    raw.extend(fetch_sbir())
    raw = [item for item in deduplicate(raw) if is_current_or_open(item)]

    candidate_counts = Counter(fallback_category(item) for item in raw)
    print(f"[INFO] Candidate categories: {dict(candidate_counts)}")
    print(f"[INFO] Total unique current/open candidates: {len(raw)}")

    curated = curate_with_gemini(client, raw)
    merged = merge_with_existing(curated)
    final = select_final_records(merged, MAX_FINAL_ITEMS)
    validate_final(final)

    final_counts = Counter(item["category"] for item in final)
    print(f"[INFO] Final categories: {dict(final_counts)}")
    print(f"[INFO] Final curated records: {len(final)} / {MAX_FINAL_ITEMS}")

    if len(final) < MIN_GOOD_RUN_ITEMS:
        existing = load_existing()
        if existing:
            print(f"[WARN] Only {len(final)} records produced; keeping existing data.json with {len(existing)} records")
            return
        raise RuntimeError(f"Only {len(final)} records were produced and no existing data is available")

    DATA_FILE.write_text(
        json.dumps(final, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"[OK] Wrote {len(final)} autonomous Access records")


if __name__ == "__main__":
    main()
