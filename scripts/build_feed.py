import json
import datetime as dt
from urllib.request import Request, urlopen

# Build settings
BUILD_HOUR_UTC = 7
WINDOW_HOURS = 4

# Reddit traction proxy: score + 2*comments
REDDIT_UA = "daily-growth-feed/1.0 (personal use)"

def fetch_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": REDDIT_UA})
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))

def now_utc() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)

def build_times() -> tuple[dt.datetime, dt.datetime]:
    n = now_utc()
    build = n.replace(hour=BUILD_HOUR_UTC, minute=0, second=0)
    if n < build:
        build -= dt.timedelta(days=1)
    window_start = build - dt.timedelta(hours=WINDOW_HOURS)
    return build, window_start

def reddit_pull(url: str, source_name: str, window_start_ts: int) -> list[dict]:
    data = fetch_json(url)
    out = []
    children = data.get("data", {}).get("children", [])
    for ch in children:
        d = ch.get("data", {})
        created = int(d.get("created_utc", 0))
        if created < window_start_ts:
            continue
        title = (d.get("title") or "").strip()
        link = d.get("url") or ""
        score = int(d.get("score") or 0)
        comments = int(d.get("num_comments") or 0)
        traction = score + 2 * comments
        if title and link:
            out.append({
                "title": title,
                "url": link,
                "source": source_name,
                "traction": traction,
                "published": dt.datetime.utcfromtimestamp(created).isoformat() + "Z",
            })
    return out

def hn_pull(min_ts: int) -> list[dict]:
    # Hacker News traction proxy via Algolia API
    url = f"https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=100&numericFilters=created_at_i>{min_ts}"
    data = fetch_json(url)
    hits = data.get("hits", [])
    out = []
    for h in hits:
        created_i = int(h.get("created_at_i") or 0)
        if created_i < min_ts:
            continue
        title = (h.get("title") or "").strip()
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        points = int(h.get("points") or 0)
        comments = int(h.get("num_comments") or 0)
        traction = points + 2 * comments
        if title and link:
            out.append({
                "title": title,
                "url": link,
                "source": "Hacker News",
                "traction": traction,
                "published": dt.datetime.utcfromtimestamp(created_i).isoformat() + "Z",
            })
    return out

def dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        key = (it.get("title", "")[:160].lower(), it.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def pick_top(items: list[dict], n: int) -> list[dict]:
    return sorted(items, key=lambda x: x.get("traction", 0), reverse=True)[:n]

def main():
    build_time, window_start = build_times()
    window_start_ts = int(window_start.replace(tzinfo=dt.timezone.utc).timestamp())

    # Pools
    finance_pool = []
    world_pool = []
    auto_pool = []
    portugal_pool = []
    tech_pool = []

    # Finance (Reddit)
    for name, url in [
        ("r/stocks", "https://www.reddit.com/r/stocks/top.json?t=hour&limit=100"),
        ("r/investing", "https://www.reddit.com/r/investing/top.json?t=hour&limit=100"),
        ("r/finance", "https://www.reddit.com/r/finance/top.json?t=hour&limit=100"),
    ]:
        try:
            finance_pool += reddit_pull(url, name, window_start_ts)
        except Exception:
            pass

    # World (Reddit)
    for name, url in [
        ("r/worldnews", "https://www.reddit.com/r/worldnews/top.json?t=hour&limit=100"),
        ("r/geopolitics", "https://www.reddit.com/r/geopolitics/top.json?t=hour&limit=100"),
    ]:
        try:
            world_pool += reddit_pull(url, name, window_start_ts)
        except Exception:
            pass

    # Auto/Moto (Reddit)
    for name, url in [
        ("r/cars", "https://www.reddit.com/r/cars/top.json?t=hour&limit=100"),
        ("r/motorcycles", "https://www.reddit.com/r/motorcycles/top.json?t=hour&limit=100"),
        ("r/autos", "https://www.reddit.com/r/autos/top.json?t=hour&limit=100"),
    ]:
        try:
            auto_pool += reddit_pull(url, name, window_start_ts)
        except Exception:
            pass

    # Portugal (Reddit traction)
    for name, url in [
        ("r/portugal", "https://www.reddit.com/r/portugal/top.json?t=hour&limit=100"),
    ]:
        try:
            portugal_pool += reddit_pull(url, name, window_start_ts)
        except Exception:
            pass

    # Tech (HN Algolia)
    try:
        tech_pool += hn_pull(window_start_ts)
    except Exception:
        pass

    # Dedupe + pick category tops
    finance_top = pick_top(dedupe(finance_pool), 5)
    world_top = pick_top(dedupe(world_pool), 5)
    tech_top = pick_top(dedupe(tech_pool), 10)
    auto_top = pick_top(dedupe(auto_pool), 5)
    portugal_top = pick_top(dedupe(portugal_pool), 5)

    sections = [
        {"name": "Finance", "items": finance_top},
        {"name": "World news", "items": world_top},
        {"name": "Tech", "items": tech_top},
        {"name": "Automotive / Motorcycle", "items": auto_top},
        {"name": "Portugal main news", "items": portugal_top},
    ]

    built_at_utc = build_time.isoformat() + "Z"
    next_build_utc = (build_time + dt.timedelta(days=1)).isoformat() + "Z"

    out = {
        "title": "Daily Growth Feed",
        "built_at_utc": built_at_utc,
        "next_build_utc": next_build_utc,
        "window_hours": WINDOW_HOURS,
        "sections": sections,
        "sources": [
            "Hacker News (Algolia API)",
            "Reddit public JSON endpoints",
        ],
    }

    with open("content.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("OK: wrote content.json")

if __name__ == "__main__":
    main()