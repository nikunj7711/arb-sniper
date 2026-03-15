#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v5.0 — THE ELITE ENGINE                        ║
║  19-Key Rotation | Deep Parser | GSAP Physics | Multi-Theme | Bet Tracker    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, math, time, hashlib, requests, logging, itertools, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

# ─── Constants ─────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
BCGAME_URL     = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"

KELLY_FRACTION = 0.30
MIN_ARB_PROFIT = 0.001   # 0.1%
MIN_EV_EDGE    = 0.005   # 0.5%
BANK_SIZE      = 10000   # default bank in Rs

ALLOWED_BOOKS = {
    "pinnacle","onexbet","bet365","unibet","betway","stake",
    "marathonbet","parimatch","betfair","dafabet","bovada",
    "draftkings","fanduel","betmgm"
}

SPORTS_LIST = [
    "soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga","soccer_italy_serie_a",
    "soccer_france_ligue_one","soccer_uefa_champs_league","soccer_uefa_europa_league",
    "basketball_nba","basketball_euroleague","icehockey_nhl","tennis_atp_french_open",
    "mma_mixed_martial_arts","americanfootball_nfl","cricket_ipl","cricket_international_championship"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"

# ═══════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
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
        with self._lock: self._quota[key] = remaining

    def mark_exhausted(self, key: str):
        with self._lock: self._quota[key] = 0

    def total_remaining(self) -> int:
        with self._lock: return max(0, sum(self._quota.values()))

    def status(self) -> list:
        with self._lock: return [{"key": f"{k[:4]}...{k[-4:]}", "remaining": self._quota.get(k, 0)} for k in self.keys]

ROTATOR = KeyRotator()

# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {"remaining_requests": 500, "total_events_scanned": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: defaults.update(json.load(f))
        except: pass
    return defaults

def save_state(state: dict):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

# ═══════════════════════════════════════════════════════════════════════════════
# ODDS API & BC.GAME FETCHERS (Backend Logic)
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 3: return []
    try:
        r = requests.get(f"{ODDS_BASE}/sports/{sport}/odds", params={"apiKey": key, "regions": REGIONS, "markets": market, "oddsFormat": "decimal", "dateFormat": "iso"}, timeout=15)
        ROTATOR.update(key, int(r.headers.get("X-Requests-Remaining", 0)), int(r.headers.get("X-Requests-Used", 0)))
        if r.status_code != 200: return []
        data = r.json()
        return [ev for ev in data if [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]]
    except: return []

def fetch_all_odds(state: dict) -> list:
    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    all_events = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for fut in as_completed({ex.submit(fetch_sport_odds, s, m): (s, m) for s, m in tasks}):
            try: all_events.extend(fut.result())
            except: pass
    state["total_events_scanned"] = len(all_events)
    return all_events

def fetch_bcgame_events() -> list:
    try:
        r = requests.get(BCGAME_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=15)
        if r.status_code != 200: return []
        raw_evs = r.json().get("data", {}).get("list", [])
        converted = []
        for ev in raw_evs:
            home = ev.get("homeName", "Home")
            away = ev.get("awayName", "Away")
            outs = [{"name": o.get("name"), "price": float(o.get("price"))} for m in ev.get("markets", []) for o in m.get("outcomes", []) if float(o.get("price", 0)) > 1.01]
            if outs: converted.append({"id": f"bc_{hash(home+away)}", "sport_title": ev.get("sportName", "Unknown"), "home_team": home, "away_team": away, "commence_time": ev.get("startTime", ""), "bookmakers": [{"key": "bcgame", "title": "BC.Game", "markets": [{"key": "h2h", "outcomes": outs}]}]})
        return converted
    except: return []

# ═══════════════════════════════════════════════════════════════════════════════
# QUANT ENGINE (Arbs & EV)
# ═══════════════════════════════════════════════════════════════════════════════
def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        for mkey in MARKETS:
            best = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mkey: continue
                    for o in mkt.get("outcomes", []):
                        name = str(o.get("name","")) + str(o.get("point", ""))
                        try:
                            price = float(o.get("price", 0))
                            if price > best.get(name, (0,))[0]: best[name] = (price, bm.get("title"), bm.get("key"))
                        except: pass
            ol = list(best.items())
            for ways in [2, 3]:
                for combo in itertools.combinations(ol, ways):
                    prices = [x[1][0] for x in combo]
                    impl = sum(1.0/p for p in prices)
                    if impl < 1.0 and (1.0/impl - 1.0) >= MIN_ARB_PROFIT:
                        arbs.append({
                            "id": f"arb_{abs(hash(ev.get('home_team')+str(impl)))}",
                            "ways": ways, "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                            "match": f"{ev.get('home_team')} vs {ev.get('away_team')}", "commence": ev.get("commence_time"),
                            "profit_pct": round((1.0/impl - 1.0)*100, 3),
                            "outcomes": [{"name": x[0], "odds": x[1][0], "book": x[1][1], "book_key": x[1][2]} for x in combo]
                        })
    return sorted(arbs, key=lambda x: x["profit_pct"], reverse=True)[:200]

def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        for mkey in MARKETS:
            pin_out = next((m.get("outcomes", []) for bm in ev.get("bookmakers", []) if bm.get("key") == "pinnacle" for m in bm.get("markets", []) if m.get("key") == mkey), None)
            if not pin_out: continue
            raw_vig = {o["name"]: 1.0/float(o["price"]) for o in pin_out if "price" in o}
            total_vig = sum(raw_vig.values())
            if total_vig == 0: continue
            true_probs = {k: v/total_vig for k,v in raw_vig.items()}
            
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        if name in true_probs:
                            price = float(o.get("price", 0))
                            tp = true_probs[name]
                            if tp > 0 and price > 1.0:
                                edge = (price - (1.0/tp)) / (1.0/tp)
                                if edge >= MIN_EV_EDGE:
                                    bets.append({
                                        "id": f"ev_{abs(hash(ev.get('home_team')+str(edge)))}",
                                        "market": mkey.upper(), "sport": ev.get("sport_title", ""),
                                        "match": f"{ev.get('home_team')} vs {ev.get('away_team')}", "commence": ev.get("commence_time"),
                                        "outcome": name, "book": bm.get("title"), "book_key": bm.get("key"),
                                        "offered_odds": price, "true_odds": round(1.0/tp, 3), "true_prob_pct": round(tp*100, 2), "edge_pct": round(edge*100, 2)
                                    })
    return sorted(bets, key=lambda x: x["edge_pct"], reverse=True)[:300]


# ═══════════════════════════════════════════════════════════════════════════════
# FULL HTML/JS/CSS GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════
def generate_html(arbs, evs, raw_bc, state, key_status) -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    total_quota = sum(k["remaining"] for k in key_status)

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="neobrutalism">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v5.0 Elite</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.2/gsap.min.js"></script>
<style>
/* =========================================
   BASE & RESET
   ========================================= */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=Inter:wght@400;700;900&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; transition: background 0.3s ease, color 0.3s ease, border-color 0.3s ease; }}
