import json
import re
import datetime as dt
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime

# ==========
# FEED GOAL
# ==========
# Balanced mode:
# - Broad RSS sources for Finance/World/Auto/Portugal
# - HN Algolia for Tech
# - Anti-fluff filtering
# - Recency preference (12h), fallback (24h), hard cap (48h)
# - Always outputs 30 headlines total with category splits:
#   Finance 5, World 5, Tech 10, Auto/Moto 5, Portugal 5

BUILD_HOUR_UTC = 7
PREF_WINDOW_HOURS = 12
FALLBACK_WINDOW_HOURS = 24
HARD_MAX_AGE_HOURS = 48

REDDIT_UA = "daily-growth-feed/1.1 (personal use)"
HTTP_HEADERS = {"User-Agent": REDDIT_UA}

# ---- Sources (RSS first for breadth) ----
FEEDS = {
    "Finance": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", 1.3),
        ("FT Home (UK)", "https://www.ft.com/rss/home/uk", 1.3),
        ("Yahoo Finance (Top)", "https://finance.yahoo.com/rss/topstories", 1.1),
        ("MarketWatch (Top)", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 1.1),
    ],
    "World news": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 1.3),
        ("BBC Europe", "https://feeds.bbci.co.uk/news/world/europe/rss.xml", 1.2),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", 1.1),
        ("CNN Top", "https://rss.cnn.com/rss/cnn_topstories.rss", 1.0),
    ],
    "Automotive / Motorcycle": [
        ("RideApart", "https://www.rideapart.com/rss/", 1.0),
        ("Motor1", "https://www.motor1.com/rss/", 1.0),
        ("Autocar", "https://www.autocar.co.uk/rss", 1.0),
        ("Visordown", "https://www.visordown.com/rss.xml", 1.0),
    ],
    "Portugal main news": [
        # RTP has official RSS feeds directory; these endpoints are widely used in practice.
        ("RTP Top", "https://www.rtp.pt/noticias/rss/top_noticias", 1.3),
        ("RTP País", "https://www.rtp.pt/noticias/rss/pais", 1.2),
        ("Diário de Notícias (Últimas)", "https://www.dn.pt/rss/ultima-hora/", 1.0),
        ("Expresso", "https://expresso.pt/rss", 1.0),
    ],
}

# ---- Tech: use HN Algolia (traction proxy = points + 2*comments) ----
HN_ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=100&numericFilters=created_at_i>{min_ts}"

# ---- Noise control (balanced) ----
FLUFF_PATTERNS = [
    r"\byou won't believe\b",
    r"\bshocking\b",
    r"\bgoes viral\b",
    r"\bone (simple|weird) trick\b",
    r"\btop \d+\b",
    r"\bbest \d+\b",
    r"\bquiz\b",
    r"\bhoroscope\b",
    r"\bcelebrity\b",
    r"\broyal family\b",
    r"\bkardashian\b",
    r"\btiktok\b",
]

# Category importance keywords (boost signal)
KEYWORDS = {
    "Finance": [
        "earnings","guidance","profit","revenue","inflation","cpi","rates","ecb","fed",
        "bond","yield","stocks","shares","merger","acquisition","bank","debt","default",
    ],
    "World news": [
        "election","ceasefire","sanctions","treaty","nato","eu","un","refugee","missile",
        "strike","attack","summit","border","coup","inflation","recession",
    ],
    "Automotive / Motorcycle": [
        "recall","lawsuit","safety","ev","battery","range","production","union","factory",
        "homologation","emissions","launch","dealership","regulation",
        "yamaha","honda","kawasaki","suzuki","ducati","bmw","ktm","harley",
    ],
    "Portugal main news": [
        "governo","parlamento","sns","habitação","impostos","inflação","economia","justiça",
        "polícia","greve","energia","iva","salários","turismo","tap",
    ],
}

def fetch_text(url: str) -> str:
    req = Request(url, headers=HTTP_HEADERS)
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")

def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url))

def now_utc() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)

