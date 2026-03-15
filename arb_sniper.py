#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║               ARB SNIPER v6.0 — ELITE QUANT ENGINE (SINGLE UI)               ║
║  Strict Point Matching | GSAP Dark UI | Dynamic JS Bankroll | Anti-Palp      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, time, hashlib, requests, logging, itertools, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

ODDS_BASE      = "https://api.the-odds-api.com/v4"
BCGAME_URL     = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"

# ANTI-PALP SETTINGS: Filter out obvious bookie mistakes
MIN_ARB_PROFIT = 0.001   # 0.1%
MAX_ARB_PROFIT = 0.15    # 15.0% (Anything higher is usually a fake/voidable error)
MIN_EV_EDGE    = 0.005   # 0.5%
MAX_EV_EDGE    = 0.30    # 30.0%

ALLOWED_BOOKS = {
    "pinnacle", "onexbet", "bet365", "unibet", "betway", "stake",
    "marathonbet", "parimatch", "betfair", "dafabet", "bovada",
    "draftkings", "fanduel", "betmgm"
}

SPORTS_LIST = [
    "upcoming", 
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "basketball_nba", "basketball_euroleague", "icehockey_nhl", 
    "mma_mixed_martial_arts", "cricket_test_match", "cricket_odi"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"

# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        self._lock  = threading.Lock()
        self._quota = {k: 500 for k in self.keys}

    def get(self) -> str:
        with self._lock:
            if not self.keys: return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int):
        with self._lock: self._quota[key] = max(0, remaining)

    def mark_exhausted(self, key: str):
        with self._lock: self._quota[key] = 0

    def total_remaining(self) -> int:
        with self._lock: return max(0, sum(self._quota.values()))

    def status(self) -> list:
        with self._lock: return [{"key": f"{k[:4]}...{k[-4:]}", "remaining": self._quota.get(k, 0)} for k in self.keys]

ROTATOR = KeyRotator()

# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {"remaining_requests": 500, "total_events_scanned": 0, "last_arb_count": 0, "last_ev_count": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: defaults.update(json.load(f))
        except: pass
    return defaults

def save_state(state: dict):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

