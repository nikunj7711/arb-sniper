import os, json, requests, time, threading, hashlib, sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION & BANK-GRADE SECURITY
# ==========================================
_raw_keys = os.getenv('ODDS_API_KEYS')
if not _raw_keys:
    print("🚨 SEC-FAULT: ODDS_API_KEYS missing. Halting.")
    sys.exit(1)
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

_raw_pass = os.getenv('DASHBOARD_PASS')
if not _raw_pass:
    print("🚨 SEC-FAULT: DASHBOARD_PASS missing. Halting.")
    sys.exit(1)
SECRET_HASH = hashlib.sha256(_raw_pass.encode()).hexdigest()

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' 

# 🚀 AGGRESSIVE PROFIT THRESHOLDS (High Volume)
MIN_EV_THRESHOLD = 0.5                  
MIN_ARB_THRESHOLD = 0.1                 

MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'
TARGET_SPORTS = ['soccer_epl', 'soccer_spain_la_liga', 'soccer_uefa_champs_league', 
                 'basketball_nba', 'icehockey_nhl', 'soccer_italy_serie_a', 'upcoming']

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
            print(f"🔄 Rotated to Key #{api_state['active_index'] + 1}")
        return api_state['active_index'] < len(API_KEYS)

def update_key_stats(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        if rem is not None: api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  3. DATA FETCHERS (RESTORED TOTALS)
# ==========================================
def fetch_bcgame_custom():
    headers = {'accept': '*/*', 'origin': 'https://bc.game', 'user-agent': 'Mozilla/5.0'}
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

            # 🛠️ RESTORED OVER/UNDER MARKETS FOR BC GAME
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
                print(f"⚠️ API Limit hit on Key #{idx+1}. Rotating...")
                if rotate_api_key(idx): continue
            return None
        except Exception as e: return None

def fetch_all_sports():
    results = {}
    print("📡 Fetching Data...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        # 🛠️ RESTORED BOOKMAKERS FILTER AND TOTALS (h2h,totals)
        futures = {executor.submit(fetch_odds_api, f'https://api.the-odds-api.com/v4/sports/{sp}/odds', {'regions': 'eu,uk,us,au', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals'}): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results

# ==========================================
#  4. MATH ENGINE (AGGRESSIVE)
# ==========================================
def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def format_ist(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b | %I:%M %p")
    except: return "TBD"

def process_markets(results):
    all_evs, all_arbs = [], []
    
    for sport, events in results.items():
        if not events: continue
        
        for event in events:
            commence = event.get('commence_time', '')
            home, away = event.get('home_team', 'A'), event.get('away_team', 'B')
            meta = {'match': f"{home} vs {away}", 'sport': sport.replace('_', ' ').upper(), 'raw_time': commence, 'time': format_ist(commence)}

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
                                    all_evs.append({**meta, 'pct': ev_pct, 'line': lk, 'ways': len(keys), 'sel': side, 'odds': best_p, 'trueO': true_os[idx], 'bk': best_bk})

            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                if len(keys) in [2, 3]:
                    margin = sum(1/outs[k]['price'] for k in keys)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {**meta, 'pct': (1-margin)*100, 'line': lk, 'ways': len(keys), 'margin': margin, 'sides': []}
                        for k in keys: arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie']})
                        all_arbs.append(arb)
                        
    return all_evs, all_arbs

# ==========================================
#  5. DYNAMIC UI GENERATOR
# ==========================================
def generate_web(evs, arbs):
    ist_now = (datetime.now(timezone.utc) + timedelta(hours=5.5)).strftime('%d %b, %I:%M %p IST')
    
    keys_html = ""
    for idx, key in enumerate(API_KEYS):
        stats = api_state.get('stats', {})
        rem = stats.get(str(idx), {}).get('remaining', '??')
        is_active = (idx == api_state.get('active_index', 0))
        color = "#06b6d4" if is_active else "#3f3f46"
        status = "ACTIVE" if is_active else "IDLE"
        masked = f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else "ERR"
        keys_html += f"<div style='background:#18181b; border:1px solid #27272a; padding:15px; border-radius:8px; border-left:4px solid {color}; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;'><div><div style='font-size:12px; color:#a1a1aa; margin-bottom:4px;'>KEY #{idx+1} <span style='background:#000; padding:2px 6px; border-radius:4px; font-size:9px; margin-left:5px; color:{color}; border:1px solid {color};'>{status}</span></div><div style='font-family:monospace; color:#fff;'>{masked}</div></div><div style='text-align:right;'><div style='font-size:10px; color:#a1a1aa;'>CALLS</div><div style='font-size:20px; font-weight:bold; color:{color};'>{rem}</div></div></div>"

    js_arbs_data = json.dumps(arbs[:150]) 
    js_evs_data = json.dumps(evs[:150])
    
    HTML = f"""<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js"></script>
    <style>
        body {{ background:#09090b; color:#fff; font-family:-apple-system, sans-serif; padding:15px; max-width:600px; margin:auto; }}
        .card {{ background:#18181b; border:1px solid #27272a; padding:15px; border-radius:12px; margin-bottom:12px; }}
        .tab {{ flex:1; text-align:center; padding:12px; background:#18181b; border-radius:8px; cursor:pointer; font-size:12px; color:#666; font-weight:bold; border:1px solid transparent; }}
        .tab.active {{ background:#3f3f46; color:#fff; border-color:#06b6d4; }}
        .pane {{ display:none; }} .active-pane {{ display:block; }}
        .btn {{ background:#27272a; color:#fff; border:none; padding:6px 10px; border-radius:6px; cursor:pointer; font-size:11px; }}
        .btn-calc {{ background: rgba(6,182,212,0.2); color:#06b6d4; border:1px solid #06b6d4; }}
        .badge {{ font-size:10px; padding:3px 7px; border-radius:5px; font-weight:bold; color:#000; }}
        .input-box {{ background:#000; border:1px solid #27272a; color:#10b981; padding:12px; border-radius:8px; width:100%; font-size:16px; font-weight:bold; margin-bottom:15px; box-sizing:border-box; outline:none; }}
        .input-box:focus {{ border-color: #06b6d4; }}
        .clock {{ background:rgba(16,185,129,0.1); color:#10b981; padding:3px 7px; border-radius:4px; font-family:monospace; }}
    </style></head><body>
        <div id='lock-screen' style='position:fixed; top:0; left:0; width:100%; height:100%; background:#09090b; z-index:999; display:flex; flex-direction:column; align-items:center; justify-content:center;'>
            <h2 style='color:#06b6d4; letter-spacing: 2px;'>⚡ SNIPER TERMINAL</h2>
            <input type='password' id='ps' style='background:#18181b; border:1px solid #27272a; color:#fff; padding:12px; border-radius:8px; text-align:center; margin-bottom:10px; width:200px;' placeholder='Authorization Code'>
            <button onclick='ck()' style='background:#06b6d4; color:#000; border:none; padding:10px 20px; border-radius:8px; font-weight:bold; cursor:pointer; width:225px;'>ENTER SYSTEM</button>
            <p id='err' style='color:#ef4444; font-size:12px; margin-top:10px;'></p>
        </div>

        <div id='content' style='display:none;'>
            <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;'>
                <div><h2 style='color:#06b6d4; margin:0;'>⚡ SNIPER PRO</h2><small style='color:#444;'>Synced: {ist_now}</small></div>
                <button onclick='localStorage.clear(); location.reload();' class='btn' style='background:#ef4444;'>LOGOUT</button>
            </div>

            <div style='background:#18181b; padding:15px; border-radius:12px; border:1px solid #27272a; margin-bottom:15px;'>
                <label style='font-size:11px; color:#a1a1aa; font-weight:bold; display:block; margin-bottom:8px;'>MASTER BANKROLL (₹)</label>
                <input type='number' id='userBankroll' class='input-box' value='5000' style='margin-bottom:0;' oninput='saveBankroll()'>
            </div>

            <div style='display:flex; gap:5px; margin-bottom:20px; position:sticky; top:0; z-index:10; background:#09090b; padding-bottom:10px;'>
                <div class='tab active' onclick='sw(0)'>ARBS ({len(arbs)})</div>
                <div class='tab' onclick='sw(1)'>EV ({len(evs)})</div>
                <div class='tab' onclick='sw(2)'>CALC</div>
                <div class='tab' onclick='sw(3)'>KEYS</div>
            </div>

            <div id='p0' class='pane active-pane'></div>
            <div id='p1' class='pane'></div>
            
            <div id='p2' class='pane'>
                <div class='card'>
                    <h3 style='margin-top:0; color:#06b6d4;'>Advanced 2-Way/3-Way Calculator</h3>
                    <label style='font-size:10px; color:#aaa;'>Investment Amount</label>
                    <input type='number' id='calcBank' class='input-box' placeholder='Investment Amount' oninput='runCalc()'>
                    <label style='font-size:10px; color:#aaa;'>Odd 1</label>
                    <input type='number' id='odd1' class='input-box' placeholder='Odd 1' oninput='runCalc()'>
                    <label style='font-size:10px; color:#aaa;'>Odd 2</label>
                    <input type='number' id='odd2' class='input-box' placeholder='Odd 2' oninput='runCalc()'>
                    <label style='font-size:10px; color:#aaa;'>Odd 3 (Optional for 3-Way)</label>
                    <input type='number' id='odd3' class='input-box' placeholder='Odd 3 (Optional)' oninput='runCalc()'>
                    <div id='calcResult' style='margin-top:5px; padding:15px; background:#000; border-radius:8px; border:1px solid #222;'>Awaiting Input...</div>
                </div>
            </div>

            <div id='p3' class='pane'>{keys_html}</div>
        </div>

        <script>
            const EXPECTED_HASH = '{SECRET_HASH}';
            const rawArbs = {js_arbs_data};
            const rawEvs = {js_evs_data};

            if(localStorage.getItem('savedBankroll')) {{
                document.getElementById('userBankroll').value = localStorage.getItem('savedBankroll');
                document.getElementById('calcBank').value = localStorage.getItem('savedBankroll');
            }}

            function saveBankroll() {{
                const val = document.getElementById('userBankroll').value;
                localStorage.setItem('savedBankroll', val);
                document.getElementById('calcBank').value = val;
                renderAll(); runCalc();
            }}

            function ck() {{ 
                if(CryptoJS.SHA256(document.getElementById('ps').value).toString() === EXPECTED_HASH) {{ 
                    document.getElementById('lock-screen').style.display='none'; 
                    document.getElementById('content').style.display='block'; 
                    localStorage.setItem('auth_hash', EXPECTED_HASH); 
                    renderAll();
                }} else {{ document.getElementById('err').innerText = 'Access Denied: Invalid Code'; }}
            }}
            
            if(localStorage.getItem('auth_hash') === EXPECTED_HASH) {{ 
                document.getElementById('lock-screen').style.display='none'; 
                document.getElementById('content').style.display='block'; 
                renderAll();
            }}

            function sw(i) {{ document.querySelectorAll('.tab').forEach((t,j)=>j==i?t.classList.add('active'):t.classList.remove('active')); document.querySelectorAll('.pane').forEach((p,j)=>j==i?p.classList.add('active-pane') : p.classList.remove('active-pane')); window.scrollTo(0,0); }}

            function formatTimeDiff(iso) {{
                let d = new Date(iso) - new Date();
                if(d < 0) return "LIVE NOW";
                return `${{Math.floor(d/3600000)}}h ${{Math.floor((d%3600000)/60000)}}m`;
            }}

            setInterval(() => {{
                document.querySelectorAll('.live-clock').forEach(el => el.innerText = '⏳ ' + formatTimeDiff(el.dataset.iso));
            }}, 1000);

            function copyText(txt) {{ navigator.clipboard.writeText(txt); alert("Copied: " + txt); }}

            function renderAll() {{
                const bank = parseFloat(document.getElementById('userBankroll').value) || 0;
                
                let arbHTML = "";
                rawArbs.forEach((a, i) => {{
                    let profit = (bank / a.margin) - bank;
                    let pColor = profit > 0 ? '#10b981' : '#ef4444';
                    let legs = "";
                    
                    a.sides.forEach(s => {{
                        let rawStk = (bank / a.margin) / s.pr;
                        let roundedStk = Math.round(rawStk / 10) * 10; 
                        legs += `<div style='display:flex; justify-content:space-between; align-items:center; background:#000; padding:10px; border-radius:6px; margin-top:6px; border:1px solid #222;'>
                            <span>${{s.sel}} @ <b style='color:#f59e0b'>${{s.pr}}</b> <small style='color:#aaa'>(${{s.bk.toUpperCase()}})</small></span>
                            <div style='text-align:right;'><div style='color:#fff; font-weight:bold;'>₹${{roundedStk}}</div><div style='color:#666; font-size:9px;'>Ex: ₹${{rawStk.toFixed(1)}}</div></div>
                        </div>`;
                    }});

                    arbHTML += `<div class='card' style='border-left: 4px solid #f59e0b;'>
                        <div style='display:flex; justify-content:space-between; align-items:center;'>
                            <span class='badge' style='background:#f59e0b;'>${{a.pct.toFixed(2)}}% ARB</span>
                            <div style='display:flex; gap:5px;'>
                                <button class='btn btn-calc' onclick='sendToCalc(${{a.sides[0]?.pr || 0}}, ${{a.sides[1]?.pr || 0}}, ${{a.sides[2]?.pr || 0}})'>🧮 CALC</button>
                                <button class='btn' style='background:#3f3f46;' onclick='copyText("${{a.match}}")'>📋 COPY</button>
                            </div>
                        </div>
                        <div style='font-size:16px; font-weight:bold; margin: 10px 0;'>${{a.match}}</div>
                        <div style='display:flex; gap:10px; font-size:11px; margin-bottom:10px;'>
                            <span style='background:#222; padding:3px 7px; border-radius:4px;'>📅 ${{a.time}}</span>
                            <span class='live-clock clock' data-iso='${{a.raw_time}}'></span>
                        </div>
                        <div style='font-size:12px; color:#aaa;'>${{a.line}} (${{a.ways}}-Way)</div>
                        ${{legs}}
                        <div style='text-align:right; font-weight:bold; color:${{pColor}}; margin-top:12px; font-size:18px;'>Est. Profit: ₹${{profit.toFixed(0)}}</div>
                    </div>`;
                }});
                document.getElementById('p0').innerHTML = arbHTML || "<div style='padding:20px; color:#aaa;'>No Matches.</div>";

                let evHTML = "";
                rawEvs.slice(0,150).forEach((e, i) => {{
                    let b = e.odds - 1; let p = 1 / e.trueO;
                    let kelly = ((b * p - (1-p)) / b) * 0.30;
                    let rawStk = Math.min(Math.max(20, bank * Math.min(kelly, 0.05)), 1000);
                    let roundedStk = Math.round(rawStk / 10) * 10;
                    
                    evHTML += `<div class='card' style='border-left: 4px solid #06b6d4;'>
                        <div style='display:flex; justify-content:space-between; align-items:center;'><span class='badge' style='background:#06b6d4;'>${{e.pct.toFixed(2)}}% EV</span><div style='display:flex; gap:5px;'><span class='live-clock clock' data-iso='${{e.raw_time}}'></span><button class='btn' style='background:#3f3f46;' onclick='copyText("${{e.match}}")'>📋</button></div></div>
                        <div style='font-size:16px; font-weight:bold; margin: 10px 0;'>${{e.match}}</div>
                        <div style='background:#000; padding:12px; border-radius:6px; border:1px solid #222;'>Bet <b>${{e.sel.toUpperCase()}}</b> @ <b style='color:#06b6d4;'>${{e.odds}}</b> <span style='float:right; color:#888;'>${{e.bk.toUpperCase()}}</span></div>
                        <div style='display:flex; justify-content:space-between; align-items:center; margin-top:10px;'>
                            <div><div style='color:#fff; font-weight:bold;'>Stake: ₹${{roundedStk}}</div><div style='color:#666; font-size:9px;'>Exact: ₹${{rawStk.toFixed(2)}}</div></div>
                            <span style='color:#444; font-size:11px;'>True: ${{e.trueO.toFixed(3)}}</span>
                        </div>
                    </div>`;
                }});
                document.getElementById('p1').innerHTML = evHTML || "<div style='padding:20px; color:#aaa;'>No Matches.</div>";
            }}

            function sendToCalc(o1, o2, o3) {{ sw(2); document.getElementById('odd1').value = o1; document.getElementById('odd2').value = o2; document.getElementById('odd3').value = o3 > 0 ? o3 : ""; runCalc(); }}

            function runCalc() {{
                const b = parseFloat(document.getElementById('calcBank').value) || 0;
                const o1 = parseFloat(document.getElementById('odd1').value) || 0;
                const o2 = parseFloat(document.getElementById('odd2').value) || 0;
                const o3 = parseFloat(document.getElementById('odd3').value) || 0;
                
                if(o1 > 0 && o2 > 0) {{
                    let invSum = (1/o1) + (1/o2) + (o3 > 0 ? (1/o3) : 0);
                    let pct = (1 - invSum) * 100;
                    let stk1 = (b/invSum) / o1;
                    let stk2 = (b/invSum) / o2;
                    let stk3 = o3 > 0 ? ((b/invSum) / o3) : 0;
                    let prof = (b/invSum) - b;
                    let color = prof > 0 ? '#10b981' : '#ef4444';
                    
                    let resultHTML = `<div style='display:flex; justify-content:space-between; margin-bottom:8px; color:#ccc;'><span>Leg 1 Exact:</span><b style='color:#fff;'>₹${{stk1.toFixed(2)}}</b></div><div style='display:flex; justify-content:space-between; margin-bottom:10px; color:#ccc;'><span>Leg 2 Exact:</span><b style='color:#fff;'>₹${{stk2.toFixed(2)}}</b></div>`;
                    if(o3 > 0) resultHTML += `<div style='display:flex; justify-content:space-between; margin-bottom:10px; color:#ccc;'><span>Leg 3 Exact:</span><b style='color:#fff;'>₹${{stk3.toFixed(2)}}</b></div>`;
                    resultHTML += `<hr style='border:none; border-top:1px solid #333; margin:15px 0;'><div style='display:flex; justify-content:space-between; margin-bottom:8px; color:${{color}};'><span>Arbitrage %:</span><b style='font-size:16px;'>${{pct.toFixed(2)}}%</b></div><div style='display:flex; justify-content:space-between; color:${{color}}; align-items:center;'><span>Profit:</span><b style='font-size:24px;'>₹${{prof.toFixed(0)}}</b></div>`;
                    document.getElementById('calcResult').innerHTML = resultHTML;
                }} else {{ document.getElementById('calcResult').innerHTML = "Awaiting valid odds input..."; }}
            }}
        </script>
    </body></html>"""
    with open("index.html", "w", encoding="utf-8") as f: f.write(HTML)

# ==========================================
#  6. MAIN TRIGGER & NTFY ALERTS
# ==========================================
if __name__ == "__main__":
    print(f"🚀 Sniper Booting... {len(API_KEYS)} Keys Loaded.")
    results = fetch_all_sports()
    bc_data = fetch_bcgame_custom()

    if bc_data:
        for bc in bc_data:
            def clean(n): return n.lower().replace('(holis)', '').replace('(e)', '').replace('fc ', '').replace(' fc', '').replace('real ', '').replace('as ', '').strip()
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
                        linked = True; break
                if linked: break

    evs, arbs = process_markets(results)
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    
    generate_web(evs, arbs)
    
    # 🔔 NTFY ALERTS
    if arbs:
        top_arb = arbs[0]
        alert_msg = f"Sniper found {len(arbs)} Locks & {len(evs)} EVs. Top Match: {top_arb['match']} ({top_arb['pct']:.2f}%)"
        try:
            requests.post(f"https://ntfy.sh/{NTFY_CHANNEL}", data=alert_msg.encode('utf-8'), headers={"Title": "ARB SNIPER SYNCED", "Tags": "moneybag,zap"}, timeout=10)
        except Exception as e: pass

    print(f"✅ Sync Complete. EV: {len(evs)} | ARB: {len(arbs)}")