def build_time_anchor() -> dt.datetime:
    """
    Anchor scoring to the last 07:00 UTC build time (today if >=07:00, else yesterday).
    This keeps your “daily refresh” concept stable.
    """
    n = now_utc()
    anchor = n.replace(hour=BUILD_HOUR_UTC, minute=0, second=0)
    if n < anchor:
        anchor -= dt.timedelta(days=1)
    return anchor

def parse_rss_datetime(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
        if d.tzinfo is None:
            return d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None

def looks_fluffy(title: str) -> bool:
    t = title.lower()
    for p in FLUFF_PATTERNS:
        if re.search(p, t):
            return True
    return False

def keyword_boost(category: str, title: str) -> int:
    t = title.lower()
    kws = KEYWORDS.get(category, [])
    score = 0
    for kw in kws:
        if kw in t:
            score += 2
    return score

def recency_boost(age_hours: float) -> float:
    # Newer = higher. Past 48h gets zero.
    if age_hours < 0:
        age_hours = 0
    if age_hours >= HARD_MAX_AGE_HOURS:
        return 0.0
    return (HARD_MAX_AGE_HOURS - age_hours)

def normalize_title(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-]", "", s)
    return s[:180]

def dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        key = (normalize_title(it.get("title","")), it.get("url",""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def rss_pull(category: str, source_name: str, url: str, source_weight: float, anchor: dt.datetime) -> list[dict]:
    xml = fetch_text(url)
    out = []
    try:
        root = ET.fromstring(xml)
    except Exception:
        return out

    # RSS: channel/item; Atom: entry
    channel = root.find("channel")
    if channel is not None:
        items = channel.findall("item")
        for it in items:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub_raw = (it.findtext("pubDate") or "").strip()
            published_dt = parse_rss_datetime(pub_raw)
            if not title or not link:
                continue
            if looks_fluffy(title):
                continue

            age_hours = None
            if published_dt:
                age_hours = (anchor.replace(tzinfo=dt.timezone.utc) - published_dt).total_seconds() / 3600.0
            else:
                # Unknown time: treat as somewhat old
                age_hours = 24.0

            if age_hours > HARD_MAX_AGE_HOURS:
                continue

            score = (source_weight * 10.0) + keyword_boost(category, title) + recency_boost(age_hours)

            out.append({
                "title": title,
                "url": link,
                "source": source_name,
                "traction": round(score, 2),
                "published": published_dt.isoformat().replace("+00:00", "Z") if published_dt else pub_raw or "—",
            })
        return out

    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    for e in entries:
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link", ns)
        link = (link_el.get("href") if link_el is not None else "").strip()
        updated_raw = (e.findtext("a:updated", default="", namespaces=ns) or "").strip()
        published_dt = None
        try:
            if updated_raw:
                published_dt = dt.datetime.fromisoformat(updated_raw.replace("Z","+00:00")).astimezone(dt.timezone.utc)
        except Exception:
            published_dt = None

        if not title or not link:
            continue
        if looks_fluffy(title):
            continue

        age_hours = (anchor.replace(tzinfo=dt.timezone.utc) - published_dt).total_seconds()/3600.0 if published_dt else 24.0
        if age_hours > HARD_MAX_AGE_HOURS:
            continue

        score = (source_weight * 10.0) + keyword_boost(category, title) + recency_boost(age_hours)

        out.append({
            "title": title,
            "url": link,
            "source": source_name,
            "traction": round(score, 2),
            "published": published_dt.isoformat().replace("+00:00", "Z") if published_dt else updated_raw or "—",
        })
    return out

def hn_pull(anchor: dt.datetime, min_hours: int) -> list[dict]:
    min_ts = int((anchor - dt.timedelta(hours=min_hours)).replace(tzinfo=dt.timezone.utc).timestamp())
    data = fetch_json(HN_ALGOLIA.format(min_ts=min_ts))
    out = []
    for h in data.get("hits", []):
        title = (h.get("title") or "").strip()
        if not title:
            continue
        if looks_fluffy(title):
            continue

        created_i = int(h.get("created_at_i") or 0)
        created_dt = dt.datetime.fromtimestamp(created_i, tz=dt.timezone.utc)
        age_hours = (anchor.replace(tzinfo=dt.timezone.utc) - created_dt).total_seconds()/3600.0

        if age_hours > HARD_MAX_AGE_HOURS:
            continue

        points = int(h.get("points") or 0)
        comments = int(h.get("num_comments") or 0)
        traction = points + 2 * comments  # real “community traction” proxy
        # Mix traction with recency to avoid very old high-score items dominating.
        score = (traction / 10.0) + recency_boost(age_hours)

        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({
            "title": title,
            "url": link,
            "source": "Hacker News",
            "traction": round(score, 2),
            "published": created_dt.isoformat().replace("+00:00","Z"),
        })
    return out

def windowed_pick(category: str, pool: list[dict], n: int, anchor: dt.datetime) -> list[dict]:
    """
    Prefer PREF_WINDOW_HOURS, then FALLBACK_WINDOW_HOURS.
    Items already filtered by HARD_MAX_AGE_HOURS.
    """
    def within(hours: int, item: dict) -> bool:
        # published can be "Z" ISO or raw; best-effort parse
        p = item.get("published","")
        if p.endswith("Z"):
            try:
                d = dt.datetime.fromisoformat(p.replace("Z","+00:00")).astimezone(dt.timezone.utc)
                age = (anchor.replace(tzinfo=dt.timezone.utc) - d).total_seconds()/3600.0
                return age <= hours
            except Exception:
                return True
        return True

    pref = [x for x in pool if within(PREF_WINDOW_HOURS, x)]
    pref = sorted(pref, key=lambda x: x.get("traction", 0), reverse=True)
    if len(pref) >= n:
        return pref[:n]

    fb = [x for x in pool if within(FALLBACK_WINDOW_HOURS, x)]
    fb = sorted(fb, key=lambda x: x.get("traction", 0), reverse=True)
    return fb[:n]

def main():
    anchor = build_time_anchor()

    # Build category pools from RSS
    pools = {k: [] for k in FEEDS.keys()}

    for category, sources in FEEDS.items():
        for (name, url, w) in sources:
            try:
                pools[category] += rss_pull(category, name, url, w, anchor)
            except Exception:
                # If one feed breaks, keep going
                pass
        pools[category] = dedupe(pools[category])

    # Tech pool from HN (more “traction-like”)
    tech_pool = []
    try:
        tech_pool += hn_pull(anchor, min_hours=FALLBACK_WINDOW_HOURS)
    except Exception:
        pass
    tech_pool = dedupe(tech_pool)

    # Pick top per category with window preference
    finance = windowed_pick("Finance", pools["Finance"], 5, anchor)
    world = windowed_pick("World news", pools["World news"], 5, anchor)
    auto = windowed_pick("Automotive / Motorcycle", pools["Automotive / Motorcycle"], 5, anchor)
    portugal = windowed_pick("Portugal main news", pools["Portugal main news"], 5, anchor)
    tech = sorted(tech_pool, key=lambda x: x.get("traction", 0), reverse=True)[:10]

    sections = [
        {"name": "Finance", "items": finance},
        {"name": "World news", "items": world},
        {"name": "Tech", "items": tech},
        {"name": "Automotive / Motorcycle", "items": auto},
        {"name": "Portugal main news", "items": portugal},
    ]

    out = {
        "title": "Daily Growth Feed (Balanced)",
        "built_at_utc": anchor.isoformat().replace("+00:00", "Z") + "Z" if not anchor.isoformat().endswith("Z") else anchor.isoformat(),
        "next_build_utc": (anchor + dt.timedelta(days=1)).isoformat().replace("+00:00","Z"),
        "policy": {
            "preferred_window_hours": PREF_WINDOW_HOURS,
            "fallback_window_hours": FALLBACK_WINDOW_HOURS,
            "hard_max_age_hours": HARD_MAX_AGE_HOURS,
            "mode": "balanced",
        },
        "sections": sections,
        "sources": [
            "RSS feeds (multi-outlet)",
            "Hacker News Search API (Algolia)",
        ],
    }

    with open("content.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("OK: wrote content.json")

if __name__ == "__main__":
    main()
