#!/usr/bin/env python3
"""
Kana card scraper — concurrency-gated, schema-versioned, resumable.

Throughput model:
  - A global semaphore caps in-flight requests to --concurrency (default 5).
  - The server's own 429/503 responses drive backoff; no artificial delay.
  - --delay (default 0) adds an optional per-worker floor between requests.

Cached-card behavior:
  - Up to date (schema_version == SCHEMA_VERSION)  -> skipped, zero requests.
  - Stale, change derivable from cached data       -> upgraded locally, zero requests.
  - Stale, change needs server data                -> re-fetched WITH --refresh-stale.

Usage:
    python kana_scraper.py                          # collectable run (resumable)
    python kana_scraper.py --concurrency 5          # cap in-flight requests
    python kana_scraper.py --delay 0.2              # optional per-worker floor
    python kana_scraper.py --refresh-stale          # re-fetch stale-schema cards
    python kana_scraper.py --include-uncollectable  # also crawl uncollectable cards
    python kana_scraper.py --dry-run                # list what would be fetched
    python kana_scraper.py --force                  # re-download everything
    python kana_scraper.py --skip-images            # skip downloading images
"""
from __future__ import annotations

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
from dataclasses import dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

# ===========================================================================
# Schema version
# ===========================================================================
# Bump whenever the per-card output schema changes.
#   - Additive / derivable from cache  -> bump + add _backfill_card() block.
#   - Needs new server data            -> bump; run with --refresh-stale.
SCHEMA_VERSION = 2

# ===========================================================================
# Constants
# ===========================================================================
BASE_URL = "http://ec2-3-39-192-44.ap-northeast-2.compute.amazonaws.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0
MAX_CONSECUTIVE_FAILS = 20   # raised: occasional 429s are normal under concurrency

CATEGORY_MAP = {
    "1": "Character",
    "2": "Spell",
    "3": "Follower",
}

FACTION_THEME = {
    1: "Vita",
    2: "Royle",
    3: "Crux",
    4: "Darklore",
    5: "Independence",
}
FACTION_PAGES_MAX = 400

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MATERIAL_RE = re.compile(
    r"^(?P<name>.*?)\s*[xX×]\s*(?P<amount>[\d,]+)\s*$"
)

# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config:
    concurrency: int = 5
    delay: float = 0.0
    force: bool = False
    refresh_stale: bool = False
    dry_run: bool = False
    skip_images: bool = False
    include_uncollectable: bool = False

    data_dir: str = "kana_db_data"
    image_dir: str = "kana_db_images"
    state_file: str = "kana_db_state.json"
    merged_name: str = "all_cards.json"
    faction_report: str = "faction_report.json"

    list_params: str = ""
    filter_params: str = ""

    def apply_uncollectable(self) -> None:
        if not self.include_uncollectable:
            return
        self.list_params = (
            "search_type=name&search_text=&etc=uncollectable&tab=etc&deck_tab=edit"
        )
        self.filter_params = "&etc=uncollectable"
        self.data_dir = self.data_dir or "kana_db_data_uncollectable"
        self.state_file = self.state_file or "kana_db_state_uncollectable.json"
        self.merged_name = self.merged_name or "all_cards_with_uncollectable.json"
        self.faction_report = self.faction_report or "faction_report_uncollectable.json"

