import requests
import time
import os
import json
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  CONFIGURATION
# ==========================================
api_keys_env = os.getenv('ODDS_API_KEYS', '')
API_KEYS = api_keys_env.split(',') if api_keys_env else []

NTFY_CHANNEL = 'nikunj_arb_alerts_2026'
TOTAL_BANKROLL = 1500

MIN_EV_THRESHOLD = 1.5  
MIN_ARB_THRESHOLD = 1.0

MY_BOOKIES = 'pinnacle,onexbet,marathonbet,dafabet,stake,betfair_ex_eu,betway'

TARGET_SPORTS = [
    'soccer_epl', 'soccer_uefa_champs_league',
    'basketball_nba', 'icehockey_nhl',
    'tennis_atp', 'tennis_wta'
]

BOOK_CAPS = {
    'betway': 300,
    'stake': 500,
    'onexbet': 1000,
    'marathonbet': 500,
    'dafabet': 400,
    'betfair_ex_eu': 2000,
    'pinnacle': 2000
}

current_key_index = 0
requests_remaining = "Unknown"
requests_used_total = "Unknown"
scan_starting_used = None

api_lock = threading.Lock()
cache_lock = threading.Lock()

# ==========================================
#  STATE & CACHE HANDLERS
# ==========================================
def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return json.load(f)
        except: return default
    return default

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4)
    except: pass

def check_alert_cache(match, line, selection, odds):
    with cache_lock:
        cache = load_json('alert_cache.json', {})
        now_ts = time.time()
        # Cleanup older than 6 hours
        cache = {k: v for k, v in cache.items() if now_ts - v < 6*3600}
        alert_key = f"{match}_{line}_{selection}_{odds}"
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
        state['ev_exposure'] += ev['stake']
        state['theoretical_profit'] += ev['green_profit']

    for arb in arbs:
        if arb.get('type') == '2-way':
            state['total_stakes'] += arb['stk1'] + arb['stk2']
        elif arb.get('type') == '3-way':
            state['total_stakes'] += arb['stk1'] + arb['stk2'] + arb['stk3']
        state['theoretical_profit'] += arb['profit']

    save_json('bankroll_state.json', state)
    return state

# ==========================================
#  CORE FUNCTIONS
# ==========================================
def get_active_api_key(): 
    return API_KEYS[current_key_index]

def rotate_api_key():
    global current_key_index, scan_starting_used
    current_key_index += 1
    if current_key_index >= len(API_KEYS):
        print(" CRITICAL ERROR: All API keys exhausted!")
        return False
    print(f" Quota reached! Switched to API Key #{current_key_index + 1}")
    scan_starting_used = None
    return True

def send_phone_alert(message, percent, match_name, alert_type):
    try:
        emoji = "" if alert_type == "ARB" else ""
        payload = {
            "topic": NTFY_CHANNEL, "message": message,
            "title": f"{emoji} {percent:.2f}% {alert_type} | {match_name}",
            "tags": ["gem", "moneybag"], "priority": 5
        }
        requests.post("https://ntfy.sh/", json=payload, timeout=10)
    except: pass

