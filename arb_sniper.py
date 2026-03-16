#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v4.0 — SECURE MOBILE EDITION                   ║
║    Global Sports Arbitrage | Multi-Key Engine | Calculator | Stealth BC      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, math, time, hashlib, requests, logging, itertools, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

# ─── Constants & Security ──────────────────────────────────────────────────────
_raw_keys = os.environ.get("ODDS_API_KEYS", "")
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

ODDS_BASE       = "https://api.the-odds-api.com/v4"
BCGAME_URL      = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL        = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE      = "api_state.json"
OUTPUT_HTML     = "index.html"
DASHBOARD_PASS  = os.environ.get("DASHBOARD_PASS", "arb2026")

KELLY_FRACTION  = 0.30
MIN_ARB_PROFIT  = 0.001   # 0.1%
MIN_EV_EDGE     = 0.005   # 0.5%
BANK_SIZE       = 10000   

ALLOWED_BOOKS = {
    "pinnacle", "onexbet", "bet365", "unibet", "betway", "stake",
    "marathonbet", "parimatch", "betfair", "dafabet", "bovada",
    "draftkings", "fanduel", "betmgm"
}

SPORTS_LIST = [
    "soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga","soccer_italy_serie_a",
    "soccer_france_ligue_one","soccer_uefa_champs_league", "basketball_nba", "basketball_euroleague",
    "icehockey_nhl", "tennis_atp", "tennis_wta", "mma_mixed_martial_arts", "cricket_t20"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS  = "eu,uk,us,au"

api_lock = threading.Lock()

# ─── State Management ──────────────────────────────────────────────────────────
def load_state():
    defaults = {"active_index": 0, "remaining_requests": 500, "used_today": 0, "last_reset": str(datetime.utcnow().date())}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── API Key Rotation Engine ───────────────────────────────────────────────────
def get_active_key(state: dict):
    with api_lock:
        idx = state.get("active_index", 0)
        if idx >= len(API_KEYS):
            return None, idx
        return API_KEYS[idx], idx

def rotate_key(failed_idx: int, state: dict):
    with api_lock:
        current_idx = state.get("active_index", 0)
        if current_idx == failed_idx:
            state["active_index"] = current_idx + 1
            save_state(state)
            log.info(f"🔄 RATE LIMIT HIT: Rotated from Key #{failed_idx + 1} to Key #{failed_idx + 2}")
        return state.get("active_index", 0) < len(API_KEYS)

# ─── The Odds API ──────────────────────────────────────────────────────────────
def fetch_sport_odds(sport: str, market: str, state: dict) -> list:
    while True:
        key, idx = get_active_key(state)
        if not key:
            log.error("❌ All 19 API keys exhausted!")
            return []
            
        url = f"{ODDS_BASE}/sports/{sport}/odds"
        params = {"apiKey": key, "regions": REGIONS, "markets": market, "oddsFormat": "decimal"}
        
        try:
            r = requests.get(url, params=params, timeout=15)
            
            if "X-Requests-Remaining" in r.headers:
                state["remaining_requests"] = int(r.headers["X-Requests-Remaining"])
            if "X-Requests-Used" in r.headers:
                state["used_today"] = int(r.headers["X-Requests-Used"])
                
            if r.status_code == 200:
                data = r.json()
                filtered = []
                for event in data:
                    bms = [b for b in event.get("bookmakers", []) if b["key"] in ALLOWED_BOOKS]
                    if bms:
                        event["bookmakers"] = bms
                        filtered.append(event)
                return filtered
                
            elif r.status_code in [401, 429]:
                # 🛠️ THE FIX: Rotate key and try again!
                if rotate_key(idx, state):
                    continue
                else:
                    return []
            else:
                log.warning(f"⚠️ API Error {r.status_code} on {sport}")
                return []
                
        except Exception as e:
            log.error(f"Fetch error for {sport}: {e}")
            return []

def fetch_all_odds(state: dict) -> list:
    if not API_KEYS:
        log.error("API Keys missing! Skipping Odds API fetch.")
        return []
        
    all_events = []
    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    log.info(f"Fetching {len(tasks)} sport/market combos concurrently using Key #{state.get('active_index', 0) + 1}...")
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_sport_odds, s, m, state): (s, m) for s, m in tasks}
        for fut in as_completed(futures):
            all_events.extend(fut.result())
    return all_events