# ===========================================================================
# Concurrency gate
# ===========================================================================
class ConcurrencyGate:
    """
    Caps the number of simultaneous in-flight requests.

    Compared to a rate limiter (X req/s), a gate (N in flight) adapts to
    actual server latency: if responses are slow, fewer complete per second;
    if fast, more. The server's own 429/503 responses provide the backoff.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self._sem = threading.BoundedSemaphore(limit)

    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._sem.release()
        return False

# ===========================================================================
# Per-worker delay floor (optional)
# ===========================================================================
class DelayFloor:
    """
    Optional per-worker minimum spacing between that worker's requests.
    Unlike the old global limiter this does NOT serialize across workers,
    so it can't become a bottleneck — it just paces each thread.

    When delay <= 0 this is a no-op.
    """

    def __init__(self, delay: float):
        self.delay = delay
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last + self.delay - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

# ===========================================================================
# robots.txt cache
# ===========================================================================
class RobotsCache:
    def __init__(self, rp: urllib.robotparser.RobotFileParser, ua: str):
        self.rp = rp
        self.ua = ua
        self._cache: dict[str, bool] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        with self._lock:
            hit = self._cache.get(url)
        if hit is not None:
            return hit
        try:
            val = self.rp.can_fetch(self.ua, url)
        except Exception:
            val = True
        with self._lock:
            self._cache[url] = val
        return val

# ===========================================================================
# HTTP layer
# ===========================================================================
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # Enlarge the connection pool so pooled sessions aren't a hidden bottleneck.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=16, pool_maxsize=16, max_retries=0
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

class Http:
    def __init__(self, session: requests.Session, gate: ConcurrencyGate, floor: DelayFloor):
        self.session = session
        self.gate = gate
        self.floor = floor

    def fetch(self, url: str, *, expect_json: bool = False, retries: int = 4) -> requests.Response:
        delay = BACKOFF_BASE
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            self.floor.wait()
            try:
                with self.gate:
                    r = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                last_exc = e
                print(f"  [retry {attempt}/{retries}] {url}: {e}")
                time.sleep(min(delay, BACKOFF_MAX))
                delay *= 2
                continue

            # Server-driven throttle: honor Retry-After when present.
            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After")
                wait = BACKOFF_BASE
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                # A bit of jitter prevents synchronized retry stampedes
                wait = min(wait, BACKOFF_MAX) + random.uniform(0, 0.5)
                print(f"  [backoff] {r.status_code} on {url}, sleeping {wait:.1f}s")
                time.sleep(wait)
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
                        f"Expected JSON from {url}, got Content-Type={ctype!r}",
                        response=r,
                    )
                try:
                    r.json()
                except ValueError as e:
                    raise requests.HTTPError(f"Invalid JSON from {url}: {e}", response=r)

            return r

        raise requests.RequestException(
            f"giving up on {url} after {retries} attempts"
        ) from last_exc

_WEBP_RE = re.compile(rb"^RIFF....WEBP", re.DOTALL)

def looks_like_webp(data: bytes) -> bool:
    return len(data) >= 12 and bool(_WEBP_RE.match(data[:12]))

# ===========================================================================
# Parsing helpers
# ===========================================================================
def _text(el) -> str:
    return el.get_text(strip=True) if el else ""

def _int_stat(soup: BeautifulSoup, selector: str) -> int:
    el = soup.select_one(selector)
    if not el:
        return 0
    try:
        return int(el.get_text(strip=True))
    except (TypeError, ValueError):
        return 0

def _panel_text(soup: BeautifulSoup, panel: str) -> str:
    div = soup.select_one(f'[data-text-panel="{panel}"]')
    if not div:
        return ""
    return "\n".join(
        p.get_text(strip=True) for p in div.select("p") if p.get_text(strip=True)
    )

def _parse_chain_entry(html: str, state: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    return {
        "level": _int_stat(soup, ".card-tile__stat--enhance"),
        "stats": {
            "atk": _int_stat(soup, ".card-tile__stat--atk"),
            "def": _int_stat(soup, ".card-tile__stat--def"),
            "hp": _int_stat(soup, ".card-tile__stat--hp"),
        },
        "has_product": state.get("has_product", False),
    }

def _split_material_name_quantity(raw: str) -> tuple[str, int | None]:
    if not raw:
        return "", None
    m = _MATERIAL_RE.match(raw.strip())
    if not m:
        return raw.strip(), None
    name = m.group("name").strip()
    try:
        amount = int(m.group("amount").replace(",", ""))
    except ValueError:
        return raw.strip(), None
    return name, amount

# ===========================================================================
# Backfill (schema migration for cached cards — no network)
# ===========================================================================
def _backfill_card(card: dict) -> bool:
    changed = False

    if card.get("schema_version", 1) < 2:
        for mat in card.get("materials", []):
            if "name" not in mat or "amount" not in mat:
                raw = mat.get("name_quantity", "")
                name, amount = _split_material_name_quantity(raw)
                mat.setdefault("name", name)
                mat.setdefault("amount", amount)
            if "image" not in mat and "image_url" in mat:
                mat["image"] = mat.pop("image_url")
            changed = True
        card["schema_version"] = 2

    return changed

# ===========================================================================
# Scraper
# ===========================================================================
class KanaScraper:
    def __init__(self, cfg: Config, robots: RobotsCache, http: Http):
        self.cfg = cfg
        self.robots = robots
        self.http = http

    # ---- discovery -------------------------------------------------------
    def discover_max_pages(self) -> int:
        qs = self.cfg.list_params or "tab=detail"
        url = f"{BASE_URL}/en/card/?{qs}"
        if not self.robots.allowed(url):
            raise SystemExit(f"[robots] disallowed: {url}")
        r = self.http.fetch(url)
        soup = BeautifulSoup(r.text, "html.parser")
        root = soup.find(attrs={"data-card-search": True})
        if root and root.has_attr("data-page-total"):
            try:
                return int(root["data-page-total"])
            except ValueError:
                pass
        print("[warn] could not detect page-total; defaulting to 1")
        return 1

    def discover_card_ids(self, max_pages: int) -> list[str]:
        ids: set[str] = set()
        for page in range(1, max_pages + 1):
            if self.cfg.list_params:
                url = f"{BASE_URL}/en/card/?{self.cfg.list_params}&page={page}"
            else:
                url = f"{BASE_URL}/en/card/?page={page}"
            if not self.robots.allowed(url):
                print(f"[discover] robots disallows page {page}; skipping")
                continue
            print(f"[discover] page {page}/{max_pages}")
            try:
                r = self.http.fetch(url)
            except requests.RequestException as e:
                print(f"[discover] page {page} failed: {e}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.find_all(attrs={"data-card-id": True}):
                ids.add(card["data-card-id"])
        return sorted(ids)

    # ---- enhancement chain ----------------------------------------------
    def _enhancement_chain(self, start_id: str, start_payload: dict | None = None) -> list[dict]:
        chain: list[dict] = []
        visited: set[str] = set()
        current_id = start_id
        payload = start_payload

        while current_id != -1 and current_id not in visited:
            visited.add(current_id)

            if payload is None:
                url = f"{BASE_URL}/en/card/select/{current_id}"
                if not self.robots.allowed(url):
                    break
                try:
                    payload = self.http.fetch(url, expect_json=True).json()
                except Exception as e:
                    print(f"  [warn] enhancement hop {current_id} failed: {e}")
                    break

            if "html" not in payload:
                break

            state = payload.get("state", {})
            entry = _parse_chain_entry(payload["html"], state)
            entry["card_id"] = current_id
            chain.append(entry)

            current_id = state.get("enh_next", -1)
            payload = None

        return chain

    # ---- card scraping ---------------------------------------------------
    def scrape_card(self, cid: str) -> str:
        detail_url = f"{BASE_URL}/en/card/select/{cid}"
        if not self.robots.allowed(detail_url):
            raise PermissionError(f"robots.txt disallows {detail_url}")

        payload = self.http.fetch(detail_url, expect_json=True).json()
        if "html" not in payload:
            raise ValueError(f"unexpected JSON shape for card {cid}: keys={list(payload)[:5]}")

        html = payload["html"]
        state = payload.get("state", {})
        soup = BeautifulSoup(html, "html.parser")

        name = _text(soup.select_one(".card-detail__name")) or f"Card_{cid}"
        category = CATEGORY_MAP.get(cid[0], "Unknown")

        meta = [p.get_text(strip=True) for p in soup.select(".card-detail__meta-text")]
        episode = meta[0] if len(meta) > 0 else ""
        rarity = meta[1] if len(meta) > 1 else ""
        cost = meta[3] if len(meta) > 3 else ""

        faction = "Unknown"
        tags = _text(soup.select_one(".card-detail__tag"))

        stats = {
            "size": _text(soup.select_one(".card-tile__stat--size")) or "0",
            "atk": _text(soup.select_one(".card-tile__stat--atk")) or "0",
            "def": _text(soup.select_one(".card-tile__stat--def")) or "0",
            "hp": _text(soup.select_one(".card-tile__stat--hp")) or "0",
        }

        skill = _panel_text(soup, "skill")
        story = _panel_text(soup, "story")
        dial = _panel_text(soup, "dial")

        producible = False
        materials: list[dict] = []
        products_el = soup.select_one("[data-card-products]")
        if products_el:
            rows = products_el.select("tr")
            if rows:
                producible = True
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue
                    style = cells[0].get("style", "")
                    m = re.search(r"url\(([^)]+)\)", style)
                    image = m.group(1).strip("'\"") if m else ""
                    raw = cells[1].get_text(strip=True)
                    mat_name, amount = _split_material_name_quantity(raw)
                    materials.append({
                        "image": image,
                        "name": mat_name,
                        "amount": amount,
                        "name_quantity": raw,
                    })

        chain: list[dict] = []
        if state.get("enh_prev", -1) == -1:
            chain = self._enhancement_chain(cid, start_payload=payload)

        uncollectable = episode.strip() == ""

        img_url = f"{BASE_URL}/static/card/Texture2D/CARD_{cid}.webp"
        if not self.cfg.skip_images:
            self._download_image(cid, img_url)

        card_data = {
            "schema_version": SCHEMA_VERSION,
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
            "materials": materials,
            "stats": stats,
            "skill": skill,
            "story": story,
            "dial": dial,
            "enhancement_chain": chain,
            "image_url": img_url,
            "image_file": f"{cid}.webp",
        }
        self._write_json(os.path.join(self.cfg.data_dir, f"{cid}.json"), card_data)
        return name

    def _download_image(self, cid: str, img_url: str) -> None:
        if not self.robots.allowed(img_url):
            raise PermissionError(f"robots.txt disallows {img_url}")
        data = self.http.fetch(img_url).content
        if not looks_like_webp(data):
            raise ValueError(f"card {cid}: image is not WebP ({len(data)} bytes) — not saving")
        path = os.path.join(self.cfg.image_dir, f"{cid}.webp")
        with open(path, "wb") as f:
            f.write(data)

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---- faction backfill ------------------------------------------------
    def crawl_factions(self) -> tuple[dict[str, str], list[list[str]]]:
        faction_by_id: dict[str, str] = {}
        conflicts: list[list[str]] = []

        with ThreadPoolExecutor(max_workers=len(FACTION_THEME)) as ex:
            futures = {
                ex.submit(self._crawl_theme, theme, label): label
                for theme, label in FACTION_THEME.items()
            }
            for fut in as_completed(futures):
                theme_map, theme_conflicts = fut.result()
                for cid, fac in theme_map.items():
                    existing = faction_by_id.get(cid)
                    if existing and existing != fac:
                        conflicts.append([cid, existing, fac])
                    else:
                        faction_by_id[cid] = fac
                conflicts.extend(theme_conflicts)
        return faction_by_id, conflicts

    def _crawl_theme(self, theme: int, label: str) -> tuple[dict[str, str], list[list[str]]]:
        theme_map: dict[str, str] = {}
        conflicts: list[list[str]] = []
        page = 1
        page_total: int | None = None
        count = 0

        while True:
            url = (
                f"{BASE_URL}/en/card/?search_type=name&search_text="
                f"&theme={theme}&page={page}{self.cfg.filter_params}"
            )
            if not self.robots.allowed(url):
                print(f"[faction] robots disallows {url}; skipping theme {theme}")
                break
            try:
                r = self.http.fetch(url)
            except requests.RequestException as e:
                print(f"[faction] theme {theme} page {page} failed: {e}; stopping")
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
                if cid in theme_map:
                    conflicts.append([cid, label, label])
                else:
                    theme_map[cid] = label
                count += 1

            print(f"[faction] {label} page {page}/{page_total or '?'} ({len(found)} ids)")

            if (
                not found
                or (page_total is not None and page >= page_total)
                or page >= FACTION_PAGES_MAX
            ):
                break
            page += 1

        print(f"[faction] {label}: {count} cards total")
        return theme_map, conflicts

    # ---- merge (with backfill) ------------------------------------------
    def merge_all(self, faction_by_id: dict[str, str], conflicts: list[list[str]]) -> None:
        print("[merge] combining per-card JSON into master file...")
        all_cards: list[dict] = []
        upgraded = 0
        skip = {self.cfg.merged_name, "uncollectable_only.json", self.cfg.faction_report}

        if os.path.isdir(self.cfg.data_dir):
            for fn in sorted(os.listdir(self.cfg.data_dir)):
                if not fn.endswith(".json") or fn in skip:
                    continue
                path = os.path.join(self.cfg.data_dir, fn)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        card = json.load(f)
                except Exception as e:
                    print(f"[warn] could not read {fn}: {e}")
                    continue

                if _backfill_card(card):
                    upgraded += 1
                    self._write_json(path, card)

                all_cards.append(card)

        if upgraded:
            print(f"[merge] backfilled {upgraded} cached card(s) to schema v{SCHEMA_VERSION}")

        unknown: list[str] = []
        counts: dict[str, int] = {label: 0 for label in FACTION_THEME.values()}
        for card in all_cards:
            fac = faction_by_id.get(card["id"])
            if fac:
                card["faction"] = fac
                counts[fac] = counts.get(fac, 0) + 1
            else:
                card["faction"] = "Unknown"
                unknown.append(card["id"])

        report = {
            "faction_counts": counts,
            "unknown": unknown,
            "conflicts": conflicts,
            "total_cards": len(all_cards),
            "uncollectable_cards": sum(1 for c in all_cards if c.get("uncollectable")),
            "include_uncollectable": self.cfg.include_uncollectable,
            "schema_version": SCHEMA_VERSION,
        }
        self._write_json(self.cfg.faction_report, report)
        print(
            f"[faction] unknown={len(unknown)} conflicts={len(conflicts)} "
            f"-> {self.cfg.faction_report}"
        )

        merged_path = os.path.join(self.cfg.data_dir, self.cfg.merged_name)
        self._write_json(merged_path, all_cards)
        print(f"[merge] saved {len(all_cards)} cards -> {merged_path}")

        if self.cfg.include_uncollectable:
            uncollectable = [c for c in all_cards if c.get("uncollectable")]
            only_path = os.path.join(self.cfg.data_dir, "uncollectable_only.json")
            self._write_json(only_path, uncollectable)
            print(f"[merge] saved {len(uncollectable)} uncollectable -> {only_path}")

# ===========================================================================
# State (resumability)
# ===========================================================================
def load_state(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("done", []))
    except Exception:
        return set()

def save_state(path: str, done: set[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, indent=2)
    os.replace(tmp, path)

# ===========================================================================
# Stale detection
# ===========================================================================
def find_stale_ids(data_dir: str) -> set[str]:
    stale: set[str] = set()
    if not os.path.isdir(data_dir):
        return stale

    skip = {"all_cards.json", "all_cards_with_uncollectable.json",
            "uncollectable_only.json", "faction_report.json",
            "faction_report_uncollectable.json"}

    for fn in os.listdir(data_dir):
        if not fn.endswith(".json") or fn in skip:
            continue
        path = os.path.join(data_dir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                card = json.load(f)
        except Exception:
            continue
        if card.get("schema_version", 1) < SCHEMA_VERSION:
            cid = card.get("id") or fn[:-5]
            stale.add(cid)
    return stale

# ===========================================================================
# Orchestration
# ===========================================================================
def run(cfg: Config) -> int:
    gate = ConcurrencyGate(cfg.concurrency)
    floor = DelayFloor(cfg.delay)
    robots_session = make_session()
    robots_http = Http(robots_session, gate, floor)

    # robots.txt
    rp = urllib.robotparser.RobotFileParser()
    robots_url = f"{BASE_URL}/robots.txt"
    try:
        r = robots_http.fetch(robots_url)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            print(f"[robots] loaded {robots_url}")
        elif r.status_code in (401, 403):
            rp.disallow_all = True
            print(f"[robots] {r.status_code} -> DISALLOW ALL")
        else:
            rp.allow_all = True
            print(f"[robots] {r.status_code} -> allow-all")
    except Exception as e:
        rp.allow_all = True
        print(f"[robots] fetch failed ({e}) -> allow-all")

    if getattr(rp, "disallow_all", False):
        print("[abort] robots.txt disallows all. Exiting.")
        return 2

    robots = RobotsCache(rp, USER_AGENT)

    # Shared session for discovery / faction / merge; per-worker sessions
    # for scraping so connection pools don't contend.
    main_http = Http(make_session(), gate, floor)
    scraper = KanaScraper(cfg, robots, main_http)

    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.image_dir, exist_ok=True)

    max_pages = scraper.discover_max_pages()
    print(f"[discover] max_pages = {max_pages}")
    all_ids = scraper.discover_card_ids(max_pages)
    print(f"[discover] {len(all_ids)} unique cards")

    # ---- plan ----------------------------------------------------------
    done = set() if cfg.force else load_state(cfg.state_file)

    if cfg.force:
        todo = list(all_ids)
        print(f"[plan] --force: re-fetching all {len(todo)} cards")
    elif cfg.refresh_stale:
        stale = find_stale_ids(cfg.data_dir)
        stale_in_catalog = stale & set(all_ids)
        missing = set(all_ids) - done
        todo = sorted(stale_in_catalog | missing)
        print(
            f"[plan] --refresh-stale: {len(missing)} missing + "
            f"{len(stale_in_catalog)} stale = {len(todo)} to fetch"
        )
    else:
        todo = [c for c in all_ids if c not in done]
        print(f"[plan] {len(done)} done, {len(todo)} to fetch")

    if cfg.dry_run:
        head = ", ".join(todo[:20])
        print(f"[dry-run] would fetch: {head}{'...' if len(todo) > 20 else ''}")
        return 0

    # ---- scrape --------------------------------------------------------
    success = 0
    consecutive_fail = 0
    fail_lock = threading.Lock()
    session_pool = [make_session() for _ in range(cfg.concurrency)]
    pool_lock = threading.Lock()

    def work(cid: str) -> tuple[str, str | None, Exception | None]:
        with pool_lock:
            sess = session_pool.pop() if session_pool else make_session()
        local_http = Http(sess, gate, floor)
        local_scraper = KanaScraper(cfg, robots, local_http)
        try:
            return cid, local_scraper.scrape_card(cid), None
        except Exception as e:
            return cid, None, e
        finally:
            with pool_lock:
                session_pool.append(sess)

    try:
        # Thread count = concurrency (each thread does one card at a time;
        # the gate additionally caps in-flight requests across all threads).
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
            futures = {ex.submit(work, cid): cid for cid in todo}
            for fut in as_completed(futures):
                cid, name, err = fut.result()
                if err is None:
                    success += 1
                    done.add(cid)
                    with fail_lock:
                        consecutive_fail = 0
                    print(f"[{len(done)}/{len(all_ids)}] {name} ({cid})")
                    if success % 20 == 0:
                        save_state(cfg.state_file, done)
                else:
                    print(f"[fail] {cid}: {err}")
                    with fail_lock:
                        consecutive_fail += 1
                        if consecutive_fail >= MAX_CONSECUTIVE_FAILS:
                            print(
                                f"[abort] {consecutive_fail} consecutive failures — "
                                f"server may be unhappy. Stopping."
                            )
                            raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("[interrupt] saving state...")
    finally:
        save_state(cfg.state_file, done)

    faction_by_id, conflicts = scraper.crawl_factions()
    scraper.merge_all(faction_by_id, conflicts)

    print(f"[done] {success} new, {len(done)} total cached.")
    return 0

# ===========================================================================
# CLI
# ===========================================================================
def parse_args(argv: list[str] | None = None) -> Config:
    ap = argparse.ArgumentParser(description="Kana card crawler (concurrency-gated)")
    ap.add_argument("--concurrency", type=int, default=5,
                    help="max simultaneous in-flight requests (default 5)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="optional per-worker floor between requests (default 0)")
    ap.add_argument("--force", action="store_true",
                    help="ignore cache entirely and re-download everything")
    ap.add_argument("--refresh-stale", action="store_true",
                    help="re-fetch cached cards whose schema_version is below current")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-images", action="store_true")
    ap.add_argument("--include-uncollectable", action="store_true")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--image-dir", default=None)
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--merged-name", default=None)
    ap.add_argument("--faction-report", default=None)

    args = ap.parse_args(argv)

    cfg = Config(
        concurrency=max(1, args.concurrency),
        delay=max(0.0, args.delay),
        force=args.force,
        refresh_stale=args.refresh_stale,
        dry_run=args.dry_run,
        skip_images=args.skip_images,
        include_uncollectable=args.include_uncollectable,
    )
    if args.data_dir:
        cfg.data_dir = args.data_dir
    if args.image_dir:
        cfg.image_dir = args.image_dir
    if args.state_file:
        cfg.state_file = args.state_file
    if args.merged_name:
        cfg.merged_name = args.merged_name
    if args.faction_report:
        cfg.faction_report = args.faction_report

    cfg.apply_uncollectable()
    return cfg


def main() -> int:
    cfg = parse_args()
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
