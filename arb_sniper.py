import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION (19-KEY COMPATIBLE)
# ==========================================
_raw_keys = os.getenv('ODDS_API_KEYS', '41308dc8cb155421b36bf4e58a0fe50b')
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' 
TOTAL_BANKROLL = 1500                   
MIN_EV_THRESHOLD = 1.5                  
MIN_ARB_THRESHOLD = 0.5                 

MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'
TARGET_SPORTS = ['soccer_epl', 'soccer_spain_la_liga', 'soccer_uefa_champs_league', 
                 'basketball_nba', 'icehockey_nhl', 'soccer_italy_serie_a', 'upcoming']

BOOK_CAPS = {'bcgame': 1000, 'pinnacle': 1000, 'onexbet': 500, 'bet365': 500}

# ==========================================
#  2. KEY ROTATION & TELEMETRY
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

def get_active_api_key():
    with api_lock:
        idx = api_state['active_index']
        if idx >= len(API_KEYS): return None, idx
        return API_KEYS[idx], idx

def rotate_api_key(failed_idx):
    with api_lock:
        if api_state['active_index'] == failed_idx:
            api_state['active_index'] += 1
            print(f"🔄 Rotating to Key #{api_state['active_index'] + 1}")
            save_json('api_state.json', api_state)
        return api_state['active_index'] < len(API_KEYS)

