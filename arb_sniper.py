#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                   ARB SNIPER v12.0 — HYBRID AI ENGINE                        ║
║  Zero-Trust Auth | Odds API Foundation | Tinyfish AI BC.Game Bypass          ║
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
import itertools
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

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

# 🤖 TINYFISH AI CONFIG
# Using your key for the GitHub Action. Keep this private!
TINYFISH_KEY   = os.environ.get("TINYFISH_API_KEY", "sk-tinyfish-L-BGAbpbQxcS3BYbQ1ECsjLAI0B_6k1Y")

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

ALLOWED_BOOKS = {
    "pinnacle", "stake", "onexbet", "parimatch", 
    "dafabet", "betway", "bet365", "marathonbet", "betfair", "matchbook",
}

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"

ALWAYS_INCLUDE_SPORTS = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "basketball_nba", "tennis_atp", "cricket_ipl"
}

# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER (THE ODDS API)
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
                {"key": f"{k[:4]}...{k[-4:]}", "remaining": self._quota.get(k, 0), "used": self._used.get(k, 0), "active": k == active_key}
                for k in self.keys
            ]

ROTATOR = KeyRotator()

# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {
        "remaining_requests": 500, "used_today": 0,
        "last_reset": str(datetime.now(timezone.utc).date()),
        "total_events_scanned": 0, "last_arb_count": 0, "last_ev_count": 0,
        "sports_scanned": 0, "key_quotas": {} 
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
# THE ODDS API (FREE TIER FOUNDATION)
# ═════════════════════════════════════════════════════════════════════════════
def fetch_all_sports() -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY": return sorted(ALWAYS_INCLUDE_SPORTS)
    try:
        r = requests.get(f"{ODDS_BASE}/sports", params={"apiKey": key, "all": "false"}, timeout=15)
        if r.status_code == 200:
            return [s["key"] for s in r.json() if not s.get("has_outrights")]
    except Exception: pass
    return sorted(ALWAYS_INCLUDE_SPORTS)

def _fetch_market(market: str) -> list:
    while True:
        key = ROTATOR.get()
        if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 0: return []
        url = f"{ODDS_BASE}/sports/upcoming/odds"
        params = {"apiKey": key, "regions": REGIONS, "markets": market, "oddsFormat": "decimal"}
        try:
            r = requests.get(url, params=params, timeout=30)
            ROTATOR.update(key, int(r.headers.get("X-Requests-Remaining", ROTATOR._quota.get(key, 0))), int(r.headers.get("X-Requests-Used", 0)))
            if r.status_code in (429, 401):
                ROTATOR.mark_exhausted(key)
                continue 
            if r.status_code != 200: return []
            
            data = r.json()
            filtered = []
            for ev in data:
                bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
                if bms:
                    ev["bookmakers"] = bms
                    filtered.append(ev)
            return filtered
        except Exception: return []

def fetch_all_odds(state: dict, sports_list: list) -> list:
    if not ROTATOR.keys: return []
    if ROTATOR.total_remaining() <= 0: return []
    events_by_id = {}
    for market in MARKETS:
        for ev in _fetch_market(market):
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
    state["remaining_requests"] = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    state["sports_scanned"] = len({ev.get("sport_key","") for ev in all_events})
    return all_events

