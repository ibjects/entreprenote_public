from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
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
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()

MAX_FINAL_ITEMS = 100
MIN_GOOD_RUN_ITEMS = 10
REQUEST_TIMEOUT = 25
CURRENT_YEAR = datetime.now(timezone.utc).year

# Opportunity sources, not startup-news feeds.
RSS_FEEDS = [
    ("Opportunity Desk", "https://opportunitydesk.org/feed/", 20),
    ("Youth Opportunities", "https://www.youthop.com/feed/", 20),
    ("fundsforNGOs", "https://www2.fundsforngos.org/feed/", 18),
    ("fundsforNGOs Listings", "https://www2.fundsforngos.org/category/listing/feed/", 18),
    ("AlphaGamma Opportunities", "https://www.alphagamma.eu/category/opportunities/feed/", 18),
]

# This is the AI discovery knowledge base. The model should search these sources first,
# then use other official program/application pages when relevant.
ACCELERATOR_KNOWLEDGE_BASE = [
    ("F6S Programs", "https://www.f6s.com/programs"),
    ("Techstars Accelerators", "https://www.techstars.com/accelerators"),
    ("MassChallenge", "https://masschallenge.org/"),
    ("Founder Institute Enrolling", "https://fi.co/enrolling"),
    ("Antler Apply", "https://www.antler.co/apply"),
    ("Antler Cohort Dates", "https://www.antler.co/cohort-start-dates"),
    ("Startupbootcamp", "https://startupbootcamp.org/"),
    ("Seedstars Programs", "https://www.seedstars.com/community/entrepreneurs/programs/"),
    ("Y Combinator Apply", "https://www.ycombinator.com/apply"),
    ("500 Global Programs", "https://500.co/"),
    ("Plug and Play", "https://www.plugandplaytechcenter.com/"),
    ("Google for Startups", "https://startup.google.com/programs/"),
    ("AWS Startups", "https://aws.amazon.com/startups/"),
    ("Microsoft for Startups", "https://www.microsoft.com/startups"),
]

SEARCH_PROMPTS = [
    "Find currently open startup accelerators, incubators, founder fellowships, pitch events, hackathons, and startup competitions worldwide. Return official application pages only.",

    "Find currently open startup grants, non-dilutive funding, prize funding, angel application programs, and crowdfunding opportunities. Return official application pages only.",

    "Find free founder mentorship, startup advisor office hours, legal clinics, finance advisors, product mentors, and growth mentoring programs. Return official booking or application pages only.",

    "Find free or low-cost entrepreneurship courses, founder workshops, bootcamps, playbooks, templates, and practical startup learning programs. Return official pages only.",

    "Find useful startup tools, free startup credits, founder discounts, cloud credits, legal services, accounting services, marketing tools, development tools, and startup service providers. Return official pages only.",
]

ACTION_WORDS = (
    "apply",
    "applications open",
    "deadline",
    "grant",
    "funding",
    "accelerator",
    "incubator",
    "venture studio",
    "residency",
    "hackathon",
    "competition",
    "challenge",
    "prize",
    "award",
    "fellowship",
    "bootcamp",
    "pitch",
    "demo day",
    "mentor",
    "mentorship",
    "advisor",
    "office hours",
    "workshop",
    "webinar",
    "course",
    "program",
    "register",
    "open call",
    "tool",
    "software",
    "platform",
    "startup credits",
    "cloud credits",
    "discount",
    "service",
    "template",
    "playbook",
    "guide",
    "sign up",
    "get started",
    "book",
    "download",
)

CLOSED_WORDS = (
    "applications closed", "application closed", "closed for applications",
    "deadline passed", "expired", "no longer accepting", "submissions closed",
)

OPEN_STATUS_WORDS = (
    "open", "accepting", "rolling", "ongoing", "active", "applications open",
)

BLOCKED_DOMAINS = {
    "producthunt.com",
    "www.producthunt.com",
    "techcrunch.com",
    "news.ycombinator.com",
}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
)

session = requests.Session()
session.headers.update({
    "User-Agent": "AccessByEntreprenote/2.0 (+https://entreprenote.com/access/)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
})


