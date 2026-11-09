#!/usr/bin/env python3
"""
Usage:
python kana_scraper.py                          # official/collectable run (resumable)
python kana_scraper.py --include-uncollectable  # also crawl uncollectable/event/token cards
python kana_scraper.py --dry-run                # list what would be fetched
python kana_scraper.py --force                  # re-download everything
python kana_scraper.py --workers 2              # override worker count
python kana_scraper.py --skip-images            # skip downloading images
python kana_scraper.py --delay 0.5              # override delay
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "http://ec2-3-39-192-44.ap-northeast-2.compute.amazonaws.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"

# Global request budget.
MIN_INTERVAL = 0.5          # 2 req/s hard ceiling (can be overridden by --delay)
JITTER_MIN   = 0.1
JITTER_MAX   = 0.4

# Back off harder on errors:
BACKOFF_BASE = 2.0          # seconds; doubles each consecutive failure
BACKOFF_MAX  = 60.0
MAX_CONSECUTIVE_FAILS = 8   # give up on a URL after this many in a row

SKIP_IMAGES = False         # Controlled via CLI
INCLUDE_UNCOLLECTABLE = False
LIST_PARAMS = ""    # full-catalog list query (discovery), without leading ?
FILTER_PARAMS = ""  # appended to other filtered queries (e.g. theme)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DATA_DIR   = "kana_db_data"
IMAGE_DIR  = "kana_db_images"
STATE_FILE = "kana_db_state.json"
MERGED_JSON = "all_cards.json"

# ---------------------------------------------------------------------------
# Faction mapping (theme filter on the list page)
# ---------------------------------------------------------------------------
# The detail page carries no faction field. Faction exists only as the
# `theme` checkbox filter on /en/card/; each theme lists its cards over
# ~24-46 pages. Verified: the five themes partition the full catalog
# exactly (552+548+524+524+278 = 2426, zero overlap).
FACTION_THEME = {
    1: "Vita",
    2: "Royle",
    3: "Crux",
    4: "Darklore",
    5: "Independence",
}

FACTION_REPORT_JSON = "faction_report.json"
FACTION_PAGES_MAX   = 400     # safety cap per theme

# ---------------------------------------------------------------------------
# Category mapping (from card ID first digit)
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "1": "Character",
    "2": "Spell",
    "3": "Follower",
}

# ---------------------------------------------------------------------------
# Rate limiter (thread-safe, global)
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Global limiter: guarantees >= MIN_INTERVAL between *any* two requests,
    plus a small random jitter so the traffic pattern is not metronomic.
    """
    def __init__(self, min_interval: float, jitter_min: float, jitter_max: float):
        self.min_interval = min_interval
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            base_wait = self._last + self.min_interval - now
            jitter = random.uniform(self.jitter_min, self.jitter_max)
            wait = max(0.0, base_wait) + jitter
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

LIMITER = RateLimiter(MIN_INTERVAL, JITTER_MIN, JITTER_MAX)

# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
def check_robots(session: requests.Session, user_agent: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    robots_url = f"{BASE_URL}/robots.txt"
    try:
        LIMITER.wait()
        r = session.get(robots_url, timeout=15)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            print(f"[robots] loaded {robots_url}")
        elif r.status_code in (401, 403):
            rp.disallow_all = True
            print(f"[robots] {robots_url} returned {r.status_code}; treating as DISALLOW ALL")
        else:
            rp.allow_all = True
            print(f"[robots] {robots_url} returned {r.status_code}; treating as allow-all")
    except Exception as e:
        rp.allow_all = True
        print(f"[robots] could not fetch {robots_url} ({e}); treating as allow-all")
    return rp

def allowed(rp: urllib.robotparser.RobotFileParser, url: str, ua: str) -> bool:
    try:
        return rp.can_fetch(ua, url)
    except Exception:
        return True

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def fetch(session: requests.Session, url: str, *, expect_json: bool = False,
          retries: int = 3) -> requests.Response:
    delay = BACKOFF_BASE
    last_exc = None
    for attempt in range(1, retries + 1):
        LIMITER.wait()
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            last_exc = e
            print(f"  [retry {attempt}/{retries}] {url}: {e}")
            time.sleep(min(delay, BACKOFF_MAX))
            delay *= 2
            continue

        if r.status_code == 429 or r.status_code == 503:
            retry_after = r.headers.get("Retry-After")
            wait = BACKOFF_BASE
            if retry_after:
                try: wait = float(retry_after)
                except ValueError: pass
            print(f"  [backoff] {r.status_code} on {url}, sleeping {wait:.1f}s")
            time.sleep(min(wait, BACKOFF_MAX))
            continue

        if r.status_code >= 500:
            print(f"  [retry {attempt}/{retries}] {url}: server {r.status_code}")
            time.sleep(min(delay, BACKOFF_MAX))
            delay *= 2
            continue

        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} for {url}", response=r)

        if expect_json:
            ctype = r.headers.get("Content-Type", "")
            if "json" not in ctype.lower():
                raise requests.HTTPError(
                    f"Expected JSON from {url}, got Content-Type={ctype!r}", response=r)
            try:
                r.json()
            except ValueError as e:
                raise requests.HTTPError(f"Invalid JSON from {url}: {e}", response=r)

        return r

    raise requests.RequestException(f"giving up on {url} after {retries} attempts") from last_exc

_WEBP_RE = re.compile(rb"^RIFF....WEBP", re.DOTALL)
def looks_like_webp(data: bytes) -> bool:
    return len(data) >= 12 and bool(_WEBP_RE.match(data[:12]))

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def get_max_pages(session: requests.Session, rp: urllib.robotparser.RobotFileParser) -> int:
    qs = LIST_PARAMS or "tab=detail"
    url = f"{BASE_URL}/en/card/?{qs}"
    if not allowed(rp, url, USER_AGENT):
        raise SystemExit(f"[robots] disallowed: {url}")
    r = fetch(session, url)
    soup = BeautifulSoup(r.text, "html.parser")
    root = soup.find(attrs={"data-card-search": True})
    if root and root.has_attr("data-page-total"):
        try:
            return int(root["data-page-total"])
        except ValueError:
            pass
    print("[warn] could not detect page-total; defaulting to 1")
    return 1

def get_card_ids(session: requests.Session, rp: urllib.robotparser.RobotFileParser,
                 max_pages: int) -> list[str]:
    ids: set[str] = set()
    for page in range(1, max_pages + 1):
        if LIST_PARAMS:
            url = f"{BASE_URL}/en/card/?{LIST_PARAMS}&page={page}"
        else:
            url = f"{BASE_URL}/en/card/?page={page}"
        if not allowed(rp, url, USER_AGENT):
            print(f"[robots] skipping disallowed page {page}")
            continue
        print(f"[discover] page {page}/{max_pages}")
        try:
            r = fetch(session, url)
        except requests.RequestException as e:
            print(f"[discover] page {page} failed: {e}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.find_all(attrs={"data-card-id": True}):
            ids.add(card["data-card-id"])
    return sorted(ids)

# ---------------------------------------------------------------------------
# State (resumability)
# ---------------------------------------------------------------------------
def load_state() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("done", []))
    except Exception:
        return set()

def save_state(done: set[str]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, indent=2)
    os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------------------
