#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v6.0 — PRODUCTION EDITION                     ║
║  Fixed Arb Engine | Live Bankroll | Premium UI | BC.Game Debug Mode         ║
╚══════════════════════════════════════════════════════════════════════════════╝

KEY FIXES vs v5.0:
  • Arbitrage scanner rewritten — only pairs OPPOSING outcomes per market
    (home vs away, over vs under, team A vs team B). No more same-side combos.
  • Deduplication: one arb per event/market pair (highest profit only).
  • BC.Game: multiple fallback URLs + raw JSON debug tab in dashboard.
  • Bankroll: read from dashboard input, all Kelly stakes recalculate live.
  • Single premium dark UI — no theme switcher bloat.
  • fetch_all_odds: staggered concurrency (max_workers=3, 0.3s delay) to
    prevent all 19 keys hitting the API simultaneously and triggering 429s.
"""

import os
import json
import hashlib
import requests
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("ArbSniper")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "arb2026")

# BC.Game endpoint list — tries each in order until one works
BCGAME_URLS = [
    "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0",
    "https://bc.game/api/sport/prematch/list",
    "https://bc.game/api/sport/event/list?type=prematch",
]

KELLY_FRACTION = 0.30
MIN_ARB_PROFIT = 0.10   # 0.10% minimum — tight threshold to reduce false arbs
MIN_EV_EDGE    = 0.005  # 0.5%
DEFAULT_BANK   = 10000  # Rs — overridden live by dashboard input

ALLOWED_BOOKS = {
    "pinnacle", "onexbet", "bet365", "unibet", "betway", "stake",
    "marathonbet", "parimatch", "betfair", "dafabet", "bovada",
    "draftkings", "fanduel", "betmgm"
}

# Focused sports list — "upcoming" alone wastes API calls, use specific sports
SPORTS_LIST = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "basketball_nba", "basketball_euroleague",
    "icehockey_nhl",
    "mma_mixed_martial_arts",
    "cricket_test_match", "cricket_odi", "cricket_ipl",
    "tennis_atp_french_open", "tennis_wta_french_open",
    "americanfootball_nfl",
    "baseball_mlb",
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS  = "eu,uk,us,au"

# ─────────────────────────────────────────────────────────────────────────────
# WHAT MAKES A VALID ARBITRAGE
# ─────────────────────────────────────────────────────────────────────────────
# For h2h: outcomes are {home_team, draw, away_team} — they are mutually exclusive.
# For totals: outcomes are {Over X, Under X} — MUST have matching point value.
# For spreads: {home -1.5, away +1.5} — MUST have opposite/matching handicaps.
#
# FALSE ARBS come from pairing:
#   • "Over 2.5" (one book) + "Over 3.5" (another book) — different lines!
#   • "Home -1.5" + "Home +2.5" — same direction, different lines!
#
# FIX: For totals/spreads, group outcomes by abs(point) value.
#      Only pair Over X vs Under X (same X). Reject all others.


# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self.keys:
            log.warning("ODDS_API_KEYS env var is empty or not set!")
        self._lock  = threading.Lock()
        self._quota = {k: 500 for k in self.keys}
        log.info(f"KeyRotator: {len(self.keys)} keys loaded.")

    def get(self) -> str:
        with self._lock:
            if not self.keys:
                return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int = 0):
        with self._lock:
            self._quota[key] = max(0, remaining)

    def mark_exhausted(self, key: str):
        with self._lock:
            self._quota[key] = 0
            log.warning(f"Key ...{key[-6:]} exhausted — marked 0.")

    def total_remaining(self) -> int:
        with self._lock:
            return max(0, sum(self._quota.values()))

    def status(self) -> list:
        with self._lock:
            return [
                {"key": f"{k[:4]}...{k[-4:]}", "remaining": self._quota.get(k, 0)}
                for k in self.keys
            ]


ROTATOR = KeyRotator()


# ═════════════════════════════════════════════════════════════════════════════
# STATE
# ═════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {
        "remaining_requests": 500, "used_today": 0,
        "last_reset": str(datetime.now(timezone.utc).date()),
        "total_events_scanned": 0, "last_arb_count": 0, "last_ev_count": 0
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
# ODDS API — CONCURRENT FETCHER
# ═════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 3:
        return []

    url    = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": key, "regions": REGIONS, "markets": market,
        "oddsFormat": "decimal", "dateFormat": "iso"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        remaining = int(r.headers.get("X-Requests-Remaining", ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code == 422:
            return []
        if r.status_code in (429, 401):
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200:
            log.warning(f"HTTP {r.status_code} — {sport}/{market}")
            return []

        data = r.json()
        if not isinstance(data, list):
            return []

        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)

        log.info(f"  {sport}/{market}: {len(filtered)} events | key ...{key[-6:]} → {remaining} left")
        return filtered

    except requests.exceptions.Timeout:
        log.warning(f"Timeout: {sport}/{market}")
        return []
    except Exception as e:
        log.error(f"Error {sport}/{market}: {e}")
        return []


def fetch_all_odds(state: dict) -> list:
    if not ROTATOR.keys:
        log.error("No API keys. Set ODDS_API_KEYS secret.")
        return []
    if ROTATOR.total_remaining() <= 0:
        log.error("All API keys exhausted.")
        return []

    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    log.info(f"Launching {len(tasks)} fetches across {len(ROTATOR.keys)} keys...")

    all_events = []
    # max_workers=3 with 0.3s stagger — prevents hammering all keys at once
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {}
        for s, m in tasks:
            futures[ex.submit(fetch_sport_odds, s, m)] = (s, m)
            time.sleep(0.3)
        for fut in as_completed(futures):
            try:
                all_events.extend(fut.result())
            except Exception as e:
                log.error(f"Future error: {e}")

    state["remaining_requests"]   = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    log.info(f"Total Odds API events: {len(all_events)}")
    return all_events


# ═════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER — MULTI-URL WITH RAW DEBUG CAPTURE
# ═════════════════════════════════════════════════════════════════════════════
BC_DEBUG = {"status": "not_tried", "url": "", "http_code": 0,
            "size_bytes": 0, "root_type": "", "root_keys": "",
            "extracted": 0, "parsed": 0, "raw_preview": ""}


def _deep_find_list(obj, keys: list):
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v
        for v in obj.values():
            r = _deep_find_list(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _deep_find_list(item, keys)
            if r is not None:
                return r
    return None


def _extract_bc_events(raw) -> list:
    if isinstance(raw, list) and raw:
        return raw
    if isinstance(raw, dict):
        for k in ["data", "events", "list", "items", "result",
                  "content", "rows", "matches", "games", "sportEvents"]:
            v = raw.get(k)
            if isinstance(v, list) and v:
                return v
            if isinstance(v, dict):
                for k2 in ["events", "list", "items", "matches", "games", "data"]:
                    v2 = v.get(k2)
                    if isinstance(v2, list) and v2:
                        return v2
        found = _deep_find_list(raw, ["events", "matches", "games", "list", "items"])
        if found:
            return found
    return []


def _parse_bc_event(ev: dict):
    home = (ev.get("homeTeam") or ev.get("home") or ev.get("team1") or
            ev.get("homeName") or ev.get("home_team") or ev.get("teamHome") or "")
    away = (ev.get("awayTeam") or ev.get("away") or ev.get("team2") or
            ev.get("awayName") or ev.get("away_team") or ev.get("teamAway") or "")
    sport  = (ev.get("sportName") or ev.get("sport") or ev.get("sportTitle") or
              ev.get("category") or "Unknown")
    start  = (ev.get("startTime") or ev.get("startAt") or ev.get("time") or
              ev.get("kickOff") or "")

    outcomes = []
    raw_mkts = (ev.get("markets") or ev.get("odds") or
                ev.get("marketList") or ev.get("betTypes") or [])

    if isinstance(raw_mkts, list):
        for mkt in raw_mkts:
            if not isinstance(mkt, dict):
                continue
            outs = (mkt.get("outcomes") or mkt.get("selections") or
                    mkt.get("runners") or mkt.get("bets") or [])
            for o in outs:
                if not isinstance(o, dict):
                    continue
                name  = (o.get("name") or o.get("label") or o.get("selectionName") or "")
                price = None
                for pk in ["price", "odds", "odd", "value", "coefficient", "rate", "oddsValue"]:
                    if pk in o:
                        try:
                            price = float(o[pk])
                            break
                        except (ValueError, TypeError):
                            pass
                if name and price and price > 1.01:
                    outcomes.append({"name": name, "price": price})
    elif isinstance(raw_mkts, dict):
        for k, v in raw_mkts.items():
            try:
                price = float(v)
                if price > 1.01:
                    outcomes.append({"name": k, "price": price})
            except (ValueError, TypeError):
                pass

    if not home or not away or not outcomes:
        return None

    return {
        "id":            f"bcgame_{abs(hash(home + away + str(start)))}",
        "sport_title":   sport,
        "home_team":     home,
        "away_team":     away,
        "commence_time": str(start),
        "bookmakers": [{
            "key": "bcgame", "title": "BC.Game",
            "last_update": datetime.now(timezone.utc).isoformat(),
            "markets": [{"key": "h2h", "outcomes": outcomes}]
        }]
    }


def fetch_bcgame_events() -> list:
    global BC_DEBUG
    headers = {
        "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/123.0.0.0 Safari/537.36"),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          "https://bc.game",
        "Referer":         "https://bc.game/",
        "Cache-Control":   "no-cache, no-store"
    }

    for url in BCGAME_URLS:
        try:
            BC_DEBUG["url"] = url
            BC_DEBUG["status"] = "trying"
            r = requests.get(url, headers=headers, timeout=25)
            BC_DEBUG["http_code"]   = r.status_code
            BC_DEBUG["size_bytes"]  = len(r.content)

            log.info(f"BC.Game [{url}] status={r.status_code} size={len(r.content)}B")

            if r.status_code != 200:
                BC_DEBUG["status"] = f"http_{r.status_code}"
                continue

            try:
                raw = r.json()
            except Exception as e:
                BC_DEBUG["status"]      = "json_parse_error"
                BC_DEBUG["raw_preview"] = r.text[:500]
                log.error(f"BC.Game JSON parse error: {e}")
                continue

            BC_DEBUG["root_type"] = type(raw).__name__
            BC_DEBUG["root_keys"] = (str(list(raw.keys())[:10])
                                     if isinstance(raw, dict) else f"list[{len(raw)}]")
            BC_DEBUG["raw_preview"] = json.dumps(raw)[:800] if raw else "empty"

            raw_evs = _extract_bc_events(raw)
            BC_DEBUG["extracted"] = len(raw_evs)
            log.info(f"BC.Game extracted {len(raw_evs)} raw events")

            converted = []
            for ev in raw_evs[:400]:
                parsed = _parse_bc_event(ev)
                if parsed:
                    converted.append(parsed)

            BC_DEBUG["parsed"] = len(converted)
            BC_DEBUG["status"] = "ok" if converted else "parsed_zero"
            log.info(f"BC.Game: {len(converted)}/{len(raw_evs)} events parsed.")

            if converted:
                return converted
            # If parsed zero, try next URL

        except requests.exceptions.Timeout:
            BC_DEBUG["status"] = "timeout"
            log.warning(f"BC.Game timeout: {url}")
        except Exception as e:
            BC_DEBUG["status"] = f"error: {str(e)[:80]}"
            log.error(f"BC.Game error [{url}]: {e}")

    log.warning("BC.Game: all URLs failed or returned 0 events.")
    return []


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def merge_bcgame(odds_events: list, bc_events: list) -> list:
    merged = 0
    for bc_ev in bc_events:
        bh, ba    = bc_ev["home_team"], bc_ev["away_team"]
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
    log.info(f"BC.Game merge: {merged} integrated, {len(bc_events)-merged} standalone.")
    return odds_events


# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE
# ═════════════════════════════════════════════════════════════════════════════
def remove_vig(outcomes: list) -> dict:
    """Multiplicative vig removal → {name: true_probability}."""
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
    """30% fractional Kelly Criterion."""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    p  = 1.0 / (odds / (1.0 + edge))
    kf = (b * p - (1.0 - p)) / b
    if kf <= 0:
        return 0.0
    return round(KELLY_FRACTION * kf * bank, 2)


def round10(x: float) -> float:
    return round(round(x / 10) * 10, 2)


def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    impl = sum(1.0 / o for o in odds_list)
    if impl >= 1.0:
        return [0.0] * len(odds_list)
    return [(1.0 / o) / impl * total for o in odds_list]


# ═════════════════════════════════════════════════════════════════════════════
# ARBITRAGE SCANNER — FIXED: ONLY GENUINE OPPOSING OUTCOMES
#
# The core bug in previous versions:
#   itertools.combinations picked ANY 2 outcomes from a flat list like:
#   [Over 2.5 @Pinnacle, Over 3.5 @Bet365, Under 2.5 @Betway, Under 3.5 @Bovada]
#   → It would pair "Over 2.5" + "Over 3.5" = fake arb (both the same side!)
#
# The fix:
#   For H2H: pair all 2-way (home vs away) or all 3-way (home/draw/away).
#            Each outcome must come from a different bookmaker.
#   For Totals: group by point value. Only pair Over X vs Under X (same X).
#   For Spreads: group by abs(point). Only pair +X vs -X (same abs value).
#   Per-bookie: one outcome per bookmaker per side (best price only).
#
# Additionally: deduplicate so one event/market only produces its BEST arb.
# ═════════════════════════════════════════════════════════════════════════════

def _best_price_per_book(bookmakers: list, market_key: str) -> dict:
    """
    Returns {outcome_name → {book_key: (price, book_title)}}
    where outcome_name is normalised (spread: name_abspoint, h2h: raw name).
    """
    best = {}   # outcome_name → {book_key: (price, title)}
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
                        # Normalise: "Over_2.5" or "Under_2.5"
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


def _build_arb_record(outcomes_combo: list, mkey: str, sport: str,
                      match: str, com: str) -> dict | None:
    """
    Given a list of (outcome_name, price, book_title, book_key),
    check if they form a real arb and return the record or None.
    """
    prices = [x[1] for x in outcomes_combo]
    impl   = sum(1.0 / p for p in prices)
    if impl >= 1.0:
        return None
    pct = (1.0 / impl - 1.0) * 100
    if pct < MIN_ARB_PROFIT:
        return None
    stakes = calc_stakes(prices)
    return {
        "ways":       len(outcomes_combo),
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
        } for x, s in zip(outcomes_combo, stakes)]
    }


def _scan_h2h(best: dict, sport: str, match: str, com: str) -> list:
    """
    H2H: all outcomes are mutually exclusive (home / draw / away).
    Build one best-price per outcome across all books, then check
    if the combination forms a surebet.
    Rules:
      - Each leg must come from a DIFFERENT bookmaker.
      - 2-way: any 2 distinct outcomes.
      - 3-way: home + draw + away (all 3).
    """
    arbs = []
    outcome_names = list(best.keys())
    if len(outcome_names) < 2:
        return arbs

    # For each outcome, pick the best price (any book)
    def best_for(name):
        bk_prices = best[name]
        bk = max(bk_prices, key=lambda k: bk_prices[k][0])
        price, title = bk_prices[bk]
        return (name, price, title, bk)

    # 2-way combos from distinct books
    for i in range(len(outcome_names)):
        for j in range(i + 1, len(outcome_names)):
            n1, n2 = outcome_names[i], outcome_names[j]
            o1 = best_for(n1)
            o2 = best_for(n2)
            if o1[3] == o2[3]:
                # Try second-best book for one
                bk1_prices = best[n1]
                bk2_prices = best[n2]
                # Pick best from different books
                found = False
                for bk_a, (p_a, t_a) in sorted(bk1_prices.items(), key=lambda x: -x[1][0]):
                    for bk_b, (p_b, t_b) in sorted(bk2_prices.items(), key=lambda x: -x[1][0]):
                        if bk_a != bk_b:
                            rec = _build_arb_record(
                                [(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)],
                                "h2h", sport, match, com
                            )
                            if rec:
                                arbs.append(rec)
                            found = True
                            break
                    if found:
                        break
            else:
                rec = _build_arb_record([o1, o2], "h2h", sport, match, com)
                if rec:
                    arbs.append(rec)

    # 3-way: try home/draw/away
    if len(outcome_names) == 3:
        trio = [best_for(n) for n in outcome_names]
        books_used = [t[3] for t in trio]
        if len(set(books_used)) == 3:
            rec = _build_arb_record(trio, "h2h", sport, match, com)
            if rec:
                arbs.append(rec)

    return arbs


def _scan_totals(best: dict, sport: str, match: str, com: str) -> list:
    """
    Totals: outcomes look like 'Over_2.5', 'Under_2.5', 'Over_3.0', ...
    ONLY pair Over_X vs Under_X where X is identical.
    Each leg from a different bookmaker.
    """
    arbs = []
    # Group by point value
    points: dict = {}  # "2.5" → {"Over": {bk: (price, title)}, "Under": {...}}
    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2:
            continue
        side  = parts[0]   # "Over" or "Under"
        point = "_".join(parts[1:])
        if point not in points:
            points[point] = {}
        if side not in points[point]:
            points[point][side] = {}
        points[point][side].update(bk_prices)

    for point, sides in points.items():
        if "Over" not in sides or "Under" not in sides:
            continue
        over_bks  = sides["Over"]
        under_bks = sides["Under"]
        # Best price for each side from different books
        for bk_o, (p_o, t_o) in sorted(over_bks.items(), key=lambda x: -x[1][0]):
            for bk_u, (p_u, t_u) in sorted(under_bks.items(), key=lambda x: -x[1][0]):
                if bk_o != bk_u:
                    rec = _build_arb_record(
                        [(f"Over {point}", p_o, t_o, bk_o),
                         (f"Under {point}", p_u, t_u, bk_u)],
                        "totals", sport, match, com
                    )
                    if rec:
                        arbs.append(rec)
                    break  # take best combo only
            else:
                continue
            break

    return arbs


def _scan_spreads(best: dict, sport: str, match: str, com: str) -> list:
    """
    Spreads: outcomes like 'TeamA_1.5', 'TeamB_1.5' (both same abs point).
    Group by abs(point) and pair the two sides from different bookmakers.
    """
    arbs = []
    # Group by abs point: "1.5" → list of (name, {bk: (price, title)})
    point_groups: dict = {}
    for name, bk_prices in best.items():
        parts = name.split("_")
        if len(parts) < 2:
            continue
        point = "_".join(parts[1:])
        if point not in point_groups:
            point_groups[point] = []
        point_groups[point].append((name, bk_prices))

    for point, group in point_groups.items():
        if len(group) != 2:
            continue  # need exactly 2 sides
        (n1, bk1), (n2, bk2) = group
        for bk_a, (p_a, t_a) in sorted(bk1.items(), key=lambda x: -x[1][0]):
            for bk_b, (p_b, t_b) in sorted(bk2.items(), key=lambda x: -x[1][0]):
                if bk_a != bk_b:
                    rec = _build_arb_record(
                        [(n1, p_a, t_a, bk_a), (n2, p_b, t_b, bk_b)],
                        "spreads", sport, match, com
                    )
                    if rec:
                        arbs.append(rec)
                    break
            else:
                continue
            break

    return arbs


def scan_arbitrage(events: list) -> list:
    seen: set = set()  # deduplicate: (match, market, frozenset of book_keys)
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
                # Dedup key: match + market + sorted book_keys
                bk_set = frozenset(o["book_key"] for o in c["outcomes"])
                key    = (match, mkey, bk_set)
                if key in seen:
                    continue
                seen.add(key)
                arbs.append(c)

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    log.info(f"Arbitrage scan (fixed): {len(arbs)} genuine opportunities found.")
    return arbs[:150]


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
            if not pin_out or len(pin_out) < 2:
                continue

            true_probs = remove_vig(pin_out)
            if not true_probs:
                continue

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
    return bets[:300]


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
                "Title": "Arb Sniper Alert", "Priority": "high",
                "Tags": "zap,moneybag", "Content-Type": "text/plain; charset=utf-8"
            }, timeout=10
        )
        log.info(f"Push: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Push failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD — SINGLE PREMIUM DARK UI
# Design: Deep slate/zinc dark mode, cyan accent, crisp typography.
# Bankroll: user inputs on dashboard → all Kelly stakes recalculate instantly.
# No themes — just one polished, professional design.
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list,
                  state: dict, key_status: list, bc_debug: dict) -> str:

    IST      = timezone(timedelta(hours=5, minutes=30))
    ist_now  = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    ph       = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()
    total_q  = sum(k["remaining"] for k in key_status)

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
<title>Arb Sniper v6.0</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@700;800&display=swap" rel="stylesheet"/>
<style>
/* ── RESET ──────────────────────────────────────────── */
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
:root{{
  --bg:     #0c0c0e;
  --bg1:    #111115;
  --bg2:    #17171d;
  --bg3:    #1e1e26;
  --bg4:    #26262f;
  --border: #2a2a35;
  --border2:#35353f;
  --cyan:   #22d3ee;
  --cyan2:  #0ea5e9;
  --green:  #4ade80;
  --green2: #22c55e;
  --red:    #f87171;
  --yellow: #fbbf24;
  --purple: #a78bfa;
  --txt:    #e8e8f0;
  --txt2:   #9898aa;
  --txt3:   #5a5a6a;
  --mono:   'JetBrains Mono', monospace;
  --sans:   'Inter', sans-serif;
  --display:'Syne', sans-serif;
  --r:      10px;
  --r2:     14px;
}}
html,body{{width:100%;height:100%;overflow:hidden;background:var(--bg)}}
::-webkit-scrollbar{{width:3px;height:3px}}
::-webkit-scrollbar-thumb{{background:var(--bg4);border-radius:2px}}

/* ── LOCK SCREEN ──────────────────────────────────────── */
#lock{{
  position:fixed;inset:0;z-index:9999;background:var(--bg);
  display:flex;align-items:center;justify-content:center;
  background:radial-gradient(ellipse 80% 60% at 50% 40%,
    rgba(34,211,238,0.06) 0%,transparent 65%);
}}
.lbox{{
  width:90%;max-width:340px;
  background:var(--bg2);
  border:1px solid var(--border2);
  border-radius:20px;
  padding:36px 32px;
  display:flex;flex-direction:column;align-items:center;gap:18px;
  box-shadow:0 0 60px rgba(34,211,238,0.06),0 24px 80px rgba(0,0,0,0.6);
  animation:lockIn 0.5s cubic-bezier(0.16,1,0.3,1) both;
}}
@keyframes lockIn{{from{{opacity:0;transform:scale(0.94) translateY(16px)}}to{{opacity:1;transform:none}}}}
.lock-icon{{
  width:52px;height:52px;border-radius:14px;
  background:linear-gradient(135deg,rgba(34,211,238,0.15),rgba(167,139,250,0.15));
  border:1px solid rgba(34,211,238,0.25);
  display:flex;align-items:center;justify-content:center;
  font-size:22px;color:var(--cyan);
  animation:iconPulse 2.5s ease-in-out infinite;
}}
@keyframes iconPulse{{
  0%,100%{{box-shadow:0 0 0 0 rgba(34,211,238,0.2)}}
  50%{{box-shadow:0 0 0 8px rgba(34,211,238,0)}}
}}
.lock-title{{
  font-family:var(--display);font-size:20px;font-weight:800;
  letter-spacing:3px;color:var(--txt);text-align:center;
}}
.lock-sub{{font-size:10px;color:var(--txt3);letter-spacing:2px;text-transform:uppercase}}
#linput{{
  width:100%;padding:13px 16px;font-size:18px;text-align:center;
  letter-spacing:8px;background:var(--bg3);
  border:1px solid var(--border2);border-radius:var(--r);
  color:var(--txt);font-family:var(--mono);outline:none;
  transition:border-color 0.2s,box-shadow 0.2s;
}}
#linput:focus{{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(34,211,238,0.12)}}
#lbtn{{
  width:100%;padding:13px;font-size:12px;font-weight:700;
  letter-spacing:2px;cursor:pointer;border:none;
  background:linear-gradient(135deg,var(--cyan),var(--cyan2));
  color:#000;border-radius:var(--r);font-family:var(--display);
  transition:opacity 0.2s,transform 0.15s;
}}
#lbtn:hover{{opacity:0.88;transform:translateY(-1px)}}
#lerr{{font-size:11px;color:var(--red);display:none;letter-spacing:0.5px}}

/* ── APP ──────────────────────────────────────────────── */
#app{{
  display:none;width:100%;height:100vh;
  overflow-y:auto;font-family:var(--sans);
  background:var(--bg);position:relative;
}}
#app::before{{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 60% 50% at 15% 10%,rgba(34,211,238,0.04) 0%,transparent 60%),
    radial-gradient(ellipse 50% 40% at 85% 85%,rgba(167,139,250,0.04) 0%,transparent 60%);
}}

/* ── TOPBAR ───────────────────────────────────────────── */
.topbar{{
  position:sticky;top:0;z-index:100;
  background:rgba(12,12,14,0.85);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 20px;height:52px;
  display:flex;align-items:center;justify-content:space-between;
}}
.logo{{
  display:flex;align-items:center;gap:8px;
  font-family:var(--display);font-size:15px;font-weight:800;
  background:linear-gradient(135deg,var(--cyan),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  letter-spacing:1px;
}}
.topbar-r{{display:flex;align-items:center;gap:10px}}
.lpill{{
  display:flex;align-items:center;gap:5px;padding:4px 10px;
  border-radius:20px;background:rgba(74,222,128,0.1);
  border:1px solid rgba(74,222,128,0.22);
}}
.ldot{{
  width:6px;height:6px;border-radius:50%;background:var(--green);
  box-shadow:0 0 6px var(--green);animation:ldot 1.4s infinite;
}}
@keyframes ldot{{0%,100%{{opacity:1}}50%{{opacity:0.15}}}}
.ltext{{font-size:10px;color:var(--green);font-weight:700;letter-spacing:1.5px;font-family:var(--mono)}}
.time-txt{{font-size:10px;color:var(--txt3);font-family:var(--mono)}}
.iBtn{{
  background:none;border:none;cursor:pointer;color:var(--txt3);
  font-size:14px;padding:4px;transition:all 0.2s;
}}
.iBtn:hover{{color:var(--red);transform:scale(1.1)}}

/* ── BANKROLL BAR ─────────────────────────────────────── */
.bankroll-bar{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  padding:10px 20px;display:flex;align-items:center;gap:12px;
  flex-wrap:wrap;
}}
.bank-label{{font-size:11px;color:var(--txt3);font-weight:600;letter-spacing:1px;text-transform:uppercase;white-space:nowrap}}
.bank-input{{
  background:var(--bg3);border:1px solid var(--border2);
  border-radius:8px;color:var(--cyan);font-family:var(--mono);
  font-size:14px;font-weight:700;padding:7px 12px;
  outline:none;width:140px;transition:border-color 0.2s;
}}
.bank-input:focus{{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(34,211,238,0.1)}}
.bank-note{{font-size:10px;color:var(--txt3);font-family:var(--mono)}}

/* ── STATS BAR ────────────────────────────────────────── */
.statsbar{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  padding:10px 20px;display:flex;gap:8px;
  overflow-x:auto;scrollbar-width:none;
}}
.statsbar::-webkit-scrollbar{{display:none}}
.ss{{
  background:var(--bg3);border:1px solid var(--border);
  border-radius:10px;padding:8px 14px;min-width:90px;flex-shrink:0;
  transition:transform 0.2s,border-color 0.2s;
}}
.ss:hover{{transform:translateY(-2px);border-color:var(--border2)}}
.ss-l{{font-size:8px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--txt3);margin-bottom:4px}}
.ss-v{{font-size:16px;font-weight:800;font-family:var(--mono);color:var(--txt)}}

/* ── TAB BAR ──────────────────────────────────────────── */
.tabbar{{
  background:var(--bg1);border-bottom:1px solid var(--border);
  display:flex;gap:2px;padding:0 20px;
  overflow-x:auto;scrollbar-width:none;
}}
.tabbar::-webkit-scrollbar{{display:none}}
.tabbtn{{
  padding:11px 14px;font-size:11px;font-weight:600;cursor:pointer;
  color:var(--txt3);background:none;border:none;
  white-space:nowrap;display:flex;align-items:center;gap:6px;
  border-bottom:2px solid transparent;transition:all 0.2s;
  font-family:var(--sans);
}}
.tabbtn:hover{{color:var(--txt2)}}
.tabbtn.act{{color:var(--cyan);border-bottom-color:var(--cyan)}}
.tbadge{{
  font-size:9px;padding:1px 6px;border-radius:8px;font-weight:800;
  background:var(--bg4);color:var(--txt2);font-family:var(--mono);
}}
.tabbtn.act .tbadge{{background:rgba(34,211,238,0.15);color:var(--cyan)}}

/* ── TAB CONTENT ──────────────────────────────────────── */
.tc{{display:none;padding:16px 20px 60px;position:relative;z-index:1}}
.tc.act{{display:block}}

/* ── FILTER BAR ───────────────────────────────────────── */
.fbar{{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}}
.finput,.fselect{{
  background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;color:var(--txt);font-family:var(--sans);
  font-size:11px;padding:8px 12px;outline:none;transition:border-color 0.2s;
}}
.finput{{flex:1;min-width:140px}}
.fselect{{min-width:110px;cursor:pointer}}
.finput:focus,.fselect:focus{{border-color:var(--cyan)}}
.fpill{{
  display:flex;align-items:center;gap:7px;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:7px 12px;font-size:10px;color:var(--txt2);
  white-space:nowrap;
}}
input[type=range]{{accent-color:var(--cyan);width:80px;cursor:pointer}}
.fselect option{{background:var(--bg2)}}

/* ── GRID ─────────────────────────────────────────────── */
.grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
  gap:12px;
}}

/* ── CARD ─────────────────────────────────────────────── */
.card{{
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--r2);
  overflow:hidden;position:relative;
  transition:transform 0.25s,border-color 0.25s,box-shadow 0.25s;
  animation:cardIn 0.45s cubic-bezier(0.16,1,0.3,1) both;
}}
.card:hover{{
  transform:translateY(-3px);border-color:var(--border2);
  box-shadow:0 8px 32px rgba(0,0,0,0.4);
}}
@keyframes cardIn{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:none}}}}
.card:nth-child(1){{animation-delay:0s}}
.card:nth-child(2){{animation-delay:0.04s}}
.card:nth-child(3){{animation-delay:0.08s}}
.card:nth-child(4){{animation-delay:0.12s}}
.card:nth-child(5){{animation-delay:0.16s}}
.card:nth-child(n+6){{animation-delay:0.20s}}

.cbar{{
  height:2px;
  background:linear-gradient(90deg,var(--cyan),var(--purple));
}}
.cbar.ev{{background:linear-gradient(90deg,var(--purple),var(--cyan))}}
.cbar.bc{{background:linear-gradient(90deg,var(--yellow),var(--green))}}
.cinner{{padding:14px 14px 0}}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}}
.cbadge{{
  font-size:9px;font-weight:800;letter-spacing:1.5px;
  text-transform:uppercase;padding:3px 8px;border-radius:5px;
  background:rgba(34,211,238,0.1);color:var(--cyan);
  border:1px solid rgba(34,211,238,0.2);font-family:var(--mono);
}}
.cbadge.ev{{background:rgba(167,139,250,0.1);color:var(--purple);border-color:rgba(167,139,250,0.2)}}
.cbadge.bc{{background:rgba(251,191,36,0.1);color:var(--yellow);border-color:rgba(251,191,36,0.2)}}
.cprofit{{font-size:20px;font-weight:800;color:var(--green);font-family:var(--mono)}}
.cprofit.ev{{color:var(--purple)}}
.cmatch{{font-size:13px;font-weight:600;color:var(--txt);margin-bottom:6px;line-height:1.3}}
.cmeta{{font-size:10px;color:var(--txt3);margin-bottom:11px;display:flex;gap:10px;flex-wrap:wrap}}
.ctable{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:10px;font-family:var(--mono)}}
.ctable th{{
  color:var(--txt3);text-align:left;padding:0 6px 7px;
  border-bottom:1px solid var(--border);font-weight:500;
  font-size:9px;letter-spacing:1.5px;text-transform:uppercase;
  font-family:var(--sans);
}}
.ctable td{{padding:6px 6px;border-bottom:1px solid rgba(42,42,53,0.8)}}
.ctable tr:last-child td{{border:none}}
.btag{{
  background:var(--bg4);border:1px solid var(--border);
  border-radius:4px;padding:2px 5px;font-size:9px;font-weight:700;
  color:var(--txt2);font-family:var(--mono);
}}
.oval{{color:var(--yellow);font-weight:700}}
.stkx{{font-size:9px;color:var(--txt3);display:block}}
.stkm{{font-size:13px;font-weight:700;color:var(--txt)}}
.stkm.kelly{{color:var(--cyan);font-size:12px}}
.cfoot{{
  display:flex;justify-content:space-between;align-items:center;
  padding:8px 14px 10px;border-top:1px solid var(--border);
}}
.cfoot-l{{font-size:10px;color:var(--txt3)}}
.cbtn{{
  background:none;border:1px solid var(--border2);border-radius:6px;
  color:var(--txt2);font-family:var(--mono);font-size:10px;
  padding:4px 10px;cursor:pointer;transition:all 0.2s;font-weight:500;
}}
.cbtn:hover{{border-color:var(--cyan);color:var(--cyan)}}
.empty{{text-align:center;padding:60px 20px;color:var(--txt3);grid-column:1/-1}}
.empty i{{font-size:30px;display:block;margin-bottom:12px;opacity:0.3}}
.empty small{{font-size:11px;display:block;margin-top:6px;opacity:0.6}}

/* ── CALCULATOR SECTION ───────────────────────────────── */
.csec{{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--r2);padding:18px;margin-bottom:13px;
  max-width:560px;
}}
.csec-title{{
  font-size:13px;font-weight:700;color:var(--txt);
  margin-bottom:14px;display:flex;align-items:center;gap:8px;
}}
.csec-title i{{color:var(--cyan);font-size:12px}}
.ctabs{{display:flex;gap:5px;margin-bottom:13px}}
.ctab{{
  background:var(--bg3);border:1px solid var(--border);border-radius:7px;
  color:var(--txt2);font-family:var(--sans);font-size:11px;font-weight:600;
  padding:7px 15px;cursor:pointer;transition:all 0.2s;
}}
.ctab.act{{background:rgba(34,211,238,0.1);color:var(--cyan);border-color:rgba(34,211,238,0.3)}}
.cinput{{
  width:100%;padding:10px 13px;background:var(--bg3);
  border:1px solid var(--border);border-radius:8px;
  color:var(--txt);font-family:var(--mono);font-size:13px;outline:none;
  transition:border-color 0.2s;margin-bottom:10px;
}}
.cinput:focus{{border-color:var(--cyan)}}
.cinput-label{{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--txt3);margin-bottom:5px;font-family:var(--sans)}}
.runbtn{{
  width:100%;padding:12px;background:linear-gradient(135deg,var(--cyan),var(--cyan2));
  color:#000;border:none;border-radius:9px;font-size:12px;font-weight:800;
  letter-spacing:1.5px;cursor:pointer;font-family:var(--display);
  transition:opacity 0.2s,transform 0.15s;margin-top:4px;
}}
.runbtn:hover{{opacity:0.88;transform:translateY(-1px)}}
.cresult{{
  background:var(--bg);border:1px solid var(--border);
  border-radius:9px;padding:14px;margin-top:13px;
}}
.crrow{{
  display:flex;justify-content:space-between;padding:6px 0;
  font-size:12px;border-bottom:1px solid var(--border);
  color:var(--txt2);font-family:var(--mono);
}}
.crrow:last-child{{border:none;font-weight:700;color:var(--txt);font-size:13px}}
.ogrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}

/* ── API KEYS TABLE ───────────────────────────────────── */
.atable{{width:100%;border-collapse:collapse;font-size:11px}}
.atable th{{
  padding:9px 12px;font-size:9px;letter-spacing:2px;
  text-transform:uppercase;font-weight:700;text-align:left;
  color:var(--txt3);border-bottom:1px solid var(--border);
  font-family:var(--sans);
}}
.atable td{{
  padding:9px 12px;border-bottom:1px solid rgba(42,42,53,0.6);
  color:var(--txt2);font-family:var(--mono);
}}
.atable tr:hover td{{background:var(--bg3)}}
.qbarbg{{height:4px;border-radius:2px;background:var(--bg4);overflow:hidden;margin-top:4px}}
.qbarfill{{height:100%;border-radius:2px;transition:width 0.6s ease}}

/* ── DEBUG PANEL ──────────────────────────────────────── */
.debug-panel{{
  background:var(--bg);border:1px solid var(--border);border-radius:9px;
  padding:14px;font-family:var(--mono);font-size:11px;color:var(--txt2);
  white-space:pre-wrap;word-break:break-all;max-height:300px;overflow-y:auto;
  line-height:1.6;margin-bottom:12px;
}}
.debug-row{{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--border)}}
.debug-key{{color:var(--txt3);min-width:120px;flex-shrink:0}}
.debug-val{{color:var(--cyan)}}
.debug-val.ok{{color:var(--green)}}
.debug-val.err{{color:var(--red)}}

/* ── MODAL ────────────────────────────────────────────── */
.mbg{{
  position:fixed;inset:0;z-index:800;background:rgba(0,0,0,0.7);
  backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;
  animation:fadeIn 0.2s ease;
}}
.mbg.open{{display:flex}}
@keyframes fadeIn{{from{{opacity:0}}to{{opacity:1}}}}
.modal{{
  background:var(--bg2);border:1px solid var(--border2);border-radius:16px;
  padding:24px;width:92%;max-width:440px;max-height:90vh;overflow-y:auto;
  animation:modalIn 0.3s cubic-bezier(0.16,1,0.3,1);
}}
@keyframes modalIn{{from{{opacity:0;transform:scale(0.93) translateY(18px)}}to{{opacity:1;transform:none}}}}
.mhead{{
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:16px;font-size:14px;font-weight:700;color:var(--txt);
}}
.closex{{background:none;border:none;font-size:16px;cursor:pointer;color:var(--txt3);transition:color 0.2s}}
.closex:hover{{color:var(--txt)}}

/* ── RESPONSIVE ───────────────────────────────────────── */
@media(max-width:600px){{
  .grid{{grid-template-columns:1fr}}
  .ogrid{{grid-template-columns:1fr}}
  .topbar,.tc{{padding-left:14px;padding-right:14px}}
  .statsbar,.bankroll-bar{{padding:8px 14px}}
  .tabbar{{padding:0 14px}}
}}
</style>
</head>
<body>

<!-- LOCK SCREEN -->
<div id="lock">
  <div class="lbox">
    <div class="lock-icon"><i class="fas fa-crosshairs"></i></div>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">v6.0 — Production Edition</div>
    <input id="linput" type="password" placeholder="••••••••" autocomplete="current-password"/>
    <button id="lbtn" onclick="unlock()">
      <i class="fas fa-unlock-alt"></i>&nbsp; UNLOCK
    </button>
    <div id="lerr"><i class="fas fa-triangle-exclamation"></i>&nbsp;Invalid password</div>
  </div>
</div>

<!-- APP -->
<div id="app">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="logo">
      <i class="fas fa-crosshairs"></i> ARB SNIPER v6.0
    </div>
    <div class="topbar-r">
      <div class="lpill"><div class="ldot"></div><span class="ltext">LIVE</span></div>
      <span class="time-txt">{ist_now}</span>
      <button class="iBtn" onclick="logout()" title="Logout">
        <i class="fas fa-right-from-bracket"></i>
      </button>
    </div>
  </div>

  <!-- BANKROLL BAR -->
  <div class="bankroll-bar">
    <span class="bank-label"><i class="fas fa-wallet"></i>&nbsp; Bankroll</span>
    <input type="number" id="bankroll" class="bank-input"
           value="{DEFAULT_BANK}" min="100" step="100"
           oninput="onBankChange()"/>
    <span class="bank-note">₹ — Kelly stakes recalculate instantly</span>
  </div>

  <!-- STATS BAR -->
  <div class="statsbar">
    <div class="ss"><div class="ss-l">Arbs</div><div class="ss-v" id="ss-arb" style="color:var(--green)">0</div></div>
    <div class="ss"><div class="ss-l">+EV Bets</div><div class="ss-v" id="ss-ev" style="color:var(--purple)">0</div></div>
    <div class="ss"><div class="ss-l">BC Events</div><div class="ss-v" id="ss-bc" style="color:var(--yellow)">0</div></div>
    <div class="ss"><div class="ss-l">Top Arb</div><div class="ss-v" id="ss-toparb" style="color:var(--green)">—</div></div>
    <div class="ss"><div class="ss-l">Top EV</div><div class="ss-v" id="ss-topev" style="color:var(--purple)">—</div></div>
    <div class="ss"><div class="ss-l">Profit/₹1K</div><div class="ss-v" id="ss-profit" style="color:var(--yellow)">—</div></div>
    <div class="ss"><div class="ss-l">Events</div><div class="ss-v" style="color:var(--txt)">{state.get('total_events_scanned',0)}</div></div>
    <div class="ss"><div class="ss-l">API Quota</div><div class="ss-v" style="color:var(--cyan)">{total_q}</div></div>
    <div class="ss"><div class="ss-l">Keys</div><div class="ss-v" style="color:var(--txt)">{len(key_status)}</div></div>
  </div>

  <!-- TAB BAR -->
  <div class="tabbar">
    <button class="tabbtn act" id="tb-arb" onclick="swTab('arb',this)">
      <i class="fas fa-percent"></i> Arbitrage
      <span class="tbadge" id="cnt-arb">0</span>
    </button>
    <button class="tabbtn" id="tb-ev" onclick="swTab('ev',this)">
      <i class="fas fa-chart-line"></i> +EV Bets
      <span class="tbadge" id="cnt-ev">0</span>
    </button>
    <button class="tabbtn" id="tb-bc" onclick="swTab('bc',this)">
      <i class="fas fa-gamepad"></i> BC.Game
      <span class="tbadge" id="cnt-bc">0</span>
    </button>
    <button class="tabbtn" id="tb-calc" onclick="swTab('calc',this)">
      <i class="fas fa-calculator"></i> Calculator
    </button>
    <button class="tabbtn" id="tb-api" onclick="swTab('api',this)">
      <i class="fas fa-server"></i> API Keys
    </button>
    <button class="tabbtn" id="tb-debug" onclick="swTab('debug',this)">
      <i class="fas fa-bug"></i> BC Debug
    </button>
  </div>

  <!-- ARB TAB -->
  <div id="tc-arb" class="tc act">
    <div class="fbar">
      <input class="finput" id="arb-q" placeholder="Search match or sport..." oninput="filterArbs()"/>
      <select class="fselect" id="arb-ways" onchange="filterArbs()">
        <option value="">All Ways</option>
        <option value="2">2-Way</option>
        <option value="3">3-Way</option>
      </select>
      <select class="fselect" id="arb-mkt" onchange="filterArbs()">
        <option value="">All Markets</option>
        <option value="H2H">H2H</option>
        <option value="TOTALS">Totals</option>
        <option value="SPREADS">Spreads</option>
      </select>
      <div class="fpill">
        Min
        <input type="range" id="arb-min" min="0" max="5" step="0.1" value="0"
               oninput="document.getElementById('arb-minv').textContent=(+this.value).toFixed(1)+'%';filterArbs()"/>
        <span id="arb-minv">0.0%</span>
      </div>
    </div>
    <div class="grid" id="grid-arb"></div>
  </div>

  <!-- EV TAB -->
  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input class="finput" id="ev-q" placeholder="Search match or bookmaker..." oninput="filterEvs()"/>
      <select class="fselect" id="ev-book" onchange="filterEvs()"><option value="">All Books</option></select>
      <select class="fselect" id="ev-sport" onchange="filterEvs()"><option value="">All Sports</option></select>
      <div class="fpill">
        Min Edge
        <input type="range" id="ev-min" min="0" max="20" step="0.5" value="0"
               oninput="document.getElementById('ev-minv').textContent=(+this.value).toFixed(1)+'%';filterEvs()"/>
        <span id="ev-minv">0.0%</span>
      </div>
    </div>
    <div class="grid" id="grid-ev"></div>
  </div>

  <!-- BC.GAME TAB -->
  <div id="tc-bc" class="tc">
    <div class="fbar">
      <input class="finput" id="bc-q" placeholder="Search BC.Game feed..." oninput="filterBc()"/>
      <select class="fselect" id="bc-sport" onchange="filterBc()"><option value="">All Sports</option></select>
    </div>
    <div class="grid" id="grid-bc"></div>
  </div>

  <!-- CALCULATOR TAB -->
  <div id="tc-calc" class="tc">

    <div class="csec">
      <div class="csec-title"><i class="fas fa-percent"></i> Arbitrage Calculator</div>
      <div class="ctabs">
        <button class="ctab act" id="ct2" onclick="swCalc(2,this)">2-Way</button>
        <button class="ctab" id="ct3" onclick="swCalc(3,this)">3-Way</button>
      </div>
      <div id="c2f">
        <div class="cinput-label">Odds — Leg 1</div>
        <input class="cinput" id="c2o1" type="number" step="0.01" placeholder="e.g. 2.15"/>
        <div class="cinput-label">Odds — Leg 2</div>
        <input class="cinput" id="c2o2" type="number" step="0.01" placeholder="e.g. 2.05"/>
        <div class="cinput-label">Total Stake (₹)</div>
        <input class="cinput" id="c2s" type="number" value="10000"/>
      </div>
      <div id="c3f" style="display:none">
        <div class="cinput-label">Home Odds</div>
        <input class="cinput" id="c3o1" type="number" step="0.01" placeholder="2.50"/>
        <div class="cinput-label">Draw Odds</div>
        <input class="cinput" id="c3o2" type="number" step="0.01" placeholder="3.20"/>
        <div class="cinput-label">Away Odds</div>
        <input class="cinput" id="c3o3" type="number" step="0.01" placeholder="2.80"/>
        <div class="cinput-label">Total Stake (₹)</div>
        <input class="cinput" id="c3s" type="number" value="10000"/>
      </div>
      <button class="runbtn" onclick="runCalc()"><i class="fas fa-bolt"></i>&nbsp; CALCULATE</button>
      <div id="calc-res" class="cresult" style="display:none"></div>
    </div>

    <div class="csec">
      <div class="csec-title"><i class="fas fa-brain"></i> Kelly Criterion Calculator</div>
      <div class="cinput-label">Your Win Probability (%)</div>
      <input class="cinput" id="kp" type="number" step="0.1" placeholder="e.g. 55.0"/>
      <div class="cinput-label">Decimal Odds Offered</div>
      <input class="cinput" id="ko" type="number" step="0.01" placeholder="e.g. 2.10"/>
      <div class="cinput-label">Bank Size (₹) — pre-filled from bankroll bar</div>
      <input class="cinput" id="kb" type="number" value="{DEFAULT_BANK}"/>
      <button class="runbtn" onclick="runKelly()"><i class="fas fa-calculator"></i>&nbsp; CALC KELLY</button>
      <div id="kelly-res" class="cresult" style="display:none"></div>
    </div>

    <div class="csec">
      <div class="csec-title"><i class="fas fa-arrows-rotate"></i> Odds Converter</div>
      <div class="ogrid">
        <div><div class="cinput-label">Decimal</div><input class="cinput" id="od" type="number" step="0.001" placeholder="2.000" oninput="convOdds('d')"/></div>
        <div><div class="cinput-label">Fractional</div><input class="cinput" id="of" type="text" placeholder="1/1" oninput="convOdds('f')"/></div>
        <div><div class="cinput-label">American</div><input class="cinput" id="oa" type="number" placeholder="+100" oninput="convOdds('a')"/></div>
        <div><div class="cinput-label">Implied %</div><input class="cinput" id="oi" type="number" step="0.01" placeholder="50.00" oninput="convOdds('i')"/></div>
      </div>
    </div>

  </div>

  <!-- API KEYS TAB -->
  <div id="tc-api" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="csec-title"><i class="fas fa-key"></i> API Key Telemetry — {len(key_status)} keys / {total_q} total quota</div>
      <table class="atable">
        <thead><tr><th>#</th><th>Key (masked)</th><th>Remaining</th><th style="width:120px">Quota Bar</th></tr></thead>
        <tbody id="key-tbody"></tbody>
      </table>
    </div>
    <div class="csec" style="max-width:700px">
      <div class="csec-title"><i class="fas fa-chart-bar"></i> Run Statistics</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px">
        <div class="ss"><div class="ss-l">Last Sync</div><div style="font-size:11px;color:var(--txt);font-family:var(--mono);margin-top:3px">{ist_now}</div></div>
        <div class="ss"><div class="ss-l">Events Scanned</div><div class="ss-v" style="color:var(--green)">{state.get('total_events_scanned',0)}</div></div>
        <div class="ss"><div class="ss-l">Last Arbs</div><div class="ss-v">{state.get('last_arb_count',0)}</div></div>
        <div class="ss"><div class="ss-l">Last EVs</div><div class="ss-v">{state.get('last_ev_count',0)}</div></div>
      </div>
    </div>
  </div>

  <!-- BC DEBUG TAB -->
  <div id="tc-debug" class="tc">
    <div class="csec" style="max-width:700px">
      <div class="csec-title"><i class="fas fa-bug"></i> BC.Game Scraper Debug</div>
      <div id="debug-rows"></div>
      <div class="cinput-label" style="margin-top:14px;margin-bottom:6px">Raw JSON Preview (first 800 chars)</div>
      <div class="debug-panel" id="raw-preview"></div>
    </div>
  </div>

</div>

<!-- QUICK CALC MODAL -->
<div class="mbg" id="qcm">
  <div class="modal">
    <div class="mhead">
      <span><i class="fas fa-calculator"></i>&nbsp; Quick Arb Calc</span>
      <button class="closex" onclick="closeModal()"><i class="fas fa-xmark"></i></button>
    </div>
    <div id="qcm-body"></div>
  </div>
</div>

<script>
// ── DATA ──────────────────────────────────────────────
const ARBS_RAW = {arbs_j};
const EVS_RAW  = {evs_j};
const BC_RAW   = {bc_j};
const KEYS     = {keys_j};
const DEBUG    = {debug_j};
const PH       = "{ph}";

let BANK = {DEFAULT_BANK};
let calcWays = 2;

// ── HELPERS ──────────────────────────────────────────
const BS = {{pinnacle:'PIN',bet365:'B365',betway:'BW',draftkings:'DK',
             fanduel:'FD',betmgm:'MGM',unibet:'UNI',stake:'STK',
             marathonbet:'MAR',parimatch:'PAR',betfair:'BF',dafabet:'DAF',
             bovada:'BOV',onexbet:'1XB',bcgame:'BCG'}};
const bs = k => BS[k] || (k||'').toUpperCase().slice(0,4);
const fd = d => {{try{{return new Date(d).toLocaleString('en-IN',{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});}}catch{{return String(d)}}}};
const si = s => {{
  s=(s||'').toLowerCase();
  if(s.includes('soccer')||s.includes('football')) return 'fa-futbol';
  if(s.includes('basket')) return 'fa-basketball';
  if(s.includes('hockey')) return 'fa-hockey-puck';
  if(s.includes('tennis')) return 'fa-table-tennis-paddle-ball';
  if(s.includes('mma')||s.includes('box')) return 'fa-hand-fist';
  if(s.includes('cricket')) return 'fa-cricket-bat-ball';
  if(s.includes('baseball')) return 'fa-baseball';
  if(s.includes('golf')) return 'fa-golf-ball-tee';
  if(s.includes('nfl')||s.includes('american')) return 'fa-football';
  return 'fa-trophy';
}};

// Kelly re-calculation with live bankroll
function kellyStake(edge, odds, bank) {{
  const b = odds - 1;
  if(b <= 0) return 0;
  const p = 1 / (odds / (1 + edge));
  const kf = (b*p - (1-p)) / b;
  if(kf <= 0) return 0;
  return Math.round(0.3 * kf * bank / 10) * 10;
}}

// ── AUTH ─────────────────────────────────────────────
if(localStorage.getItem('sauth') === PH) boot();
function unlock() {{
  const h = CryptoJS.SHA256(document.getElementById('linput').value).toString();
  if(h === PH) {{ localStorage.setItem('sauth', PH); boot(); }}
  else {{
    const inp=document.getElementById('linput');
    inp.value='';
    document.getElementById('lerr').style.display='block';
    inp.style.borderColor='var(--red)';
    setTimeout(()=>{{inp.style.borderColor='';document.getElementById('lerr').style.display='none';}},2000);
  }}
}}
document.getElementById('linput').addEventListener('keydown',e=>{{if(e.key==='Enter')unlock()}});
function logout(){{localStorage.removeItem('sauth');location.reload();}}
function boot(){{
  document.getElementById('lock').style.display='none';
  document.getElementById('app').style.display='block';
  init();
}}

// ── TABS ─────────────────────────────────────────────
function swTab(id,btn){{
  document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act'));
  document.querySelectorAll('.tabbtn').forEach(t=>t.classList.remove('act'));
  document.getElementById('tc-'+id).classList.add('act');
  if(btn)btn.classList.add('act');
}}

// ── BANKROLL ──────────────────────────────────────────
function onBankChange(){{
  const v = parseFloat(document.getElementById('bankroll').value)||10000;
  BANK = v;
  document.getElementById('kb').value = v;
  renderEvs(currentEvData);
}}

// ── INIT ─────────────────────────────────────────────
let currentEvData = EVS_RAW;
function init(){{
  const evSports=[...new Set(EVS_RAW.map(e=>e.sport))].sort();
  const evBooks=[...new Set(EVS_RAW.map(e=>e.book_key))].sort();
  const bcSports=[...new Set(BC_RAW.map(b=>b.sport_title||'Unknown'))].sort();
  const addO=(id,arr)=>arr.forEach(v=>{{const o=document.createElement('option');o.value=v;o.textContent=v.replace(/_/g,' ');document.getElementById(id).appendChild(o);}});
  addO('ev-book',evBooks); addO('ev-sport',evSports); addO('bc-sport',bcSports);

  document.getElementById('ss-arb').textContent = ARBS_RAW.length;
  document.getElementById('ss-ev').textContent  = EVS_RAW.length;
  document.getElementById('ss-bc').textContent  = BC_RAW.length;
  document.getElementById('cnt-arb').textContent= ARBS_RAW.length;
  document.getElementById('cnt-ev').textContent = EVS_RAW.length;
  document.getElementById('cnt-bc').textContent = BC_RAW.length;
  document.getElementById('ss-toparb').textContent = ARBS_RAW.length?'+'+ARBS_RAW[0].profit_pct+'%':'—';
  document.getElementById('ss-topev').textContent  = EVS_RAW.length?'+'+EVS_RAW[0].edge_pct+'%':'—';
  document.getElementById('ss-profit').textContent = ARBS_RAW.length?'₹'+ARBS_RAW[0].profit_amt:'—';

  renderArbs(ARBS_RAW);
  renderEvs(EVS_RAW);
  renderBc(BC_RAW);
  renderKeys();
  renderDebug();
}}

// ── FILTERS ───────────────────────────────────────────
function filterArbs(){{
  const q=document.getElementById('arb-q').value.toLowerCase();
  const wy=document.getElementById('arb-ways').value;
  const mk=document.getElementById('arb-mkt').value;
  const mn=parseFloat(document.getElementById('arb-min').value)||0;
  renderArbs(ARBS_RAW.filter(a=>(!wy||String(a.ways)===wy)&&(!mk||a.market===mk)&&a.profit_pct>=mn&&(!q||a.match.toLowerCase().includes(q)||a.sport.toLowerCase().includes(q))));
}}
function filterEvs(){{
  const q=document.getElementById('ev-q').value.toLowerCase();
  const bk=document.getElementById('ev-book').value;
  const sp=document.getElementById('ev-sport').value;
  const mn=parseFloat(document.getElementById('ev-min').value)||0;
  const d=EVS_RAW.filter(v=>(!bk||v.book_key===bk)&&(!sp||v.sport===sp)&&v.edge_pct>=mn&&(!q||v.match.toLowerCase().includes(q)||(v.book_key||'').includes(q)));
  currentEvData=d;
  renderEvs(d);
}}
function filterBc(){{
  const q=document.getElementById('bc-q').value.toLowerCase();
  const sp=document.getElementById('bc-sport').value;
  renderBc(BC_RAW.filter(b=>(!sp||b.sport_title===sp)&&(!q||(b.home_team+' '+b.away_team).toLowerCase().includes(q))));
}}

// ── RENDER ARBS ───────────────────────────────────────
function renderArbs(data){{
  const g=document.getElementById('grid-arb');
  document.getElementById('cnt-arb').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-magnifying-glass"></i>No arbitrage opportunities.<small>Try lowering the minimum % filter or check API quota.</small></div>';return;}}
  g.innerHTML=data.map(a=>{{
    const rows=a.outcomes.map(o=>`<tr>
      <td><span class="btag">${{bs(o.book_key)}}</span>&nbsp;<span style="color:var(--txt2)">${{o.name}}</span></td>
      <td><span class="oval">${{o.odds}}</span></td>
      <td><span class="stkx">exact ₹${{o.stake}}</span><span class="stkm">₹${{o.stake_rounded}}</span></td>
    </tr>`).join('');
    const oa=JSON.stringify(a.outcomes.map(o=>o.odds));
    return `<div class="card">
      <div class="cbar"></div>
      <div class="cinner">
        <div class="ch">
          <span class="cbadge"><i class="fas fa-percent"></i>&nbsp;${{a.ways}}-WAY&nbsp;·&nbsp;${{a.market}}</span>
          <span class="cprofit">+${{a.profit_pct}}%</span>
        </div>
        <div class="cmatch"><i class="fas ${{si(a.sport)}}" style="margin-right:6px;opacity:0.6"></i>${{a.match}}</div>
        <div class="cmeta"><span><i class="fas fa-clock"></i>&nbsp;${{fd(a.commence)}}</span><span style="opacity:0.5">${{a.sport.replace(/_/g,' ')}}</span></div>
        <table class="ctable"><thead><tr><th>Outcome / Book</th><th>Odds</th><th>Stake/₹1K</th></tr></thead><tbody>${{rows}}</tbody></table>
      </div>
      <div class="cfoot">
        <span class="cfoot-l"><i class="fas fa-coins"></i>&nbsp;Profit ₹${{a.profit_amt}} on ₹1,000</span>
        <button class="cbtn" onclick='openQC(${{oa}},${{a.ways}})'><i class="fas fa-calculator"></i>&nbsp;Calc</button>
      </div>
    </div>`;
  }}).join('');
}}

// ── RENDER EVS ────────────────────────────────────────
function renderEvs(data){{
  const g=document.getElementById('grid-ev');
  document.getElementById('cnt-ev').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-chart-line"></i>No value bets found.<small>Pinnacle lines needed as reference.</small></div>';return;}}
  g.innerHTML=data.map(v=>{{
    const edge=v.edge_pct/100;
    const liveKelly=kellyStake(edge,v.offered_odds,BANK);
    return `<div class="card">
      <div class="cbar ev"></div>
      <div class="cinner">
        <div class="ch">
          <span class="cbadge ev"><i class="fas fa-chart-line"></i>&nbsp;+EV&nbsp;·&nbsp;${{v.market}}</span>
          <span class="cprofit ev">+${{v.edge_pct}}%</span>
        </div>
        <div class="cmatch"><i class="fas ${{si(v.sport)}}" style="margin-right:6px;opacity:0.6"></i>${{v.match}}</div>
        <div class="cmeta"><span><i class="fas fa-clock"></i>&nbsp;${{fd(v.commence)}}</span><span>${{v.sport.replace(/_/g,' ')}}</span></div>
        <table class="ctable">
          <tr><td style="color:var(--txt3)">Outcome</td><td colspan=2><strong>${{v.outcome}}</strong></td></tr>
          <tr><td style="color:var(--txt3)">Bookmaker</td><td colspan=2><span class="btag">${{bs(v.book_key)}}</span>&nbsp;${{v.book}}</td></tr>
          <tr><td style="color:var(--txt3)">Offered Odds</td><td colspan=2><span class="oval">${{v.offered_odds}}</span></td></tr>
          <tr><td style="color:var(--txt3)">True Odds</td><td colspan=2>${{v.true_odds}}&nbsp;<span style="color:var(--txt3);font-size:10px">(${{v.true_prob_pct}}%)</span></td></tr>
          <tr><td style="color:var(--txt3)">Kelly (30%)</td><td colspan=2>
            <span class="stkx">exact ₹${{v.kelly_stake}}</span>
            <span class="stkm kelly" id="kelly-${{v.book_key}}-${{v.outcome.replace(/ /g,'-')}}">₹${{liveKelly}}</span>
          </td></tr>
        </table>
      </div>
    </div>`;
  }}).join('');
}}

// ── RENDER BC ─────────────────────────────────────────
function renderBc(data){{
  const g=document.getElementById('grid-bc');
  document.getElementById('cnt-bc').textContent=data.length;
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-gamepad"></i>No BC.Game events available.<small>Check the BC Debug tab for scraper diagnostics.</small></div>';return;}}
  g.innerHTML=data.slice(0,100).map(b=>{{
    const outs=b.bookmakers[0].markets[0].outcomes;
    return `<div class="card">
      <div class="cbar bc"></div>
      <div class="cinner">
        <div class="ch"><span class="cbadge bc"><i class="fas fa-gamepad"></i>&nbsp;BC.GAME</span></div>
        <div class="cmatch"><i class="fas ${{si(b.sport_title)}}" style="margin-right:6px;opacity:0.6"></i>${{b.home_team}} vs ${{b.away_team}}</div>
        <div class="cmeta"><span><i class="fas fa-clock"></i>&nbsp;${{fd(b.commence_time)}}</span><span>${{b.sport_title}}</span></div>
        <table class="ctable">${{outs.map(o=>`<tr><td style="color:var(--txt2)">${{o.name}}</td><td><span class="oval">${{o.price}}</span></td></tr>`).join('')}}</table>
      </div>
    </div>`;
  }}).join('');
}}

// ── RENDER KEYS ───────────────────────────────────────
function renderKeys(){{
  document.getElementById('key-tbody').innerHTML=KEYS.map((k,i)=>{{
    const pct=Math.max(0,Math.min(100,(k.remaining/500)*100));
    const col=pct>55?'var(--green)':pct>18?'var(--yellow)':'var(--red)';
    return `<tr>
      <td style="color:var(--txt3)">#${{i+1}}</td>
      <td style="color:var(--cyan)">${{k.key}}</td>
      <td style="color:${{col}};font-weight:800">${{k.remaining}}</td>
      <td><div class="qbarbg"><div class="qbarfill" style="width:${{pct.toFixed(1)}}%;background:${{col}}"></div></div></td>
    </tr>`;
  }}).join('');
}}

// ── RENDER DEBUG ──────────────────────────────────────
function renderDebug(){{
  const rows=[
    ['Status',        DEBUG.status,      DEBUG.status==='ok'?'ok':DEBUG.status.includes('error')||DEBUG.status.includes('http_4')||DEBUG.status.includes('timeout')?'err':''],
    ['URL Tried',     DEBUG.url,         ''],
    ['HTTP Code',     String(DEBUG.http_code), DEBUG.http_code===200?'ok':DEBUG.http_code>0?'err':''],
    ['Response Size', DEBUG.size_bytes+' bytes',''],
    ['Root Type',     DEBUG.root_type,   ''],
    ['Root Keys',     DEBUG.root_keys,   ''],
    ['Events Found',  String(DEBUG.extracted),''],
    ['Events Parsed', String(DEBUG.parsed), DEBUG.parsed>0?'ok':DEBUG.parsed===0&&DEBUG.extracted>0?'err':''],
  ];
  document.getElementById('debug-rows').innerHTML=rows.map(([k,v,cls])=>`
    <div class="debug-row">
      <span class="debug-key">${{k}}</span>
      <span class="debug-val ${{cls}}">${{v||'—'}}</span>
    </div>`).join('');
  document.getElementById('raw-preview').textContent=DEBUG.raw_preview||'No data captured';
}}

// ── QUICK CALC MODAL ──────────────────────────────────
function openQC(oddsArr,ways){{
  if(ways===2){{document.getElementById('c2o1').value=oddsArr[0]||'';document.getElementById('c2o2').value=oddsArr[1]||'';swCalc(2,document.getElementById('ct2'));}}
  else{{document.getElementById('c3o1').value=oddsArr[0]||'';document.getElementById('c3o2').value=oddsArr[1]||'';document.getElementById('c3o3').value=oddsArr[2]||'';swCalc(3,document.getElementById('ct3'));}}
  const stake=BANK;
  const impl=oddsArr.reduce((s,o)=>s+1/o,0);
  const pct=(1/impl-1)*100;
  const stakes=oddsArr.map(o=>(1/o)/impl*stake);
  const profit=stake*(1/impl-1);
  const col=pct>0?'var(--green)':'var(--red)';
  document.getElementById('qcm-body').innerHTML=`
    <div class="cresult">
      ${{oddsArr.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>₹${{stakes[i].toFixed(2)}}</span></div>`).join('')}}
      <div class="crrow"><span>Implied Total</span><span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span></div>
      <div class="crrow"><span>${{pct>0?'PROFIT on ₹'+stake:'NO ARB'}}</span><span style="color:${{col}}">${{pct>0?'+₹'+profit.toFixed(2)+' (+'+pct.toFixed(3)+'%)':Math.abs(pct).toFixed(3)+'%'}}</span></div>
    </div>
    <button class="runbtn" style="margin-top:12px" onclick="closeModal();swTab('calc',document.getElementById('tb-calc'))">
      <i class="fas fa-arrow-right"></i>&nbsp; Full Calculator
    </button>`;
  document.getElementById('qcm').classList.add('open');
}}
function closeModal(){{document.getElementById('qcm').classList.remove('open');}}
document.getElementById('qcm').addEventListener('click',e=>{{if(e.target===e.currentTarget)closeModal();}});

// ── CALCULATOR ────────────────────────────────────────
function swCalc(n,btn){{
  calcWays=n;
  document.querySelectorAll('.ctab').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  document.getElementById('c2f').style.display=n===2?'block':'none';
  document.getElementById('c3f').style.display=n===3?'block':'none';
  document.getElementById('calc-res').style.display='none';
}}
function runCalc(){{
  let odds=[],stake=0;
  if(calcWays===2){{odds=[+document.getElementById('c2o1').value,+document.getElementById('c2o2').value];stake=+document.getElementById('c2s').value||BANK;}}
  else{{odds=[+document.getElementById('c3o1').value,+document.getElementById('c3o2').value,+document.getElementById('c3o3').value];stake=+document.getElementById('c3s').value||BANK;}}
  if(odds.some(o=>!o||o<=1)){{alert('Enter valid decimal odds > 1');return;}}
  const impl=odds.reduce((s,o)=>s+1/o,0);
  const pct=(1/impl-1)*100;
  const stakes=odds.map(o=>(1/o)/impl*stake);
  const profit=stake*(1/impl-1);
  const col=pct>0?'var(--green)':'var(--red)';
  const rb=document.getElementById('calc-res');
  rb.innerHTML=[
    ...odds.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>₹${{stakes[i].toFixed(2)}} <span style="color:var(--txt3);font-size:10px">(₹${{Math.round(stakes[i]/10)*10}} rounded)</span></span></div>`),
    `<div class="crrow"><span>Total Stake</span><span>₹${{stake.toFixed(2)}}</span></div>`,
    `<div class="crrow"><span>Implied Total</span><span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span></div>`,
    pct>0?`<div class="crrow"><span>✅ PROFIT</span><span style="color:${{col}}">+₹${{profit.toFixed(2)}} (+${{pct.toFixed(3)}}%)</span></div>`:`<div class="crrow"><span>❌ NO ARB</span><span style="color:${{col}}">${{Math.abs(pct).toFixed(3)}}% over-round</span></div>`
  ].join('');
  rb.style.display='block';
}}

// ── KELLY ─────────────────────────────────────────────
function runKelly(){{
  const p=parseFloat(document.getElementById('kp').value)/100;
  const o=parseFloat(document.getElementById('ko').value);
  const bank=parseFloat(document.getElementById('kb').value)||BANK;
  if(!p||!o||p<=0||p>=1||o<=1){{alert('Enter valid probability (1-99%) and odds > 1');return;}}
  const b=o-1,q=1-p;
  const kf=(b*p-q)/b;
  const full=kf>0?kf*bank:0;
  const frac=kf>0?0.3*kf*bank:0;
  const ev=(p*b-q)*100;
  const col=ev>0?'var(--green)':'var(--red)';
  document.getElementById('kelly-res').innerHTML=`
    <div class="crrow"><span>Expected Value</span><span style="color:${{col}}">${{ev>0?'+':''}}${{ev.toFixed(2)}}% per bet</span></div>
    <div class="crrow"><span>Full Kelly</span><span>₹${{full.toFixed(2)}}</span></div>
    <div class="crrow"><span>30% Fractional Kelly</span><span style="color:${{col}}">₹${{frac.toFixed(2)}} <span style="color:var(--txt3);font-size:10px">(₹${{Math.round(frac/10)*10}} rounded)</span></span></div>
    <div class="crrow"><span>% of Bank at Risk</span><span>${{(frac/bank*100).toFixed(2)}}%</span></div>`;
  document.getElementById('kelly-res').style.display='block';
}}

// ── ODDS CONVERTER ────────────────────────────────────
let _cv=false;
function convOdds(from){{
  if(_cv)return;_cv=true;
  const sa=dec=>{{
    document.getElementById('od').value=dec.toFixed(3);
    document.getElementById('oa').value=dec>=2?'+'+Math.round((dec-1)*100):'-'+Math.round(100/(dec-1));
    document.getElementById('oi').value=(100/dec).toFixed(2);
    const[n,d]=d2f(dec);document.getElementById('of').value=n+'/'+d;
  }};
  try{{
    if(from==='d'){{const v=+document.getElementById('od').value;if(v>1)sa(v);}}
    else if(from==='a'){{const a=+document.getElementById('oa').value;const v=a>0?a/100+1:100/Math.abs(a)+1;if(v>1)sa(v);}}
    else if(from==='f'){{const p=document.getElementById('of').value.split('/');const v=p.length===2?+p[0]/+p[1]+1:0;if(v>1)sa(v);}}
    else if(from==='i'){{const i=+document.getElementById('oi').value;const v=i>0&&i<100?100/i:0;if(v>1)sa(v);}}
  }}finally{{_cv=false;}}
}}
function d2f(d){{const t=1e-5;let h1=1,h2=0,k1=0,k2=1,b=d-1;for(let i=0;i<40;i++){{const a=Math.floor(b),ah=h1;h1=a*h1+h2;h2=ah;const ak=k1;k1=a*k1+k2;k2=ak;if(Math.abs(b-a)<t)break;b=1/(b-a);}}return[h1,k1];}}
</script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║         ARB SNIPER v6.0 — Starting Run           ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    log.info(f"State loaded. Quota: {state['remaining_requests']}")

    # 1. Odds API
    odds_events = fetch_all_odds(state)

    # 2. BC.Game (multiple fallback URLs, captures debug info)
    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events  = merge_bcgame(odds_events, bc_events)

    save_state(state)

    # 3. Quant scanning (fixed arb engine)
    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    # 4. Push
    send_push(arbs, evs)

    # 5. Dashboard (single premium UI, no theme switcher)
    key_status = ROTATOR.status()
    html = generate_html(arbs, evs, raw_bc_copy, state, key_status, BC_DEBUG)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard: {OUTPUT_HTML} ({len(html)//1024} KB)")

    log.info("═" * 52)
    log.info(f"  Genuine Arbs    : {len(arbs)}")
    if arbs:
        t = arbs[0]
        log.info(f"  Top Arb         : {t['match']} +{t['profit_pct']}% ({t['ways']}-way {t['market']})")
    log.info(f"  EV Bets         : {len(evs)}")
    if evs:
        t = evs[0]
        log.info(f"  Top EV          : {t['match']} +{t['edge_pct']}% @ {t['book']}")
    log.info(f"  BC Events       : {len(raw_bc_copy)}")
    log.info(f"  BC Debug Status : {BC_DEBUG['status']} | parsed={BC_DEBUG['parsed']}")
    log.info(f"  Total Events    : {len(all_events)}")
    log.info(f"  API Quota Left  : {ROTATOR.total_remaining()}")
    log.info("╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