def clean_text(value: object, limit: int = 900) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def clean_url(value: object) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "ref", "source"}
        ]
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query, doseq=True),
            "",
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

    raw = clean_text(value, 80)
    if not raw:
        return None

    raw = re.sub(r"\b(?:deadline|closing|close date|opening|open date|published)\s*:\s*", "", raw, flags=re.I)
    raw = raw.strip(" .")

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    month_date = re.search(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},\s+\d{4}\b",
        raw,
        flags=re.I,
    )
    if month_date:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(month_date.group(0), fmt).date()
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
    match = re.search(
        rf"(?:{label_pattern})\s*:?\s*({date_pattern})",
        text,
        flags=re.I,
    )
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
    summary = clean_text(item.get("summary") or item.get("description"), 1200)

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
    today = datetime.now(timezone.utc).date()
    text = clean_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('description', '')} {item.get('status', '')}",
        1800,
    ).lower()
    status = clean_text(item.get("status"), 80).lower()

    if any(word in text for word in CLOSED_WORDS):
        return False

    opening, deadline, published = candidate_dates(item)

    # A real, unexpired deadline always wins, even if the program opened earlier.
    if deadline:
        return deadline >= today

    # Rolling/open programs can remain available without a fixed deadline.
    if any(word in status for word in OPEN_STATUS_WORDS) or any(
        phrase in text for phrase in ("rolling applications", "rolling intake", "applications are open")
    ):
        return True

    # Forecasted/posted records with no deadline must be from this year.
    if status in {"posted", "forecasted"}:
        anchor = opening or published
        return bool(anchor and anchor.year >= CURRENT_YEAR)

    # For undated RSS/search records, only retain records opened or published this year.
    anchor = opening or published
    return bool(anchor and anchor.year >= CURRENT_YEAR)