# ─── BC.Game Scraper ───────────────────────────────────────────────────────────
def fetch_bcgame_events() -> list:
    # 🛠️ THE FIX: Stealth headers to bypass GitHub Actions IP bans
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://bc.game',
        'Referer': 'https://bc.game/'
    }
    
    try:
        r = requests.get(BCGAME_URL, headers=headers, timeout=20)
        if r.status_code != 200:
            log.warning(f"BC.Game returned HTTP {r.status_code}. They might be blocking GitHub.")
            return []
        raw = r.json()
        events_raw = []
        if isinstance(raw, dict):
            for key in ["data", "events", "list", "items"]:
                if key in raw:
                    events_raw = raw[key]
                    break
        elif isinstance(raw, list):
            events_raw = raw

        converted = []
        for ev in events_raw[:150]:
            try:
                home = ev.get("homeTeam", ev.get("home", ev.get("team1", "")))
                away = ev.get("awayTeam", ev.get("away", ev.get("team2", "")))
                sport = ev.get("sportName", ev.get("sport", "Unknown"))
                commence = ev.get("startTime", ev.get("startAt", ev.get("time", "")))
                markets = ev.get("markets", ev.get("odds", []))

                outcomes = []
                if isinstance(markets, list):
                    for mkt in markets:
                        if isinstance(mkt, dict):
                            for o in mkt.get("outcomes", mkt.get("selections", [])):
                                outcomes.append({
                                    "name": o.get("name", o.get("label", "")),
                                    "price": float(o.get("price", o.get("odds", 2.0)))
                                })

                if home and away and outcomes:
                    converted.append({
                        "id": f"bcgame_{hash(home+away)}",
                        "sport_title": sport,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": str(commence),
                        "bookmakers": [{
                            "key": "bcgame",
                            "title": "BC.Game",
                            "last_update": str(datetime.now(timezone.utc).isoformat()),
                            "markets": [{"key": "h2h", "outcomes": outcomes}]
                        }]
                    })
            except Exception:
                continue
        log.info(f"BC.Game: {len(converted)} raw events parsed.")
        return converted
    except Exception as e:
        log.error(f"BC.Game fetch failed: {e}")
        return []

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    merged_count = 0
    for bc_ev in bc_events:
        bc_home, bc_away = bc_ev["home_team"], bc_ev["away_team"]
        best_match, best_score = None, 0.0
        for ev in odds_events:
            score = (similarity(bc_home, ev.get("home_team", "")) + similarity(bc_away, ev.get("away_team", ""))) / 2
            if score > best_score:
                best_score, best_match = score, ev
        if best_score > 0.72 and best_match:
            best_match["bookmakers"].extend(bc_ev["bookmakers"])
            merged_count += 1
        else:
            odds_events.append(bc_ev)
    log.info(f"BC.Game merge: {merged_count} integrated, rest independent.")
    return odds_events

# ─── Quant Math Engine ─────────────────────────────────────────────────────────
def remove_vig_pinnacle(pinnacle_outcomes: list) -> dict:
    raw_probs = {o["name"]: 1.0 / o["price"] for o in pinnacle_outcomes}
    total_prob = sum(raw_probs.values())
    if total_prob <= 0: return {}
    return {name: p / total_prob for name, p in raw_probs.items()}

def kelly_stake(edge: float, odds: float, bank: float) -> float:
    b = odds - 1.0
    if b <= 0: return 0.0
    p = 1.0 / (odds / (1.0 + edge)) 
    kelly_full = (b * p - (1.0 - p)) / b
    if kelly_full <= 0: return 0.0
    return round(KELLY_FRACTION * kelly_full * bank, 2)

def calculate_arb_stakes(odds_list: list, total_stake: float = 1000.0) -> list:
    total_implied = sum(1.0 / o for o in odds_list)
    if total_implied >= 1.0: return [0.0] * len(odds_list)
    return [(1.0 / o) / total_implied * total_stake for o in odds_list]

