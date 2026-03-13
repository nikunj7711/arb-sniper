import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION (19-KEY READY)
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

def get_active_api_key():
    with api_lock:
        idx = api_state['active_index']
        if idx >= len(API_KEYS): return None, idx
        return API_KEYS[idx], idx

def rotate_api_key(failed_idx):
    with api_lock:
        if api_state['active_index'] == failed_idx:
            api_state['active_index'] += 1
            print(f"🔄 Key #{failed_idx + 1} Exhausted. Rotating to Key #{api_state['active_index'] + 1}")
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
    print("🥷 Fetching BC.Game Zero-Cost Pipeline...")
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

def fetch_all_sports():
    results = {}
    print("📡 Fetching Odds-API Master Data...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_odds_api, f'https://api.the-odds-api.com/v4/sports/{sp}/odds', {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals'}): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results

# ==========================================
#  4. MATH ENGINE & ANTI-DETECTION
# ==========================================
def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def calculate_kelly(soft_o, true_o, bank, bookie):
    b, p = soft_o - 1.0, 1.0 / true_o
    k = ((b * p - (1-p)) / b) * 0.30
    raw_stake = min(max(20, bank * min(k, 0.05)), BOOK_CAPS.get(bookie, 1000))
    return round(raw_stake / 10) * 10

def format_ist(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%d %b | %I:%M %p")
    except: return "TBD"

def get_countdown(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        diff = dt - datetime.now(timezone.utc)
        if diff.total_seconds() < 0: return "LIVE NOW"
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    except: return ""

def process_markets(results):
    all_evs, all_arbs = [], []
    for sport, events in results.items():
        if not events: continue
        for event in events:
            home, away = event.get('home_team', 'A'), event.get('away_team', 'B')
            commence = event.get('commence_time', '')
            match_name = f"{home} vs {away}"
            
            meta = {
                'match': match_name, 'sport': sport.replace('_', ' ').upper(),
                'time': format_ist(commence), 'countdown': get_countdown(commence)
            }
            
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
                                    all_evs.append({**meta, 'pct': ev_pct, 'line': lk, 'ways': len(keys), 'sel': side, 'odds': best_p, 'trueO': true_os[idx], 'bk': best_bk, 'stk': calculate_kelly(best_p, true_os[idx], TOTAL_BANKROLL, best_bk)})

            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                if len(keys) in [2, 3]:
                    margin = sum(1/outs[k]['price'] for k in keys)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {**meta, 'pct': (1-margin)*100, 'line': lk, 'ways': len(keys), 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': []}
                        for k in keys:
                            raw_s = (TOTAL_BANKROLL/margin)/outs[k]['price']
                            arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': round(raw_s / 10) * 10})
                        all_arbs.append(arb)
    return all_evs, all_arbs

# ==========================================
#  5. PRO DASHBOARD (WITH KEYS TAB)
# ==========================================
# ==========================================
#  5. PRO DASHBOARD (WITH KEYS TAB & SECURE AUTH)
# ==========================================
def generate_web(evs, arbs):
    ist_now = (datetime.now(timezone.utc) + timedelta(hours=5.5)).strftime('%d %b, %I:%M %p IST')
    SECRET_PASS = "ARB2026" 

    # 🔑 Build the detailed Keys Tab HTML
    keys_html = ""
    for idx, key in enumerate(API_KEYS):
        rem = api_state.get('stats', {}).get(str(idx), {}).get('remaining', '??')
        is_active = (idx == api_state.get('active_index', 0))
        color = "#06b6d4" if is_active else "#3f3f46"
        status = "ACTIVE" if is_active else "IDLE"
        masked = f"{key[:4]}••••••••••••{key[-4:]}" if len(key) > 8 else "INVALID_KEY"

        keys_html += f"""
        <div style='background:#18181b; border:1px solid #27272a; padding:15px; border-radius:8px; border-left:4px solid {color}; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;'>
            <div>
                <div style='font-size:12px; color:#a1a1aa; margin-bottom:4px;'>KEY #{idx+1} <span style='background:#000; padding:2px 6px; border-radius:4px; font-size:9px; margin-left:5px; color:{color}; border:1px solid {color};'>{status}</span></div>
                <div style='font-family:monospace; font-size:16px; color:#fff;'>{masked}</div>
            </div>
            <div style='text-align:right;'>
                <div style='font-size:10px; color:#a1a1aa;'>REMAINING CALLS</div>
                <div style='font-size:22px; font-weight:bold; color:{color};'>{rem}</div>
            </div>
        </div>
        """

    def match_card(item, is_arb):
        color = "#f59e0b" if is_arb else "#06b6d4"
        stability = "STABLE" if item['pct'] < 8 else "VOLATILE"
        stability_color = "#10b981" if stability == "STABLE" else "#ef4444"

        legs = ""
        if is_arb:
            for s in item['sides']:
                legs += f"<div style='display:flex; justify-content:space-between; background:#000; padding:8px; border-radius:6px; margin-top:5px; border:1px solid #222;'><span>{s['sel']} @ <b style='color:{color}'>{s['pr']}</b> <small>({s['bk'].upper()})</small></span><span style='color:#fff'>₹{s['stk']:.0f}</span></div>"

        return f"""
        <div class='card' style='border-left: 4px solid {color};'>
            <div style='display:flex; justify-content:space-between; align-items:start;'>
                <div>
                    <span class='badge' style='background:{color}; color:#000;'>{item['pct']:.2f}% {'ARB' if is_arb else 'EV'}</span>
                    <span style='font-size:9px; margin-left:5px; color:{stability_color}; font-weight:bold;'>● {stability} MKT</span>
                    <div style='font-size:10px; color:#a1a1aa; margin-top:5px;'>🏆 {item['sport']}</div>
                </div>
                <button class='copy-btn' onclick='navigator.clipboard.writeText("{item["match"]}")'>COPY</button>
            </div>
            <div style='font-size:17px; font-weight:bold; margin: 12px 0;'>{item['match']}</div>
            <div style='display:flex; gap:10px; font-size:11px; margin-bottom:8px;'>
                <span style='background:#222; padding:3px 7px; border-radius:4px;'>📅 {item['time']}</span>
                <span style='background:rgba(16,185,129,0.1); color:#10b981; padding:3px 7px; border-radius:4px;'>⏳ {item['countdown']}</span>
            </div>
            <div style='font-size:11px; margin-bottom:10px; color:#10b981; font-weight:bold;'>🛡️ STEALTH: ROUNDED STAKE</div>
            <div style='font-size:13px; color:#eee;'>Line: <b>{item['line']}</b> ({item['ways']}-Way)</div>
            {legs if is_arb else f"<div style='background:#000; padding:10px; border-radius:6px; margin-top:10px;'>Bet <b>{item['sel'].upper()}</b> @ {item['odds']} <span style='float:right; color:{color};'>{item['bk'].upper()}</span></div><div style='display:flex; justify-content:space-between; margin-top:8px; font-size:11px;'><span>Stake: <b>₹{item['stk']:.0f}</b></span><span style='color:#444;'>True: {item['trueO']:.3f}</span></div>"}
            {f"<div style='text-align:right; font-weight:bold; color:#10b981; margin-top:12px; font-size:18px;'>Profit: ₹{item['profit']:.0f}</div>" if is_arb else ""}
        </div>"""

    HTML = f"""<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><style>
        body {{ background:#09090b; color:#fff; font-family:sans-serif; padding:15px; max-width:600px; margin:auto; }}
        .card {{ background:#18181b; border:1px solid #27272a; padding:15px; border-radius:12px; margin-bottom:12px; }}
        #lock-screen {{ position:fixed; top:0; left:0; width:100%; height:100%; background:#09090b; z-index:999; display:flex; flex-direction:column; align-items:center; justify-content:center; }}
        .pass-input {{ background:#18181b; border:1px solid #27272a; color:#fff; padding:12px; border-radius:8px; text-align:center; margin-bottom:10px; }}
        .tab {{ flex:1; text-align:center; padding:12px; background:#18181b; border-radius:8px; cursor:pointer; font-size:12px; color:#666; font-weight:bold; }}
        .tab.active {{ background:#3f3f46; color:#fff; border:1px solid #06b6d4; }}
        .pane {{ display:none; }} .active-pane {{ display:block; }}
        .copy-btn {{ background:#3f3f46; color:#fff; border:none; padding:6px 10px; border-radius:6px; cursor:pointer; font-size:11px; }}
        .badge {{ font-size:10px; padding:3px 7px; border-radius:5px; font-weight:bold; }}
    </style></head><body>
        <div id='lock-screen'>
            <h2 style='color:#06b6d4;'>⚡ TERMINAL LOCKED</h2>
            <input type='password' id='ps' class='pass-input' placeholder='Enter Code'>
            <button onclick='ck()' style='background:#06b6d4; border:none; padding:10px 20px; border-radius:8px; font-weight:bold; cursor:pointer;'>UNLOCK</button>
            <p id='err' style='color:#ef4444; font-size:12px; margin-top:10px;'></p>
        </div>
        <div id='content' style='display:none;'>
            <div style='margin-bottom:20px;'>
                <h2 style='color:#06b6d4; margin:0;'>⚡ SNIPER PRO</h2>
                <small style='color:#444;'>Synced: {ist_now}</small>
            </div>
            <div style='display:flex; gap:5px; margin-bottom:20px;'>
                <div class='tab active' onclick='sw(0)'>ARB ({len(arbs)})</div>
                <div class='tab' onclick='sw(1)'>VALUE ({len(evs)})</div>
                <div class='tab' onclick='sw(2)'>KEYS</div>
            </div>
            <div id='p0' class='pane active-pane'>{"".join([match_card(a, True) for a in arbs])}</div>
            <div id='p1' class='pane'>{"".join([match_card(e, False) for e in evs[:50]])}</div>
            <div id='p2' class='pane'>{keys_html}</div>
        </div>
        <script>
            // The secret pass is injected directly into the JS variable
            const currentSecret = '{SECRET_PASS}';
            
            function ck() {{ 
                if(document.getElementById('ps').value === currentSecret) {{ 
                    document.getElementById('lock-screen').style.display='none'; 
                    document.getElementById('content').style.display='block'; 
                    // Save the actual password string as the token
                    localStorage.setItem('auth', currentSecret); 
                }} else {{
                    document.getElementById('err').innerText = 'Incorrect Password';
                }}
            }}
            
            // Check if their saved token matches the CURRENT secret
            if(localStorage.getItem('auth') === currentSecret) {{ 
                document.getElementById('lock-screen').style.display='none'; 
                document.getElementById('content').style.display='block'; 
            }}
            
            function sw(i) {{ document.querySelectorAll('.tab').forEach((t,j)=>j==i?t.classList.add('active'):t.classList.remove('active')); document.querySelectorAll('.pane').forEach((p,j)=>j==i?p.classList.add('active-pane') : p.classList.remove('active-pane')); }}
        </script>
    </body></html>"""
    with open("index.html", "w", encoding="utf-8") as f: f.write(HTML)

# ==========================================
#  7. MAIN TRIGGER (THE MONSTER MATCHER)
# ==========================================
if __name__ == "__main__":
    print(f"🚀 Sniper Booting... {len(API_KEYS)} Keys Loaded.")
    results = fetch_all_sports()
    bc_data = fetch_bcgame_custom()
    
    if bc_data:
        print(f"🔄 Attempting to merge {len(bc_data)} BC.Game matches...")
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
                    
                    h_match = (bc_h == api_h) or any(w in api_h for w in bc_h.split() if len(w) > 4)
                    a_match = (bc_a == api_a) or any(w in api_a for w in bc_a.split() if len(w) > 4)
                    
                    if h_match and a_match:
                        if 'bookmakers' not in ev: ev['bookmakers'] = []
                        ev['bookmakers'].append(bc['bookmakers'][0])
                        merge_count += 1; linked = True
                        print(f"  🔗 LINKED: {bc['home_team']} <-> {ev['home_team']}")
                        break
                if linked: break
        print(f"✅ SUCCESSFULLY INJECTED {merge_count} BC.GAME MATCHES!")

    evs, arbs = process_markets(results)
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    generate_web(evs, arbs)
    print(f"✅ Sync Complete. EV: {len(evs)} | ARB: {len(arbs)}")