def iso_or_empty(value: date | None) -> str:
    return value.isoformat() if value else ""


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
                summary = clean_text(
                    entry.get("summary") or entry.get("description") or "",
                    900,
                )
                if not title or not allowed_url(link):
                    continue
                if not looks_actionable(title, summary):
                    continue

                published = rss_published_date(entry)
                candidate = {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                    "method": "rss",
                    "published_at": iso_or_empty(published),
                    "status": "open" if "applications open" in f"{title} {summary}".lower() else "",
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
                json={
                    "keyword": keyword,
                    "oppStatuses": "posted|forecasted",
                    "rows": 15,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            hits = ((payload.get("data") or {}).get("oppHits") or [])

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
                        f"Agency: {hit.get('agencyName', '')}. "
                        f"Opening: {hit.get('openDate', '')}. "
                        f"Closing: {hit.get('closeDate', '')}. "
                        f"Status: {status}.",
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
                }

                if is_current_or_open(candidate):
                    records.append(candidate)

        except Exception as exc:
            print(f"[WARN] Grants.gov '{keyword}' failed: {exc}")

    print(f"[OK] Grants.gov: {len(records)} current/open grant records")
    return records


def extract_json_array(text: str) -> list[dict]:
    value = (text or "").replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        start = value.find("[")
        end = value.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            parsed = json.loads(value[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []


def knowledge_base_text() -> str:
    return "\n".join(f"- {name}: {url}" for name, url in ACCELERATOR_KNOWLEDGE_BASE)


def discover_with_google_search(client: genai.Client) -> list[dict]:
    records: list[dict] = []
    today = datetime.now(timezone.utc).date().isoformat()
    search_tool = types.Tool(google_search=types.GoogleSearch())
    sources = knowledge_base_text()

    for search_prompt in SEARCH_PROMPTS:
        prompt = f"""
Today is {today}.
{search_prompt}

Known source pages to search first:
{sources}

Rules:
- Verify that applications are currently open, rolling, or have a deadline on or after today.
- Exclude anything whose application deadline has passed.
- If no deadline exists, keep it only when the page clearly says applications are open/rolling,
  or the program was opened/published in {CURRENT_YEAR}.
- Exclude news articles, generic directory articles, closed cohorts, jobs, ordinary scholarships,
  and product-launch announcement articles.
- Keep a tool, service, advisor resource, or learning resource when a founder can currently
  sign up, start using it, book it, claim credits, download it, or access it.
- For an evergreen tool, service, advisor resource, or learning resource with no deadline,
  set status to "open" only when it is currently accessible.
- Prefer the official organizer application page. F6S application pages are allowed when they
  clearly show an active apply deadline.

Return ONLY a JSON array with 8 to 15 objects. Each object must contain exactly:
- title
- summary
- link
- source
- opening_date: YYYY-MM-DD or empty string
- deadline: YYYY-MM-DD or empty string
- status: open, rolling, posted, forecasted, or empty string
- suggested_category: Funding, Event, Advisor, Learning, or Tool
- suggested_subcategory: Accelerator, Incubator, Venture Studio, Founder Residency,
  Fellowship, Hackathon, Competition / Challenge, Grant, Mentorship, Workshop, or another concise type

Do not use markdown.
"""
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(tools=[search_tool]),
            )
            found = extract_json_array(response.text or "")
            accepted = 0

            for item in found:
                title = clean_text(item.get("title"), 180)
                summary = clean_text(item.get("summary"), 900)
                link = clean_url(item.get("link"))
                if not title or not allowed_url(link):
                    continue
                if not looks_actionable(title, summary):
                    continue

                candidate = {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": clean_text(item.get("source"), 100) or "Web discovery",
                    "method": "gemini-search",
                    "opening_date": iso_or_empty(parse_date_value(item.get("opening_date"))),
                    "deadline": iso_or_empty(parse_date_value(item.get("deadline"))),
                    "status": clean_text(item.get("status"), 40).lower(),
                    "suggested_category": clean_text(item.get("suggested_category"), 30),
                    "suggested_subcategory": clean_text(item.get("suggested_subcategory"), 50),
                }

                if not is_current_or_open(candidate):
                    continue

                records.append(candidate)
                accepted += 1

            print(f"[OK] Gemini Search: {accepted} current/open records")
        except Exception as exc:
            print(f"[WARN] Gemini Search failed: {exc}")

    return records


def deduplicate(records: list[dict]) -> list[dict]:
    output: list[dict] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()

    for item in records:
        link = clean_url(item.get("link"))
        title = clean_text(item.get("title"), 180)
        title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
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


def fallback_category(item: dict) -> str:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    suggested = clean_text(item.get("suggested_category"), 30).title()
    if suggested in {"Funding", "Event", "Advisor", "Learning", "Tool"}:
        return suggested
    if any(word in text for word in ("grant", "funding", "fund", "prize", "award", "investment")):
        return "Funding"
    if any(word in text for word in ("mentor", "advisor", "office hours", "legal clinic")):
        return "Advisor"
    if any(word in text for word in ("course", "workshop", "webinar", "training", "playbook")):
        return "Learning"
    if any(word in text for word in (
        "accelerator", "incubator", "venture studio", "residency", "hackathon",
        "competition", "challenge", "fellowship", "pitch", "event", "program",
    )):
        return "Event"
    return "Tool"

CATEGORY_ORDER = (
    "Event",
    "Advisor",
    "Learning",
    "Tool",
    "Funding",
)


def build_balanced_candidate_pool(
    records: list[dict],
    limit: int = 70,
) -> list[dict]:
    buckets: dict[str, list[dict]] = {
        category: [] for category in CATEGORY_ORDER
    }

    for item in records:
        category = fallback_category(item)
        buckets[category].append(item)

    balanced: list[dict] = []

    while len(balanced) < limit:
        added = False

        for category in CATEGORY_ORDER:
            if buckets[category]:
                balanced.append(buckets[category].pop(0))
                added = True

                if len(balanced) >= limit:
                    break

        if not added:
            break

    return balanced


def balance_final_records(
    records: list[dict],
    limit: int,
) -> list[dict]:
    buckets: dict[str, list[dict]] = {
        category: [] for category in CATEGORY_ORDER
    }

    for item in records:
        category = str(item.get("category", "")).title()

        if category in buckets:
            buckets[category].append(item)

    balanced: list[dict] = []

    while len(balanced) < limit:
        added = False

        for category in CATEGORY_ORDER:
            if buckets[category]:
                balanced.append(buckets[category].pop(0))
                added = True

                if len(balanced) >= limit:
                    break

        if not added:
            break

    return balanced

def fallback_subcategory(item: dict, category: str) -> str:
    suggested = clean_text(item.get("suggested_subcategory"), 50)
    if suggested:
        return suggested

    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    rules = [
        ("accelerator", "Accelerator"),
        ("incubator", "Incubator"),
        ("venture studio", "Venture Studio"),
        ("residency", "Founder Residency"),
        ("hackathon", "Hackathon"),
        ("fellowship", "Fellowship"),
        ("competition", "Competition / Challenge"),
        ("challenge", "Competition / Challenge"),
        ("mentor", "Mentorship"),
        ("workshop", "Workshop"),
        ("webinar", "Webinar"),
        ("course", "Course"),
        ("grant", "Grant"),
    ]
    for keyword, label in rules:
        if keyword in text:
            return label

    return {
        "Funding": "Grant",
        "Event": "Program",
        "Advisor": "Mentorship",
        "Learning": "Learning Program",
        "Tool": "Startup Tool",
    }[category]


def accelerator_priority(item: dict) -> int:
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('suggested_subcategory', '')}".lower()
    if "accelerator" in text or "incubator" in text:
        return 0
    if "venture studio" in text or "residency" in text or "fellowship" in text:
        return 1
    if "hackathon" in text or "competition" in text or "challenge" in text:
        return 2
    if "grant" in text or "funding" in text:
        return 3
    return 4


def curate_with_gemini(
    client: genai.Client,
    raw_records: list[dict],
) -> list[dict]:
    candidate_pool = build_balanced_candidate_pool(
        raw_records,
        70,
    )

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
            "suggested_category": item.get(
                "suggested_category",
                "",
            ),
            "suggested_subcategory": item.get(
                "suggested_subcategory",
                "",
            ),
        })

    prompt = f"""
You are the autonomous curator for Access by Entreprenote.
Today is {datetime.now(timezone.utc).date().isoformat()}.
The platform is not a startup-news feed.

Keep only actionable founder resources where someone can currently:
- apply;
- register;
- join;
- book an advisor;
- enter a competition;
- receive funding;
- start a course;
- download a useful resource;
- claim startup credits;
- or begin using a useful founder tool or service.

Date rules:
- Keep an item when its deadline is today or later.
- Keep rolling or currently open programs.
- An evergreen tool, service, advisor resource, or learning resource may
  have no deadline when it is currently accessible.
- Reject closed and expired programs.

Create a balanced directory across these five categories:

1. Event:
   Accelerators, incubators, fellowships, founder programs, hackathons,
   competitions, conferences, webinars and pitch events.

2. Funding:
   Grants, non-dilutive funding, prize funding, crowdfunding and currently
   open investor or funding application channels.

3. Advisor:
   Mentorship, founder office hours, legal advice, finance advice,
   product advice and growth advice.

4. Learning:
   Courses, workshops, templates, playbooks, bootcamps and practical
   founder education.

5. Tool:
   Startup software, cloud credits, founder discounts, legal services,
   accounting services, development services, marketing tools and
   other practical startup resources.

Do not return only one category.

When enough valid candidates exist:
- include records from all five categories;
- aim for at least five records from each category;
- do not fill one category with weak records merely to reach a quota.

Reject:
- ordinary startup news;
- funding-round announcements;
- opinion articles;
- product-launch announcement articles;
- expired programs;
- jobs;
- irrelevant scholarships;
- inaccessible resources.

Return 30 to 60 records when enough valid candidates exist.

Return only a JSON array. Every object must contain exactly:
- source_index: integer matching the supplied index
- title: concise factual title
- category: one of Funding, Event, Advisor, Learning, Tool
- subcategory: concise resource type
- description: factual founder-focused summary, maximum 220 characters
- keep: boolean

Never create or alter links. The application will reuse the original
link using source_index.

Candidates:
{json.dumps(compact, ensure_ascii=False)}
"""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=14000,
            ),
        )

        decisions = extract_json_array(
            response.text or "",
        )
    except Exception as exc:
        print(
            f"[WARN] Gemini curation failed: {exc}",
        )
        decisions = []

    final: list[dict] = []
    used_indexes: set[int] = set()

    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )

    for decision in decisions:
        try:
            index = int(
                decision.get("source_index"),
            )
        except (TypeError, ValueError):
            continue

        if (
            index < 0
            or index >= len(candidate_pool)
            or index in used_indexes
        ):
            continue

        if decision.get("keep") is False:
            continue

        # The source_index refers to candidate_pool.
        raw = candidate_pool[index]

        if not is_current_or_open(raw):
            continue

        category = clean_text(
            decision.get("category"),
            30,
        ).title()

        if category not in {
            "Funding",
            "Event",
            "Advisor",
            "Learning",
            "Tool",
        }:
            category = fallback_category(raw)

        opening, deadline, _ = candidate_dates(raw)

        final.append({
            "title": clean_text(
                decision.get("title")
                or raw.get("title"),
                180,
            ),
            "category": category,
            "subcategory": (
                clean_text(
                    decision.get("subcategory"),
                    50,
                )
                or fallback_subcategory(
                    raw,
                    category,
                )
            ),
            "description": clean_text(
                decision.get("description")
                or raw.get("summary"),
                240,
            ),
            "link": raw["link"],
            "source": raw.get("source", ""),
            "opening_date": iso_or_empty(opening),
            "deadline": iso_or_empty(deadline),
            "status": clean_text(
                raw.get("status"),
                40,
            ),
            "updated_at": now,
        })

        used_indexes.add(index)

    if len(final) < 30:
        print(
            f"[WARN] Gemini returned only {len(final)} "
            "records; backfilling balanced candidates",
        )

        existing_links = {
            item["link"] for item in final
        }

        # Backfill from the balanced pool, not prioritized.
        for raw in candidate_pool:
            if raw["link"] in existing_links:
                continue

            if not looks_actionable(
                raw.get("title", ""),
                raw.get("summary", ""),
            ):
                continue

            if not is_current_or_open(raw):
                continue

            category = fallback_category(raw)
            opening, deadline, _ = candidate_dates(raw)

            final.append({
                "title": clean_text(
                    raw.get("title"),
                    180,
                ),
                "category": category,
                "subcategory": fallback_subcategory(
                    raw,
                    category,
                ),
                "description": clean_text(
                    raw.get("summary"),
                    240,
                ),
                "link": raw["link"],
                "source": raw.get("source", ""),
                "opening_date": iso_or_empty(opening),
                "deadline": iso_or_empty(deadline),
                "status": clean_text(
                    raw.get("status"),
                    40,
                ),
                "updated_at": now,
            })

            existing_links.add(raw["link"])

            if len(final) >= 50:
                break

    deduplicated = deduplicate(final)

    return balance_final_records(
        deduplicated,
        MAX_FINAL_ITEMS,
    )