# ═════════════════════════════════════════════════════════════════════════════
# ODDS API FETCHER
# ═════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 3: return []
    try:
        r = requests.get(f"{ODDS_BASE}/sports/{sport}/odds", params={"apiKey": key, "regions": REGIONS, "markets": market, "oddsFormat": "decimal", "dateFormat": "iso"}, timeout=15)
        remaining = int(r.headers.get("X-Requests-Remaining", ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code == 422: return [] 
        if r.status_code in (429, 401): 
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200: return []

        data = r.json()
        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)
        return filtered
    except: return []

def fetch_all_odds(state: dict) -> list:
    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    all_events = []
    with ThreadPoolExecutor(max_workers=3) as ex: # Capped to 3 to prevent API rate limit crashes
        futures = {}
        for s, m in tasks:
            futures[ex.submit(fetch_sport_odds, s, m)] = (s, m)
            time.sleep(0.3)
        for fut in as_completed(futures):
            try: all_events.extend(fut.result())
            except: pass
    state["remaining_requests"]   = ROTATOR.total_remaining()
    state["total_events_scanned"] = len(all_events)
    return all_events

# ═════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER
# ═════════════════════════════════════════════════════════════════════════════
def fetch_bcgame_events() -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://bc.game",
        "Referer": "https://bc.game/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    try:
        r = requests.get(BCGAME_URL, headers=headers, timeout=20)
        if r.status_code != 200:
            log.warning(f"BC.Game blocked request (Status: {r.status_code}). Likely Cloudflare.")
            return []
        
        raw = r.json()
        raw_evs = raw.get("data", {}).get("list", [])
        converted = []
        for ev in raw_evs[:200]:
            home = ev.get("homeName", "")
            away = ev.get("awayName", "")
            outcomes = []
            for m in ev.get("markets", []):
                for o in m.get("outcomes", []):
                    try:
                        price = float(o.get("price", 0))
                        if price > 1.01: outcomes.append({"name": o.get("name", ""), "price": price})
                    except: pass
            if home and away and outcomes:
                converted.append({
                    "id": f"bc_{abs(hash(home+away))}", "sport_title": ev.get("sportName", "Unknown"),
                    "home_team": home, "away_team": away, "commence_time": ev.get("startTime", ""),
                    "bookmakers": [{"key": "bcgame", "title": "BC.Game", "markets": [{"key": "h2h", "outcomes": outcomes}]}]
                })
        return converted
    except Exception as e:
        log.warning(f"BC.Game Error: {e}")
        return []

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (SequenceMatcher(None, bh.lower(), ev.get("home_team", "").lower()).ratio() + SequenceMatcher(None, ba.lower(), ev.get("away_team", "").lower()).ratio()) / 2
            if s > best_score: best_score, best_ev = s, ev
        if best_score > 0.75 and best_ev: best_ev["bookmakers"].extend(bc_ev["bookmakers"])
        else: odds_events.append(bc_ev)
    return odds_events

# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE (STRICT MATCHING)
# ═════════════════════════════════════════════════════════════════════════════
def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        home, away, sport = ev.get("home_team", "?"), ev.get("away_team", "?"), ev.get("sport_title", "?")
        for mkey in MARKETS:
            # STRICT GROUPING: We group outcomes by their exact point value to prevent fake arbs
            points_map = {} 
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mkey: continue
                    for o in mkt.get("outcomes", []):
                        name = str(o.get("name", ""))
                        pt = o.get("point")
                        pt_val = abs(float(pt)) if pt is not None else 0.0
                        try: price = float(o.get("price", 0))
                        except: continue
                        
                        # ANTI-PALP: Kill crazy odds that are obviously bookie mistakes
                        if price <= 1.01 or price > 50.0: continue
                        
                        if pt_val not in points_map: points_map[pt_val] = {}
                        if name not in points_map[pt_val] or price > points_map[pt_val][name][0]:
                            points_map[pt_val][name] = (price, bm.get("title", "?"), bm.get("key", "?"))

            # Evaluate each exact point group individually
            for pt_val, best in points_map.items():
                ol = list(best.items())
                if len(ol) < 2: continue

                for ways in [2, 3]:
                    for combo in itertools.combinations(ol, ways):
                        prices = [x[1][0] for x in combo]
                        impl = sum(1.0 / p for p in prices)
                        if impl < 1.0:
                            pct = (1.0 / impl - 1.0)
                            # ANTI-PALP: Check minimum profit AND maximum profit (ignore fake 40% arbs)
                            if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                                market_display = f"{mkey.upper()} (Line: {pt_val})" if pt_val != 0.0 else mkey.upper()
                                arbs.append({
                                    "id": f"arb_{abs(hash(home+str(pct)))}",
                                    "ways": ways, "market": market_display, "sport": sport, "match": f"{home} vs {away}",
                                    "profit_pct": round(pct * 100, 3), "implied": impl,
                                    "outcomes": [{"name": x[0], "odds": x[1][0], "book": x[1][1], "book_key": x[1][2]} for x in combo]
                                })
    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs[:200]

def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        for mkey in MARKETS:
            pin_out = next((m.get("outcomes", []) for bm in ev.get("bookmakers", []) if bm.get("key") == "pinnacle" for m in bm.get("markets", []) if m.get("key") == mkey), None)
            if not pin_out or len(pin_out) < 2: continue

            raw_vig = {o["name"]: 1.0 / float(o["price"]) for o in pin_out if "price" in o}
            total_vig = sum(raw_vig.values())
            if total_vig <= 0: continue
            true_probs = {k: v / total_vig for k, v in raw_vig.items()}

            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        if name not in true_probs: continue
                        try: price = float(o.get("price", 0))
                        except: continue
                        if price <= 1.01 or price > 50.0: continue
                        
                        tp = true_probs[name]
                        if tp <= 0: continue
                        edge = (price - (1.0 / tp)) / (1.0 / tp)
                        
                        # ANTI-PALP: Max EV edge cap
                        if MIN_EV_EDGE <= edge <= MAX_EV_EDGE:
                            bets.append({
                                "id": f"ev_{abs(hash(ev.get('home_team')+str(edge)))}",
                                "market": mkey.upper(), "sport": ev.get("sport_title", "?"), "match": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                                "outcome": name, "book": bm.get("title", "?"), "book_key": bm.get("key", "?"),
                                "offered_odds": price, "true_odds": round(1.0 / tp, 3), "true_prob_pct": round(tp * 100, 2), "edge_pct": round(edge * 100, 2)
                            })
    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    return bets[:300]

def send_push(arbs: list, evs: list):
    if not arbs and not evs: return
    msg = f"TOP ARB: +{arbs[0]['profit_pct']}%" if arbs else f"TOP EV: +{evs[0]['edge_pct']}%"
    try: requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={"Title": "Arb Sniper Run", "Priority": "high", "Tags": "zap"}, timeout=5)
    except: pass