body {{ min-height: 100vh; overflow-x: hidden; padding-bottom: 50px; }}
::-webkit-scrollbar {{ height: 6px; width: 6px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 10px; }}

/* =========================================
   THEMES (Minimalism, Neobrutalism, Glass, Clay, Liquid)
   ========================================= */
[data-theme="minimalism"] {{ --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0; --text: #0f172a; --text-muted: #64748b; --accent: #2563eb; --green: #16a34a; --font: 'Inter', sans-serif; }}
[data-theme="minimalism"] .card, [data-theme="minimalism"] .csec, [data-theme="minimalism"] .modal {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }}

[data-theme="neobrutalism"] {{ --bg: #ffde59; --surface: #ffffff; --border: #000000; --text: #000000; --text-muted: #333333; --accent: #ff00ff; --green: #00ff00; --font: 'JetBrains Mono', monospace; }}
[data-theme="neobrutalism"] body {{ font-weight: bold; text-transform: uppercase; }}
[data-theme="neobrutalism"] .card, [data-theme="neobrutalism"] .csec, [data-theme="neobrutalism"] .modal {{ background: var(--surface); border: 4px solid var(--border); border-radius: 0; box-shadow: 8px 8px 0px var(--border); }}
[data-theme="neobrutalism"] input, [data-theme="neobrutalism"] select, [data-theme="neobrutalism"] button {{ border: 3px solid #000; box-shadow: 4px 4px 0 #000; font-weight: bold; border-radius: 0; }}

[data-theme="glassmorphism"] {{ --bg: #0f172a; --surface: rgba(255,255,255,0.05); --border: rgba(255,255,255,0.1); --text: #f8fafc; --text-muted: #94a3b8; --accent: #38bdf8; --green: #4ade80; --font: 'Inter', sans-serif; }}
[data-theme="glassmorphism"] body {{ background: linear-gradient(135deg, #020617 0%, #1e1b4b 100%); }}
[data-theme="glassmorphism"] .card, [data-theme="glassmorphism"] .csec, [data-theme="glassmorphism"] .modal {{ background: var(--surface); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border: 1px solid var(--border); border-radius: 24px; box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1); }}

[data-theme="claymorphism"] {{ --bg: #e0e5ec; --surface: #e0e5ec; --border: transparent; --text: #4a5568; --text-muted: #a0aec0; --accent: #ff7b54; --green: #38a169; --font: 'Inter', sans-serif; }}
[data-theme="claymorphism"] .card, [data-theme="claymorphism"] .csec, [data-theme="claymorphism"] .modal {{ border-radius: 36px; border: none; box-shadow: 20px 20px 60px #bec3c9, -20px -20px 60px #ffffff, inset 4px 4px 8px rgba(255, 255, 255, 0.4), inset -4px -4px 8px rgba(0, 0, 0, 0.05); }}
[data-theme="claymorphism"] input, [data-theme="claymorphism"] select, [data-theme="claymorphism"] button {{ box-shadow: 6px 6px 12px #bec3c9, -6px -6px 12px #ffffff; border: none; border-radius: 16px; }}

[data-theme="liquid"] {{ --bg: #000000; --surface: rgba(0, 255, 204, 0.05); --border: rgba(0, 255, 204, 0.2); --text: #ffffff; --text-muted: rgba(255,255,255,0.6); --accent: #00ffcc; --green: #00ffcc; --font: 'Inter', sans-serif; }}
[data-theme="liquid"] .grid {{ filter: url('#goo'); padding-bottom: 40px; }}
[data-theme="liquid"] .card, [data-theme="liquid"] .csec, [data-theme="liquid"] .modal {{ border: 1px solid var(--border); border-radius: 50px; box-shadow: 0 0 20px rgba(0,255,204,0.05); }}

/* =========================================
   STRUCTURAL CSS
   ========================================= */
body {{ font-family: var(--font); background: var(--bg); color: var(--text); }}
.topbar {{ display: flex; justify-content: space-between; align-items: center; padding: 15px 20px; background: var(--bg); z-index: 100; position: sticky; top:0; }}
.statsbar {{ display: flex; gap: 15px; padding: 10px 20px; overflow-x: auto; white-space: nowrap; border-bottom: 1px solid var(--border); font-size: 12px; font-weight: bold; background: var(--bg); }}
.tabs {{ display: flex; gap: 12px; padding: 15px 20px; overflow-x: auto; background: var(--bg); }}
.tab {{ padding: 10px 20px; background: var(--surface); color: var(--text); border: 1px solid var(--border); cursor: pointer; border-radius: 8px; font-weight: bold; }}
.tab.active {{ background: var(--accent); color: var(--bg); border-color: var(--accent); }}
.tc {{ display: none; padding: 0 20px; }} .tc.active {{ display: block; }}
.fbar {{ display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }}
input, select, button {{ padding: 10px 14px; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; outline: none; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 25px; }}
.card {{ padding: 24px; position: relative; }}
.ctable {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
.ctable td {{ padding: 8px 0; border-bottom: 1px solid rgba(128,128,128,0.2); font-size: 14px; }}
.cbtn {{ background: transparent; color: var(--accent); border: 1px solid var(--accent); padding: 5px 10px; cursor: pointer; border-radius: 6px; font-size: 11px; font-weight: bold; }}
.cbtn:hover {{ background: var(--accent); color: var(--bg); }}

/* MODAL */
.modal-bg {{ position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(5px); z-index: 999; display: none; align-items: center; justify-content: center; }}
.modal-bg.open {{ display: flex; }}
.modal {{ width: 90%; max-width: 500px; padding: 30px; position: relative; }}
.closex {{ position: absolute; right: 20px; top: 20px; background: transparent; border: none; color: var(--text); font-size: 20px; cursor: pointer; box-shadow: none !important; }}
</style>
</head>
<body>

<svg style="width:0;height:0;position:absolute;" aria-hidden="true"><defs><filter id="goo"><feGaussianBlur in="SourceGraphic" stdDeviation="10" result="blur" /><feColorMatrix in="blur" mode="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 19 -9" result="goo" /><feBlend in="SourceGraphic" in2="goo" /></filter></defs></svg>

<div id="app">
  <div class="topbar">
    <div class="logo"><strong><i class="fas fa-crosshairs"></i> ARB SNIPER v5.0 ELITE</strong></div>
    <div style="display:flex; gap:10px; align-items:center;">
      <input type="number" id="user-bankroll" title="Bankroll" onchange="updateBankroll(this.value)" style="width: 100px;" />
      <select id="theme-selector" onchange="switchTheme(this.value)">
        <option value="minimalism">Minimalism</option><option value="neobrutalism">Neo-Brutalism</option>
        <option value="glassmorphism">Glassmorphism</option><option value="claymorphism">Claymorphism</option>
        <option value="liquid">Liquid Glass</option>
      </select>
    </div>
  </div>

  <div class="statsbar">
    <span style="color:var(--text-muted)"><i class="fas fa-clock"></i> {ist_now}</span>
    <span style="color:var(--accent)"><i class="fas fa-server"></i> API Quota: {total_quota}</span>
    <span style="color:var(--green)"><i class="fas fa-bolt"></i> Total Events Scanned: {state.get('total_events_scanned',0)}</span>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="swTab('arb', this)"><i class="fas fa-percent"></i> Arbs <span id="cnt-arb">0</span></button>
    <button class="tab" onclick="swTab('ev', this)"><i class="fas fa-chart-line"></i> +EV <span id="cnt-ev">0</span></button>
    <button class="tab" onclick="swTab('bc', this)"><i class="fas fa-gamepad"></i> BC.Game <span id="cnt-bc">0</span></button>
    <button class="tab" onclick="swTab('track', this)"><i class="fas fa-bookmark"></i> Tracker</button>
    <button class="tab" onclick="swTab('calc', this)"><i class="fas fa-calculator"></i> Calculators</button>
  </div>

  <div id="tc-arb" class="tc active">
    <div class="fbar">
      <input type="text" id="arb-q" placeholder="Search team..." oninput="renderArbs()" style="flex:1" />
      <select id="arb-sport" onchange="renderArbs()"><option value="">All Sports</option></select>
      <select id="arb-ways" onchange="renderArbs()"><option value="">All Ways</option><option value="2">2-Way</option><option value="3">3-Way</option></select>
    </div>
    <div class="grid" id="grid-arb"></div>
  </div>

  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input type="text" id="ev-q" placeholder="Search team or book..." oninput="renderEvs()" style="flex:1" />
      <select id="ev-sport" onchange="renderEvs()"><option value="">All Sports</option></select>
    </div>
    <div class="grid" id="grid-ev"></div>
  </div>

  <div id="tc-bc" class="tc"><div class="grid" id="grid-bc"></div></div>

  <div id="tc-track" class="tc">
    <div style="margin-bottom: 20px; display:flex; justify-content:space-between; align-items:center;">
      <h3><i class="fas fa-bookmark" style="color:var(--accent)"></i> My Tracked Bets (Local Storage)</h3>
      <button class="cbtn" onclick="clearTracker()"><i class="fas fa-trash"></i> Clear All</button>
    </div>
    <div class="grid" id="grid-track"></div>
  </div>

  <div id="tc-calc" class="tc">
    <div class="grid">
        <div class="csec card">
          <h3 style="margin-bottom: 15px;"><i class="fas fa-brain" style="color:var(--accent)"></i> Kelly Calculator</h3>
          <input style="width:100%; margin-bottom:10px" id="kp" type="number" step="0.1" placeholder="Win Prob % (e.g. 55.0)"/>
          <input style="width:100%; margin-bottom:10px" id="ko" type="number" step="0.01" placeholder="Dec Odds (e.g. 2.10)"/>
          <button style="width:100%; background:var(--accent); color:var(--bg)" onclick="runKelly()">CALCULATE</button>
          <div id="kelly-res" style="margin-top:15px; font-weight:bold;"></div>
        </div>
        <div class="csec card">
          <h3 style="margin-bottom: 15px;"><i class="fas fa-arrows-rotate" style="color:var(--green)"></i> Odds Converter</h3>
          <input style="width:100%; margin-bottom:10px" id="od" type="number" step="0.001" placeholder="Decimal" oninput="convOdds('d')"/>
          <input style="width:100%; margin-bottom:10px" id="oa" type="number" placeholder="American" oninput="convOdds('a')"/>
          <input style="width:100%; margin-bottom:10px" id="oi" type="number" step="0.01" placeholder="Implied %" oninput="convOdds('i')"/>
        </div>
    </div>
  </div>
</div>

<div class="modal-bg" id="qm">
  <div class="modal">
    <button class="closex" onclick="closeModal()"><i class="fas fa-xmark"></i></button>
    <h3 style="margin-bottom: 15px; color:var(--accent)"><i class="fas fa-calculator"></i> Quick Calc</h3>
    <div id="qm-body"></div>
  </div>
</div>

<script>
const ARBS = {json.dumps(arbs)};
const EVS  = {json.dumps(evs)};
const BC_RAW = {json.dumps(raw_bc)};

// STATE & AUDIO ENGINE
let CURRENT_BANKROLL = localStorage.getItem('arb_bankroll') || 10000;
document.getElementById('user-bankroll').value = CURRENT_BANKROLL;
const savedTheme = localStorage.getItem('arb_theme') || 'neobrutalism';
document.documentElement.setAttribute('data-theme', savedTheme);
document.getElementById('theme-selector').value = savedTheme;

// SYNTHESIZED UI SOUNDS
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playSound(type) {{
  if(audioCtx.state === 'suspended') audioCtx.resume();
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.connect(gain); gain.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  const theme = document.documentElement.getAttribute('data-theme');
  
  if(theme === 'neobrutalism') {{ osc.type = 'square'; osc.frequency.setValueAtTime(150, now); gain.gain.setValueAtTime(0.1, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.1); osc.start(now); osc.stop(now + 0.1); }}
  else if(theme === 'liquid') {{ osc.type = 'sine'; osc.frequency.setValueAtTime(400, now); osc.frequency.exponentialRampToValueAtTime(800, now + 0.3); gain.gain.setValueAtTime(0.1, now); gain.gain.linearRampToValueAtTime(0, now + 0.3); osc.start(now); osc.stop(now + 0.3); }}
  else {{ osc.type = 'triangle'; osc.frequency.setValueAtTime(600, now); gain.gain.setValueAtTime(0.05, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.1); osc.start(now); osc.stop(now + 0.1); }}
}}

// POPULATE FILTERS
const aS = [...new Set(ARBS.map(a => a.sport))].sort();
const eS = [...new Set(EVS.map(e => e.sport))].sort();
aS.forEach(v => document.getElementById('arb-sport').add(new Option(v.replace(/_/g,' '), v)));
eS.forEach(v => document.getElementById('ev-sport').add(new Option(v.replace(/_/g,' '), v)));

// TAB & THEME LOGIC
function swTab(id, btn) {{
  playSound();
  document.querySelectorAll('.tc').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tc-' + id).classList.add('active');
  if(btn) btn.classList.add('active');
  if(id === 'track') renderTracker();
  animateCards(document.documentElement.getAttribute('data-theme'));
}}

function switchTheme(theme) {{
  playSound();
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('arb_theme', theme);
  animateCards(theme);
}}

function updateBankroll(val) {{
  CURRENT_BANKROLL = parseFloat(val) || 10000;
  localStorage.setItem('arb_bankroll', CURRENT_BANKROLL);
  renderAll(); 
}}

// BET TRACKER LOGIC
function getTracked() {{ return JSON.parse(localStorage.getItem('arb_tracker') || '[]'); }}
function trackBet(type, id) {{
  playSound();
  let tracked = getTracked();
  if(!tracked.some(t => t.id === id)) {{
    const item = type === 'arb' ? ARBS.find(a => a.id === id) : EVS.find(e => e.id === id);
    item.track_type = type; item.track_date = new Date().toLocaleString();
    tracked.push(item);
    localStorage.setItem('arb_tracker', JSON.stringify(tracked));
    alert('Saved to Tracker!');
  }}
}}
function clearTracker() {{ playSound(); localStorage.removeItem('arb_tracker'); renderTracker(); }}

function renderTracker() {{
  const g = document.getElementById('grid-track');
  const tracked = getTracked();
  if(!tracked.length) {{ g.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px">Tracker empty.</div>'; return; }}
  
  g.innerHTML = tracked.map(t => `<div class="card item">
    <div style="color:var(--accent); font-weight:bold; margin-bottom:5px;">${{t.track_type.toUpperCase()}} - Saved ${{t.track_date}}</div>
    <div style="font-weight: 900; font-size: 16px;">${{t.match}}</div>
    <div style="margin-top:10px; font-size:12px">${{t.sport.replace(/_/g,' ')}}</div>
  </div>`).join('');
}}

// RENDER ARBS
function renderArbs() {{
  const q = document.getElementById('arb-q').value.toLowerCase();
  const sp = document.getElementById('arb-sport').value;
  const wy = document.getElementById('arb-ways').value;
  
  const data = ARBS.filter(a => (!sp || a.sport === sp) && (!wy || String(a.ways) === wy) && (!q || a.match.toLowerCase().includes(q)));
  document.getElementById('cnt-arb').textContent = data.length;
  const g = document.getElementById('grid-arb');
  if(!data.length) {{ g.innerHTML = '<div style="padding:40px">No arbs found.</div>'; return; }}
  
  g.innerHTML = data.map(a => {{
    const impl = a.outcomes.reduce((sum, o) => sum + (1 / o.odds), 0);
    const profitAmt = (CURRENT_BANKROLL * (1/impl - 1)).toFixed(2);
    const oa = JSON.stringify(a.outcomes.map(o => o.odds));
    
    const rows = a.outcomes.map(o => {{
        const stake = ((1 / o.odds) / impl * CURRENT_BANKROLL).toFixed(2);
        return `<tr><td>${{o.name}}<br><small style="color:var(--text-muted)">${{o.book_key}}</small></td><td style="text-align:right; font-weight:bold">${{o.odds}}</td><td style="text-align:right; color:var(--accent); font-weight:bold">Rs${{stake}}</td></tr>`;
    }}).join('');

    return `<div class="card item">
      <div style="display:flex; justify-content:space-between; margin-bottom: 12px;">
        <span style="color:var(--accent); font-weight:bold">${{a.ways}}-WAY ARB</span>
        <span style="color:var(--green); font-weight:bold; font-size: 18px;">+${{a.profit_pct}}%</span>
      </div>
      <div style="font-weight: 900; font-size: 16px;">${{a.match}}</div>
      <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;">${{a.market}}</div>
      <table class="ctable">${{rows}}</table>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-top: 15px;">
        <span style="font-weight: 900; font-size: 15px;"><i class="fas fa-coins" style="color:var(--green)"></i> PROFIT: Rs${{profitAmt}}</span>
        <div>
          <button class="cbtn" onclick='openQC(${{oa}})'><i class="fas fa-calculator"></i> Calc</button>
          <button class="cbtn" onclick='trackBet("arb", "${{a.id}}")'><i class="fas fa-bookmark"></i> Track</button>
        </div>
      </div>
    </div>`;
  }}).join('');
}}

// RENDER EV
function renderEvs() {{
  const q = document.getElementById('ev-q').value.toLowerCase();
  const sp = document.getElementById('ev-sport').value;
  const data = EVS.filter(v => (!sp || v.sport === sp) && (!q || v.match.toLowerCase().includes(q) || v.book.toLowerCase().includes(q)));
  document.getElementById('cnt-ev').textContent = data.length;
  
  const g = document.getElementById('grid-ev');
  if(!data.length) return g.innerHTML = '<div style="padding:40px">No +EV bets found.</div>';
  
  g.innerHTML = data.map(v => {{
    const kf = ((v.offered_odds-1) * (v.true_prob_pct/100) - (1-(v.true_prob_pct/100))) / (v.offered_odds-1);
    const stake = kf > 0 ? (0.3 * kf * CURRENT_BANKROLL).toFixed(2) : "0.00";

    return `<div class="card item">
      <div style="display:flex; justify-content:space-between; margin-bottom: 12px;">
        <span style="color:var(--accent); font-weight:bold">+EV BET</span><span style="color:var(--green); font-weight:bold; font-size: 18px;">+${{v.edge_pct}}% EDGE</span>
      </div>
      <div style="font-weight: 900; font-size: 16px;">${{v.match}}</div>
      <table class="ctable" style="margin-top:15px">
        <tr><td style="color:var(--text-muted)">Outcome</td><td style="text-align:right; font-weight:bold">${{v.outcome}}</td></tr>
        <tr><td style="color:var(--text-muted)">Bookmaker</td><td style="text-align:right; font-weight:bold">${{v.book}}</td></tr>
        <tr><td style="color:var(--text-muted)">Offered / True</td><td style="text-align:right; font-weight:bold">${{v.offered_odds}} / ${{v.true_odds}}</td></tr>
        <tr><td style="color:var(--text-muted)">Rec. Stake (30%)</td><td style="text-align:right; font-weight:bold; color:var(--accent)">Rs${{stake}}</td></tr>
      </table>
      <div style="text-align:right; margin-top:10px;"><button class="cbtn" onclick='trackBet("ev", "${{v.id}}")'><i class="fas fa-bookmark"></i> Track</button></div>
    </div>`;
  }}).join('');
}}

function renderBc() {{
  const g = document.getElementById('grid-bc');
  document.getElementById('cnt-bc').textContent = BC_RAW.length;
  g.innerHTML = BC_RAW.slice(0, 50).map(b => `<div class="card item"><div style="color:var(--accent); font-weight:bold; margin-bottom: 12px;">BC.GAME EXCLUSIVE</div><div style="font-weight: 900; font-size: 16px;">${{b.home_team}} vs ${{b.away_team}}</div><table class="ctable">${{b.bookmakers[0].markets[0].outcomes.map(o => `<tr><td>${{o.name}}</td><td style="text-align:right; font-weight:bold">${{o.price}}</td></tr>`).join('')}}</table></div>`).join('');
}}

// MODAL QUICK CALC
function openQC(oa) {{
  playSound();
  const impl = oa.reduce((s, o) => s + 1/o, 0);
  const pct = (1/impl - 1) * 100;
  const stakes = oa.map(o => ((1/o)/impl * CURRENT_BANKROLL).toFixed(2));
  document.getElementById('qm-body').innerHTML = `
    <table class="ctable">${{oa.map((o,i) => `<tr><td>Leg ${{i+1}} @ ${{o}}</td><td style="text-align:right; font-weight:bold; color:var(--accent)">Rs${{stakes[i]}}</td></tr>`).join('')}}</table>
    <div style="margin-top:15px; font-weight:bold; font-size:16px; color:${{pct>0?'var(--green)':'var(--red)'}}">PROFIT: Rs${{(CURRENT_BANKROLL * (1/impl - 1)).toFixed(2)}}</div>
  `;
  document.getElementById('qm').classList.add('open');
}}
function closeModal() {{ playSound(); document.getElementById('qm').classList.remove('open'); }}

// ANIMATION ENGINE
function animateCards(theme) {{
  const cards = document.querySelectorAll('.tc.active .card');
  if (!cards.length) return;
  gsap.killTweensOf(cards);
  if (theme === 'neobrutalism') gsap.fromTo(cards, {{ y: -40, opacity: 0, rotation: -2, scale: 0.95 }}, {{ y: 0, opacity: 1, rotation: 0, scale: 1, duration: 0.6, stagger: 0.04, ease: "elastic.out(1, 0.5)" }});
  else if (theme === 'claymorphism') gsap.fromTo(cards, {{ scale: 0.85, opacity: 0, y: 30 }}, {{ scale: 1, opacity: 1, y: 0, duration: 0.7, stagger: 0.05, ease: "back.out(1.5)" }});
  else if (theme === 'liquid') gsap.fromTo(cards, {{ y: 40, opacity: 0, scale: 0.9 }}, {{ y: 0, opacity: 1, scale: 1, duration: 1.0, stagger: 0.1, ease: "sine.inOut" }});
  else if (theme === 'glassmorphism') gsap.fromTo(cards, {{ y: 30, opacity: 0 }}, {{ y: 0, opacity: 1, duration: 0.7, stagger: 0.06, ease: "power2.out" }});
  else gsap.fromTo(cards, {{ opacity: 0, y: 10 }}, {{ opacity: 1, y:0, duration: 0.3, stagger: 0.02, ease: "linear" }});
}}

// CALC UTILS
function runKelly() {{
  playSound();
  const p = parseFloat(document.getElementById('kp').value) / 100; const o = parseFloat(document.getElementById('ko').value);
  if(!p || !o) return;
  const kf = ((o-1)*p - (1-p)) / (o-1);
  const res = document.getElementById('kelly-res');
  res.innerHTML = `EV: <span style="color:var(--green)">${{((p*(o-1)-(1-p))*100).toFixed(2)}}%</span><br>Rec Stake (30%): <span style="color:var(--accent)">Rs${{(kf>0 ? 0.3*kf*CURRENT_BANKROLL : 0).toFixed(2)}}</span>`;
}}
let _cv = false;
function convOdds(f) {{
  if(_cv) return; _cv = true;
  try {{
    if(f==='d') {{ const v = parseFloat(document.getElementById('od').value); if(v>1) {{ document.getElementById('oa').value = v>=2 ? '+'+Math.round((v-1)*100) : '-'+Math.round(100/(v-1)); document.getElementById('oi').value = (100/v).toFixed(2); }} }}
  }} finally {{ _cv = false; }}
}}

// BOOT
function renderAll() {{ renderArbs(); renderEvs(); renderBc(); renderTracker(); animateCards(document.documentElement.getAttribute('data-theme')); }}
renderAll();
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══ ARB SNIPER v5.0 ELITE — Starting ══╗")
    state = load_state()

    odds_events = fetch_all_odds(state)
    bc_events   = fetch_bcgame_events()
    all_events  = odds_events + bc_events 
    
    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    # Push Notification
    if arbs or evs:
        msg = f"ARB: {len(arbs)} | EV: {len(evs)} | Quota: {ROTATOR.total_remaining()}"
        try: requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={"Title": "Arb Sniper Run Complete", "Priority": "default"}, timeout=5)
        except: pass

    key_status = ROTATOR.status()
    html = generate_html(arbs, evs, bc_events, state, key_status)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    log.info(f"  Arbs Found:  {len(arbs)}")
    log.info(f"  EV Bets:     {len(evs)}")
    log.info("╚══ Run Complete ══╝")

if __name__ == "__main__":
    main()