# Per-card work
# ---------------------------------------------------------------------------
def scrape_card(session: requests.Session, rp: urllib.robotparser.RobotFileParser,
                cid: str) -> str:
    """Returns the card name on success, raises on failure."""
    detail_url = f"{BASE_URL}/en/card/select/{cid}"
    if not allowed(rp, detail_url, USER_AGENT):
        raise PermissionError(f"robots.txt disallows {detail_url}")

    # 1) Detail JSON
    r = fetch(session, detail_url, expect_json=True)
    payload = r.json()
    if "html" not in payload:
        raise ValueError(f"unexpected JSON shape for card {cid}: keys={list(payload)[:5]}")
    html = payload["html"]
    detail_soup = BeautifulSoup(html, "html.parser")

    # --- Name ---
    name_el = detail_soup.select_one(".card-detail__name")
    name = name_el.get_text(strip=True) if name_el else f"Card_{cid}"

    # --- Category (from card ID first digit) ---
    category = CATEGORY_MAP.get(cid[0], "Unknown")

    # --- Meta (Episode, Rarity, Cost) ---
    meta_texts = [p.get_text(strip=True) for p in detail_soup.select(".card-detail__meta-text")]
    episode = meta_texts[0] if len(meta_texts) > 0 else ""
    rarity  = meta_texts[1] if len(meta_texts) > 1 else ""
    cost    = meta_texts[3] if len(meta_texts) > 3 else ""

    # --- Faction (backfilled from the list-page theme filter at merge time) ---
    faction = "Unknown"

    # --- Tags ---
    tag_el = detail_soup.select_one(".card-detail__tag")
    tags = tag_el.get_text(strip=True) if tag_el else ""

    # --- Stats ---
    def stat_text(selector):
        el = detail_soup.select_one(selector)
        return el.get_text(strip=True) if el else "0"

    stats = {
        "size": stat_text(".card-tile__stat--size"),
        "atk":  stat_text(".card-tile__stat--atk"),
        "def":  stat_text(".card-tile__stat--def"),
        "hp":   stat_text(".card-tile__stat--hp"),
    }

    # --- Skill & Story text ---
    skill_div = detail_soup.select_one('[data-text-panel="skill"]')
    skill_text = "\n".join(
        [p.get_text(strip=True) for p in skill_div.select("p") if p.get_text(strip=True)]
    ) if skill_div else ""

    story_div = detail_soup.select_one('[data-text-panel="story"]')
    story_text = "\n".join(
        [p.get_text(strip=True) for p in story_div.select("p") if p.get_text(strip=True)]
    ) if story_div else ""

    # --- Producible (has crafting materials?) ---
    products_el = detail_soup.select_one('[data-card-products]')
    producible = False
    if products_el:
        material_rows = products_el.select('tr')
        producible = len(material_rows) > 0

    # --- Uncollectable ---
    uncollectable = (episode.strip() == "" or episode.strip() == "\xa0")

    # 2) Image
    img_url = f"{BASE_URL}/static/card/Texture2D/CARD_{cid}.webp"
    if not SKIP_IMAGES:
        if not allowed(rp, img_url, USER_AGENT):
            raise PermissionError(f"robots.txt disallows {img_url}")
        img_r = fetch(session, img_url)
        img_data = img_r.content
        if not looks_like_webp(img_data):
            ctype = img_r.headers.get("Content-Type", "?")
            raise ValueError(
                f"card {cid}: image is not WebP (Content-Type={ctype!r}, "
                f"{len(img_data)} bytes) — refusing to save")
        img_path = os.path.join(IMAGE_DIR, f"{cid}.webp")
        with open(img_path, "wb") as f:
            f.write(img_data)

    # 3) Persist JSON
    json_path = os.path.join(DATA_DIR, f"{cid}.json")
    card_data = {
        "id": cid,
        "name": name,
        "category": category,
        "episode": episode,
        "rarity": rarity,
        "cost": cost,
        "faction": faction,
        "tags": tags,
        "uncollectable": uncollectable,
        "producible": producible,
        "stats": stats,
        "skill": skill_text,
        "story": story_text,
        "image_url": img_url,
        "image_file": f"{cid}.webp",
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(card_data, f, ensure_ascii=False, indent=2)

    return name

# ---------------------------------------------------------------------------
# Faction backfill (list-page theme filter)
# ---------------------------------------------------------------------------
def crawl_factions(session: requests.Session,
                   rp: urllib.robotparser.RobotFileParser) -> tuple[dict[str, str], list[list[str]]]:
    """Page through each theme filter; map card id -> faction label."""
    faction_by_id: dict[str, str] = {}
    conflicts: list[list[str]] = []
    for theme, label in FACTION_THEME.items():
        page = 1
        page_total = None
        theme_count = 0
        while True:
            url = f"{BASE_URL}/en/card/?search_type=name&search_text=&theme={theme}&page={page}{FILTER_PARAMS}"
            if not allowed(rp, url, USER_AGENT):
                print(f"[faction] robots.txt disallows {url}; skipping theme {theme}")
                break
            try:
                r = fetch(session, url)
            except requests.RequestException as e:
                print(f"[faction] theme {theme} page {page} failed: {e}; stopping theme")
                break
            soup = BeautifulSoup(r.text, "html.parser")
            if page_total is None:
                root = soup.find(attrs={"data-card-search": True})
                if root and root.has_attr("data-page-total"):
                    try:
                        page_total = int(root["data-page-total"])
                    except ValueError:
                        page_total = 1
            found = [c["data-card-id"] for c in soup.find_all(attrs={"data-card-id": True})]
            for cid in found:
                if cid in faction_by_id:
                    conflicts.append([cid, faction_by_id[cid], label])
                else:
                    faction_by_id[cid] = label
                    theme_count += 1
            print(f"[faction] theme {theme}/{label} page {page}/{page_total or '?'} ({len(found)} ids)")
            if not found or (page_total is not None and page >= page_total) or page >= FACTION_PAGES_MAX:
                break
            page += 1
        print(f"[faction] theme {theme}/{label}: {theme_count} cards")
    return faction_by_id, conflicts

# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
def merge_all_cards(faction_by_id: dict[str, str] | None = None,
                    conflicts: list[list[str]] | None = None) -> None:
    print("[merge] combining all card data into a single JSON file...")
    all_cards = []
    if os.path.exists(DATA_DIR):
        for filename in sorted(os.listdir(DATA_DIR)):
            if filename.endswith(".json") and filename not in {MERGED_JSON, "uncollectable_only.json"}:
                filepath = os.path.join(DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        all_cards.append(json.load(f))
                except Exception as e:
                    print(f"[warn] could not read {filename}: {e}")

    faction_by_id = faction_by_id or {}
    unknown = []
    counts: dict[str, int] = {label: 0 for label in FACTION_THEME.values()}
    for c in all_cards:
        fac = faction_by_id.get(c["id"])
        if fac:
            c["faction"] = fac
            counts.setdefault(fac, 0)
            counts[fac] += 1
        else:
            c["faction"] = "Unknown"   # frame-based guesses are unusable; never keep them
            unknown.append(c["id"])

    report = {
        "faction_counts": counts,
        "unknown": unknown,
        "conflicts": conflicts or [],
        "total_cards": len(all_cards),
        "uncollectable_cards": sum(1 for c in all_cards if c.get("uncollectable")),
        "include_uncollectable": INCLUDE_UNCOLLECTABLE,
    }
    with open(FACTION_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[faction] unknown={len(unknown)} conflicts={len(conflicts or [])} "
          f"report={FACTION_REPORT_JSON}")

    merged_path = os.path.join(DATA_DIR, MERGED_JSON)
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(all_cards, f, ensure_ascii=False, indent=2)
    print(f"[merge] saved {len(all_cards)} cards to {merged_path}")

    if INCLUDE_UNCOLLECTABLE:
        uncollectable = [c for c in all_cards if c.get("uncollectable")]
        only_path = os.path.join(DATA_DIR, "uncollectable_only.json")
        with open(only_path, "w", encoding="utf-8") as f:
            json.dump(uncollectable, f, ensure_ascii=False, indent=2)
        print(f"[merge] saved {len(uncollectable)} uncollectable/event/token cards to {only_path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    global MIN_INTERVAL, SKIP_IMAGES, INCLUDE_UNCOLLECTABLE, LIST_PARAMS, FILTER_PARAMS
    global DATA_DIR, IMAGE_DIR, STATE_FILE, MERGED_JSON, FACTION_REPORT_JSON

    ap = argparse.ArgumentParser(description="Polite Kana card crawler")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel workers (keep low to be polite; default 2)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="minimum seconds between requests (default 0.5)")
    ap.add_argument("--force", action="store_true",
                    help="ignore saved state and re-download everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="discover cards but do not download")
    ap.add_argument("--skip-images", action="store_true",
                    help="skip downloading card images to save bandwidth")
    ap.add_argument("--include-uncollectable", action="store_true",
                    help="include uncollectable/event/token cards (etc=uncollectable)")
    ap.add_argument("--data-dir", default=None, help="override JSON data directory")
    ap.add_argument("--image-dir", default=None, help="override image directory")
    ap.add_argument("--state-file", default=None, help="override resumable state file")
    ap.add_argument("--merged-name", default=None, help="override merged JSON filename (inside data dir)")
    ap.add_argument("--faction-report", default=None, help="override faction report path")
    args = ap.parse_args()

    MIN_INTERVAL = args.delay
    SKIP_IMAGES = args.skip_images
    INCLUDE_UNCOLLECTABLE = args.include_uncollectable
    if INCLUDE_UNCOLLECTABLE:
        LIST_PARAMS = "search_type=name&search_text=&etc=uncollectable&tab=etc&deck_tab=edit"
        FILTER_PARAMS = "&etc=uncollectable"
    else:
        LIST_PARAMS = ""
        FILTER_PARAMS = ""
    LIMITER.min_interval = MIN_INTERVAL

    if INCLUDE_UNCOLLECTABLE:
        DATA_DIR = args.data_dir or "kana_db_data_uncollectable"
        IMAGE_DIR = args.image_dir or "kana_db_images"  # share assets
        STATE_FILE = args.state_file or "kana_db_state_uncollectable.json"
        MERGED_JSON = args.merged_name or "all_cards_with_uncollectable.json"
        FACTION_REPORT_JSON = args.faction_report or "faction_report_uncollectable.json"
    else:
        DATA_DIR = args.data_dir or "kana_db_data"
        IMAGE_DIR = args.image_dir or "kana_db_images"
        STATE_FILE = args.state_file or "kana_db_state.json"
        MERGED_JSON = args.merged_name or "all_cards.json"
        FACTION_REPORT_JSON = args.faction_report or "faction_report.json"

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)

    session = make_session()
    rp = check_robots(session, USER_AGENT)
    if getattr(rp, "disallow_all", False):
        print("[abort] robots.txt says disallow all. Exiting.")
        return 2

    max_pages = get_max_pages(session, rp)
    print(f"[discover] max_pages = {max_pages}")
    all_ids = get_card_ids(session, rp, max_pages)
    print(f"[discover] {len(all_ids)} unique cards")

    done = set() if args.force else load_state()
    todo = [c for c in all_ids if c not in done]
    print(f"[plan] {len(done)} already done, {len(todo)} to fetch")

    if args.dry_run:
        print("[dry-run] would fetch:", ", ".join(todo[:20]),
              ("..." if len(todo) > 20 else ""))
        return 0

    success = 0
    consecutive_fail = 0
    worker_sessions: list[requests.Session] = [make_session() for _ in range(args.workers)]
    session_q = list(worker_sessions)
    lock = threading.Lock()

    def work(cid: str) -> tuple[str, str | None, Exception | None]:
        with lock:
            sess = session_q.pop() if session_q else make_session()
        try:
            name = scrape_card(sess, rp, cid)
            return cid, name, None
        except Exception as e:
            return cid, None, e
        finally:
            with lock:
                session_q.append(sess)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(work, cid): cid for cid in todo}
            for fut in as_completed(futures):
                cid, name, err = fut.result()
                if err is None:
                    success += 1
                    done.add(cid)
                    consecutive_fail = 0
                    print(f"[{len(done)}/{len(all_ids)}] {name} ({cid})")
                    if success % 20 == 0:
                        save_state(done)
                else:
                    consecutive_fail += 1
                    print(f"[fail] {cid}: {err}")
                    if consecutive_fail >= MAX_CONSECUTIVE_FAILS:
                        print(f"[abort] {consecutive_fail} consecutive failures — "
                              f"the server may be unhappy. Stopping.")
                        raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("[interrupt] saving state and exiting...")
    finally:
        save_state(done)

    # Faction backfill runs even when everything is cached — it is the
    # cheapest way to fix factions on an existing kana_db_data/ cache.
    faction_by_id, conflicts = crawl_factions(session, rp)

    # Merge all individual JSON files into one
    merge_all_cards(faction_by_id, conflicts)

    print(f"[done] {success} new, {len(done)} total cached.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