# ═════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR (SINGLE ELITE UI + DYNAMIC JS STAKES)
# ═════════════════════════════════════════════════════════════════════════════
def generate_html(arbs: list, evs: list, raw_bc: list, state: dict, key_status: list) -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    
    arbs_j = json.dumps(arbs, ensure_ascii=False)
    evs_j  = json.dumps(evs,  ensure_ascii=False)
    bc_j   = json.dumps(raw_bc, ensure_ascii=False)
    keys_j = json.dumps(key_status, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Arb Sniper v6.0 Elite</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.2/gsap.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet"/>
<style>
:root {{
    --bg: #0A0E17; --surface: #111827; --border: #1F2937;
    --text: #F3F4F6; --text-muted: #9CA3AF;
    --accent: #3B82F6; --accent-hover: #2563EB;
    --green: #10B981; --red: #EF4444; --orange: #F59E0B;
    --font: 'Inter', sans-serif; --mono: 'JetBrains Mono', monospace;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
body {{ background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; overflow-x: hidden; }}
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}

/* TOPBAR & BANKROLL */
.topbar {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 15px 24px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }}
.logo {{ font-weight: 900; font-size: 18px; letter-spacing: 1px; display: flex; align-items: center; gap: 8px; }}
.bankroll-wrap {{ display: flex; align-items: center; gap: 10px; background: var(--bg); border: 1px solid var(--border); padding: 5px 15px; border-radius: 8px; }}
.bankroll-input {{ background: transparent; border: none; color: var(--green); font-family: var(--mono); font-size: 16px; font-weight: 700; width: 100px; outline: none; }}

/* TABS */
.tabbar {{ display: flex; gap: 10px; padding: 20px 24px; overflow-x: auto; }}
.tab {{ background: var(--surface); color: var(--text-muted); border: 1px solid var(--border); padding: 10px 20px; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; transition: all 0.2s; white-space: nowrap; }}
.tab:hover {{ border-color: var(--text-muted); color: var(--text); }}
.tab.active {{ background: rgba(59, 130, 246, 0.1); border-color: var(--accent); color: var(--accent); }}

/* CONTENT */
.tc {{ display: none; padding: 0 24px 40px; }}
.tc.active {{ display: block; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 20px; }}

/* CARDS */
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; position: relative; overflow: hidden; transition: transform 0.2s, box-shadow 0.2s; }}
.card:hover {{ transform: translateY(-4px); box-shadow: 0 10px 30px rgba(0,0,0,0.5); border-color: rgba(255,255,255,0.15); }}
.c-head {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }}
.badge {{ font-size: 10px; font-weight: 800; padding: 4px 8px; border-radius: 6px; letter-spacing: 0.5px; text-transform: uppercase; }}
.b-arb {{ background: rgba(16, 185, 129, 0.1); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.2); }}
.b-ev {{ background: rgba(59, 130, 246, 0.1); color: var(--accent); border: 1px solid rgba(59, 130, 246, 0.2); }}
.profit-txt {{ font-family: var(--mono); font-size: 20px; font-weight: 700; }}
.match-title {{ font-weight: 800; font-size: 15px; margin-bottom: 6px; line-height: 1.4; }}
.meta {{ font-size: 11px; color: var(--text-muted); margin-bottom: 15px; display: flex; gap: 10px; }}

/* TABLES */
table {{ width: 100%; border-collapse: collapse; margin-bottom: 15px; font-size: 12px; }}
td {{ padding: 8px 0; border-bottom: 1px solid var(--border); }}
tr:last-child td {{ border: none; }}
.odds-val {{ font-family: var(--mono); font-weight: 700; font-size: 14px; color: var(--text); }}
.stake-val {{ font-family: var(--mono); font-weight: 700; color: var(--green); font-size: 14px; display: block; }}
.stake-exact {{ font-size: 10px; color: var(--text-muted); font-family: var(--font); font-weight: 400; }}

