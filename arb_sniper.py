#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v9.0 — ZERO-TRUST ANALYTICS                    ║
║  Zero-Trust Auth | Advanced Market Analytics | BC.Game | API Telemetry       ║
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
    subprocess.check_call([sys.executable, "-m", "pip", "install", "cloudscraper", "-q"])

import cloudscraper

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & CONFIGURATIONS
# ─────────────────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"

# ── MONETIZATION & GATEWAY CONFIG ─────────────────────────────────────────────
YOUR_UPI_ID  = "Furiousfighter06-1@okhdfcbank"
YOUR_NAME    = "Furious Fighter"
FIREBASE_URL = "https://payment-engine-e3bff-default-rtdb.asia-southeast1.firebasedatabase.app"
SUB_PRICE    = 500  

# ── ARBITRAGE THRESHOLDS ──────────────────────────────────────────────────────
MIN_ARB_PROFIT = 0.05   
MAX_ARB_PROFIT = 15.0   
MIN_EV_EDGE    = 0.005  
KELLY_FRACTION = 0.30   
DEFAULT_BANK   = 10000  

# ── BC.GAME ENDPOINTS ─────────────────────────────────────────────────────────
BCGAME_BASE    = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en"

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
    "tennis_atp", "tennis_wta", "americanfootball_nfl",
}

# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER (WATERFALL FIX)
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self.keys:
            log.warning("ODDS_API_KEYS env var not set! Add it to GitHub Secrets.")
        self._lock  = threading.Lock()
        self._quota = {k: 500 for k in self.keys}
        self._used  = {k: 0   for k in self.keys}

    def load_memory(self, saved_quotas: dict):
        with self._lock:
            for k in self.keys:
                if k in saved_quotas:
                    self._quota[k] = saved_quotas[k]

    def get(self) -> str:
        with self._lock:
            if not self.keys: return "MISSING_KEY"
            # WATERFALL LOGIC: Stick to one key until it dies
            for k in self.keys:
                if self._quota.get(k, 0) > 2:
                    return k
            return "MISSING_KEY"

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

    def dump_quotas(self) -> dict:
        with self._lock: return self._quota

    def status(self) -> list:
        with self._lock:
            active_key = None
            for k in self.keys:
                if self._quota.get(k, 0) > 2:
                    active_key = k
                    break
            
            return [
                {
                    "key":       f"{k[:4]}...{k[-4:]}",
                    "remaining": self._quota.get(k, 0),
                    "used":      self._used.get(k, 0),
                    "active":    k == active_key
                }
                for k in self.keys
            ]

ROTATOR = KeyRotator()

# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT (MEMORY FIX)
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
        "key_quotas":           {} 
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
    if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 0: return []

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
# BC.GAME SCRAPER (CLOUDSCRAPER FIX)
# ═════════════════════════════════════════════════════════════════════════════
def fetch_bcgame_events() -> list:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://bc.game',
        'Referer': 'https://bc.game/'
    }
    try:
        r = scraper.get(f"{BCGAME_BASE}/0", headers=headers, timeout=20)
        if r.status_code != 200: return []
        manifest = r.json()
        
        top_ids  = manifest.get("top_events_versions",  [])
        rest_ids = manifest.get("rest_events_versions", [])
        all_ids  = top_ids + rest_ids
        if not all_ids: return []

        all_sports = {}; all_tourns = {}; all_events = {}
        for chunk_id in all_ids[:5]: # Limit to 5 chunks for speed
            chunk_res = scraper.get(f"{BCGAME_BASE}/{chunk_id}", headers=headers, timeout=10)
            if chunk_res.status_code != 200: continue
            chunk = chunk_res.json()
            if not chunk: continue
            
            all_sports.update(chunk.get("sports", {}))
            all_tourns.update(chunk.get("tournaments", {}))
            all_events.update(chunk.get("events", {}))

        converted = []
        for ev_id, ev in all_events.items():
            desc = ev.get("desc", {})
            if desc.get("type", "match") not in ("match", "game", ""): continue
            
            comps = desc.get("competitors", [])
            if len(comps) < 2: continue
            home, away = comps[0].get("name",""), comps[1].get("name","")
            
            markets = ev.get("markets", {})
            h2h = []
            if "11" in markets:
                for line_key, sels in markets["11"].items():
                    if line_key not in ("", "0"): continue
                    prices = [float(s.get("k",0)) for s in sels.values() if float(s.get("k",0)) > 1.01]
                    if len(prices) == 2: h2h = [{"name":"Home","price":prices[0]}, {"name":"Away","price":prices[1]}]
                    elif len(prices) == 3: h2h = [{"name":"Home","price":prices[0]}, {"name":"Draw","price":prices[1]}, {"name":"Away","price":prices[2]}]
            
            if not h2h: continue
            
            sport = all_sports.get(str(desc.get("sport", "")), {}).get("name", "Unknown")
            ts = desc.get("scheduled", "")
            try: start = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
            except: start = str(ts)

            converted.append({
                "id": f"bcgame_{ev_id}", "sport_title": sport, "home_team": home, "away_team": away,
                "commence_time": start,
                "bookmakers": [{"key": "bc_game", "title": "BC.Game", "markets": [{"key": "h2h", "outcomes": h2h}]}]
            })
        return converted
    except Exception as e:
        log.error(f"BC.Game fetch failed: {e}")
        return []

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (similarity(bh, ev.get("home_team", "")) + similarity(ba, ev.get("away_team", ""))) / 2
            if s > 0.72 and s > best_score:
                best_score, best_ev = s, ev
        if best_score > 0.72 and best_ev:
            best_ev["bookmakers"].extend(bc_ev["bookmakers"])
        else:
            odds_events.append(bc_ev)
    return odds_events

# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE & SCANNER
# ═════════════════════════════════════════════════════════════════════════════
def remove_vig(outcomes: list) -> dict:
    raw = {o["name"]: 1.0 / float(o["price"]) for o in outcomes if float(o.get("price",0)) > 1.0}
    total = sum(raw.values())
    if total <= 0: return {}
    return {k: v / total for k, v in raw.items()}

def kelly_stake(edge: float, odds: float, bank: float) -> float:
    b = odds - 1.0
    if b <= 0: return 0.0
    p  = 1.0 / (odds / (1.0 + edge))
    kf = (b * p - (1.0 - p)) / b
    return round(KELLY_FRACTION * kf * bank, 2) if kf > 0 else 0.0

def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    impl = sum(1.0 / o for o in odds_list)
    if impl >= 1.0: return [0.0] * len(odds_list)
    return [(1.0 / o) / impl * total for o in odds_list]

def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        match = f"{ev.get('home_team')} vs {ev.get('away_team')}"
        for mkey in MARKETS:
            best = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mkey: continue
                    for o in mkt.get("outcomes", []):
                        raw_name, pt = str(o.get("name", "")), o.get("point")
                        name = f"{raw_name}_{abs(float(pt))}" if pt is not None else raw_name
                        price = float(o.get("price", 0))
                        if price > 1.01 and (name not in best or price > best[name][0]):
                            best[name] = (price, bm.get("title", "?"), bm.get("key", "?"))
            
            names = list(best.keys())
            if len(names) < 2: continue
            
            has_draw = any('draw' in n.lower() for n in names)
            
            if len(names) == 2 and not has_draw:
                p1, p2 = best[names[0]][0], best[names[1]][0]
                impl = (1/p1) + (1/p2)
                if impl < 1.0:
                    pct = (1/impl - 1) * 100
                    if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                        stakes = calc_stakes([p1, p2])
                        arbs.append({
                            "ways": 2, "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                            "match": match, "commence": ev.get("commence_time", ""), "profit_pct": round(pct, 3),
                            "profit_amt": round((1/impl - 1)*1000, 2),
                            "outcomes": [
                                {"name": names[0], "odds": p1, "book_key": best[names[0]][2], "stake": stakes[0]},
                                {"name": names[1], "odds": p2, "book_key": best[names[1]][2], "stake": stakes[1]}
                            ]
                        })
            elif len(names) == 3:
                p1, p2, p3 = best[names[0]][0], best[names[1]][0], best[names[2]][0]
                impl = (1/p1) + (1/p2) + (1/p3)
                if impl < 1.0:
                    pct = (1/impl - 1) * 100
                    if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                        stakes = calc_stakes([p1, p2, p3])
                        arbs.append({
                            "ways": 3, "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                            "match": match, "commence": ev.get("commence_time", ""), "profit_pct": round(pct, 3),
                            "profit_amt": round((1/impl - 1)*1000, 2),
                            "outcomes": [
                                {"name": names[0], "odds": p1, "book_key": best[names[0]][2], "stake": stakes[0]},
                                {"name": names[1], "odds": p2, "book_key": best[names[1]][2], "stake": stakes[1]},
                                {"name": names[2], "odds": p3, "book_key": best[names[2]][2], "stake": stakes[2]}
                            ]
                        })
    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs[:300]

