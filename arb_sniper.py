import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION (YOUR RULES)
# ==========================================
# This pulls your 19 keys from GitHub securely
_raw_keys = os.getenv('ODDS_API_KEYS', '')
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' # Your phone's alert channel
TOTAL_BANKROLL = 1500                   # Your total betting budget in Rupees
MIN_EV_THRESHOLD = 1.5                  # Only alert if the Expected Value is > 1.5%
MIN_ARB_THRESHOLD = 0.5                 # UPDATED: Lowered to 0.5% to catch high-frequency glitches

# The specific bookmakers we want to scan and compare via API
MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'

# The sports we are scanning
TARGET_SPORTS = [
    'soccer_epl', 
    'soccer_spain_la_liga', 
    'soccer_uefa_champs_league',
    'basketball_nba', 
    'icehockey_nhl', 
    'tennis_atp'
]

# Maximum amount you are allowed to bet on each specific site
BOOK_CAPS = {
    'betway': 300, 'stake': 500, 'onexbet': 400, 'marathonbet': 400,  
    'pinnacle': 1000, 'bet365': 400, 'unibet': 350, 'bcgame': 1000 # Added BC.Game cap
}

# ==========================================
#  2. MEMORY & AUTO-RECOVERY
# ==========================================
api_lock = threading.Lock()

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return json.load(f)
        except: return default
    return default

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2)
    except: pass

api_state = load_json('api_state.json', {})
if 'active_index' not in api_state: api_state['active_index'] = 0
if 'stats' not in api_state: api_state['stats'] = {}

if len(API_KEYS) > 0 and api_state['active_index'] >= len(API_KEYS):
    print("🛡️ Auto-Recovery Activated: Resetting Key Index to 0.")
    api_state['active_index'] = 0
    api_state['stats'] = {}

alert_cache = load_json('alert_cache.json', {})
now_ts = time.time()
alert_cache = {k: v for k, v in alert_cache.items() if isinstance(v, (int, float)) and (now_ts - v < 6*3600)}

def is_duplicate_alert(alert_key):
    if alert_key in alert_cache: return True
    alert_cache[alert_key] = time.time()
    return False

def get_active_api_key():
    with api_lock:
        idx = api_state['active_index']
        if idx >= len(API_KEYS): return None, idx
        return API_KEYS[idx], idx

def rotate_api_key(failed_idx):
    with api_lock:
        if api_state['active_index'] == failed_idx:
            api_state['active_index'] += 1
            save_json('api_state.json', api_state)
            print(f"🔄 Key #{failed_idx + 1} Exhausted! Switching to Key #{api_state['active_index'] + 1}")
        return api_state['active_index'] < len(API_KEYS)