# ═════════════════════════════════════════════════════════════════════════════
# 🤖 TINYFISH AI AGENT (THE 1-CREDIT BC.GAME LOCKPICK)
# ═════════════════════════════════════════════════════════════════════════════
def fetch_bcgame_via_ai() -> list:
    log.info("🤖 Dispatching Tinyfish AI to BC.Game (Cost: 1 Credit)...")
    url = "http" + "s://bc.game/sports/soccer"
    api_url = "http" + "s://agent.tinyfish.ai/v1/automation/run-sse"
    
    goal = (
        "Look at the upcoming soccer matches on this page. Extract the Match Winner (1x2) odds. "
        "Return ONLY a raw JSON array. DO NOT include markdown formatting like ```json. "
        "Format exactly like this: [{\"home_team\": \"Team A\", \"away_team\": \"Team B\", "
        "\"home_odds\": 2.10, \"draw_odds\": 3.20, \"away_odds\": 3.50}]"
    )

    try:
        with requests.post(
            api_url, 
            headers={'X-API-Key': TINYFISH_KEY, 'Content-Type': 'application/json'},
            json={'url': url, 'goal': goal}, stream=True, timeout=180
        ) as response:
            if response.status_code != 200:
                log.error(f"Tinyfish API Error: {response.status_code}")
                return []

            for line in response.iter_lines():
                if line:
                    decoded = line.decode('utf-8')
                    if decoded.startswith('data: '):
                        try:
                            payload = json.loads(decoded[6:])
                            if 'result' in payload:
                                raw_result = payload['result']
                                if isinstance(raw_result, str):
                                    cleaned = raw_result.replace("```json", "").replace("```", "").strip()
                                    parsed_data = json.loads(cleaned)
                                else:
                                    parsed_data = raw_result
                                
                                if isinstance(parsed_data, dict):
                                    for v in parsed_data.values():
                                        if isinstance(v, list): parsed_data = v; break
                                    else: parsed_data = [parsed_data]
                                
                                log.info(f"✅ AI successfully extracted {len(parsed_data)} matches from BC.Game!")
                                
                                # ── TRANSLATE AI JSON TO QUANT ENGINE FORMAT ──
                                standard_events = []
                                for match in parsed_data:
                                    home = match.get('home_team')
                                    away = match.get('away_team')
                                    if not home or not away: continue

                                    outcomes = []
                                    if match.get('home_odds'): outcomes.append({"name": "Home", "price": float(match['home_odds'])})
                                    if match.get('draw_odds'): outcomes.append({"name": "Draw", "price": float(match['draw_odds'])})
                                    if match.get('away_odds'): outcomes.append({"name": "Away", "price": float(match['away_odds'])})

                                    if len(outcomes) >= 2:
                                        standard_events.append({
                                            "id": f"bcgame_ai_{hash(home+away)}",
                                            "sport_title": "Soccer",
                                            "home_team": home,
                                            "away_team": away,
                                            "commence_time": str(datetime.now(timezone.utc).isoformat()), # Appx
                                            "bookmakers": [{
                                                "key": "bc_game", "title": "BC.Game",
                                                "markets": [{"key": "h2h", "outcomes": outcomes}]
                                            }]
                                        })
                                return standard_events
                        except Exception: pass
    except Exception as e:
        log.error(f"Tinyfish failed: {e}")
    return []

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    merged = 0
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (similarity(bh, ev.get("home_team", "")) + similarity(ba, ev.get("away_team", ""))) / 2
            if s > 0.72 and s > best_score:
                best_score, best_ev = s, ev
        if best_score > 0.72 and best_ev:
            best_ev["bookmakers"].extend(bc_ev["bookmakers"])
            merged += 1
        else:
            odds_events.append(bc_ev)
    log.info(f"AI Merge: {merged} matches locked to Odds API. {len(bc_events) - merged} standalone.")
    return odds_events

# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE
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
            
            outcomes_list = list(best.items())
            if len(outcomes_list) < 2: continue
            
            if mkey == "h2h":
                has_draw = any('draw' in n[0].lower() for n in outcomes_list)
                if len(outcomes_list) == 2 and not has_draw: combos = list(itertools.combinations(outcomes_list, 2))
                elif len(outcomes_list) == 3: combos = list(itertools.combinations(outcomes_list, 3))
                else: combos = []
            elif mkey in ["totals", "spreads"]:
                points = {}
                for item in outcomes_list:
                    parts = item[0].split("_")
                    if len(parts) >= 2:
                        points.setdefault("_".join(parts[1:]), []).append(item)
                combos = [c for c in points.values() if len(c) == 2]
            else: combos = []

            for combo in combos:
                prices = [c[1][0] for c in combo]
                impl = sum(1.0/p for p in prices)
                if impl < 1.0:
                    pct = (1/impl - 1) * 100
                    if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                        stakes = calc_stakes(prices)
                        arbs.append({
                            "ways": len(combo), "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                            "match": match, "commence": ev.get("commence_time", ""), "profit_pct": round(pct, 3),
                            "profit_amt": round((1/impl - 1)*1000, 2),
                            "outcomes": [{"name": c[0], "odds": c[1][0], "book_key": c[1][2], "stake": s} for c, s in zip(combo, stakes)]
                        })
    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs[:300]

