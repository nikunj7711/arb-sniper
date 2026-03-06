import requests
import time
import os
import json
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  CONFIGURATION & ENVIRONMENT
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
#  STATE MANAGEMENT (JSON MEMORY)
# ==========================================
api_lock = threading.Lock()
cache_lock = threading.Lock()

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

# --- ADVANCED API SEQUENTIAL MANAGER ---
api_state = load_json('api_state.json', {'active_index': 0, 'stats': {}})

def get_active_api_key():
    with api_lock:
        idx = api_state.get('active_index', 0)
        if idx >= len(API_KEYS):
            return None, idx
        return API_KEYS[idx], idx

def rotate_api_key(failed_idx):
    with api_lock:
        # Only rotate if another thread hasn't already rotated it
        if api_state.get('active_index', 0) == failed_idx:
            api_state['active_index'] += 1
            save_json('api_state.json', api_state)
            print(f"🔄 Key #{failed_idx + 1} Exhausted! Switching to Key #{api_state['active_index'] + 1}")
        return api_state['active_index'] < len(API_KEYS)

def update_key_telemetry(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']:
            api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = rem
        save_json('api_state.json', api_state)

# --- CACHE & BANKROLL MANAGERS ---
def is_duplicate_alert(match, line, selection, odds):
    with cache_lock:
        cache = load_json('alert_cache.json', {})
        now_ts = time.time()
        
        # Bulletproof cleanup: safely ignores old string data from previous versions
        clean_cache = {}
        for k, v in cache.items():
            if isinstance(v, (float, int)) and (now_ts - v < 6*3600):
                clean_cache[k] = v
                
        cache = clean_cache
        alert_key = f"{match}|{line}|{selection}|{odds:.2f}"
        
        if alert_key in cache:
            save_json('alert_cache.json', cache)
            return True
            
        cache[alert_key] = now_ts
        save_json('alert_cache.json', cache)
        return False

def update_bankroll_state(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_str = ist_now.strftime('%Y-%m-%d')
    state = load_json('bankroll_state.json', {})
    if state.get('date') != today_str:
        state = {'date': today_str, 'starting_bankroll': TOTAL_BANKROLL, 'total_stakes': 0, 'theoretical_profit': 0, 'ev_exposure': 0}
    for ev in evs:
        state['total_stakes'] += ev['stake']
        state['ev_exposure'] += ev['stake'] * (ev['pct']/100)
    for arb in arbs:
        state['total_stakes'] += arb['stk1'] + arb['stk2'] + arb.get('stk3', 0)
        state['theoretical_profit'] += arb['profit']
    save_json('bankroll_state.json', state)
    return state

def compute_clv(all_evs):
    history = load_json('history_log.json', [])
    prev_by_key = {f"{e['match']}|{e['line']}|{e.get('selection','')}": e for e in history if e.get('type') == 'EV'}
    updated = False
    for ev in all_evs:
        k = f"{ev['match']}|{ev['line']}|{ev['selection']}"
        if k in prev_by_key and prev_by_key[k].get('true_odds', 0) > 1.0:
            clv_pct = ((ev['true'] / prev_by_key[k]['true_odds']) - 1) * 100
            ev['clv_pct'] = round(clv_pct, 3)
        else:
            ev['clv_pct'] = None
    return all_evs

# ==========================================
#  CORE UTILITIES
# ==========================================
def send_phone_alert(msg, pct, match, a_type):
    try:
        emoji = "🚨" if a_type == "ARB" else "📈"
        requests.post("https://ntfy.sh/", json={
            "topic": NTFY_CHANNEL, "message": msg,
            "title": f"{emoji} {pct:.2f}% {a_type} | {match}",
            "tags": ["gem", "moneybag"], "priority": 5
        }, timeout=5)
    except: pass

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
#  API FETCHING (CLOUD)
# ==========================================
def fetch_odds_with_retry(url, params):
    while True:
        key, idx = get_active_api_key()
        if not key: return None
        params['apiKey'] = key
        
        try:
            res = requests.get(url, params=params, timeout=15)
        except:
            return None
            
        rem = res.headers.get('x-requests-remaining')
        if rem: update_key_telemetry(idx, rem)

        if res.status_code in [401, 429]:
            if rotate_api_key(idx): continue
            else: return None
        elif res.status_code == 200:
            return res.json()
        else:
            return None

def fetch_all_sports_parallel():
    results = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_odds_with_retry, f'https://api.the-odds-api.com/v4/sports/{sp}/odds', {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'totals,spreads', 'oddsFormat': 'decimal'}): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results

# ==========================================
#  MATH ENGINE
# ==========================================
def extract_hybrid_data(bookmakers_list):
    ev_lines, arb_lines = {}, {}
    for bookie in bookmakers_list:
        b_name = bookie['key']
        for market in bookie.get('markets', []):
            m_type = market['key'].upper()
            for outcome in market['outcomes']:
                point = str(outcome.get('point', '0'))
                name, price = outcome['name'], outcome['price']
                if b_name == 'betfair_ex_eu': price = 1 + (price - 1) * 0.97
                line_key = f"{m_type}_{point}"

                if line_key not in ev_lines: ev_lines[line_key] = {'pin': {}, 'softs': {}}
                if b_name == 'pinnacle': ev_lines[line_key]['pin'][name] = price
                else:
                    if name not in ev_lines[line_key]['softs']: ev_lines[line_key]['softs'][name] = {}
                    ev_lines[line_key]['softs'][name][b_name] = price
                        
                if line_key not in arb_lines: arb_lines[line_key] = {}
                if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                    arb_lines[line_key][name] = {'price': price, 'bookie': b_name}
    return ev_lines, arb_lines

def evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport):
    found_evs, found_arbs = [], []
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
                            breakdown = [{'bookie': k, 'odds': v, 'ev_pct': ((v/true_odds)-1)*100, 'is_best': k==best_bk} for k,v in softs[side].items()]
                            breakdown.append({'bookie': 'pinnacle', 'odds': pinny[side], 'ev_pct': 0, 'is_best': False})
                            found_evs.append({
                                'pct': ev_pct, 'match': match_name, 'time': match_time, 'sport': sport, 'line': lk,
                                'selection': side, 'odds': best_p, 'true': true_odds, 'bookie': best_bk,
                                'stake': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk),
                                'confidence': max(0, min(100, int((abs((1/best_p) - (1/true_odds)) / (1/true_odds)) * 500))),
                                'ev_breakdown': breakdown
                            })

    for lk, outs in arb_lines.items():
        keys = list(outs.keys())
        for ways in [2, 3]:
            if len(keys) < ways: continue
            k_slice = keys[:ways]
            margin = sum(1/outs[k]['price'] for k in k_slice)
            if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                arb = {'pct': (1-margin)*100, 'match': match_name, 'time': match_time, 'sport': sport, 'line': lk, 'ways': ways, 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL}
                for i, k in enumerate(k_slice):
                    arb[f's{i+1}'] = k
                    arb[f's{i+1}_price'] = outs[k]['price']
                    arb[f's{i+1}_bookie'] = outs[k]['bookie']
                    arb[f'stk{i+1}'] = (TOTAL_BANKROLL/margin)/outs[k]['price']
                found_arbs.append(arb)
    return found_evs, found_arbs

