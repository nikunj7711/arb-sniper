import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION
# ==========================================
_raw_keys = os.getenv('ODDS_API_KEYS', '41308dc8cb155421b36bf4e58a0fe50b')
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' 
TOTAL_BANKROLL = 1500                   
MIN_EV_THRESHOLD = 1.0                  
MIN_ARB_THRESHOLD = 0.3                 

MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'
TARGET_SPORTS = ['soccer_epl', 'soccer_spain_la_liga', 'soccer_uefa_champs_league', 
                 'basketball_nba', 'icehockey_nhl', 'soccer_italy_serie_a', 'upcoming']

BOOK_CAPS = {'bcgame': 1000, 'pinnacle': 1000, 'onexbet': 500}

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
        return api_state['active_index'] < len(API_KEYS)

def update_key_stats(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        if rem is not None: api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  3. DATA FETCHERS
# ==========================================
def fetch_bcgame_custom():
    headers = {'accept': '*/*', 'origin': 'https://bc.game', 'user-agent': 'Mozilla/5.0'}
    print("🥷 Fetching BC.Game Pipeline...")
    try:
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        v_res = requests.get(map_url, headers=headers, timeout=10).json()
        version = v_res['top_events_versions'][0]
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        std = []
        for m in events.values():
            d, mk = m.get('desc', {}), m.get('markets', {})
            c = d.get('competitors', [])
            if len(c) < 2: continue
            
            h2h, tots = [], []
            m1 = mk.get("1", {}).get("", {})
            for k, n in [("1", c[0]['name']), ("2", c[1]['name']), ("3", "Draw")]:
                if k in m1: h2h.append({'name': n, 'price': float(m1[k]["k"])})
            
            m18 = mk.get("18", {})
            for pk, pd in m18.items():
                val = float(pk.replace("total=", ""))
                if "12" in pd: tots.append({'name': 'Over', 'price': float(pd["12"]["k"]), 'point': val})
                if "13" in pd: tots.append({'name': 'Under', 'price': float(pd["13"]["k"]), 'point': val})
            
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
                update_key_stats(idx, res.headers.get('x-requests-remaining'))
                return res.json()
            if res.status_code in [401, 429]:
                if rotate_api_key(idx): continue
            return None
        except: return None

# ==========================================
#  4. MATH & PROCESSING
# ==========================================
def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def calculate_kelly(soft_o, true_o, bank, bookie):
    b, p = soft_o - 1.0, 1.0 / true_o
    k = ((b * p - (1-p)) / b) * 0.30
    return min(max(20, bank * min(k, 0.05)), BOOK_CAPS.get(bookie, 1000))

def process_markets(results):
    all_evs, all_arbs = [], []
    for sport, events in results.items():
        if not events: continue
        for event in events:
            home, away = event.get('home_team', 'A'), event.get('away_team', 'B')
            commence = event.get('commence_time', '')
            
            ev_lines, arb_lines = {}, {}
            for bookie in event.get('bookmakers', []):
                b_name = bookie['key']
                for market in bookie.get('markets', []):
                    m_type = market['key'].upper()
                    for out in market['outcomes']:
                        lk = f"{m_type} {out.get('point', '')}".strip()
                        name, price = out['name'], out['price']
                        if lk not in ev_lines: ev_lines[lk] = {'pin': {}, 'softs': {}}
                        if lk not in arb_lines: arb_lines[lk] = {}
                        if b_name == 'pinnacle': ev_lines[lk]['pin'][name] = price
                        else:
                            if name not in ev_lines[lk]['softs']: ev_lines[lk]['softs'][name] = {}
                            ev_lines[lk]['softs'][name][b_name] = price
                        if name not in arb_lines[lk] or price > arb_lines[lk][name]['price']:
                            arb_lines[lk][name] = {'price': price, 'bookie': b_name}

            for lk, d in ev_lines.items():
                if len(d['pin']) in [2, 3]:
                    keys = list(d['pin'].keys())
                    true_os = remove_vig(*[d['pin'][k] for k in keys])
                    for idx, side in enumerate(keys):
                        if side in d['softs']:
                            best_bk = max(d['softs'][side], key=d['softs'][side].get)
                            best_p = d['softs'][side][best_bk]
                            if best_p > true_os[idx]:
                                ev_pct = ((best_p / true_os[idx]) - 1) * 100
                                if ev_pct >= MIN_EV_THRESHOLD:
                                    all_evs.append({'pct': ev_pct, 'match': f"{home} vs {away}", 'line': lk, 'sel': side, 'odds': best_p, 'trueO': true_os[idx], 'bk': best_bk, 'stk': calculate_kelly(best_p, true_os[idx], TOTAL_BANKROLL, best_bk), 'sport': sport.upper(), 'time': commence})

            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                if len(keys) in [2, 3]:
                    margin = sum(1/outs[k]['price'] for k in keys)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {'pct': (1-margin)*100, 'match': f"{home} vs {away}", 'line': lk, 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': [], 'sport': sport.upper(), 'time': commence}
                        for k in keys: arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': (TOTAL_BANKROLL/margin)/outs[k]['price']})
                        all_arbs.append(arb)
    return all_evs, all_arbs

# ==========================================
#  5. WEB GENERATOR
# ==========================================
def generate_web(evs, arbs):
    ist = (datetime.now(timezone.utc) + timedelta(hours=5.5)).strftime('%d %b, %I:%M %p IST')
    
    def card(item, is_arb):
        color = "#f59e0b" if is_arb else "#06b6d4"
        content = ""
        if is_arb:
            legs = "".join([f"<div style='display:flex; justify-content:space-between; font-size:12px; background:#09090b; padding:6px; margin-top:4px; border-radius:4px;'><span>{s['sel']} @ <b>{s['pr']}</b></span><span>{s['bk'].upper()}</span></div>" for s in item['sides']])
            content = f"{legs}<div style='text-align:right; margin-top:10px; color:#10b981; font-weight:bold;'>Profit: ₹{item['profit']:.0f}</div>"
        else:
            content = f"<div style='background:#09090b; padding:10px; border-radius:6px;'>Bet <b>{item['sel'].upper()}</b> @ {item['odds']} <span style='float:right;'>{item['bk'].upper()}</span></div><div style='display:flex; justify-content:space-between; margin-top:8px; font-size:11px;'><span>Stake: <b>₹{item['stk']:.0f}</b></span><span>True: {item['trueO']:.3f}</span></div>"
        
        return f"""<div class='card' style='border-left: 4px solid {color};'>
            <div style='display:flex; justify-content:space-between;'><span class='badge' style='background:{color}; color:#000;'>{item['pct']:.2f}%</span><button class='copy-btn' onclick='navigator.clipboard.writeText("{item["match"]}")'>COPY</button></div>
            <div style='font-weight:bold; margin:10px 0;'>{item['match']}</div>
            <div style='font-size:12px; color:#a1a1aa; margin-bottom:8px;'>{item['line']} | {item['sport']}</div>
            {content}
        </div>"""

    net_html = "".join([f"<div style='background:#18181b; padding:10px; margin-bottom:5px; border-radius:8px;'>Key #{i+1}: {api_state['stats'].get(str(i), {}).get('remaining', '??')} left</div>" for i in range(len(API_KEYS))])

    HTML = f"""<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><style>
    body {{ background:#09090b; color:#fff; font-family:sans-serif; padding:15px; max-width:600px; margin:auto; }}
    .card {{ background:#18181b; padding:15px; border-radius:12px; margin-bottom:12px; border:1px solid #27272a; }}
    .badge {{ font-size:11px; padding:3px 8px; border-radius:5px; font-weight:bold; }}
    .copy-btn {{ background:#27272a; color:#fff; border:none; padding:5px 10px; border-radius:6px; cursor:pointer; font-size:11px; }}
    .tabs {{ display:flex; gap:5px; margin-bottom:20px; }}
    .tab {{ flex:1; text-align:center; padding:10px; background:#18181b; border-radius:8px; cursor:pointer; font-size:12px; color:#a1a1aa; }}
    .tab.active {{ background:#3f3f46; color:#fff; }}
    .pane {{ display:none; }} .active-pane {{ display:block; }}
    </style></head><body>
    <h2 style='color:#06b6d4;'>⚡ SNIPER TERMINAL</h2>
    <div class='tabs'><div class='tab active' onclick='sw(0)'>ARB ({len(arbs)})</div><div class='tab' onclick='sw(1)'>VALUE ({len(evs)})</div><div class='tab' onclick='sw(2)'>NET</div></div>
    <div id='p0' class='pane active-pane'>{"".join([card(a, True) for a in arbs])}</div>
    <div id='p1' class='pane'>{"".join([card(e, False) for e in evs[:40]])}</div>
    <div id='p2' class='pane'>{net_html}</div>
    <script>function sw(i){{ document.querySelectorAll('.tab').forEach((t,j)=>j==i?t.classList.add('active'):t.classList.remove('active')); document.querySelectorAll('.pane').forEach((p,j)=>j==i?p.classList.add('active-pane'):p.classList.remove('active-pane')); }}</script>
    </body></html>"""
    with open("index.html", "w", encoding="utf-8") as f: f.write(HTML)

# ==========================================
#  6. MAIN TRIGGER
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
        found = False
        for events in results.values():
            if not events: continue
            for ev in events:
                api_h, api_a = clean(ev.get('home_team', '')), clean(ev.get('away_team', ''))
                if (bc_h == api_h or any(w in api_h for w in bc_h.split() if len(w)>4)) and \
                   (bc_a == api_a or any(w in api_a for w in bc_a.split() if len(w)>4)):
                    ev['bookmakers'].append(bc['bookmakers'][0])
                    merge_count += 1; found = True; break
            if found: break
    print(f"✅ Linked {merge_count} BC.Game matches.")

    evs, arbs = process_markets(results)
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    generate_web(evs, arbs)
    print("✅ Dashboard Updated.")
