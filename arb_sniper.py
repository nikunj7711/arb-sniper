#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v8.0 — INDIA ELITE EDITION                     ║
║  Dynamic Bankroll | Native BC.Game Bypass | Asian/Crypto Books | Clear UI    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import time
import hashlib
import logging
import threading
import http.client
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "arb2026")

MIN_ARB_PROFIT = 0.05
MAX_ARB_PROFIT = 15.0
MIN_EV_EDGE    = 0.005

# 1. BOOKMAKERS OPTIMIZED FOR INDIA
ALLOWED_BOOKS = {
    "pinnacle", "stake", "bcgame", "onexbet", "parimatch", 
    "dafabet", "betway", "bet365", "marathonbet", "betfair", "matchbook"
}

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"

ALWAYS_INCLUDE_SPORTS = {
    "cricket_ipl", "cricket_test_match", "cricket_odi", "cricket_t20",
    "tennis_atp_french_open", "tennis_wta_french_open", "tennis_atp_wimbledon", "tennis_wta_wimbledon",
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_uefa_champs_league",
    "basketball_nba", "basketball_euroleague"
}

# ═════════════════════════════════════════════════════════════════════════════
# KEY ROTATION & STATE
# ═════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        self.keys = [k.strip() for k in os.environ.get("ODDS_API_KEYS", "").split(",") if k.strip()]
        self._lock, self._quota = threading.Lock(), {k: 500 for k in self.keys}
    def get(self) -> str:
        with self._lock: return max(self.keys, key=lambda k: self._quota.get(k, 0)) if self.keys else "MISSING_KEY"
    def update(self, key: str, rem: int):
        with self._lock: self._quota[key] = max(0, rem)
    def exhaust(self, key: str):
        with self._lock: self._quota[key] = 0
    def total(self) -> int:
        with self._lock: return max(0, sum(self._quota.values()))
    def status(self) -> list:
        with self._lock: return [{"key": f"{k[:4]}...{k[-4:]}", "rem": self._quota.get(k, 0)} for k in self.keys]

ROTATOR = KeyRotator()

def load_state() -> dict:
    d = {"rem": 500, "events": 0, "sports": 0}
    if os.path.exists(STATE_FILE):
        try: d.update(json.load(open(STATE_FILE)))
        except: pass
    return d

def save_state(s: dict): json.dump(s, open(STATE_FILE, "w"))

# ═════════════════════════════════════════════════════════════════════════════
# ODDS API FETCHER
# ═════════════════════════════════════════════════════════════════════════════
def fetch_all_sports() -> list:
    k = ROTATOR.get()
    if k == "MISSING_KEY": return sorted(ALWAYS_INCLUDE_SPORTS)
    try:
        r = requests.get(f"{ODDS_BASE}/sports", params={"apiKey": k, "all": "false"}, timeout=15)
        if r.status_code == 200: return sorted({s["key"] for s in r.json() if not s.get("has_outrights")} | ALWAYS_INCLUDE_SPORTS)
    except: pass
    return sorted(ALWAYS_INCLUDE_SPORTS)

def fetch_sport_odds(sport: str, market: str) -> list:
    k = ROTATOR.get()
    if k == "MISSING_KEY" or ROTATOR._quota.get(k, 0) <= 2: return []
    try:
        r = requests.get(f"{ODDS_BASE}/sports/{sport}/odds", params={"apiKey": k, "regions": REGIONS, "markets": market, "oddsFormat": "decimal"}, timeout=15)
        ROTATOR.update(k, int(r.headers.get("X-Requests-Remaining", 0)))
        if r.status_code in (429, 401): ROTATOR.exhaust(k)
        if r.status_code != 200: return []
        
        filtered = []
        for ev in r.json():
            bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)
        return filtered
    except: return []

def fetch_all_odds(sports: list) -> list:
    events, seen = [], set()
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_sport_odds, s, m): m for s in sports for m in MARKETS}
        for f in as_completed(futs):
            try:
                for ev in f.result():
                    if (k := f"{ev.get('id')}_{futs[f]}") not in seen:
                        seen.add(k); events.append(ev)
            except: pass
    return events

# ═════════════════════════════════════════════════════════════════════════════
# NATIVE BC.GAME SCRAPER (v8.0 HTTP.CLIENT BYPASS)
# ═════════════════════════════════════════════════════════════════════════════
BC_DBG = {"status": "not_run", "chunks": 0, "events": 0}