def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        for market_key in MARKETS:
            best_odds = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt["key"] != market_key: continue
                    for outcome in mkt.get("outcomes", []):
                        name = outcome.get("name", "")
                        if outcome.get("point") is not None:
                            name = f"{name}_{abs(float(outcome['point']))}"
                        price = float(outcome.get("price", 0))
                        if price > 1.0 and (name not in best_odds or price > best_odds[name][0]):
                            best_odds[name] = (price, bm["title"], bm["key"])

            outcomes_list = list(best_odds.items())
            if len(outcomes_list) < 2: continue

            has_draw = any('draw' in c[0].lower() for c in outcomes_list)
            
            # 2-WAY
            if len(outcomes_list) == 2 and not has_draw:
                names, prices = [c[0] for c in outcomes_list], [c[1][0] for c in outcomes_list]
                total_impl = sum(1.0/p for p in prices)
                if total_impl < 1.0 and (1.0/total_impl - 1.0) >= MIN_ARB_PROFIT:
                    stakes = calculate_arb_stakes(prices, 1000)
                    arbs.append({
                        "type": "2-WAY ARB", "market": market_key.upper(), "sport": ev.get("sport_title", "Unknown"),
                        "match": f"{ev.get('home_team')} vs {ev.get('away_team')}", "commence": ev.get("commence_time"),
                        "outcomes": [{"name": n, "odds": p, "book_key": c[1][2], "stake_rounded": round(round(s/10)*10, 2)} for n,p,c,s in zip(names, prices, outcomes_list, stakes)],
                        "profit_pct": round((1.0/total_impl - 1.0) * 100, 3)
                    })
            
            # 3-WAY
            elif len(outcomes_list) == 3:
                names, prices = [c[0] for c in outcomes_list], [c[1][0] for c in outcomes_list]
                total_impl = sum(1.0/p for p in prices)
                if total_impl < 1.0 and (1.0/total_impl - 1.0) >= MIN_ARB_PROFIT:
                    stakes = calculate_arb_stakes(prices, 1000)
                    arbs.append({
                        "type": "3-WAY ARB", "market": market_key.upper(), "sport": ev.get("sport_title", "Unknown"),
                        "match": f"{ev.get('home_team')} vs {ev.get('away_team')}", "commence": ev.get("commence_time"),
                        "outcomes": [{"name": n, "odds": p, "book_key": c[1][2], "stake_rounded": round(round(s/10)*10, 2)} for n,p,c,s in zip(names, prices, outcomes_list, stakes)],
                        "profit_pct": round((1.0/total_impl - 1.0) * 100, 3)
                    })

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs

def scan_ev_bets(events: list) -> list:
    ev_bets = []
    for ev in events:
        for market_key in MARKETS:
            pin_outcomes = next((m.get("outcomes", []) for bm in ev.get("bookmakers", []) if bm["key"] == "pinnacle" for m in bm.get("markets", []) if m["key"] == market_key), None)
            if not pin_outcomes or len(pin_outcomes) < 2: continue
            
            true_probs = remove_vig_pinnacle(pin_outcomes)
            if not true_probs: continue

            for bm in ev.get("bookmakers", []):
                if bm["key"] == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m["key"] != market_key: continue
                    for o in m.get("outcomes", []):
                        if o.get("name") in true_probs and o.get("price", 0) > 1.0:
                            t_prob = true_probs[o["name"]]
                            edge = (o["price"] - (1.0/t_prob)) / (1.0/t_prob)
                            if edge >= MIN_EV_EDGE:
                                k_stake = kelly_stake(edge, o["price"], BANK_SIZE)
                                ev_bets.append({
                                    "market": market_key.upper(), "sport": ev.get("sport_title", "Unknown"),
                                    "match": f"{ev.get('home_team')} vs {ev.get('away_team')}", "commence": ev.get("commence_time"),
                                    "outcome": o["name"], "book_key": bm["key"], "offered_odds": o["price"],
                                    "edge_pct": round(edge * 100, 3), "kelly_stake_rounded": round(round(k_stake/10)*10, 2)
                                })
    ev_bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    return ev_bets

