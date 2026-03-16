#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v8.0 — INDIA EDITION
║  Two-Step BC.Game API | India Books | localStorage Bankroll | Clear Markets               ║
║  Dynamic Sport Discovery | All Markets | Fixed Arb Engine | Dual CF Bypass  ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT'S NEW IN v7.0:
  • Dynamic sport list — fetches ALL in-season sports from The Odds API
    automatically instead of a hardcoded list. Catches every sport that has
    active odds right now (soccer, NBA, NHL, tennis, cricket, MMA, golf, etc.)
  • All 4 regions (eu, uk, us, au) fetched per sport for maximum bookmaker
    coverage — more books = more arb opportunities
  • Bookmaker list expanded to 20+ books including all major exchanges
  • Fixed arb engine (v6.1 fixes carried forward):
      - Bug 1: 3-way arbs need >= 2 books (not == 3)
      - Bug 2: strict 2-way/3-way branching — no fake Soccer arbs
      - Anti-palp: rejects arbs > 15% profit (stale/error lines)
  • Dual BC.Game bypass: cloudscraper (Layer 1) + ScraperAPI proxy (Layer 2)
  • Live bankroll input — Kelly stakes update instantly in browser
  • BC Debug tab shows exactly what happened at each URL + layer
"""

import os
import sys
import json
import time
import hashlib
import logging
import warnings
import threading
import urllib.parse
import subprocess
import importlib.util
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ── AUTO-INSTALL cloudscraper if missing ─────────────────────────────────────
if importlib.util.find_spec("cloudscraper") is None:
    print("Installing cloudscraper...")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "cloudscraper", "-q"])

import cloudscraper as _cloudscraper

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("ArbSniper")
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "arb2026")
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()

# ── ARBITRAGE THRESHOLDS ──────────────────────────────────────────────────────
MIN_ARB_PROFIT = 0.05   # 0.05% minimum — catch even razor-thin arbs
MAX_ARB_PROFIT = 15.0   # Anti-palp cap — above 15% is almost certainly an error
MIN_EV_EDGE    = 0.005  # 0.5% edge required to flag as +EV
KELLY_FRACTION = 0.30   # 30% fractional Kelly
DEFAULT_BANK   = 10000  # Rs — overridden live by dashboard input

# ── BC.GAME ENDPOINTS ─────────────────────────────────────────────────────────
# BC.Game sptpub API — two-step format (confirmed working as of 2026):
#   Step 1: GET /en/0       → status dict: {"status": {"hex_id": version, ...}}
#   Step 2: GET /en/{id}    → actual events for that sport/league category
# The hex IDs in status are converted to decimal for the URL path.
BCGAME_BASE    = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en"
BCGAME_DISC    = f"{BCGAME_BASE}/0"       # discovery endpoint
BCGAME_MAX_CAT = 30                        # max categories to fetch per run

# ── BOOKMAKERS — INDIA-ACCESSIBLE ONLY ───────────────────────────────────────
# Only offshore/crypto books accessible from India. US (DraftKings, FanDuel)
# and AU (TAB, Neds) books removed — they are useless to Indian users.
# Pinnacle is ESSENTIAL as the sharp reference for true-odds (EV) calculation.
ALLOWED_BOOKS = {
    "pinnacle",       # Sharp reference — required for EV baseline
    "stake",          # Crypto book, widely used in India
    "bc_game",        # Crypto book, India-accessible
    "onexbet",        # 1xBet — extremely popular in India
    "parimatch",      # India-accessible
    "dafabet",        # Targets Asian/Indian market specifically
    "betway",         # Accepts Indian players
    "bet365",         # Most popular in India (VPN-accessible)
    "marathonbet",    # India-accessible
    "betfair",        # Exchange — best prices for arbing
    "matchbook",      # Exchange — sharp market prices
}

# ── MARKETS TO SCAN ───────────────────────────────────────────────────────────
# h2h = match winner, totals = over/under, spreads = handicap
MARKETS = ["h2h", "totals", "spreads"]

# ── REGIONS ───────────────────────────────────────────────────────────────────
# Fetch all 4 regions so we get the widest bookmaker coverage.
# eu=European books, uk=UK books, us=US books, au=Australian books
REGIONS = "eu,uk,us,au"

# ── SPORTS THAT ARE ALWAYS INCLUDED EVEN IF NOT "IN SEASON" ──────────────────
ALWAYS_INCLUDE_SPORTS = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_turkey_super_league", "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie", "soccer_mexico_ligamx",
    "basketball_nba", "basketball_euroleague", "basketball_ncaab",
    "icehockey_nhl", "icehockey_sweden_hockey_league",
    "americanfootball_nfl", "americanfootball_ncaaf",
    "baseball_mlb",
    "tennis_atp_french_open", "tennis_wta_french_open",
    "tennis_atp_wimbledon", "tennis_wta_wimbledon",
    "tennis_atp_us_open", "tennis_wta_us_open",
    "mma_mixed_martial_arts", "boxing_boxing",
    "cricket_ipl", "cricket_test_match", "cricket_odi", "cricket_t20",
    "golf_masters_tournament_winner", "golf_pga_championship_winner",
    "rugby_union_world_cup", "rugby_league_nrl",
    "aussierules_afl",
}


# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# Thread-safe: always picks the key with the most remaining quota.
# With 19 keys you have up to 19 × 500 = 9,500 API calls available per day.
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self.keys:
            log.warning("ODDS_API_KEYS env var not set!")
        self._lock  = threading.Lock()
        self._quota = {k: 500 for k in self.keys}
        self._used  = {k: 0   for k in self.keys}
        log.info(f"KeyRotator: {len(self.keys)} keys | "
                 f"{self.total_remaining()} total quota")

    def get(self) -> str:
        with self._lock:
            if not self.keys:
                return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int = 0):
        with self._lock:
            self._quota[key] = max(0, remaining)
            self._used[key]  = used

    def mark_exhausted(self, key: str):
        with self._lock:
            self._quota[key] = 0
            log.warning(f"Key ...{key[-6:]} marked exhausted.")

    def total_remaining(self) -> int:
        with self._lock:
            return max(0, sum(self._quota.values()))

    def total_used(self) -> int:
        with self._lock:
            return sum(self._used.values())

    def status(self) -> list:
        with self._lock:
            return [
                {
                    "key":       f"{k[:4]}...{k[-4:]}",
                    "remaining": self._quota.get(k, 0),
                    "used":      self._used.get(k, 0),
                }
                for k in self.keys
            ]


ROTATOR = KeyRotator()


# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {
        "remaining_requests":   500,
        "used_today":           0,
        "last_reset":           str(datetime.now(timezone.utc).date()),
        "total_events_scanned": 0,
        "last_arb_count":       0,
        "last_ev_count":        0,
        "sports_scanned":       0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ═════════════════════════════════════════════════════════════════════════════
# DYNAMIC SPORT DISCOVERY
# Fetches ALL currently in-season sports from The Odds API.
# This is the key to maximum coverage — no more missing sports.
# ═════════════════════════════════════════════════════════════════════════════
def fetch_all_sports() -> list:
    """
    Returns a list of sport keys that currently have active odds.
    Combines: API-discovered in-season sports + ALWAYS_INCLUDE_SPORTS.
    Falls back to ALWAYS_INCLUDE_SPORTS if API call fails.
    """
    key = ROTATOR.get()
    if key == "MISSING_KEY":
        log.warning("No API keys — using hardcoded sport list")
        return sorted(ALWAYS_INCLUDE_SPORTS)

    try:
        r = requests.get(
            f"{ODDS_BASE}/sports",
            params={"apiKey": key, "all": "false"},   # all=false → only in-season
            timeout=15
        )
        # This call costs 0 API credits — it's a free metadata endpoint
        if r.status_code == 200:
            sports_data = r.json()
            active_keys = {s["key"] for s in sports_data if not s.get("has_outrights")}
            # Merge with always-include list
            combined = active_keys | ALWAYS_INCLUDE_SPORTS
            log.info(f"Sports: {len(active_keys)} active from API + "
                     f"{len(ALWAYS_INCLUDE_SPORTS)} hardcoded = "
                     f"{len(combined)} total")
            return sorted(combined)
        else:
            log.warning(f"Sports fetch HTTP {r.status_code} — using hardcoded list")
            return sorted(ALWAYS_INCLUDE_SPORTS)
    except Exception as e:
        log.error(f"Sports fetch error: {e} — using hardcoded list")
        return sorted(ALWAYS_INCLUDE_SPORTS)


# ═════════════════════════════════════════════════════════════════════════════
# ODDS API — CONCURRENT FETCHER
# Fetches EVERY sport × EVERY market combination in parallel.
# Uses key rotation so multiple keys can run simultaneously.
# ═════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    """
    Fetch odds for one sport + one market.
    Automatically selects the key with the most remaining quota.
    """
    key = ROTATOR.get()
    if key == "MISSING_KEY":
        return []
    if ROTATOR._quota.get(key, 0) <= 2:
        log.debug(f"Skipping {sport}/{market} — quota too low")
        return []

    url    = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey":     key,
        "regions":    REGIONS,          # all 4 regions for max book coverage
        "markets":    market,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=15)

        # Always update quota tracker from response headers
        remaining = int(r.headers.get("X-Requests-Remaining",
                                      ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code == 422:
            return []   # Sport not currently available — not an error
        if r.status_code in (429, 401):
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200:
            log.warning(f"HTTP {r.status_code} — {sport}/{market}")
            return []

        data = r.json()
        if not isinstance(data, list):
            return []

        # Filter to only our allowed bookmakers (done locally)
        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", [])
                   if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)

        if filtered:
            log.info(f"  {sport}/{market}: {len(filtered)} events | "
                     f"key ...{key[-6:]} → {remaining} left")
        return filtered

    except requests.exceptions.Timeout:
        log.warning(f"Timeout: {sport}/{market}")
        return []
    except Exception as e:
        log.error(f"Error {sport}/{market}: {e}")
        return []


def fetch_all_odds(state: dict, sports_list: list) -> list:
    """
    Launch concurrent fetches for ALL sports × ALL markets.
    Uses max_workers=3 with 0.3s stagger to prevent 429 floods.
    With 19 keys × 500 quota = 9,500 total calls available.
    """
    if not ROTATOR.keys:
        log.error("No API keys. Set ODDS_API_KEYS in GitHub secrets.")
        return []
    if ROTATOR.total_remaining() <= 0:
        log.error("All API keys exhausted for today.")
        return []

    tasks = [(s, m) for s in sports_list for m in MARKETS]
    log.info(f"Launching {len(tasks)} fetches "
             f"({len(sports_list)} sports × {len(MARKETS)} markets) "
             f"across {len(ROTATOR.keys)} keys...")

    all_events = []
    seen_ids   = set()   # deduplicate events that appear in multiple regions

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {}
        for s, m in tasks:
            futures[ex.submit(fetch_sport_odds, s, m)] = (s, m)
            time.sleep(0.3)   # stagger submissions to spread API load

        for fut in as_completed(futures):
            try:
                results = fut.result()
                for ev in results:
                    ev_id = ev.get("id", "")
                    mkt   = futures[fut][1]
                    key   = f"{ev_id}_{mkt}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        all_events.append(ev)
            except Exception as e:
                log.error(f"Future error: {e}")

    state["remaining_requests"]   = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    state["sports_scanned"]       = len(sports_list)
    log.info(f"Total events collected: {len(all_events)} "
             f"(from {len(sports_list)} sports)")
    return all_events


# ═════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER — CONFIRMED SCHEMA v8.0
#
# API architecture (confirmed by live testing):
#   GET /en/0
#     → {top_events_versions:[int], rest_events_versions:[int,...], status:{...}}
#   GET /en/{chunk_id}   (chunk_id = large timestamp int from versions lists)
#     → {sports:{}, categories:{}, tournaments:{}, events:{}}
#
# Event schema (ev["desc"] holds all metadata):
#   desc.type        : "match" | "tournament" | "stage"
#   desc.competitors : [{name, qualifier?, sport_id}]
#   desc.scheduled   : Unix timestamp in SECONDS
#   desc.sport       : sport_id string
#   desc.tournament  : tournament_id string
#
# Market schema (ev["markets"]):
#   "11"  + line="" or "0"  → H2H Match Winner
#   "223" + line="hcp=X"    → Handicap / Spread
#   "202" + line="setnr=X"  → Set/Game Totals (Over/Under)
#   "534"                   → Outright winner (skip)
#
# odds coefficient in sel_data["k"] (string, must float())
# sel_ids = "tt:outcometext:..." strings (positional naming used)
#
# User-Agent: MUST be "insomnia/12.4.0" — browser UA returns 403 on chunks
# ═════════════════════════════════════════════════════════════════════════════
BC_DEBUG = {
    "status":         "not_tried",
    "chunks_total":   0,
    "chunks_fetched": 0,
    "events_raw":     0,
    "matches_parsed": 0,
    "outrights_skip": 0,
    "no_teams_skip":  0,
    "no_odds_skip":   0,
    "raw_preview":    "",
}

# BC.Game host — NOT behind Cloudflare, insomnia UA works directly
_BC_HOST    = "api-k-c7818b61-623.sptpub.com"
_BC_HEADERS = {"User-Agent": "insomnia/12.4.0", "Accept": "application/json"}


def _bc_fetch(path: str) -> dict | None:
    """
    Low-level fetch using http.client (stdlib only — no requests needed).
    Handles gzip-compressed chunks automatically.
    Falls back to ScraperAPI proxy if direct fetch fails.
    """
    import http.client, zlib, gzip, io

    def _parse(raw: bytes) -> dict | None:
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        try:
            return json.loads(zlib.decompress(raw, 16 + zlib.MAX_WBITS).decode("utf-8"))
        except Exception:
            pass
        try:
            return json.loads(gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8"))
        except Exception:
            pass
        return None

    # ── Direct (confirmed working — sptpub.com has no Cloudflare) ─────────────
    try:
        conn = http.client.HTTPSConnection(_BC_HOST, timeout=20)
        conn.request("GET", path, "", _BC_HEADERS)
        res = conn.getresponse()
        raw = res.read()
        if res.status == 200:
            parsed = _parse(raw)
            if parsed is not None:
                return parsed
        log.debug(f"BC direct HTTP {res.status} for {path[-40:]}")
    except Exception as e:
        log.debug(f"BC direct exception: {e}")

    # ── ScraperAPI fallback (if SCRAPERAPI_KEY is set) ─────────────────────────
    if SCRAPERAPI_KEY:
        try:
            proxy = (f"http://scraperapi:{SCRAPERAPI_KEY}"
                     f"@proxy-server.scraperapi.com:8001")
            r = requests.get(
                f"https://{_BC_HOST}{path}",
                proxies={"http": proxy, "https": proxy},
                headers=_BC_HEADERS,
                verify=False, timeout=45,
            )
            if r.status_code == 200:
                parsed = _parse(r.content)
                if parsed is not None:
                    return parsed
        except Exception as e:
            log.debug(f"BC ScraperAPI exception: {e}")

    return None


def _bc_sport_name(sport_id: str, all_sports: dict) -> str:
    return all_sports.get(str(sport_id), {}).get("name", f"sport_{sport_id}")


def _bc_league_name(tourn_id: str, all_tourns: dict) -> str:
    return all_tourns.get(str(tourn_id), {}).get("name", "")


def _bc_parse_teams(desc: dict) -> tuple[str, str]:
    """Extract home/away names from desc.competitors list."""
    comps = desc.get("competitors", [])
    home = away = ""
    for c in comps:
        q = str(c.get("qualifier", c.get("q", ""))).lower()
        n = c.get("name", "")
        if q in ("home", "1", "h"):   home = n
        elif q in ("away", "2", "a"): away = n
    # Positional fallback for 2-team events with no qualifier
    if (not home or not away) and len(comps) == 2:
        home = comps[0].get("name", "")
        away = comps[1].get("name", "")
    return home, away


def _bc_parse_h2h(markets: dict) -> list:
    """
    Market "11" with empty or "0" line key = H2H Match Winner.
    2 outcomes → [Home, Away]      (no-draw: tennis, basketball, etc.)
    3 outcomes → [Home, Draw, Away] (soccer, etc.)
    sel_ids are tt:outcometext:... strings — use positional naming.
    """
    if "11" not in markets:
        return []
    for line_key, sels in markets["11"].items():
        if line_key not in ("", "0"):
            continue
        if not isinstance(sels, dict):
            continue
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict):
                continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01:
                    prices.append(round(p, 3))
            except (ValueError, TypeError):
                pass
        if len(prices) == 2:
            return [{"name": "Home", "price": prices[0]},
                    {"name": "Away", "price": prices[1]}]
        if len(prices) == 3:
            return [{"name": "Home", "price": prices[0]},
                    {"name": "Draw", "price": prices[1]},
                    {"name": "Away", "price": prices[2]}]
    return []


def _bc_parse_handicap(markets: dict) -> list:
    """
    Market "223" + line "hcp=X" → Handicap/Spread.
    Returns list of outcome dicts (best line only).
    """
    if "223" not in markets:
        return []
    results = []
    for line_key, sels in markets["223"].items():
        if not line_key.startswith("hcp="):
            continue
        if not isinstance(sels, dict):
            continue
        hcp = line_key.replace("hcp=", "")
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict):
                continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01:
                    prices.append(round(p, 3))
            except (ValueError, TypeError):
                pass
        if len(prices) == 2:
            results.append({
                "name":  f"Home {hcp}",
                "price": prices[0],
                "point": float(hcp) if hcp.lstrip("-").replace(".","").isdigit() else 0,
            })
            results.append({
                "name":  f"Away {hcp}",
                "price": prices[1],
                "point": -float(hcp) if hcp.lstrip("-").replace(".","").isdigit() else 0,
            })
    return results[:4]   # max 2 lines × 2 sides


def _bc_parse_totals(markets: dict) -> list:
    """
    Market "202" + any line → Set/Game Totals (Over/Under).
    """
    if "202" not in markets:
        return []
    results = []
    for line_key, sels in markets["202"].items():
        if not isinstance(sels, dict):
            continue
        line_label = line_key.replace("setnr=", "") if "setnr=" in line_key else line_key
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict):
                continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01:
                    prices.append(round(p, 3))
            except (ValueError, TypeError):
                pass
        if len(prices) == 2:
            results.append({"name": f"Over {line_label}",  "price": prices[0]})
            results.append({"name": f"Under {line_label}", "price": prices[1]})
    return results[:4]   # max 2 lines × 2 sides


def fetch_bcgame_events() -> list:
    """
    Fetch BC.Game pre-match events using confirmed two-step architecture.

    Step 1: GET /en/0  → manifest with chunk IDs in top/rest_events_versions
    Step 2: GET /en/{chunk_id} for each chunk → stitch relational tables
    Step 3: Parse each event using confirmed desc + markets schema
    """
    global BC_DEBUG

    brand_id = "2103509236163162112"
    base     = f"/api/v4/prematch/brand/{brand_id}/en"

    # ── Step 1: Manifest ───────────────────────────────────────────────────────
    log.info("BC.Game: fetching manifest /en/0 ...")
    manifest = _bc_fetch(f"{base}/0")
    if not manifest:
        BC_DEBUG["status"]      = "manifest_failed"
        BC_DEBUG["raw_preview"] = "Manifest /en/0 returned nothing."
        log.warning("BC.Game: manifest fetch failed")
        return []

    top_ids  = manifest.get("top_events_versions",  [])
    rest_ids = manifest.get("rest_events_versions", [])
    all_ids  = top_ids + rest_ids
    BC_DEBUG["chunks_total"] = len(all_ids)
    BC_DEBUG["raw_preview"]  = (f"top={top_ids[:2]}  rest={rest_ids[:3]}  "
                                f"total={len(all_ids)} chunks")
    log.info(f"BC.Game manifest: {len(top_ids)} top + {len(rest_ids)} rest "
             f"= {len(all_ids)} chunks")

    if not all_ids:
        BC_DEBUG["status"] = "no_chunk_ids"
        return []

    # ── Step 2: Fetch all chunks and stitch ────────────────────────────────────
    all_sports = {}; all_cats = {}; all_tourns = {}; all_events = {}
    fetched = 0

    for chunk_id in all_ids:
        chunk = _bc_fetch(f"{base}/{chunk_id}")
        if not chunk:
            continue
        all_sports.update(chunk.get("sports",      {}))
        all_cats.update(  chunk.get("categories",  {}))
        all_tourns.update(chunk.get("tournaments", {}))
        all_events.update(chunk.get("events",      {}))
        fetched += 1
        log.debug(f"BC chunk {chunk_id}: +{len(chunk.get('events',{}))} events")

    BC_DEBUG["chunks_fetched"] = fetched
    BC_DEBUG["events_raw"]     = len(all_events)
    log.info(f"BC.Game stitched: {len(all_sports)} sports  "
             f"{len(all_tourns)} leagues  {len(all_events)} events")

    if not all_events:
        BC_DEBUG["status"] = "no_events_in_chunks"
        return []

    # ── Step 3: Parse events ───────────────────────────────────────────────────
    converted = []
    skip_out = skip_teams = skip_odds = 0

    for ev_id, ev in all_events.items():
        desc    = ev.get("desc", {})
        ev_type = desc.get("type", "match")

        # Skip outrights (tournament/stage winner markets)
        if ev_type not in ("match", "game", ""):
            skip_out += 1
            continue

        home, away = _bc_parse_teams(desc)
        if not home or not away:
            skip_teams += 1
            continue

        markets  = ev.get("markets", {})
        h2h      = _bc_parse_h2h(markets)
        handicap = _bc_parse_handicap(markets)
        totals   = _bc_parse_totals(markets)

        # Build all_outcomes — used for merge with Odds API events
        all_outcomes = h2h or []
        if not all_outcomes:
            skip_odds += 1
            continue

        sid   = desc.get("sport",      "")
        tid   = desc.get("tournament", "")
        ts    = desc.get("scheduled",  "")
        sport = _bc_sport_name(sid, all_sports)
        lg    = _bc_league_name(tid, all_tourns)

        # Format start time (scheduled is in SECONDS, not ms)
        try:
            ts_val = float(ts)
            start  = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
        except Exception:
            start = str(ts)

        # Build bookmaker entry in standard Odds API format
        mkt_list = []
        if h2h:
            mkt_list.append({"key": "h2h", "outcomes": h2h})
        if handicap:
            mkt_list.append({"key": "spreads", "outcomes": handicap})
        if totals:
            mkt_list.append({"key": "totals", "outcomes": totals})

        converted.append({
            "id":            f"bcgame_{ev_id}",
            "sport_title":   sport,
            "sport_key":     f"bcgame_{sport.lower().replace(' ', '_')}",
            "home_team":     home,
            "away_team":     away,
            "commence_time": start,
            "bookmakers": [{
                "key":         "bc_game",
                "title":       "BC.Game",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets":     mkt_list,
            }],
        })

    BC_DEBUG["matches_parsed"] = len(converted)
    BC_DEBUG["outrights_skip"] = skip_out
    BC_DEBUG["no_teams_skip"]  = skip_teams
    BC_DEBUG["no_odds_skip"]   = skip_odds
    BC_DEBUG["status"]         = f"ok_{len(converted)}_matches" if converted else "ok_but_no_h2h"

    log.info(f"BC.Game ✅ {len(converted)} matches  "
             f"(skipped: {skip_out} outrights, {skip_teams} no-teams, "
             f"{skip_odds} no-h2h-odds)")
    return converted


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def merge_bcgame(odds_events: list, bc_events: list) -> list:
    """Fuzzy-merge BC.Game events into Odds API events by team name."""
    merged = 0
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (similarity(bh, ev.get("home_team", "")) +
                 similarity(ba, ev.get("away_team", ""))) / 2
            if s > best_score:
                best_score, best_ev = s, ev
        if best_score > 0.72 and best_ev:
            best_ev["bookmakers"].extend(bc_ev["bookmakers"])
            merged += 1
        else:
            odds_events.append(bc_ev)
    log.info(f"BC.Game merge: {merged} integrated, "
             f"{len(bc_events) - merged} standalone added.")
    return odds_events


# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE
# ═════════════════════════════════════════════════════════════════════════════
def remove_vig(outcomes: list) -> dict:
    """
    Pinnacle multiplicative vig removal → {name: true_probability}.
    Steps:
      1. raw_prob = 1 / decimal_odds   (this includes the vig)
      2. total    = sum(all raw_probs)  (> 1.0, the excess is the margin)
      3. true_prob = raw_prob / total   (now sums to exactly 1.0)
    """
    raw = {}
    for o in outcomes:
        try:
            raw[o["name"]] = 1.0 / float(o["price"])
        except (ValueError, TypeError, KeyError):
            pass
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def kelly_stake(edge: float, odds: float, bank: float) -> float:
    """30% fractional Kelly Criterion stake."""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    p  = 1.0 / (odds / (1.0 + edge))
    kf = (b * p - (1.0 - p)) / b
    if kf <= 0:
        return 0.0
    return round(KELLY_FRACTION * kf * bank, 2)


def round10(x: float) -> float:
    """Round stake to nearest Rs 10 for stealth placement."""
    return round(round(x / 10) * 10, 2)


def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    """Calculate individual leg stakes for a guaranteed-profit arb."""
    impl = sum(1.0 / o for o in odds_list)
    if impl >= 1.0:
        return [0.0] * len(odds_list)
    return [(1.0 / o) / impl * total for o in odds_list]


# ═════════════════════════════════════════════════════════════════════════════
# ARBITRAGE SCANNER — FULLY FIXED ENGINE
#
# FIX 1: 3-way arbs need >= 2 distinct books (not == 3)
# FIX 2: Strict branching — 2-way markets ONLY get 2-way logic,
#         3-way markets ONLY get 3-way logic (no fake Soccer arbs)
# FIX 3: Anti-palp cap at 15% — rejects stale/error lines
# ═════════════════════════════════════════════════════════════════════════════
def _best_price_per_book(bookmakers: list, market_key: str) -> dict:
    """
    Returns {outcome_name: {book_key: (price, title)}} for one market.
    Outcome names are normalised for totals/spreads by appending abs(point).
    Example: "Over" at point 2.5 becomes "Over_2.5"
    This is critical to avoid pairing Over_2.5 with Under_3.0 as an "arb".
    """
    best: dict = {}
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for o in mkt.get("outcomes", []):
                raw_name = str(o.get("name", ""))
                pt       = o.get("point")
                try:
                    price = float(o.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price <= 1.01:
                    continue

                if pt is not None:
                    try:
                        name = f"{raw_name}_{abs(float(pt))}"
                    except (ValueError, TypeError):
                        name = raw_name
                else:
                    name = raw_name

                bk  = bm.get("key", "?")
                ttl = bm.get("title", "?")
                if name not in best:
                    best[name] = {}
                if bk not in best[name] or price > best[name][bk][0]:
                    best[name][bk] = (price, ttl)
    return best


def _build_arb_record(combo: list, mkey: str, sport: str,
                      match: str, com: str):
    """
    combo = list of (outcome_name, price, book_title, book_key)
    Returns arb dict if profitable and within anti-palp range, else None.
    """
    prices = [x[1] for x in combo]
    impl   = sum(1.0 / p for p in prices)
    if impl >= 1.0:
        return None
    pct = (1.0 / impl - 1.0) * 100
    if pct < MIN_ARB_PROFIT:
        return None
    if pct > MAX_ARB_PROFIT:
        log.debug(f"Anti-palp: {pct:.2f}% rejected on {match}/{mkey}")
        return None
    stakes = calc_stakes(prices)
    return {
        "ways":       len(combo),
        "market":     mkey.upper(),
        "sport":      sport,
        "match":      match,
        "commence":   com,
        "profit_pct": round(pct, 3),
        "profit_amt": round((1.0 / impl - 1.0) * 1000, 2),
        "outcomes": [{
            "name":          x[0],
            "odds":          round(x[1], 3),
            "book":          x[2],
            "book_key":      x[3],
            "stake":         round(s, 2),
            "stake_rounded": round10(s),
        } for x, s in zip(combo, stakes)],
    }


def _scan_h2h(best: dict, sport: str, match: str, com: str) -> list:
    """
    STRICT BRANCHING:
    - 2 outcomes → tennis/mma/nba etc. → ONLY 2-way combinations
    - 3 outcomes → soccer/rugby etc.  → ONLY 3-way combinations
    - other count → skip

    FIX 1: 3-way only needs >= 2 distinct books (was wrongly == 3)
    FIX 2: 3-way market NEVER produces 2-way combos (was catastrophically wrong)
    """
    arbs  = []
    names = list(best.keys())

    if len(names) == 2:
        # ── Pure 2-way market (Tennis, MMA, NBA, etc.) ─────────────────────
        n1, n2 = names[0], names[1]
        bk1, bk2 = best[n1], best[n2]
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                if bk_a == bk_b:
                    continue
                rec = _build_arb_record(
                    [(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)],
                    "h2h", sport, match, com
                )
                if rec:
                    if (best_rec is None or
                            rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break   # best_bk_b found for this bk_a
        if best_rec:
            arbs.append(best_rec)

    elif len(names) == 3:
        # ── 3-way market (Soccer, Rugby, etc.) ─────────────────────────────
        # Try ALL cross-product book combinations for the 3 legs.
        # Keep only the best profitable one.
        n1, n2, n3 = names[0], names[1], names[2]
        bk1, bk2, bk3 = best[n1], best[n2], best[n3]
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                for bk_c, (p_c, t_c) in sorted(bk3.items(),
                                                key=lambda x: -x[1][0]):
                    # FIX 1: >= 2 distinct books is enough (not 3)
                    if len({bk_a, bk_b, bk_c}) < 2:
                        continue
                    rec = _build_arb_record(
                        [(n1, p_a, t_a, bk_a),
                         (n2, p_b, t_b, bk_b),
                         (n3, p_c, t_c, bk_c)],
                        "h2h", sport, match, com
                    )
                    if rec:
                        if (best_rec is None or
                                rec["profit_pct"] > best_rec["profit_pct"]):
                            best_rec = rec
        if best_rec:
            arbs.append(best_rec)
    # Any other outcome count → skip (ambiguous / partial market)
    return arbs


def _scan_totals(best: dict, sport: str, match: str, com: str) -> list:
    """
    Totals: 'Over_2.5', 'Under_2.5', 'Over_3.0', ...
    ONLY pair Over_X vs Under_X where X is IDENTICAL.
    Pairing Over_2.5 vs Under_3.0 is NOT a valid arb — different lines.
    """
    arbs   = []
    points: dict = {}   # "2.5" → {"Over": {bk: (p, t)}, "Under": {bk: (p, t)}}

    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2:
            continue
        side  = parts[0]              # "Over" or "Under"
        point = "_".join(parts[1:])   # "2.5" or "224.5" etc.
        points.setdefault(point, {})
        points[point].setdefault(side, {})
        points[point][side].update(bk_prices)

    for point, sides in points.items():
        if "Over" not in sides or "Under" not in sides:
            continue
        over_bks  = sides["Over"]
        under_bks = sides["Under"]
        best_rec  = None
        for bk_o, (p_o, t_o) in sorted(over_bks.items(),
                                        key=lambda x: -x[1][0]):
            for bk_u, (p_u, t_u) in sorted(under_bks.items(),
                                            key=lambda x: -x[1][0]):
                if bk_o == bk_u:
                    continue
                rec = _build_arb_record(
                    [(f"Over {point}", p_o, t_o, bk_o),
                     (f"Under {point}", p_u, t_u, bk_u)],
                    "totals", sport, match, com
                )
                if rec:
                    if (best_rec is None or
                            rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break
        if best_rec:
            arbs.append(best_rec)
    return arbs


def _scan_spreads(best: dict, sport: str, match: str, com: str) -> list:
    """
    Spreads: 'TeamA_1.5', 'TeamB_1.5' — same abs(point), opposite sides.
    Group by point value → pair exactly 2 sides from different books.
    """
    arbs         = []
    point_groups: dict = {}   # "1.5" → [(name, {bk: (p, t)}), ...]

    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2:
            continue
        point = "_".join(parts[1:])
        point_groups.setdefault(point, []).append((name, bk_prices))

    for point, group in point_groups.items():
        if len(group) != 2:
            continue   # need exactly 2 opposing sides
        (n1, bk1), (n2, bk2) = group
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                if bk_a == bk_b:
                    continue
                rec = _build_arb_record(
                    [(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)],
                    "spreads", sport, match, com
                )
                if rec:
                    if (best_rec is None or
                            rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break
        if best_rec:
            arbs.append(best_rec)
    return arbs


def scan_arbitrage(events: list) -> list:
    seen: set  = set()
    arbs: list = []

    for ev in events:
        home  = ev.get("home_team", "?")
        away  = ev.get("away_team", "?")
        sport = ev.get("sport_title", "Unknown")
        com   = ev.get("commence_time", "")
        match = f"{home} vs {away}"

        for mkey in MARKETS:
            best = _best_price_per_book(ev.get("bookmakers", []), mkey)
            if not best:
                continue

            if mkey == "h2h":
                candidates = _scan_h2h(best, sport, match, com)
            elif mkey == "totals":
                candidates = _scan_totals(best, sport, match, com)
            elif mkey == "spreads":
                candidates = _scan_spreads(best, sport, match, com)
            else:
                candidates = []

            for c in candidates:
                bk_set = frozenset(o["book_key"] for o in c["outcomes"])
                key    = (match, mkey, bk_set)
                if key in seen:
                    continue
                seen.add(key)
                arbs.append(c)

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    log.info(f"Arbitrage: {len(arbs)} genuine opportunities found.")
    return arbs[:300]   # cap at 300 to keep HTML manageable


# ═════════════════════════════════════════════════════════════════════════════
# EV SCANNER
# ═════════════════════════════════════════════════════════════════════════════
def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        home  = ev.get("home_team", "?")
        away  = ev.get("away_team", "?")
        sport = ev.get("sport_title", "Unknown")
        com   = ev.get("commence_time", "")

        for mkey in MARKETS:
            # Find Pinnacle lines (our reference for true odds)
            pin_out = None
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    for m in bm.get("markets", []):
                        if m.get("key") == mkey:
                            pin_out = m.get("outcomes", [])
            if not pin_out or len(pin_out) < 2:
                continue

            true_probs = remove_vig(pin_out)
            if not true_probs:
                continue

            # Compare every soft book against Pinnacle's de-vigged true odds
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey:
                        continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        if name not in true_probs:
                            continue
                        try:
                            price = float(o["price"])
                        except (ValueError, TypeError, KeyError):
                            continue
                        if price <= 1.0:
                            continue

                        tp   = true_probs[name]
                        to   = 1.0 / tp
                        edge = (price - to) / to

                        if edge >= MIN_EV_EDGE:
                            ks = kelly_stake(edge, price, DEFAULT_BANK)
                            bets.append({
                                "market":              mkey.upper(),
                                "sport":               sport,
                                "match":               f"{home} vs {away}",
                                "commence":            com,
                                "outcome":             name,
                                "book":                bm.get("title", "?"),
                                "book_key":            bm.get("key", "?"),
                                "offered_odds":        round(price, 3),
                                "true_odds":           round(to, 3),
                                "true_prob_pct":       round(tp * 100, 2),
                                "edge_pct":            round(edge * 100, 3),
                                "kelly_stake":         ks,
                                "kelly_stake_rounded": round10(ks),
                            })

    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"EV scan: {len(bets)} value bets found.")
    return bets[:500]


# ═════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs:
        return
    if arbs:
        t   = arbs[0]
        msg = (f"ARB: {t['match']} | +{t['profit_pct']}% | "
               f"{t['ways']}-way {t['market']} | {len(evs)} EV bets")
    else:
        t   = evs[0]
        msg = (f"EV: {t['match']} | +{t['edge_pct']}% edge | "
               f"{t['book']} | {len(evs)} total")
    try:
        r = requests.post(
            NTFY_URL, data=msg.encode("utf-8"),
            headers={
                "Title":        "Arb Sniper Alert",
                "Priority":     "high",
                "Tags":         "zap,moneybag",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        log.info(f"Push: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Push failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR — PREMIUM DARK UI
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list,
                  state: dict, key_status: list,
                  bc_debug: dict, sports_count: int) -> str:

    IST     = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    ph      = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()
    total_q = sum(k["remaining"] for k in key_status)

    arbs_j  = json.dumps(arbs,       ensure_ascii=False)
    evs_j   = json.dumps(evs,        ensure_ascii=False)
    bc_j    = json.dumps(raw_bc,     ensure_ascii=False)
    keys_j  = json.dumps(key_status, ensure_ascii=False)
    debug_j = json.dumps(bc_debug,   ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v7.0 ⚡</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@700;800&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
:root{{
  --bg:#0c0c0e;--bg1:#111115;--bg2:#17171d;--bg3:#1e1e26;--bg4:#26262f;
  --border:#2a2a35;--border2:#35353f;
  --cyan:#22d3ee;--cyan2:#0ea5e9;
  --green:#4ade80;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa;--orange:#fb923c;
  --txt:#e8e8f0;--txt2:#9898aa;--txt3:#5a5a6a;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;--disp:'Syne',sans-serif;
}}
html,body{{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--txt)}}
::-webkit-scrollbar{{width:3px;height:3px}}
::-webkit-scrollbar-thumb{{background:var(--bg4);border-radius:2px}}

/* ── LOCK ──────────────────────────────────── */
#lock{{
  position:fixed;inset:0;z-index:9999;background:var(--bg);
  display:flex;align-items:center;justify-content:center;
  background:radial-gradient(ellipse 80% 60% at 50% 40%,rgba(34,211,238,0.06) 0%,transparent 65%);
}}
.lbox{{
  width:90%;max-width:340px;background:var(--bg2);border:1px solid var(--border2);
  border-radius:20px;padding:36px 32px;
  display:flex;flex-direction:column;align-items:center;gap:18px;
  box-shadow:0 0 60px rgba(34,211,238,0.06),0 24px 80px rgba(0,0,0,0.6);
  animation:lockIn .5s cubic-bezier(.16,1,.3,1) both;
}}
@keyframes lockIn{{from{{opacity:0;transform:scale(.94) translateY(16px)}}to{{opacity:1;transform:none}}}}
.lock-icon{{
  width:52px;height:52px;border-radius:14px;
  background:linear-gradient(135deg,rgba(34,211,238,.15),rgba(167,139,250,.15));
  border:1px solid rgba(34,211,238,.25);display:flex;align-items:center;justify-content:center;
  font-size:22px;color:var(--cyan);animation:iconPulse 2.5s ease-in-out infinite;
}}
@keyframes iconPulse{{0%,100%{{box-shadow:0 0 0 0 rgba(34,211,238,.2)}}50%{{box-shadow:0 0 0 8px rgba(34,211,238,0)}}}}
.lock-title{{font-family:var(--disp);font-size:20px;font-weight:800;letter-spacing:3px;color:var(--txt)}}
.lock-sub{{font-size:10px;color:var(--txt3);letter-spacing:2px;text-transform:uppercase}}
#linput{{
  width:100%;padding:13px 16px;font-size:18px;text-align:center;letter-spacing:8px;
  background:var(--bg3);border:1px solid var(--border2);border-radius:10px;
  color:var(--txt);font-family:var(--mono);outline:none;
  transition:border-color .2s,box-shadow .2s;
}}
#linput:focus{{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(34,211,238,.12)}}
#lbtn{{
  width:100%;padding:13px;font-size:12px;font-weight:700;letter-spacing:2px;
  cursor:pointer;border:none;font-family:var(--disp);
  background:linear-gradient(135deg,var(--cyan),var(--cyan2));color:#000;
  border-radius:10px;transition:opacity .2s,transform .15s;
}}
#lbtn:hover{{opacity:.88;transform:translateY(-1px)}}
#lerr{{font-size:11px;color:var(--red);display:none;letter-spacing:.5px}}

/* ── APP ──────────────────────────────────── */
#app{{display:none;width:100%;height:100vh;overflow-y:auto;background:var(--bg)}}
#app::before{{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 60% 50% at 15% 10%,rgba(34,211,238,.03) 0%,transparent 60%),
    radial-gradient(ellipse 50% 40% at 85% 85%,rgba(167,139,250,.03) 0%,transparent 60%);
}}

/* ── TOPBAR ──────────────────────────────── */
.topbar{{
  position:sticky;top:0;z-index:100;height:52px;padding:0 20px;
  background:rgba(12,12,14,.9);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}}
.logo{{
  font-family:var(--disp);font-size:15px;font-weight:800;letter-spacing:1px;
  display:flex;align-items:center;gap:8px;
  background:linear-gradient(135deg,var(--cyan),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}}
.topbar-r{{display:flex;align-items:center;gap:10px}}
.lpill{{
  display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;
  background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.22);
}}
.ldot{{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:ldot 1.4s infinite}}
@keyframes ldot{{0%,100%{{opacity:1}}50%{{opacity:.15}}}}
.ltext{{font-size:10px;color:var(--green);font-weight:700;letter-spacing:1.5px;font-family:var(--mono)}}
.ttxt{{font-size:10px;color:var(--txt3);font-family:var(--mono)}}
.ibtn{{background:none;border:none;cursor:pointer;color:var(--txt3);font-size:14px;padding:4px;transition:all .2s}}
.ibtn:hover{{color:var(--red);transform:scale(1.1)}}

/* ── BANKROLL BAR ────────────────────────── */
.bkbar{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  padding:9px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
}}
.bklabel{{font-size:11px;color:var(--txt3);font-weight:600;letter-spacing:1px;text-transform:uppercase;white-space:nowrap}}
.bkinput{{
  background:var(--bg3);border:1px solid var(--border2);border-radius:8px;
  color:var(--cyan);font-family:var(--mono);font-size:14px;font-weight:700;
  padding:7px 12px;outline:none;width:150px;transition:border-color .2s;
}}
.bkinput:focus{{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(34,211,238,.1)}}
.bknote{{font-size:10px;color:var(--txt3);font-family:var(--mono)}}

/* ── STATS BAR ───────────────────────────── */
.sbar{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  padding:9px 20px;display:flex;gap:7px;overflow-x:auto;scrollbar-width:none;
}}
.sbar::-webkit-scrollbar{{display:none}}
.ss{{
  background:var(--bg3);border:1px solid var(--border);border-radius:10px;
  padding:8px 13px;min-width:88px;flex-shrink:0;
  transition:transform .2s,border-color .2s;cursor:default;
}}
.ss:hover{{transform:translateY(-2px);border-color:var(--border2)}}
.ssl{{font-size:8px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--txt3);margin-bottom:4px}}
.ssv{{font-size:15px;font-weight:800;font-family:var(--mono)}}

/* ── TABS ────────────────────────────────── */
.tabs{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  display:flex;gap:2px;padding:0 20px;overflow-x:auto;scrollbar-width:none;
}}
.tabs::-webkit-scrollbar{{display:none}}
.tab{{
  padding:11px 13px;font-size:11px;font-weight:600;cursor:pointer;
  color:var(--txt3);background:none;border:none;white-space:nowrap;
  display:flex;align-items:center;gap:6px;
  border-bottom:2px solid transparent;transition:all .2s;font-family:var(--sans);
}}
.tab:hover{{color:var(--txt2)}}
.tab.act{{color:var(--cyan);border-bottom-color:var(--cyan)}}
.tbadge{{
  font-size:9px;padding:1px 6px;border-radius:8px;font-weight:800;
  background:var(--bg4);color:var(--txt2);font-family:var(--mono);
}}
.tab.act .tbadge{{background:rgba(34,211,238,.15);color:var(--cyan)}}

/* ── TAB CONTENT ─────────────────────────── */
.tc{{display:none;padding:14px 20px 60px;position:relative;z-index:1}}
.tc.act{{display:block}}

/* ── FILTER BAR ──────────────────────────── */
.fbar{{display:flex;gap:8px;margin-bottom:13px;flex-wrap:wrap;align-items:center}}
.fi,.fs{{
  background:var(--bg3);border:1px solid var(--border);border-radius:8px;
  color:var(--txt);font-family:var(--sans);font-size:11px;
  padding:8px 12px;outline:none;transition:border-color .2s;
}}
.fi{{flex:1;min-width:140px}} .fs{{min-width:110px;cursor:pointer}}
.fi:focus,.fs:focus{{border-color:var(--cyan)}}
.fpill{{
  display:flex;align-items:center;gap:7px;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:7px 12px;font-size:10px;color:var(--txt2);white-space:nowrap;
}}
input[type=range]{{accent-color:var(--cyan);width:80px;cursor:pointer}}
.fs option{{background:var(--bg2)}}

/* ── GRID ────────────────────────────────── */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}}

/* ── CARD ────────────────────────────────── */
.card{{
  background:var(--bg2);border:1px solid var(--border);border-radius:14px;
  overflow:hidden;position:relative;
  transition:transform .25s,border-color .25s,box-shadow .25s;
  animation:cardIn .4s cubic-bezier(.16,1,.3,1) both;
}}
.card:hover{{transform:translateY(-3px);border-color:var(--border2);box-shadow:0 8px 32px rgba(0,0,0,.4)}}
@keyframes cardIn{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:none}}}}
.card:nth-child(1){{animation-delay:0s}}.card:nth-child(2){{animation-delay:.04s}}
.card:nth-child(3){{animation-delay:.08s}}.card:nth-child(4){{animation-delay:.12s}}
.card:nth-child(5){{animation-delay:.16s}}.card:nth-child(n+6){{animation-delay:.2s}}
.cbar{{height:2px;background:linear-gradient(90deg,var(--cyan),var(--purple))}}
.cbar.ev{{background:linear-gradient(90deg,var(--purple),var(--cyan))}}
.cbar.bc{{background:linear-gradient(90deg,var(--yellow),var(--green))}}
.cinn{{padding:14px 14px 0}}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}}
.cbdg{{
  font-size:9px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;
  padding:3px 7px;border-radius:5px;font-family:var(--mono);
  background:rgba(34,211,238,.1);color:var(--cyan);border:1px solid rgba(34,211,238,.2);
}}
.cbdg.ev{{background:rgba(167,139,250,.1);color:var(--purple);border-color:rgba(167,139,250,.2)}}
.cbdg.bc{{background:rgba(251,191,36,.1);color:var(--yellow);border-color:rgba(251,191,36,.2)}}
.cpft{{font-size:20px;font-weight:800;color:var(--green);font-family:var(--mono)}}
.cpft.ev{{color:var(--purple)}}
.cmatch{{font-size:13px;font-weight:600;color:var(--txt);margin-bottom:6px;line-height:1.3}}
.cmeta{{font-size:10px;color:var(--txt3);margin-bottom:10px;display:flex;gap:10px;flex-wrap:wrap}}
.ctbl{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:10px;font-family:var(--mono)}}
.ctbl th{{
  color:var(--txt3);text-align:left;padding:0 6px 7px;border-bottom:1px solid var(--border);
  font-weight:500;font-size:9px;letter-spacing:1.5px;text-transform:uppercase;font-family:var(--sans);
}}
.ctbl td{{padding:6px 6px;border-bottom:1px solid rgba(42,42,53,.8)}}
.ctbl tr:last-child td{{border:none}}
.btag{{background:var(--bg4);border:1px solid var(--border);border-radius:4px;padding:2px 5px;font-size:9px;font-weight:700;color:var(--txt2)}}
.oval{{color:var(--yellow);font-weight:700}}
.stkx{{font-size:9px;color:var(--txt3);display:block}}
.stkm{{font-size:13px;font-weight:700;color:var(--txt)}}
.stkm.kly{{color:var(--cyan);font-size:12px}}
.cfoot{{display:flex;justify-content:space-between;align-items:center;padding:8px 14px 10px;border-top:1px solid var(--border)}}
.cfl{{font-size:10px;color:var(--txt3)}}
.cbtn{{
  background:none;border:1px solid var(--border2);border-radius:6px;
  color:var(--txt2);font-family:var(--mono);font-size:10px;padding:4px 10px;
  cursor:pointer;transition:all .2s;font-weight:500;
}}
.cbtn:hover{{border-color:var(--cyan);color:var(--cyan)}}
.empty{{text-align:center;padding:60px 20px;color:var(--txt3);grid-column:1/-1}}
.empty i{{font-size:30px;display:block;margin-bottom:12px;opacity:.3}}
.empty small{{font-size:11px;display:block;margin-top:6px;opacity:.6}}

/* ── CALC & SECTIONS ─────────────────────── */
.csec{{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:13px;max-width:560px}}
.cstit{{font-size:13px;font-weight:700;color:var(--txt);margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.cstit i{{color:var(--cyan);font-size:12px}}
.ctabs{{display:flex;gap:5px;margin-bottom:13px}}
.ctab{{background:var(--bg3);border:1px solid var(--border);border-radius:7px;color:var(--txt2);font-family:var(--sans);font-size:11px;font-weight:600;padding:7px 15px;cursor:pointer;transition:all .2s}}
.ctab.act{{background:rgba(34,211,238,.1);color:var(--cyan);border-color:rgba(34,211,238,.3)}}
.clbl{{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--txt3);margin-bottom:5px;font-family:var(--sans)}}
.cinp{{
  width:100%;padding:10px 13px;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;color:var(--txt);font-family:var(--mono);font-size:13px;
  outline:none;transition:border-color .2s;margin-bottom:10px;
}}
.cinp:focus{{border-color:var(--cyan)}}
.runbtn{{
  width:100%;padding:12px;background:linear-gradient(135deg,var(--cyan),var(--cyan2));
  color:#000;border:none;border-radius:9px;font-size:12px;font-weight:800;
  letter-spacing:1.5px;cursor:pointer;font-family:var(--disp);
  transition:opacity .2s,transform .15s;margin-top:4px;
}}
.runbtn:hover{{opacity:.88;transform:translateY(-1px)}}
.cres{{background:var(--bg);border:1px solid var(--border);border-radius:9px;padding:14px;margin-top:13px}}
.crrow{{display:flex;justify-content:space-between;padding:6px 0;font-size:12px;border-bottom:1px solid var(--border);color:var(--txt2);font-family:var(--mono)}}
.crrow:last-child{{border:none;font-weight:700;color:var(--txt);font-size:13px}}
.ogrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}

/* ── API TABLE ───────────────────────────── */
.atbl{{width:100%;border-collapse:collapse;font-size:11px}}
.atbl th{{padding:9px 12px;font-size:9px;letter-spacing:2px;text-transform:uppercase;font-weight:700;text-align:left;color:var(--txt3);border-bottom:1px solid var(--border);font-family:var(--sans)}}
.atbl td{{padding:9px 12px;border-bottom:1px solid rgba(42,42,53,.6);color:var(--txt2);font-family:var(--mono)}}
.atbl tr:hover td{{background:var(--bg3)}}
.qbg{{height:4px;border-radius:2px;background:var(--bg4);overflow:hidden;margin-top:4px}}
.qfill{{height:100%;border-radius:2px;transition:width .6s ease}}

/* ── DEBUG ───────────────────────────────── */
.dbgpanel{{background:var(--bg);border:1px solid var(--border);border-radius:9px;padding:14px;font-family:var(--mono);font-size:11px;color:var(--txt2);white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto;line-height:1.6;margin-bottom:12px}}
.dbgrow{{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--border)}}
.dbgk{{color:var(--txt3);min-width:120px;flex-shrink:0}}
.dbgv{{color:var(--cyan)}}
.dbgv.ok{{color:var(--green)}}
.dbgv.err{{color:var(--red)}}

/* ── MODAL ───────────────────────────────── */
.mbg{{position:fixed;inset:0;z-index:800;background:rgba(0,0,0,.72);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center}}
.mbg.open{{display:flex}}
.modal{{background:var(--bg2);border:1px solid var(--border2);border-radius:16px;padding:24px;width:92%;max-width:440px;max-height:90vh;overflow-y:auto;animation:modalIn .3s cubic-bezier(.16,1,.3,1)}}
@keyframes modalIn{{from{{opacity:0;transform:scale(.93) translateY(18px)}}to{{opacity:1;transform:none}}}}
.mhead{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;font-size:14px;font-weight:700;color:var(--txt)}}
.closex{{background:none;border:none;font-size:16px;cursor:pointer;color:var(--txt3);transition:color .2s}}
.closex:hover{{color:var(--txt)}}

@media(max-width:600px){{.grid{{grid-template-columns:1fr}}.ogrid{{grid-template-columns:1fr}}.topbar,.tc{{padding-left:14px;padding-right:14px}}}}
</style>
</head>
<body>

<!-- LOCK -->
<div id="lock">
  <div class="lbox">
    <div class="lock-icon"><i class="fas fa-crosshairs"></i></div>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">v8.0 — India Edition</div>
    <input id="linput" type="password" placeholder="••••••••" autocomplete="current-password"/>
    <button id="lbtn" onclick="unlock()"><i class="fas fa-unlock-alt"></i>&nbsp;UNLOCK</button>
    <div id="lerr"><i class="fas fa-triangle-exclamation"></i>&nbsp;Invalid password</div>
  </div>
</div>

<!-- APP -->
<div id="app">

  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> ARB SNIPER v8.0</div>
    <div class="topbar-r">
      <div class="lpill"><div class="ldot"></div><span class="ltext">LIVE</span></div>
      <span class="ttxt">{ist_now}</span>
      <button class="ibtn" onclick="logout()" title="Logout"><i class="fas fa-right-from-bracket"></i></button>
    </div>
  </div>

  <div class="bkbar">
    <span class="bklabel"><i class="fas fa-wallet"></i>&nbsp;Bankroll</span>
    <input type="number" id="bankroll" class="bkinput" value="{DEFAULT_BANK}" min="100" step="100" oninput="onBank()"/>
    <span class="bknote">₹ — all Kelly stakes update live as you type</span>
  </div>

  <div class="sbar">
    <div class="ss"><div class="ssl">Arbs</div><div class="ssv" id="ss-arb" style="color:var(--green)">0</div></div>
    <div class="ss"><div class="ssl">+EV Bets</div><div class="ssv" id="ss-ev" style="color:var(--purple)">0</div></div>
    <div class="ss"><div class="ssl">BC Events</div><div class="ssv" id="ss-bc" style="color:var(--yellow)">0</div></div>
    <div class="ss"><div class="ssl">Top Arb</div><div class="ssv" id="ss-ta" style="color:var(--green)">—</div></div>
    <div class="ss"><div class="ssl">Top EV</div><div class="ssv" id="ss-te" style="color:var(--purple)">—</div></div>
    <div class="ss"><div class="ssl">Profit/₹1K</div><div class="ssv" id="ss-pr" style="color:var(--yellow)">—</div></div>
    <div class="ss"><div class="ssl">Sports</div><div class="ssv" style="color:var(--cyan)">{sports_count}</div></div>
    <div class="ss"><div class="ssl">Events</div><div class="ssv" style="color:var(--txt)">{state.get('total_events_scanned',0)}</div></div>
    <div class="ss"><div class="ssl">API Quota</div><div class="ssv" style="color:var(--cyan)">{total_q}</div></div>
    <div class="ss"><div class="ssl">Keys</div><div class="ssv" style="color:var(--txt)">{len(key_status)}</div></div>
  </div>

  <div class="tabs">
    <button class="tab act" id="tb-arb"   onclick="swTab('arb',this)">  <i class="fas fa-percent"></i>    Arbitrage  <span class="tbadge" id="c-arb">0</span></button>
    <button class="tab"     id="tb-ev"    onclick="swTab('ev',this)">   <i class="fas fa-chart-line"></i>  +EV Bets   <span class="tbadge" id="c-ev">0</span></button>
    <button class="tab"     id="tb-bc"    onclick="swTab('bc',this)">   <i class="fas fa-gamepad"></i>     BC.Game    <span class="tbadge" id="c-bc">0</span></button>
    <button class="tab"     id="tb-calc"  onclick="swTab('calc',this)"> <i class="fas fa-calculator"></i>  Calculator</button>
    <button class="tab"     id="tb-api"   onclick="swTab('api',this)">  <i class="fas fa-server"></i>      API Keys</button>
    <button class="tab"     id="tb-dbg"   onclick="swTab('dbg',this)">  <i class="fas fa-bug"></i>         BC Debug</button>
  </div>

  <!-- ARB TAB -->
  <div id="tc-arb" class="tc act">
    <div class="fbar">
      <input class="fi" id="aq" placeholder="Search match, sport..." oninput="fArb()"/>
      <select class="fs" id="aw" onchange="fArb()"><option value="">All Ways</option><option value="2">2-Way</option><option value="3">3-Way</option></select>
      <select class="fs" id="am" onchange="fArb()"><option value="">All Markets</option><option value="H2H">H2H (Match Winner)</option><option value="TOTALS">TOTALS (Over/Under)</option><option value="SPREADS">SPREADS (Handicap)</option></select>
      <select class="fs" id="as" onchange="fArb()"><option value="">All Sports</option></select>
      <div class="fpill">Min<input type="range" id="amin" min="0" max="5" step="0.05" value="0" oninput="document.getElementById('aminv').textContent=(+this.value).toFixed(2)+'%';fArb()"/><span id="aminv">0.00%</span></div>
    </div>
    <div class="grid" id="g-arb"></div>
  </div>

  <!-- EV TAB -->
  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input class="fi" id="eq" placeholder="Search match or bookmaker..." oninput="fEv()"/>
      <select class="fs" id="eb" onchange="fEv()"><option value="">All Books</option></select>
      <select class="fs" id="es" onchange="fEv()"><option value="">All Sports</option></select>
      <div class="fpill">Min Edge<input type="range" id="emin" min="0" max="20" step="0.5" value="0" oninput="document.getElementById('eminv').textContent=(+this.value).toFixed(1)+'%';fEv()"/><span id="eminv">0.0%</span></div>
    </div>
    <div class="grid" id="g-ev"></div>
  </div>

  <!-- BC TAB -->
  <div id="tc-bc" class="tc">
    <div class="fbar">
      <input class="fi" id="bq" placeholder="Search BC.Game..." oninput="fBc()"/>
      <select class="fs" id="bsp" onchange="fBc()"><option value="">All Sports</option></select>
    </div>
    <div class="grid" id="g-bc"></div>
  </div>

  <!-- CALCULATOR TAB -->
  <div id="tc-calc" class="tc">
    <div class="csec">
      <div class="cstit"><i class="fas fa-percent"></i> Arbitrage Calculator</div>
      <div class="ctabs">
        <button class="ctab act" id="ct2" onclick="swCalc(2,this)">2-Way</button>
        <button class="ctab"     id="ct3" onclick="swCalc(3,this)">3-Way</button>
      </div>
      <div id="c2f">
        <div class="clbl">Odds — Leg 1</div><input class="cinp" id="c2o1" type="number" step="0.01" placeholder="2.15"/>
        <div class="clbl">Odds — Leg 2</div><input class="cinp" id="c2o2" type="number" step="0.01" placeholder="2.05"/>
        <div class="clbl">Total Stake (₹)</div><input class="cinp" id="c2s" type="number" value="{DEFAULT_BANK}"/>
      </div>
      <div id="c3f" style="display:none">
        <div class="clbl">Home Odds</div><input class="cinp" id="c3o1" type="number" step="0.01" placeholder="2.50"/>
        <div class="clbl">Draw Odds</div><input class="cinp" id="c3o2" type="number" step="0.01" placeholder="3.20"/>
        <div class="clbl">Away Odds</div><input class="cinp" id="c3o3" type="number" step="0.01" placeholder="2.80"/>
        <div class="clbl">Total Stake (₹)</div><input class="cinp" id="c3s" type="number" value="{DEFAULT_BANK}"/>
      </div>
      <button class="runbtn" onclick="runCalc()"><i class="fas fa-bolt"></i>&nbsp;CALCULATE</button>
      <div id="calc-res" class="cres" style="display:none"></div>
    </div>
    <div class="csec">
      <div class="cstit"><i class="fas fa-brain"></i> Kelly Criterion Calculator</div>
      <div class="clbl">Your Win Probability (%)</div><input class="cinp" id="kp" type="number" step="0.1" placeholder="55.0"/>
      <div class="clbl">Decimal Odds Offered</div><input class="cinp" id="ko" type="number" step="0.01" placeholder="2.10"/>
      <div class="clbl">Bank Size (₹) — synced from bankroll bar</div><input class="cinp" id="kb" type="number" value="{DEFAULT_BANK}"/>
      <button class="runbtn" onclick="runKelly()"><i class="fas fa-calculator"></i>&nbsp;CALC KELLY</button>
      <div id="kelly-res" class="cres" style="display:none"></div>
    </div>
    <div class="csec">
      <div class="cstit"><i class="fas fa-arrows-rotate"></i> Odds Converter</div>
      <div class="ogrid">
        <div><div class="clbl">Decimal</div><input class="cinp" id="od" type="number" step="0.001" placeholder="2.000" oninput="cvt('d')"/></div>
        <div><div class="clbl">Fractional</div><input class="cinp" id="of" type="text" placeholder="1/1" oninput="cvt('f')"/></div>
        <div><div class="clbl">American</div><input class="cinp" id="oa" type="number" placeholder="+100" oninput="cvt('a')"/></div>
        <div><div class="clbl">Implied %</div><input class="cinp" id="oi" type="number" step="0.01" placeholder="50.00" oninput="cvt('i')"/></div>
      </div>
    </div>
  </div>

  <!-- API KEYS TAB -->
  <div id="tc-api" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="cstit"><i class="fas fa-key"></i> API Key Telemetry — {len(key_status)} keys / {total_q} quota remaining</div>
      <table class="atbl">
        <thead><tr><th>#</th><th>Key (masked)</th><th>Remaining</th><th>Used</th><th style="width:110px">Bar</th></tr></thead>
        <tbody id="ktbody"></tbody>
      </table>
    </div>
    <div class="csec" style="max-width:700px">
      <div class="cstit"><i class="fas fa-chart-bar"></i> Run Statistics</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px">
        <div class="ss" style="min-width:unset"><div class="ssl">Last Sync</div><div style="font-size:11px;color:var(--txt);font-family:var(--mono);margin-top:3px">{ist_now}</div></div>
        <div class="ss" style="min-width:unset"><div class="ssl">Sports Scanned</div><div class="ssv" style="color:var(--cyan)">{sports_count}</div></div>
        <div class="ss" style="min-width:unset"><div class="ssl">Events Scanned</div><div class="ssv" style="color:var(--green)">{state.get('total_events_scanned',0)}</div></div>
        <div class="ss" style="min-width:unset"><div class="ssl">Last Arbs</div><div class="ssv">{state.get('last_arb_count',0)}</div></div>
        <div class="ss" style="min-width:unset"><div class="ssl">Last EVs</div><div class="ssv">{state.get('last_ev_count',0)}</div></div>
        <div class="ss" style="min-width:unset"><div class="ssl">Total Quota Left</div><div class="ssv" style="color:var(--yellow)">{total_q}</div></div>
      </div>
    </div>
  </div>

  <!-- BC DEBUG TAB -->
  <div id="tc-dbg" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="cstit"><i class="fas fa-bug"></i> BC.Game Scraper Debug</div>
      <div id="dbg-rows"></div>
      <div class="clbl" style="margin-top:14px;margin-bottom:6px">Raw JSON Preview (first 800 chars)</div>
      <div class="dbgpanel" id="raw-preview"></div>
    </div>
  </div>

</div>

<!-- QUICK CALC MODAL -->
<div class="mbg" id="qcm">
  <div class="modal">
    <div class="mhead"><span><i class="fas fa-calculator"></i>&nbsp;Quick Calc</span><button class="closex" onclick="closeM()"><i class="fas fa-xmark"></i></button></div>
    <div id="qcm-body"></div>
  </div>
</div>

<script>
const ARBS=({arbs_j});const EVS=({evs_j});const BC=({bc_j});const KEYS=({keys_j});const DBG=({debug_j});const PH="{ph}";
let BANK=parseFloat(localStorage.getItem('arb_bank'))||{DEFAULT_BANK},calcW=2,curEV=EVS;

const BS={{pinnacle:'PIN',bet365:'B365',betway:'BW',stake:'STK',marathonbet:'MAR',parimatch:'PAR',betfair:'BF',dafabet:'DAF',onexbet:'1XB',bc_game:'BCG',matchbook:'MBK'}};
const bs=k=>BS[k]||(k||'').toUpperCase().slice(0,4);
const fd=d=>{{try{{return new Date(d).toLocaleString('en-IN',{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});}}catch{{return String(d)}}}};
const si=s=>{{s=(s||'').toLowerCase();if(s.includes('soccer')||s.includes('football'))return'fa-futbol';if(s.includes('basket'))return'fa-basketball';if(s.includes('hockey'))return'fa-hockey-puck';if(s.includes('tennis'))return'fa-table-tennis-paddle-ball';if(s.includes('mma')||s.includes('box'))return'fa-hand-fist';if(s.includes('cricket'))return'fa-cricket-bat-ball';if(s.includes('baseball'))return'fa-baseball';if(s.includes('golf'))return'fa-golf-ball-tee';if(s.includes('nfl')||s.includes('american'))return'fa-football';if(s.includes('rugby'))return'fa-football';return'fa-trophy';}};
const kly=(e,o,b)=>{{const bv=o-1;if(bv<=0)return 0;const p=1/(o/(1+e)),kf=(bv*p-(1-p))/bv;return kf<=0?0:Math.round(.3*kf*b/10)*10;}};
// Market display label — converts raw market key to human-readable label
const mktLabel=m=>{{const mp={{H2H:'H2H (Match Winner)',TOTALS:'TOTALS (Over/Under)',SPREADS:'SPREADS (Handicap)',h2h:'H2H (Match Winner)',totals:'TOTALS (Over/Under)',spreads:'SPREADS (Handicap)'}};return mp[m]||m;}};
// Scale an arb stake (originally computed for Rs1000) to user's actual bankroll
const scaleStk=(rawStk,bank)=>Math.round((rawStk/1000)*bank/10)*10;

// AUTH
if(localStorage.getItem('sa')===PH)boot();
function unlock(){{const h=CryptoJS.SHA256(document.getElementById('linput').value).toString();if(h===PH){{localStorage.setItem('sa',PH);boot();}}else{{const i=document.getElementById('linput');i.value='';document.getElementById('lerr').style.display='block';i.style.borderColor='var(--red)';setTimeout(()=>{{i.style.borderColor='';document.getElementById('lerr').style.display='none';}},2000);}};}}
document.getElementById('linput').addEventListener('keydown',e=>{{if(e.key==='Enter')unlock();}});
function logout(){{localStorage.removeItem('sa');location.reload();}}
function boot(){{document.getElementById('lock').style.display='none';document.getElementById('app').style.display='block';init();}}

// TABS
function swTab(id,b){{document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act'));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act'));document.getElementById('tc-'+id).classList.add('act');if(b)b.classList.add('act');}}

// BANKROLL — persisted in localStorage so user never has to re-enter it
function onBank(){{
  BANK=parseFloat(document.getElementById('bankroll').value)||10000;
  localStorage.setItem('arb_bank',String(BANK));
  document.getElementById('kb').value=BANK;
  rEV(curEV);
  rArb(ARBS.filter(_=>true));  // re-render arb cards with new bank-scaled stakes
}}

// INIT
function init(){{
  // Restore saved bankroll from localStorage and apply to input + kb field
  const savedBank=parseFloat(localStorage.getItem('arb_bank'));
  if(savedBank&&savedBank>0){{
    BANK=savedBank;
    document.getElementById('bankroll').value=savedBank;
    document.getElementById('kb').value=savedBank;
  }}
  const aS=[...new Set(ARBS.map(a=>a.sport))].sort();
  const eS=[...new Set(EVS.map(e=>e.sport))].sort();
  const eB=[...new Set(EVS.map(e=>e.book_key))].sort();
  const bS=[...new Set(BC.map(b=>b.sport_title||'?'))].sort();
  const add=(id,arr)=>arr.forEach(v=>{{const o=document.createElement('option');o.value=v;o.textContent=v.replace(/_/g,' ');document.getElementById(id).appendChild(o);}});
  add('as',aS);add('es',eS);add('eb',eB);add('bsp',bS);
  document.getElementById('ss-arb').textContent=ARBS.length;
  document.getElementById('ss-ev').textContent=EVS.length;
  document.getElementById('ss-bc').textContent=BC.length;
  document.getElementById('c-arb').textContent=ARBS.length;
  document.getElementById('c-ev').textContent=EVS.length;
  document.getElementById('c-bc').textContent=BC.length;
  document.getElementById('ss-ta').textContent=ARBS.length?'+'+ARBS[0].profit_pct+'%':'—';
  document.getElementById('ss-te').textContent=EVS.length?'+'+EVS[0].edge_pct+'%':'—';
  document.getElementById('ss-pr').textContent=ARBS.length?'₹'+ARBS[0].profit_amt:'—';
  rArb(ARBS);rEV(EVS);rBc(BC);rKeys();rDbg();
}}

// FILTERS
function fArb(){{const q=document.getElementById('aq').value.toLowerCase(),wy=document.getElementById('aw').value,mk=document.getElementById('am').value,sp=document.getElementById('as').value,mn=+document.getElementById('amin').value||0;rArb(ARBS.filter(a=>(!wy||String(a.ways)===wy)&&(!mk||a.market===mk)&&(!sp||a.sport===sp)&&a.profit_pct>=mn&&(!q||a.match.toLowerCase().includes(q)||a.sport.toLowerCase().includes(q))));}}
function fEv(){{const q=document.getElementById('eq').value.toLowerCase(),bk=document.getElementById('eb').value,sp=document.getElementById('es').value,mn=+document.getElementById('emin').value||0;const d=EVS.filter(v=>(!bk||v.book_key===bk)&&(!sp||v.sport===sp)&&v.edge_pct>=mn&&(!q||v.match.toLowerCase().includes(q)||(v.book_key||'').includes(q)));curEV=d;rEV(d);}}
function fBc(){{const q=document.getElementById('bq').value.toLowerCase(),sp=document.getElementById('bsp').value;rBc(BC.filter(b=>(!sp||b.sport_title===sp)&&(!q||(b.home_team+' '+b.away_team).toLowerCase().includes(q))));}}

// RENDER ARBS — stakes scaled to user's bankroll, clear market labels
function rArb(data){{
  const g=document.getElementById('g-arb');document.getElementById('c-arb').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-magnifying-glass"></i>No arbitrage opportunities found.<small>Adjust filters or check API quota in the API Keys tab.</small></div>';return;}}
  g.innerHTML=data.map(a=>{{
    // Scale stakes from the Python-computed Rs1000 base to the user's actual bank
    const rows=a.outcomes.map(o=>{{
      const liveStk=scaleStk(o.stake,BANK);
      const exactStk=((o.stake/1000)*BANK).toFixed(2);
      return `<tr>
        <td><span class="btag">${{bs(o.book_key)}}</span>&nbsp;<span style="color:var(--txt2)">${{o.name}}</span></td>
        <td><span class="oval">${{o.odds}}</span></td>
        <td><span class="stkx">exact ₹${{exactStk}}</span><span class="stkm">₹${{liveStk}}</span></td>
      </tr>`;
    }}).join('');
    const oa=JSON.stringify(a.outcomes.map(o=>o.odds));
    const profitOnBank=((a.profit_pct/100)*BANK).toFixed(2);
    return `<div class="card"><div class="cbar"></div><div class="cinn">
      <div class="ch">
        <span class="cbdg"><i class="fas fa-percent"></i>&nbsp;${{a.ways}}-WAY&nbsp;·&nbsp;${{mktLabel(a.market)}}</span>
        <span class="cpft">+${{a.profit_pct}}%</span>
      </div>
      <div class="cmatch"><i class="fas ${{si(a.sport)}}" style="margin-right:6px;opacity:.6"></i>${{a.match}}</div>
      <div class="cmeta">
        <span><i class="fas fa-clock"></i>&nbsp;${{fd(a.commence)}}</span>
        <span style="opacity:.5">${{a.sport.replace(/_/g,' ')}}</span>
      </div>
      <table class="ctbl">
        <thead><tr><th>Outcome / Book</th><th>Odds</th><th>Stake (Your Bank)</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table>
    </div>
    <div class="cfoot">
      <span class="cfl"><i class="fas fa-coins"></i>&nbsp;Profit ₹${{profitOnBank}} on ₹${{BANK.toLocaleString('en-IN')}}</span>
      <button class="cbtn" onclick='openQC(${{oa}},${{a.ways}})'><i class="fas fa-calculator"></i>&nbsp;Calc</button>
    </div></div>`;
  }}).join('');
}}

// RENDER EVS
function rEV(data){{
  const g=document.getElementById('g-ev');document.getElementById('c-ev').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-chart-line"></i>No value bets found.<small>Pinnacle lines required as reference odds baseline.</small></div>';return;}}
  g.innerHTML=data.map(v=>{{
    const lk=kly(v.edge_pct/100,v.offered_odds,BANK);
    return `<div class="card"><div class="cbar ev"></div><div class="cinn">
      <div class="ch"><span class="cbdg ev"><i class="fas fa-chart-line"></i>&nbsp;+EV&nbsp;·&nbsp;${{mktLabel(v.market)}}</span><span class="cpft ev">+${{v.edge_pct}}%</span></div>
      <div class="cmatch"><i class="fas ${{si(v.sport)}}" style="margin-right:6px;opacity:.6"></i>${{v.match}}</div>
      <div class="cmeta"><span><i class="fas fa-clock"></i>&nbsp;${{fd(v.commence)}}</span><span>${{v.sport.replace(/_/g,' ')}}</span></div>
      <table class="ctbl">
        <tr><td style="color:var(--txt3)">Outcome</td><td colspan=2><strong>${{v.outcome}}</strong></td></tr>
        <tr><td style="color:var(--txt3)">Bookmaker</td><td colspan=2><span class="btag">${{bs(v.book_key)}}</span>&nbsp;${{v.book}}</td></tr>
        <tr><td style="color:var(--txt3)">Offered Odds</td><td colspan=2><span class="oval">${{v.offered_odds}}</span></td></tr>
        <tr><td style="color:var(--txt3)">True Odds</td><td colspan=2>${{v.true_odds}}&nbsp;<span style="color:var(--txt3);font-size:10px">(${{v.true_prob_pct}}%)</span></td></tr>
        <tr><td style="color:var(--txt3)">Kelly (30%)</td><td colspan=2><span class="stkx">exact ₹${{v.kelly_stake}}</span><span class="stkm kly">₹${{lk}}</span></td></tr>
      </table>
    </div></div>`;
  }}).join('');
}}

// RENDER BC
function rBc(data){{
  const g=document.getElementById('g-bc');document.getElementById('c-bc').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-gamepad"></i>No BC.Game events.<small>Check the BC Debug tab — add SCRAPERAPI_KEY to GitHub secrets.</small></div>';return;}}
  g.innerHTML=data.slice(0,120).map(b=>{{
    const outs=b.bookmakers[0].markets[0].outcomes;
    return `<div class="card"><div class="cbar bc"></div><div class="cinn">
      <div class="ch"><span class="cbdg bc"><i class="fas fa-gamepad"></i>&nbsp;BC.GAME</span></div>
      <div class="cmatch"><i class="fas ${{si(b.sport_title)}}" style="margin-right:6px;opacity:.6"></i>${{b.home_team}} vs ${{b.away_team}}</div>
      <div class="cmeta"><span><i class="fas fa-clock"></i>&nbsp;${{fd(b.commence_time)}}</span><span>${{b.sport_title}}</span></div>
      <table class="ctbl">${{outs.map(o=>`<tr><td style="color:var(--txt2)">${{o.name}}</td><td><span class="oval">${{o.price}}</span></td></tr>`).join('')}}</table>
    </div></div>`;
  }}).join('');
}}

// RENDER KEYS
function rKeys(){{
  document.getElementById('ktbody').innerHTML=KEYS.map((k,i)=>{{
    const pct=Math.max(0,Math.min(100,(k.remaining/500)*100));
    const col=pct>55?'var(--green)':pct>18?'var(--yellow)':'var(--red)';
    return `<tr><td style="color:var(--txt3)">#${{i+1}}</td><td style="color:var(--cyan)">${{k.key}}</td><td style="font-weight:800;color:${{col}}">${{k.remaining}}</td><td style="color:var(--txt3)">${{k.used||0}}</td><td><div class="qbg"><div class="qfill" style="width:${{pct.toFixed(1)}}%;background:${{col}}"></div></div></td></tr>`;
  }}).join('');
}}

// RENDER DEBUG
function rDbg(){{
  const rows=[
    ['Status',           DBG.status||'—',       DBG.status&&DBG.status.startsWith('ok')?'ok':DBG.status&&(DBG.status.includes('fail')||DBG.status.includes('error'))?'err':''],
    ['Chunks Total',     String(DBG.chunks_total||0),  (DBG.chunks_total||0)>0?'ok':''],
    ['Chunks Fetched',   String(DBG.chunks_fetched||0),(DBG.chunks_fetched||0)>0?'ok':(DBG.chunks_fetched===0&&(DBG.chunks_total||0)>0)?'err':''],
    ['Events (raw)',     String(DBG.events_raw||0),    (DBG.events_raw||0)>0?'ok':''],
    ['Matches Parsed',   String(DBG.matches_parsed||0),(DBG.matches_parsed||0)>0?'ok':(DBG.matches_parsed===0&&(DBG.events_raw||0)>0)?'err':''],
    ['Outrights Skipped',String(DBG.outrights_skip||0),''],
    ['No-Teams Skipped', String(DBG.no_teams_skip||0), ''],
    ['No-H2H Skipped',   String(DBG.no_odds_skip||0),  ''],
    ['Raw Preview',      DBG.raw_preview||'—',  ''],
  ];
  document.getElementById('dbg-rows').innerHTML=rows.map(([k,v,c])=>`<div class="dbgrow"><span class="dbgk">${{k}}</span><span class="dbgv ${{c}}">${{v||'—'}}</span></div>`).join('');
  document.getElementById('raw-preview').textContent=DBG.raw_preview||'No data captured';
}}

// QUICK CALC MODAL
function openQC(oa,ways){{
  if(ways===2){{document.getElementById('c2o1').value=oa[0]||'';document.getElementById('c2o2').value=oa[1]||'';swCalc(2,document.getElementById('ct2'));}}
  else{{document.getElementById('c3o1').value=oa[0]||'';document.getElementById('c3o2').value=oa[1]||'';document.getElementById('c3o3').value=oa[2]||'';swCalc(3,document.getElementById('ct3'));}}
  const impl=oa.reduce((s,o)=>s+1/o,0),pct=(1/impl-1)*100,stakes=oa.map(o=>(1/o)/impl*BANK),profit=BANK*(1/impl-1),col=pct>0?'var(--green)':'var(--red)';
  document.getElementById('qcm-body').innerHTML=`<div class="cres">${{oa.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>₹${{stakes[i].toFixed(2)}}</span></div>`).join('')}}<div class="crrow"><span>Implied</span><span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span></div><div class="crrow"><span>${{pct>0?'PROFIT on ₹'+BANK:'NO ARB'}}</span><span style="color:${{col}}">${{pct>0?'+₹'+profit.toFixed(2)+' (+'+pct.toFixed(3)+'%)':Math.abs(pct).toFixed(3)+'%'}}</span></div></div><button class="runbtn" style="margin-top:12px" onclick="closeM();swTab('calc',document.getElementById('tb-calc'))"><i class="fas fa-arrow-right"></i>&nbsp;Full Calculator</button>`;
  document.getElementById('qcm').classList.add('open');
}}
function closeM(){{document.getElementById('qcm').classList.remove('open');}}
document.getElementById('qcm').addEventListener('click',e=>{{if(e.target===e.currentTarget)closeM();}});

// CALCULATOR
function swCalc(n,b){{calcW=n;document.querySelectorAll('.ctab').forEach(x=>x.classList.remove('act'));b.classList.add('act');document.getElementById('c2f').style.display=n===2?'block':'none';document.getElementById('c3f').style.display=n===3?'block':'none';document.getElementById('calc-res').style.display='none';}}
function runCalc(){{
  let odds=[],stk=0;
  if(calcW===2){{odds=[+document.getElementById('c2o1').value,+document.getElementById('c2o2').value];stk=+document.getElementById('c2s').value||BANK;}}
  else{{odds=[+document.getElementById('c3o1').value,+document.getElementById('c3o2').value,+document.getElementById('c3o3').value];stk=+document.getElementById('c3s').value||BANK;}}
  if(odds.some(o=>!o||o<=1)){{alert('Enter valid decimal odds > 1');return;}}
  const impl=odds.reduce((s,o)=>s+1/o,0),pct=(1/impl-1)*100,stakes=odds.map(o=>(1/o)/impl*stk),profit=stk*(1/impl-1),col=pct>0?'var(--green)':'var(--red)';
  const rb=document.getElementById('calc-res');
  rb.innerHTML=[...odds.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>₹${{stakes[i].toFixed(2)}} <span style="color:var(--txt3);font-size:10px">(₹${{Math.round(stakes[i]/10)*10}} rnd)</span></span></div>`),`<div class="crrow"><span>Total Stake</span><span>₹${{stk.toFixed(2)}}</span></div>`,`<div class="crrow"><span>Implied</span><span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span></div>`,pct>0?`<div class="crrow"><span>✅ PROFIT</span><span style="color:${{col}}">+₹${{profit.toFixed(2)}} (+${{pct.toFixed(3)}}%)</span></div>`:`<div class="crrow"><span>❌ NO ARB</span><span style="color:${{col}}">${{Math.abs(pct).toFixed(3)}}% over-round</span></div>`].join('');
  rb.style.display='block';
}}
function runKelly(){{
  const p=parseFloat(document.getElementById('kp').value)/100,o=parseFloat(document.getElementById('ko').value),bank=parseFloat(document.getElementById('kb').value)||BANK;
  if(!p||!o||p<=0||p>=1||o<=1){{alert('Enter valid probability (1-99%) and odds > 1');return;}}
  const b=o-1,q=1-p,kf=(b*p-q)/b,full=kf>0?kf*bank:0,frac=kf>0?.3*kf*bank:0,ev=(p*b-q)*100,col=ev>0?'var(--green)':'var(--red)';
  document.getElementById('kelly-res').innerHTML=`<div class="crrow"><span>Expected Value</span><span style="color:${{col}}">${{ev>0?'+':''}}${{ev.toFixed(2)}}% per bet</span></div><div class="crrow"><span>Full Kelly</span><span>₹${{full.toFixed(2)}}</span></div><div class="crrow"><span>30% Fractional Kelly</span><span style="color:${{col}}">₹${{frac.toFixed(2)}} <span style="color:var(--txt3);font-size:10px">(₹${{Math.round(frac/10)*10}} rnd)</span></span></div><div class="crrow"><span>% of Bank at Risk</span><span>${{(frac/bank*100).toFixed(2)}}%</span></div>`;
  document.getElementById('kelly-res').style.display='block';
}}

// ODDS CONVERTER
let _cv=false;
function cvt(from){{if(_cv)return;_cv=true;const sa=d=>{{document.getElementById('od').value=d.toFixed(3);document.getElementById('oa').value=d>=2?'+'+Math.round((d-1)*100):'-'+Math.round(100/(d-1));document.getElementById('oi').value=(100/d).toFixed(2);const[n,dv]=d2f(d);document.getElementById('of').value=n+'/'+dv;}};try{{if(from==='d'){{const v=+document.getElementById('od').value;if(v>1)sa(v);}}else if(from==='a'){{const a=+document.getElementById('oa').value,v=a>0?a/100+1:100/Math.abs(a)+1;if(v>1)sa(v);}}else if(from==='f'){{const p=document.getElementById('of').value.split('/'),v=p.length===2?+p[0]/+p[1]+1:0;if(v>1)sa(v);}}else if(from==='i'){{const i=+document.getElementById('oi').value,v=i>0&&i<100?100/i:0;if(v>1)sa(v);}}}}finally{{_cv=false;}}}}
function d2f(d){{const t=1e-5;let h1=1,h2=0,k1=0,k2=1,b=d-1;for(let i=0;i<40;i++){{const a=Math.floor(b),ah=h1;h1=a*h1+h2;h2=ah;const ak=k1;k1=a*k1+k2;k2=ak;if(Math.abs(b-a)<t)break;b=1/(b-a);}}return[h1,k1];}}
</script>
</body></html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║      ARB SNIPER v8.0 — Maximum Coverage          ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    log.info(f"State loaded | Quota: {state['remaining_requests']} | "
             f"Keys: {len(ROTATOR.keys)}")

    # 1. Discover ALL currently in-season sports dynamically
    sports_list = fetch_all_sports()
    log.info(f"Sports to scan: {len(sports_list)}")

    # 2. Fetch odds for ALL sports × ALL markets concurrently
    odds_events = fetch_all_odds(state, sports_list)

    # 3. BC.Game (two-layer Cloudflare bypass)
    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events  = merge_bcgame(odds_events, bc_events)

    save_state(state)

    # 4. Scan for genuine arbitrage opportunities and +EV bets
    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    # 5. Push notification
    send_push(arbs, evs)

    # 6. Generate dashboard
    key_status = ROTATOR.status()
    html = generate_html(
        arbs, evs, raw_bc_copy, state, key_status,
        BC_DEBUG, len(sports_list)
    )
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard written: {OUTPUT_HTML} ({len(html) // 1024} KB)")

    # 7. Summary
    log.info("═" * 54)
    log.info(f"  Sports scanned  : {len(sports_list)}")
    log.info(f"  Events scanned  : {len(all_events)}")
    log.info(f"  Genuine arbs    : {len(arbs)}")
    if arbs:
        t = arbs[0]
        log.info(f"  Top arb         : {t['match']} "
                 f"+{t['profit_pct']}% ({t['ways']}-way {t['market']})")
    log.info(f"  EV bets         : {len(evs)}")
    if evs:
        t = evs[0]
        log.info(f"  Top EV          : {t['match']} "
                 f"+{t['edge_pct']}% @ {t['book']}")
    log.info(f"  BC events       : {len(raw_bc_copy)} "
             f"[{BC_DEBUG.get('status','?')}]")
    log.info(f"  API quota left  : {ROTATOR.total_remaining()}")
    log.info(f"  Keys active     : {len(ROTATOR.keys)}")
    log.info("╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