def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        match = f"{ev.get('home_team')} vs {ev.get('away_team')}"
        for mkey in MARKETS:
            pin_out = None
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    pin_out = next((m.get("outcomes", []) for m in bm.get("markets", []) if m.get("key") == mkey), None)
            
            if not pin_out or len(pin_out) < 2: continue
            true_probs = remove_vig(pin_out)
            if not true_probs: continue

            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        price = float(o.get("price", 0))
                        if name in true_probs and price > 1.01:
                            tp = true_probs[name]
                            to = 1.0 / tp
                            edge = (price - to) / to
                            if edge >= MIN_EV_EDGE:
                                ks = kelly_stake(edge, price, DEFAULT_BANK)
                                bets.append({
                                    "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                                    "match": match, "commence": ev.get("commence_time", ""),
                                    "outcome": name, "book_key": bm.get("key", "?"), "book": bm.get("title", "?"),
                                    "offered_odds": round(price, 3), "true_odds": round(to, 3),
                                    "true_prob_pct": round(tp * 100, 2), "edge_pct": round(edge * 100, 3),
                                    "kelly_stake": ks
                                })
    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    return bets[:500]

# ═════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs: return
    if arbs:
        t = arbs[0]
        msg = f"ARB: {t['match']} | +{t['profit_pct']}% | {t['ways']}-way {t['market']} | {len(evs)} EVs"
    else:
        t = evs[0]
        msg = f"EV: {t['match']} | +{t['edge_pct']}% edge | {t['book']} | {len(evs)} total"
    try:
        requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={"Title": "Arb Sniper Alert", "Tags": "zap,moneybag"}, timeout=10)
    except: pass

# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR — V9.0 ZERO-TRUST ANALYTICS
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list, state: dict, key_status: list, sports_count: int) -> str:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M:%S %p IST")
    total_q = sum(k["remaining"] for k in key_status)

    arbs_j  = json.dumps(arbs, ensure_ascii=False)
    evs_j   = json.dumps(evs, ensure_ascii=False)
    bc_j    = json.dumps(raw_bc, ensure_ascii=False)
    keys_j  = json.dumps(key_status, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v9.0 ⚡ Analytics</title>
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
html,body{{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--txt);font-family:var(--sans);}}
::-webkit-scrollbar{{width:3px;height:3px}}
::-webkit-scrollbar-thumb{{background:var(--bg4);border-radius:2px}}

