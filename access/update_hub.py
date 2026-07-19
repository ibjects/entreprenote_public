import os
import json
import feedparser
from google import genai

# 1. Setup New Google GenAI Client
API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY)

# 2. Verified RSS Feeds for Startup Ecosystem
FEEDS = [
    "https://www.producthunt.com/feed",
    "https://techcrunch.com/startups/feed/",
    "https://techcrunch.com/funding/feed/",
    "https://entrepreneur.economictimes.indiatimes.com/rss/startups",
    "https://news.ycombinator.com/rss"
]

def scrape_feeds():
    raw_content = ""
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            # Grab top 8 items from each feed to stay within token limits
            for entry in feed.entries[:8]: 
                summary = entry.get('summary', '')[:300] # truncate long summaries
                raw_content += f"Title: {entry.title}\nLink: {entry.link}\nSummary: {summary}\n\n"
        except Exception as e:
            print(f"Failed to parse {url}: {e}")
    return raw_content

def process_with_llm(raw_text):
    prompt = f"""
    You are an expert startup resource curator for "Access by Entreprenote".
    Review these raw RSS feed extracts and extract the actual entrepreneurial opportunities, new startup tools, funding announcements, or learning resources. 
    Ignore generic news, opinion pieces, or unrelated content.

    Format the output STRICTLY as a valid JSON array of objects. Do not use markdown backticks (```json). Just output the raw array.
    Each object must have these exact keys:
    - "title": (string) A catchy, clear title.
    - "category": (string) MUST be one of: "Funding", "Event", "Learning", "Advisor", or "Tool".
    - "description": (string) 1-2 sentences explaining why a founder should care.
    - "link": (string) the exact URL provided.

    Raw Data:
    {raw_text}
    """
    
    try:
        # Swapped to 3.5-flash for stable free-tier quota limits
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt,
        )
        
        # Clean up any potential markdown formatting the LLM might add
        clean_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        print("Failed to generate or parse JSON from LLM. Error:", e)
        return []

if __name__ == "__main__":
    print("Fetching RSS feeds...")
    raw_data = scrape_feeds()
    
    print("Processing via Gemini 3.5 Flash...")
    curated_data = process_with_llm(raw_data)
    
    if curated_data:
        # Load existing data to merge, keeping the most recent 50 items
        existing_data = []
        if os.path.exists('data.json'):
            try:
                with open('data.json', 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            except:
                pass
        
        # Prevent duplicates based on link
        existing_links = {item['link'] for item in existing_data}
        new_items = [item for item in curated_data if item['link'] not in existing_links]
        
        combined_data = new_items + existing_data
        final_data = combined_data[:50] # Keep platform fast by limiting to newest 50
        
        with open('data.json', 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=4)
        print(f"Successfully added {len(new_items)} new resources!")
    else:
        print("No valid data generated.")