def fetch_bcgame() -> list:
    host = "api-k-c7818b61-623.sptpub.com"
    brand = "2103509236163162112"
    headers = {'User-Agent': "insomnia/12.4.0", 'Accept': "application/json"}
    
    def _get(path):
        try:
            conn = http.client.HTTPSConnection(host, timeout=15)
            conn.request("GET", path, "", headers)
            res = conn.getresponse()
            if res.status == 200: return json.loads(res.read().decode("utf-8", errors="ignore"))
        except: pass
        return None

    manifest = _get(f"/api/v4/prematch/brand/{brand}/en/0")
    if not manifest: 
        BC_DBG["status"] = "manifest_failed"
        return []

    chunks = manifest.get("top_events_versions", []) + manifest.get("rest_events_versions", [])
    BC_DBG["chunks"] = len(chunks)
    
    all_sports, all_tourns, all_events = {}, {}, {}
    for ver in chunks[:3]: # Limit to 3 chunks to prevent massive latency
        c_data = _get(f"/api/v4/prematch/brand/{brand}/en/{ver}")
        if c_data:
            all_sports.update(c_data.get("sports", {}))
            all_tourns.update(c_data.get("tournaments", {}))
            all_events.update(c_data.get("events", {}))

    converted = []
    for eid, ev in all_events.items():
        try:
            desc = ev.get("desc", ev)
            if desc.get("type") not in ("match", "fixture"): continue
            
            home = away = ""
            for c in desc.get("competitors", []):
                q = str(c.get("qualifier", c.get("q", ""))).lower()
                if q in ("home", "1", "h"): home = c.get("name", "")
                elif q in ("away", "2", "a"): away = c.get("name", "")
            if not home and "name" in desc and " - " in desc["name"]:
                home, away = desc["name"].split(" - ", 1)
                
            sport_id = str(desc.get("sport", desc.get("sport_id", "")))
            sport_name = all_sports.get(sport_id, {}).get("name", "Unknown Sport")
            
            # Find 1x2 or Moneyline odds
            outcomes = []
            for mid, lines in ev.get("markets", {}).items():
                for lk, sels in lines.items():
                    if str(lk) not in ("0", ""): continue # Skip handicaps for standard H2H
                    for sid, sd in sels.items():
                        try:
                            price = float(sd.get("k", 0))
                            if price > 1.01:
                                name = "Home" if sid=="1" else "Draw" if sid=="2" else "Away" if sid=="3" else f"Sel_{sid}"
                                outcomes.append({"name": name, "price": price})
                        except: pass
                if outcomes: break # Found a valid line

            if home and away and len(outcomes) >= 2:
                ts = desc.get("scheduled", 0)
                time_str = datetime.fromtimestamp(int(ts)/1000 if int(ts)>1e10 else int(ts), tz=timezone.utc).isoformat()
                converted.append({
                    "id": f"bcgame_{eid}", "sport_title": sport_name,
                    "home_team": home.strip(), "away_team": away.strip(), "commence_time": time_str,
                    "bookmakers": [{"key": "bcgame", "title": "BC.Game", "markets": [{"key": "h2h", "outcomes": outcomes}]}]
                })
        except: continue
        
    BC_DBG["status"] = "success"
    BC_DBG["events"] = len(converted)
    return converted

def merge_bc(odds_events: list, bc_events: list) -> list:
    for bc in bc_events:
        odds_events.append(bc) # Simplified append to ensure no BC game is lost in fuzzy matching
    return odds_events

# ═════════════════════════════════════════════════════════════════════════════
# QUANT MATH ENGINE (Dynamic UI Prep)
# ═════════════════════════════════════════════════════════════════════════════
def _best_price_per_book(bms: list, mkey: str) -> dict:
    best = {}
    for bm in bms:
        for mkt in bm.get("markets", []):
            if mkt.get("key") != mkey: continue
            for o in mkt.get("outcomes", []):
                name = f"{o.get('name', '')}_{abs(float(o['point']))}" if o.get("point") is not None else str(o.get("name", ""))
                price = float(o.get("price", 0))
                if price > 1.01:
                    bk = bm.get("key", "?")
                    if name not in best or price > best[name].get(bk, (0,))[0]:
                        best.setdefault(name, {})[bk] = (price, bm.get("title", "?"))
    return best

