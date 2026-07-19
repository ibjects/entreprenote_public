from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
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

MAX_FINAL_ITEMS = 80
MIN_GOOD_RUN_ITEMS = 10
REQUEST_TIMEOUT = 25

# These are opportunity sources, not startup-news feeds.
RSS_FEEDS = [
    ("Opportunity Desk", "https://opportunitydesk.org/feed/", 18),
    ("Youth Opportunities", "https://www.youthop.com/feed/", 18),
    ("fundsforNGOs", "https://www2.fundsforngos.org/feed/", 15),
    ("fundsforNGOs Listings", "https://www2.fundsforngos.org/category/listing/feed/", 15),
    ("AlphaGamma Opportunities", "https://www.alphagamma.eu/category/opportunities/feed/", 15),
]

SEARCH_PROMPTS = [
    "Find currently open startup accelerators, incubators, founder fellowships, grants, and non-dilutive funding programs worldwide. Return official application pages only.",
    "Find currently open hackathons, startup competitions, innovation challenges, pitch competitions, and prize-money events worldwide. Return official registration or application pages only.",
    "Find free or low-cost founder mentorship, startup advisor office hours, entrepreneurship workshops, bootcamps, and practical founder learning programs that are currently available. Return official pages only.",
]

ACTION_WORDS = (
    "apply", "applications open", "deadline", "grant", "funding", "accelerator",
    "incubator", "hackathon", "competition", "challenge", "prize", "award",
    "fellowship", "bootcamp", "pitch", "demo day", "mentor", "mentorship",
    "office hours", "workshop", "webinar", "course", "program", "register",
)

BLOCKED_DOMAINS = {
    "producthunt.com",
    "www.producthunt.com",
    "techcrunch.com",
    "news.ycombinator.com",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "AccessByEntreprenote/1.0 (+https://entreprenote.com/access/)",
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

                records.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                    "method": "rss",
                })
                added += 1

            print(f"[OK] {source}: {added} actionable RSS records")
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
                    "rows": 10,
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

                records.append({
                    "title": title,
                    "summary": clean_text(
                        f"Agency: {hit.get('agencyName', '')}. "
                        f"Opening: {hit.get('openDate', '')}. "
                        f"Closing: {hit.get('closeDate', '')}. "
                        f"Status: {hit.get('oppStatus', '')}.",
                        900,
                    ),
                    "link": f"https://www.grants.gov/search-results-detail/{opportunity_id}",
                    "source": "Grants.gov",
                    "method": "api",
                })

        except Exception as exc:
            print(f"[WARN] Grants.gov '{keyword}' failed: {exc}")

    print(f"[OK] Grants.gov: {len(records)} raw grant records")
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


def discover_with_google_search(client: genai.Client) -> list[dict]:
    records: list[dict] = []
    today = datetime.now(timezone.utc).date().isoformat()
    search_tool = types.Tool(google_search=types.GoogleSearch())

    for search_prompt in SEARCH_PROMPTS:
        prompt = f"""
Today is {today}.
{search_prompt}

Exclude news articles, expired opportunities, jobs, ordinary scholarships, product launches,
and pages that do not let a founder apply, register, join, book, compete, or learn.

Return ONLY a JSON array with 6 to 10 objects. Each object must contain exactly:
- title
- summary
- link
- source
- suggested_category

suggested_category must be one of Funding, Event, Advisor, Learning, Tool.
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

                records.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": clean_text(item.get("source"), 100) or "Web discovery",
                    "method": "gemini-search",
                    "suggested_category": clean_text(item.get("suggested_category"), 30),
                })
                accepted += 1

            print(f"[OK] Gemini Search: {accepted} accepted records")
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
    if any(word in text for word in ("accelerator", "incubator", "hackathon", "competition", "challenge", "fellowship", "pitch", "event", "program")):
        return "Event"
    return "Tool"


def curate_with_gemini(client: genai.Client, raw_records: list[dict]) -> list[dict]:
    compact = []
    for index, item in enumerate(raw_records[:70]):
        compact.append({
            "index": index,
            "title": item.get("title", ""),
            "summary": item.get("summary", "")[:700],
            "link": item.get("link", ""),
            "source": item.get("source", ""),
            "suggested_category": item.get("suggested_category", ""),
        })

    prompt = f"""
You are the autonomous curator for Access by Entreprenote.
The platform is NOT a startup-news feed.

Keep only actionable founder opportunities where someone can currently apply, register, join,
book an advisor, enter a competition, receive funding, or start a practical learning resource.

Strongly prioritize, in this order:
1. Grants, non-dilutive funding, prize money and funding application channels
2. Accelerators, incubators and founder fellowships
3. Hackathons, startup competitions, innovation challenges and pitch events
4. Founder events, bootcamps and workshops
5. Mentorship, advisors and office hours
6. High-value founder learning resources

Reject:
- product launches
- generic software launches
- startup news
- funding-round announcements
- opinion articles
- ordinary blog posts
- expired programs
- jobs

Return 20 to 40 records when enough valid candidates exist. Do not arbitrarily return only five.
Return ONLY a JSON array. Every object must contain exactly:
- source_index: integer matching the supplied index
- title: concise factual title
- category: one of Funding, Event, Advisor, Learning, Tool
- description: factual founder-focused summary, maximum 220 characters
- keep: boolean

Never create or alter links. The application will reuse the original link by source_index.

Candidates:
{json.dumps(compact, ensure_ascii=False)}
"""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=10000,
            ),
        )
        decisions = extract_json_array(response.text or "")
    except Exception as exc:
        print(f"[WARN] Gemini curation failed: {exc}")
        decisions = []

    final: list[dict] = []
    used_indexes: set[int] = set()

    for decision in decisions:
        try:
            index = int(decision.get("source_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(compact) or index in used_indexes:
            continue
        if decision.get("keep") is False:
            continue

        raw = raw_records[index]
        category = clean_text(decision.get("category"), 30).title()
        if category not in {"Funding", "Event", "Advisor", "Learning", "Tool"}:
            category = fallback_category(raw)

        final.append({
            "title": clean_text(decision.get("title") or raw.get("title"), 180),
            "category": category,
            "description": clean_text(decision.get("description") or raw.get("summary"), 240),
            "link": raw["link"],
            "source": raw.get("source", ""),
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        })
        used_indexes.add(index)

    # Critical safety net: if Gemini returns too few, backfill strong actionable records.
    if len(final) < 20:
        print(f"[WARN] Gemini returned only {len(final)} records; backfilling deterministically")
        existing_links = {item["link"] for item in final}
        for raw in raw_records:
            if raw["link"] in existing_links:
                continue
            if not looks_actionable(raw.get("title", ""), raw.get("summary", "")):
                continue
            final.append({
                "title": clean_text(raw.get("title"), 180),
                "category": fallback_category(raw),
                "description": clean_text(raw.get("summary"), 240),
                "link": raw["link"],
                "source": raw.get("source", ""),
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            })
            existing_links.add(raw["link"])
            if len(final) >= 30:
                break

    return deduplicate(final)[:MAX_FINAL_ITEMS]


def load_existing() -> list[dict]:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def main() -> None:
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from GitHub Actions secrets")

    client = genai.Client(api_key=API_KEY)

    raw = []
    raw.extend(fetch_rss())
    raw.extend(fetch_grants_gov())
    raw.extend(discover_with_google_search(client))
    raw = deduplicate(raw)

    print(f"[INFO] Total unique actionable candidates: {len(raw)}")

    curated = curate_with_gemini(client, raw)
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
        json.dumps(curated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] Replaced data.json with {len(curated)} curated records")


if __name__ == "__main__":
    main()