def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        match = f"{ev.get('home_team')} vs {ev.get('away_team')}"
        for mkey in MARKETS:
            pin_out = next((m.get("outcomes", []) for bm in ev.get("bookmakers", []) if bm.get("key") == "pinnacle" for m in bm.get("markets", []) if m.get("key") == mkey), None)
            if not pin_out or len(pin_out) < 2: continue
            
            true_probs = remove_vig(pin_out)
            if not true_probs: continue

            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name, price = str(o.get("name", "")), float(o.get("price", 0))
                        name = f"{name}_{abs(float(o.get('point')))}" if o.get("point") is not None else name
                        
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
                                    "true_prob_pct": round(tp * 100, 2), "edge_pct": round(edge * 100, 3), "kelly_stake": ks
                                })
    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    return bets[:500]

# ═════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs: return
    msg = f"ARB: {arbs[0]['match']} | +{arbs[0]['profit_pct']}% | {arbs[0]['ways']}-way" if arbs else f"EV: {evs[0]['match']} | +{evs[0]['edge_pct']}% @ {evs[0]['book']}"
    try: requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={"Title": "Arb Sniper Alert", "Tags": "zap"}, timeout=10)
    except: pass

# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR — V12.0
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list, state: dict, key_status: list) -> str:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M:%S %p IST")
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v12.0 ⚡ Hybrid AI</title>
<link rel="stylesheet" href="[https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css](https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css)"/>
<link href="[https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@700;800&display=swap](https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@700;800&display=swap)" rel="stylesheet"/>
<script src="[https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js](https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js)"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
:root{{ --bg:#0c0c0e;--bg1:#111115;--bg2:#17171d;--bg3:#1e1e26;--bg4:#26262f; --border:#2a2a35;--cyan:#22d3ee;--green:#4ade80;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa; --txt:#e8e8f0;--txt2:#9898aa;--txt3:#5a5a6a; --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;--disp:'Syne',sans-serif; }}
html,body{{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--txt);font-family:var(--sans);}}
::-webkit-scrollbar{{width:3px;height:3px}}::-webkit-scrollbar-thumb{{background:var(--bg4);border-radius:2px}}

#lock{{ position:fixed;inset:0;z-index:9999;background:var(--bg); display:flex;align-items:center;justify-content:center; }}
.lbox{{ width:90%;max-width:360px;background:var(--bg2);border:1px solid var(--border); border-radius:20px;padding:36px 32px; display:flex;flex-direction:column;align-items:center;gap:18px; }}
#userIdInput{{ width:100%;padding:13px 16px;font-size:16px;text-align:center; background:var(--bg3);border:1px solid var(--border);border-radius:10px; color:var(--txt);outline:none; }}
#lbtn{{ width:100%;padding:13px;font-weight:700; cursor:pointer;border:none; background:var(--cyan);color:#000; border-radius:10px; }}

#app{{display:none;width:100%;height:100vh;overflow-y:auto;}}
.topbar{{ position:sticky;top:0;z-index:100;padding:15px 20px; background:var(--bg1); border-bottom:1px solid var(--border); display:flex;justify-content:space-between; }}
.logo{{ font-family:var(--disp);font-size:15px;font-weight:800; color:var(--cyan); }}
.tabs{{ background:var(--bg1);border-bottom:1px solid var(--border); display:flex;gap:2px;padding:0 20px;overflow-x:auto; }}
.tab{{ padding:11px 13px;font-size:11px;font-weight:600;cursor:pointer; color:var(--txt3);background:none;border:none; border-bottom:2px solid transparent; }}
.tab.act{{color:var(--cyan);border-bottom-color:var(--cyan)}}

.tc{{display:none;padding:20px;}} .tc.act{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}}
.card{{ background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px; }}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}}
.cbdg{{ font-size:9px;font-weight:800;padding:3px 7px;border-radius:5px; background:rgba(34,211,238,.1);color:var(--cyan); }}
.cpft{{font-size:20px;font-weight:800;color:var(--green);}}
.cmatch{{font-size:13px;font-weight:600;margin-bottom:6px;}}
.ctbl{{width:100%;border-collapse:collapse;font-size:11px;}}
.ctbl td{{padding:6px;border-bottom:1px solid rgba(42,42,53,.8)}}
</style>
</head>
<body>