# ==========================================
#  WEB DASHBOARD GENERATOR
# ==========================================
def generate_web_dashboard(evs, arbs, cur_time, br_state):
    # Mask API keys for frontend security (e.g. 1a2b••••9z0x)
    masked_keys = [f"{k[:4]}••••{k[-4:]}" if len(k) > 10 else "Invalid Key" for k in API_KEYS]
    
    # Calculate starting cost
    starting_reqs = sum(int(v.get('remaining', 500)) for v in api_state.get('stats', {}).values())

    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARB SNIPER ⚡ | Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@500;700;800&display=swap" rel="stylesheet">
<style>
    :root {
        --bg:#09090b; --bg-card:#18181b; --border:#27272a; --hover:#27272a;
        --cyan:#06b6d4; --green:#10b981; --gold:#f59e0b; --red:#ef4444; --purple:#8b5cf6;
        --text-m:#f4f4f5; --text-s:#a1a1aa;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text-m); padding: 15px; max-width: 900px; margin: auto; }
    
    .hdr { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 20px; }
    .hdr h1 { font-size: 24px; font-weight: 800; background: linear-gradient(90deg, var(--cyan), var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .hdr .time { font-family: 'JetBrains Mono'; font-size: 12px; color: var(--text-s); }
    
    .btn { background: var(--border); color: var(--text-m); border: 1px solid #3f3f46; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: 0.2s; width: 100%; display: flex; justify-content: center; align-items: center; gap: 8px;}
    .btn:hover { background: #3f3f46; }
    .btn-green { background: linear-gradient(135deg, #10b981, #059669); border: none; color: white; box-shadow: 0 4px 15px rgba(16,185,129,0.3); }
    .btn-green:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(16,185,129,0.4); }
    .btn-red { background: linear-gradient(135deg, #ef4444, #b91c1c); border: none; color: white; }

    .ctrl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }
    .ctrl-box { background: var(--bg-card); border: 1px solid var(--border); padding: 12px; border-radius: 10px; }
    .lbl { font-size: 11px; color: var(--text-s); text-transform: uppercase; font-weight: 800; margin-bottom: 5px; display: block; letter-spacing: 1px; }
    .inp { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text-m); padding: 8px 12px; border-radius: 6px; font-family: 'JetBrains Mono'; font-weight: 700; }
    
    .tabs { display: flex; gap: 5px; background: var(--bg-card); padding: 5px; border-radius: 10px; border: 1px solid var(--border); margin-bottom: 20px; }
    .tab { flex: 1; text-align: center; padding: 10px; border-radius: 6px; cursor: pointer; font-weight: 600; color: var(--text-s); transition: 0.2s; }
    .tab.active { background: var(--hover); color: var(--text-m); }
    .pane { display: none; } .pane.active { display: block; animation: fadeIn 0.3s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

    /* Key Network UI */
    .net-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 10px; }
    .net-card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px; display: flex; justify-content: space-between; align-items: center; }
    .net-k { font-family: 'JetBrains Mono'; font-size: 13px; font-weight: 700; }
    .net-rem { font-family: 'JetBrains Mono'; font-size: 12px; font-weight: 800; }
    .s-act { color: var(--cyan); border-color: var(--cyan); background: rgba(6, 182, 212, 0.1); }
    .s-exh { color: var(--red); opacity: 0.6; }
    .s-rdy { color: var(--green); }

    /* Betting Cards */
    .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 15px; margin-bottom: 15px; position: relative; overflow: hidden; }
    .card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
    .card.ev::before { background: var(--cyan); } .card.arb::before { background: var(--gold); }
    .c-hdr { display: flex; justify-content: space-between; margin-bottom: 10px; border-bottom: 1px dashed var(--border); padding-bottom: 10px; }
    .c-tag { font-family: 'JetBrains Mono'; font-weight: 800; font-size: 18px; }
    .hl { color: var(--cyan); } .hl-g { color: var(--gold); }
    .bd-tbl { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
    .bd-tbl td { padding: 5px; border-bottom: 1px solid var(--border); }
</style>
</head>
<body>

<div class="hdr">
    <h1>⚡ ARB SNIPER</h1>
    <div class="time">Last Cloud Sweep:<br>__TIME__</div>
</div>

<div class="ctrl-grid">
    <div class="ctrl-box" style="grid-column: 1 / -1;">
        <button class="btn btn-green" id="btnStart" onclick="startEngine()">▶ START AUTONOMOUS ENGINE</button>
        <button class="btn btn-red" id="btnStop" onclick="stopEngine()" style="display:none;">⏹ STOP ENGINE</button>
        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:12px;">
            <label style="font-size:13px; color:var(--text-s); display:flex; align-items:center; gap:8px;">
                <input type="checkbox" id="autoLoop" checked style="width:16px;height:16px;accent-color:var(--cyan);">
                Auto-Loop (5 min interval)
            </label>
            <span id="scanStatus" style="font-family:'JetBrains Mono'; font-size:12px; color:var(--green); font-weight:700;">SYSTEM IDLE</span>
        </div>
    </div>
    <div class="ctrl-box">
        <span class="lbl">Total Bankroll (₹)</span>
        <input class="inp" type="number" id="cfgBankroll" value="__BANKROLL__" oninput="recalc()">
    </div>
    <div class="ctrl-box">
        <span class="lbl">Kelly Fraction (%)</span>
        <input class="inp" type="number" id="cfgKelly" value="30" max="100" oninput="recalc()">
    </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="showPane('ev')">💎 EV (<span id="cntEV">0</span>)</div>
    <div class="tab" onclick="showPane('arb')">🔒 ARB (<span id="cntARB">0</span>)</div>
    <div class="tab" onclick="showPane('net')">📡 NETWORK</div>
</div>

<div id="pane-ev" class="pane active"><div id="ev-container"></div></div>
<div id="pane-arb" class="pane"><div id="arb-container"></div></div>

<div id="pane-net" class="pane">
    <div class="ctrl-box">
        <span class="lbl">API Sequential Matrix</span>
        <div style="font-size:12px; color:var(--text-s); margin-bottom:10px;">The system automatically fails-over to the next key when a 429 quota limit is reached.</div>
        <div class="net-grid" id="netGrid">
            </div>
    </div>
</div>

<script>
const STATE = {
    evs: __EV_JSON__,
    arbs: __ARB_JSON__,
    keys: __API_KEYS__,
    masked: __MASKED_KEYS__,
    apiState: __API_STATE__,
    activeIdx: __ACTIVE_IDX__,
    isScanning: false,
    loopTimer: null,
    caps: {'betway':300,'stake':500,'onexbet':400,'marathonbet':400,'dafabet':350,'betfair_ex_eu':600,'pinnacle':1000}
};

function showPane(p) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('pane-'+p).classList.add('active');
}

function fmt(n) { return '₹' + Math.round(n).toLocaleString('en-IN'); }
function bkName(k) { const m={'onexbet':'1xBet','pinnacle':'Pinnacle','marathonbet':'Marathon','dafabet':'Dafabet','stake':'Stake.com','betfair_ex_eu':'Betfair','betway':'Betway'}; return m[k]||k; }

function recalc() {
    const bk = parseFloat(document.getElementById('cfgBankroll').value) || 1500;
    const kf = (parseFloat(document.getElementById('cfgKelly').value) || 30) / 100;
    
    // Recalculate EV stakes
    document.querySelectorAll('.ev-stake').forEach(el => {
        const soft = parseFloat(el.dataset.soft);
        const trueO = parseFloat(el.dataset.true);
        const book = el.dataset.book;
        const b = soft - 1, p = 1 / trueO;
        let k = ((b * p - (1-p)) / b) * kf;
        if(k > 0.05) k = 0.05;
        let s = k > 0 ? Math.max(20, bk * k) : 0;
        if (STATE.caps[book]) s = Math.min(s, STATE.caps[book]);
        el.textContent = fmt(s);
    });

    // Recalculate ARB stakes
    document.querySelectorAll('.arb-stake').forEach(el => {
        const margin = parseFloat(el.dataset.margin);
        const price = parseFloat(el.dataset.price);
        el.textContent = fmt((bk/margin)/price);
    });
    document.querySelectorAll('.arb-profit').forEach(el => {
        const margin = parseFloat(el.dataset.margin);
        el.textContent = fmt((bk/margin)-bk);
    });
}

function buildUI() {
    document.getElementById('cntEV').textContent = STATE.evs.length;
    document.getElementById('cntARB').textContent = STATE.arbs.length;
    
    let evHtml = '';
    STATE.evs.forEach(ev => {
        let rows = '';
        ev.ev_breakdown.forEach(r => {
            rows += `<tr style="${r.is_best ? 'color:var(--cyan);font-weight:700;':''}"><td>${bkName(r.bookie)}</td><td>${r.odds.toFixed(3)}</td><td>${r.ev_pct>0?'+':''}${r.ev_pct.toFixed(2)}%</td></tr>`;
        });
        
        evHtml += `
        <div class="card ev">
            <div class="c-hdr">
                <div>
                    <span style="color:var(--text-s);font-size:12px;">${ev.sport.replace(/_/g,' ').toUpperCase()}</span><br>
                    <strong>${ev.match}</strong>
                </div>
                <div class="c-tag hl">${ev.pct.toFixed(2)}%</div>
            </div>
            <div style="margin-bottom:12px;">
                <span style="font-family:'JetBrains Mono'; color:var(--text-s); font-size:11px;">LINE</span><br>
                <span style="font-weight:700;">${ev.line} &nbsp;👉&nbsp; ${ev.selection.toUpperCase()}</span>
            </div>
            <div style="display:flex; justify-content:space-between; background:#27272a; padding:10px; border-radius:8px;">
                <div><span style="font-size:11px; color:var(--text-s);">REC. STAKE</span><br><strong class="ev-stake hl" data-soft="${ev.odds}" data-true="${ev.true}" data-book="${ev.bookie}" style="font-size:18px;">${fmt(ev.stake)}</strong></div>
                <div style="text-align:right;"><span style="font-size:11px; color:var(--text-s);">BOOKMAKER</span><br><strong class="hl-g" style="font-size:16px;">${bkName(ev.bookie)} @ ${ev.odds.toFixed(2)}</strong></div>
            </div>
            <details style="margin-top:10px; font-size:12px; color:var(--text-s); cursor:pointer;"><summary>View Odds Matrix (True: ${ev.true.toFixed(3)})</summary>
                <table class="bd-tbl">${rows}</table>
            </details>
        </div>`;
    });
    document.getElementById('ev-container').innerHTML = evHtml || '<div style="text-align:center; padding:30px; color:var(--text-s);">No EV edges detected in current scan.</div>';

    let arbHtml = '';
    STATE.arbs.forEach(arb => {
        const margin = 1 - (arb.pct/100);
        let legs = '';
        for(let i=1; i<=arb.ways; i++) {
            let pr = arb['s'+i+'_price'], bk = arb['s'+i+'_bookie'];
            legs += `<div style="display:flex; justify-content:space-between; margin-bottom:5px;">
                <span>${arb['s'+i].toUpperCase()} @ <span class="hl-g">${pr.toFixed(2)}</span> [${bkName(bk)}]</span>
                <strong class="arb-stake hl" data-margin="${margin}" data-price="${pr}">${fmt(arb['stk'+i])}</strong>
            </div>`;
        }
        arbHtml += `
        <div class="card arb">
            <div class="c-hdr">
                <div><span style="font-size:12px; color:var(--text-s);">${arb.sport.replace(/_/g,' ').toUpperCase()}</span><br><strong>${arb.match}</strong></div>
                <div class="c-tag hl-g">${arb.pct.toFixed(2)}%</div>
            </div>
            <div style="margin-bottom:10px; font-weight:700;">${arb.line} <span style="font-size:10px; background:#3f3f46; padding:2px 6px; border-radius:4px; margin-left:8px;">${arb.ways}-WAY</span></div>
            <div style="background:#27272a; padding:10px; border-radius:8px; margin-bottom:10px; font-family:'JetBrains Mono'; font-size:13px;">${legs}</div>
            <div style="text-align:right; font-size:12px; color:var(--text-s);">GUARANTEED PROFIT: <strong class="arb-profit" style="color:var(--green); font-size:18px;" data-margin="${margin}">${fmt(arb.profit)}</strong></div>
        </div>`;
    });
    document.getElementById('arb-container').innerHTML = arbHtml || '<div style="text-align:center; padding:30px; color:var(--text-s);">No Arbitrage locks detected.</div>';

    renderNetwork();
    recalc();
}

function renderNetwork() {
    let netHtml = '';
    STATE.masked.forEach((mKey, idx) => {
        let statusClass = "s-rdy", statusText = "STANDBY";
        let rem = STATE.apiState.stats[idx] ? STATE.apiState.stats[idx].remaining : 500;
        
        if (idx === STATE.activeIdx) { statusClass = "s-act"; statusText = "ACTIVE NOW"; }
        else if (rem <= 0) { statusClass = "s-exh"; statusText = "EXHAUSTED"; }
        
        netHtml += `
        <div class="net-card ${statusClass}">
            <div>
                <div style="font-size:10px; color:var(--text-s); margin-bottom:2px;">KEY #${idx+1} &nbsp;[${statusText}]</div>
                <div class="net-k">${mKey}</div>
            </div>
            <div class="net-rem">${rem}/500</div>
        </div>`;
    });
    document.getElementById('netGrid').innerHTML = netHtml;
}

// ── BROWSER CLIENT-SIDE SCANNER ──
function startEngine() {
    STATE.isScanning = true;
    document.getElementById('btnStart').style.display = 'none';
    document.getElementById('btnStop').style.display = '';
    executeSweep();
}

function stopEngine() {
    STATE.isScanning = false;
    clearTimeout(STATE.loopTimer);
    document.getElementById('btnStart').style.display = '';
    document.getElementById('btnStop').style.display = 'none';
    document.getElementById('scanStatus').textContent = "SYSTEM IDLE";
    document.getElementById('scanStatus').style.color = "var(--text-s)";
}

async function executeSweep() {
    if (!STATE.isScanning) return;
    document.getElementById('scanStatus').textContent = "🔄 SWEEP IN PROGRESS...";
    document.getElementById('scanStatus').style.color = "var(--cyan)";
    
    // Notify user we are using the proxy cloud engine to trigger GitHub
    // Since complex math and full history tracking runs best on the cloud Python script, 
    // the JS engine triggers the GitHub Action to run, then reloads when fresh.
    let pat = localStorage.getItem('gh_dispatch_token');
    if (!pat) {
        pat = prompt("Enter your GitHub PAT (ghp_...) to authorize Auto-Loop sweeps:");
        if (!pat) { stopEngine(); return; }
        localStorage.setItem('gh_dispatch_token', pat);
    }
    
    try {
        let res = await fetch('https://api.github.com/repos/nikunj7711/arb-sniper/actions/workflows/sniper.yml/dispatches', {
            method: 'POST',
            headers: { 'Accept': 'application/vnd.github.v3+json', 'Authorization': 'token ' + pat, 'Content-Type': 'application/json' },
            body: JSON.stringify({ ref: 'main' })
        });
        
        if(res.ok) {
            document.getElementById('scanStatus').textContent = "☁️ CLOUD ENGINE FIRED. WAITING 45s...";
            document.getElementById('scanStatus').style.color = "var(--gold)";
            
            // Wait 45 seconds for GitHub to finish building the new index.html, then reload
            STATE.loopTimer = setTimeout(() => {
                if(STATE.isScanning) {
                    if (document.getElementById('autoLoop').checked) {
                        // Set a flag in session storage so it automatically resumes after reload
                        sessionStorage.setItem('auto_resume', 'true');
                    }
                    window.location.reload(true);
                }
            }, 45000);
            
        } else {
            alert("Authorization failed. Resetting token.");
            localStorage.removeItem('gh_dispatch_token');
            stopEngine();
        }
    } catch (e) {
        console.error(e); stopEngine();
    }
}

// Auto-Resume check on page load
window.onload = () => {
    buildUI();
    if (sessionStorage.getItem('auto_resume') === 'true') {
        // Wait 5 minutes before firing the next loop to save credits
        document.getElementById('btnStart').style.display = 'none';
        document.getElementById('btnStop').style.display = '';
        document.getElementById('scanStatus').textContent = "⏳ STANDBY (5 MINUTE TIMEOUT)";
        document.getElementById('scanStatus').style.color = "var(--gold)";
        STATE.isScanning = true;
        
        STATE.loopTimer = setTimeout(() => {
            executeSweep();
        }, 300000); // 5 minutes
    }
};

</script>
</body>
</html>"""

# ==========================================
#  MAIN EXECUTION (THE FACTORY)
# ==========================================
if __name__ == "__main__":
    print("🚀 GitHub Actions Cloud Factory Started...")
    
    # 1. Fetch data
    results = fetch_all_sports_parallel()
    all_evs, all_arbs = [], []
    
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_str = ist_now.strftime('%d %b %Y, %I:%M:%S %p IST')

    # 2. Process data
    for sport, events in results.items():
        if not events: continue
        for event in events:
            match_name = f"{event['home_team']} vs {event['away_team']}"
            match_time = event['commence_time']
            ev_lines, arb_lines = extract_hybrid_data(event.get('bookmakers', []))
            new_evs, new_arbs = evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport)
            all_evs.extend(new_evs)
            all_arbs.extend(new_arbs)

    all_evs = compute_clv(all_evs)
    all_evs.sort(key=lambda x: x['pct'], reverse=True)
    all_arbs.sort(key=lambda x: x['pct'], reverse=True)
    
    bankroll_state = update_bankroll_state(all_evs, all_arbs)

    # 3. Send Push Notifications (with Duplicate Suppression)
    for arb in all_arbs:
        if not is_duplicate_alert(arb['match'], arb['line'], "ARB", arb['pct']):
            msg = f"💎 💰 🚨 {arb['pct']:.2f}% ARB | {arb['match']}\n🏆 {arb['sport'].replace('_', ' ').title()}\n📈 {arb['line']}\n\n✨ Profit: ₹{arb['profit']:.0f}"
            send_phone_alert(msg, arb['pct'], arb['match'], "ARB")

    for ev in all_evs:
        if not is_duplicate_alert(ev['match'], ev['line'], ev['selection'], ev['odds']):
            msg = f"💎 💰 📈 {ev['pct']:.2f}% EV | {ev['match']}\n🏆 {ev['sport'].replace('_', ' ').title()}\n📈 {ev['line']}\n\n💰 BET EXACTLY: ₹{ev['stake']:.0f}\n👉 {ev['selection'].upper()} @ {ev['odds']:.2f} on {ev['bookie'].title()}\n\n🧠 True Odds: {ev['true']:.3f}"
            send_phone_alert(msg, ev['pct'], ev['match'], "EV")

    # 4. Generate the HTML File
    final_html = HTML.replace('__TIME__', current_time_str)
    final_html = final_html.replace('__BANKROLL__', str(TOTAL_BANKROLL))
    final_html = final_html.replace('__EV_JSON__', json.dumps(all_evs))
    final_html = final_html.replace('__ARB_JSON__', json.dumps(all_arbs))
    final_html = final_html.replace('__API_KEYS__', INJECTED_KEYS_JS)
    
    masked_keys = [f"{k[:4]}••••{k[-4:]}" if len(k) > 10 else "Invalid" for k in API_KEYS]
    final_html = final_html.replace('__MASKED_KEYS__', json.dumps(masked_keys))
    final_html = final_html.replace('__API_STATE__', json.dumps(api_state))
    final_html = final_html.replace('__ACTIVE_IDX__', str(api_state.get('active_index', 0)))

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(final_html)
        
    print("✅ Terminal UI Built Successfully.")
    print("📊 API State Tracker:", json.dumps(api_state, indent=2))