def format_time_ist(iso_string):
    try:
        dt_utc = datetime.strptime(iso_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    except: return "Unknown Time"

def display_bookie(api_key):
    mapping = {'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet', 'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'}
    return mapping.get(api_key, api_key.title())

def remove_vig(*odds):
    margin = sum(1/o for o in odds)
    return tuple(1 / ((1/o) / margin) for o in odds)

def calculate_kelly(soft_odds, true_odds, bankroll, bookie=None):
    b = soft_odds - 1.0
    p = 1.0 / true_odds
    q = 1.0 - p
    safe_kelly = ((b * p - q) / b) * 0.30
    if safe_kelly <= 0: return 0
    if safe_kelly > 0.05: safe_kelly = 0.05
    stake = max(20, bankroll * safe_kelly)
    if bookie and bookie in BOOK_CAPS:
        stake = min(stake, BOOK_CAPS[bookie])
    return stake

def calculate_green_up(back_stake, back_odds, lay_odds):
    target_lay_stake = (back_stake * back_odds) / lay_odds
    guaranteed_profit = target_lay_stake - back_stake
    return target_lay_stake, guaranteed_profit

def extract_hybrid_data(bookmakers_list, target_bookies):
    ev_lines = {}; arb_lines = {}
    for bookie in bookmakers_list:
        b_name = bookie['key']
        if b_name not in target_bookies: continue
        for market in bookie.get('markets', []):
            if market['key'] in ['totals', 'spreads', 'h2h']:
                m_type = market['key'].upper()
                for outcome in market['outcomes']:
                    point = str(outcome.get('point', '0')) if m_type != 'H2H' else '0'
                    name = outcome['name']
                    price = outcome['price']
                    if b_name == 'betfair_ex_eu': price = 1 + (price - 1) * 0.97
                    line_key = f"{m_type}_{point}"
                    
                    if line_key not in ev_lines: ev_lines[line_key] = {'pinnacle': {}, 'softs': {}}
                    if b_name == 'pinnacle': ev_lines[line_key]['pinnacle'][name] = price
                    else:
                        if name not in ev_lines[line_key]['softs']: ev_lines[line_key]['softs'][name] = {}
                        ev_lines[line_key]['softs'][name][b_name] = price
                            
                    if line_key not in arb_lines: arb_lines[line_key] = {}
                    if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                        arb_lines[line_key][name] = {'price': price, 'bookie': b_name}
    return ev_lines, arb_lines

def evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport, history_map):
    found_evs, found_arbs = [], []
    
    for line_key, data in ev_lines.items():
        pinny, softs = data['pinnacle'], data.get('softs', {})
        
        true_probs = {}
        if len(pinny) == 2:
            s1, s2 = list(pinny.keys())
            t1, t2 = remove_vig(pinny[s1], pinny[s2])
            true_probs = {s1: 1/t1, s2: 1/t2}
        elif len(pinny) == 3:
            s1, s2, s3 = list(pinny.keys())
            t1, t2, t3 = remove_vig(pinny[s1], pinny[s2], pinny[s3])
            true_probs = {s1: 1/t1, s2: 1/t2, s3: 1/t3}

        for side, true_prob in true_probs.items():
            true_odds = 1 / true_prob
            if side in softs:
                best_bookie = None
                best_price = 0
                breakdown = {}
                
                for b_name, price in softs[side].items():
                    ev_for_book = ((price / true_odds) - 1) * 100
                    breakdown[b_name] = {'price': price, 'ev': ev_for_book}
                    if price > best_price:
                        best_price = price
                        best_bookie = b_name

                if best_price > true_odds:
                    ev_pct = ((best_price / true_odds) - 1) * 100
                    if ev_pct >= MIN_EV_THRESHOLD:
                        stake = calculate_kelly(best_price, true_odds, TOTAL_BANKROLL, best_bookie)
                        t_lay, g_profit = calculate_green_up(stake, best_price, true_odds)
                        
                        diff = abs(true_prob - (1.0/best_price))
                        confidence = max(0.0, 100.0 - (diff * 1000))
                        
                        clv = None
                        past_odds = history_map.get((match_name, line_key, side))
                        if past_odds:
                            clv = ((past_odds / true_odds) - 1) * 100

                        found_evs.append({
                            'pct': ev_pct, 'match': match_name, 'time': match_time, 'sport': sport,
                            'line': line_key, 'selection': side, 'odds': best_price,
                            'true': true_odds, 'bookie': best_bookie,
                            'stake': stake, 'target_lay': t_lay, 'green_profit': g_profit,
                            'breakdown': breakdown, 'confidence': confidence, 'clv': clv
                        })

    for line_key, outcomes in arb_lines.items():
        keys = list(outcomes.keys())
        if len(keys) == 2:
            k1, k2 = keys
            margin = (1 / outcomes[k1]['price']) + (1 / outcomes[k2]['price'])
            if margin < 1.0:
                arb_pct = (1 - margin) * 100
                if arb_pct >= MIN_ARB_THRESHOLD:
                    found_arbs.append({
                        'type': '2-way', 'pct': arb_pct, 'match': match_name, 'time': match_time, 'sport': sport, 'line': line_key,
                        's1': k1, 's1_data': outcomes[k1], 's2': k2, 's2_data': outcomes[k2],
                        'stk1': (TOTAL_BANKROLL / margin) / outcomes[k1]['price'],
                        'stk2': (TOTAL_BANKROLL / margin) / outcomes[k2]['price'],
                        'profit': (TOTAL_BANKROLL / margin) - TOTAL_BANKROLL
                    })
        elif len(keys) == 3:
            k1, k2, k3 = keys
            margin = (1 / outcomes[k1]['price']) + (1 / outcomes[k2]['price']) + (1 / outcomes[k3]['price'])
            if margin < 1.0:
                arb_pct = (1 - margin) * 100
                if arb_pct >= MIN_ARB_THRESHOLD:
                    found_arbs.append({
                        'type': '3-way', 'pct': arb_pct, 'match': match_name, 'time': match_time, 'sport': sport, 'line': line_key,
                        's1': k1, 's1_data': outcomes[k1], 's2': k2, 's2_data': outcomes[k2], 's3': k3, 's3_data': outcomes[k3],
                        'stk1': (TOTAL_BANKROLL / margin) / outcomes[k1]['price'],
                        'stk2': (TOTAL_BANKROLL / margin) / outcomes[k2]['price'],
                        'stk3': (TOTAL_BANKROLL / margin) / outcomes[k3]['price'],
                        'profit': (TOTAL_BANKROLL / margin) - TOTAL_BANKROLL
                    })
                    
    return found_evs, found_arbs