# ─── Dashboard HTML Generator ──────────────────────────────────────────────────
def generate_html(arbs: list, evs: list, raw_bc: list, state: dict) -> str:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p IST")
    pass_hash = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()
    
    # 🔒 Get the active key safely
    active_idx = state.get("active_index", 0)
    current_key = API_KEYS[active_idx] if active_idx < len(API_KEYS) else "DEPLETED"
    masked_key = f"{current_key[:4]}••••••••••••{current_key[-4:]}" if len(current_key) > 10 else "DEPLETED"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=0"/>
<title>Arb Sniper ⚡ Pro Terminal</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<style>
  :root {{ --bg0:#09090b; --bg1:#111113; --bg2:#18181b; --bg3:#27272a; --border:#3f3f46; --accent:#22d3ee; --accent2:#a78bfa; --green:#4ade80; --red:#f87171; --yellow:#fbbf24; --txt:#e4e4e7; --txt2:#a1a1aa; --txt3:#71717a; --font:'JetBrains Mono',monospace; }}
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;700;800&display=swap');
  *{{box-sizing:border-box;margin:0;padding:0; -webkit-tap-highlight-color: transparent;}}
  body{{background:var(--bg0);color:var(--txt);font-family:var(--font);min-height:100vh;overflow-x:hidden}}

  #lockscreen{{position:fixed;inset:0;z-index:9999;background:var(--bg0);display:flex;align-items:center;justify-content:center;flex-direction:column;}}
  .lock-box{{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:30px;display:flex;flex-direction:column;align-items:center;gap:20px;width:90%;max-width:350px;}}
  #lock-input{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--txt);font-size:16px;padding:14px;width:100%;text-align:center;letter-spacing:4px;outline:none;}}
  #lock-btn{{background:var(--accent);color:#000;font-weight:700;border:none;border-radius:8px;padding:14px;width:100%;font-size:14px;cursor:pointer;}}

  #app{{display:none}}
  .topbar{{background:var(--bg1);border-bottom:1px solid var(--border);padding:15px 20px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;}}
  .logo{{font-family:'Syne',sans-serif;font-size:16px;font-weight:800;color:var(--accent);}}
  
  .main-tabs{{
    display:flex; gap:10px; padding:15px 20px; border-bottom:1px solid var(--border);
    overflow-x:auto; white-space:nowrap; -webkit-overflow-scrolling:touch; scrollbar-width:none;
  }}
  .main-tabs::-webkit-scrollbar{{display:none;}}
  .main-tab{{
    background:var(--bg2); border:1px solid var(--border); border-radius:8px;
    color:var(--txt2); font-family:var(--font); font-size:12px; padding:12px 18px;
    cursor:pointer; transition:all .2s; font-weight:bold; display:flex; align-items:center; gap:8px;
  }}
  .main-tab.active{{background:rgba(34,211,238,.1); color:var(--accent); border-color:var(--accent);}}
  .tab-content{{display:none; padding:20px;}}
  .tab-content.active{{display:block;}}

  .cards-grid{{display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:16px;}}
  .card{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;}}
  .card-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
  .badge{{font-size:10px;font-weight:700;padding:4px 8px;border-radius:6px;background:var(--bg3);color:var(--txt2);}}
  .profit{{font-family:'Syne',sans-serif;font-weight:800;font-size:18px;color:var(--green);}}
  .match-name{{font-size:14px;font-weight:700;margin-bottom:8px;color:var(--txt)}}
  .meta{{font-size:11px;color:var(--txt3);margin-bottom:12px;}}
  
  .table{{width:100%;border-collapse:collapse;font-size:12px;}}
  .table th{{color:var(--txt3);text-align:left;padding-bottom:8px;border-bottom:1px solid var(--border);font-weight:normal;}}
  .table td{{padding:8px 0;border-bottom:1px solid rgba(63,63,70,.4);}}
  .book-tag{{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:10px;}}
  .odds{{color:var(--yellow);font-weight:700;}}
  .stake{{font-weight:700;font-size:13px;float:right;}}

  .api-card{{background:linear-gradient(145deg, var(--bg2), var(--bg1)); border-left:4px solid var(--accent2);}}
  .api-stat-row{{display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border);font-size:13px;}}
  .api-stat-row:last-child{{border:none;}}
  .api-val{{font-weight:bold;color:var(--txt);}}
  
  /* Calculator Styles */
  .calc-input-group {{margin-bottom: 12px;}}
  .calc-input-group label {{display:block; font-size:11px; color:var(--txt3); margin-bottom:4px;}}
  .calc-input {{width:100%; background:var(--bg0); border:1px solid var(--border); color:var(--txt); padding:12px; border-radius:6px; font-family:var(--font); outline:none;}}
  .calc-input:focus {{border-color:var(--accent);}}
  .calc-btn {{width:100%; background:var(--accent); color:#000; font-weight:bold; border:none; padding:12px; border-radius:6px; margin-top:10px; cursor:pointer;}}
  .calc-res {{background:var(--bg0); border:1px solid var(--border); border-radius:6px; padding:15px; margin-top:15px; display:none;}}

  .empty-state{{text-align:center;padding:50px 20px;color:var(--txt3);grid-column:1/-1;}}
</style>
</head>
<body>

<div id="lockscreen">
  <div class="lock-box">
    <i class="fas fa-shield-halved" style="font-size:40px;color:var(--accent)"></i>
    <h2 style="font-family:'Syne',sans-serif;">ARB SNIPER</h2>
    <input id="lock-input" type="password" placeholder="PASSWORD" />
    <button id="lock-btn" onclick="checkPass()">ENTER</button>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> SNIPER v4.0</div>
    <div style="display:flex; gap:12px; align-items:center;">
      <div style="font-size:11px;color:var(--txt3)"><i class="fas fa-clock"></i> {ist_now}</div>
      <button onclick="logout()" style="background:none; border:none; color:var(--red); font-size:16px; cursor:pointer;" title="Logout"><i class="fas fa-right-from-bracket"></i></button>
    </div>
  </div>

  <div class="main-tabs">
    <button class="main-tab active" onclick="switchTab('arb')"><i class="fas fa-percent"></i> Arbs (<span id="count-arb">0</span>)</button>
    <button class="main-tab" onclick="switchTab('ev')"><i class="fas fa-chart-line"></i> +EV (<span id="count-ev">0</span>)</button>
    <button class="main-tab" onclick="switchTab('bc')"><i class="fas fa-gamepad"></i> BC.Game</button>
    <button class="main-tab" onclick="switchTab('calc')"><i class="fas fa-calculator"></i> Calculator</button>
    <button class="main-tab" onclick="switchTab('api')"><i class="fas fa-key"></i> System API</button>
  </div>

  <div id="tab-arb" class="tab-content active">
    <div class="cards-grid" id="grid-arb"></div>
  </div>

  <div id="tab-ev" class="tab-content">
    <div class="cards-grid" id="grid-ev"></div>
  </div>

  <div id="tab-bc" class="tab-content">
    <div style="margin-bottom:15px;font-size:12px;color:var(--txt3);">Raw uncalculated feeds directly from BC.Game.</div>
    <div class="cards-grid" id="grid-bc"></div>
  </div>

  <div id="tab-calc" class="tab-content">
    <div class="cards-grid">
      <div class="card">
        <h3 style="margin-bottom:15px; color:var(--accent);"><i class="fas fa-calculator"></i> 2-Way / 3-Way Arb Calc</h3>
        <div class="calc-input-group"><label>Total Investment (₹)</label><input type="number" id="calc-bank" class="calc-input" value="10000" /></div>
        <div class="calc-input-group"><label>Odd 1</label><input type="number" id="calc-o1" class="calc-input" placeholder="e.g. 2.10" /></div>
        <div class="calc-input-group"><label>Odd 2</label><input type="number" id="calc-o2" class="calc-input" placeholder="e.g. 2.05" /></div>
        <div class="calc-input-group"><label>Odd 3 (Leave blank for 2-Way)</label><input type="number" id="calc-o3" class="calc-input" placeholder="e.g. 3.40 (Optional)" /></div>
        <button class="calc-btn" onclick="runCalc()"><i class="fas fa-play"></i> Calculate Profit</button>
        <div id="calc-res" class="calc-res"></div>
      </div>
    </div>
  </div>

  <div id="tab-api" class="tab-content">
    <div class="cards-grid">
      <div class="card api-card">
        <h3 style="margin-bottom:15px;font-family:'Syne';"><i class="fas fa-server"></i> The Odds API Telemetry</h3>
        <div class="api-stat-row"><span>Active API Key (Index #{active_idx + 1})</span> <span class="api-val" style="font-family:monospace;color:var(--accent2);">{masked_key}</span></div>
        <div class="api-stat-row"><span>Quota Remaining</span> <span class="api-val" style="color:var(--green);font-size:16px;">{state.get('remaining_requests', 0)}</span></div>
        <div class="api-stat-row"><span>Requests Used Today</span> <span class="api-val" style="color:var(--yellow);">{state.get('used_today', 0)}</span></div>
        <div class="api-stat-row"><span>Last Sync Time</span> <span class="api-val">{ist_now}</span></div>
      </div>
    </div>
  </div>
</div>

<script>
const PASS_HASH = "{pass_hash}";
const ARBS = {json.dumps(arbs)};
const EVS = {json.dumps(evs)};
const BC_RAW = {json.dumps(raw_bc)};

if (localStorage.getItem('sniper_auth') === PASS_HASH) {{
  document.getElementById('lockscreen').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  renderData();
}}

function checkPass() {{
  const val = document.getElementById('lock-input').value;
  if (CryptoJS.SHA256(val).toString() === PASS_HASH) {{
    localStorage.setItem('sniper_auth', PASS_HASH);
    document.getElementById('lockscreen').style.display='none';
    document.getElementById('app').style.display='block';
    renderData();
  }} else {{
    document.getElementById('lock-input').value='';
    alert('Invalid Password');
  }}
}}
document.getElementById('lock-input').addEventListener('keydown',e=>{{if(e.key==='Enter')checkPass()}});

function logout() {{
  localStorage.removeItem('sniper_auth');
  location.reload();
}}

function switchTab(tabId) {{
  document.querySelectorAll('.main-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  event.currentTarget.classList.add('active');
  document.getElementById('tab-'+tabId).classList.add('active');
}}

function fmtDate(d) {{ try {{ return new Date(d).toLocaleString('en-IN',{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}}); }} catch(e){{return d}} }}

function runCalc() {{
  const b = parseFloat(document.getElementById('calc-bank').value) || 10000;
  const o1 = parseFloat(document.getElementById('calc-o1').value) || 0;
  const o2 = parseFloat(document.getElementById('calc-o2').value) || 0;
  const o3 = parseFloat(document.getElementById('calc-o3').value) || 0;
  
  if (o1 <= 0 || o2 <= 0) return alert("Enter at least Odd 1 and Odd 2.");
  
  let totalImpl = (1/o1) + (1/o2);
  if(o3 > 0) totalImpl += (1/o3);
  
  const profitPct = (1/totalImpl - 1) * 100;
  const profitAmt = b * (1/totalImpl - 1);
  
  let resHTML = `<div class="api-stat-row"><span>Total Implied %</span><span class="api-val">${{(totalImpl*100).toFixed(2)}}%</span></div>`;
  resHTML += `<div class="api-stat-row"><span>Stake 1 (@${{o1}})</span><span class="api-val">₹${{((1/o1)/totalImpl * b).toFixed(2)}}</span></div>`;
  resHTML += `<div class="api-stat-row"><span>Stake 2 (@${{o2}})</span><span class="api-val">₹${{((1/o2)/totalImpl * b).toFixed(2)}}</span></div>`;
  if(o3 > 0) resHTML += `<div class="api-stat-row"><span>Stake 3 (@${{o3}})</span><span class="api-val">₹${{((1/o3)/totalImpl * b).toFixed(2)}}</span></div>`;
  
  if (totalImpl < 1) {{
    resHTML += `<div class="api-stat-row" style="border:none;margin-top:10px;"><span style="color:var(--green);font-weight:bold;">ARB FOUND!</span><span style="color:var(--green);font-weight:bold;font-size:16px;">+₹${{profitAmt.toFixed(2)}} (+${{profitPct.toFixed(2)}}%)</span></div>`;
  }} else {{
    resHTML += `<div class="api-stat-row" style="border:none;margin-top:10px;"><span style="color:var(--red);font-weight:bold;">NO ARB (Negative)</span><span style="color:var(--red);font-weight:bold;font-size:16px;">${{profitPct.toFixed(2)}}%</span></div>`;
  }}
  
  const resBox = document.getElementById('calc-res');
  resBox.innerHTML = resHTML;
  resBox.style.display = 'block';
}}

function renderData() {{
  document.getElementById('count-arb').textContent = ARBS.length;
  document.getElementById('count-ev').textContent = EVS.length;

  const arbGrid = document.getElementById('grid-arb');
  if(!ARBS.length) arbGrid.innerHTML = '<div class="empty-state"><i class="fas fa-ghost fa-2x"></i><p style="margin-top:10px">No Arbs Found</p></div>';
  else arbGrid.innerHTML = ARBS.map(a => `
    <div class="card" style="border-top:3px solid var(--green)">
      <div class="card-header"><span class="badge">${{a.type}}</span> <span class="profit">+${{a.profit_pct}}%</span></div>
      <div class="match-name">${{a.match}}</div>
      <div class="meta">${{fmtDate(a.commence)}} | ${{a.sport}}</div>
      <table class="table">
        ${{a.outcomes.map(o=>`<tr><td><span class="book-tag">${{o.book_key.toUpperCase().slice(0,4)}}</span> ${{o.name}}</td><td class="odds">${{o.odds}}</td><td><span class="stake">₹${{o.stake_rounded}}</span></td></tr>`).join('')}}
      </table>
    </div>`).join('');

  const evGrid = document.getElementById('grid-ev');
  if(!EVS.length) evGrid.innerHTML = '<div class="empty-state"><i class="fas fa-ghost fa-2x"></i><p style="margin-top:10px">No Value Bets Found</p></div>';
  else evGrid.innerHTML = EVS.map(e => `
    <div class="card" style="border-top:3px solid var(--accent)">
      <div class="card-header"><span class="badge">${{e.outcome}}</span> <span class="profit" style="color:var(--accent)">+${{e.edge_pct}}%</span></div>
      <div class="match-name">${{e.match}}</div>
      <div class="meta">${{fmtDate(e.commence)}} | ${{e.sport}}</div>
      <table class="table">
        <tr><td>Bookmaker</td><td style="text-align:right"><span class="book-tag">${{e.book_key.toUpperCase()}}</span></td></tr>
        <tr><td>Offered Odds</td><td class="odds" style="text-align:right">${{e.offered_odds}}</td></tr>
        <tr><td>Kelly Stake</td><td class="stake" style="color:var(--txt)">₹${{e.kelly_stake_rounded}}</td></tr>
      </table>
    </div>`).join('');

  const bcGrid = document.getElementById('grid-bc');
  if(!BC_RAW.length) bcGrid.innerHTML = '<div class="empty-state"><p>No BC.Game Data Available</p></div>';
  else bcGrid.innerHTML = BC_RAW.slice(0,50).map(b => {{
    const mkt = b.bookmakers[0].markets[0].outcomes;
    return `
    <div class="card">
      <div class="card-header"><span class="badge" style="background:#1e1e24;color:#4ade80"><i class="fas fa-gamepad"></i> BC.GAME</span></div>
      <div class="match-name">${{b.home_team}} vs ${{b.away_team}}</div>
      <div class="meta">${{fmtDate(b.commence_time)}} | ${{b.sport_title}}</div>
      <table class="table">
        ${{mkt.map(o=>`<tr><td>${{o.name}}</td><td class="odds" style="text-align:right">${{o.price}}</td></tr>`).join('')}}
      </table>
    </div>`
  }}).join('');
}}
</script>
</body></html>"""
    return html

# ─── Main Orchestrator ─────────────────────────────────────────────────────────
def main():
    log.info("╔══ ARB SNIPER v4.0 — Starting Run ══╗")
    state = load_state()
    
    odds_events = fetch_all_odds(state)
    
    bc_events = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events = merge_bcgame(odds_events, bc_events)
    
    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    html = generate_html(arbs, evs, raw_bc_copy, state)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
        
    log.info("╚══ Run Complete ══╝")

if __name__ == "__main__":
    main()