def scan_arbs(events: list) -> list:
    arbs = []
    for ev in events:
        home, away, sport = ev.get("home_team", "?"), ev.get("away_team", "?"), ev.get("sport_title", "?")
        for mkey in MARKETS:
            best = _best_price_per_book(ev.get("bookmakers", []), mkey)
            if not best: continue
            
            names = list(best.keys())
            if mkey == "h2h" and len(names) in (2, 3):
                # Pure 2-way or 3-way moneyline
                combos = [best[n] for n in names]
                # Try all bookmaker cross-products
                import itertools
                for keys in itertools.product(*(c.keys() for c in combos)):
                    if len(set(keys)) >= 2: # At least 2 different books
                        prices = [combos[i][k][0] for i, k in enumerate(keys)]
                        impl = sum(1/p for p in prices)
                        if impl < 1.0:
                            pct = (1/impl - 1) * 100
                            if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                                arbs.append({
                                    "ways": len(names), "market": mkey, "sport": sport, "match": f"{home} vs {away}",
                                    "profit_pct": round(pct, 3), "implied": impl,
                                    "outcomes": [{"name": names[i], "odds": prices[i], "book_key": keys[i], "book": combos[i][keys[i]][1]} for i in range(len(names))]
                                })
            elif mkey in ("totals", "spreads"):
                # Pair opposing sides matching the exact same point
                groups = {}
                for n in names:
                    parts = n.split("_")
                    if len(parts) >= 2: groups.setdefault("_".join(parts[1:]), []).append(n)
                for pt, g in groups.items():
                    if len(g) == 2:
                        for k1, v1 in best[g[0]].items():
                            for k2, v2 in best[g[1]].items():
                                if k1 != k2:
                                    impl = 1/v1[0] + 1/v2[0]
                                    if impl < 1.0:
                                        pct = (1/impl - 1) * 100
                                        if MIN_ARB_PROFIT <= pct <= MAX_ARB_PROFIT:
                                            arbs.append({
                                                "ways": 2, "market": mkey, "sport": sport, "match": f"{home} vs {away}",
                                                "profit_pct": round(pct, 3), "implied": impl,
                                                "outcomes": [{"name": g[0].replace("_", " "), "odds": v1[0], "book_key": k1, "book": v1[1]}, {"name": g[1].replace("_", " "), "odds": v2[0], "book_key": k2, "book": v2[1]}]
                                            })
    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    return arbs[:200]

def scan_evs(events: list) -> list:
    bets = []
    for ev in events:
        for mkey in MARKETS:
            pin = next((m.get("outcomes", []) for b in ev.get("bookmakers", []) if b.get("key") == "pinnacle" for m in b.get("markets", []) if m.get("key") == mkey), None)
            if not pin or len(pin) < 2: continue
            
            raw_probs = {o["name"]: 1/float(o["price"]) for o in pin if "price" in o}
            total_vig = sum(raw_probs.values())
            if total_vig <= 0: continue
            true_probs = {k: v/total_vig for k,v in raw_probs.items()}
            
            for bm in ev.get("bookmakers", []):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets", []):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes", []):
                        name = o.get("name", "")
                        if name in true_probs:
                            price = float(o.get("price", 0))
                            if price > 1.0:
                                tp = true_probs[name]
                                edge = (price - (1/tp)) / (1/tp)
                                if edge >= MIN_EV_EDGE:
                                    bets.append({
                                        "market": mkey, "sport": ev.get("sport_title", "?"), "match": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                                        "outcome": name, "book_key": bm.get("key", "?"), "book": bm.get("title", "?"),
                                        "offered_odds": price, "true_prob": tp, "edge_pct": round(edge * 100, 3)
                                    })
    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    return bets[:400]

