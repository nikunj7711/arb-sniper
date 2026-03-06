import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  CONFIGURATION
# ==========================================
_raw_keys = os.getenv('ODDS_API_KEYS', '')
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026'
TOTAL_BANKROLL = 1500
MIN_EV_THRESHOLD = 1.5
MIN_ARB_THRESHOLD = 1.0

MY_BOOKIES = 'pinnacle,onexbet,marathonbet,dafabet,stake,betfair_ex_eu,betway'
TARGET_SPORTS = ['soccer_epl', 'soccer_uefa_champs_league', 'basketball_nba', 'icehockey_nhl', 'tennis_atp', 'tennis_wta']
BOOK_CAPS = {'betway': 300, 'stake': 500, 'onexbet': 400, 'marathonbet': 400, 'dafabet': 350, 'betfair_ex_eu': 600, 'pinnacle': 1000}

# ==========================================
#  STATE & CACHE MANAGERS
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

api_state = load_json('api_state.json', {'active_index': 0, 'stats': {}})
alert_cache = load_json('alert_cache.json', {})

# Clean old cache (6 hours)
now_ts = time.time()
alert_cache = {k: v for k, v in alert_cache.items() if isinstance(v, (int, float)) and (now_ts - v < 6*3600)}

def is_duplicate_alert(alert_key):
    if alert_key in alert_cache: return True
    alert_cache[alert_key] = time.time()
    return False

def get_active_api_key():
    with api_lock:
        idx = api_state.get('active_index', 0)
        if idx >= len(API_KEYS): return None, idx
        return API_KEYS[idx], idx

def rotate_api_key(failed_idx):
    with api_lock:
        if api_state.get('active_index', 0) == failed_idx:
            api_state['active_index'] += 1
            save_json('api_state.json', api_state)
            print(f"🔄 Key #{failed_idx + 1} Exhausted! Switching to Key #{api_state['active_index'] + 1}")
        return api_state['active_index'] < len(API_KEYS)