<div id="lock">
  <div class="lbox" id="login-box">
    <div style="font-family:'Syne',sans-serif;font-size:20px;font-weight:800;color:var(--txt)">SNIPER V12.0</div>
    <div style="font-size:10px;color:var(--txt3)">Hybrid AI Node Connection</div>
    <input id="userIdInput" type="text" placeholder="Enter License ID"/>
    <button id="lbtn" onclick="authenticateUser()">CONNECT SECURE NODE</button>
    <div id="lerr" style="color:var(--red);display:none;font-size:11px;">Network Error</div>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> V12.0 HYBRID AI</div>
    <div style="font-size:11px;color:var(--txt3);">{ist_now}</div>
  </div>

  <div style="padding:10px 20px; background:var(--bg1); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:10px;">
    <span style="font-size:11px;color:var(--txt2);">Bankroll (₹)</span>
    <input type="number" id="bankroll" value="{DEFAULT_BANK}" style="background:var(--bg3);border:1px solid var(--border);color:var(--cyan);padding:5px 10px;border-radius:6px;width:120px;" oninput="onBank()"/>
  </div>

  <div class="tabs">
    <button class="tab act" onclick="swTab('arb',this)">Arbitrage</button>
    <button class="tab" onclick="swTab('ev',this)">+EV Bets</button>
    <button class="tab" onclick="swTab('bc',this)">AI Extractions</button>
  </div>

  <div id="tc-arb" class="tc act"><div class="grid" id="g-arb"></div></div>
  <div id="tc-ev" class="tc"><div class="grid" id="g-ev"></div></div>
  <div id="tc-bc" class="tc"><div class="grid" id="g-bc"></div></div>
</div>

<script>
const ARBS={json.dumps(arbs)}; const EVS={json.dumps(evs)}; const BC={json.dumps(raw_bc)};
let BANK=parseFloat(localStorage.getItem('arb_bank'))||{DEFAULT_BANK};

let currentUser = localStorage.getItem('arb_session') || "";
if(currentUser) verifySubscription(false);

async function authenticateUser() {{
    currentUser = document.getElementById('userIdInput').value.trim().toLowerCase();
    if(!currentUser) return;
    localStorage.setItem('arb_session', currentUser);
    verifySubscription(true);
}}

async function verifySubscription(showLoading) {{
    if(showLoading) document.getElementById('lbtn').innerHTML = 'VERIFYING...';
    try {{
        const res = await fetch(`{FIREBASE_URL}/users/${{currentUser}}.json`);
        const data = await res.json();
        if (data && data.sub_expiry && data.sub_expiry > new Date().getTime()) {{
            document.getElementById('lock').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            initApp(); 
        }} else {{ alert("License expired. Contact Admin."); }}
    }} catch (e) {{ document.getElementById('lerr').style.display = 'block'; }}
}}