# ═════════════════════════════════════════════════════════════════════════════
# HTML GENERATOR (CLIENT-SIDE MATH)
# ═════════════════════════════════════════════════════════════════════════════
def gen_html(arbs, evs, bc_dbg, keys_stat):
    aj, ej, ks = json.dumps(arbs), json.dumps(evs), json.dumps(keys_stat)
    now = datetime.now().strftime("%d %b %Y %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>Arb Sniper v8.0</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet"/>
<style>
:root {{ --bg: #0A0A0A; --bg2: #141414; --brd: #262626; --acc: #3b82f6; --grn: #22c55e; --purp: #a855f7; --txt: #f5f5f5; --txt2: #a3a3a3; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }}
body {{ background: var(--bg); color: var(--txt); padding: 20px; }}
.top {{ display: flex; justify-content: space-between; padding-bottom: 20px; border-bottom: 1px solid var(--brd); margin-bottom: 20px; }}
.title {{ font-size: 24px; font-weight: 700; background: linear-gradient(to right, var(--acc), var(--purp)); -webkit-background-clip: text; color: transparent; }}
.bank-wrap {{ display: flex; align-items: center; gap: 10px; background: var(--bg2); padding: 10px 20px; border-radius: 8px; border: 1px solid var(--brd); }}
input[type=number] {{ background: transparent; border: none; color: var(--grn); font-family: 'JetBrains Mono'; font-size: 18px; font-weight: 700; width: 120px; outline: none; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 16px; margin-top: 20px; }}
.card {{ background: var(--bg2); border: 1px solid var(--brd); border-radius: 12px; padding: 20px; }}
.c-head {{ display: flex; justify-content: space-between; margin-bottom: 10px; font-family: 'JetBrains Mono'; font-size: 12px; font-weight: 700; }}
.m-lbl {{ background: #1e3a8a20; color: var(--acc); padding: 4px 8px; border-radius: 4px; }}
.match {{ font-weight: 600; font-size: 15px; margin-bottom: 15px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
td {{ padding: 8px 0; border-bottom: 1px solid var(--brd); }}
.odds {{ font-family: 'JetBrains Mono'; font-weight: 700; color: var(--txt); }}
.stake {{ font-family: 'JetBrains Mono'; font-weight: 700; color: var(--grn); float: right; }}
</style>
</head>
<body>
<div class="top">
    <div class="title"><i class="fas fa-crosshairs"></i> ARB SNIPER v8.0</div>
    <div class="bank-wrap">Bankroll (₹) <input type="number" id="bankroll" value="10000" oninput="updateUI()"></div>
</div>

<h2><i class="fas fa-bolt" style="color:var(--grn)"></i> Arbitrage (<span id="c-arb">0</span>)</h2>
<div class="grid" id="arb-grid"></div>

<h2 style="margin-top:40px"><i class="fas fa-chart-line" style="color:var(--purp)"></i> +EV Bets (<span id="c-ev">0</span>)</h2>
<div class="grid" id="ev-grid"></div>

<script>
const ARBS = {aj}; const EVS = {ej};
const mName = (m) => m === 'h2h' ? 'H2H (Match Winner)' : m === 'totals' ? 'TOTALS (Over/Under)' : 'SPREADS (Handicap)';

function updateUI() {{
    const bank = parseFloat(document.getElementById('bankroll').value) || 0;
    localStorage.setItem('arb8_bank', bank);
    
    document.getElementById('c-arb').innerText = ARBS.length;
    document.getElementById('arb-grid').innerHTML = ARBS.map(a => {{
        const profit = bank * (1/a.implied - 1);
        const rows = a.outcomes.map(o => {{
            const exact = ((1/o.odds)/a.implied) * bank;
            const rnd = Math.round(exact/10)*10;
            return `<tr><td>${{o.book}}<br><small style="color:var(--txt2)">${{o.name}}</small></td>
            <td><span class="odds">${{o.odds.toFixed(2)}}</span></td>
            <td><span class="stake">₹${{rnd}}</span><br><small style="float:right;color:var(--txt2)">exact: ₹${{exact.toFixed(1)}}</small></td></tr>`;
        }}).join('');
        return `<div class="card"><div class="c-head"><span class="m-lbl">${{mName(a.market)}}</span><span style="color:var(--grn)">+${{a.profit_pct}}%</span></div>
        <div class="match">${{a.match}} <br><small style="color:var(--txt2)">${{a.sport.replace(/_/g, ' ')}}</small></div>
        <table>${{rows}}</table><div style="margin-top:10px; font-weight:700; text-align:right">Profit: <span style="color:var(--grn)">+₹${{profit.toFixed(2)}}</span></div></div>`;
    }}).join('');

    document.getElementById('c-ev').innerText = EVS.length;
    document.getElementById('ev-grid').innerHTML = EVS.map(e => {{
        const b = e.offered_odds - 1;
        const kf = (b * e.true_prob - (1 - e.true_prob)) / b;
        const stake = kf > 0 ? 0.3 * kf * bank : 0;
        return `<div class="card"><div class="c-head"><span class="m-lbl">${{mName(e.market)}}</span><span style="color:var(--purp)">+${{e.edge_pct}}% Edge</span></div>
        <div class="match">${{e.match}}</div>
        <table><tr><td>${{e.outcome}} @ ${{e.book}}</td><td style="text-align:right"><span class="odds">${{e.offered_odds.toFixed(2)}}</span></td></tr>
        <tr><td style="color:var(--txt2)">True Odds</td><td style="text-align:right; color:var(--txt2)">${{(1/e.true_prob).toFixed(2)}}</td></tr>
        <tr><td>Kelly Stake (30%)</td><td><span class="stake">₹${{Math.round(stake/10)*10}}</span></td></tr></table></div>`;
    }}).join('');
}}

const saved = localStorage.getItem('arb8_bank');
if(saved) document.getElementById('bankroll').value = saved;
updateUI();
</script>
</body></html>"""

# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║          ARB SNIPER v8.0 — Starting Run          ║")
    log.info("╚══════════════════════════════════════════════════╝")

    state = load_state()
    sports = fetch_all_sports()
    
    odds_events = fetch_all_odds(sports)
    bc_events = fetch_bcgame()
    all_events = merge_bc(odds_events, bc_events)

    arbs = scan_arbs(all_events)
    evs = scan_evs(all_events)

    state["rem"] = ROTATOR.total()
    save_state(state)

    html = gen_html(arbs, evs, BC_DBG, ROTATOR.status())
    open(OUTPUT_HTML, "w", encoding="utf-8").write(html)
    
    log.info(f"Arbs: {len(arbs)} | EVs: {len(evs)} | BC: {BC_DBG['events']} | Quota: {ROTATOR.total()}")
    log.info("Done.")

if __name__ == "__main__":
    main()