/* BUTTONS */
.btn {{ background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-weight: 600; font-size: 11px; cursor: pointer; transition: all 0.2s; }}
.btn:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}

.empty {{ text-align: center; padding: 60px; color: var(--text-muted); grid-column: 1/-1; border: 1px dashed var(--border); border-radius: 12px; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo"><i class="fas fa-crosshairs" style="color:var(--accent)"></i> ARB SNIPER</div>
  <div class="bankroll-wrap">
    <span style="font-size:12px; color:var(--text-muted); font-weight:600">BANKROLL ₹</span>
    <input type="number" id="bankroll-input" class="bankroll-input" value="10000" oninput="updateBankroll()"/>
  </div>
</div>

<div class="tabbar">
  <button class="tab active" onclick="swTab('arb', this)"><i class="fas fa-percent"></i> Arbs (<span id="c-arb">0</span>)</button>
  <button class="tab" onclick="swTab('ev', this)"><i class="fas fa-chart-line"></i> +EV (<span id="c-ev">0</span>)</button>
  <button class="tab" onclick="swTab('bc', this)"><i class="fas fa-gamepad"></i> BC.Game</button>
  <button class="tab" onclick="swTab('api', this)"><i class="fas fa-server"></i> API Status</button>
</div>

<div id="tc-arb" class="tc active"><div class="grid" id="grid-arb"></div></div>

<div id="tc-ev" class="tc"><div class="grid" id="grid-ev"></div></div>

<div id="tc-bc" class="tc"><div class="grid" id="grid-bc"></div></div>

<div id="tc-api" class="tc">
  <div class="card" style="max-width: 600px;">
    <div class="match-title">System Status | {ist_now}</div>
    <div class="meta" style="margin-bottom: 20px;">Total Events: {state.get('total_events_scanned', 0)} | Quota Left: {total_quota}</div>
    <table>
      <thead><tr><th style="text-align:left;color:var(--text-muted);padding-bottom:10px">API KEY</th><th style="text-align:right;color:var(--text-muted);padding-bottom:10px">QUOTA</th></tr></thead>
      <tbody id="api-body"></tbody>
    </table>
  </div>
</div>

<script>
const ARBS = {arbs_j};
const EVS  = {evs_j};
const Bcs  = {bc_j};
const Keys = {keys_j};

// Bankroll Init
const brInput = document.getElementById('bankroll-input');
let CURRENT_BANKROLL = localStorage.getItem('arb_bankroll_v6') || 10000;
brInput.value = CURRENT_BANKROLL;

document.getElementById('c-arb').textContent = ARBS.length;
document.getElementById('c-ev').textContent = EVS.length;

function swTab(id, btn) {{
  document.querySelectorAll('.tc').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tc-' + id).classList.add('active');
  btn.classList.add('active');
  triggerAnim();
}}

function updateBankroll() {{
  CURRENT_BANKROLL = parseFloat(brInput.value) || 0;
  localStorage.setItem('arb_bankroll_v6', CURRENT_BANKROLL);
  renderArbs(); renderEvs();
}}

function renderArbs() {{
  const g = document.getElementById('grid-arb');
  if(!ARBS.length) return g.innerHTML = '<div class="empty">No arbs found. Waiting for market shifts.</div>';
  
  g.innerHTML = ARBS.map(a => {{
    const profitAmt = (CURRENT_BANKROLL * (1/a.implied - 1)).toFixed(2);
    
    const rows = a.outcomes.map(o => {{
      const exactStake = ((1 / o.odds) / a.implied * CURRENT_BANKROLL);
      const roundStake = Math.round(exactStake / 10) * 10;
      return `<tr>
        <td><div>${{o.name}}</div><div style="font-size:10px;color:var(--text-muted)">${{o.book}}</div></td>
        <td style="text-align:right"><span class="odds-val">${{o.odds}}</span></td>
        <td style="text-align:right"><span class="stake-val">₹${{roundStake}}</span><span class="stake-exact">Exact: ₹${{exactStake.toFixed(2)}}</span></td>
      </tr>`;
    }}).join('');

    return `
    <div class="card item">
      <div class="c-head">
        <span class="badge b-arb">${{a.ways}}-WAY · ${{a.market}}</span>
        <span class="profit-txt" style="color:var(--green)">+${{a.profit_pct}}%</span>
      </div>
      <div class="match-title">${{a.match}}</div>
      <div class="meta"><span><i class="fas fa-tag"></i> ${{a.sport.replace(/_/g,' ')}}</span></div>
      <table>${{rows}}</table>
      <div style="border-top:1px solid var(--border); padding-top:12px; display:flex; justify-content:space-between; align-items:center">
        <span style="font-weight:700; font-size:14px; color:var(--text)">Profit: <span style="color:var(--green)">₹${{profitAmt}}</span></span>
      </div>
    </div>`;
  }}).join('');
}}

function renderEvs() {{
  const g = document.getElementById('grid-ev');
  if(!EVS.length) return g.innerHTML = '<div class="empty">No value bets found.</div>';
  
  g.innerHTML = EVS.map(v => {{
    const b = v.offered_odds - 1;
    const p = v.true_prob_pct / 100;
    const kf = (b * p - (1-p)) / b;
    const exactStake = kf > 0 ? (0.30 * kf * CURRENT_BANKROLL) : 0;
    const roundStake = Math.round(exactStake / 10) * 10;

    return `
    <div class="card item">
      <div class="c-head">
        <span class="badge b-ev">+EV BET · ${{v.market}}</span>
        <span class="profit-txt" style="color:var(--accent)">+${{v.edge_pct}}% Edge</span>
      </div>
      <div class="match-title">${{v.match}}</div>
      <div class="meta"><span>${{v.book}}</span></div>
      <table>
        <tr><td style="color:var(--text-muted)">Outcome</td><td style="text-align:right;font-weight:700">${{v.outcome}}</td></tr>
        <tr><td style="color:var(--text-muted)">Offered / True</td><td style="text-align:right"><span class="odds-val">${{v.offered_odds}}</span> <span style="color:var(--text-muted);font-size:10px">/ ${{v.true_odds}}</span></td></tr>
        <tr><td style="color:var(--text-muted)">Kelly (30%)</td><td style="text-align:right"><span class="stake-val">₹${{roundStake}}</span><span class="stake-exact">Exact: ₹${{exactStake.toFixed(2)}}</span></td></tr>
      </table>
    </div>`;
  }}).join('');
}}

function renderBc() {{
  const g = document.getElementById('grid-bc');
  if(!Bcs.length) return g.innerHTML = '<div class="empty">No BC.Game events pulled. Cloudflare may be blocking GitHub servers.</div>';
  g.innerHTML = Bcs.map(b => `<div class="card item"><div class="badge b-ev" style="margin-bottom:10px; display:inline-block">BC.GAME</div><div class="match-title">${{b.home_team}} vs ${{b.away_team}}</div><div class="meta">${{b.sport_title}}</div><table>${{b.bookmakers[0].markets[0].outcomes.map(o=>`<tr><td>${{o.name}}</td><td style="text-align:right" class="odds-val">${{o.price}}</td></tr>`).join('')}}</table></div>`).join('');
}}

function renderApi() {{
  document.getElementById('api-body').innerHTML = Keys.map(k => `<tr><td style="font-family:var(--mono); color:var(--text-muted)">${{k.key}}</td><td style="text-align:right; font-weight:700; color:${{k.remaining>50?'var(--green)':'var(--red)'}}">${{k.remaining}}</td></tr>`).join('');
}}

function triggerAnim() {{
  const cards = document.querySelectorAll('.tc.active .card');
  if(cards.length) {{
    gsap.killTweensOf(cards);
    gsap.fromTo(cards, {{y: 30, opacity: 0}}, {{y: 0, opacity: 1, duration: 0.5, stagger: 0.05, ease: "power2.out"}});
  }}
}}

// Initialize
renderArbs(); renderEvs(); renderBc(); renderApi(); triggerAnim();
</script>
</body></html>"""

# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║         ARB SNIPER v6.0 — Starting Run           ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    odds_events = fetch_all_odds(state)
    bc_events   = fetch_bcgame_events()
    all_events  = merge_bcgame(odds_events, list(bc_events))

    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    send_push(arbs, evs)

    html = generate_html(arbs, evs, bc_events, state, ROTATOR.status())
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f: f.write(html)

    log.info(f"  Arbs Found : {len(arbs)}")
    log.info(f"  EV Bets    : {len(evs)}")
    log.info(f"  API Quota  : {ROTATOR.total_remaining()}")
    log.info("╚══════════════════════════════════════════════════╝")

if __name__ == "__main__":
    main()