def update_key_telemetry(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  MATH ENGINE
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
#  CLOUD FETCHING
# ==========================================
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
        futures = {executor.submit(fetch_odds_with_retry, f'https://api.the-odds-api.com/v4/sports/{sp}/odds', {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'totals,spreads', 'oddsFormat': 'decimal'}): sp for sp in TARGET_SPORTS}
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
#  PROCESS MARKETS
# ==========================================
def process_markets(results):
    all_evs, all_arbs = [], []
    for sport, events in results.items():
        if not events: continue
        for event in events:
            home = event.get('home_team', 'Team A')
            away = event.get('away_team', 'Team B')
            match_time = format_ist_time(event['commence_time'])
            
            ev_lines, arb_lines = {}, {}
            for bookie in event.get('bookmakers', []):
                b_name = bookie['key']
                for market in bookie.get('markets', []):
                    m_type = market['key'].upper()
                    for outcome in market['outcomes']:
                        point = str(outcome.get('point', '0'))
                        name, price = outcome['name'], outcome['price']
                        if b_name == 'betfair_ex_eu': price = 1 + (price - 1) * 0.97
                        line_key = f"{m_type} {point}" if point != "0" else f"{m_type}"

                        if line_key not in ev_lines: ev_lines[line_key] = {'pin': {}, 'softs': {}}
                        if line_key not in arb_lines: arb_lines[line_key] = {}
                        
                        if b_name == 'pinnacle': ev_lines[line_key]['pin'][name] = price
                        else:
                            if name not in ev_lines[line_key]['softs']: ev_lines[line_key]['softs'][name] = {}
                            ev_lines[line_key]['softs'][name][b_name] = price
                                
                        if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                            arb_lines[line_key][name] = {'price': price, 'bookie': b_name}

            # Evaluate EV
            for lk, d in ev_lines.items():
                pinny, softs = d['pin'], d['softs']
                if len(pinny) == 2:
                    s1, s2 = list(pinny.keys())
                    t1, t2 = remove_vig(pinny[s1], pinny[s2])
                    for side, true_odds in [(s1, t1), (s2, t2)]:
                        if side in softs:
                            best_bk = max(softs[side], key=softs[side].get)
                            best_p = softs[side][best_bk]
                            if best_p > true_odds:
                                ev_pct = ((best_p / true_odds) - 1) * 100
                                if ev_pct >= MIN_EV_THRESHOLD:
                                    all_evs.append({
                                        'pct': ev_pct, 'home': home, 'away': away, 'time': match_time, 'sport': sport.replace('_', ' ').upper(),
                                        'line': lk, 'sel': side, 'odds': best_p, 'trueO': true_odds, 'bk': best_bk,
                                        'stk': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk),
                                        'conf': max(0, min(100, int((abs((1/best_p) - (1/true_odds)) / (1/true_odds)) * 500)))
                                    })

            # Evaluate ARB
            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                for ways in [2, 3]:
                    if len(keys) < ways: continue
                    k_slice = keys[:ways]
                    margin = sum(1/outs[k]['price'] for k in k_slice)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {'pct': (1-margin)*100, 'home': home, 'away': away, 'time': match_time, 'sport': sport.replace('_', ' ').upper(), 'line': lk, 'ways': ways, 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': []}
                        for k in k_slice:
                            arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': (TOTAL_BANKROLL/margin)/outs[k]['price']})
                        all_arbs.append(arb)

    all_evs.sort(key=lambda x: x['pct'], reverse=True)
    all_arbs.sort(key=lambda x: x['pct'], reverse=True)
    return all_evs, all_arbs

# ==========================================
#  WEBSITE GENERATOR
# ==========================================
def generate_web(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    build_time = ist_now.strftime('%d %b %Y, %I:%M %p IST')
    
    # Generate API Cards
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

    # Generate EV Cards
    ev_html = ""
    for e in evs:
        ev_html += f"""
        <div style="background:#18181b; border:1px solid #27272a; border-radius:12px; margin-bottom:15px; overflow:hidden;">
            <div style="padding:12px 15px; border-bottom:1px solid #27272a; background:rgba(6,182,212,0.05); display:flex; justify-content:space-between;">
                <span style="font-size:11px; color:#a1a1aa; font-weight:bold;">🏆 {e['sport']} &nbsp;|&nbsp; 📅 {e['time']}</span>
                <span style="color:#06b6d4; font-weight:800; font-family:monospace;">{e['pct']:.2f}% EV</span>
            </div>
            <div style="padding:15px;">
                <div style="font-size:16px; font-weight:800; margin-bottom:10px;">{e['home']} <span style="font-size:12px; color:#a1a1aa;">vs</span> {e['away']}</div>
                <div style="font-size:12px; color:#a1a1aa; margin-bottom:10px;">LINE: <strong style="color:#fff">{e['line']}</strong></div>
                
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

    # Generate ARB Cards
    arb_html = ""
    for a in arbs:
        legs_html = ""
        for s in a['sides']:
            legs_html += f"""
            <div style="display:flex; justify-content:space-between; background:#09090b; padding:10px; border-radius:8px; border:1px solid #27272a; margin-bottom:5px;">
                <span>{s['sel'].upper()} @ <strong style="color:#f59e0b;">{s['pr']:.2f}</strong> <span style="font-size:10px; background:#27272a; padding:2px 6px; border-radius:4px;">{s['bk'].title().replace('_',' ')}</span></span>
                <strong style="color:#06b6d4;">₹{s['stk']:.0f}</strong>
            </div>"""
            
        arb_html += f"""
        <div style="background:#18181b; border:1px solid #27272a; border-radius:12px; margin-bottom:15px; overflow:hidden;">
            <div style="padding:12px 15px; border-bottom:1px solid #27272a; background:rgba(245,158,11,0.05); display:flex; justify-content:space-between;">
                <span style="font-size:11px; color:#a1a1aa; font-weight:bold;">🏆 {a['sport']} &nbsp;|&nbsp; 📅 {a['time']}</span>
                <span style="color:#f59e0b; font-weight:800; font-family:monospace;">{a['pct']:.2f}% ARB</span>
            </div>
            <div style="padding:15px;">
                <div style="font-size:16px; font-weight:800; margin-bottom:10px;">{a['home']} <span style="font-size:12px; color:#a1a1aa;">vs</span> {a['away']}</div>
                <div style="font-size:12px; color:#a1a1aa; margin-bottom:10px;">LINE: <strong style="color:#fff">{a['line']}</strong> ({a['ways']}-Way)</div>
                {legs_html}
                <div style="text-align:right; margin-top:10px;"><span style="font-size:12px; color:#a1a1aa;">GUARANTEED PROFIT:</span> <strong style="color:#10b981; font-size:20px;">₹{a['profit']:.0f}</strong></div>
            </div>
        </div>"""

    if not ev_html: ev_html = "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No EV edges detected right now.</div>"
    if not arb_html: arb_html = "<div style='text-align:center; padding:40px; color:#a1a1aa;'>No Arbitrage locks detected right now.</div>"

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
    
    .tabs {{ display: flex; gap: 5px; background: #18181b; padding: 5px; border-radius: 10px; border: 1px solid #27272a; margin-bottom: 20px; }}
    .tab {{ flex: 1; text-align: center; padding: 12px; border-radius: 6px; cursor: pointer; font-weight: bold; color: #a1a1aa; font-size: 13px; }}
    .tab.active {{ background: #3f3f46; color: #fff; }}
    .pane {{ display: none; }} .pane.active {{ display: block; }}
</style>
</head>
<body>

<div class="hdr">
    <div>
        <h1 class="title">⚡ ARB SNIPER</h1>
        <div class="subtitle">Cloud Scanning Active · Updates Every Hour</div>
    </div>
    <div class="time-badge">SYNCED: {build_time}</div>
</div>

<div class="tabs">
    <div class="tab active" onclick="showPane('ev')">💎 EV Edges ({len(evs)})</div>
    <div class="tab" onclick="showPane('arb')">🔒 Arbitrage ({len(arbs)})</div>
    <div class="tab" onclick="showPane('net')">📡 API Network</div>
</div>

<div id="pane-ev" class="pane active">{ev_html}</div>
<div id="pane-arb" class="pane">{arb_html}</div>
<div id="pane-net" class="pane">{net_html}</div>

<script>
    function showPane(p) {{
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.pane').forEach(t => t.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('pane-'+p).classList.add('active');
    }}
    // Auto-refresh the page every 5 minutes to fetch the latest GitHub build
    setInterval(() => window.location.reload(true), 300000);
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(HTML)

# ==========================================
#  MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    print("🚀 Cloud Engine Booting...")
    results = fetch_all_sports()
    evs, arbs = process_markets(results)
    
    # High-Detail NTFY Alerts
    for a in arbs:
        alert_key = f"ARB|{a['match']}|{a['line']}|{a['pct']:.2f}"
        if not is_duplicate_alert(alert_key):
            msg = f"🏆 {a['sport']}\n📅 {a['time']}\n📈 {a['line']}\n\n"
            for s in a['sides']: 
                msg += f"🔵 ₹{s['stk']:.0f} on {s['sel'].upper()} @ {s['pr']:.2f} [{s['bk'].title().replace('_',' ')}]\n"
            msg += f"\n✨ Guaranteed Profit: ₹{a['profit']:.0f}"
            requests.post("https://ntfy.sh/", json={"topic": NTFY_CHANNEL, "message": msg, "title": f"🚨 {a['pct']:.2f}% ARB | {a['home']} vs {a['away']}", "tags": ["gem", "moneybag"], "priority": 5}, timeout=5)

    for e in evs:
        alert_key = f"EV|{e['match']}|{e['line']}|{e['sel']}|{e['odds']:.2f}"
        if not is_duplicate_alert(alert_key):
            msg = f"🏆 {e['sport']}\n📅 {e['time']}\n📈 {e['line']}\n\n"
            msg += f"💰 BET EXACTLY: ₹{e['stk']:.0f}\n"
            msg += f"👉 {e['sel'].upper()} @ {e['odds']:.2f} on {e['bk'].title().replace('_',' ')}\n\n"
            msg += f"🧠 True Odds: {e['trueO']:.3f} | Confidence: {e['conf']}/100"
            requests.post("https://ntfy.sh/", json={"topic": NTFY_CHANNEL, "message": msg, "title": f"📈 {e['pct']:.2f}% EV | {e['home']} vs {e['away']}", "tags": ["gem", "moneybag"], "priority": 5}, timeout=5)

    save_json('api_state.json', api_state)
    save_json('alert_cache.json', alert_cache)
    generate_web(evs, arbs)
    print(f"✅ Global Terminal Synced. Found {len(evs)} EV and {len(arbs)} ARBs.")