def load_existing() -> list[dict]:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def preserve_history_and_sort(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing_by_link = {
        clean_url(item.get("link") or item.get("url")): item
        for item in load_existing()
        if clean_url(item.get("link") or item.get("url"))
    }

    for item in items:
        link = clean_url(item.get("link"))
        previous = existing_by_link.get(link, {})
        item["first_seen"] = previous.get("first_seen") or previous.get("updated_at") or now
        item["last_seen"] = now
        item["updated_at"] = now

    items.sort(
        key=lambda item: (
            accelerator_priority(item),
            item.get("deadline") or "9999-12-31",
            item.get("first_seen") or "",
        )
    )
    return items[:MAX_FINAL_ITEMS]


def main() -> None:
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from GitHub Actions secrets")

    client = genai.Client(api_key=API_KEY)

    # Discover accelerator/incubator programs first so they cannot be pushed out by grants.
    raw: list[dict] = []
    raw.extend(discover_with_google_search(client))
    raw.extend(fetch_rss())
    raw.extend(fetch_grants_gov())
    raw = [item for item in deduplicate(raw) if is_current_or_open(item)]

    category_counts = {
        category: sum(
            1
            for item in raw
            if fallback_category(item) == category
        )
        for category in CATEGORY_ORDER
    }

    print(
        "[INFO] Candidate categories:",
        category_counts,
    )

    print(f"[INFO] Total unique current/open candidates: {len(raw)}")

    curated = curate_with_gemini(client, raw)
    curated = preserve_history_and_sort(curated)
    final_category_counts = {
        category: sum(
            1
            for item in curated
            if item.get("category") == category
        )
        for category in CATEGORY_ORDER
    }

    print(
        "[INFO] Final categories:",
        final_category_counts,
    )   
    print(f"[INFO] Final curated records: {len(curated)}")

    if len(curated) < MIN_GOOD_RUN_ITEMS:
        existing = load_existing()
        if existing:
            print(
                f"[WARN] Only {len(curated)} records produced. "
                f"Keeping existing data.json with {len(existing)} records."
            )
            return
        raise RuntimeError(
            f"Only {len(curated)} records were produced and no existing data is available"
        )

    DATA_FILE.write_text(
        json.dumps(curated, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"[OK] Replaced data.json with {len(curated)} curated records")


if __name__ == "__main__":
    main()