/* ── SECURE ZERO-TRUST PORTAL ──────────────────────────────────── */
#lock{{ position:fixed;inset:0;z-index:9999;background:var(--bg); display:flex;align-items:center;justify-content:center; }}
.lbox{{ width:90%;max-width:360px;background:var(--bg2);border:1px solid var(--border2); border-radius:20px;padding:36px 32px; display:flex;flex-direction:column;align-items:center;gap:18px; box-shadow:0 0 60px rgba(34,211,238,0.06); }}
.lock-title{{font-family:var(--disp);font-size:20px;font-weight:800;letter-spacing:3px;color:var(--txt)}}
.lock-sub{{font-size:10px;color:var(--txt3);letter-spacing:2px;text-transform:uppercase}}
#userIdInput{{ width:100%;padding:13px 16px;font-size:16px;text-align:center;letter-spacing:2px; background:var(--bg3);border:1px solid var(--border2);border-radius:10px; color:var(--txt);font-family:var(--mono);outline:none; }}
#userIdInput:focus{{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(34,211,238,.12)}}
#lbtn{{ width:100%;padding:13px;font-size:12px;font-weight:700;letter-spacing:2px; cursor:pointer;border:none;font-family:var(--disp); background:linear-gradient(135deg,var(--cyan),var(--cyan2));color:#000; border-radius:10px; }}
#lerr{{font-size:11px;color:var(--red);display:none;text-align:center;}}

/* ── APP & TABS ──────────────────────────────────── */
#app{{display:none;width:100%;height:100vh;overflow-y:auto;}}
.topbar{{ position:sticky;top:0;z-index:100;height:52px;padding:0 20px; background:rgba(12,12,14,.9);backdrop-filter:blur(20px); border-bottom:1px solid var(--border); display:flex;align-items:center;justify-content:space-between; }}
.logo{{ font-family:var(--disp);font-size:15px;font-weight:800;letter-spacing:1px; background:linear-gradient(135deg,var(--cyan),var(--purple)); -webkit-background-clip:text;-webkit-text-fill-color:transparent; }}
.tabs{{ background:var(--bg1);border-bottom:1px solid var(--border); display:flex;gap:2px;padding:0 20px;overflow-x:auto;scrollbar-width:none; }}
.tabs::-webkit-scrollbar{{display:none}}
.tab{{ padding:11px 13px;font-size:11px;font-weight:600;cursor:pointer; color:var(--txt3);background:none;border:none;white-space:nowrap; display:flex;align-items:center;gap:6px; border-bottom:2px solid transparent;transition:all .2s; }}
.tab.act{{color:var(--cyan);border-bottom-color:var(--cyan)}}

.tc{{display:none;padding:20px;}}
.tc.act{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}}

/* ── CARDS ────────────────────────────────── */
.card{{ background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px; }}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}}
.cbdg{{ font-size:9px;font-weight:800;letter-spacing:1.5px;padding:3px 7px;border-radius:5px;font-family:var(--mono); background:rgba(34,211,238,.1);color:var(--cyan);border:1px solid rgba(34,211,238,.2); }}
.cpft{{font-size:20px;font-weight:800;color:var(--green);font-family:var(--mono)}}
.cmatch{{font-size:13px;font-weight:600;color:var(--txt);margin-bottom:6px;}}
.ctbl{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:10px;font-family:var(--mono)}}
.ctbl th{{ color:var(--txt3);text-align:left;padding:0 6px 7px;border-bottom:1px solid var(--border); font-size:9px;letter-spacing:1.5px;text-transform:uppercase; }}
.ctbl td{{padding:6px 6px;border-bottom:1px solid rgba(42,42,53,.8)}}

/* ── ANALYTICS UI ────────────────────────── */
.stat-box{{background:linear-gradient(145deg, var(--bg3), var(--bg2)); border:1px solid var(--border); padding:20px; border-radius:12px; text-align:center;}}
.stat-val{{font-size:32px; font-weight:800; font-family:var(--mono); color:var(--green); margin:10px 0;}}
.stat-lbl{{font-size:11px; color:var(--txt2); text-transform:uppercase; letter-spacing:1px;}}
.bar-row{{display:flex; align-items:center; margin-bottom:10px; gap:10px; font-size:11px;}}
.bar-lbl{{width:80px; text-overflow:ellipsis; overflow:hidden; white-space:nowrap;}}
.bar-wrap{{flex:1; height:8px; background:var(--bg4); border-radius:4px; overflow:hidden;}}
.bar-fill{{height:100%; background:var(--cyan);}}
.bar-val{{width:40px; text-align:right; font-family:var(--mono); color:var(--txt);}}
</style>
</head>
<body>

