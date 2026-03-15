#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              ARB SNIPER v5.0 — ELITE QUANT ENGINE + 8-THEME UI              ║
║   19-Key Rotation | BC.Game | Full Arb/EV Engine | Multi-Theme Dashboard    ║
╚══════════════════════════════════════════════════════════════════════════════╝

FEATURES:
  • Thread-safe 19-key API rotation (picks highest quota key per request)
  • BC.Game deep recursive JSON parser (handles all nested response formats)
  • Arbitrage scanner using itertools.combinations (catches ALL 2/3-way arbs)
  • Pinnacle vig-removal → True Odds → EV edge detection
  • 30% Fractional Kelly staking
  • Push notifications via ntfy.sh
  • 8-theme dashboard: Glassmorphism | Skeuomorphism | Neo-Brutalism |
    Claymorphism | Minimalism | Liquid Glass | Dark Terminal | Aurora Neon
  • Per-theme full page transformation: colors, fonts, borders, shadows, animations
  • Lockscreen with SHA-256 + localStorage session
  • Filter bars, search, tab routing, quick-calc modal, odds converter
  • 19-key API telemetry with quota bars
  • Stealth rounded stakes to avoid bookie pattern detection
"""

import os
import json
import math
import time
import hashlib
import requests
import logging
import itertools
import threading
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
BCGAME_URL     = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"

DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "arb2026")
KELLY_FRACTION = 0.30
MIN_ARB_PROFIT = 0.001   # 0.1%
MIN_EV_EDGE    = 0.005   # 0.5%
BANK_SIZE      = 10000   # default bank in Rs

ALLOWED_BOOKS = {
    "pinnacle", "onexbet", "bet365", "unibet", "betway", "stake",
    "marathonbet", "parimatch", "betfair", "dafabet", "bovada",
    "draftkings", "fanduel", "betmgm"
}

SPORTS_LIST = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_turkey_super_league", "basketball_nba", "basketball_euroleague",
    "icehockey_nhl", "tennis_atp_french_open", "tennis_wta_french_open",
    "mma_mixed_martial_arts", "americanfootball_nfl", "cricket_ipl",
    "cricket_international_championship", "boxing_boxing", "baseball_mlb",
    "aussierules_afl", "rugby_union_world_cup", "golf_masters_tournament_winner"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"


# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# Thread-safe round-robin: always picks the key with most remaining quota.
# Marks keys exhausted on 429/401. Shows full status table in dashboard.
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self.keys:
            log.warning("ODDS_API_KEYS env var is empty or not set!")
        self._lock  = threading.Lock()
        # Start all keys at 500 (the free tier daily limit)
        self._quota = {k: 500 for k in self.keys}
        log.info(f"KeyRotator: {len(self.keys)} keys loaded.")

    def get(self) -> str:
        """Return the key with the highest remaining quota."""
        with self._lock:
            if not self.keys:
                return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int):
        with self._lock:
            self._quota[key] = max(0, remaining)

    def mark_exhausted(self, key: str):
        with self._lock:
            self._quota[key] = 0
            log.warning(f"Key ...{key[-6:]} exhausted / invalid — marked 0.")

    def total_remaining(self) -> int:
        with self._lock:
            return max(0, sum(self._quota.values()))

    def status(self) -> list:
        with self._lock:
            return [
                {
                    "key": f"{k[:4]}...{k[-4:]}",
                    "remaining": self._quota.get(k, 0)
                }
                for k in self.keys
            ]


ROTATOR = KeyRotator()


# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# Persists API quota, run statistics, and arb/EV counts across runs.
# ═════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {
        "remaining_requests": 500,
        "used_today": 0,
        "last_reset": str(datetime.now(timezone.utc).date()),
        "total_events_scanned": 0,
        "last_arb_count": 0,
        "last_ev_count": 0
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
# ODDS API — CONCURRENT FETCHER WITH AUTO KEY ROTATION
# Uses ThreadPoolExecutor(max_workers=12) for blazing-fast parallel fetches.
# Each call independently picks the best key via ROTATOR.get().
# ═════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    """Fetch one sport/market combo. Rotates keys automatically."""
    key = ROTATOR.get()
    if key == "MISSING_KEY":
        return []
    if ROTATOR._quota.get(key, 0) <= 3:
        log.debug(f"Skipping {sport}/{market} — all keys near quota limit.")
        return []

    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey":      key,
        "regions":     REGIONS,
        "markets":     market,
        "oddsFormat":  "decimal",
        "dateFormat":  "iso"
    }
    try:
        r = requests.get(url, params=params, timeout=15)

        # Always update quota from headers
        remaining = int(r.headers.get("X-Requests-Remaining",
                                      ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code == 422:
            return []   # sport not currently available — not an error
        if r.status_code in (429, 401):
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200:
            log.warning(f"HTTP {r.status_code} — {sport}/{market}")
            return []

        data = r.json()
        if not isinstance(data, list):
            return []

        # Filter bookmakers locally (never pass to API — causes empty responses)
        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", [])
                   if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)

        log.info(f"  {sport}/{market}: {len(filtered)} events | "
                 f"key ...{key[-6:]} → {remaining} left")
        return filtered

    except requests.exceptions.Timeout:
        log.warning(f"Timeout: {sport}/{market}")
        return []
    except Exception as e:
        log.error(f"Error {sport}/{market}: {e}")
        return []


def fetch_all_odds(state: dict) -> list:
    if not ROTATOR.keys:
        log.error("No API keys. Set ODDS_API_KEYS secret in GitHub.")
        return []
    if ROTATOR.total_remaining() <= 0:
        log.error("All API keys exhausted for today.")
        return []

    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    log.info(f"Launching {len(tasks)} concurrent fetches "
             f"across {len(ROTATOR.keys)} keys...")

    all_events = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_sport_odds, s, m): (s, m)
                   for s, m in tasks}
        for fut in as_completed(futures):
            try:
                all_events.extend(fut.result())
            except Exception as e:
                log.error(f"Future error: {e}")

    state["remaining_requests"]   = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    log.info(f"Total Odds API events collected: {len(all_events)}")
    return all_events


# ═════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER — DEEP RECURSIVE JSON PARSER
# The BC.Game API can nest data at arbitrary depth and uses many key variants.
# _deep_find_list() recursively hunts for any list under known key names.
# _parse_bc_event() tries every known field name for teams, prices, sports.
# ═════════════════════════════════════════════════════════════════════════════
def _deep_find_list(obj, keys: list):
    """Recursively search obj for any key in `keys` whose value is a non-empty list."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v
        for v in obj.values():
            found = _deep_find_list(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_list(item, keys)
            if found is not None:
                return found
    return None


def _extract_bc_events(raw) -> list:
    """Try every known BC.Game response structure to extract event list."""
    # Direct list at root
    if isinstance(raw, list) and raw:
        return raw

    if isinstance(raw, dict):
        # Flat top-level keys
        for k in ["data", "events", "list", "items", "result",
                  "content", "rows", "matches", "games"]:
            v = raw.get(k)
            if isinstance(v, list) and v:
                return v
            # One level deeper
            if isinstance(v, dict):
                for k2 in ["events", "list", "items", "matches",
                           "games", "data"]:
                    v2 = v.get(k2)
                    if isinstance(v2, list) and v2:
                        return v2
        # Deep recursive fallback
        found = _deep_find_list(raw, ["events", "matches", "games",
                                      "list", "items"])
        if found:
            return found
    return []


def _parse_bc_event(ev: dict):
    """Parse a single BC.Game event dict into standard format."""
    home = (ev.get("homeTeam") or ev.get("home") or ev.get("team1") or
            ev.get("homeName") or ev.get("home_team") or
            ev.get("teamHome") or "")
    away = (ev.get("awayTeam") or ev.get("away") or ev.get("team2") or
            ev.get("awayName") or ev.get("away_team") or
            ev.get("teamAway") or "")
    sport = (ev.get("sportName") or ev.get("sport") or
             ev.get("sportTitle") or ev.get("category") or "Unknown")
    start = (ev.get("startTime") or ev.get("startAt") or
             ev.get("time") or ev.get("kickOff") or "")

    # Extract outcomes from every known nesting pattern
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
                name = (o.get("name") or o.get("label") or
                        o.get("selectionName") or "")
                price = None
                for pk in ["price", "odds", "odd", "value",
                           "coefficient", "rate", "oddsValue"]:
                    if pk in o:
                        try:
                            price = float(o[pk])
                            break
                        except (ValueError, TypeError):
                            pass
                if name and price and price > 1.01:
                    outcomes.append({"name": name, "price": price})
    elif isinstance(raw_mkts, dict):
        # Flat dict: {"home": 1.85, "draw": 3.2, "away": 2.1}
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
        "id":           f"bcgame_{abs(hash(home + away + str(start)))}",
        "sport_title":  sport,
        "home_team":    home,
        "away_team":    away,
        "commence_time": str(start),
        "bookmakers": [{
            "key":         "bcgame",
            "title":       "BC.Game",
            "last_update": datetime.now(timezone.utc).isoformat(),
            "markets": [{"key": "h2h", "outcomes": outcomes}]
        }]
    }


def fetch_bcgame_events() -> list:
    headers = {
        "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          "https://bc.game",
        "Referer":         "https://bc.game/",
        "Cache-Control":   "no-cache"
    }
    try:
        r = requests.get(BCGAME_URL, headers=headers, timeout=25)
        log.info(f"BC.Game status={r.status_code} "
                 f"size={len(r.content)}B "
                 f"ct={r.headers.get('Content-Type','?')}")

        if r.status_code != 200:
            log.warning(f"BC.Game returned {r.status_code}")
            return []

        try:
            raw = r.json()
        except Exception as e:
            log.error(f"BC.Game JSON parse error: {e} | "
                      f"preview: {r.text[:300]}")
            return []

        log.info(f"BC.Game root type={type(raw).__name__} "
                 f"keys={list(raw.keys()) if isinstance(raw, dict) else len(raw)}")

        raw_evs = _extract_bc_events(raw)
        log.info(f"BC.Game: extracted {len(raw_evs)} raw event objects")

        converted = []
        for ev in raw_evs[:400]:
            parsed = _parse_bc_event(ev)
            if parsed:
                converted.append(parsed)

        log.info(f"BC.Game: {len(converted)}/{len(raw_evs)} events parsed.")
        return converted

    except requests.exceptions.Timeout:
        log.error("BC.Game request timed out (25s)")
        return []
    except Exception as e:
        log.error(f"BC.Game scraper error: {e}")
        return []


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(
        None, a.lower().strip(), b.lower().strip()
    ).ratio()


def merge_bcgame(odds_events: list, bc_events: list) -> list:
    """
    Fuzzy-merge BC.Game events into the main Odds API list.
    Events with > 72% team-name similarity are merged (BC.Game as extra bookmaker).
    Unmatched events are appended as standalone events.
    """
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
    Multiplicative vig removal (Pinnacle method).
    Returns {outcome_name: true_probability} dict.
    Steps:
      1. Convert each decimal odds to raw implied probability (1/odds)
      2. Sum all raw probabilities (total > 1.0 due to vig)
      3. Divide each by total → fair probabilities that sum to 1.0
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
    """
    Fractional Kelly Criterion stake.
    Formula: f* = (b*p - q) / b  where b = odds-1, p = win prob, q = 1-p
    Returns fractional Kelly * KELLY_FRACTION * bank_size
    """
    b = odds - 1.0
    if b <= 0:
        return 0.0
    p = 1.0 / (odds / (1.0 + edge))
    kf = (b * p - (1.0 - p)) / b
    if kf <= 0:
        return 0.0
    return round(KELLY_FRACTION * kf * bank, 2)


def round10(x: float) -> float:
    """Round to nearest Rs 10 for stealth staking."""
    return round(round(x / 10) * 10, 2)


def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    """Calculate individual stakes for a multi-way arbitrage bet."""
    impl = sum(1.0 / o for o in odds_list)
    if impl >= 1.0:
        return [0.0] * len(odds_list)
    return [(1.0 / o) / impl * total for o in odds_list]


# ═════════════════════════════════════════════════════════════════════════════
# ARBITRAGE SCANNER
# Uses itertools.combinations to check ALL possible 2-way and 3-way pairings
# across bookmakers — no longer restricted to exact outcome count == 2 or 3.
# Handles spread/handicap markets by normalising point values with abs().
# ═════════════════════════════════════════════════════════════════════════════
def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        home  = ev.get("home_team", "?")
        away  = ev.get("away_team", "?")
        sport = ev.get("sport_title", "Unknown")
        com   = ev.get("commence_time", "")

        for mkey in MARKETS:
            # Build best-price map: {outcome_name: (price, book_title, book_key)}
            best: dict = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mkey:
                        continue
                    for o in mkt.get("outcomes", []):
                        name = str(o.get("name", ""))
                        pt   = o.get("point")
                        if pt is not None:
                            try:
                                name = f"{name}_{abs(float(pt))}"
                            except (ValueError, TypeError):
                                pass
                        try:
                            price = float(o.get("price", 0))
                        except (ValueError, TypeError):
                            continue
                        if price <= 1.0:
                            continue
                        if name not in best or price > best[name][0]:
                            best[name] = (
                                price,
                                bm.get("title", "?"),
                                bm.get("key", "?")
                            )

            ol = list(best.items())
            if len(ol) < 2:
                continue

            # Check all 2-way combos
            for combo in itertools.combinations(ol, 2):
                prices = [x[1][0] for x in combo]
                impl   = sum(1.0 / p for p in prices)
                if impl < 1.0:
                    pct = (1.0 / impl - 1.0) * 100
                    if pct >= MIN_ARB_PROFIT * 100:
                        stakes = calc_stakes(prices)
                        arbs.append({
                            "ways":       2,
                            "market":     mkey.upper(),
                            "sport":      sport,
                            "match":      f"{home} vs {away}",
                            "commence":   com,
                            "profit_pct": round(pct, 3),
                            "profit_amt": round((1.0 / impl - 1.0) * 1000, 2),
                            "outcomes": [{
                                "name":          x[0],
                                "odds":          round(x[1][0], 3),
                                "book":          x[1][1],
                                "book_key":      x[1][2],
                                "stake":         round(s, 2),
                                "stake_rounded": round10(s)
                            } for x, s in zip(combo, stakes)]
                        })

            # Check all 3-way combos
            for combo in itertools.combinations(ol, 3):
                prices = [x[1][0] for x in combo]
                impl   = sum(1.0 / p for p in prices)
                if impl < 1.0:
                    pct = (1.0 / impl - 1.0) * 100
                    if pct >= MIN_ARB_PROFIT * 100:
                        stakes = calc_stakes(prices)
                        arbs.append({
                            "ways":       3,
                            "market":     mkey.upper(),
                            "sport":      sport,
                            "match":      f"{home} vs {away}",
                            "commence":   com,
                            "profit_pct": round(pct, 3),
                            "profit_amt": round((1.0 / impl - 1.0) * 1000, 2),
                            "outcomes": [{
                                "name":          x[0],
                                "odds":          round(x[1][0], 3),
                                "book":          x[1][1],
                                "book_key":      x[1][2],
                                "stake":         round(s, 2),
                                "stake_rounded": round10(s)
                            } for x, s in zip(combo, stakes)]
                        })

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    log.info(f"Arbitrage scan: {len(arbs)} opportunities found.")
    return arbs[:200]  # cap to keep HTML size manageable


# ═════════════════════════════════════════════════════════════════════════════
# EV SCANNER
# Compares every soft bookmaker's odds against Pinnacle's de-vigged true odds.
# Flags any outcome where (soft_odds - true_odds) / true_odds >= MIN_EV_EDGE.
# ═════════════════════════════════════════════════════════════════════════════
def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        home  = ev.get("home_team", "?")
        away  = ev.get("away_team", "?")
        sport = ev.get("sport_title", "Unknown")
        com   = ev.get("commence_time", "")

        for mkey in MARKETS:
            # Find Pinnacle lines for this market
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

            # Compare every soft book against Pinnacle true odds
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
                        to   = 1.0 / tp          # true odds (no vig)
                        edge = (price - to) / to  # percentage edge

                        if edge >= MIN_EV_EDGE:
                            ks = kelly_stake(edge, price, BANK_SIZE)
                            bets.append({
                                "market":               mkey.upper(),
                                "sport":                sport,
                                "match":                f"{home} vs {away}",
                                "commence":             com,
                                "outcome":              name,
                                "book":                 bm.get("title", "?"),
                                "book_key":             bm.get("key", "?"),
                                "offered_odds":         round(price, 3),
                                "true_odds":            round(to, 3),
                                "true_prob_pct":        round(tp * 100, 2),
                                "edge_pct":             round(edge * 100, 3),
                                "kelly_stake":          ks,
                                "kelly_stake_rounded":  round10(ks)
                            })

    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"EV scan: {len(bets)} value bets found.")
    return bets[:300]