def fetch_odds_with_retry(url, params):
    global requests_remaining, requests_used_total, scan_starting_used
    while True:
        with api_lock:
            if not API_KEYS: return None
            active_key = get_active_api_key()
            
        params['apiKey'] = active_key
        try:
            res = requests.get(url, params=params, timeout=15)
        except:
            return None
        
        with api_lock:
            if 'x-requests-remaining' in res.headers: requests_remaining = res.headers['x-requests-remaining']
            if 'x-requests-used' in res.headers:
                requests_used_total = res.headers['x-requests-used']
                if scan_starting_used is None: scan_starting_used = int(requests_used_total) - 2

        if res.status_code == 401:
            with api_lock:
                if rotate_api_key(): continue
                else: return None
        elif res.status_code == 429:
            if 'quota' in res.json().get('message', '').lower():
                with api_lock:
                    if rotate_api_key(): continue
                    else: return None
            else: time.sleep(2); continue
        elif res.status_code == 200: return res.json()
        else: return None

# ==========================================
#  HTML GENERATION (JAVASCRIPT DRIVEN)
# ==========================================
def generate_web_dashboard(evs, arbs, current_time, bankroll_state):
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    
    credits_burned = int(requests_used_total) - scan_starting_used if scan_starting_used is not None and str(requests_used_total).isdigit() else "Unknown"
    
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
        <meta http-equiv="Pragma" content="no-cache">
        <meta http-equiv="Expires" content="0">
        <title>Arb Sniper Live Dashboard</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0d1117; color: #c9d1d9; margin: 0; padding: 15px; max-width: 800px; margin: auto; }
            h1 { color: #58a6ff; text-align: center; font-size: 26px; margin-bottom: 5px; }
            .time { text-align: center; color: #8b949e; font-size: 14px; margin-bottom: 20px; font-weight: bold; }
            
            .btn-run { background-color: #238636; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 6px; cursor: pointer; font-weight: bold; width: 100%; box-shadow: 0 4px 6px rgba(0,0,0,0.3); margin-bottom: 20px; }
            .btn-run:active { background-color: #2ea043; }
            
            .controls-panel { background: #161b22; padding: 15px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #30363d; display: flex; flex-wrap: wrap; gap: 15px; align-items: center; justify-content: space-between;}
            .slider-container { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 250px; }
            .btn-secondary { background: #21262d; border: 1px solid #363b42; color: #c9d1d9; padding: 8px 12px; border-radius: 5px; cursor: pointer; font-weight: bold; }
            .btn-secondary:hover { background: #30363d; }
            
            .tabs { display: flex; border-bottom: 1px solid #30363d; margin-bottom: 20px; }
            .tab { flex: 1; text-align: center; padding: 12px; cursor: pointer; font-size: 16px; font-weight: bold; color: #8b949e; }
            .tab.active { color: #ffffff; border-bottom: 3px solid #58a6ff; background-color: #161b22; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            
            .card { background-color: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 20px; font-size: 15px; line-height: 1.6; }
            .card-header { font-weight: bold; font-size: 16px; margin-bottom: 12px; border-bottom: 1px dashed #30363d; padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
            .detail-block { margin-bottom: 12px; }
            .highlight { color: #ffffff; font-weight: bold; }
            .highlight-stake { color: #e3b341; font-weight: bold; }
            .profit-highlight { color: #e3b341; font-weight: bold; font-size: 16px; }
            
            .badge { padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
            .conf-high { background: #238636; color: white;}
            .conf-med { background: #d29922; color: white;}
            .conf-low { background: #da3633; color: white;}
            
            table.breakdown { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
            table.breakdown th, table.breakdown td { border: 1px solid #30363d; padding: 5px; text-align: left; color:#c9d1d9; }
            table.breakdown th { background: #21262d; color: #8b949e; }
            
            .empty-state { text-align: center; color: #8b949e; padding: 30px; font-style: italic; background-color: #161b22; border-radius: 8px; border: 1px dashed #30363d; }
            .telemetry { text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid #30363d; font-size: 12px; color: #484f58; line-height: 1.6; }
            
            details summary { cursor: pointer; color: #58a6ff; margin-top: 8px; font-size: 13px; font-weight: bold; }
            details summary:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h1> Arb Sniper Terminal</h1>
        <div class="time">Last Sweep: __TIME__</div>
        
        <button class="btn-run" onclick="triggerScan()"> Launch Cloud Scan Now</button>
        
        <div class="controls-panel">
            <div class="slider-container">
                <label for="kelly-slider">Kelly Fraction: <span id="kelly-val" class="highlight">30</span>%</label>
                <input type="range" id="kelly-slider" min="0" max="100" value="30" oninput="document.getElementById('kelly-val').innerText = this.value; renderData();" style="flex:1;">
            </div>
            <div>
                <input type="checkbox" id="top5-toggle" onchange="renderData()"> <label for="top5-toggle">Top 5 Only</label>
            </div>
            <button class="btn-secondary" onclick="exportCSV()">Export CSV</button>
        </div>
        
        <div class="tabs">
            <div class="tab active" id="tab-ev" onclick="switchTab('ev')"> EV Edges (<span id="ev-count">0</span>)</div>
            <div class="tab" id="tab-arb" onclick="switchTab('arb')"> Arbitrage (<span id="arb-count">0</span>)</div>
            <div class="tab" id="tab-analytics" onclick="switchTab('analytics')"> Analytics </div>
        </div>
        
        <div id="content-ev" class="tab-content active"></div>
        <div id="content-arb" class="tab-content"></div>
        <div id="content-analytics" class="tab-content"></div>
        
        <div class="telemetry">
            <strong>SYSTEM TELEMETRY</strong><br>
            __TELEMETRY_DATA__
        </div>

        <script>
            const evData = __EV_DATA__;
            const arbData = __ARB_DATA__;
            const bookCaps = __BOOK_CAPS__;
            const brState = __BR_STATE__;
            const totalBankroll = __BANKROLL__;

            function displayBookie(api_key) {
                const mapping = {'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet', 'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'};
                return mapping[api_key] || (api_key.charAt(0).toUpperCase() + api_key.slice(1));
            }

            function switchTab(tab) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
                document.getElementById('content-' + tab).classList.add('active');
                document.getElementById('tab-' + tab).classList.add('active');
            }

            function renderData() {
                let isTop5 = document.getElementById('top5-toggle').checked;
                let sliderVal = parseInt(document.getElementById('kelly-slider').value) / 100;
                
                let evLimit = isTop5 ? 5 : evData.length;
                let arbLimit = isTop5 ? 5 : arbData.length;
                
                document.getElementById('ev-count').innerText = evData.length;
                document.getElementById('arb-count').innerText = arbData.length;

                // --- EV RENDER ---
                let evHtml = '';
                for(let i=0; i<Math.min(evLimit, evData.length); i++) {
                    let ev = evData[i];
                    let true_prob = 1 / ev.true;
                    let b = ev.odds - 1.0;
                    
                    let raw_k = ((b * true_prob - (1-true_prob)) / b) * sliderVal;
                    let max_k = (0.05 / 0.30) * sliderVal; // Proportional upper limit check
                    if (raw_k > max_k) raw_k = max_k;
                    
                    let stake = Math.max(20, totalBankroll * raw_k);
                    if (raw_k <= 0) stake = 0;
                    let cap = bookCaps[ev.bookie] || 1000;
                    stake = Math.min(stake, cap);

                    let confClass = ev.confidence >= 75 ? 'conf-high' : (ev.confidence >= 50 ? 'conf-med' : 'conf-low');

                    let bdHtml = '<table class="breakdown"><tr><th>Bookmaker</th><th>Odds</th><th>EV%</th></tr>';
                    let sortedBreakdown = Object.entries(ev.breakdown).sort((a,b) => b[1].ev - a[1].ev);
                    for(let [bk, bd] of sortedBreakdown) {
                        let hl = bk === ev.bookie ? 'style="background:#2ea04333;"' : '';
                        bdHtml += `<tr ${hl}><td>${displayBookie(bk)}</td><td>${bd.price.toFixed(2)}</td><td>${bd.ev.toFixed(2)}%</td></tr>`;
                    }
                    bdHtml += '</table>';

                    let clvStr = ev.clv !== null ? `<br>Historical CLV: <span class="highlight">${ev.clv.toFixed(2)}%</span>` : '';

                    evHtml += `
                    <div class="card">
                        <div class="card-header"> 
                            <span><span class="highlight">${ev.pct.toFixed(2)}% EV</span> | ${ev.match}</span>
                            <span class="badge ${confClass}">Conf: ${ev.confidence.toFixed(0)}</span>
                        </div>
                        <div class="detail-block">
                            ${ev.sport.replace('_', ' ').toUpperCase()}<br>
                            ${ev.time}<br>
                            <span class="highlight">${ev.line}</span>${clvStr}
                        </div>
                        <div class="detail-block">
                            BET EXACTLY: <span class="highlight-stake">₹${stake.toFixed(0)}</span> (Cap: ₹${cap})<br>
                            <span class="highlight">${ev.selection.toUpperCase()} ${(ev.line.split('_')[1] || '').replace('0', '')} @ ${ev.odds.toFixed(2)}</span> on ${displayBookie(ev.bookie)}
                        </div>
                        <div>
                            True Odds: ${ev.true.toFixed(2)}
                            <details><summary>View Odds Breakdown</summary>${bdHtml}</details>
                        </div>
                    </div>`;
                }
                document.getElementById('content-ev').innerHTML = evHtml || '<div class="empty-state"> No massive EV edges found right now.</div>';

                // --- ARB RENDER ---
                let arbHtml = '';
                for(let i=0; i<Math.min(arbLimit, arbData.length); i++) {
                    let arb = arbData[i];
                    let is3way = arb.type === '3-way';
                    
                    let stks = `
                        <span class="highlight-stake">₹${arb.stk1.toFixed(0)}</span> on <span class="highlight">${arb.s1.toUpperCase()} @ ${arb.s1_data.price.toFixed(2)}</span> [${displayBookie(arb.s1_data.bookie)}]<br>
                        <span class="highlight-stake">₹${arb.stk2.toFixed(0)}</span> on <span class="highlight">${arb.s2.toUpperCase()} @ ${arb.s2_data.price.toFixed(2)}</span> [${displayBookie(arb.s2_data.bookie)}]
                    `;
                    if(is3way) {
                        stks += `<br><span class="highlight-stake">₹${arb.stk3.toFixed(0)}</span> on <span class="highlight">${arb.s3.toUpperCase()} @ ${arb.s3_data.price.toFixed(2)}</span> [${displayBookie(arb.s3_data.bookie)}]`;
                    }
                    
                    arbHtml += `
                    <div class="card">
                        <div class="card-header"> 
                            <span><span class="highlight">${arb.pct.toFixed(2)}% ARB</span> | ${arb.match}</span>
                            <span class="badge" style="background:#484f58;">${is3way ? '3-Way' : '2-Way'}</span>
                        </div>
                        <div class="detail-block">
                            ${arb.sport.replace('_', ' ').toUpperCase()}<br>
                            ${arb.time}<br>
                            <span class="highlight">${arb.line}</span>
                        </div>
                        <div class="detail-block">${stks}</div>
                        <div>Profit: <span class="profit-highlight">₹${arb.profit.toFixed(0)}</span></div>
                    </div>`;
                }
                document.getElementById('content-arb').innerHTML = arbHtml || '<div class="empty-state"> No Arbitrage opportunities found right now.</div>';

                // --- ANALYTICS RENDER ---
                let buckets = {'0-2%':0, '2-5%':0, '5-10%':0, '10%+':0};
                let sumEv = 0, maxEv = 0;
                evData.forEach(ev => {
                    sumEv += ev.pct;
                    if(ev.pct > maxEv) maxEv = ev.pct;
                    if(ev.pct < 2) buckets['0-2%']++;
                    else if(ev.pct < 5) buckets['2-5%']++;
                    else if(ev.pct < 10) buckets['5-10%']++;
                    else buckets['10%+']++;
                });
                let avgEv = evData.length ? (sumEv / evData.length).toFixed(2) : 0;

                let anaHtml = `
                <div class="card">
                    <div class="card-header">Daily Bankroll Tracking</div>
                    <table class="breakdown" style="border:none;">
                        <tr><td style="border:none;">Tracking Date:</td><td style="border:none;"><span class="highlight">${brState.date || 'N/A'}</span></td></tr>
                        <tr><td style="border:none;">Starting Bankroll:</td><td style="border:none;"><span class="highlight">₹${brState.starting_bankroll || 0}</span></td></tr>
                        <tr><td style="border:none;">Total Stakes Rec:</td><td style="border:none;"><span class="highlight-stake">₹${(brState.total_stakes || 0).toFixed(0)}</span></td></tr>
                        <tr><td style="border:none;">EV Exposure:</td><td style="border:none;"><span class="highlight">₹${(brState.ev_exposure || 0).toFixed(0)}</span></td></tr>
                        <tr><td style="border:none;">Theoretical Arb Profit:</td><td style="border:none;"><span class="profit-highlight">₹${(brState.theoretical_profit || 0).toFixed(0)}</span></td></tr>
                    </table>
                </div>
                <div class="card">
                    <div class="card-header">Current Scan EV Distribution</div>
                    <div style="display:flex; justify-content:space-between; margin-bottom: 10px;">
                        <div>Average EV: <span class="highlight">${avgEv}%</span></div>
                        <div>Highest EV: <span class="highlight">${maxEv.toFixed(2)}%</span></div>
                    </div>
                    <table class="breakdown">
                        <tr><th>Bucket Bracket</th><th>Edge Count</th></tr>
                        <tr><td>0.0% - 2.0%</td><td>${buckets['0-2%']}</td></tr>
                        <tr><td>2.0% - 5.0%</td><td>${buckets['2-5%']}</td></tr>
                        <tr><td>5.0% - 10.0%</td><td>${buckets['5-10%']}</td></tr>
                        <tr><td>10.0%+</td><td>${buckets['10%+']}</td></tr>
                    </table>
                </div>`;
                document.getElementById('content-analytics').innerHTML = anaHtml;
            }

            function exportCSV() {
                let csv = "Type,Match,Sport,Time,Line,Selection,Odds,Bookmaker,EV/ARB %,Stake,Profit\n";
                evData.forEach(ev => {
                    csv += `EV,"${ev.match}","${ev.sport}","${ev.time}","${ev.line}","${ev.selection}",${ev.odds},"${ev.bookie}",${ev.pct},${ev.stake},${ev.green_profit}\n`;
                });
                arbData.forEach(arb => {
                    let totalStk = arb.stk1 + arb.stk2 + (arb.stk3 || 0);
                    csv += `ARB,"${arb.match}","${arb.sport}","${arb.time}","${arb.line}","Multiple",0,"Multiple",${arb.pct},${totalStk},${arb.profit}\n`;
                });
                let encodedUri = encodeURI("data:text/csv;charset=utf-8," + csv);
                let link = document.createElement("a");
                link.setAttribute("href", encodedUri);
                link.setAttribute("download", "arb_sniper_scan.csv");
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }

            function triggerScan() {
                let pat = localStorage.getItem('gh_dispatch_token');
                if (!pat) {
                    pat = prompt("Enter your GitHub PAT (ghp_...) to authorize this scan:\\n(This is safely stored only in your local browser, never public)");
                    if (!pat) return;
                    localStorage.setItem('gh_dispatch_token', pat);
                }
                
                // Assuming nikunj7711 based on original script, user can modify if needed
                fetch('https://api.github.com/repos/nikunj7711/arb-sniper/actions/workflows/sniper.yml/dispatches', {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/vnd.github.v3+json',
                        'Authorization': 'token ' + pat,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ ref: 'main' })
                })
                .then(response => {
                    if(response.ok) {
                        alert(" Engine Fired! Scan is running. Wait 2-3 minutes, then Hard Refresh this page.");
                    } else {
                        alert(" Authorization failed! Your token might be wrong or expired. Resetting token...");
                        localStorage.removeItem('gh_dispatch_token');
                    }
                })
                .catch(error => console.error('Error:', error));
            }

            window.onload = function() { renderData(); };
        </script>
    </body>
    </html>
    """
    
    html = html_template.replace("__TIME__", current_time)
    html = html.replace("__EV_DATA__", json.dumps(evs))
    html = html.replace("__ARB_DATA__", json.dumps(arbs))
    html = html.replace("__BOOK_CAPS__", json.dumps(BOOK_CAPS))
    html = html.replace("__BR_STATE__", json.dumps(bankroll_state))
    html = html.replace("__BANKROLL__", str(TOTAL_BANKROLL))
    
    telemetry_str = f"Active Key: #{current_key_index + 1} | Monthly Quota: {requests_remaining}/500 | Scan Cost: ~{credits_burned} credits"
    html = html.replace("__TELEMETRY_DATA__", telemetry_str)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(" Web Dashboard successfully updated (index.html)")

# ==========================================
#  MAIN EXECUTION
# ==========================================
def run_hybrid_scanner():
    global scan_starting_used
    my_bookies_list = MY_BOOKIES.split(',')
    scan_starting_used = None
    
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_str = ist_now.strftime('%d %b %Y, %I:%M:%S %p IST')
    
    print(f"\n [{current_time_str}] ALL-SPORTS Sweep (EV + ARB) active...")
    all_evs, all_arbs = [], []
    
    history_log = load_json('history_log.json', [])
    history_map = {(x['match'], x['line'], x['selection']): x['odds'] for x in history_log[-5000:]}

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_sport = {}
        for sport in TARGET_SPORTS:
            url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds'
            params = {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'totals,spreads,h2h', 'oddsFormat': 'decimal'}
            future = executor.submit(fetch_odds_with_retry, url, params.copy())
            future_to_sport[future] = sport

        for future in as_completed(future_to_sport):
            sport = future_to_sport[future]
            events = future.result()
            if not events: continue
                
            for event in events:
                match_name = f"{event['home_team']} vs {event['away_team']}"
                match_time = format_time_ist(event['commence_time'])
                ev_lines, arb_lines = extract_hybrid_data(event.get('bookmakers', []), my_bookies_list)
                new_evs, new_arbs = evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport, history_map)
                all_evs.extend(new_evs)
                all_arbs.extend(new_arbs)
            
    if all_arbs:
        all_arbs.sort(key=lambda x: x['pct'], reverse=True)
        for arb in all_arbs:
            stk_str = f" ₹{arb['stk1']:.0f} on {arb['s1'].upper()} @ {arb['s1_data']['price']:.2f} [{display_bookie(arb['s1_data']['bookie'])}]\n ₹{arb['stk2']:.0f} on {arb['s2'].upper()} @ {arb['s2_data']['price']:.2f} [{display_bookie(arb['s2_data']['bookie'])}]"
            if arb.get('type') == '3-way':
                stk_str += f"\n ₹{arb['stk3']:.0f} on {arb['s3'].upper()} @ {arb['s3_data']['price']:.2f} [{display_bookie(arb['s3_data']['bookie'])}]"
            
            msg = f"   {arb['pct']:.2f}% ARB | {arb['match']}\n {arb['sport'].replace('_', ' ').title()}\n {arb['time']}\n {arb['line']}\n\n{stk_str}\n\n Profit: ₹{arb['profit']:.0f}"
            
            if not check_alert_cache(arb['match'], arb['line'], "ARB", arb['pct']):
                send_phone_alert(msg, arb['pct'], arb['match'], "ARB")
                
                history_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "match": arb['match'], "sport": arb['sport'], "line": arb['line'],
                    "selection": "ARB", "odds": arb['pct'], "true_odds": 0, "bookie": "Multiple", "type": arb.get('type'),
                    "stake_recommendation": sum([arb.get('stk1',0), arb.get('stk2',0), arb.get('stk3',0)])
                })

    if all_evs:
        all_evs.sort(key=lambda x: x['pct'], reverse=True)
        for ev in all_evs:
            msg = f"   {ev['pct']:.2f}% EV | {ev['match']}\n {ev['sport'].replace('_', ' ').title()}\n {ev['time']}\n {ev['line']}\n\n BET EXACTLY: ₹{ev['stake']:.0f}\n {ev['selection'].upper()} {(ev['line'].split('_')[1] if '_' in ev['line'] else '')} @ {ev['odds']:.2f} on {display_bookie(ev['bookie'])}\n\n True Odds: {ev['true']:.2f}"
            
            if not check_alert_cache(ev['match'], ev['line'], ev['selection'], ev['odds']):
                send_phone_alert(msg, ev['pct'], ev['match'], "EV")
                
                history_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "match": ev['match'], "sport": ev['sport'], "line": ev['line'],
                    "selection": ev['selection'], "odds": ev['odds'], "true_odds": ev['true'], "bookie": ev['bookie'],
                    "stake_recommendation": ev['stake'], "clv": ev.get('clv')
                })

    save_json('history_log.json', history_log[-10000:])
    
    bankroll_state = update_bankroll_state(all_evs, all_arbs)
    generate_web_dashboard(all_evs, all_arbs, current_time_str, bankroll_state)

    print("\n" + "=" * 65)
    print(" API USAGE REPORT")
    print("=" * 65)
    print(f" Active Key Index: #{current_key_index + 1}")
    print(f" Remaining Monthly Credits: {requests_remaining} / 500")
    print("=" * 65)

if __name__ == "__main__":
    print(" GitHub Actions Master Cloud Engine Started...")
    run_hybrid_scanner()
    print(" Scan complete.")