<div id="lock">
  <div class="lbox" id="login-box">
    <div class="lock-title">SNIPER V9.0</div>
    <div class="lock-sub">Zero-Trust Node Connection</div>
    <input id="userIdInput" type="text" placeholder="Enter License ID" autocomplete="off"/>
    <button id="lbtn" onclick="authenticateUser()">CONNECT SECURE NODE</button>
    <div id="lerr"></div>
  </div>

  <div class="lbox" id="pay-box" style="display: none;">
    <div class="lock-title" style="font-size: 16px;">LICENSE REQUIRED</div>
    <div style="width: 100%; background:var(--bg3); padding:15px; border-radius:8px; margin-bottom: 15px;">
        <div style="display:flex;justify-content:space-between;font-size:14px;margin-bottom:10px"><span>License:</span><span>₹{SUB_PRICE}.00</span></div>
        <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--txt3);"><span>Network Fee:</span><span>₹<span id="subFee">0.00</span></span></div>
        <div style="display:flex;justify-content:space-between;font-size:16px;color:var(--green);font-weight:bold;margin-top:10px;border-top:1px dashed var(--border);padding-top:10px"><span>Total:</span><span>₹<span id="subTotal">0.00</span></span></div>
    </div>
    <img id="subQrCode" src="" alt="Payment QR" style="width:180px;height:180px;border-radius:8px;background:#fff;padding:5px;">
    <a id="subDeepLink" href="#" style="display:block;background:var(--cyan);color:#000;padding:12px;width:100%;text-align:center;border-radius:8px;text-decoration:none;font-weight:bold;margin-top:10px;">Pay via UPI App</a>
    <div style="font-size:11px;color:var(--txt3);margin-top:10px;"><i class="fas fa-circle-notch fa-spin"></i> Awaiting Backend Confirmation...</div>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> V9.0 ANALYTICS</div>
    <div style="display:flex;gap:10px;align-items:center;font-size:11px;color:var(--txt3);">
      <span><i class="fas fa-clock"></i> {ist_now}</span>
      <button onclick="logout()" style="background:none;border:none;color:var(--red);cursor:pointer;"><i class="fas fa-right-from-bracket"></i></button>
    </div>
  </div>

  <div style="padding:10px 20px; background:var(--bg1); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:10px;">
    <span style="font-size:11px;color:var(--txt2);text-transform:uppercase;">Bankroll (₹)</span>
    <input type="number" id="bankroll" value="{DEFAULT_BANK}" style="background:var(--bg3);border:1px solid var(--border);color:var(--cyan);padding:5px 10px;border-radius:6px;width:120px;font-family:var(--mono);" oninput="onBank()"/>
  </div>

  <div class="tabs">
    <button class="tab act" onclick="swTab('arb',this)"><i class="fas fa-percent"></i> Arbitrage <span class="tbadge" id="c-arb">0</span></button>
    <button class="tab" onclick="swTab('ev',this)"><i class="fas fa-chart-line"></i> +EV Bets <span class="tbadge" id="c-ev">0</span></button>
    <button class="tab" onclick="swTab('ana',this)"><i class="fas fa-chart-pie"></i> Analytics</button>
    <button class="tab" onclick="swTab('bc',this)"><i class="fas fa-gamepad"></i> BC.Game</button>
    <button class="tab" onclick="swTab('api',this)"><i class="fas fa-server"></i> API Status</button>
  </div>

  <div id="tc-arb" class="tc act"><div class="grid" id="g-arb"></div></div>

  <div id="tc-ev" class="tc"><div class="grid" id="g-ev"></div></div>

  <div id="tc-ana" class="tc">
    <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:15px; margin-bottom:20px;">
      <div class="stat-box"><div class="stat-lbl">Total Arb Profit Available</div><div class="stat-val" id="ana-tot-profit">₹0</div><div style="font-size:10px;color:var(--txt3)">Assuming ₹10k staked per arb</div></div>
      <div class="stat-box"><div class="stat-lbl">Market Avg EV Edge</div><div class="stat-val" id="ana-avg-edge" style="color:var(--purple)">0%</div></div>
      <div class="stat-box"><div class="stat-lbl">Most Profitable Sport</div><div class="stat-val" id="ana-top-sport" style="color:var(--cyan);font-size:22px;">—</div></div>
    </div>
    
    <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:15px;">
        <div class="card">
            <h3 style="font-size:14px;margin-bottom:15px;"><i class="fas fa-building"></i> Top Bookmakers (+EV Count)</h3>
            <div id="ana-books"></div>
        </div>
        <div class="card">
            <h3 style="font-size:14px;margin-bottom:15px;"><i class="fas fa-futbol"></i> Opportunities by Sport</h3>
            <div id="ana-sports"></div>
        </div>
    </div>
  </div>

  <div id="tc-bc" class="tc"><div class="grid" id="g-bc"></div></div>

  <div id="tc-api" class="tc">
    <div class="card" style="max-width:600px;">
      <h3 style="margin-bottom:15px;font-size:14px;"><i class="fas fa-key"></i> System Telemetry</h3>
      <table class="ctbl">
        <thead><tr><th>#</th><th>Key</th><th>Remaining</th><th>Used</th></tr></thead>
        <tbody id="ktbody"></tbody>
      </table>
    </div>
  </div>