def update_key_telemetry(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  3. THE MATH ENGINE
# ==========================================
def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def calculate_kelly(soft_odds, true_odds, bankroll, bookie=None):
    b, p = soft_odds - 1.0, 1.0 / true_odds
    safe_kelly = ((b * p - (1-p)) / b) * 0.30 
    if safe_kelly <= 0: return 0
    stake = max(20, bankroll * min(safe_kelly, 0.05)) 
    return min(stake, BOOK_CAPS.get(bookie, 1000))

# ==========================================
#  4. CLOUD FETCHING (THE HUNTER)
# ==========================================

def fetch_bcgame_custom():
    """
    CUSTOM PIPELINE: Bypasses API limits to pull unlimited BC.Game odds.
    Translates the data into The-Odds-API format so your bot can read it instantly.
    """
    headers = {
        'accept': '*/*',
        'origin': 'https://bc.game',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    print("🥷 Injecting custom BC.Game Zero-Cost Pipeline...")
    
    try:
        # Step 1: Steal the active map version
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        version = requests.get(map_url, headers=headers, timeout=10).json()['top_events_versions'][0]
        
        # Step 2: Download the master database
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        standardized_events = []
        
        # Step 3: Translate BC.Game data into Odds-API format
        for event_id, match in events.items():
            desc = match.get('desc', {})
            markets = match.get('markets', {})
            
            comps = desc.get('competitors', [])
            if len(comps) < 2: continue
            home_team = comps[0].get('name')
            away_team = comps[1].get('name')
            
            commence_time = datetime.fromtimestamp(desc.get('scheduled', 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            translated_markets = []
            
            # Extract Match Winner (H2H - Market "1")
            h2h_outcomes = []
            market_1 = markets.get("1", {}).get("", {})
            if market_1:
                if "1" in market_1: h2h_outcomes.append({'name': home_team, 'price': float(market_1["1"]["k"])})
                if "2" in market_1: h2h_outcomes.append({'name': away_team, 'price': float(market_1["2"]["k"])})
                if "3" in market_1: h2h_outcomes.append({'name': 'Draw', 'price': float(market_1["3"]["k"])})
            if h2h_outcomes:
                translated_markets.append({'key': 'h2h', 'outcomes': h2h_outcomes})

            # Extract Totals (Over/Under - Market "18")
            market_18 = markets.get("18", {})
            total_outcomes = []
            for point_key, point_data in market_18.items():
                point_val = point_key.replace("total=", "")
                if "12" in point_data: total_outcomes.append({'name': 'Over', 'price': float(point_data["12"]["k"]), 'point': float(point_val)})
                if "13" in point_data: total_outcomes.append({'name': 'Under', 'price': float(point_data["13"]["k"]), 'point': float(point_val)})
            if total_outcomes:
                translated_markets.append({'key': 'totals', 'outcomes': total_outcomes})
            
            if translated_markets:
                standardized_events.append({
                    'home_team': home_team,
                    'away_team': away_team,
                    'commence_time': commence_time,
                    'bookmakers': [{
                        'key': 'bcgame',
                        'title': 'BC.Game',
                        'markets': translated_markets
                    }]
                })
                
        return standardized_events
    except Exception as e:
        print(f"⚠️ BC.Game Scraper Error: {e}")
        return []

def fetch_odds_with_retry(url, params):
    while True:
        key, idx = get_active_api_key()
        if not key: return None
        params['apiKey'] = key
        try:
            res = requests.get(url, params=params, timeout=15)
        except: return None
            
        rem = res.headers.get('x-requests-remaining')
        if rem: update_key_telemetry(idx, rem)

        if res.status_code in [401, 429]:
            if rotate_api_key(idx): continue
            else: return None
        elif res.status_code == 200:
            return res.json()
        return None

def fetch_all_sports():
    results = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(
            fetch_odds_with_retry, 
            f'https://api.the-odds-api.com/v4/sports/{sp}/odds', 
            {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals,spreads'} 
        ): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results

def format_ist_time(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%d %b, %I:%M %p")
    except: return "Unknown Time"

# ==========================================
#  5. DATA PROCESSING (FINDING THE GOLD)
# ==========================================
def process_markets(results):
    all_evs, all_arbs = [], []
    now_utc = datetime.now(timezone.utc)
    
    for sport, events in results.items():
        if not events: continue
        for event in events:
            is_live = False
            try:
                dt_commence = datetime.strptime(event['commence_time'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if dt_commence < now_utc: is_live = True
            except: pass
            
            home = event.get('home_team', 'Team A')
            away = event.get('away_team', 'Team B')
            match_name = f"{home} vs {away}"
            match_time = format_ist_time(event['commence_time'])
            
            ev_lines, arb_lines = {}, {}
            
            for bookie in event.get('bookmakers', []):
                b_name = bookie['key']
                for market in bookie.get('markets', []):
                    m_type = market['key'].upper()
                    for outcome in market['outcomes']:
                        name, price = outcome['name'], outcome['price']
                        
                        point = str(outcome.get('point', ''))
                        line_key = f"{m_type} {point}".strip()

                        if line_key not in ev_lines: ev_lines[line_key] = {'pin': {}, 'softs': {}}
                        if line_key not in arb_lines: arb_lines[line_key] = {}
                        
                        if b_name == 'pinnacle': ev_lines[line_key]['pin'][name] = price
                        else:
                            if name not in ev_lines[line_key]['softs']: ev_lines[line_key]['softs'][name] = {}
                            ev_lines[line_key]['softs'][name][b_name] = price
                                
                        if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                            arb_lines[line_key][name] = {'price': price, 'bookie': b_name}

            # --- EV EVALUATION (Value Betting) ---
            for lk, d in ev_lines.items():
                pinny, softs = d['pin'], d['softs']
                ways = len(pinny)
                if ways in [2, 3]: 
                    keys = list(pinny.keys())
                    true_odds_vals = remove_vig(*[pinny[k] for k in keys])
                    
                    for idx, side in enumerate(keys):
                        true_odds = true_odds_vals[idx]
                        if side in softs:
                            best_bk = max(softs[side], key=softs[side].get)
                            best_p = softs[side][best_bk]
                            if best_p > true_odds:
                                ev_pct = ((best_p / true_odds) - 1) * 100
                                if ev_pct >= MIN_EV_THRESHOLD:
                                    all_evs.append({
                                        'pct': ev_pct, 'match': match_name, 'home': home, 'away': away, 'time': match_time, 'sport': sport.replace('_', ' ').upper(),
                                        'line': lk, 'ways': ways, 'sel': side, 'odds': best_p, 'trueO': true_odds, 'bk': best_bk,
                                        'stk': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk),
                                        'conf': max(0, min(100, int((abs((1/best_p) - (1/true_odds)) / (1/true_odds)) * 500))),
                                        'is_live': is_live
                                    })

            # --- ARBITRAGE EVALUATION (Risk-Free Betting) ---
            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                ways = len(keys)
                
                if ways not in [2, 3]: 
                    continue 
                    
                margin = sum(1/outs[k]['price'] for k in keys)
                
                if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                    arb = {'pct': (1-margin)*100, 'match': match_name, 'home': home, 'away': away, 'time': match_time, 'sport': sport.replace('_', ' ').upper(), 'line': lk, 'ways': ways, 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': [], 'is_live': is_live}
                    for k in keys:
                        arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': (TOTAL_BANKROLL/margin)/outs[k]['price']})
                    all_arbs.append(arb)

    all_evs.sort(key=lambda x: x['pct'], reverse=True)
    all_arbs.sort(key=lambda x: x['pct'], reverse=True)
    return all_evs, all_arbs

# ==========================================
#  6. WEBSITE GENERATOR (VISUAL DASHBOARD)
# ==========================================
def generate_web(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    build_time = ist_now.strftime('%d %b %Y, %I:%M %p IST')
    
    net_html = ""
    for idx, key in enumerate(API_KEYS):
        masked = f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else "Invalid"
        rem = api_state.get('stats', {}).get(str(idx), {}).get('remaining', 500)
        status = "ACTIVE" if idx == api_state.get('active_index', 0) else ("EXHAUSTED" if rem == 0 else "STANDBY")
        color = "#06b6d4" if status == "ACTIVE" else ("#ef4444" if status == "EXHAUSTED" else "#10b981")
        net_html += f"""
        <div style="background:#18181b; border:1px solid #27272a; padding:15px; border-radius:10px; border-left:4px solid {color}; margin-bottom:10px;">
            <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
                <strong style="font-family:monospace; font-size:14px;">{masked}</strong>
                <span style="font-size:10px; font-weight:bold; color:{color};">{status}</span>
            </div>
            <div style="font-size:12px; color:#a1a1aa;">Remaining Quota: <strong style="color:#fff">{rem}/500</strong></div>
        </div>"""

    def build_ev_card(e):
        live_badge = '<span style="color:#ef4444; font-weight:bold; animation: pulse 1.5s infinite;">🔴 LIVE (In-Play)</span>' if e.get('is_live') else f"📅 {e['time']}"
        return f"""
        <div style="background:#18181b; border:1px solid #27272a; border-radius:12px; margin-bottom:15px; overflow:hidden;">
            <div style="padding:12px 15px; border-bottom:1px solid #27272a; background:rgba(6,182,212,0.05); display:flex; justify-content:space-between;">
                <span style="font-size:11px; color:#a1a1aa; font-weight:bold;">🏆 {e['sport']} &nbsp;|&nbsp; {live_badge}</span>
                <span style="color:#06b6d4; font-weight:800; font-family:monospace;">{e['pct']:.2f}% EV</span>
            </div>
            <div style="padding:15px;">
                <div style="font-size:16px; font-weight:800; margin-bottom:10px;">{e['home']} <span style="font-size:12px; color:#a1a1aa;">vs</span> {e['away']}</div>
                <div style="font-size:12px; color:#a1a1aa; margin-bottom:10px;">LINE: <strong style="color:#fff">{e['line']}</strong> ({e['ways']}-Way)</div>
                <div style="display:flex; justify-content:space-between; align-items:center; background:#09090b; padding:10px; border-radius:8px; border:1px solid #27272a;">
                    <div style="font-size:16px;">👉 Bet <strong style="color:#06b6d4;">{e['sel'].upper()} @ {e['odds']:.2f}</strong></div>
                    <div style="font-size:11px; background:#27272a; padding:4px 8px; border-radius:4px; color:#fff;">{e['bk'].title().replace('_',' ')}</div>
                </div>
                <div style="display:flex; justify-content:space-between; margin-top:10px; padding-top:10px; border-top:1px dashed #27272a;">
                    <div><span style="font-size:10px; color:#a1a1aa;">KELLY STAKE</span><br><strong style="color:#06b6d4; font-size:18px;">₹{e['stk']:.0f}</strong></div>
                    <div style="text-align:right;"><span style="font-size:10px; color:#a1a1aa;">TRUE ODDS | CONF</span><br><strong style="font-family:monospace;">{e['trueO']:.3f} | {e['conf']}/100</strong></div>
                </div>
            </div>
        </div>"""

    def build_arb_card(a):
        live_badge = '<span style="color:#ef4444; font-weight:bold; animation: pulse 1.5s infinite;">🔴 LIVE (In-Play)</span>' if a.get('is_live') else f"📅 {a['time']}"
        legs_html = ""
        for s in a['sides']:
            legs_html += f"""
            <div style="display:flex; justify-content:space-between; background:#09090b; padding:10px; border-radius:8px; border:1px solid #27272a; margin-bottom:5px;">
                <span>{s['sel'].upper()} @ <strong style="color:#f59e0b;">{s['pr']:.2f}</strong> <span style="font-size:10px; background:#27272a; padding:2px 6px; border-radius:4px;">{s['bk'].title().replace('_',' ')}</span></span>
                <strong style="color:#06b6d4;">₹{s['stk']:.0f}</strong>
            </div>"""
        return f"""
        <div style="background:#18181b; border:1px solid #27272a; border-radius:12px; margin-bottom:15px; overflow:hidden;">
            <div style="padding:12px 15px; border-bottom:1px solid #27272a; background:rgba(245,158,11,0.05); display:flex; justify-content:space-between;">
                <span style="font-size:11px; color:#a1a1aa; font-weight:bold;">🏆 {a['sport']} &nbsp;|&nbsp; {live_badge}</span>
                <span style="color:#f59e0b; font-weight:800; font-family:monospace;">{a['pct']:.2f}% ARB</span>
            </div>
            <div style="padding:15px;">
                <div style="font-size:16px; font-weight:800; margin-bottom:10px;">{a['home']} <span style="font-size:12px; color:#a1a1aa;">vs</span> {a['away']}</div>
                <div style="font-size:12px; color:#a1a1aa; margin-bottom:10px;">LINE: <strong style="color:#fff">{a['line']}</strong> ({a['ways']}-Way)</div>
                {legs_html}
                <div style="text-align:right; margin-top:10px;"><span style="font-size:12px; color:#a1a1aa;">GUARANTEED PROFIT:</span> <strong style="color:#10b981; font-size:20px;">₹{a['profit']:.0f}</strong></div>
            </div>
        </div>"""

    pre_evs = [e for e in evs if not e.get('is_live')]
    live_evs = [e for e in evs if e.get('is_live')]
    pre_arbs = [a for a in arbs if not a.get('is_live')]
    live_arbs = [a for a in arbs if a.get('is_live')]

    pre_ev_html = "".join([build_ev_card(e) for e in pre_evs[:50]]) or "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No Pre-Match EV edges detected.</div>"
    live_ev_html = "".join([build_ev_card(e) for e in live_evs[:50]]) or "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No Live EV edges detected.</div>"
    pre_arb_html = "".join([build_arb_card(a) for a in pre_arbs[:50]]) or "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No Pre-Match Arbitrage locks.</div>"
    live_arb_html = "".join([build_arb_card(a) for a in live_arbs[:50]]) or "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No Live Arbitrage locks.</div>"

    HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARB SNIPER | Auto-Terminal</title>
<style>
    body {{ background: #09090b; color: #f4f4f5; font-family: system-ui, -apple-system, sans-serif; padding: 15px; max-width: 800px; margin: auto; }}
    .hdr {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #27272a; padding-bottom: 15px; margin-bottom: 20px; }}
    .title {{ font-size: 24px; font-weight: 800; color: #06b6d4; margin:0; }}
    .subtitle {{ font-size: 11px; color: #a1a1aa; font-family: monospace; }}
    .time-badge {{ background: rgba(16,185,129,0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 12px; border-radius: 6px; font-size: 11px; font-weight: bold; font-family: monospace; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; background: #18181b; padding: 6px; border-radius: 10px; border: 1px solid #27272a; margin-bottom: 20px; }}
    .tab {{ flex: 1 1 20%; min-width: 110px; text-align: center; padding: 10px; border-radius: 6px; cursor: pointer; font-weight: bold; color: #a1a1aa; font-size: 12px; transition: 0.2s; }}
    .tab.active {{ background: #3f3f46; color: #fff; }}
    .pane {{ display: none; }} .pane.active {{ display: block; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
</style>
</head>
<body>
<div class="hdr">
    <div>
        <h1 class="title">⚡ ARB SNIPER</h1>
        <div class="subtitle">Cloud Scanning Active · Target: {len(API_KEYS)} Keys</div>
    </div>
    <div class="time-badge">SYNCED: {build_time}</div>
</div>
<div class="tabs">
    <div class="tab active" onclick="showPane('pre-ev')">💎 Pre-Match EV ({len(pre_evs)})</div>
    <div class="tab" onclick="showPane('live-ev')">🔴 Live EV ({len(live_evs)})</div>
    <div class="tab" onclick="showPane('pre-arb')">🔒 Pre-Match ARB ({len(pre_arbs)})</div>
    <div class="tab" onclick="showPane('live-arb')">🔴 Live ARB ({len(live_arbs)})</div>
    <div class="tab" onclick="showPane('net')">📡 Network</div>
</div>
<div id="pane-pre-ev" class="pane active">{pre_ev_html}</div>
<div id="pane-live-ev" class="pane">{live_ev_html}</div>
<div id="pane-pre-arb" class="pane">{pre_arb_html}</div>
<div id="pane-live-arb" class="pane">{live_arb_html}</div>
<div id="pane-net" class="pane">{net_html}</div>
<script>
    function showPane(p) {{
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.pane').forEach(t => t.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('pane-'+p).classList.add('active');
    }}
    setInterval(() => window.location.reload(true), 300000);
</script>
</body>
</html>"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(HTML)

# ==========================================
#  7. THE TRIGGER (RUNNING THE SHOW)
# ==========================================
if __name__ == "__main__":
    print(f"🚀 Cloud Engine Booting... Loaded {len(API_KEYS)} Keys.")
    
    # 1. Fetch data from The-Odds-API
    results = fetch_all_sports()
    
    # 2. INJECT BC.GAME CUSTOM DATA
    bc_data = fetch_bcgame_custom()
    if bc_data:
        results['BC_GAME_GLOBAL'] = bc_data # Plugs straight into your math engine!
        
    # 3. Process everything together
    evs, arbs = process_markets(results)
    
    # Send Arbitrage Alerts to your phone
    for a in arbs:
        alert_key = f"ARB|{a['match']}|{a['line']}|{a['pct']:.2f}"
        if not is_duplicate_alert(alert_key):
            time_str = "🔴 LIVE" if a.get('is_live') else f"📅 {a['time']}"
            msg = f"🏆 {a['sport']}\n{time_str}\n📈 {a['line']} ({a['ways']}-Way)\n\n"
            for s in a['sides']: 
                msg += f"🔵 ₹{s['stk']:.0f} on {s['sel'].upper()} @ {s['pr']:.2f} [{s['bk'].title()}]\n"
            msg += f"\n✨ Profit: ₹{a['profit']:.0f}"
            
            requests.post("https://ntfy.sh/", 
                          json={"topic": NTFY_CHANNEL, "message": msg, "title": f"🚨 {a['pct']:.2f}% ARB | {a['match']}", "tags": ["moneybag","gem"]},
                          timeout=5)

    # Send EV Alerts to your phone
    for e in evs:
        alert_key = f"EV|{e['match']}|{e['line']}|{e['sel']}|{e['odds']:.2f}"
        if not is_duplicate_alert(alert_key):
            time_str = "🔴 LIVE" if e.get('is_live') else f"📅 {e['time']}"
            msg = f"🏆 {e['sport']}\n{time_str}\n📈 {e['line']} ({e['ways']}-Way)\n\n"
            msg += f"💰 BET: ₹{e['stk']:.0f}\n"
            msg += f"👉 {e['sel'].upper()} @ {e['odds']:.2f} on {e['bk'].title()}\n\n"
            msg += f"🧠 True Odds: {e['trueO']:.3f}"
            
            requests.post("https://ntfy.sh/", 
                          json={"topic": NTFY_CHANNEL, "message": msg, "title": f"📈 {e['pct']:.2f}% EV | {e['match']}", "tags": ["chart_with_upwards_trend","star"]},
                          timeout=5)

    save_json('api_state.json', api_state)
    save_json('alert_cache.json', alert_cache)
    generate_web(evs, arbs)
    print(f"✅ Global Terminal Synced. EV: {len(evs)} | ARB: {len(arbs)}")