# ═════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION (ntfy.sh)
# ASCII-safe headers, UTF-8 body. Tags: zap,moneybag for emoji on phone.
# ═════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs:
        log.info("No opportunities. Skipping push notification.")
        return

    if arbs:
        t   = arbs[0]
        msg = (f"TOP ARB: {t['match']} | +{t['profit_pct']}% | "
               f"{t['ways']}-way {t['market']} | {len(evs)} EV bets")
    else:
        t   = evs[0]
        msg = (f"TOP EV: {t['match']} | +{t['edge_pct']}% edge | "
               f"{t['book']} | {len(evs)} total EV bets")

    try:
        r = requests.post(
            NTFY_URL,
            data=msg.encode("utf-8"),
            headers={
                "Title":        "Arb Sniper Alert",
                "Priority":     "high",
                "Tags":         "zap,moneybag",
                "Content-Type": "text/plain; charset=utf-8"
            },
            timeout=10
        )
        log.info(f"Push notification: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Push notification failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR
# Generates a complete single-file static HTML dashboard with:
#   • SHA-256 lockscreen + localStorage session persistence
#   • 8 switchable themes (Glassmorphism, Skeuomorphism, Neo-Brutalism,
#     Claymorphism, Minimalism, Liquid Glass, Dark Terminal, Aurora Neon)
#   • Per-theme: unique fonts, colors, borders, shadows, card shapes, animations
#   • 5 tabs: Arbitrage | +EV Bets | BC.Game | Calculator | API Keys
#   • Live filter bars + search on every tab
#   • Quick-calc modal (pre-filled from card "Calc" button)
#   • Full Arb Calculator (2/3-way) + Kelly Calculator + Odds Converter
#   • 19-key API telemetry table with colour-coded quota bars
#   • Stealth stakes: show rounded Rs10 value prominently, exact in small grey
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list,
                  state: dict, key_status: list) -> str:

    IST      = timezone(timedelta(hours=5, minutes=30))
    ist_now  = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    pass_hash = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()

    total_quota = sum(k["remaining"] for k in key_status)
    total_keys  = len(key_status)

    arbs_j = json.dumps(arbs, ensure_ascii=False)
    evs_j  = json.dumps(evs,  ensure_ascii=False)
    bc_j   = json.dumps(raw_bc, ensure_ascii=False)
    keys_j = json.dumps(key_status, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v5.0 ⚡</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@600;700;800&family=Unbounded:wght@400;700;900&family=Playfair+Display:wght@400;600;700&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,300&family=Space+Grotesk:wght@300;400;500;700&family=IBM+Plex+Mono:wght@400;500;700&family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,600;1,9..144,300&display=swap" rel="stylesheet"/>
<style>
/* ══════════════════════════════════════════════════════
   GLOBAL RESET & BASE
══════════════════════════════════════════════════════ */
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
html,body{{width:100%;height:100%;overflow:hidden}}
body{{transition:background 0.7s ease}}
::-webkit-scrollbar{{width:3px;height:3px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:rgba(128,128,128,0.25);border-radius:2px}}

/* ══════════════════════════════════════════════════════
   THEME SWITCHER BAR (always on top)
══════════════════════════════════════════════════════ */
#theme-bar{{
  position:fixed;top:0;left:0;right:0;z-index:9998;
  display:flex;align-items:center;gap:5px;
  padding:8px 16px;
  background:rgba(0,0,0,0.5);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid rgba(255,255,255,0.1);
  overflow-x:auto;scrollbar-width:none;
}}
#theme-bar::-webkit-scrollbar{{display:none}}
.tb-logo{{
  font-size:11px;font-weight:900;letter-spacing:3px;
  color:rgba(255,255,255,0.5);white-space:nowrap;margin-right:6px;
  font-family:'IBM Plex Mono',monospace;
}}
.tp{{
  flex-shrink:0;padding:5px 11px;border-radius:20px;
  font-size:10px;font-weight:700;letter-spacing:0.8px;cursor:pointer;
  border:1px solid rgba(255,255,255,0.18);
  background:rgba(255,255,255,0.07);color:rgba(255,255,255,0.6);
  transition:all 0.2s;white-space:nowrap;
  font-family:'IBM Plex Mono',monospace;text-transform:uppercase;
}}
.tp:hover{{background:rgba(255,255,255,0.15);color:#fff;transform:translateY(-1px)}}
.tp.active{{background:rgba(255,255,255,0.92);color:#111;border-color:transparent;box-shadow:0 2px 12px rgba(255,255,255,0.3)}}

/* ══════════════════════════════════════════════════════
   LOCK SCREEN
══════════════════════════════════════════════════════ */
#lock{{
  position:fixed;inset:0;z-index:9999;
  display:flex;align-items:center;justify-content:center;
  transition:opacity 0.5s ease;
}}
.lbox{{
  display:flex;flex-direction:column;align-items:center;gap:16px;
  width:90%;max-width:320px;
}}
#linput{{
  font-size:20px;padding:14px;width:100%;
  text-align:center;letter-spacing:8px;
  outline:none;transition:all 0.2s;
  font-family:'IBM Plex Mono',monospace;
}}
#lbtn{{
  width:100%;padding:13px;font-size:12px;font-weight:800;
  letter-spacing:2px;cursor:pointer;border:none;
  transition:all 0.2s;font-family:'Syne',sans-serif;
}}
#lbtn:hover{{opacity:0.85;transform:translateY(-1px)}}
#lerr{{font-size:11px;display:none;letter-spacing:1px}}
.lock-icon{{font-size:40px;animation:lockglow 2s ease-in-out infinite alternate}}
@keyframes lockglow{{from{{opacity:0.6}}to{{opacity:1}}}}
.lock-title{{font-size:22px;font-weight:900;letter-spacing:4px}}
.lock-sub{{font-size:9px;letter-spacing:3px;opacity:0.5;text-transform:uppercase}}

/* ══════════════════════════════════════════════════════
   APP SHELL
══════════════════════════════════════════════════════ */
#app{{
  display:none;
  width:100%;height:100vh;
  padding-top:48px;
  overflow-y:auto;
  position:relative;
  transition:all 0.8s cubic-bezier(0.16,1,0.3,1);
}}
#app::before{{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  transition:all 0.8s ease;
}}

/* ══════════════════════════════════════════════════════
   SHARED STRUCTURAL LAYOUT
══════════════════════════════════════════════════════ */
.topbar{{
  position:sticky;top:48px;z-index:100;
  padding:0 18px;height:50px;
  display:flex;align-items:center;justify-content:space-between;
  transition:all 0.6s ease;
}}
.logo-wrap{{display:flex;align-items:center;gap:8px}}
.logo-txt{{font-size:15px;font-weight:800;letter-spacing:1px;transition:all 0.6s}}
.topbar-r{{display:flex;align-items:center;gap:10px}}
.live-pill{{
  display:flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:20px;
  transition:all 0.4s;
}}
.live-dot{{width:6px;height:6px;border-radius:50%;animation:blink 1.4s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:0.15}}}}
.live-txt{{font-size:10px;font-weight:800;letter-spacing:1.5px}}
.time-txt{{font-size:10px;transition:color 0.4s}}
.logout-btn{{
  background:none;border:none;cursor:pointer;
  font-size:14px;transition:all 0.2s;padding:4px;
}}
.logout-btn:hover{{transform:scale(1.15)}}

/* STATS BAR */
.statsbar{{
  padding:8px 18px;display:flex;gap:7px;
  overflow-x:auto;scrollbar-width:none;
  transition:all 0.5s ease;
}}
.statsbar::-webkit-scrollbar{{display:none}}
.ssc{{
  min-width:88px;padding:8px 12px;
  transition:all 0.4s cubic-bezier(0.16,1,0.3,1);
  cursor:default;flex-shrink:0;
}}
.ssc:hover{{transform:translateY(-2px)}}
.ss-l{{
  font-size:8px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;margin-bottom:4px;
  transition:color 0.4s;
}}
.ss-v{{font-size:16px;font-weight:800;transition:all 0.4s}}

/* TABS */
.tabbar{{
  display:flex;gap:3px;padding:0 18px;
  overflow-x:auto;scrollbar-width:none;
  transition:all 0.5s ease;
}}
.tabbar::-webkit-scrollbar{{display:none}}
.tabbtn{{
  padding:10px 13px;font-size:11px;font-weight:700;cursor:pointer;
  white-space:nowrap;background:none;
  display:flex;align-items:center;gap:6px;
  transition:all 0.25s;border:1px solid transparent;
}}
.tbadge{{
  font-size:9px;padding:1px 5px;border-radius:8px;
  font-weight:900;transition:all 0.3s;
}}

/* TAB CONTENT */
.tc{{display:none;padding:14px 18px 48px;position:relative;z-index:1}}
.tc.act{{display:block}}

/* FILTER BAR */
.fbar{{display:flex;gap:7px;margin-bottom:13px;flex-wrap:wrap;align-items:center}}
.finput{{
  flex:1;min-width:130px;padding:8px 12px;
  font-family:inherit;font-size:11px;outline:none;
  transition:all 0.2s;
}}
.fselect{{
  padding:8px 10px;font-family:inherit;font-size:11px;
  outline:none;cursor:pointer;transition:all 0.2s;
  min-width:110px;
}}
.frange-wrap{{
  display:flex;align-items:center;gap:7px;padding:7px 11px;
  font-size:10px;white-space:nowrap;transition:all 0.3s;
}}
input[type=range]{{
  width:80px;cursor:pointer;accent-color:currentColor;
}}

/* CARD GRID */
.grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:13px;
}}
.card{{
  overflow:hidden;position:relative;
  transition:transform 0.3s,box-shadow 0.3s,border-color 0.3s;
}}

/* CARD INTERNALS */
.cstripe{{width:100%;height:3px;transition:background 0.4s}}
.cinner{{padding:14px 14px 0}}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}}
.ctypebadge{{
  font-size:9px;font-weight:900;letter-spacing:1.5px;
  text-transform:uppercase;padding:3px 7px;
  transition:all 0.4s;
}}
.cprofit{{font-size:19px;font-weight:900;transition:all 0.4s}}
.cmatch{{font-size:13px;font-weight:600;margin-bottom:6px;line-height:1.3;transition:color 0.4s}}
.cmeta{{
  font-size:10px;margin-bottom:10px;
  display:flex;flex-wrap:wrap;gap:7px;
  transition:color 0.4s;
}}
.ctable{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:10px}}
.ctable td{{padding:6px 6px;border-bottom:1px solid rgba(128,128,128,0.1)}}
.ctable tr:last-child td{{border:none}}
.btag{{
  padding:2px 5px;font-size:9px;font-weight:800;
  transition:all 0.3s;
}}
.oval{{font-weight:800;font-size:12px;transition:color 0.4s}}
.stake-x{{font-size:9px;opacity:0.45;display:block}}
.stake-m{{font-size:13px;font-weight:800;transition:color 0.4s}}
.cfoot{{
  display:flex;justify-content:space-between;align-items:center;
  padding:8px 14px 10px;
  border-top:1px solid rgba(128,128,128,0.1);
  margin-top:4px;
}}
.cfoot-l{{font-size:10px;opacity:0.5;transition:color 0.4s}}
.calcbtn{{
  background:none;font-family:inherit;font-size:10px;cursor:pointer;
  padding:4px 9px;transition:all 0.2s;font-weight:700;
}}
.empty-state{{
  text-align:center;padding:60px 20px;
  opacity:0.35;grid-column:1/-1;font-size:13px;
}}
.empty-state i{{font-size:32px;display:block;margin-bottom:12px}}

/* CALCULATOR SECTION */
.csec{{margin-bottom:14px;overflow:hidden;transition:all 0.4s}}
.csec-title{{
  font-size:14px;font-weight:700;margin-bottom:14px;
  display:flex;align-items:center;gap:8px;
  transition:color 0.4s;
}}
.ctabs{{display:flex;gap:5px;margin-bottom:13px}}
.ctab{{
  padding:7px 15px;font-size:11px;font-weight:700;cursor:pointer;
  transition:all 0.2s;background:none;font-family:inherit;
}}
.cresult{{margin-top:13px;transition:all 0.4s}}
.crrow{{
  display:flex;justify-content:space-between;
  padding:6px 0;font-size:12px;
  border-bottom:1px solid rgba(128,128,128,0.1);
}}
.crrow:last-child{{border:none;font-weight:800;font-size:13px}}

/* API KEYS TABLE */
.atable{{width:100%;border-collapse:collapse;font-size:11px}}
.atable th{{
  padding:9px 12px;font-size:9px;letter-spacing:2px;
  text-transform:uppercase;font-weight:700;text-align:left;
  transition:all 0.4s;
}}
.atable td{{padding:9px 12px;transition:all 0.4s}}
.qbar-bg{{height:4px;border-radius:2px;overflow:hidden;margin-top:4px}}
.qbar-fill{{height:100%;border-radius:2px;transition:width 0.6s ease}}

/* MODAL */
.modal-bg{{
  position:fixed;inset:0;z-index:800;
  display:none;align-items:center;justify-content:center;
  background:rgba(0,0,0,0.75);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);
  animation:fadeIn 0.2s ease;
}}
.modal-bg.open{{display:flex}}
@keyframes fadeIn{{from{{opacity:0}}to{{opacity:1}}}}
.modal{{
  width:92%;max-width:450px;max-height:90vh;
  overflow-y:auto;transition:all 0.4s;
  animation:modalIn 0.3s cubic-bezier(0.16,1,0.3,1);
}}
@keyframes modalIn{{from{{opacity:0;transform:scale(0.92) translateY(20px)}}to{{opacity:1;transform:scale(1) translateY(0)}}}}
.modal-title{{
  font-size:15px;font-weight:800;
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:16px;transition:color 0.4s;
}}
.closex{{
  background:none;border:none;font-size:18px;cursor:pointer;
  opacity:0.5;transition:opacity 0.2s;
}}
.closex:hover{{opacity:1}}

/* CARD STAGGER ANIMATIONS (per theme) */
.card{{animation-fill-mode:both}}
.card:nth-child(1){{animation-delay:0.0s}}
.card:nth-child(2){{animation-delay:0.05s}}
.card:nth-child(3){{animation-delay:0.10s}}
.card:nth-child(4){{animation-delay:0.15s}}
.card:nth-child(5){{animation-delay:0.20s}}
.card:nth-child(6){{animation-delay:0.25s}}
.card:nth-child(n+7){{animation-delay:0.30s}}

/* ODDS GRID */
.ogrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}

@media(max-width:580px){{
  .grid{{grid-template-columns:1fr}}
  .ogrid{{grid-template-columns:1fr}}
  .topbar{{padding:0 12px}}
  .tc{{padding:12px 12px 40px}}
}}