</div>

<script>
const ARBS={arbs_j}; const EVS={evs_j}; const BC={bc_j}; const KEYS={keys_j};
let BANK=parseFloat(localStorage.getItem('arb_bank'))||{DEFAULT_BANK};

// ── ZERO-TRUST AUTHENTICATION ──
const FIREBASE_URL = "{FIREBASE_URL}";
const SUB_PRICE = {SUB_PRICE};
let currentUser = localStorage.getItem('arb_session') || "";
let pollInterval;
let safeAmountKey = "";

if(currentUser) verifySubscription(false);

async function authenticateUser() {{
    const input = document.getElementById('userIdInput').value.trim().toLowerCase();
    if(!input) return;
    currentUser = input;
    localStorage.setItem('arb_session', currentUser);
    verifySubscription(true);
}}

async function verifySubscription(showLoading) {{
    const errDiv = document.getElementById('lerr');
    if(showLoading) document.getElementById('lbtn').innerHTML = '<i class="fas fa-spinner fa-spin"></i> VERIFYING...';
    
    try {{
        const res = await fetch(`${{FIREBASE_URL}}/users/${{currentUser}}.json`, {{ cache: "no-store" }});
        const data = await res.json();
        const now = new Date().getTime();

        if (data && data.sub_expiry && data.sub_expiry > now) {{
            document.getElementById('lock').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            initApp(); 
        }} else {{
            triggerPaymentGateway();
        }}
    }} catch (e) {{
        errDiv.innerText = "Network Error."; errDiv.style.display = 'block';
        document.getElementById('lbtn').innerHTML = 'CONNECT SECURE NODE';
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

    const upiLink = `upi://pay?pa=${{'{YOUR_UPI_ID}'}}&pn=${{encodeURIComponent('{YOUR_NAME}')}}&am=${{targetAmountStr}}&cu=INR`;
    document.getElementById("subQrCode").src = `https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=${{encodeURIComponent(upiLink)}}`;
    document.getElementById("subDeepLink").href = upiLink;

    pollInterval = setInterval(async () => {{
        try {{
            const res = await fetch(`${{FIREBASE_URL}}/users/${{currentUser}}.json`, {{ cache: "no-store" }});
            const data = await res.json();
            if (data && data.sub_expiry && data.sub_expiry > new Date().getTime()) {{
                clearInterval(pollInterval);
                document.getElementById('lock').style.display = 'none';
                document.getElementById('app').style.display = 'block';
                initApp();
            }}
        }} catch(e){{}}
    }}, 3000);
}}

function logout() {{ localStorage.removeItem('arb_session'); location.reload(); }}
function swTab(id,btn){{ document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act')); document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act')); document.getElementById('tc-'+id).classList.add('act'); btn.classList.add('act'); }}
function onBank(){{ BANK=parseFloat(document.getElementById('bankroll').value)||10000; localStorage.setItem('arb_bank',String(BANK)); initApp(); }}