function swTab(id,btn){{ document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act')); document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act')); document.getElementById('tc-'+id).classList.add('act'); btn.classList.add('act'); }}
function onBank(){{ BANK=parseFloat(document.getElementById('bankroll').value)||10000; localStorage.setItem('arb_bank',String(BANK)); initApp(); }}

function initApp(){{ renderArbs(); renderEVs(); renderBC(); }}

function renderArbs(){{
    const g=document.getElementById('g-arb');
    if(!ARBS.length) return g.innerHTML='<div style="color:var(--txt3);padding:20px;">No Arbs Found</div>';
    g.innerHTML = ARBS.map(a=>{{
        const profit = ((a.profit_pct/100)*BANK).toFixed(2);
        const rows = a.outcomes.map(o=>`<tr><td>${{o.book_key.toUpperCase().slice(0,6)}}</td><td style="color:var(--yellow)">${{o.odds}}</td><td style="text-align:right">₹${{Math.round((o.stake/1000)*BANK/10)*10}}</td></tr>`).join('');
        return `<div class="card"><div class="ch"><span class="cbdg">${{a.ways}}-WAY</span><span class="cpft">+${{a.profit_pct}}%</span></div><div class="cmatch">${{a.match}}</div><table class="ctbl">${{rows}}</table><div style="font-size:11px;color:var(--txt2);margin-top:10px;">Est. Profit: <span style="color:var(--green)">₹${{profit}}</span></div></div>`;
    }}).join('');
}}

function renderEVs(){{
    const g=document.getElementById('g-ev');
    if(!EVS.length) return g.innerHTML='<div style="color:var(--txt3);padding:20px;">No EVs Found</div>';
    g.innerHTML = EVS.map(e=>{{
        const kStake = Math.round(e.kelly_stake/10000*BANK/10)*10;
        return `<div class="card"><div class="ch"><span class="cbdg" style="color:var(--purple)">+EV BET</span><span class="cpft" style="color:var(--purple)">+${{e.edge_pct}}%</span></div><div class="cmatch">${{e.match}}</div><table class="ctbl"><tr><td>Book</td><td>${{e.book_key.toUpperCase()}}</td></tr><tr><td>Odds</td><td style="color:var(--yellow)">${{e.offered_odds}}</td></tr></table><div style="font-size:11px;color:var(--txt2);margin-top:10px;">Kelly Stake: <span style="color:var(--cyan)">₹${{kStake}}</span></div></div>`;
    }}).join('');
}}

function renderBC(){{
    const g=document.getElementById('g-bc');
    if(!BC.length) return g.innerHTML='<div style="color:var(--txt3);padding:20px;">No AI Data</div>';
    g.innerHTML = BC.slice(0,50).map(b=>`<div class="card"><div class="ch"><span class="cbdg" style="color:var(--yellow)">AI EXTRACTED</span></div><div class="cmatch">${{b.home_team}} vs ${{b.away_team}}</div><table class="ctbl">${{b.bookmakers[0].markets[0].outcomes.map(o=>`<tr><td>${{o.name}}</td><td style="color:var(--yellow);text-align:right;">${{o.price}}</td></tr>`).join('')}}</table></div>`).join('');
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
    log.info("║         ARB SNIPER v12.0 — HYBRID ENGINE         ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    ROTATOR.load_memory(state.get("key_quotas", {}))

    # 1. Fetch 60+ sports from Odds API (FREE)
    sports_list = fetch_all_sports()
    odds_events = fetch_all_odds(state, sports_list)

    # 2. Use AI specifically as a lockpick for BC.Game (Cost: 1 Credit)
    ai_events = fetch_bcgame_via_ai()
    raw_ai_copy = list(ai_events)
    
    # 3. Merge Free Data + AI Data
    all_events = merge_bcgame(odds_events, ai_events)

    state["key_quotas"] = ROTATOR.dump_quotas()
    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    send_push(arbs, evs)

    html = generate_html(arbs, evs, raw_ai_copy, state, ROTATOR.status())
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Dashboard generated successfully.")

if __name__ == "__main__":
    main()
