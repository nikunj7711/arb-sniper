#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v8.0 — ENTERPRISE NODE EDITION
║  Two-Step BC.Game API | India Books | localStorage Bankroll | Clear Markets  ║
║  Dynamic Sport Discovery | All Markets | Fixed Arb Engine | Auto-Billing     ║
╚══════════════════════════════════════════════════════════════════════════════╝
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
# CONSTANTS & CONFIGURATIONS
# ─────────────────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()

# ── MONETIZATION & GATEWAY CONFIG ─────────────────────────────────────────────
YOUR_UPI_ID  = "Furiousfighter06-1@okhdfcbank"
YOUR_NAME    = "Furious Fighter"
FIREBASE_URL = "https://payment-engine-e3bff-default-rtdb.asia-southeast1.firebasedatabase.app"
SUB_PRICE    = 500  # Base subscription price for 30 days (INR)

# ── ARBITRAGE THRESHOLDS ──────────────────────────────────────────────────────
MIN_ARB_PROFIT = 0.05   # 0.05% minimum
MAX_ARB_PROFIT = 15.0   # Anti-palp cap
MIN_EV_EDGE    = 0.005  # 0.5% edge required
KELLY_FRACTION = 0.30   # 30% fractional Kelly
DEFAULT_BANK   = 10000  # Rs — overridden live by dashboard input

# ── BC.GAME ENDPOINTS ─────────────────────────────────────────────────────────
BCGAME_BASE    = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en"
BCGAME_DISC    = f"{BCGAME_BASE}/0"       
BCGAME_MAX_CAT = 30                        

# ── BOOKMAKERS — INDIA-ACCESSIBLE ONLY ───────────────────────────────────────
ALLOWED_BOOKS = {
    "pinnacle", "stake", "bc_game", "onexbet", "parimatch", 
    "dafabet", "betway", "bet365", "marathonbet", "betfair", "matchbook",
}

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"

ALWAYS_INCLUDE_SPORTS = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "basketball_nba", "basketball_euroleague",
    "icehockey_nhl", "mma_mixed_martial_arts",
    "cricket_ipl", "cricket_test_match", "cricket_odi",
    "tennis_atp_french_open", "tennis_wta_french_open",
    "americanfootball_nfl",
}

# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
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

    def get(self) -> str:
        with self._lock:
            if not self.keys: return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int = 0):
        with self._lock:
            self._quota[key] = max(0, remaining)
            self._used[key]  = used

    def mark_exhausted(self, key: str):
        with self._lock:
            self._quota[key] = 0

    def total_remaining(self) -> int:
        with self._lock: return max(0, sum(self._quota.values()))

    def total_used(self) -> int:
        with self._lock: return sum(self._used.values())

    def status(self) -> list:
        with self._lock:
            return [
                {
                    "key":       f"{k[:4]}...{k[-4:]}",
                    "remaining": self._quota.get(k, 0),
                    "used":      self._used.get(k, 0),
                    "active":    k == max(self.keys, key=lambda x: self._quota.get(x, 0)) if self.keys else False
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
        except Exception: pass
    return defaults

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ═════════════════════════════════════════════════════════════════════════════
# DYNAMIC SPORT DISCOVERY
# ═════════════════════════════════════════════════════════════════════════════
def fetch_all_sports() -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY": return sorted(ALWAYS_INCLUDE_SPORTS)
    try:
        r = requests.get(f"{ODDS_BASE}/sports", params={"apiKey": key, "all": "false"}, timeout=15)
        if r.status_code == 200:
            active = [s["key"] for s in r.json() if not s.get("has_outrights")]
            return active
        return sorted(ALWAYS_INCLUDE_SPORTS)
    except Exception:
        return sorted(ALWAYS_INCLUDE_SPORTS)

# ═════════════════════════════════════════════════════════════════════════════
# ODDS API — CONCURRENT FETCHER
# ═════════════════════════════════════════════════════════════════════════════
def _fetch_market(market: str) -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY": return []
    if ROTATOR._quota.get(key, 0) <= 0: return []

    url    = f"{ODDS_BASE}/sports/upcoming/odds"
    params = {
        "apiKey":     key,
        "regions":    REGIONS,
        "markets":    market,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        remaining = int(r.headers.get("X-Requests-Remaining", ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code in (429, 401):
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200: return []

        data = r.json()
        if not isinstance(data, list): return []

        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)
        return filtered

    except Exception:
        return []

def fetch_all_odds(state: dict, sports_list: list) -> list:
    if not ROTATOR.keys: return []
    if ROTATOR.total_remaining() <= 0:
        state["quota_exhausted"] = True
        return []

    events_by_id: dict = {}
    for market in MARKETS:
        results = _fetch_market(market)
        for ev in results:
            ev_id = ev.get("id", "")
            if ev_id not in events_by_id:
                events_by_id[ev_id] = ev
            else:
                existing_bms = {bm["key"]: bm for bm in events_by_id[ev_id]["bookmakers"]}
                for bm in ev["bookmakers"]:
                    bk = bm["key"]
                    if bk not in existing_bms:
                        events_by_id[ev_id]["bookmakers"].append(bm)
                    else:
                        existing_mkt_keys = {m["key"] for m in existing_bms[bk].get("markets", [])}
                        for mkt in bm.get("markets", []):
                            if mkt["key"] not in existing_mkt_keys:
                                existing_bms[bk]["markets"].append(mkt)

    all_events = list(events_by_id.values())
    state["remaining_requests"]   = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    state["sports_scanned"]       = len({ev.get("sport_key","") for ev in all_events})
    return all_events

# ═════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER
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

_BC_HOST    = "api-k-c7818b61-623.sptpub.com"
_BC_HEADERS = {"User-Agent": "insomnia/12.4.0", "Accept": "application/json"}

def _bc_fetch(path: str) -> dict | None:
    import zlib, gzip, io
    full_url = f"https://{_BC_HOST}{path}"

    def _parse(raw: bytes) -> dict | None:
        try: return json.loads(raw.decode("utf-8"))
        except Exception: pass
        try: return json.loads(zlib.decompress(raw, 16 + zlib.MAX_WBITS).decode("utf-8"))
        except Exception: pass
        try: return json.loads(gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8"))
        except Exception: pass
        return None

    try:
        r = requests.get(full_url, headers=_BC_HEADERS, timeout=20, verify=True)
        if r.status_code == 200:
            parsed = _parse(r.content)
            if parsed: return parsed
    except Exception: pass

    if SCRAPERAPI_KEY:
        try:
            api_url = (f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={urllib.parse.quote(full_url, safe='')}&keep_headers=true&autoparse=false&render=false&country_code=de")
            r = requests.get(api_url, headers=_BC_HEADERS, timeout=60)
            if r.status_code == 200:
                parsed = _parse(r.content)
                if parsed: return parsed
        except Exception: pass

    return None

def _bc_sport_name(sport_id: str, all_sports: dict) -> str:
    return all_sports.get(str(sport_id), {}).get("name", f"sport_{sport_id}")

def _bc_league_name(tourn_id: str, all_tourns: dict) -> str:
    return all_tourns.get(str(tourn_id), {}).get("name", "")

def _bc_parse_teams(desc: dict) -> tuple[str, str]:
    comps = desc.get("competitors", [])
    home = away = ""
    for c in comps:
        q = str(c.get("qualifier", c.get("q", ""))).lower()
        n = c.get("name", "")
        if q in ("home", "1", "h"):   home = n
        elif q in ("away", "2", "a"): away = n
    if (not home or not away) and len(comps) == 2:
        home = comps[0].get("name", "")
        away = comps[1].get("name", "")
    return home, away

def _bc_parse_h2h(markets: dict) -> list:
    if "11" not in markets: return []
    for line_key, sels in markets["11"].items():
        if line_key not in ("", "0"): continue
        if not isinstance(sels, dict): continue
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict): continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01: prices.append(round(p, 3))
            except Exception: pass
        if len(prices) == 2:
            return [{"name": "Home", "price": prices[0]}, {"name": "Away", "price": prices[1]}]
        if len(prices) == 3:
            return [{"name": "Home", "price": prices[0]}, {"name": "Draw", "price": prices[1]}, {"name": "Away", "price": prices[2]}]
    return []

def _bc_parse_handicap(markets: dict) -> list:
    if "223" not in markets: return []
    results = []
    for line_key, sels in markets["223"].items():
        if not line_key.startswith("hcp="): continue
        if not isinstance(sels, dict): continue
        hcp = line_key.replace("hcp=", "")
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict): continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01: prices.append(round(p, 3))
            except Exception: pass
        if len(prices) == 2:
            results.append({"name": f"Home {hcp}", "price": prices[0], "point": float(hcp) if hcp.lstrip("-").replace(".","").isdigit() else 0})
            results.append({"name": f"Away {hcp}", "price": prices[1], "point": -float(hcp) if hcp.lstrip("-").replace(".","").isdigit() else 0})
    return results[:4]

def _bc_parse_totals(markets: dict) -> list:
    if "202" not in markets: return []
    results = []
    for line_key, sels in markets["202"].items():
        if not isinstance(sels, dict): continue
        line_label = line_key.replace("setnr=", "") if "setnr=" in line_key else line_key
        prices = []
        for sel_data in sels.values():
            if not isinstance(sel_data, dict): continue
            try:
                p = float(sel_data.get("k", 0))
                if p > 1.01: prices.append(round(p, 3))
            except Exception: pass
        if len(prices) == 2:
            results.append({"name": f"Over {line_label}",  "price": prices[0]})
            results.append({"name": f"Under {line_label}", "price": prices[1]})
    return results[:4]

def fetch_bcgame_events() -> list:
    global BC_DEBUG
    brand_id = "2103509236163162112"
    base     = f"/api/v4/prematch/brand/{brand_id}/en"

    manifest = _bc_fetch(f"{base}/0")
    if not manifest:
        BC_DEBUG["status"] = "manifest_failed"
        return []

    top_ids  = manifest.get("top_events_versions",  [])
    rest_ids = manifest.get("rest_events_versions", [])
    all_ids  = top_ids + rest_ids
    BC_DEBUG["chunks_total"] = len(all_ids)

    if not all_ids: return []

    all_sports = {}; all_cats = {}; all_tourns = {}; all_events = {}
    fetched = 0

    for chunk_id in all_ids:
        chunk = _bc_fetch(f"{base}/{chunk_id}")
        if not chunk: continue
        all_sports.update(chunk.get("sports",      {}))
        all_cats.update(  chunk.get("categories",  {}))
        all_tourns.update(chunk.get("tournaments", {}))
        all_events.update(chunk.get("events",      {}))
        fetched += 1

    BC_DEBUG["chunks_fetched"] = fetched
    BC_DEBUG["events_raw"]     = len(all_events)

    if not all_events: return []

    converted = []
    skip_out = skip_teams = skip_odds = 0

    for ev_id, ev in all_events.items():
        desc    = ev.get("desc", {})
        ev_type = desc.get("type", "match")

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

        all_outcomes = h2h or []
        if not all_outcomes:
            skip_odds += 1
            continue

        sid   = desc.get("sport",      "")
        tid   = desc.get("tournament", "")
        ts    = desc.get("scheduled",  "")
        sport = _bc_sport_name(sid, all_sports)
        lg    = _bc_league_name(tid, all_tourns)

        try:
            ts_val = float(ts)
            start  = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
        except Exception:
            start = str(ts)

        mkt_list = []
        if h2h: mkt_list.append({"key": "h2h", "outcomes": h2h})
        if handicap: mkt_list.append({"key": "spreads", "outcomes": handicap})
        if totals: mkt_list.append({"key": "totals", "outcomes": totals})

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
    BC_DEBUG["status"]         = f"ok_{len(converted)}_matches" if converted else "ok_but_no_h2h"
    return converted

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    merged = 0
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (similarity(bh, ev.get("home_team", "")) + similarity(ba, ev.get("away_team", ""))) / 2
            if s > best_score:
                best_score, best_ev = s, ev
        if best_score > 0.72 and best_ev:
            best_ev["bookmakers"].extend(bc_ev["bookmakers"])
            merged += 1
        else:
            odds_events.append(bc_ev)
    return odds_events

# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE
# ═════════════════════════════════════════════════════════════════════════════
def remove_vig(outcomes: list) -> dict:
    raw = {}
    for o in outcomes:
        try: raw[o["name"]] = 1.0 / float(o["price"])
        except Exception: pass
    total = sum(raw.values())
    if total <= 0: return {}
    return {k: v / total for k, v in raw.items()}

def kelly_stake(edge: float, odds: float, bank: float) -> float:
    b = odds - 1.0
    if b <= 0: return 0.0
    p  = 1.0 / (odds / (1.0 + edge))
    kf = (b * p - (1.0 - p)) / b
    if kf <= 0: return 0.0
    return round(KELLY_FRACTION * kf * bank, 2)

def round10(x: float) -> float:
    return round(round(x / 10) * 10, 2)

def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    impl = sum(1.0 / o for o in odds_list)
    if impl >= 1.0: return [0.0] * len(odds_list)
    return [(1.0 / o) / impl * total for o in odds_list]

# ═════════════════════════════════════════════════════════════════════════════
# ARBITRAGE SCANNER
# ═════════════════════════════════════════════════════════════════════════════
def _best_price_per_book(bookmakers: list, market_key: str) -> dict:
    best: dict = {}
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key: continue
            for o in mkt.get("outcomes", []):
                raw_name = str(o.get("name", ""))
                pt       = o.get("point")
                try: price = float(o.get("price", 0))
                except Exception: continue
                if price <= 1.01: continue

                if pt is not None:
                    try: name = f"{raw_name}_{abs(float(pt))}"
                    except Exception: name = raw_name
                else:
                    name = raw_name

                bk  = bm.get("key", "?")
                ttl = bm.get("title", "?")
                if name not in best: best[name] = {}
                if bk not in best[name] or price > best[name][bk][0]:
                    best[name][bk] = (price, ttl)
    return best

def _build_arb_record(combo: list, mkey: str, sport: str, match: str, com: str):
    prices = [x[1] for x in combo]
    impl   = sum(1.0 / p for p in prices)
    if impl >= 1.0: return None
    pct = (1.0 / impl - 1.0) * 100
    if pct < MIN_ARB_PROFIT or pct > MAX_ARB_PROFIT: return None
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
    arbs  = []
    names = list(best.keys())

    if len(names) == 2:
        n1, n2 = names[0], names[1]
        bk1, bk2 = best[n1], best[n2]
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                if bk_a == bk_b: continue
                rec = _build_arb_record([(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)], "h2h", sport, match, com)
                if rec:
                    if (best_rec is None or rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break
        if best_rec: arbs.append(best_rec)

    elif len(names) == 3:
        n1, n2, n3 = names[0], names[1], names[2]
        bk1, bk2, bk3 = best[n1], best[n2], best[n3]
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                for bk_c, (p_c, t_c) in sorted(bk3.items(), key=lambda x: -x[1][0]):
                    if len({bk_a, bk_b, bk_c}) < 2: continue
                    rec = _build_arb_record([(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b), (n3, p_c, t_c, bk_c)], "h2h", sport, match, com)
                    if rec:
                        if (best_rec is None or rec["profit_pct"] > best_rec["profit_pct"]):
                            best_rec = rec
        if best_rec: arbs.append(best_rec)
    return arbs

def _scan_totals(best: dict, sport: str, match: str, com: str) -> list:
    arbs   = []
    points = {}
    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2: continue
        side  = parts[0]
        point = "_".join(parts[1:])
        points.setdefault(point, {})
        points[point].setdefault(side, {})
        points[point][side].update(bk_prices)

    for point, sides in points.items():
        if "Over" not in sides or "Under" not in sides: continue
        over_bks  = sides["Over"]
        under_bks = sides["Under"]
        best_rec  = None
        for bk_o, (p_o, t_o) in sorted(over_bks.items(), key=lambda x: -x[1][0]):
            for bk_u, (p_u, t_u) in sorted(under_bks.items(), key=lambda x: -x[1][0]):
                if bk_o == bk_u: continue
                rec = _build_arb_record([(f"Over {point}", p_o, t_o, bk_o), (f"Under {point}", p_u, t_u, bk_u)], "totals", sport, match, com)
                if rec:
                    if (best_rec is None or rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break
        if best_rec: arbs.append(best_rec)
    return arbs

def _scan_spreads(best: dict, sport: str, match: str, com: str) -> list:
    arbs         = []
    point_groups = {}
    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2: continue
        point = "_".join(parts[1:])
        point_groups.setdefault(point, []).append((name, bk_prices))

    for point, group in point_groups.items():
        if len(group) != 2: continue
        (n1, bk1), (n2, bk2) = group
        best_rec = None
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                if bk_a == bk_b: continue
                rec = _build_arb_record([(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)], "spreads", sport, match, com)
                if rec:
                    if (best_rec is None or rec["profit_pct"] > best_rec["profit_pct"]):
                        best_rec = rec
                    break
        if best_rec: arbs.append(best_rec)
    return arbs

def scan_arbitrage(events: list) -> list:
    seen = set()
    arbs = []
    for ev in events:
        home  = ev.get("home_team", "?")
        away  = ev.get("away_team", "?")
        sport = ev.get("sport_title", "Unknown")
        com   = ev.get("commence_time", "")
        match = f"{home} vs {away}"

        for mkey in MARKETS:
            best = _best_price_per_book(ev.get("bookmakers", []), mkey)
            if not best: continue

            if mkey == "h2h":          candidates = _scan_h2h(best, sport, match, com)
            elif mkey == "totals":     candidates = _scan_totals(best, sport, match, com)
            elif mkey == "spreads":    candidates = _scan_spreads(best, sport, match, com)
            else: candidates = []

            for c in candidates:
                bk_set = frozenset(o["book_key"] for o in c["outcomes"])
                key    = (match, mkey, bk_set)
                if key in seen: continue
                seen.add(key)
                arbs.append(c)

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs[:300]

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
            pin_out = None
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    for m in bm.get("markets", []):
                        if m.get("key") == mkey:
                            pin_out = m.get("outcomes", [])
            if not pin_out or len(pin_out) < 2: continue

            true_probs = remove_vig(pin_out)
            if not true_probs: continue

            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        if name not in true_probs: continue
                        try: price = float(o["price"])
                        except Exception: continue
                        if price <= 1.0: continue

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
    return bets[:500]

# ═════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs: return
    if arbs:
        t   = arbs[0]
        msg = f"ARB: {t['match']} | +{t['profit_pct']}% | {t['ways']}-way {t['market']} | {len(evs)} EV bets"
    else:
        t   = evs[0]
        msg = f"EV: {t['match']} | +{t['edge_pct']}% edge | {t['book']} | {len(evs)} total"
    try:
        requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={"Title": "Arb Sniper Alert", "Priority": "high", "Tags": "zap,moneybag", "Content-Type": "text/plain; charset=utf-8"}, timeout=10)
    except Exception: pass

# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR — PREMIUM DARK UI + FIREBASE GATEWAY
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list,
                  state: dict, key_status: list,
                  bc_debug: dict, sports_count: int) -> str:

    IST     = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    total_q = sum(k["remaining"] for k in key_status)

    arbs_j  = json.dumps(arbs,      ensure_ascii=False)
    evs_j   = json.dumps(evs,       ensure_ascii=False)
    bc_j    = json.dumps(raw_bc,    ensure_ascii=False)
    keys_j  = json.dumps(key_status, ensure_ascii=False)
    debug_j = json.dumps(bc_debug,  ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v8.0 ⚡</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
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

/* ── LOCK / SUBSCRIPTION PORTAL ──────────────────────────────────── */
#lock{{
  position:fixed;inset:0;z-index:9999;background:var(--bg);
  display:flex;align-items:center;justify-content:center;
  background:radial-gradient(ellipse 80% 60% at 50% 40%,rgba(34,211,238,0.06) 0%,transparent 65%);
}}
.lbox{{
  width:90%;max-width:360px;background:var(--bg2);border:1px solid var(--border2);
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
#userIdInput{{
  width:100%;padding:13px 16px;font-size:16px;text-align:center;letter-spacing:2px;
  background:var(--bg3);border:1px solid var(--border2);border-radius:10px;
  color:var(--txt);font-family:var(--mono);outline:none;
  transition:border-color .2s,box-shadow .2s;
}}
#userIdInput:focus{{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(34,211,238,.12)}}
#lbtn{{
  width:100%;padding:13px;font-size:12px;font-weight:700;letter-spacing:2px;
  cursor:pointer;border:none;font-family:var(--disp);
  background:linear-gradient(135deg,var(--cyan),var(--cyan2));color:#000;
  border-radius:10px;transition:opacity .2s,transform .15s;
}}
#lbtn:hover{{opacity:.88;transform:translateY(-1px)}}
#lerr{{font-size:11px;color:var(--red);display:none;letter-spacing:.5px;text-align:center;}}

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

/* ── DEBUG & MODAL ───────────────────────── */
.dbgpanel{{background:var(--bg);border:1px solid var(--border);border-radius:9px;padding:14px;font-family:var(--mono);font-size:11px;color:var(--txt2);white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto;line-height:1.6;margin-bottom:12px}}
.dbgrow{{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--border)}}
.dbgk{{color:var(--txt3);min-width:120px;flex-shrink:0}}
.dbgv{{color:var(--cyan)}}
.dbgv.ok{{color:var(--green)}}
.dbgv.err{{color:var(--red)}}
.mbg{{position:fixed;inset:0;z-index:800;background:rgba(0,0,0,.72);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center}}
.mbg.open{{display:flex}}
.modal{{background:var(--bg2);border:1px solid var(--border2);border-radius:16px;padding:24px;width:92%;max-width:440px;max-height:90vh;overflow-y:auto;animation:modalIn .3s cubic-bezier(.16,1,.3,1)}}
@keyframes modalIn{{from{{opacity:0;transform:scale(.93) translateY(18px)}}to{{opacity:1;transform:none}}}}
.mhead{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;font-size:14px;font-weight:700;color:var(--txt)}}
.closex{{background:none;border:none;font-size:16px;cursor:pointer;color:var(--txt3);transition:color .2s}}
.closex:hover{{color:var(--txt)}}
.receipt-box{{background:#111;padding:18px;border-radius:6px;margin-bottom:20px;text-align:left;font-size:14px;border:1px solid #2a2a2a;}}
.receipt-row{{display:flex;justify-content:space-between;margin-bottom:10px;color:#ddd;}}
.direct-pay-btn{{display:block;background-color:#0056b3;color:white;text-decoration:none;padding:14px 20px;border-radius:6px;font-size:15px;font-weight:600;margin-top:15px;transition:background 0.3s;text-align:center;}}
.direct-pay-btn:hover{{background-color:#004494;}}
.loader{{border:3px solid #333;border-top:3px solid #4CAF50;border-radius:50%;width:24px;height:24px;animation:spin 1s linear infinite;margin:15px auto;}}

@media(max-width:600px){{.grid{{grid-template-columns:1fr}}.ogrid{{grid-template-columns:1fr}}.topbar,.tc{{padding-left:14px;padding-right:14px}}}}
</style>
</head>
<body>

<div id="lock">
  <div class="lbox" id="login-box">
    <div class="lock-icon"><i class="fas fa-crosshairs"></i></div>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">v8.0 — Enterprise Node</div>
    
    <input id="userIdInput" type="text" placeholder="Enter Username" autocomplete="off"/>
    <button id="lbtn" onclick="authenticateUser()"><i class="fas fa-right-to-bracket"></i>&nbsp;CONNECT NODE</button>
    <div id="lerr"></div>
  </div>

  <div class="lbox" id="pay-box" style="display: none; padding: 24px;">
    <div class="lock-title" style="font-size: 16px;">LICENSE REQUIRED</div>
    <div class="lock-sub" style="margin-bottom: 15px;">30-Day Access Provisioning</div>
    
    <div class="receipt-box" style="width: 100%; margin-bottom: 15px;">
        <div class="receipt-row"><span>License:</span><span>₹{SUB_PRICE}.00</span></div>
        <div class="receipt-row" style="color:#888;font-size:13px;"><span>Network Fee:</span><span>₹<span id="subFee">0.00</span></span></div>
        <div class="receipt-row" style="color:#4CAF50;font-weight:bold;margin-top:10px;padding-top:10px;border-top:1px dashed #444;"><span>Required:</span><span style="font-family: monospace; font-size: 16px;">₹<span id="subTotal">0.00</span></span></div>
    </div>

    <div style="padding: 10px; margin-bottom: 10px; background:#fff; border-radius:8px;">
        <img id="subQrCode" src="" alt="Payment QR" style="width: 180px; height: 180px; display: block; margin:0 auto;">
    </div>
    
    <a id="subDeepLink" href="#" class="direct-pay-btn" style="width: 100%; margin-top: 0;">Pay Directly via UPI</a>
    
    <div style="display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 15px;">
        <div class="loader" style="width: 16px; height: 16px; margin: 0; border-width: 2px;"></div>
        <span style="font-size: 11px; color: var(--txt3);">Awaiting network confirmation...</span>
    </div>
  </div>
</div>

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
    <div class="ss"><div class="ssl">API Quota</div><div class="ssv" style="color:{'var(--green)' if total_q > 5000 else 'var(--yellow)' if total_q > 1000 else 'var(--red)'}">{total_q}</div></div>
    <div class="ss"><div class="ssl">Runs Left</div><div class="ssv" style="color:var(--cyan)">{max(0, total_q // 3)}</div></div>
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

  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input class="fi" id="eq" placeholder="Search match or bookmaker..." oninput="fEv()"/>
      <select class="fs" id="eb" onchange="fEv()"><option value="">All Books</option></select>
      <select class="fs" id="es" onchange="fEv()"><option value="">All Sports</option></select>
      <div class="fpill">Min Edge<input type="range" id="emin" min="0" max="20" step="0.5" value="0" oninput="document.getElementById('eminv').textContent=(+this.value).toFixed(1)+'%';fEv()"/><span id="eminv">0.0%</span></div>
    </div>
    <div class="grid" id="g-ev"></div>
  </div>

  <div id="tc-bc" class="tc">
    <div class="fbar">
      <input class="fi" id="bq" placeholder="Search BC.Game..." oninput="fBc()"/>
      <select class="fs" id="bsp" onchange="fBc()"><option value="">All Sports</option></select>
    </div>
    <div class="grid" id="g-bc"></div>
  </div>

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

  <div id="tc-api" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="cstit"><i class="fas fa-key"></i> API Key Telemetry — {len(key_status)} keys / {total_q} quota remaining</div>
      <table class="atbl">
        <thead><tr><th>#</th><th>Key (masked)</th><th>Remaining</th><th>Used</th><th style="width:110px">Bar</th></tr></thead>
        <tbody id="ktbody"></tbody>
      </table>
    </div>
  </div>

  <div id="tc-dbg" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="cstit"><i class="fas fa-bug"></i> BC.Game Scraper Debug</div>
      <div id="dbg-rows"></div>
      <div class="clbl" style="margin-top:14px;margin-bottom:6px">Raw JSON Preview</div>
      <div class="dbgpanel" id="raw-preview"></div>
    </div>
  </div>
</div>

<div class="mbg" id="qcm">
  <div class="modal">
    <div class="mhead"><span><i class="fas fa-calculator"></i>&nbsp;Quick Calc</span><button class="closex" onclick="closeM()"><i class="fas fa-xmark"></i></button></div>
    <div id="qcm-body"></div>
  </div>
</div>

<script>
const ARBS={arbs_j};const EVS={evs_j};const BC={bc_j};const KEYS={keys_j};const DBG={debug_j};
let BANK=parseFloat(localStorage.getItem('arb_bank'))||{DEFAULT_BANK},calcW=2,curEV=EVS;

// ── MONETIZATION & FIREBASE AUTHENTICATION LOGIC ──
const YOUR_UPI_ID = "{YOUR_UPI_ID}";
const YOUR_NAME = "{YOUR_NAME}";
const FIREBASE_URL = "{FIREBASE_URL}";
const SUB_PRICE = {SUB_PRICE};

let currentUser = "";
let autoCheckInterval;
let safeAmountKey = "";

if(localStorage.getItem('arb_session')){{
    currentUser = localStorage.getItem('arb_session');
    verifySubscription();
}}

async function authenticateUser() {{
    const input = document.getElementById('userIdInput').value.trim().toLowerCase();
    if(!input) return;
    currentUser = input;
    localStorage.setItem('arb_session', currentUser);
    verifySubscription();
}}

document.getElementById('userIdInput').addEventListener('keydown', e => {{
    if(e.key === 'Enter') authenticateUser();
}});

async function verifySubscription() {{
    const errDiv = document.getElementById('lerr');
    const btn = document.getElementById('lbtn');
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> VERIFYING...';
    
    try {{
        const res = await fetch(`${{FIREBASE_URL}}/users/${{currentUser}}.json`, {{ cache: "no-store" }});
        const data = await res.json();
        const now = new Date().getTime();

        if (data && data.sub_expiry && data.sub_expiry > now) {{
            document.getElementById('lock').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            init(); 
        }} else {{
            triggerPaymentGateway();
        }}
    }} catch (error) {{
        errDiv.innerText = "Network Error. Check connection.";
        errDiv.style.display = 'block';
        btn.innerHTML = '<i class="fas fa-right-to-bracket"></i> CONNECT NODE';
    }}
}}

function triggerPaymentGateway() {{
    document.getElementById('login-box').style.display = 'none';
    document.getElementById('pay-box').style.display = 'flex';

    const randomCents = Math.floor(Math.random() * 99) + 1; 
    const feeAmount = randomCents / 100; 
    const finalAmount = SUB_PRICE + feeAmount; 
    
    const targetAmountStr = finalAmount.toFixed(2); 
    safeAmountKey = targetAmountStr.replace(".", "_"); 

    document.getElementById("subFee").innerText = feeAmount.toFixed(2);
    document.getElementById("subTotal").innerText = targetAmountStr;

    const upiLink = `upi://pay?pa=${{YOUR_UPI_ID}}&pn=${{encodeURIComponent(YOUR_NAME)}}&am=${{targetAmountStr}}&cu=INR`;
    document.getElementById("subQrCode").src = `https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=${{encodeURIComponent(upiLink)}}`;
    document.getElementById("subDeepLink").href = upiLink;

    autoCheckInterval = setInterval(checkSubPayment, 3000);
}}

async function checkSubPayment() {{
    try {{
        const response = await fetch(`${{FIREBASE_URL}}/payments/${{safeAmountKey}}.json`, {{ cache: "no-store" }});
        const data = await response.json();

        if (data && data.status === "CONFIRMED") {{
            clearInterval(autoCheckInterval);

            await fetch(`${{FIREBASE_URL}}/payments/${{safeAmountKey}}.json`, {{
                method: 'PATCH',
                body: JSON.stringify({{ status: "USED" }})
            }});
            if (data.utr) {{
                await fetch(`${{FIREBASE_URL}}/utr_records/${{data.utr}}.json`, {{
                    method: 'PATCH',
                    body: JSON.stringify({{ status: "USED" }})
                }});
            }}

            const newExpiry = new Date().getTime() + (30 * 24 * 60 * 60 * 1000); 
            await fetch(`${{FIREBASE_URL}}/users/${{currentUser}}.json`, {{
                method: 'PATCH',
                body: JSON.stringify({{ sub_expiry: newExpiry }})
            }});

            alert("Payment Verified. License provisioned for 30 days.");
            document.getElementById('lock').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            init();
        }}
    }} catch (error) {{ }}
}}

function logout() {{
    localStorage.removeItem('arb_session');
    location.reload();
}}

const BS={{pinnacle:'PIN',bet365:'B365',betway:'BW',stake:'STK',marathonbet:'MAR',parimatch:'PAR',betfair:'BF',dafabet:'DAF',onexbet:'1XB',bc_game:'BCG',matchbook:'MBK'}};
const bs=k=>BS[k]||(k||'').toUpperCase().slice(0,4);
const fd=d=>{{try{{return new Date(d).toLocaleString('en-IN',{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});}}catch{{return String(d)}}}};
const si=s=>{{s=(s||'').toLowerCase();if(s.includes('soccer')||s.includes('football'))return'fa-futbol';if(s.includes('basket'))return'fa-basketball';if(s.includes('hockey'))return'fa-hockey-puck';if(s.includes('tennis'))return'fa-table-tennis-paddle-ball';if(s.includes('mma')||s.includes('box'))return'fa-hand-fist';if(s.includes('cricket'))return'fa-cricket-bat-ball';if(s.includes('baseball'))return'fa-baseball';if(s.includes('golf'))return'fa-golf-ball-tee';if(s.includes('nfl')||s.includes('american'))return'fa-football';if(s.includes('rugby'))return'fa-football';return'fa-trophy';}};
const kly=(e,o,b)=>{{const bv=o-1;if(bv<=0)return 0;const p=1/(o/(1+e)),kf=(bv*p-(1-p))/bv;return kf<=0?0:Math.round(.3*kf*b/10)*10;}};
const mktLabel=m=>{{const mp={{H2H:'H2H (Match Winner)',TOTALS:'TOTALS (Over/Under)',SPREADS:'SPREADS (Handicap)',h2h:'H2H (Match Winner)',totals:'TOTALS (Over/Under)',spreads:'SPREADS (Handicap)'}};return mp[m]||m;}};
const scaleStk=(rawStk,bank)=>Math.round((rawStk/1000)*bank/10)*10;

function swTab(id,b){{document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act'));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act'));document.getElementById('tc-'+id).classList.add('act');if(b)b.classList.add('act');}}

function onBank(){{
  BANK=parseFloat(document.getElementById('bankroll').value)||10000;
  localStorage.setItem('arb_bank',String(BANK));
  document.getElementById('kb').value=BANK;
  rEV(curEV);
  rArb(ARBS.filter(_=>true));
}}

function init(){{
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

function fArb(){{const q=document.getElementById('aq').value.toLowerCase(),wy=document.getElementById('aw').value,mk=document.getElementById('am').value,sp=document.getElementById('as').value,mn=+document.getElementById('amin').value||0;rArb(ARBS.filter(a=>(!wy||String(a.ways)===wy)&&(!mk||a.market===mk)&&(!sp||a.sport===sp)&&a.profit_pct>=mn&&(!q||a.match.toLowerCase().includes(q)||a.sport.toLowerCase().includes(q))));}}
function fEv(){{const q=document.getElementById('eq').value.toLowerCase(),bk=document.getElementById('eb').value,sp=document.getElementById('es').value,mn=+document.getElementById('emin').value||0;const d=EVS.filter(v=>(!bk||v.book_key===bk)&&(!sp||v.sport===sp)&&v.edge_pct>=mn&&(!q||v.match.toLowerCase().includes(q)||(v.book_key||'').includes(q)));curEV=d;rEV(d);}}
function fBc(){{const q=document.getElementById('bq').value.toLowerCase(),sp=document.getElementById('bsp').value;rBc(BC.filter(b=>(!sp||b.sport_title===sp)&&(!q||(b.home_team+' '+b.away_team).toLowerCase().includes(q))));}}

function rArb(data){{
  const g=document.getElementById('g-arb');document.getElementById('c-arb').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-magnifying-glass"></i>No arbitrage opportunities found.<small>Adjust filters or check API quota in the API Keys tab.</small></div>';return;}}
  g.innerHTML=data.map(a=>{{
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

function rBc(data){{
  const g=document.getElementById('g-bc');document.getElementById('c-bc').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-gamepad"></i>No BC.Game events.</div>';return;}}
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

function rKeys(){{
  document.getElementById('ktbody').innerHTML=KEYS.map((k,i)=>{{
    const pct=Math.max(0,Math.min(100,(k.remaining/500)*100));
    const col=pct>55?'var(--green)':pct>18?'var(--yellow)':'var(--red)';
    const act=k.active===true;
    const rs=act?'background:rgba(34,211,238,0.07);outline:1px solid rgba(34,211,238,0.22);':'';
    const ab=act?'&nbsp;<span style="font-size:9px;background:rgba(34,211,238,0.18);color:var(--cyan);padding:1px 5px;border-radius:4px;font-family:var(--mono)">ACTIVE</span>':'';
    return `<tr style="${{rs}}"><td style="color:var(--txt3)">#${{i+1}}</td><td style="color:${{act?'var(--cyan)':'var(--txt2)'}}">${{k.key}}${{ab}}</td><td style="font-weight:800;color:${{col}}">${{k.remaining}}</td><td style="color:var(--txt3)">${{k.used||0}}</td><td><div class="qbg"><div class="qfill" style="width:${{pct.toFixed(1)}}%;background:${{col}}"></div></div></td></tr>`;
  }}).join('');
}}

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
  ];
  document.getElementById('dbg-rows').innerHTML=rows.map(([k,v,c])=>`<div class="dbgrow"><span class="dbgk">${{k}}</span><span class="dbgv ${{c}}">${{v||'—'}}</span></div>`).join('');
  document.getElementById('raw-preview').textContent=DBG.raw_preview||'No data captured';
}}

function openQC(oa,ways){{
  if(ways===2){{document.getElementById('c2o1').value=oa[0]||'';document.getElementById('c2o2').value=oa[1]||'';swCalc(2,document.getElementById('ct2'));}}
  else{{document.getElementById('c3o1').value=oa[0]||'';document.getElementById('c3o2').value=oa[1]||'';document.getElementById('c3o3').value=oa[2]||'';swCalc(3,document.getElementById('ct3'));}}
  const impl=oa.reduce((s,o)=>s+1/o,0),pct=(1/impl-1)*100,stakes=oa.map(o=>(1/o)/impl*BANK),profit=BANK*(1/impl-1),col=pct>0?'var(--green)':'var(--red)';
  document.getElementById('qcm-body').innerHTML=`<div class="cres">${{oa.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>₹${{stakes[i].toFixed(2)}}</span></div>`).join('')}}<div class="crrow"><span>Implied</span><span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span></div><div class="crrow"><span>${{pct>0?'PROFIT on ₹'+BANK:'NO ARB'}}</span><span style="color:${{col}}">${{pct>0?'+₹'+profit.toFixed(2)+' (+'+pct.toFixed(3)+'%)':Math.abs(pct).toFixed(3)+'%'}}</span></div></div><button class="runbtn" style="margin-top:12px" onclick="closeM();swTab('calc',document.getElementById('tb-calc'))"><i class="fas fa-arrow-right"></i>&nbsp;Full Calculator</button>`;
  document.getElementById('qcm').classList.add('open');
}}
function closeM(){{document.getElementById('qcm').classList.remove('open');}}
document.getElementById('qcm').addEventListener('click',e=>{{if(e.target===e.currentTarget)closeM();}});

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
    log.info("║       ARB SNIPER v8.0 — ENTERPRISE NODE          ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    log.info(f"State loaded | Quota: {state['remaining_requests']} | Keys: {len(ROTATOR.keys)}")

    sports_list = fetch_all_sports()
    log.info(f"Sports to scan: {len(sports_list)}")

    odds_events = fetch_all_odds(state, sports_list)

    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events  = merge_bcgame(odds_events, bc_events)

    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    send_push(arbs, evs)

    key_status = ROTATOR.status()
    html = generate_html(
        arbs, evs, raw_bc_copy, state, key_status,
        BC_DEBUG, len(sports_list)
    )
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard generated: {OUTPUT_HTML} ({len(html) // 1024} KB)")

if __name__ == "__main__":
    main()