function initApp(){{
    document.getElementById('c-arb').textContent=ARBS.length;
    document.getElementById('c-ev').textContent=EVS.length;
    renderArbs(); renderEVs(); renderBC(); renderAnalytics(); renderAPI();
}}

function renderArbs(){{
    const g=document.getElementById('g-arb');
    if(!ARBS.length) {{ g.innerHTML='<div style="color:var(--txt3);padding:20px;">No Arbs Found</div>'; return; }}
    g.innerHTML = ARBS.map(a=>{{
        const profit = ((a.profit_pct/100)*BANK).toFixed(2);
        const rows = a.outcomes.map(o=>`<tr><td>${{o.book_key.toUpperCase().slice(0,4)}} ${{(o.name).slice(0,10)}}</td><td style="color:var(--yellow);font-weight:bold;">${{o.odds}}</td><td style="text-align:right">₹${{Math.round((o.stake/1000)*BANK)}}</td></tr>`).join('');
        return `<div class="card"><div class="ch"><span class="cbdg">${{a.ways}}-WAY ARB</span><span class="cpft">+${{a.profit_pct}}%</span></div><div class="cmatch">${{a.match}}</div><div style="font-size:10px;color:var(--txt3);margin-bottom:10px;">${{a.sport}}</div><table class="ctbl">${{rows}}</table><div style="font-size:11px;color:var(--txt2);margin-top:10px;text-align:right;">Est. Profit: <span style="color:var(--green)">₹${{profit}}</span></div></div>`;
    }}).join('');
}}

function renderEVs(){{
    const g=document.getElementById('g-ev');
    if(!EVS.length) {{ g.innerHTML='<div style="color:var(--txt3);padding:20px;">No EVs Found</div>'; return; }}
    g.innerHTML = EVS.map(e=>{{
        const kStake = Math.round(e.kelly_stake/10000*BANK);
        return `<div class="card" style="border-top:3px solid var(--purple)"><div class="ch"><span class="cbdg" style="color:var(--purple);background:rgba(167,139,250,0.1)">+EV BET</span><span class="cpft" style="color:var(--purple)">+${{e.edge_pct}}%</span></div><div class="cmatch">${{e.match}}</div><div style="font-size:10px;color:var(--txt3);margin-bottom:10px;">${{e.sport}}</div><table class="ctbl"><tr><td>Book</td><td>${{e.book_key.toUpperCase()}}</td></tr><tr><td>Outcome</td><td>${{e.outcome}}</td></tr><tr><td>Odds</td><td style="color:var(--yellow)">${{e.offered_odds}}</td></tr><tr><td>True</td><td>${{e.true_odds}}</td></tr></table><div style="font-size:11px;color:var(--txt2);margin-top:10px;text-align:right;">Kelly Stake: <span style="color:var(--cyan)">₹${{kStake}}</span></div></div>`;
    }}).join('');
}}