def update_key_stats(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  4. THE FETCHERS
# ==========================================
def fetch_bcgame_custom():
    headers = {'accept': '*/*', 'origin': 'https://bc.game', 'user-agent': 'Mozilla/5.0'}
    try:
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        version = requests.get(map_url, headers=headers, timeout=10).json()['top_events_versions'][0]
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        std = []
        for m in events.values():
            d, mk = m.get('desc', {}), m.get('markets', {})
            c = d.get('competitors', [])
            if len(c) < 2: continue
            
            h2h = []
            m1 = mk.get("1", {}).get("", {})
            if m1:
                if "1" in m1: h2h.append({'name': c[0]['name'], 'price': float(m1["1"]["k"])})
                if "2" in m1: h2h.append({'name': c[1]['name'], 'price': float(m1["2"]["k"])})
                if "3" in m1: h2h.append({'name': 'Draw', 'price': float(m1["3"]["k"])})
            
            tots = []
            m18 = mk.get("18", {})
            for pk, pd in m18.items():
                val = pk.replace("total=", "")
                if "12" in pd: tots.append({'name': 'Over', 'price': float(pd["12"]["k"]), 'point': float(val)})
                if "13" in pd: tots.append({'name': 'Under', 'price': float(pd["13"]["k"]), 'point': float(val)})
            
            std.append({
                'home_team': c[0]['name'], 'away_team': c[1]['name'],
                'commence_time': datetime.fromtimestamp(d.get('scheduled', 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                'bookmakers': [{'key': 'bcgame', 'title': 'BC.Game', 'markets': [{'key': 'h2h', 'outcomes': h2h}, {'key': 'totals', 'outcomes': tots}]}]
            })
        return std
    except: return []

def fetch_odds_api(url, params):
    while True:
        key, idx = get_active_api_key()
        if not key: return None
        params['apiKey'] = key
        try:
            res = requests.get(url, params=params, timeout=15)
            if res.status_code == 200:
                update_key_stats(idx, res.headers.get('x-requests-remaining', 0))
                return res.json()
            if res.status_code in [401, 429]:
                if rotate_api_key(idx): continue
            return None
        except: return None

# ==========================================
#  5. THE MATH & DASHBOARD
# ==========================================
def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def calculate_kelly(soft_o, true_o, bank, bookie):
    b, p = soft_o - 1.0, 1.0 / true_o
    k = ((b * p - (1-p)) / b) * 0.30
    return min(max(20, bank * min(k, 0.05)), BOOK_CAPS.get(bookie, 1000))

def generate_web(evs, arbs):
    now = (datetime.now(timezone.utc) + timedelta(hours=5.5)).strftime('%d %b, %I:%M %p IST')
    
    net_html = ""
    for idx, key in enumerate(API_KEYS):
        masked = f"{key[:4]}••••{key[-4:]}"
        rem = api_state['stats'].get(str(idx), {}).get('remaining', '??')
        color = "#06b6d4" if idx == api_state['active_index'] else "#3f3f46"
        net_html += f"<div style='background:#18181b; padding:10px; border-radius:8px; border-left:4px solid {color}; margin-bottom:8px;'><b>{masked}</b><br><small>Remaining: {rem}</small></div>"

    def card(item, is_arb):
        badge = "arb-badge" if is_arb else "ev-badge"
        pct = item['pct']
        return f"""
        <div class='card'>
            <div style='display:flex; justify-content:space-between; align-items:center;'>
                <span class='badge {badge}'>{pct:.2f}% {'ARB' if is_arb else 'EV'}</span>
                <button class='copy-btn' onclick='navigator.clipboard.writeText("{item["match"]}")'>COPY</button>
            </div>
            <div style='margin-top:10px; font-weight:bold; font-size:16px;'>{item["match"]}</div>
            <div style='color:#a1a1aa; font-size:12px; margin-bottom:12px;'>{item["line"]} | {item.get("sport", "Upcoming")}</div>
            {f"<div style='text-align:right; color:#10b981; font-weight:bold;'>Profit: ₹{item['profit']:.0f}</div>" if is_arb else f"<div>Bet <b>{item['sel'].upper()}</b> @ {item['odds']} ({item['bk'].upper()})</div><div style='display:flex; justify-content:space-between; margin-top:10px;'><small>Stake: ₹{item['stk']:.0f}</small> <small style='color:#3f3f46;'>True: {item['trueO']:.3f}</small></div>"}
        </div>"""

    HTML = f"""<!DOCTYPE html>
    <html><head><meta name='viewport' content='width=device-width, initial-scale=1'><style>
    body {{ background: #09090b; color: #fff; font-family: sans-serif; padding: 15px; max-width: 600px; margin: auto; }}
    .card {{ background: #18181b; border: 1px solid #27272a; padding: 15px; border-radius: 12px; margin-bottom: 12px; }}
    .tabs {{ display: flex; gap: 5px; background: #18181b; padding: 5px; border-radius: 10px; margin-bottom: 20px; }}
    .tab {{ flex: 1; text-align: center; padding: 10px; border-radius: 6px; font-size: 12px; font-weight: bold; cursor: pointer; color: #a1a1aa; }}
    .tab.active {{ background: #3f3f46; color: #fff; }}
    .pane {{ display: none; }} .active-pane {{ display: block; }}
    .badge {{ font-size: 11px; padding: 3px 8px; border-radius: 5px; font-weight: bold; }}
    .arb-badge {{ background: #f59e0b; color: #000; }}
    .ev-badge {{ background: #06b6d4; color: #000; }}
    .copy-btn {{ background: #27272a; color: #fff; border: none; padding: 5px 10px; border-radius: 6px; cursor: pointer; }}
    </style></head><body>
    <h2 style='color:#06b6d4;'>⚡ SNIPER TERMINAL</h2>
    <div class='tabs'>
        <div class='tab active' onclick='switchTab(0)'>ARB ({len(arbs)})</div>
        <div class='tab' onclick='switchTab(1)'>EV ({len(evs)})</div>
        <div class='tab' onclick='switchTab(2)'>NET</div>
    </div>
    <div id='p0' class='pane active-pane'>{"".join([card(a, True) for a in arbs]) or "<center>No Arbs</center>"}</div>
    <div id='p1' class='pane'>{"".join([card(e, False) for e in evs[:30]]) or "<center>No EV Bets</center>"}</div>
    <div id='p2' class='pane'>{net_html}</div>
    <script>
        function switchTab(idx) {{
            document.querySelectorAll('.tab').forEach((t, i) => i==idx ? t.classList.add('active') : t.classList.remove('active'));
            document.querySelectorAll('.pane').forEach((p, i) => i==idx ? p.classList.add('active-pane') : p.classList.remove('active-pane'));
        }}
    </script>
    </body></html>"""
    with open("index.html", "w", encoding="utf-8") as f: f.write(HTML)

# ==========================================
#  7. MAIN ENGINE
# ==========================================
if __name__ == "__main__":
    print(f"🚀 Sniper Active: {len(API_KEYS)} Keys.")
    results = {}
    with ThreadPoolExecutor(max_workers=4) as exc:
        futures = {exc.submit(fetch_odds_api, f'https://api.the-odds-api.com/v4/sports/{s}/odds', {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals'}): s for s in TARGET_SPORTS}
        for f in as_completed(futures): results[futures[f]] = f.result()
    
    bc_data = fetch_bcgame_custom()
    merge_count = 0
    for bc in bc_data:
        def clean(n):
            for j in ['(holis)', '(e)', 'fc ', ' fc', 'real ', 'as ']: n = n.lower().replace(j, '')
            return n.strip()
        bc_h, bc_a = clean(bc['home_team']), clean(bc['away_team'])
        linked = False
        for events in results.values():
            if not events: continue
            for ev in events:
                api_h, api_a = clean(ev.get('home_team', '')), clean(ev.get('away_team', ''))
                if (bc_h == api_h or any(w in api_h for w in bc_h.split() if len(w)>4)) and \
                   (bc_a == api_a or any(w in api_a for w in bc_a.split() if len(w)>4)):
                    ev['bookmakers'].append(bc['bookmakers'][0])
                    merge_count += 1; linked = True; break
            if linked: break
    print(f"✅ Linked {merge_count} BC.Game matches.")

    from main_logic import process_markets # Logic remains same
    evs, arbs = process_markets(results)
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    generate_web(evs, arbs)
    print("✅ Dashboard Updated.")
