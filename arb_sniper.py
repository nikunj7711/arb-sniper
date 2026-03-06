import os, json, requests, time, threading
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
#  STATE & API MANAGER
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
        api_state['stats'][str(idx)]['remaining'] = rem
        save_json('api_state.json', api_state)

# ==========================================
#  CORE MATH ENGINE
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

def fetch_all_sports_parallel():
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
        return ist.strftime("%d %b, %I:%M %p IST")
    except: return "Unknown Time"

# ==========================================
#  DATA CRUNCHER
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
                                    breakdown = [{'bookie': k, 'odds': v, 'ev': ((v/true_odds)-1)*100, 'best': k==best_bk} for k,v in softs[side].items()]
                                    breakdown.append({'bookie': 'pinnacle', 'odds': pinny[side], 'ev': 0, 'best': False})
                                    all_evs.append({
                                        'pct': ev_pct, 'home': home, 'away': away, 'time': match_time, 'sport': sport.replace('_', ' ').upper(),
                                        'line': lk, 'sel': side, 'odds': best_p, 'trueO': true_odds, 'bk': best_bk,
                                        'stk': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk),
                                        'conf': max(0, min(100, int((abs((1/best_p) - (1/true_odds)) / (1/true_odds)) * 500))),
                                        'bd': breakdown
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
#  WEB GENERATOR (GLOBAL SYNC)
# ==========================================
def generate_web(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    build_time = ist_now.strftime('%d %b %Y, %I:%M %p IST')
    
    masked_keys = [f"{k[:4]}••••{k[-4:]}" for k in API_KEYS]
    
    HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARB SNIPER | Global Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@500;700;800&display=swap" rel="stylesheet">
<style>
    :root {
        --bg:#09090b; --bg-card:#18181b; --border:#27272a; --hover:#3f3f46;
        --cyan:#06b6d4; --green:#10b981; --gold:#f59e0b; --red:#ef4444; --purple:#8b5cf6;
        --text-m:#f4f4f5; --text-s:#a1a1aa;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text-m); padding: 15px; max-width: 900px; margin: auto; }
    
    /* HEADER */
    .hdr { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 20px; }
    .hdr-title { font-size: 24px; font-weight: 800; background: linear-gradient(90deg, var(--cyan), var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .hdr-sub { font-family: 'JetBrains Mono'; font-size: 11px; color: var(--text-s); margin-top: 5px; }
    .sync-badge { background: rgba(16,185,129,0.1); border: 1px solid var(--green); color: var(--green); padding: 5px 10px; border-radius: 6px; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: 700; display: flex; align-items: center; gap: 5px; }
    .sync-dot { width: 6px; height: 6px; background: var(--green); border-radius: 50%; box-shadow: 0 0 8px var(--green); animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

    /* CONTROLS */
    .ctrl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }
    .ctrl-box { background: var(--bg-card); border: 1px solid var(--border); padding: 12px; border-radius: 10px; }
    .lbl { font-size: 11px; color: var(--text-s); text-transform: uppercase; font-weight: 800; margin-bottom: 5px; display: block; letter-spacing: 1px; }
    .inp { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--gold); padding: 8px 12px; border-radius: 6px; font-family: 'JetBrains Mono'; font-weight: 700; font-size: 16px; outline: none; }
    .inp:focus { border-color: var(--gold); }
    .btn-sync { width: 100%; background: linear-gradient(135deg, var(--cyan), #0284c7); border: none; color: white; padding: 12px; border-radius: 8px; font-weight: 800; cursor: pointer; font-size: 14px; text-transform: uppercase; box-shadow: 0 4px 15px rgba(6,182,212,0.3); transition: 0.2s; grid-column: 1 / -1; }
    .btn-sync:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(6,182,212,0.5); }

    /* TABS */
    .tabs { display: flex; gap: 5px; background: var(--bg-card); padding: 5px; border-radius: 10px; border: 1px solid var(--border); margin-bottom: 20px; }
    .tab { flex: 1; text-align: center; padding: 10px; border-radius: 6px; cursor: pointer; font-weight: 600; color: var(--text-s); transition: 0.2s; font-size: 13px; }
    .tab.active { background: var(--hover); color: var(--text-m); }
    .pane { display: none; } .pane.active { display: block; animation: fadeIn 0.3s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

    /* CINEMATIC CARDS (HIGH DETAIL) */
    .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 15px; position: relative; overflow: hidden; }
    .card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
    .card.ev::before { background: var(--cyan); } .card.arb::before { background: var(--gold); }
    
    .c-hdr { padding: 12px 15px; border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02); display: flex; justify-content: space-between; align-items: center; }
    .c-sport { font-size: 11px; color: var(--text-s); font-weight: 700; letter-spacing: 1px; display: flex; align-items: center; gap: 5px; }
    .c-time { font-family: 'JetBrains Mono'; font-size: 11px; color: var(--text-s); }
    
    .c-match { padding: 15px; display: flex; justify-content: space-between; align-items: center; }
    .teams { flex: 1; }
    .team { font-size: 16px; font-weight: 800; margin: 4px 0; }
    .vs { font-size: 10px; color: var(--text-s); font-style: italic; font-weight: 700; }
    .edge-badge { font-family: 'JetBrains Mono'; font-size: 22px; font-weight: 800; padding: 8px 12px; border-radius: 8px; border: 1px solid; text-align: center; }
    .badge-ev { color: var(--cyan); border-color: rgba(6,182,212,0.3); background: rgba(6,182,212,0.1); }
    .badge-arb { color: var(--gold); border-color: rgba(245,158,11,0.3); background: rgba(245,158,11,0.1); }

    .c-action { padding: 15px; background: rgba(0,0,0,0.2); border-top: 1px dashed var(--border); }
    .action-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .hl-cyan { color: var(--cyan); font-weight: 800; } .hl-gold { color: var(--gold); font-weight: 800; }
    
    .leg { display: flex; justify-content: space-between; align-items: center; background: var(--bg); padding: 10px; border-radius: 8px; margin-bottom: 5px; border: 1px solid var(--border); }
    .bk-tag { font-size: 10px; background: var(--hover); padding: 2px 6px; border-radius: 4px; color: var(--text-m); }

    .bd-tbl { width: 100%; border-collapse: collapse; margin-top: 10px; font-family: 'JetBrains Mono'; font-size: 11px; }
    .bd-tbl td, .bd-tbl th { padding: 6px; border-bottom: 1px solid var(--border); text-align: left; }
    .bd-tbl th { color: var(--text-s); font-size: 9px; text-transform: uppercase; }

    /* NETWORK */
    .net-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
    .net-card { background: var(--bg-card); border: 1px solid var(--border); padding: 15px; border-radius: 10px; }
</style>
</head>
<body>

<div class="wrap">
    <div class="hdr">
        <div>
            <div class="hdr-title">⚡ ARB SNIPER</div>
            <div class="hdr-sub">Global Cloud Synchronized</div>
        </div>
        <div class="sync-badge">
            <div class="sync-dot"></div> DATA SYNCED: __TIME__
        </div>
    </div>

    <div class="ctrl-grid">
        <button class="btn-sync" onclick="triggerCloudScan()" id="btnSync">🔄 FORCE CLOUD SCAN NOW</button>
        <div class="ctrl-box">
            <span class="lbl">Global Bankroll (₹)</span>
            <input class="inp" type="number" id="cfgBankroll" value="1500" oninput="recalcStakes()">
        </div>
        <div class="ctrl-box">
            <span class="lbl">Kelly Fraction (%)</span>
            <input class="inp" type="number" id="cfgKelly" value="30" max="100" oninput="recalcStakes()">
        </div>
    </div>

    <div class="tabs">
        <div class="tab active" onclick="showPane('ev')">💎 EV (<span id="cntEV">0</span>)</div>
        <div class="tab" onclick="showPane('arb')">🔒 ARB (<span id="cntARB">0</span>)</div>
        <div class="tab" onclick="showPane('net')">📡 NETWORK</div>
        <div class="tab" onclick="showPane('calc')">🧮 CALC</div>
    </div>

    <div id="pane-ev" class="pane active"><div id="ev-container"></div></div>
    <div id="pane-arb" class="pane"><div id="arb-container"></div></div>
    
    <div id="pane-net" class="pane">
        <div class="net-grid" id="net-container"></div>
    </div>
    
    <div id="pane-calc" class="pane">
        <div class="ctrl-box" style="margin-bottom:15px;">
            <span class="lbl">Live Arbitrage Calculator</span>
            <div style="display:flex; gap:10px; margin-top:10px;">
                <input class="inp" type="number" id="cAA" placeholder="Odds A" oninput="calcARB()">
                <input class="inp" type="number" id="cAB" placeholder="Odds B" oninput="calcARB()">
            </div>
            <div style="margin-top:15px; font-family:'JetBrains Mono'; color:var(--text-s);">
                Margin: <strong id="rAM" style="color:var(--text-m)">--</strong> <br>
                ARB %: <strong id="rAP" style="color:var(--gold)">--</strong> <br>
                Profit: <strong id="rAG" style="color:var(--green); font-size:18px;">--</strong>
            </div>
        </div>
    </div>
</div>

<script>
const STATE = {
    evs: __EV_JSON__,
    arbs: __ARB_JSON__,
    apiStats: __API_STATE__,
    maskedKeys: __MASKED_KEYS__,
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

function recalcStakes() {
    const bk = parseFloat(document.getElementById('cfgBankroll').value) || 1500;
    const kf = (parseFloat(document.getElementById('cfgKelly').value) || 30) / 100;
    localStorage.setItem('arb_g_bk', bk);
    
    // EV Recalc
    document.querySelectorAll('.ev-stake').forEach(el => {
        const soft = parseFloat(el.dataset.soft), trueO = parseFloat(el.dataset.true), book = el.dataset.book;
        const b = soft - 1, p = 1 / trueO;
        let k = ((b * p - (1-p)) / b) * kf;
        let s = k > 0 ? Math.max(20, bk * Math.min(k, 0.05)) : 0;
        if (STATE.caps[book]) s = Math.min(s, STATE.caps[book]);
        el.textContent = fmt(s);
    });

    // ARB Recalc
    document.querySelectorAll('.arb-stake').forEach(el => {
        el.textContent = fmt((bk/parseFloat(el.dataset.margin))/parseFloat(el.dataset.price));
    });
    document.querySelectorAll('.arb-profit').forEach(el => {
        el.textContent = fmt((bk/parseFloat(el.dataset.margin))-bk);
    });
    calcARB();
}

function buildUI() {
    document.getElementById('cntEV').textContent = STATE.evs.length;
    document.getElementById('cntARB').textContent = STATE.arbs.length;
    
    // EV CARDS (High Detail)
    let evHtml = '';
    STATE.evs.forEach(ev => {
        let bRows = '<tr><th>Bookmaker</th><th>Odds</th><th>EV%</th></tr>';
        ev.bd.forEach(r => { bRows += `<tr style="${r.best ? 'color:var(--cyan);font-weight:700;':''}"><td>${bkName(r.bookie)}</td><td>${r.odds.toFixed(3)}</td><td>${r.ev>0?'+':''}${r.ev.toFixed(2)}%</td></tr>`; });
        
        evHtml += `
        <div class="card ev">
            <div class="c-hdr">
                <span class="c-sport">🏆 ${ev.sport}</span>
                <span class="c-time">📅 ${ev.time}</span>
            </div>
            <div class="c-match">
                <div class="teams">
                    <div class="team">${ev.home}</div>
                    <div class="vs">vs</div>
                    <div class="team">${ev.away}</div>
                </div>
                <div class="edge-badge badge-ev">${ev.pct.toFixed(2)}%</div>
            </div>
            <div class="c-action">
                <div style="font-size:11px; color:var(--text-s); text-transform:uppercase; margin-bottom:5px;">Target Line: <strong style="color:var(--text-m)">${ev.line}</strong></div>
                <div class="action-row">
                    <span style="font-size:16px;">👉 Bet <strong>${ev.sel.toUpperCase()}</strong> @ <span class="hl-cyan">${ev.odds.toFixed(2)}</span></span>
                    <span class="bk-tag">${bkName(ev.bk)}</span>
                </div>
                <div class="action-row" style="background:var(--bg); padding:10px; border-radius:8px; border:1px solid var(--border);">
                    <div><span style="font-size:10px; color:var(--text-s);">KELLY STAKE</span><br><strong class="ev-stake hl-cyan" data-soft="${ev.odds}" data-true="${ev.trueO}" data-book="${ev.bk}" style="font-size:20px;">${fmt(ev.stk)}</strong></div>
                    <div style="text-align:right;"><span style="font-size:10px; color:var(--text-s);">TRUE ODDS / CONFIDENCE</span><br><strong style="font-family:'JetBrains Mono'">${ev.trueO.toFixed(3)} | ${ev.conf}/100</strong></div>
                </div>
                <details style="margin-top:10px; font-size:12px; color:var(--text-s); cursor:pointer;"><summary>View Odds Matrix</summary><table class="bd-tbl">${bRows}</table></details>
            </div>
        </div>`;
    });
    document.getElementById('ev-container').innerHTML = evHtml || '<div style="text-align:center; padding:40px; color:var(--text-s);">No EV edges currently available.</div>';

    // ARB CARDS (High Detail)
    let arbHtml = '';
    STATE.arbs.forEach(arb => {
        const margin = 1 - (arb.pct/100);
        let legs = '';
        arb.sides.forEach(s => {
            legs += `<div class="leg">
                <span>${s.sel.toUpperCase()} @ <strong class="hl-gold">${s.pr.toFixed(2)}</strong> <span class="bk-tag">${bkName(s.bk)}</span></span>
                <strong class="arb-stake hl-cyan" data-margin="${margin}" data-price="${s.pr}">${fmt(s.stk)}</strong>
            </div>`;
        });
        
        arbHtml += `
        <div class="card arb">
            <div class="c-hdr">
                <span class="c-sport">🏆 ${arb.sport}</span>
                <span class="c-time">📅 ${arb.time}</span>
            </div>
            <div class="c-match">
                <div class="teams"><div class="team">${arb.home}</div><div class="vs">vs</div><div class="team">${arb.away}</div></div>
                <div class="edge-badge badge-arb">${arb.pct.toFixed(2)}%</div>
            </div>
            <div class="c-action">
                <div style="font-size:11px; color:var(--text-s); text-transform:uppercase; margin-bottom:8px;">Target Line: <strong style="color:var(--text-m)">${arb.line}</strong> <span class="bk-tag">${arb.ways}-WAY</span></div>
                ${legs}
                <div style="text-align:right; margin-top:10px; font-size:12px; color:var(--text-s);">GUARANTEED PROFIT: <strong class="arb-profit" style="color:var(--green); font-size:20px;" data-margin="${margin}">${fmt(arb.profit)}</strong></div>
            </div>
        </div>`;
    });
    document.getElementById('arb-container').innerHTML = arbHtml || '<div style="text-align:center; padding:40px; color:var(--text-s);">No Arbitrage locks currently available.</div>';

    // NETWORK CARDS
    let netHtml = '';
    STATE.maskedKeys.forEach((mKey, idx) => {
        let rem = STATE.apiStats.stats[idx] ? STATE.apiStats.stats[idx].remaining : 500;
        let c = idx === STATE.apiStats.active_index ? 'var(--cyan)' : (rem > 0 ? 'var(--green)' : 'var(--red)');
        let stat = idx === STATE.apiStats.active_index ? 'ACTIVE' : (rem > 0 ? 'STANDBY' : 'EMPTY');
        netHtml += `<div class="net-card" style="border-left: 3px solid ${c}">
            <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
                <strong style="font-family:'JetBrains Mono'; font-size:14px;">${mKey}</strong>
                <span style="font-size:10px; font-weight:800; color:${c}; background:rgba(255,255,255,0.05); padding:2px 6px; border-radius:4px;">${stat}</span>
            </div>
            <div style="font-size:12px; color:var(--text-s);">Quota Remaining: <strong style="color:var(--text-m); font-family:'JetBrains Mono';">${rem}/500</strong></div>
        </div>`;
    });
    document.getElementById('net-container').innerHTML = netHtml;
}

function calcARB() {
    const a = parseFloat(document.getElementById('cAA').value);
    const b = parseFloat(document.getElementById('cAB').value);
    const bk = parseFloat(document.getElementById('cfgBankroll').value) || 1500;
    if(a && b) {
        const m = 1/a + 1/b;
        document.getElementById('rAM').textContent = m.toFixed(4);
        document.getElementById('rAP').textContent = ((1-m)*100).toFixed(2)+'%';
        document.getElementById('rAG').textContent = fmt((bk/m)-bk);
    }
}

async function triggerCloudScan() {
    let pat = localStorage.getItem('gh_dispatch_token');
    if (!pat) {
        pat = prompt("Enter your GitHub PAT (ghp_...) to trigger the Cloud Engine:");
        if (!pat) return; localStorage.setItem('gh_dispatch_token', pat);
    }
    const btn = document.getElementById('btnSync');
    btn.textContent = "☁️ FIRING CLOUD ENGINE... (WAIT 45s)";
    btn.style.background = "var(--gold)";
    
    try {
        let res = await fetch('https://api.github.com/repos/nikunj7711/arb-sniper/actions/workflows/sniper.yml/dispatches', {
            method: 'POST', headers: { 'Accept': 'application/vnd.github.v3+json', 'Authorization': 'token ' + pat, 'Content-Type': 'application/json' },
            body: JSON.stringify({ ref: 'main' })
        });
        if(res.ok) setTimeout(() => window.location.reload(true), 45000);
        else { alert("Authorization failed."); localStorage.removeItem('gh_dispatch_token'); btn.textContent = "🔄 FORCE CLOUD SCAN NOW"; btn.style.background = ""; }
    } catch(e) { alert("Network error."); }
}

// Auto Refresh every 5 minutes to pull the latest GitHub schedule
setInterval(() => window.location.reload(true), 300000);

window.onload = () => {
    if(localStorage.getItem('arb_g_bk')) document.getElementById('cfgBankroll').value = localStorage.getItem('arb_g_bk');
    buildUI(); recalcStakes();
};
</script>
</body>
</html>"""

    final_html = HTML.replace('__TIME__', build_time)
    final_html = final_html.replace('__EV_JSON__', json.dumps(evs))
    final_html = final_html.replace('__ARB_JSON__', json.dumps(arbs))
    final_html = final_html.replace('__MASKED_KEYS__', json.dumps(masked_keys))
    final_html = final_html.replace('__API_STATE__', json.dumps(api_state))

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(final_html)

# ==========================================
#  MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    print("🚀 Cloud Engine Booting...")
    results = fetch_all_sports_parallel()
    evs, arbs = process_markets(results)
    
    # NTFY PUSH ALERTS
    for a in arbs:
        msg = f"🏆 {a['sport']}\n📅 {a['time']}\n📈 {a['line']}\n\n"
        for s in a['sides']: msg += f"🔵 {s['sel'].upper()} @ {s['pr']:.2f} [{s['bk'].title()}]\n"
        msg += f"\n✨ Profit: ₹{a['profit']:.0f}"
        requests.post("https://ntfy.sh/", json={"topic": NTFY_CHANNEL, "message": msg, "title": f"🚨 {a['pct']:.2f}% ARB | {a['home']} vs {a['away']}", "tags": ["gem", "moneybag"], "priority": 5}, timeout=5)

    for e in evs:
        msg = f"🏆 {e['sport']}\n📅 {e['time']}\n📈 {e['line']}\n\n💰 BET EXACTLY: ₹{e['stk']:.0f}\n👉 {e['sel'].upper()} @ {e['odds']:.2f} on {e['bk'].title()}\n\n🧠 True Odds: {e['trueO']:.3f}"
        requests.post("https://ntfy.sh/", json={"topic": NTFY_CHANNEL, "message": msg, "title": f"📈 {e['pct']:.2f}% EV | {e['home']} vs {e['away']}", "tags": ["gem", "moneybag"], "priority": 5}, timeout=5)

    generate_web(evs, arbs)
    print(f"✅ Global Terminal Synced. Found {len(evs)} EV and {len(arbs)} ARBs.")