/* ══════════════════════════════════════════════════════
   THEME 1: GLASSMORPHISM
   Frosted glass panels over animated aurora gradient blobs
══════════════════════════════════════════════════════ */
.t-glass{{
  background:#090520;
  font-family:'Space Grotesk',sans-serif;
}}
.t-glass #app::before{{
  background:
    radial-gradient(ellipse 80% 60% at 15% 25%,rgba(138,43,226,0.4) 0%,transparent 60%),
    radial-gradient(ellipse 70% 80% at 85% 65%,rgba(0,140,255,0.35) 0%,transparent 60%),
    radial-gradient(ellipse 60% 50% at 50% 80%,rgba(255,60,180,0.2) 0%,transparent 60%);
  animation:glassBg 9s ease-in-out infinite alternate;
}}
@keyframes glassBg{{
  0%{{opacity:0.8;transform:scale(1) rotate(0deg)}}
  100%{{opacity:1;transform:scale(1.06) rotate(1.5deg)}}
}}
.t-glass #lock{{background:rgba(9,5,32,0.95)}}
.t-glass .lbox{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.14);border-radius:20px;padding:32px;backdrop-filter:blur(20px)}}
.t-glass .lock-icon{{color:#c77dff}}
.t-glass .lock-title{{color:#fff;font-family:'Syne',sans-serif}}
.t-glass .lock-sub{{color:rgba(255,255,255,0.4)}}
.t-glass #linput{{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.15);color:#fff;border-radius:10px}}
.t-glass #linput:focus{{border-color:rgba(199,125,255,0.7);box-shadow:0 0 0 3px rgba(199,125,255,0.1)}}
.t-glass #lbtn{{background:linear-gradient(135deg,#8b5cf6,#22d3ee);color:#fff;border-radius:10px;letter-spacing:2px}}
.t-glass #lerr{{color:#f87171}}
.t-glass .topbar{{background:rgba(255,255,255,0.05);border-bottom:1px solid rgba(255,255,255,0.1);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}}
.t-glass .logo-txt{{background:linear-gradient(135deg,#e0aaff,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Syne',sans-serif}}
.t-glass .live-pill{{background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.25)}}
.t-glass .live-dot{{background:#4ade80;box-shadow:0 0 6px #4ade80}}
.t-glass .live-txt{{color:#4ade80}}
.t-glass .time-txt{{color:rgba(255,255,255,0.4)}}
.t-glass .logout-btn{{color:rgba(255,255,255,0.4)}}
.t-glass .logout-btn:hover{{color:#f87171}}
.t-glass .statsbar{{background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.07)}}
.t-glass .ssc{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;backdrop-filter:blur(12px)}}
.t-glass .ss-l{{color:rgba(255,255,255,0.35)}}
.t-glass .tabbar{{background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.07)}}
.t-glass .tabbtn{{color:rgba(255,255,255,0.38);border-radius:10px;font-family:'Space Grotesk',sans-serif}}
.t-glass .tabbtn:hover{{background:rgba(255,255,255,0.07);color:rgba(255,255,255,0.8)}}
.t-glass .tabbtn.act{{background:rgba(138,43,226,0.18);border-color:rgba(138,43,226,0.45);color:#d4b0ff;box-shadow:0 0 20px rgba(138,43,226,0.2)}}
.t-glass .card{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.13);border-radius:18px;backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);box-shadow:0 8px 32px rgba(0,0,0,0.4),inset 0 1px 0 rgba(255,255,255,0.18);animation:glassCardIn 0.6s cubic-bezier(0.16,1,0.3,1) both}}
@keyframes glassCardIn{{from{{opacity:0;transform:translateY(24px) scale(0.95);filter:blur(4px)}}to{{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}}}
.t-glass .card:hover{{transform:translateY(-5px);border-color:rgba(138,43,226,0.4);box-shadow:0 16px 48px rgba(0,0,0,0.55),inset 0 1px 0 rgba(255,255,255,0.25),0 0 0 1px rgba(138,43,226,0.2)}}
.t-glass .cstripe{{background:linear-gradient(90deg,rgba(138,43,226,0.9),rgba(34,211,238,0.9),rgba(255,100,200,0.7))}}
.t-glass .ctypebadge{{background:rgba(138,43,226,0.18);color:#d4b0ff;border:1px solid rgba(138,43,226,0.3);border-radius:5px}}
.t-glass .cprofit{{background:linear-gradient(135deg,#4ade80,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-glass .cmatch{{color:rgba(255,255,255,0.9)}}
.t-glass .cmeta{{color:rgba(255,255,255,0.4)}}
.t-glass .oval{{color:#fbbf24}}
.t-glass .btag{{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);border-radius:5px;color:rgba(255,255,255,0.65)}}
.t-glass .stake-m{{color:rgba(255,255,255,0.9)}}
.t-glass .calcbtn{{border:1px solid rgba(138,43,226,0.4);color:#c77dff;border-radius:6px}}
.t-glass .calcbtn:hover{{background:rgba(138,43,226,0.2)}}
.t-glass .csec{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:16px;backdrop-filter:blur(14px)}}
.t-glass .csec-title{{color:rgba(255,255,255,0.85)}}
.t-glass .ctab{{color:rgba(255,255,255,0.4);border:1px solid rgba(255,255,255,0.12);border-radius:8px}}
.t-glass .ctab.act{{background:rgba(138,43,226,0.2);color:#d4b0ff;border-color:rgba(138,43,226,0.5)}}
.t-glass .finput,.t-glass .fselect{{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);color:#fff;border-radius:8px}}
.t-glass .finput:focus,.t-glass .fselect:focus{{border-color:rgba(138,43,226,0.6)}}
.t-glass .frange-wrap{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:rgba(255,255,255,0.5)}}
.t-glass .cresult{{background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:14px}}
.t-glass .crrow{{color:rgba(255,255,255,0.7)}}
.t-glass .run-btn{{background:linear-gradient(135deg,#8b5cf6,#22d3ee);color:#fff;border:none;border-radius:10px;width:100%;padding:12px;font-size:12px;font-weight:800;letter-spacing:1.5px;cursor:pointer;margin-top:4px;font-family:'Syne',sans-serif;transition:opacity 0.2s}}
.t-glass .run-btn:hover{{opacity:0.85}}
.t-glass .atable th{{color:rgba(255,255,255,0.35);border-bottom:1px solid rgba(255,255,255,0.08)}}
.t-glass .atable td{{color:rgba(255,255,255,0.7);border-bottom:1px solid rgba(255,255,255,0.06)}}
.t-glass .modal{{background:rgba(20,10,50,0.9);border:1px solid rgba(255,255,255,0.14);border-radius:16px;padding:24px;backdrop-filter:blur(20px)}}
.t-glass .modal-title{{color:rgba(255,255,255,0.9)}}
.t-glass .closex{{color:rgba(255,255,255,0.5)}}
.t-glass .empty-state{{color:rgba(255,255,255,0.3)}}
.t-glass .fselect option{{background:#1a0a40;color:#fff}}

/* ══════════════════════════════════════════════════════
   THEME 2: SKEUOMORPHISM
   Rich leather textures, embossed paper, inset shadows
══════════════════════════════════════════════════════ */
.t-skeu{{
  background:#2c1f12;
  font-family:'Fraunces',serif;
}}
.t-skeu #app::before{{
  background:
    repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px),
    repeating-linear-gradient(90deg,transparent,transparent 2px,rgba(0,0,0,0.02) 2px,rgba(0,0,0,0.02) 4px);
}}
.t-skeu #lock{{background:linear-gradient(160deg,#3a2a1a,#1e1208)}}
.t-skeu .lbox{{background:linear-gradient(160deg,#f0e0c0,#dcc8a0);border:2px solid #a07840;border-radius:8px;padding:30px;box-shadow:0 2px 0 rgba(255,255,255,0.6) inset,0 -2px 0 rgba(0,0,0,0.3) inset,4px 8px 24px rgba(0,0,0,0.6)}}
.t-skeu .lock-icon{{color:#8b5e10;text-shadow:0 1px 0 rgba(255,255,255,0.5)}}
.t-skeu .lock-title{{color:#3a2a0a;font-family:'Playfair Display',serif;text-shadow:0 1px 0 rgba(255,255,255,0.5)}}
.t-skeu .lock-sub{{color:#8b6030}}
.t-skeu #linput{{background:linear-gradient(180deg,#d8c8a0,#ead8b0);border:2px solid #a08050;color:#2a1a06;border-radius:4px;box-shadow:0 2px 6px rgba(0,0,0,0.3) inset}}
.t-skeu #linput:focus{{border-color:#c89020;box-shadow:0 2px 6px rgba(0,0,0,0.3) inset,0 0 0 3px rgba(200,144,32,0.2)}}
.t-skeu #lbtn{{background:linear-gradient(180deg,#c89020,#a07010);border:2px solid #806010;color:#fff;border-radius:4px;box-shadow:0 2px 0 rgba(255,255,255,0.2) inset,0 4px 8px rgba(0,0,0,0.4);text-shadow:0 1px 2px rgba(0,0,0,0.5);letter-spacing:2px}}
.t-skeu #lerr{{color:#8b1a1a}}
.t-skeu .topbar{{background:linear-gradient(180deg,#4a3420 0%,#3a2618 60%,#2c1e12 100%);border-bottom:3px solid #1a0e06;box-shadow:0 3px 0 rgba(200,150,60,0.15),0 6px 20px rgba(0,0,0,0.7)}}
.t-skeu .logo-txt{{color:#d4a020;font-family:'Playfair Display',serif;font-style:italic;text-shadow:0 2px 4px rgba(0,0,0,0.5),0 -1px 0 rgba(255,220,100,0.2)}}
.t-skeu .live-pill{{background:rgba(30,80,30,0.6);border:1px solid #1a4a1a;box-shadow:0 1px 0 rgba(255,255,255,0.1) inset}}
.t-skeu .live-dot{{background:#2d8b30;box-shadow:0 0 4px #2d8b30}}
.t-skeu .live-txt{{color:#6dc870}}
.t-skeu .time-txt{{color:rgba(210,170,100,0.6)}}
.t-skeu .logout-btn{{color:rgba(210,170,100,0.5)}}
.t-skeu .statsbar{{background:linear-gradient(180deg,#352215,#2a1a0d);border-bottom:2px solid #1a0c06}}
.t-skeu .ssc{{background:linear-gradient(160deg,#ede0c0,#e0cca0);border:1px solid #b89060;border-radius:4px;box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 2px 6px rgba(0,0,0,0.4)}}
.t-skeu .ss-l{{color:#7a5825}}
.t-skeu .ss-v{{color:#3a2006}}
.t-skeu .tabbar{{background:linear-gradient(180deg,#3d2a1a,#2c1c0e);border-bottom:2px solid #1a0e06;box-shadow:0 2px 8px rgba(0,0,0,0.5)}}
.t-skeu .tabbtn{{color:rgba(210,175,110,0.55);border-radius:3px;font-family:'Fraunces',serif}}
.t-skeu .tabbtn:hover{{background:rgba(210,175,110,0.1);color:rgba(210,175,110,0.9)}}
.t-skeu .tabbtn.act{{background:linear-gradient(180deg,#f0e0c0,#dcc8a0);border:1px solid #a08050;color:#2a1a06;box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 2px 5px rgba(0,0,0,0.3)}}
.t-skeu .card{{background:linear-gradient(160deg,#f5e8cc,#e8d4a8);border:1px solid #b89060;border-radius:5px;box-shadow:0 1px 0 rgba(255,255,255,0.8) inset,0 -1px 0 rgba(0,0,0,0.18) inset,3px 6px 16px rgba(0,0,0,0.55),0 0 0 1px rgba(0,0,0,0.2);animation:skeuIn 0.5s ease both}}
@keyframes skeuIn{{from{{opacity:0;transform:scale(0.97)}}to{{opacity:1;transform:scale(1)}}}}
.t-skeu .card:hover{{transform:translateY(-3px);box-shadow:0 1px 0 rgba(255,255,255,0.85) inset,0 -1px 0 rgba(0,0,0,0.15) inset,5px 10px 24px rgba(0,0,0,0.65),0 0 0 1px rgba(0,0,0,0.25)}}
.t-skeu .cstripe{{background:linear-gradient(180deg,#c89020,#a07010);height:8px;border-bottom:2px solid #6a4a08;box-shadow:0 2px 4px rgba(0,0,0,0.3)}}
.t-skeu .ctypebadge{{background:linear-gradient(180deg,#2a5a30,#1a3d20);border:1px solid #0a2a10;color:#90d098;border-radius:3px;box-shadow:0 1px 0 rgba(255,255,255,0.15) inset,0 1px 4px rgba(0,0,0,0.4)}}
.t-skeu .cprofit{{color:#8b3a0a;text-shadow:0 1px 0 rgba(255,255,255,0.5)}}
.t-skeu .cmatch{{color:#2a1a06}}
.t-skeu .cmeta{{color:#7a5020}}
.t-skeu .oval{{color:#8b3a0a;text-shadow:0 1px 0 rgba(255,255,255,0.4)}}
.t-skeu .btag{{background:linear-gradient(180deg,#dcc8a0,#c8b088);border:1px solid #a08050;border-radius:3px;color:#3a2a0a;box-shadow:0 1px 0 rgba(255,255,255,0.5) inset,0 1px 3px rgba(0,0,0,0.3)}}
.t-skeu .stake-m{{color:#2a1a06}}
.t-skeu .calcbtn{{border:1px solid #a08050;color:#6a4a10;border-radius:3px;box-shadow:0 1px 0 rgba(255,255,255,0.5) inset,0 1px 3px rgba(0,0,0,0.2)}}
.t-skeu .calcbtn:hover{{background:linear-gradient(180deg,#dcc8a0,#c8b088)}}
.t-skeu .csec{{background:linear-gradient(160deg,#f0e4c4,#e4d0a4);border:1px solid #b89060;border-radius:5px;padding:16px;box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 2px 6px rgba(0,0,0,0.35)}}
.t-skeu .csec-title{{color:#2a1a06;font-family:'Playfair Display',serif}}
.t-skeu .ctab{{color:#7a5020;border:1px solid #b89060;border-radius:3px;box-shadow:0 1px 0 rgba(255,255,255,0.5) inset}}
.t-skeu .ctab.act{{background:linear-gradient(180deg,#c89020,#a07010);color:#fff;border-color:#6a4a08;box-shadow:0 1px 0 rgba(255,255,255,0.2) inset,0 2px 4px rgba(0,0,0,0.3)}}
.t-skeu .finput,.t-skeu .fselect{{background:linear-gradient(180deg,#dcd0b0,#ecdca0);border:1px solid #b09060;color:#2a1a06;border-radius:3px;box-shadow:0 2px 4px rgba(0,0,0,0.2) inset}}
.t-skeu .frange-wrap{{background:linear-gradient(180deg,#dcc8a0,#c8b488);border:1px solid #a08050;border-radius:3px;color:#5a3a14}}
.t-skeu .cresult{{background:linear-gradient(160deg,#f0e4c4,#e0d0a4);border:1px solid #b89060;border-radius:4px;padding:14px;box-shadow:0 2px 4px rgba(0,0,0,0.2) inset}}
.t-skeu .crrow{{color:#2a1a06;border-bottom-color:rgba(160,120,60,0.25)}}
.t-skeu .run-btn{{background:linear-gradient(180deg,#c89020,#a07010);border:2px solid #6a4a08;color:#fff;border-radius:4px;width:100%;padding:12px;font-size:12px;font-weight:700;letter-spacing:1.5px;cursor:pointer;margin-top:4px;font-family:'Playfair Display',serif;box-shadow:0 2px 0 rgba(255,255,255,0.2) inset,0 3px 6px rgba(0,0,0,0.4);text-shadow:0 1px 2px rgba(0,0,0,0.5);transition:all 0.2s}}
.t-skeu .atable th{{color:rgba(120,80,20,0.7);border-bottom:2px solid rgba(160,120,60,0.4)}}
.t-skeu .atable td{{color:#3a2a0a;border-bottom:1px solid rgba(160,120,60,0.2)}}
.t-skeu .modal{{background:linear-gradient(160deg,#f0e4c4,#e4d0a4);border:2px solid #b89060;border-radius:6px;padding:22px;box-shadow:0 1px 0 rgba(255,255,255,0.7) inset,0 20px 60px rgba(0,0,0,0.6)}}
.t-skeu .modal-title{{color:#2a1a06;font-family:'Playfair Display',serif}}
.t-skeu .closex{{color:#7a5020}}
.t-skeu .empty-state{{color:rgba(120,80,20,0.4)}}

/* ══════════════════════════════════════════════════════
   THEME 3: NEO-BRUTALISM
   Raw borders, stark yellows, zero radius, offset shadows
══════════════════════════════════════════════════════ */
.t-brutal{{
  background:#f2eed8;
  font-family:'Unbounded',sans-serif;
}}
.t-brutal #lock{{background:#111}}
.t-brutal .lbox{{background:#ffe500;border:4px solid #111;border-radius:0;padding:28px;box-shadow:8px 8px 0 #ff3b3b}}
.t-brutal .lock-icon{{color:#111}}
.t-brutal .lock-title{{color:#111;font-family:'Unbounded',sans-serif;font-size:18px}}
.t-brutal .lock-sub{{color:#333;font-size:9px}}
.t-brutal #linput{{background:#fff;border:3px solid #111;color:#111;border-radius:0;box-shadow:4px 4px 0 #111}}
.t-brutal #linput:focus{{border-color:#ff3b3b;box-shadow:4px 4px 0 #ff3b3b}}
.t-brutal #lbtn{{background:#111;border:3px solid #111;color:#ffe500;border-radius:0;letter-spacing:3px;box-shadow:4px 4px 0 #ff3b3b}}
.t-brutal #lerr{{color:#ff3b3b;font-weight:900}}
.t-brutal .topbar{{background:#111;border-bottom:4px solid #333}}
.t-brutal .logo-txt{{color:#ffe500;font-family:'Unbounded',sans-serif;font-size:13px}}
.t-brutal .live-pill{{background:#ffe500;border:2px solid #111;border-radius:0;box-shadow:3px 3px 0 #111}}
.t-brutal .live-dot{{background:#111}}
.t-brutal .live-txt{{color:#111}}
.t-brutal .time-txt{{color:rgba(255,229,0,0.7)}}
.t-brutal .logout-btn{{color:#ffe500}}
.t-brutal .statsbar{{background:#f5f0d5;border-bottom:4px solid #111}}
.t-brutal .ssc{{background:#ffe500;border:3px solid #111;border-radius:0;box-shadow:4px 4px 0 #111}}
.t-brutal .ss-l{{color:#333;font-size:8px}}
.t-brutal .ss-v{{color:#111;font-size:18px}}
.t-brutal .tabbar{{background:#f0ead0;border-bottom:4px solid #111}}
.t-brutal .tabbtn{{color:#666;border-radius:0;font-family:'Unbounded',sans-serif;font-size:9px;font-weight:900;border:2px solid transparent}}
.t-brutal .tabbtn:hover{{background:#ffe500;border-color:#111;color:#111;box-shadow:3px 3px 0 #111;transform:translate(-2px,-2px)}}
.t-brutal .tabbtn.act{{background:#ffe500;border:3px solid #111;color:#111;box-shadow:4px 4px 0 #111;transform:translate(-2px,-2px)}}
.t-brutal .card{{background:#fff;border:3px solid #111;border-radius:0;box-shadow:7px 7px 0 #111;animation:brutalIn 0.35s cubic-bezier(0.34,1.56,0.64,1) both}}
@keyframes brutalIn{{0%{{opacity:0;transform:translate(8px,8px)}}100%{{opacity:1;transform:translate(0,0)}}}}
.t-brutal .card:hover{{transform:translate(-4px,-4px);box-shadow:11px 11px 0 #111}}
.t-brutal .card:nth-child(3n+1) .cstripe{{background:#ff3b3b;height:8px}}
.t-brutal .card:nth-child(3n+2) .cstripe{{background:#ffe500;height:8px}}
.t-brutal .card:nth-child(3n+3) .cstripe{{background:#00d4ff;height:8px}}
.t-brutal .ctypebadge{{background:#111;color:#ffe500;border-radius:0;box-shadow:3px 3px 0 #ff3b3b}}
.t-brutal .cprofit{{color:#ff3b3b;font-size:22px}}
.t-brutal .cmatch{{color:#111;font-size:12px}}
.t-brutal .cmeta{{color:#555;font-size:9px;letter-spacing:1px}}
.t-brutal .oval{{color:#ff3b3b}}
.t-brutal .btag{{background:#111;color:#ffe500;border-radius:0;box-shadow:2px 2px 0 #ff3b3b}}
.t-brutal .stake-m{{color:#111}}
.t-brutal .calcbtn{{border:2px solid #111;color:#111;border-radius:0;font-weight:900;box-shadow:3px 3px 0 #111}}
.t-brutal .calcbtn:hover{{background:#ffe500;transform:translate(-2px,-2px);box-shadow:5px 5px 0 #111}}
.t-brutal .csec{{background:#fff;border:3px solid #111;border-radius:0;padding:16px;box-shadow:6px 6px 0 #111}}
.t-brutal .csec-title{{color:#111;font-size:12px;letter-spacing:1px}}
.t-brutal .ctab{{color:#555;border:2px solid #111;border-radius:0;font-family:'Unbounded',sans-serif;font-size:9px;font-weight:900}}
.t-brutal .ctab.act{{background:#ffe500;color:#111;box-shadow:3px 3px 0 #111;transform:translate(-2px,-2px)}}
.t-brutal .finput,.t-brutal .fselect{{background:#fff;border:2px solid #111;color:#111;border-radius:0}}
.t-brutal .finput:focus,.t-brutal .fselect:focus{{border-color:#ff3b3b;box-shadow:3px 3px 0 #ff3b3b}}
.t-brutal .frange-wrap{{background:#ffe500;border:2px solid #111;border-radius:0;color:#111;font-weight:700;box-shadow:3px 3px 0 #111}}
.t-brutal .cresult{{background:#f8f4dc;border:2px solid #111;border-radius:0;padding:14px;box-shadow:4px 4px 0 #111}}
.t-brutal .crrow{{color:#111;border-bottom-color:rgba(0,0,0,0.2)}}
.t-brutal .run-btn{{background:#111;border:3px solid #111;color:#ffe500;border-radius:0;width:100%;padding:12px;font-size:11px;font-weight:900;letter-spacing:2px;cursor:pointer;margin-top:4px;font-family:'Unbounded',sans-serif;box-shadow:5px 5px 0 #ff3b3b;transition:all 0.15s}}
.t-brutal .run-btn:hover{{transform:translate(-3px,-3px);box-shadow:8px 8px 0 #ff3b3b}}
.t-brutal .atable th{{color:#555;border-bottom:3px solid #111;font-family:'Unbounded',sans-serif;font-size:8px}}
.t-brutal .atable td{{color:#111;border-bottom:1px solid rgba(0,0,0,0.15)}}
.t-brutal .modal{{background:#fff;border:4px solid #111;border-radius:0;padding:22px;box-shadow:10px 10px 0 #ff3b3b}}
.t-brutal .modal-title{{color:#111;font-size:13px;letter-spacing:1px}}
.t-brutal .closex{{color:#555;font-weight:900}}
.t-brutal .empty-state{{color:rgba(0,0,0,0.25)}}
.t-brutal .fselect option{{background:#fff;color:#111}}

/* ══════════════════════════════════════════════════════
   THEME 4: CLAYMORPHISM
   Puffy, inflated, soft pastel forms with neumorphic shadows
══════════════════════════════════════════════════════ */
.t-clay{{
  background:linear-gradient(160deg,#ffecd2 0%,#fcb69f 35%,#e0c3fc 70%,#c2e9fb 100%);
  font-family:'DM Sans',sans-serif;
}}
.t-clay #lock{{background:linear-gradient(160deg,#ffecd2,#e0c3fc)}}
.t-clay .lbox{{background:linear-gradient(145deg,#fff8f2,#ffeee6);border:none;border-radius:28px;padding:32px;box-shadow:10px 10px 20px rgba(255,140,90,0.3),-6px -6px 16px rgba(255,255,255,0.95)}}
.t-clay .lock-icon{{color:#f5576c}}
.t-clay .lock-title{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'DM Sans',sans-serif}}
.t-clay .lock-sub{{color:#c07860}}
.t-clay #linput{{background:rgba(255,255,255,0.8);border:none;color:#5a2020;border-radius:16px;box-shadow:4px 4px 10px rgba(255,130,80,0.2) inset,-2px -2px 6px rgba(255,255,255,0.9) inset}}
.t-clay #linput:focus{{box-shadow:4px 4px 12px rgba(245,87,108,0.25) inset,-2px -2px 6px rgba(255,255,255,0.95) inset,0 0 0 3px rgba(245,87,108,0.15)}}
.t-clay #lbtn{{background:linear-gradient(135deg,#f5576c,#f093fb);border:none;color:#fff;border-radius:16px;box-shadow:5px 5px 12px rgba(245,87,108,0.4),-2px -2px 8px rgba(255,255,255,0.8);letter-spacing:1px}}
.t-clay #lerr{{color:#f5576c;font-weight:600}}
.t-clay .topbar{{background:rgba(255,255,255,0.6);border:none;box-shadow:0 4px 20px rgba(255,150,100,0.2);backdrop-filter:blur(10px)}}
.t-clay .logo-txt{{background:linear-gradient(135deg,#f5576c,#f093fb,#4facfe);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'DM Sans',sans-serif}}
.t-clay .live-pill{{background:rgba(255,255,255,0.7);border:none;box-shadow:3px 3px 8px rgba(67,233,123,0.25),-1px -1px 4px rgba(255,255,255,0.9);border-radius:20px}}
.t-clay .live-dot{{background:#43e97b;box-shadow:0 0 6px #43e97b}}
.t-clay .live-txt{{color:#1a8040}}
.t-clay .time-txt{{color:#a06040}}
.t-clay .logout-btn{{color:#c07860}}
.t-clay .statsbar{{background:rgba(255,255,255,0.45);border:none;box-shadow:0 2px 10px rgba(255,150,100,0.15)}}
.t-clay .ssc{{background:linear-gradient(145deg,#fff8f2,#ffeee6);border:none;border-radius:16px;box-shadow:5px 5px 12px rgba(255,130,80,0.22),-3px -3px 8px rgba(255,255,255,0.92)}}
.t-clay .ss-l{{color:#c07860}}
.t-clay .ss-v{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-clay .tabbar{{background:rgba(255,255,255,0.5);border:none;box-shadow:0 2px 12px rgba(255,150,100,0.12)}}
.t-clay .tabbtn{{color:#c07860;border-radius:14px;font-family:'DM Sans',sans-serif;font-weight:600}}
.t-clay .tabbtn:hover{{background:rgba(255,255,255,0.6);box-shadow:3px 3px 8px rgba(255,130,80,0.2),-1px -1px 4px rgba(255,255,255,0.8);transform:scale(1.04)}}
.t-clay .tabbtn.act{{background:linear-gradient(145deg,#fff0e8,#ffe4d8);box-shadow:4px 4px 10px rgba(245,87,108,0.25),-2px -2px 6px rgba(255,255,255,0.9);color:#c03040;border:none}}
.t-clay .card{{background:linear-gradient(145deg,#fff8f0,#ffeee4);border:none;border-radius:24px;box-shadow:8px 8px 18px rgba(255,130,80,0.28),-5px -5px 14px rgba(255,255,255,0.95);animation:clayIn 0.65s cubic-bezier(0.34,1.56,0.64,1) both}}
@keyframes clayIn{{0%{{opacity:0;transform:scale(0.88) translateY(18px)}}60%{{transform:scale(1.04) translateY(-4px)}}100%{{opacity:1;transform:scale(1) translateY(0)}}}}
.t-clay .card:hover{{transform:translateY(-7px) scale(1.02);box-shadow:12px 18px 28px rgba(255,130,80,0.35),-5px -5px 18px rgba(255,255,255,0.98)}}
.t-clay .cstripe{{background:linear-gradient(90deg,#ff9a9e,#fad0c4,#a18cd1,#c2e9fb);height:6px;border-radius:6px 6px 0 0}}
.t-clay .ctypebadge{{background:linear-gradient(135deg,#43e97b,#38f9d7);color:#fff;border:none;border-radius:10px;box-shadow:3px 3px 8px rgba(67,233,123,0.4)}}
.t-clay .cprofit{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-clay .cmatch{{color:#5a2020}}
.t-clay .cmeta{{color:#a06040}}
.t-clay .oval{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-clay .btag{{background:linear-gradient(135deg,#a18cd1,#fbc2eb);border:none;border-radius:10px;color:#fff;box-shadow:2px 2px 6px rgba(161,140,209,0.4)}}
.t-clay .stake-m{{color:#5a2020}}
.t-clay .calcbtn{{border:none;background:linear-gradient(135deg,rgba(161,140,209,0.2),rgba(251,194,235,0.2));color:#8060b0;border-radius:10px;box-shadow:3px 3px 8px rgba(161,140,209,0.3),-1px -1px 4px rgba(255,255,255,0.8)}}
.t-clay .calcbtn:hover{{box-shadow:5px 5px 12px rgba(161,140,209,0.4),-1px -1px 4px rgba(255,255,255,0.9);transform:scale(1.05)}}
.t-clay .csec{{background:linear-gradient(145deg,#fff8f0,#ffeee4);border:none;border-radius:20px;padding:16px;box-shadow:6px 6px 14px rgba(255,130,80,0.2),-3px -3px 10px rgba(255,255,255,0.9)}}
.t-clay .csec-title{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'DM Sans',sans-serif;font-weight:700}}
.t-clay .ctab{{color:#c07860;border:none;border-radius:12px;background:rgba(255,255,255,0.5);box-shadow:3px 3px 7px rgba(255,130,80,0.18),-1px -1px 4px rgba(255,255,255,0.85)}}
.t-clay .ctab.act{{background:linear-gradient(135deg,#f5576c,#f093fb);color:#fff;box-shadow:4px 4px 10px rgba(245,87,108,0.35)}}
.t-clay .finput,.t-clay .fselect{{background:rgba(255,255,255,0.75);border:none;color:#5a2020;border-radius:14px;box-shadow:3px 3px 8px rgba(255,130,80,0.15) inset,-1px -1px 4px rgba(255,255,255,0.9) inset}}
.t-clay .frange-wrap{{background:rgba(255,255,255,0.65);border:none;border-radius:14px;color:#a06040;box-shadow:3px 3px 8px rgba(255,130,80,0.15),-1px -1px 4px rgba(255,255,255,0.85)}}
.t-clay .cresult{{background:rgba(255,255,255,0.7);border:none;border-radius:14px;padding:14px;box-shadow:4px 4px 12px rgba(255,130,80,0.18) inset}}
.t-clay .crrow{{color:#5a2020;border-bottom-color:rgba(200,140,100,0.2)}}
.t-clay .run-btn{{background:linear-gradient(135deg,#f5576c,#f093fb);border:none;color:#fff;border-radius:16px;width:100%;padding:12px;font-size:12px;font-weight:700;cursor:pointer;margin-top:4px;font-family:'DM Sans',sans-serif;box-shadow:5px 5px 14px rgba(245,87,108,0.4),-2px -2px 8px rgba(255,255,255,0.8);letter-spacing:0.5px;transition:all 0.3s cubic-bezier(0.34,1.56,0.64,1)}}
.t-clay .run-btn:hover{{transform:scale(1.03) translateY(-2px);box-shadow:7px 8px 18px rgba(245,87,108,0.5)}}
.t-clay .atable th{{color:#c07860;border-bottom:1px solid rgba(200,140,100,0.3)}}
.t-clay .atable td{{color:#5a2020;border-bottom:1px solid rgba(200,140,100,0.15)}}
.t-clay .modal{{background:linear-gradient(145deg,#fff8f0,#ffeee4);border:none;border-radius:24px;padding:22px;box-shadow:12px 16px 32px rgba(255,130,80,0.3),-6px -6px 20px rgba(255,255,255,0.98)}}
.t-clay .modal-title{{background:linear-gradient(135deg,#f5576c,#f093fb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-clay .closex{{color:#c07860}}
.t-clay .empty-state{{color:rgba(160,96,64,0.4)}}

/* ══════════════════════════════════════════════════════
   THEME 5: MINIMALISM
   Pure white, 1px rules, generous space, maximum restraint
══════════════════════════════════════════════════════ */
.t-minimal{{
  background:#f8f8f6;
  font-family:'DM Sans',sans-serif;
  color:#1a1a1a;
}}
.t-minimal #lock{{background:#f8f8f6}}
.t-minimal .lbox{{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:32px;box-shadow:0 2px 16px rgba(0,0,0,0.06)}}
.t-minimal .lock-icon{{color:#111}}
.t-minimal .lock-title{{color:#111;font-family:'DM Sans',sans-serif;letter-spacing:2px}}
.t-minimal .lock-sub{{color:#999}}
.t-minimal #linput{{background:#f5f5f5;border:1px solid #ddd;color:#111;border-radius:4px}}
.t-minimal #linput:focus{{border-color:#111;background:#fff}}
.t-minimal #lbtn{{background:#111;border:1px solid #111;color:#fff;border-radius:4px;letter-spacing:2px}}
.t-minimal #lerr{{color:#c0392b}}
.t-minimal .topbar{{background:#fff;border-bottom:1px solid #e8e8e8}}
.t-minimal .logo-txt{{color:#111;font-family:'DM Sans',sans-serif;font-size:14px;letter-spacing:-0.5px}}
.t-minimal .live-pill{{background:#e8f5e9;border:1px solid #c8e6c9;border-radius:20px}}
.t-minimal .live-dot{{background:#2e7d32}}
.t-minimal .live-txt{{color:#2e7d32}}
.t-minimal .time-txt{{color:#999}}
.t-minimal .logout-btn{{color:#ccc}}
.t-minimal .logout-btn:hover{{color:#c0392b}}
.t-minimal .statsbar{{background:#fff;border-bottom:1px solid #ebebeb}}
.t-minimal .ssc{{background:#f8f8f6;border:1px solid #ebebeb;border-radius:6px}}
.t-minimal .ss-l{{color:#aaa}}
.t-minimal .ss-v{{color:#111;font-size:15px}}
.t-minimal .tabbar{{background:#fafaf8;border-bottom:1px solid #ebebeb}}
.t-minimal .tabbtn{{color:#aaa;border-radius:4px;font-weight:500}}
.t-minimal .tabbtn:hover{{color:#333;border-color:#e0e0e0}}
.t-minimal .tabbtn.act{{background:#111;color:#fff;border-color:#111;border-radius:4px}}
.t-minimal .card{{background:#fff;border:1px solid #ebebeb;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,0.04);animation:minIn 0.25s ease both}}
@keyframes minIn{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
.t-minimal .card:hover{{box-shadow:0 4px 16px rgba(0,0,0,0.08);border-color:#d0d0d0}}
.t-minimal .card:nth-child(odd) .cstripe{{background:#111}}
.t-minimal .card:nth-child(even) .cstripe{{background:#e0e0e0}}
.t-minimal .cstripe{{height:2px}}
.t-minimal .ctypebadge{{background:#f0f0f0;color:#555;border:1px solid #e0e0e0;border-radius:3px}}
.t-minimal .cprofit{{color:#1b5e20}}
.t-minimal .cmatch{{color:#1a1a1a}}
.t-minimal .cmeta{{color:#999}}
.t-minimal .oval{{color:#1a1a1a;font-weight:600}}
.t-minimal .btag{{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:3px;color:#555}}
.t-minimal .stake-m{{color:#1a1a1a}}
.t-minimal .calcbtn{{border:1px solid #e0e0e0;color:#555;border-radius:3px}}
.t-minimal .calcbtn:hover{{background:#f5f5f5;border-color:#aaa}}
.t-minimal .csec{{background:#fff;border:1px solid #ebebeb;border-radius:6px;padding:16px}}
.t-minimal .csec-title{{color:#1a1a1a}}
.t-minimal .ctab{{color:#999;border:1px solid #e0e0e0;border-radius:3px}}
.t-minimal .ctab.act{{background:#111;color:#fff;border-color:#111}}
.t-minimal .finput,.t-minimal .fselect{{background:#f8f8f6;border:1px solid #e0e0e0;color:#1a1a1a;border-radius:4px}}
.t-minimal .finput:focus,.t-minimal .fselect:focus{{border-color:#111;background:#fff}}
.t-minimal .frange-wrap{{background:#f5f5f3;border:1px solid #e5e5e5;border-radius:4px;color:#888}}
.t-minimal .cresult{{background:#f8f8f6;border:1px solid #ebebeb;border-radius:4px;padding:14px}}
.t-minimal .crrow{{color:#1a1a1a;border-bottom-color:#ebebeb}}
.t-minimal .run-btn{{background:#111;border:1px solid #111;color:#fff;border-radius:4px;width:100%;padding:12px;font-size:12px;font-weight:600;letter-spacing:1.5px;cursor:pointer;margin-top:4px;font-family:'DM Sans',sans-serif;transition:opacity 0.15s}}
.t-minimal .run-btn:hover{{opacity:0.8}}
.t-minimal .atable th{{color:#aaa;border-bottom:1px solid #ebebeb}}
.t-minimal .atable td{{color:#333;border-bottom:1px solid #f0f0f0}}
.t-minimal .modal{{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:22px;box-shadow:0 8px 40px rgba(0,0,0,0.1)}}
.t-minimal .modal-title{{color:#1a1a1a}}
.t-minimal .closex{{color:#ccc}}
.t-minimal .empty-state{{color:rgba(0,0,0,0.2)}}
.t-minimal .fselect option{{background:#fff;color:#111}}

/* ══════════════════════════════════════════════════════
   THEME 6: LIQUID GLASS
   Deep navy with teal energy, conic swirl animations
══════════════════════════════════════════════════════ */
.t-liquid{{
  background:#03091a;
  font-family:'Space Grotesk',sans-serif;
}}
.t-liquid #app::before{{
  background:
    radial-gradient(ellipse 100% 70% at 8% 18%,rgba(0,255,175,0.22) 0%,transparent 55%),
    radial-gradient(ellipse 80% 60% at 92% 78%,rgba(0,90,255,0.28) 0%,transparent 55%),
    radial-gradient(ellipse 70% 90% at 48% 55%,rgba(80,0,255,0.16) 0%,transparent 60%);
  animation:liqBg 14s ease-in-out infinite alternate;
}}
@keyframes liqBg{{0%{{transform:scale(1)}}100%{{transform:scale(1.07) rotate(1deg)}}}}
.t-liquid #lock{{background:#03091a}}
.t-liquid .lbox{{background:rgba(3,20,50,0.8);border:1px solid rgba(0,255,175,0.2);border-radius:20px;padding:30px;backdrop-filter:blur(20px);box-shadow:0 8px 40px rgba(0,0,0,0.6),inset 0 1px 0 rgba(0,255,175,0.15)}}
.t-liquid .lock-icon{{color:#00ffaf;text-shadow:0 0 20px rgba(0,255,175,0.5)}}
.t-liquid .lock-title{{background:linear-gradient(135deg,#00ffaf,#00b8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Space Grotesk',sans-serif}}
.t-liquid .lock-sub{{color:rgba(0,255,175,0.4)}}
.t-liquid #linput{{background:rgba(0,40,80,0.6);border:1px solid rgba(0,255,175,0.25);color:#fff;border-radius:12px}}
.t-liquid #linput:focus{{border-color:rgba(0,255,175,0.7);box-shadow:0 0 0 3px rgba(0,255,175,0.1),0 0 20px rgba(0,255,175,0.1)}}
.t-liquid #lbtn{{background:linear-gradient(135deg,#00c896,#0078ff);border:none;color:#fff;border-radius:12px;letter-spacing:1.5px}}
.t-liquid #lerr{{color:#ff6b6b}}
.t-liquid .topbar{{background:rgba(3,10,30,0.7);border-bottom:1px solid rgba(0,255,175,0.12);backdrop-filter:blur(20px)}}
.t-liquid .logo-txt{{background:linear-gradient(135deg,#00ffaf,#00b8ff,#5050ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Space Grotesk',sans-serif}}
.t-liquid .live-pill{{background:rgba(0,255,175,0.08);border:1px solid rgba(0,255,175,0.2)}}
.t-liquid .live-dot{{background:#00ffaf;box-shadow:0 0 8px #00ffaf}}
.t-liquid .live-txt{{color:#00ffaf}}
.t-liquid .time-txt{{color:rgba(0,255,175,0.4)}}
.t-liquid .logout-btn{{color:rgba(0,255,175,0.4)}}
.t-liquid .statsbar{{background:rgba(3,10,30,0.6);border-bottom:1px solid rgba(0,255,175,0.07)}}
.t-liquid .ssc{{background:rgba(3,20,55,0.7);border:1px solid rgba(0,255,175,0.14);border-radius:14px;backdrop-filter:blur(10px)}}
.t-liquid .ss-l{{color:rgba(0,255,175,0.4)}}
.t-liquid .ss-v{{background:linear-gradient(135deg,#00ffaf,#00b8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-liquid .tabbar{{background:rgba(3,10,30,0.6);border-bottom:1px solid rgba(0,255,175,0.08)}}
.t-liquid .tabbtn{{color:rgba(0,255,175,0.32);border-radius:10px}}
.t-liquid .tabbtn:hover{{color:rgba(0,255,175,0.8);background:rgba(0,255,175,0.05);border-color:rgba(0,255,175,0.2)}}
.t-liquid .tabbtn.act{{background:rgba(0,255,175,0.1);border-color:rgba(0,255,175,0.4);color:#00ffaf;box-shadow:0 0 18px rgba(0,255,175,0.15)}}
.t-liquid .card{{background:rgba(3,20,55,0.55);border:1px solid rgba(0,255,175,0.15);border-radius:18px;box-shadow:0 8px 32px rgba(0,0,0,0.55),inset 0 1px 0 rgba(0,255,175,0.1);animation:liqCardIn 0.7s cubic-bezier(0.16,1,0.3,1) both;overflow:hidden;position:relative}}
.t-liquid .card::after{{content:'';position:absolute;top:-60%;left:-60%;width:220%;height:220%;background:conic-gradient(from 0deg,transparent 75%,rgba(0,255,175,0.04) 100%);animation:liqSpin 10s linear infinite;pointer-events:none}}
@keyframes liqSpin{{to{{transform:rotate(360deg)}}}}
@keyframes liqCardIn{{from{{opacity:0;transform:translateY(28px);filter:blur(6px)}}to{{opacity:1;transform:translateY(0);filter:blur(0)}}}}
.t-liquid .card:hover{{border-color:rgba(0,255,175,0.45);box-shadow:0 16px 48px rgba(0,0,0,0.7),0 0 32px rgba(0,255,175,0.1);transform:translateY(-4px)}}
.t-liquid .cstripe{{background:linear-gradient(90deg,transparent,rgba(0,255,175,0.9),rgba(0,180,255,0.8),transparent);animation:liqStroke 3s ease-in-out infinite}}
@keyframes liqStroke{{0%,100%{{opacity:0.5}}50%{{opacity:1}}}}
.t-liquid .ctypebadge{{background:rgba(0,255,175,0.1);color:#00ffaf;border:1px solid rgba(0,255,175,0.3);border-radius:5px}}
.t-liquid .cprofit{{background:linear-gradient(135deg,#00ffaf,#00b8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-liquid .cmatch{{color:rgba(200,240,255,0.9)}}
.t-liquid .cmeta{{color:rgba(0,255,175,0.38)}}
.t-liquid .oval{{color:#00ffaf;text-shadow:0 0 8px rgba(0,255,175,0.4)}}
.t-liquid .btag{{background:rgba(0,90,255,0.2);border:1px solid rgba(0,90,255,0.35);border-radius:5px;color:rgba(100,190,255,0.9)}}
.t-liquid .stake-m{{color:rgba(200,240,255,0.9)}}
.t-liquid .calcbtn{{border:1px solid rgba(0,255,175,0.3);color:#00ffaf;border-radius:7px}}
.t-liquid .calcbtn:hover{{background:rgba(0,255,175,0.08)}}
.t-liquid .csec{{background:rgba(3,20,55,0.7);border:1px solid rgba(0,255,175,0.14);border-radius:14px;padding:16px;backdrop-filter:blur(12px)}}
.t-liquid .csec-title{{background:linear-gradient(135deg,#00ffaf,#00b8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-liquid .ctab{{color:rgba(0,255,175,0.35);border:1px solid rgba(0,255,175,0.15);border-radius:8px}}
.t-liquid .ctab.act{{background:rgba(0,255,175,0.12);color:#00ffaf;border-color:rgba(0,255,175,0.45)}}
.t-liquid .finput,.t-liquid .fselect{{background:rgba(0,30,70,0.7);border:1px solid rgba(0,255,175,0.18);color:#fff;border-radius:8px}}
.t-liquid .finput:focus,.t-liquid .fselect:focus{{border-color:rgba(0,255,175,0.55)}}
.t-liquid .frange-wrap{{background:rgba(0,30,70,0.6);border:1px solid rgba(0,255,175,0.14);border-radius:8px;color:rgba(0,255,175,0.5)}}
.t-liquid .cresult{{background:rgba(0,10,30,0.7);border:1px solid rgba(0,255,175,0.15);border-radius:10px;padding:14px}}
.t-liquid .crrow{{color:rgba(200,240,255,0.75);border-bottom-color:rgba(0,255,175,0.1)}}
.t-liquid .run-btn{{background:linear-gradient(135deg,#00c896,#0078ff);border:none;color:#fff;border-radius:12px;width:100%;padding:12px;font-size:12px;font-weight:700;cursor:pointer;margin-top:4px;font-family:'Space Grotesk',sans-serif;letter-spacing:1px;transition:opacity 0.2s}}
.t-liquid .run-btn:hover{{opacity:0.85}}
.t-liquid .atable th{{color:rgba(0,255,175,0.38);border-bottom:1px solid rgba(0,255,175,0.1)}}
.t-liquid .atable td{{color:rgba(200,240,255,0.75);border-bottom:1px solid rgba(0,255,175,0.07)}}
.t-liquid .modal{{background:rgba(3,15,45,0.92);border:1px solid rgba(0,255,175,0.2);border-radius:18px;padding:22px;backdrop-filter:blur(20px)}}
.t-liquid .modal-title{{background:linear-gradient(135deg,#00ffaf,#00b8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-liquid .closex{{color:rgba(0,255,175,0.45)}}
.t-liquid .empty-state{{color:rgba(0,255,175,0.25)}}
.t-liquid .fselect option{{background:#03091a;color:#fff}}

/* ══════════════════════════════════════════════════════
   THEME 7: DARK TERMINAL
   Matrix green phosphor on pitch black, CRT scanlines
══════════════════════════════════════════════════════ */
.t-terminal{{
  background:#020b02;
  font-family:'IBM Plex Mono',monospace;
}}
.t-terminal #app::before{{
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,65,0.018) 2px,rgba(0,255,65,0.018) 4px);
  animation:scanln 12s linear infinite;
}}
@keyframes scanln{{from{{background-position:0 0}}to{{background-position:0 200px}}}}
.t-terminal #lock{{background:#020b02}}
.t-terminal .lbox{{background:#000;border:1px solid rgba(0,255,65,0.35);border-radius:4px;padding:28px;box-shadow:0 0 40px rgba(0,255,65,0.08),inset 0 0 30px rgba(0,0,0,0.8)}}
.t-terminal .lock-icon{{color:#00ff41;text-shadow:0 0 20px rgba(0,255,65,0.7)}}
.t-terminal .lock-title{{color:#00ff41;font-family:'IBM Plex Mono',monospace;text-shadow:0 0 12px rgba(0,255,65,0.5);font-size:16px;letter-spacing:5px}}
.t-terminal .lock-sub{{color:rgba(0,255,65,0.4)}}
.t-terminal #linput{{background:#000;border:1px solid rgba(0,255,65,0.35);color:#00ff41;border-radius:2px;font-family:'IBM Plex Mono',monospace}}
.t-terminal #linput:focus{{border-color:rgba(0,255,65,0.8);box-shadow:0 0 12px rgba(0,255,65,0.15),0 0 0 1px rgba(0,255,65,0.3)}}
.t-terminal #lbtn{{background:rgba(0,255,65,0.1);border:1px solid rgba(0,255,65,0.45);color:#00ff41;border-radius:2px;letter-spacing:3px;font-family:'IBM Plex Mono',monospace;text-shadow:0 0 8px rgba(0,255,65,0.6)}}
.t-terminal #lerr{{color:#ff4141;text-shadow:0 0 8px rgba(255,65,65,0.5)}}
.t-terminal .topbar{{background:#000;border-bottom:1px solid rgba(0,255,65,0.22);box-shadow:0 0 20px rgba(0,255,65,0.06)}}
.t-terminal .logo-txt{{color:#00ff41;font-family:'IBM Plex Mono',monospace;text-shadow:0 0 16px rgba(0,255,65,0.55),0 0 32px rgba(0,255,65,0.25);letter-spacing:3px;font-size:13px}}
.t-terminal .live-pill{{background:transparent;border:1px solid rgba(0,255,65,0.3)}}
.t-terminal .live-dot{{background:#00ff41;box-shadow:0 0 8px #00ff41}}
.t-terminal .live-txt{{color:#00ff41;text-shadow:0 0 8px rgba(0,255,65,0.7)}}
.t-terminal .time-txt{{color:rgba(0,255,65,0.4)}}
.t-terminal .logout-btn{{color:rgba(0,255,65,0.4)}}
.t-terminal .statsbar{{background:#000;border-bottom:1px solid rgba(0,255,65,0.12)}}
.t-terminal .ssc{{background:#030f03;border:1px solid rgba(0,255,65,0.2);border-radius:2px;box-shadow:inset 0 0 12px rgba(0,0,0,0.8)}}
.t-terminal .ss-l{{color:rgba(0,255,65,0.38);font-size:8px;letter-spacing:2px}}
.t-terminal .ss-v{{color:#00ff41;text-shadow:0 0 8px rgba(0,255,65,0.4)}}
.t-terminal .tabbar{{background:#000;border-bottom:1px solid rgba(0,255,65,0.14)}}
.t-terminal .tabbtn{{color:rgba(0,255,65,0.3);border-radius:2px;font-size:10px;letter-spacing:1px}}
.t-terminal .tabbtn:hover{{color:rgba(0,255,65,0.8);border-color:rgba(0,255,65,0.3);text-shadow:0 0 6px rgba(0,255,65,0.5)}}
.t-terminal .tabbtn.act{{background:rgba(0,255,65,0.08);border-color:rgba(0,255,65,0.45);color:#00ff41;box-shadow:0 0 12px rgba(0,255,65,0.15)}}
.t-terminal .card{{background:#030f03;border:1px solid rgba(0,255,65,0.22);border-radius:3px;box-shadow:0 0 18px rgba(0,255,65,0.07),inset 0 0 20px rgba(0,0,0,0.6);animation:termIn 0.45s ease both}}
@keyframes termIn{{0%{{opacity:0;border-left-width:3px;border-left-color:rgba(0,255,65,0.8)}}100%{{opacity:1;border-left-width:1px}}}}
.t-terminal .card:hover{{border-color:rgba(0,255,65,0.55);box-shadow:0 0 28px rgba(0,255,65,0.12)}}
.t-terminal .cstripe{{background:rgba(0,255,65,0.4);height:1px}}
.t-terminal .ctypebadge{{background:transparent;color:#00ff41;border:1px solid rgba(0,255,65,0.45);border-radius:2px;text-shadow:0 0 6px rgba(0,255,65,0.5)}}
.t-terminal .cprofit{{color:#00ff41;text-shadow:0 0 12px rgba(0,255,65,0.5)}}
.t-terminal .cmatch{{color:rgba(0,255,65,0.88);text-shadow:0 0 8px rgba(0,255,65,0.25)}}
.t-terminal .cmeta{{color:rgba(0,255,65,0.35)}}
.t-terminal .oval{{color:#00ff41;font-weight:800;text-shadow:0 0 10px rgba(0,255,65,0.6)}}
.t-terminal .btag{{background:transparent;border:1px solid rgba(0,255,65,0.3);border-radius:2px;color:rgba(0,255,65,0.7)}}
.t-terminal .stake-m{{color:rgba(0,255,65,0.9);text-shadow:0 0 6px rgba(0,255,65,0.3)}}
.t-terminal .calcbtn{{border:1px solid rgba(0,255,65,0.3);color:rgba(0,255,65,0.6);border-radius:2px}}
.t-terminal .calcbtn:hover{{background:rgba(0,255,65,0.07);color:#00ff41}}
.t-terminal .csec{{background:#030f03;border:1px solid rgba(0,255,65,0.18);border-radius:2px;padding:16px;box-shadow:inset 0 0 16px rgba(0,0,0,0.7)}}
.t-terminal .csec-title{{color:#00ff41;text-shadow:0 0 8px rgba(0,255,65,0.4)}}
.t-terminal .ctab{{color:rgba(0,255,65,0.35);border:1px solid rgba(0,255,65,0.18);border-radius:2px}}
.t-terminal .ctab.act{{background:rgba(0,255,65,0.1);color:#00ff41;border-color:rgba(0,255,65,0.5);text-shadow:0 0 8px rgba(0,255,65,0.5)}}
.t-terminal .finput,.t-terminal .fselect{{background:#000;border:1px solid rgba(0,255,65,0.28);color:#00ff41;border-radius:2px;font-family:'IBM Plex Mono',monospace}}
.t-terminal .finput:focus,.t-terminal .fselect:focus{{border-color:rgba(0,255,65,0.7);box-shadow:0 0 8px rgba(0,255,65,0.1)}}
.t-terminal .frange-wrap{{background:#000;border:1px solid rgba(0,255,65,0.2);border-radius:2px;color:rgba(0,255,65,0.5)}}
.t-terminal .cresult{{background:#000;border:1px solid rgba(0,255,65,0.18);border-radius:2px;padding:14px}}
.t-terminal .crrow{{color:rgba(0,255,65,0.75);border-bottom-color:rgba(0,255,65,0.1)}}
.t-terminal .run-btn{{background:rgba(0,255,65,0.1);border:1px solid rgba(0,255,65,0.45);color:#00ff41;border-radius:2px;width:100%;padding:12px;font-size:11px;font-weight:700;cursor:pointer;margin-top:4px;font-family:'IBM Plex Mono',monospace;letter-spacing:2px;text-shadow:0 0 8px rgba(0,255,65,0.5);transition:all 0.2s}}
.t-terminal .run-btn:hover{{background:rgba(0,255,65,0.18);box-shadow:0 0 16px rgba(0,255,65,0.2)}}
.t-terminal .atable th{{color:rgba(0,255,65,0.38);border-bottom:1px solid rgba(0,255,65,0.15)}}
.t-terminal .atable td{{color:rgba(0,255,65,0.7);border-bottom:1px solid rgba(0,255,65,0.08)}}
.t-terminal .modal{{background:#010a01;border:1px solid rgba(0,255,65,0.3);border-radius:3px;padding:22px;backdrop-filter:blur(5px)}}
.t-terminal .modal-title{{color:#00ff41;text-shadow:0 0 10px rgba(0,255,65,0.4)}}
.t-terminal .closex{{color:rgba(0,255,65,0.45)}}
.t-terminal .empty-state{{color:rgba(0,255,65,0.25)}}
.t-terminal .fselect option{{background:#020b02;color:#00ff41}}

/* ══════════════════════════════════════════════════════
   THEME 8: AURORA NEON
   Deep violet, shifting hue-rotate aurora, gradient everything
══════════════════════════════════════════════════════ */
.t-aurora{{
  background:#08001a;
  font-family:'Space Grotesk',sans-serif;
}}
.t-aurora #app::before{{
  background:
    radial-gradient(ellipse 110% 55% at 0% 0%,rgba(255,0,140,0.42) 0%,transparent 52%),
    radial-gradient(ellipse 90% 65% at 100% 0%,rgba(0,200,255,0.38) 0%,transparent 52%),
    radial-gradient(ellipse 75% 75% at 50% 100%,rgba(160,0,255,0.42) 0%,transparent 58%),
    radial-gradient(ellipse 65% 45% at 75% 50%,rgba(0,255,140,0.18) 0%,transparent 42%);
  animation:aurora 11s ease-in-out infinite alternate;
}}
@keyframes aurora{{0%{{filter:hue-rotate(0deg) brightness(1)}}100%{{filter:hue-rotate(45deg) brightness(1.12)}}}}
.t-aurora #lock{{background:rgba(8,0,26,0.97)}}
.t-aurora .lbox{{background:rgba(20,5,45,0.85);border:1px solid rgba(255,0,140,0.25);border-radius:20px;padding:30px;backdrop-filter:blur(20px);box-shadow:0 8px 40px rgba(200,0,120,0.2)}}
.t-aurora .lock-icon{{background:linear-gradient(135deg,#ff006c,#a855f7,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-aurora .lock-title{{background:linear-gradient(135deg,#ff6ec7,#a855f7,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Syne',sans-serif;letter-spacing:4px}}
.t-aurora .lock-sub{{color:rgba(255,110,199,0.4)}}
.t-aurora #linput{{background:rgba(30,5,60,0.7);border:1px solid rgba(255,0,140,0.25);color:#fff;border-radius:12px}}
.t-aurora #linput:focus{{border-color:rgba(255,0,140,0.6);box-shadow:0 0 0 3px rgba(255,0,140,0.1),0 0 20px rgba(255,0,140,0.1)}}
.t-aurora #lbtn{{background:linear-gradient(135deg,#ff006c,#a855f7,#00c8ff);border:none;color:#fff;border-radius:12px;letter-spacing:2px}}
.t-aurora #lerr{{color:#ff6b6b}}
.t-aurora .topbar{{background:rgba(8,0,26,0.75);border-bottom:1px solid rgba(255,0,140,0.18);backdrop-filter:blur(20px);box-shadow:0 4px 30px rgba(200,0,120,0.15)}}
.t-aurora .logo-txt{{background:linear-gradient(135deg,#ff6ec7,#a855f7,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Syne',sans-serif}}
.t-aurora .live-pill{{background:rgba(0,255,140,0.08);border:1px solid rgba(0,255,140,0.25)}}
.t-aurora .live-dot{{background:#00ff8c;box-shadow:0 0 8px #00ff8c}}
.t-aurora .live-txt{{color:#00ff8c}}
.t-aurora .time-txt{{color:rgba(255,110,199,0.45)}}
.t-aurora .logout-btn{{color:rgba(255,110,199,0.45)}}
.t-aurora .statsbar{{background:rgba(8,0,26,0.65);border-bottom:1px solid rgba(255,0,140,0.08)}}
.t-aurora .ssc{{background:rgba(20,5,45,0.7);border:1px solid rgba(255,0,140,0.16);border-radius:14px;backdrop-filter:blur(10px)}}
.t-aurora .ss-l{{color:rgba(255,110,199,0.4)}}
.t-aurora .ss-v{{background:linear-gradient(135deg,#ff6ec7,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-aurora .tabbar{{background:rgba(8,0,26,0.65);border-bottom:1px solid rgba(255,0,140,0.1)}}
.t-aurora .tabbtn{{color:rgba(255,110,199,0.32);border-radius:10px}}
.t-aurora .tabbtn:hover{{color:rgba(255,110,199,0.8);background:rgba(255,0,140,0.07);border-color:rgba(255,0,140,0.2)}}
.t-aurora .tabbtn.act{{background:rgba(255,0,140,0.14);border-color:rgba(255,0,140,0.42);color:#ff6ec7;box-shadow:0 0 18px rgba(255,0,140,0.2)}}
.t-aurora .card{{background:rgba(18,5,40,0.7);border:1px solid rgba(255,0,140,0.18);border-radius:20px;box-shadow:0 8px 32px rgba(0,0,0,0.55),inset 0 1px 0 rgba(255,110,199,0.12);animation:auroraIn 0.8s cubic-bezier(0.16,1,0.3,1) both;position:relative;overflow:hidden}}
.t-aurora .card::before{{content:'';position:absolute;top:0;left:0;right:0;height:80%;background:linear-gradient(180deg,rgba(255,0,140,0.04) 0%,transparent 50%);pointer-events:none}}
@keyframes auroraIn{{from{{opacity:0;transform:scale(0.93) translateY(20px);filter:blur(5px)}}to{{opacity:1;transform:scale(1) translateY(0);filter:blur(0)}}}}
.t-aurora .card:hover{{border-color:rgba(255,0,140,0.48);box-shadow:0 16px 48px rgba(0,0,0,0.65),0 0 36px rgba(200,0,120,0.18);transform:translateY(-5px)}}
.t-aurora .cstripe{{background:linear-gradient(90deg,#ff006c,#ff6ec7,#a855f7,#00c8ff,#00ff8c);background-size:300% 100%;animation:auroraStroke 5s linear infinite}}
@keyframes auroraStroke{{0%{{background-position:0% 0%}}100%{{background-position:300% 0%}}}}
.t-aurora .ctypebadge{{background:rgba(0,255,140,0.1);color:#00ff8c;border:1px solid rgba(0,255,140,0.3);border-radius:5px}}
.t-aurora .cprofit{{background:linear-gradient(135deg,#00ff8c,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-aurora .cmatch{{color:rgba(240,220,255,0.92)}}
.t-aurora .cmeta{{color:rgba(255,110,199,0.38)}}
.t-aurora .oval{{background:linear-gradient(90deg,#ffd700,#ff6ec7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-weight:800}}
.t-aurora .btag{{background:rgba(0,200,255,0.12);border:1px solid rgba(0,200,255,0.3);border-radius:5px;color:rgba(100,220,255,0.9)}}
.t-aurora .stake-m{{color:rgba(240,220,255,0.9)}}
.t-aurora .calcbtn{{border:1px solid rgba(255,0,140,0.3);color:#ff6ec7;border-radius:8px}}
.t-aurora .calcbtn:hover{{background:rgba(255,0,140,0.08)}}
.t-aurora .csec{{background:rgba(18,5,40,0.8);border:1px solid rgba(255,0,140,0.16);border-radius:16px;padding:16px;backdrop-filter:blur(14px)}}
.t-aurora .csec-title{{background:linear-gradient(135deg,#ff6ec7,#a855f7,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-aurora .ctab{{color:rgba(255,110,199,0.35);border:1px solid rgba(255,0,140,0.15);border-radius:8px}}
.t-aurora .ctab.act{{background:rgba(255,0,140,0.15);color:#ff6ec7;border-color:rgba(255,0,140,0.45);box-shadow:0 0 14px rgba(255,0,140,0.2)}}
.t-aurora .finput,.t-aurora .fselect{{background:rgba(25,5,55,0.8);border:1px solid rgba(255,0,140,0.2);color:#fff;border-radius:10px}}
.t-aurora .finput:focus,.t-aurora .fselect:focus{{border-color:rgba(255,0,140,0.6)}}
.t-aurora .frange-wrap{{background:rgba(25,5,55,0.7);border:1px solid rgba(255,0,140,0.15);border-radius:10px;color:rgba(255,110,199,0.5)}}
.t-aurora .cresult{{background:rgba(10,2,28,0.8);border:1px solid rgba(255,0,140,0.15);border-radius:10px;padding:14px}}
.t-aurora .crrow{{color:rgba(230,210,255,0.75);border-bottom-color:rgba(255,0,140,0.1)}}
.t-aurora .run-btn{{background:linear-gradient(135deg,#ff006c,#a855f7,#00c8ff);border:none;color:#fff;border-radius:12px;width:100%;padding:12px;font-size:12px;font-weight:700;cursor:pointer;margin-top:4px;font-family:'Space Grotesk',sans-serif;letter-spacing:1px;box-shadow:0 4px 20px rgba(200,0,120,0.35);transition:opacity 0.2s,transform 0.2s}}
.t-aurora .run-btn:hover{{opacity:0.88;transform:translateY(-1px)}}
.t-aurora .atable th{{color:rgba(255,110,199,0.38);border-bottom:1px solid rgba(255,0,140,0.12)}}
.t-aurora .atable td{{color:rgba(230,210,255,0.75);border-bottom:1px solid rgba(255,0,140,0.07)}}
.t-aurora .modal{{background:rgba(12,3,30,0.92);border:1px solid rgba(255,0,140,0.22);border-radius:18px;padding:22px;backdrop-filter:blur(20px)}}
.t-aurora .modal-title{{background:linear-gradient(135deg,#ff6ec7,#a855f7,#00c8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.t-aurora .closex{{color:rgba(255,110,199,0.45)}}
.t-aurora .empty-state{{color:rgba(255,110,199,0.25)}}
.t-aurora .fselect option{{background:#08001a;color:#fff}}
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════
     THEME SWITCHER BAR
═══════════════════════════════════════════════════════ -->
<div id="theme-bar">
  <div class="tb-logo">THEME</div>
  <button class="tp active" onclick="setTheme('glass',this)">Glassmorphism</button>
  <button class="tp" onclick="setTheme('skeu',this)">Skeuomorphism</button>
  <button class="tp" onclick="setTheme('brutal',this)">Neo-Brutalism</button>
  <button class="tp" onclick="setTheme('clay',this)">Claymorphism</button>
  <button class="tp" onclick="setTheme('minimal',this)">Minimalism</button>
  <button class="tp" onclick="setTheme('liquid',this)">Liquid Glass</button>
  <button class="tp" onclick="setTheme('terminal',this)">Dark Terminal</button>
  <button class="tp" onclick="setTheme('aurora',this)">Aurora Neon</button>
</div>

<!-- ═══════════════════════════════════════════════════════
     LOCK SCREEN
═══════════════════════════════════════════════════════ -->
<div id="lock">
  <div class="lbox">
    <i class="fas fa-crosshairs lock-icon"></i>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">v5.0 — ELITE EDITION</div>
    <input id="linput" type="password" placeholder="••••••••"
           autocomplete="current-password"/>
    <button id="lbtn" onclick="unlock()">
      <i class="fas fa-unlock-alt"></i>&nbsp; UNLOCK
    </button>
    <div id="lerr">
      <i class="fas fa-triangle-exclamation"></i> Invalid password
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     MAIN APP
═══════════════════════════════════════════════════════ -->
<div id="app" class="t-glass">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="logo-wrap">
      <i class="fas fa-crosshairs" style="font-size:15px"></i>
      <span class="logo-txt">ARB SNIPER v5.0</span>
    </div>
    <div class="topbar-r">
      <div class="live-pill">
        <div class="live-dot"></div>
        <span class="live-txt">LIVE</span>
      </div>
      <span class="time-txt">{ist_now}</span>
      <button class="logout-btn" onclick="logout()" title="Logout">
        <i class="fas fa-right-from-bracket"></i>
      </button>
    </div>
  </div>

  <!-- STATS BAR -->
  <div class="statsbar">
    <div class="ssc"><div class="ss-l">Arbs</div>
      <div class="ss-v" id="ss-arb">0</div></div>
    <div class="ssc"><div class="ss-l">+EV Bets</div>
      <div class="ss-v" id="ss-ev">0</div></div>
    <div class="ssc"><div class="ss-l">BC Events</div>
      <div class="ss-v" id="ss-bc">0</div></div>
    <div class="ssc"><div class="ss-l">Top Arb</div>
      <div class="ss-v" id="ss-toparb">—</div></div>
    <div class="ssc"><div class="ss-l">Top EV Edge</div>
      <div class="ss-v" id="ss-topev">—</div></div>
    <div class="ssc"><div class="ss-l">Profit / ₹1K</div>
      <div class="ss-v" id="ss-profit">—</div></div>
    <div class="ssc"><div class="ss-l">Events</div>
      <div class="ss-v">{state.get('total_events_scanned', 0)}</div></div>
    <div class="ssc"><div class="ss-l">API Quota</div>
      <div class="ss-v">{total_quota}</div></div>
    <div class="ssc"><div class="ss-l">Keys</div>
      <div class="ss-v">{total_keys}</div></div>
  </div>

  <!-- TAB BAR -->
  <div class="tabbar" id="tabbar">
    <button class="tabbtn act" id="tb-arb" onclick="swTab('arb',this)">
      <i class="fas fa-percent"></i> Arbitrage
      <span class="tbadge" id="cnt-arb" style="background:#4ade80;color:#000">0</span>
    </button>
    <button class="tabbtn" id="tb-ev" onclick="swTab('ev',this)">
      <i class="fas fa-chart-line"></i> +EV Bets
      <span class="tbadge" id="cnt-ev" style="background:#a78bfa;color:#000">0</span>
    </button>
    <button class="tabbtn" id="tb-bc" onclick="swTab('bc',this)">
      <i class="fas fa-gamepad"></i> BC.Game
      <span class="tbadge" id="cnt-bc" style="background:#fbbf24;color:#000">0</span>
    </button>
    <button class="tabbtn" id="tb-calc" onclick="swTab('calc',this)">
      <i class="fas fa-calculator"></i> Calculator
    </button>
    <button class="tabbtn" id="tb-api" onclick="swTab('api',this)">
      <i class="fas fa-server"></i> API Keys
    </button>
  </div>

  <!-- ── TAB: ARBITRAGE ─────────────────────────────── -->
  <div id="tc-arb" class="tc act">
    <div class="fbar">
      <input class="finput" id="arb-q"
             placeholder="&#xf002;  Search match or sport..."
             oninput="filterArbs()"/>
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
      <div class="frange-wrap">
        Min
        <input type="range" id="arb-min" min="0" max="5"
               step="0.1" value="0"
               oninput="document.getElementById('arb-minv').textContent=
                        (+this.value).toFixed(1)+'%';filterArbs()"/>
        <span id="arb-minv">0.0%</span>
      </div>
    </div>
    <div class="grid" id="grid-arb"></div>
  </div>

  <!-- ── TAB: +EV BETS ──────────────────────────────── -->
  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input class="finput" id="ev-q"
             placeholder="&#xf002;  Search match or bookmaker..."
             oninput="filterEvs()"/>
      <select class="fselect" id="ev-book" onchange="filterEvs()">
        <option value="">All Books</option>
      </select>
      <select class="fselect" id="ev-sport" onchange="filterEvs()">
        <option value="">All Sports</option>
      </select>
      <div class="frange-wrap">
        Min Edge
        <input type="range" id="ev-min" min="0" max="20"
               step="0.5" value="0"
               oninput="document.getElementById('ev-minv').textContent=
                        (+this.value).toFixed(1)+'%';filterEvs()"/>
        <span id="ev-minv">0.0%</span>
      </div>
    </div>
    <div class="grid" id="grid-ev"></div>
  </div>

  <!-- ── TAB: BC.GAME ───────────────────────────────── -->
  <div id="tc-bc" class="tc">
    <div class="fbar">
      <input class="finput" id="bc-q"
             placeholder="&#xf002;  Search BC.Game feed..."
             oninput="filterBc()"/>
      <select class="fselect" id="bc-sport" onchange="filterBc()">
        <option value="">All Sports</option>
      </select>
    </div>
    <div class="grid" id="grid-bc"></div>
  </div>

  <!-- ── TAB: CALCULATOR ────────────────────────────── -->
  <div id="tc-calc" class="tc" style="max-width:540px">

    <!-- Arb Calculator -->
    <div class="csec">
      <div class="csec-title">
        <i class="fas fa-percent"></i> Arbitrage Calculator
      </div>
      <div class="ctabs">
        <button class="ctab act" id="ct2" onclick="swCalc(2,this)">2-Way</button>
        <button class="ctab" id="ct3" onclick="swCalc(3,this)">3-Way</button>
      </div>
      <div id="c2form">
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">ODDS — LEG 1</div>
          <input class="finput" id="c2o1" type="number" step="0.01"
                 placeholder="e.g. 2.15" style="width:100%"/>
        </div>
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">ODDS — LEG 2</div>
          <input class="finput" id="c2o2" type="number" step="0.01"
                 placeholder="e.g. 2.05" style="width:100%"/>
        </div>
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">TOTAL STAKE (₹)</div>
          <input class="finput" id="c2s" type="number"
                 value="10000" style="width:100%"/>
        </div>
      </div>
      <div id="c3form" style="display:none">
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">HOME ODDS</div>
          <input class="finput" id="c3o1" type="number" step="0.01"
                 placeholder="e.g. 2.50" style="width:100%"/>
        </div>
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">DRAW ODDS</div>
          <input class="finput" id="c3o2" type="number" step="0.01"
                 placeholder="e.g. 3.20" style="width:100%"/>
        </div>
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">AWAY ODDS</div>
          <input class="finput" id="c3o3" type="number" step="0.01"
                 placeholder="e.g. 2.80" style="width:100%"/>
        </div>
        <div style="margin-bottom:10px">
          <div class="ss-l" style="margin-bottom:5px">TOTAL STAKE (₹)</div>
          <input class="finput" id="c3s" type="number"
                 value="10000" style="width:100%"/>
        </div>
      </div>
      <button class="run-btn" onclick="runCalc()">
        <i class="fas fa-bolt"></i>&nbsp; CALCULATE
      </button>
      <div id="calc-res" class="cresult" style="display:none"></div>
    </div>

    <!-- Kelly Calculator -->
    <div class="csec">
      <div class="csec-title">
        <i class="fas fa-brain"></i> Kelly Criterion Calculator
      </div>
      <div style="margin-bottom:10px">
        <div class="ss-l" style="margin-bottom:5px">YOUR WIN PROBABILITY (%)</div>
        <input class="finput" id="kp" type="number" step="0.1"
               placeholder="e.g. 55.0" style="width:100%"/>
      </div>
      <div style="margin-bottom:10px">
        <div class="ss-l" style="margin-bottom:5px">DECIMAL ODDS OFFERED</div>
        <input class="finput" id="ko" type="number" step="0.01"
               placeholder="e.g. 2.10" style="width:100%"/>
      </div>
      <div style="margin-bottom:10px">
        <div class="ss-l" style="margin-bottom:5px">BANK SIZE (₹)</div>
        <input class="finput" id="kb" type="number"
               value="10000" style="width:100%"/>
      </div>
      <button class="run-btn" onclick="runKelly()">
        <i class="fas fa-calculator"></i>&nbsp; CALC KELLY STAKE
      </button>
      <div id="kelly-res" class="cresult" style="display:none"></div>
    </div>

    <!-- Odds Converter -->
    <div class="csec">
      <div class="csec-title">
        <i class="fas fa-arrows-rotate"></i> Odds Converter
      </div>
      <div class="ogrid">
        <div>
          <div class="ss-l" style="margin-bottom:5px">DECIMAL</div>
          <input class="finput" id="od" type="number" step="0.001"
                 placeholder="2.000" oninput="convOdds('d')" style="width:100%"/>
        </div>
        <div>
          <div class="ss-l" style="margin-bottom:5px">FRACTIONAL</div>
          <input class="finput" id="of" type="text"
                 placeholder="1/1" oninput="convOdds('f')" style="width:100%"/>
        </div>
        <div>
          <div class="ss-l" style="margin-bottom:5px">AMERICAN</div>
          <input class="finput" id="oa" type="number"
                 placeholder="+100" oninput="convOdds('a')" style="width:100%"/>
        </div>
        <div>
          <div class="ss-l" style="margin-bottom:5px">IMPLIED %</div>
          <input class="finput" id="oi" type="number" step="0.01"
                 placeholder="50.00" oninput="convOdds('i')" style="width:100%"/>
        </div>
      </div>
    </div>

  </div><!-- /tc-calc -->

  <!-- ── TAB: API KEYS ──────────────────────────────── -->
  <div id="tc-api" class="tc">
    <div class="csec" style="max-width:720px">
      <div class="csec-title">
        <i class="fas fa-key"></i>
        API Key Status — {total_keys} keys / {total_quota} total quota
      </div>
      <table class="atable" style="width:100%">
        <thead>
          <tr>
            <th>#</th>
            <th>Key (masked)</th>
            <th>Remaining</th>
            <th style="width:130px">Quota Bar</th>
          </tr>
        </thead>
        <tbody id="key-tbody"></tbody>
      </table>
    </div>
    <div class="csec" style="max-width:720px">
      <div class="csec-title">
        <i class="fas fa-chart-pie"></i> Run Statistics
      </div>
      <div class="ogrid" style="font-size:12px">
        <div class="ssc" style="min-width:unset">
          <div class="ss-l">Last Sync</div>
          <div style="font-size:12px;font-weight:600;margin-top:3px">{ist_now}</div>
        </div>
        <div class="ssc" style="min-width:unset">
          <div class="ss-l">Events Scanned</div>
          <div class="ss-v" style="color:#4ade80;font-size:15px">{state.get('total_events_scanned', 0)}</div>
        </div>
        <div class="ssc" style="min-width:unset">
          <div class="ss-l">Last Arb Count</div>
          <div class="ss-v" style="font-size:15px">{state.get('last_arb_count', 0)}</div>
        </div>
        <div class="ssc" style="min-width:unset">
          <div class="ss-l">Last EV Count</div>
          <div class="ss-v" style="font-size:15px">{state.get('last_ev_count', 0)}</div>
        </div>
      </div>
    </div>
  </div>

</div><!-- /app -->

<!-- QUICK CALC MODAL -->
<div class="modal-bg" id="qcm">
  <div class="modal">
    <div class="modal-title">
      <span><i class="fas fa-calculator"></i> Quick Arb Calc</span>
      <button class="closex" onclick="closeModal()">
        <i class="fas fa-xmark"></i>
      </button>
    </div>
    <div id="qcm-body"></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     JAVASCRIPT
═══════════════════════════════════════════════════════ -->
<script>
// ── DATA (injected from Python) ──────────────────────
const ARBS   = {arbs_j};
const EVS    = {evs_j};
const BC_RAW = {bc_j};
const KEYS   = {keys_j};
const PH     = "{pass_hash}";

// ── HELPERS ──────────────────────────────────────────
const BS = {{pinnacle:'PIN',bet365:'B365',betway:'BW',draftkings:'DK',
             fanduel:'FD',betmgm:'MGM',unibet:'UNI',stake:'STK',
             marathonbet:'MAR',parimatch:'PAR',betfair:'BF',dafabet:'DAF',
             bovada:'BOV',onexbet:'1XB',bcgame:'BCG'}};
const bs = k => BS[k] || (k||'').toUpperCase().slice(0,4);
const fd = d => {{
  try {{
    return new Date(d).toLocaleString('en-IN',
      {{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});
  }} catch {{ return String(d); }}
}};
const si = s => {{
  s = (s||'').toLowerCase();
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

// ── AUTH ─────────────────────────────────────────────
let currentTheme = 'glass';
if(localStorage.getItem('sauth') === PH) boot();

function unlock() {{
  const h = CryptoJS.SHA256(
    document.getElementById('linput').value
  ).toString();
  if(h === PH) {{
    localStorage.setItem('sauth', PH);
    boot();
  }} else {{
    const inp = document.getElementById('linput');
    const err = document.getElementById('lerr');
    inp.value = '';
    err.style.display = 'block';
    inp.style.borderColor = 'rgba(248,113,113,0.8)';
    setTimeout(() => {{
      inp.style.borderColor = '';
      err.style.display = 'none';
    }}, 2000);
  }}
}}
document.getElementById('linput').addEventListener(
  'keydown', e => {{ if(e.key === 'Enter') unlock(); }}
);
function logout() {{
  localStorage.removeItem('sauth');
  location.reload();
}}
function boot() {{
  document.getElementById('lock').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  init();
}}

// ── THEME SWITCHER ────────────────────────────────────
function setTheme(t, btn) {{
  document.querySelectorAll('.tp').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const app = document.getElementById('app');
  // Fade out, swap class, fade in
  app.style.opacity = '0';
  app.style.transform = 'scale(0.99)';
  setTimeout(() => {{
    app.className = 't-' + t;
    currentTheme = t;
    app.style.opacity = '1';
    app.style.transform = 'scale(1)';
  }}, 180);
}}
document.getElementById('app').style.transition =
  'opacity 0.18s ease, transform 0.18s ease';

// ── TAB ROUTING ───────────────────────────────────────
function swTab(id, btn) {{
  document.querySelectorAll('.tc').forEach(t => t.classList.remove('act'));
  document.querySelectorAll('.tabbtn').forEach(t => t.classList.remove('act'));
  document.getElementById('tc-' + id).classList.add('act');
  if(btn) btn.classList.add('act');
}}

// ── INIT ─────────────────────────────────────────────
function init() {{
  // Populate dropdowns
  const arbSports = [...new Set(ARBS.map(a => a.sport))].sort();
  const evSports  = [...new Set(EVS.map(e => e.sport))].sort();
  const evBooks   = [...new Set(EVS.map(e => e.book_key))].sort();
  const bcSports  = [...new Set(BC_RAW.map(b => b.sport_title || 'Unknown'))].sort();
  const addOpts = (selId, arr) => arr.forEach(v => {{
    const s = document.createElement('option');
    s.value = v; s.textContent = v.replace(/_/g,' ');
    document.getElementById(selId).appendChild(s);
  }});
  addOpts('ev-book', evBooks);
  addOpts('ev-sport', evSports);
  addOpts('bc-sport', bcSports);

  // Stats bar
  document.getElementById('ss-arb').textContent  = ARBS.length;
  document.getElementById('ss-ev').textContent   = EVS.length;
  document.getElementById('ss-bc').textContent   = BC_RAW.length;
  document.getElementById('ss-toparb').textContent =
    ARBS.length ? '+' + ARBS[0].profit_pct + '%' : '—';
  document.getElementById('ss-topev').textContent =
    EVS.length  ? '+' + EVS[0].edge_pct + '%'     : '—';
  document.getElementById('ss-profit').textContent =
    ARBS.length ? '₹' + ARBS[0].profit_amt : '—';

  // Tab badges
  document.getElementById('cnt-arb').textContent = ARBS.length;
  document.getElementById('cnt-ev').textContent  = EVS.length;
  document.getElementById('cnt-bc').textContent  = BC_RAW.length;

  renderArbs(ARBS);
  renderEvs(EVS);
  renderBc(BC_RAW);
  renderKeys();
}}

// ── FILTER FUNCTIONS ──────────────────────────────────
function filterArbs() {{
  const q   = document.getElementById('arb-q').value.toLowerCase();
  const wy  = document.getElementById('arb-ways').value;
  const mk  = document.getElementById('arb-mkt').value;
  const mn  = parseFloat(document.getElementById('arb-min').value) || 0;
  renderArbs(ARBS.filter(a =>
    (!wy || String(a.ways) === wy) &&
    (!mk || a.market === mk) &&
    a.profit_pct >= mn &&
    (!q  || a.match.toLowerCase().includes(q) ||
             a.sport.toLowerCase().includes(q))
  ));
}}
function filterEvs() {{
  const q  = document.getElementById('ev-q').value.toLowerCase();
  const bk = document.getElementById('ev-book').value;
  const sp = document.getElementById('ev-sport').value;
  const mn = parseFloat(document.getElementById('ev-min').value) || 0;
  renderEvs(EVS.filter(v =>
    (!bk || v.book_key === bk) &&
    (!sp || v.sport === sp) &&
    v.edge_pct >= mn &&
    (!q  || v.match.toLowerCase().includes(q) ||
             (v.book_key||'').includes(q))
  ));
}}
function filterBc() {{
  const q  = document.getElementById('bc-q').value.toLowerCase();
  const sp = document.getElementById('bc-sport').value;
  renderBc(BC_RAW.filter(b =>
    (!sp || b.sport_title === sp) &&
    (!q  || (b.home_team + ' ' + b.away_team).toLowerCase().includes(q))
  ));
}}

// ── RENDER ARB CARDS ──────────────────────────────────
function renderArbs(data) {{
  const g = document.getElementById('grid-arb');
  document.getElementById('cnt-arb').textContent = data.length;
  if(!data || !data.length) {{
    g.innerHTML = '<div class="empty-state"><i class="fas fa-magnifying-glass"></i>' +
      'No arbitrage opportunities match your filters.</div>';
    return;
  }}
  g.innerHTML = data.map((a, idx) => {{
    const rows = a.outcomes.map(o =>
      `<tr>
        <td><span class="btag">${{bs(o.book_key)}}</span>
            <span style="opacity:0.75;margin-left:4px">${{o.name}}</span></td>
        <td><span class="oval">${{o.odds}}</span></td>
        <td>
          <span class="stake-x">exact ₹${{o.stake}}</span>
          <span class="stake-m">₹${{o.stake_rounded}}</span>
        </td>
      </tr>`
    ).join('');
    const oa = JSON.stringify(a.outcomes.map(o => o.odds));
    return `
    <div class="card">
      <div class="cstripe"></div>
      <div class="cinner">
        <div class="ch">
          <span class="ctypebadge">
            <i class="fas fa-percent"></i> ${{a.ways}}-WAY · ${{a.market}}
          </span>
          <span class="cprofit">+${{a.profit_pct}}%</span>
        </div>
        <div class="cmatch">
          <i class="fas ${{si(a.sport)}}" style="margin-right:5px"></i>${{a.match}}
        </div>
        <div class="cmeta">
          <span><i class="fas fa-calendar"></i> ${{fd(a.commence)}}</span>
          <span><i class="fas fa-tag"></i> ${{a.sport.replace(/_/g,' ')}}</span>
        </div>
        <table class="ctable">
          <tr>
            <th>Outcome / Book</th>
            <th>Odds</th>
            <th>Stake / ₹1K</th>
          </tr>
          ${{rows}}
        </table>
      </div>
      <div class="cfoot">
        <span class="cfoot-l">
          <i class="fas fa-coins"></i> Profit ₹${{a.profit_amt}} / ₹1000
        </span>
        <button class="calcbtn"
                onclick='openQC(${{oa}}, ${{a.ways}})'>
          <i class="fas fa-calculator"></i> Calc
        </button>
      </div>
    </div>`;
  }}).join('');
}}

// ── RENDER EV CARDS ───────────────────────────────────
function renderEvs(data) {{
  const g = document.getElementById('grid-ev');
  document.getElementById('cnt-ev').textContent = data.length;
  if(!data || !data.length) {{
    g.innerHTML = '<div class="empty-state"><i class="fas fa-chart-line"></i>' +
      'No value bets match your filters.</div>';
    return;
  }}
  g.innerHTML = data.map(v => `
    <div class="card">
      <div class="cstripe"></div>
      <div class="cinner">
        <div class="ch">
          <span class="ctypebadge">
            <i class="fas fa-chart-line"></i> +EV · ${{v.market}}
          </span>
          <span class="cprofit">+${{v.edge_pct}}%</span>
        </div>
        <div class="cmatch">
          <i class="fas ${{si(v.sport)}}" style="margin-right:5px"></i>${{v.match}}
        </div>
        <div class="cmeta">
          <span><i class="fas fa-calendar"></i> ${{fd(v.commence)}}</span>
          <span>${{v.sport.replace(/_/g,' ')}}</span>
        </div>
        <table class="ctable">
          <tr><td class="cfoot-l">Outcome</td>
              <td colspan="2"><strong>${{v.outcome}}</strong></td></tr>
          <tr><td class="cfoot-l">Bookmaker</td>
              <td colspan="2"><span class="btag">${{bs(v.book_key)}}</span>
                  &nbsp;${{v.book}}</td></tr>
          <tr><td class="cfoot-l">Offered Odds</td>
              <td colspan="2"><span class="oval">${{v.offered_odds}}</span></td></tr>
          <tr><td class="cfoot-l">True Odds</td>
              <td colspan="2">${{v.true_odds}}
                  <span class="cfoot-l">&nbsp;(${{v.true_prob_pct}}%)</span></td></tr>
          <tr><td class="cfoot-l">Kelly (30%)</td>
              <td colspan="2">
                <span class="stake-x">exact ₹${{v.kelly_stake}}</span>
                <span class="stake-m">₹${{v.kelly_stake_rounded}}</span>
              </td></tr>
        </table>
      </div>
    </div>
  `).join('');
}}

// ── RENDER BC.GAME CARDS ──────────────────────────────
function renderBc(data) {{
  const g = document.getElementById('grid-bc');
  document.getElementById('cnt-bc').textContent = data.length;
  if(!data || !data.length) {{
    g.innerHTML = '<div class="empty-state"><i class="fas fa-gamepad"></i>' +
      'No BC.Game events available. The endpoint may be temporarily down.</div>';
    return;
  }}
  g.innerHTML = data.slice(0,100).map(b => {{
    const outs = b.bookmakers[0].markets[0].outcomes;
    return `
    <div class="card">
      <div class="cstripe"></div>
      <div class="cinner">
        <div class="ch">
          <span class="ctypebadge">
            <i class="fas fa-gamepad"></i> BC.GAME
          </span>
        </div>
        <div class="cmatch">
          <i class="fas ${{si(b.sport_title)}}" style="margin-right:5px"></i>
          ${{b.home_team}} vs ${{b.away_team}}
        </div>
        <div class="cmeta">
          <span><i class="fas fa-calendar"></i> ${{fd(b.commence_time)}}</span>
          <span>${{b.sport_title}}</span>
        </div>
        <table class="ctable">
          ${{outs.map(o =>
            `<tr><td class="cfoot-l">${{o.name}}</td>
                 <td><span class="oval">${{o.price}}</span></td></tr>`
          ).join('')}}
        </table>
      </div>
    </div>`;
  }}).join('');
}}

// ── RENDER API KEYS TABLE ─────────────────────────────
function renderKeys() {{
  document.getElementById('key-tbody').innerHTML = KEYS.map((k, i) => {{
    const pct = Math.max(0, Math.min(100, (k.remaining / 500) * 100));
    const col = pct > 55 ? '#4ade80' : pct > 18 ? '#fbbf24' : '#f87171';
    return `
    <tr>
      <td style="color:var(--txt3,#888)">#${{i+1}}</td>
      <td style="font-family:'IBM Plex Mono',monospace;opacity:0.8">
        ${{k.key}}
      </td>
      <td style="font-weight:800;color:${{col}}">${{k.remaining}}</td>
      <td>
        <div class="qbar-bg" style="background:rgba(128,128,128,0.15)">
          <div class="qbar-fill"
               style="width:${{pct.toFixed(1)}}%;background:${{col}}"></div>
        </div>
      </td>
    </tr>`;
  }}).join('');
}}

// ── QUICK CALC MODAL ──────────────────────────────────
function openQC(oddsArr, ways) {{
  // Pre-fill full calculator
  if(ways === 2) {{
    document.getElementById('c2o1').value = oddsArr[0] || '';
    document.getElementById('c2o2').value = oddsArr[1] || '';
    swCalc(2, document.getElementById('ct2'));
  }} else {{
    document.getElementById('c3o1').value = oddsArr[0] || '';
    document.getElementById('c3o2').value = oddsArr[1] || '';
    document.getElementById('c3o3').value = oddsArr[2] || '';
    swCalc(3, document.getElementById('ct3'));
  }}

  const stake = 10000;
  const impl  = oddsArr.reduce((s, o) => s + 1/o, 0);
  const pct   = (1/impl - 1) * 100;
  const stakes = oddsArr.map(o => (1/o)/impl*stake);
  const profit = stake * (1/impl - 1);
  const col    = pct > 0 ? '#4ade80' : '#f87171';

  document.getElementById('qcm-body').innerHTML = `
    <div class="cresult">
      ${{oddsArr.map((o, i) =>
        `<div class="crrow">
          <span>Leg ${{i+1}} @ ${{o}}</span>
          <span>₹${{stakes[i].toFixed(2)}}</span>
        </div>`
      ).join('')}}
      <div class="crrow">
        <span>Implied Total</span>
        <span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span>
      </div>
      <div class="crrow">
        <span>${{pct > 0 ? 'PROFIT on ₹10,000' : 'NO ARB — Over-round'}}</span>
        <span style="color:${{col}}">
          ${{pct > 0 ? '+₹'+profit.toFixed(2)+' (+'+pct.toFixed(3)+'%)'
                     : Math.abs(pct).toFixed(3)+'%'}}
        </span>
      </div>
    </div>
    <button class="run-btn" style="margin-top:12px"
            onclick="closeModal();swTab('calc',document.getElementById('tb-calc'))">
      <i class="fas fa-arrow-right"></i>&nbsp; Full Calculator
    </button>`;
  document.getElementById('qcm').classList.add('open');
}}
function closeModal() {{
  document.getElementById('qcm').classList.remove('open');
}}
document.getElementById('qcm').addEventListener('click', e => {{
  if(e.target === e.currentTarget) closeModal();
}});

// ── CALCULATOR ────────────────────────────────────────
let calcWays = 2;
function swCalc(n, btn) {{
  calcWays = n;
  document.querySelectorAll('.ctab').forEach(b => b.classList.remove('act'));
  btn.classList.add('act');
  document.getElementById('c2form').style.display = n===2 ? 'block':'none';
  document.getElementById('c3form').style.display = n===3 ? 'block':'none';
  document.getElementById('calc-res').style.display = 'none';
}}
function runCalc() {{
  let odds = [], stake = 0;
  if(calcWays === 2) {{
    odds  = [+document.getElementById('c2o1').value,
             +document.getElementById('c2o2').value];
    stake = +document.getElementById('c2s').value || 10000;
  }} else {{
    odds  = [+document.getElementById('c3o1').value,
             +document.getElementById('c3o2').value,
             +document.getElementById('c3o3').value];
    stake = +document.getElementById('c3s').value || 10000;
  }}
  if(odds.some(o => !o || o <= 1)) {{
    alert('Please enter valid decimal odds greater than 1');
    return;
  }}
  const impl   = odds.reduce((s, o) => s + 1/o, 0);
  const pct    = (1/impl - 1) * 100;
  const stakes = odds.map(o => (1/o)/impl*stake);
  const profit = stake * (1/impl - 1);
  const col    = pct > 0 ? '#4ade80' : '#f87171';
  const rb     = document.getElementById('calc-res');
  rb.innerHTML = [
    ...odds.map((o, i) =>
      `<div class="crrow">
        <span>Leg ${{i+1}} @ ${{o}}</span>
        <span>₹${{stakes[i].toFixed(2)}}
          <span class="cfoot-l" style="margin-left:5px">
            (₹${{Math.round(stakes[i]/10)*10}} rounded)
          </span>
        </span>
      </div>`),
    `<div class="crrow">
      <span>Total Stake</span><span>₹${{stake.toFixed(2)}}</span>
     </div>`,
    `<div class="crrow">
      <span>Implied Total</span>
      <span style="color:${{col}}">${{(impl*100).toFixed(3)}}%</span>
     </div>`,
    pct > 0
      ? `<div class="crrow">
          <span>✅ PROFIT</span>
          <span style="color:${{col}}">
            +₹${{profit.toFixed(2)}} (+${{pct.toFixed(3)}}%)
          </span>
         </div>`
      : `<div class="crrow">
          <span>❌ NO ARB — Over-round</span>
          <span style="color:${{col}}">${{Math.abs(pct).toFixed(3)}}%</span>
         </div>`
  ].join('');
  rb.style.display = 'block';
}}

// ── KELLY CALCULATOR ──────────────────────────────────
function runKelly() {{
  const p    = parseFloat(document.getElementById('kp').value) / 100;
  const o    = parseFloat(document.getElementById('ko').value);
  const bank = parseFloat(document.getElementById('kb').value) || 10000;
  if(!p || !o || p <= 0 || p >= 1 || o <= 1) {{
    alert('Enter valid probability (1–99%) and decimal odds > 1');
    return;
  }}
  const b  = o - 1;
  const q  = 1 - p;
  const kf = (b*p - q) / b;
  const full  = kf > 0 ? kf * bank : 0;
  const frac  = kf > 0 ? 0.30 * kf * bank : 0;
  const evPct = (p * b - q) * 100;
  const col   = evPct > 0 ? '#4ade80' : '#f87171';
  const rb    = document.getElementById('kelly-res');
  rb.innerHTML = `
    <div class="crrow">
      <span>Expected Value</span>
      <span style="color:${{col}}">
        ${{evPct > 0 ? '+' : ''}}${{evPct.toFixed(2)}}% per bet
      </span>
    </div>
    <div class="crrow">
      <span>Full Kelly Stake</span>
      <span>₹${{full.toFixed(2)}}</span>
    </div>
    <div class="crrow">
      <span>30% Fractional Kelly</span>
      <span style="color:${{col}}">
        ₹${{frac.toFixed(2)}}
        <span class="cfoot-l" style="margin-left:5px">
          (₹${{Math.round(frac/10)*10}} rounded)
        </span>
      </span>
    </div>
    <div class="crrow">
      <span>% of Bankroll at Risk</span>
      <span>${{(frac/bank*100).toFixed(2)}}%</span>
    </div>`;
  rb.style.display = 'block';
}}

// ── ODDS CONVERTER ────────────────────────────────────
let _cv = false;
function convOdds(from) {{
  if(_cv) return;
  _cv = true;
  const setAll = dec => {{
    document.getElementById('od').value = dec.toFixed(3);
    document.getElementById('oa').value = dec >= 2
      ? '+' + Math.round((dec-1)*100)
      : '-' + Math.round(100/(dec-1));
    document.getElementById('oi').value = (100/dec).toFixed(2);
    const [n,d] = d2f(dec);
    document.getElementById('of').value = n + '/' + d;
  }};
  try {{
    if(from === 'd') {{
      const v = +document.getElementById('od').value;
      if(v > 1) setAll(v);
    }} else if(from === 'a') {{
      const a = +document.getElementById('oa').value;
      const v = a > 0 ? a/100+1 : 100/Math.abs(a)+1;
      if(v > 1) setAll(v);
    }} else if(from === 'f') {{
      const p = document.getElementById('of').value.split('/');
      const v = p.length===2 ? +p[0]/+p[1]+1 : 0;
      if(v > 1) setAll(v);
    }} else if(from === 'i') {{
      const i = +document.getElementById('oi').value;
      const v = i > 0 && i < 100 ? 100/i : 0;
      if(v > 1) setAll(v);
    }}
  }} finally {{ _cv = false; }}
}}
function d2f(d) {{
  const t = 1e-5;
  let h1=1, h2=0, k1=0, k2=1, b=d-1;
  for(let i=0; i<40; i++) {{
    const a=Math.floor(b), ah=h1;
    h1=a*h1+h2; h2=ah;
    const ak=k1;
    k1=a*k1+k2; k2=ak;
    if(Math.abs(b-a) < t) break;
    b = 1/(b-a);
  }}
  return [h1, k1];
}}
</script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║         ARB SNIPER v5.0 — Starting Run           ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    log.info(f"State loaded. Current quota: {state['remaining_requests']}")

    # 1. Fetch Odds API data concurrently across all keys
    odds_events = fetch_all_odds(state)

    # 2. Fetch BC.Game + merge
    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)  # preserve raw for dashboard BC tab
    all_events  = merge_bcgame(odds_events, bc_events)

    # 3. Save updated state (quota etc.)
    save_state(state)

    # 4. Quant scanning
    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    # 5. Update counters in state
    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    # 6. Push notification
    send_push(arbs, evs)

    # 7. Generate multi-theme HTML dashboard
    key_status = ROTATOR.status()
    html = generate_html(arbs, evs, raw_bc_copy, state, key_status)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard written: {OUTPUT_HTML} "
             f"({len(html)//1024} KB)")

    # 8. Final summary
    log.info("═" * 52)
    log.info(f"  Arbitrage Opportunities : {len(arbs)}")
    if arbs:
        t = arbs[0]
        log.info(f"  Top Arb : {t['match']} — {t['profit_pct']}% "
                 f"({t['ways']}-way {t['market']})")
    log.info(f"  Value Bets (+EV)        : {len(evs)}")
    if evs:
        t = evs[0]
        log.info(f"  Top EV  : {t['match']} — {t['edge_pct']}% "
                 f"edge @ {t['book']}")
    log.info(f"  BC.Game Events          : {len(raw_bc_copy)}")
    log.info(f"  Total Events Scanned    : {len(all_events)}")
    log.info(f"  API Quota Remaining     : {ROTATOR.total_remaining()}")
    log.info(f"  Active Keys             : {len(ROTATOR.keys)}")
    log.info("═" * 52)
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║              Run Complete ✓                       ║")
    log.info("╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