function renderBC(){{
    const g=document.getElementById('g-bc');
    if(!BC.length) {{ g.innerHTML='<div style="color:var(--txt3);padding:20px;">No BC Data</div>'; return; }}
    g.innerHTML = BC.slice(0,50).map(b=>`<div class="card"><div class="ch"><span class="cbdg" style="color:var(--yellow);background:rgba(251,191,36,0.1)">BC.GAME</span></div><div class="cmatch">${{b.home_team}} vs ${{b.away_team}}</div><div style="font-size:10px;color:var(--txt3);margin-bottom:10px;">${{b.sport_title}}</div><table class="ctbl">${{b.bookmakers[0].markets[0].outcomes.map(o=>`<tr><td>${{o.name}}</td><td style="color:var(--yellow);text-align:right;">${{o.price}}</td></tr>`).join('')}}</table></div>`).join('');
}}

function renderAPI(){{
    document.getElementById('ktbody').innerHTML=KEYS.map((k,i)=>`<tr><td>${{i+1}}</td><td style="color:var(--cyan);font-family:monospace">${{k.key}}</td><td style="color:${{k.remaining>100?'var(--green)':'var(--red)'}}">${{k.remaining}}</td><td>${{k.used}}</td><td>${{k.active ? '🟢' : '⚫'}}</td></tr>`).join('');
}}

// ── HIGH-END ANALYTICS ENGINE ──
function renderAnalytics(){{
    let totP = 0;
    ARBS.forEach(a => totP += ((a.profit_pct/100) * 10000)); 
    document.getElementById('ana-tot-profit').innerText = `₹${{totP.toFixed(0)}}`;

    let avgE = 0;
    if(EVS.length) {{
        let s = 0; EVS.forEach(e => s += e.edge_pct);
        avgE = s/EVS.length;
    }}
    document.getElementById('ana-avg-edge').innerText = `+${{avgE.toFixed(2)}}%`;

    let bCounts = {{}};
    EVS.forEach(e => bCounts[e.book_key] = (bCounts[e.book_key]||0)+1);
    let bArr = Object.entries(bCounts).sort((a,b)=>b[1]-a[1]).slice(0,5);
    let maxB = bArr.length ? bArr[0][1] : 1;
    document.getElementById('ana-books').innerHTML = bArr.map(b=>`
        <div class="bar-row">
            <div class="bar-lbl">${{b[0].toUpperCase()}}</div>
            <div class="bar-wrap"><div class="bar-fill" style="width:${{(b[1]/maxB)*100}}%"></div></div>
            <div class="bar-val">${{b[1]}}</div>
        </div>
    `).join('');

    let sCounts = {{}};
    [...ARBS, ...EVS].forEach(x => sCounts[x.sport] = (sCounts[x.sport]||0)+1);
    let sArr = Object.entries(sCounts).sort((a,b)=>b[1]-a[1]);
    if(sArr.length) document.getElementById('ana-top-sport').innerText = sArr[0][0].replace(/_/g,' ').substring(0,15);
    
    let maxS = sArr.length ? sArr[0][1] : 1;
    document.getElementById('ana-sports').innerHTML = sArr.slice(0,5).map(s=>`
        <div class="bar-row">
            <div class="bar-lbl">${{s[0].replace('soccer_','').replace('basketball_','')}}</div>
            <div class="bar-wrap"><div class="bar-fill" style="background:var(--purple);width:${{(s[1]/maxS)*100}}%"></div></div>
            <div class="bar-val">${{s[1]}}</div>
        </div>
    `).join('');
}}
</script>
</body>
</html>"""
    return html

# ═════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║         ARB SNIPER v9.0 — ZERO-TRUST NODE        ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    ROTATOR.load_memory(state.get("key_quotas", {}))

    sports_list = fetch_all_sports()
    odds_events = fetch_all_odds(state, sports_list)

    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events  = merge_bcgame(odds_events, bc_events)

    state["key_quotas"] = ROTATOR.dump_quotas()
    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    send_push(arbs, evs)

    key_status = ROTATOR.status()
    html = generate_html(arbs, evs, raw_bc_copy, state, key_status, len(sports_list))
    
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
        
    log.info(f"Dashboard generated: {OUTPUT_HTML} ({len(html) // 1024} KB)")

if __name__ == "__main__":
    main()
