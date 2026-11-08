#!/usr/bin/env python3
"""
Usage:
    python card_crawler.py                 # normal run (resumable)
    python card_crawler.py --dry-run       # list what would be fetched
    python card_crawler.py --force         # re-download everything
    python card_crawler.py --workers 2     # override worker count
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

# Global request budget. The script enforces BOTH:
#   - a minimum gap between requests (rate cap)
#   - a jittered extra sleep (human-ish, avoids lockstep bursts)
MIN_INTERVAL = 0.5          # 2 req/s hard ceiling
JITTER_MIN   = 0.1
JITTER_MAX   = 0.4
# Back off harder on errors:
BACKOFF_BASE = 2.0          # seconds; doubles each consecutive failure
BACKOFF_MAX  = 60.0
MAX_CONSECUTIVE_FAILS = 8   # give up on a URL after this many in a row

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DATA_DIR   = "kana_db_data"
IMAGE_DIR  = "kana_db_images"
STATE_FILE = "kana_db_state.json"   # remembers which cards are already done

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
    """
    Fetch and parse robots.txt. Returns a parser. If robots.txt is missing
    or unreachable we conservatively treat everything as allowed (RFC 9309
    says 404 = allow all), but we log it so the user knows.
    """
    rp = urllib.robotparser.RobotFileParser()
    robots_url = f"{BASE_URL}/robots.txt"
    try:
        LIMITER.wait()
        r = session.get(robots_url, timeout=15)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            print(f"[robots] loaded {robots_url}")
        elif r.status_code in (401, 403):
            # Explicitly disallowed — refuse to crawl.
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
    """
    Polite GET with exponential backoff and Retry-After support.
    Raises requests.RequestException on final failure.
    """
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

        # Honor server-requested backoff.
        if r.status_code == 429 or r.status_code == 503:
            retry_after = r.headers.get("Retry-After")
            wait = BACKOFF_BASE
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    pass
            print(f"  [backoff] {r.status_code} on {url}, sleeping {wait:.1f}s")
            time.sleep(min(wait, BACKOFF_MAX))
            continue

        if r.status_code >= 500:
            print(f"  [retry {attempt}/{retries}] {url}: server {r.status_code}")
            time.sleep(min(delay, BACKOFF_MAX))
            delay *= 2
            continue

        if r.status_code >= 400:
            # 4xx (other than 429) is not retryable.
            raise requests.HTTPError(f"{r.status_code} for {url}", response=r)

        if expect_json:
            ctype = r.headers.get("Content-Type", "")
            if "json" not in ctype.lower():
                raise requests.HTTPError(
                    f"Expected JSON from {url}, got Content-Type={ctype!r}",
                    response=r,
                )
            try:
                r.json()  # validate now, not later
            except ValueError as e:
                raise requests.HTTPError(
                    f"Invalid JSON from {url}: {e}", response=r)
        return r

    raise requests.RequestException(
        f"giving up on {url} after {retries} attempts") from last_exc

# WebP magic bytes: "RIFF....WEBP" (12 bytes: RIFF + 4 size + WEBP)
_WEBP_RE = re.compile(rb"^RIFF....WEBP", re.DOTALL)

def looks_like_webp(data: bytes) -> bool:
    return len(data) >= 12 and bool(_WEBP_RE.match(data[:12]))

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def get_max_pages(session: requests.Session, rp: urllib.robotparser.RobotFileParser) -> int:
    url = f"{BASE_URL}/en/card/"
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

    hp_el = detail_soup.find(class_="card-tile__stat--hp")
    hp = hp_el.get_text(strip=True) if hp_el else "Unknown"

    name_el = detail_soup.find(class_="card-detail__title")
    name = name_el.get_text(strip=True) if name_el else f"Card_{cid}"

    # 2) Image
    img_url = f"{BASE_URL}/static/card/Texture2D/CARD_{cid}.webp"
    if not allowed(rp, img_url, USER_AGENT):
        raise PermissionError(f"robots.txt disallows {img_url}")

    img_r = fetch(session, img_url)
    img_data = img_r.content
    if not looks_like_webp(img_data):
        ctype = img_r.headers.get("Content-Type", "?")
        raise ValueError(
            f"card {cid}: image is not WebP (Content-Type={ctype!r}, "
            f"{len(img_data)} bytes) — refusing to save")

    # 3) Persist
    json_path = os.path.join(DATA_DIR, f"{cid}.json")
    img_path  = os.path.join(IMAGE_DIR, f"{cid}.webp")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"id": cid, "name": name, "hp": hp, "image_url": img_url},
            f, ensure_ascii=False, indent=2,
        )
    with open(img_path, "wb") as f:
        f.write(img_data)
    return name

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Polite Kana card crawler")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel workers (keep low to be polite; default 2)")
    ap.add_argument("--force", action="store_true",
                    help="ignore saved state and re-download everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="discover cards but do not download")
    args = ap.parse_args()

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

    # One Session per worker for cookie continuity, but a small pool is fine.
    worker_sessions: list[requests.Session] = [make_session() for _ in range(args.workers)]
    session_q = list(worker_sessions)  # simple reuse; each worker pops one
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
                    # Persist state every 20 successes
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

    print(f"[done] {success} new, {len(done)} total cached.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
