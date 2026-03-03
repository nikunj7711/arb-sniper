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

# ==========================================
#  FEATURE 12: PER-BOOK BANKROLL CAPS
# ==========================================
BOOK_CAPS = {
    'betway': 300,
    'stake': 500,
    'onexbet': 400,
    'marathonbet': 400,
    'dafabet': 350,
    'betfair_ex_eu': 600,
    'pinnacle': 1000,
}

current_key_index = 0
requests_remaining = "Unknown"
requests_used_total = "Unknown"
scan_starting_used = None

# ==========================================
#  FEATURE 5: ALERT CACHE (DUPLICATE SUPPRESSION)
# ==========================================
ALERT_CACHE_FILE = 'alert_cache.json'
ALERT_CACHE_EXPIRY_HOURS = 6

def load_alert_cache():
    try:
        with open(ALERT_CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_alert_cache(cache):
    try:
        with open(ALERT_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except:
        pass

def is_duplicate_alert(cache, match, line, selection, odds):
    key = f"{match}|{line}|{selection}|{odds:.2f}"
    if key in cache:
        ts = cache[key]
        try:
            cached_time = datetime.fromisoformat(ts)
            if datetime.now(timezone.utc) - cached_time < timedelta(hours=ALERT_CACHE_EXPIRY_HOURS):
                return True
        except:
            pass
    return False

def mark_alert_sent(cache, match, line, selection, odds):
    key = f"{match}|{line}|{selection}|{odds:.2f}"
    cache[key] = datetime.now(timezone.utc).isoformat()

def prune_alert_cache(cache):
    now = datetime.now(timezone.utc)
    return {
        k: v for k, v in cache.items()
        if (now - datetime.fromisoformat(v)) < timedelta(hours=ALERT_CACHE_EXPIRY_HOURS)
    }

# ==========================================
#  FEATURE 1: SMART DAILY BANKROLL TRACKING
# ==========================================
BANKROLL_STATE_FILE = 'bankroll_state.json'

def load_bankroll_state():
    try:
        with open(BANKROLL_STATE_FILE, 'r') as f:
            state = json.load(f)
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d')
        if state.get('date') != ist_today:
            state = {'date': ist_today, 'starting_bankroll': TOTAL_BANKROLL, 'total_stakes': 0.0, 'theoretical_arb_profit': 0.0, 'theoretical_ev_exposure': 0.0}
            save_bankroll_state(state)
        return state
    except:
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d')
        state = {'date': ist_today, 'starting_bankroll': TOTAL_BANKROLL, 'total_stakes': 0.0, 'theoretical_arb_profit': 0.0, 'theoretical_ev_exposure': 0.0}
        save_bankroll_state(state)
        return state

def save_bankroll_state(state):
    try:
        with open(BANKROLL_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except:
        pass

def update_bankroll_state(all_evs, all_arbs):
    state = load_bankroll_state()
    for ev in all_evs:
        state['total_stakes'] += ev.get('stake', 0)
        state['theoretical_ev_exposure'] += ev.get('stake', 0) * (ev['pct'] / 100)
    for arb in all_arbs:
        state['total_stakes'] += arb.get('stk1', 0) + arb.get('stk2', 0)
        state['theoretical_arb_profit'] += arb.get('profit', 0)
    save_bankroll_state(state)
    return state

# ==========================================
#  FEATURE 2: HISTORICAL EDGE LOGGING
# ==========================================
HISTORY_LOG_FILE = 'history_log.json'

def load_history_log():
    try:
        with open(HISTORY_LOG_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_history_log(log):
    try:
        with open(HISTORY_LOG_FILE, 'w') as f:
            json.dump(log, f, indent=2)
    except:
        pass

def append_to_history(evs, arbs):
    log = load_history_log()
    ist_now_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d %H:%M:%S IST')
    scan_keys = set()

    for entry in log:
        scan_keys.add(f"{entry.get('match')}|{entry.get('line')}|{entry.get('selection','')}|{entry.get('odds',0):.2f}|{entry.get('type')}")

    new_entries = []
    for ev in evs:
        key = f"{ev['match']}|{ev['line']}|{ev['selection']}|{ev['odds']:.2f}|EV"
        if key not in scan_keys:
            entry = {
                'type': 'EV',
                'timestamp': ist_now_str,
                'match': ev['match'],
                'sport': ev['sport'],
                'line': ev['line'],
                'bookmaker': ev['bookie'],
                'odds': round(ev['odds'], 3),
                'true_odds': round(ev['true'], 3),
                'selection': ev['selection'],
                'ev_pct': round(ev['pct'], 3),
                'stake': round(ev['stake'], 2),
                'clv_pct': None,
            }
            new_entries.append(entry)
            scan_keys.add(key)

    for arb in arbs:
        key = f"{arb['match']}|{arb['line']}|{arb['s1']}|{arb['s1_data']['price']:.2f}|ARB"
        if key not in scan_keys:
            entry = {
                'type': 'ARB',
                'timestamp': ist_now_str,
                'match': arb['match'],
                'sport': arb['sport'],
                'line': arb['line'],
                'bookmaker': f"{arb['s1_data']['bookie']}+{arb['s2_data']['bookie']}",
                'odds': round(arb['s1_data']['price'], 3),
                'selection': arb['s1'],
                'arb_pct': round(arb['pct'], 3),
                'stake': round(arb['stk1'] + arb['stk2'], 2),
                'clv_pct': None,
            }
            new_entries.append(entry)
            scan_keys.add(key)

    log.extend(new_entries)
    save_history_log(log)
    return log

# ==========================================
#  FEATURE 6: CLV TRACKING MODEL
# ==========================================
def compute_clv(all_evs, history_log):
    prev_by_key = {}
    for entry in history_log:
        if entry.get('type') == 'EV' and entry.get('true_odds'):
            k = f"{entry['match']}|{entry['line']}|{entry.get('selection','')}"
            prev_by_key[k] = entry

    updated = False
    for ev in all_evs:
        k = f"{ev['match']}|{ev['line']}|{ev['selection']}"
        if k in prev_by_key:
            prev = prev_by_key[k]
            prev_true = prev.get('true_odds')
            if prev_true and prev_true > 1.0:
                curr_true = ev['true']
                clv_pct = ((curr_true / prev_true) - 1) * 100
                ev['clv_pct'] = round(clv_pct, 3)
                for entry in history_log:
                    if (entry.get('match') == prev['match'] and
                            entry.get('line') == prev['line'] and
                            entry.get('selection', '') == prev.get('selection', '') and
                            entry.get('clv_pct') is None):
                        entry['clv_pct'] = round(clv_pct, 3)
                        updated = True
        else:
            ev['clv_pct'] = None

    if updated:
        save_history_log(history_log)
    return all_evs

# ==========================================
#  CORE UTILITIES
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
        requests.post("https://ntfy.sh/", json=payload)
    except:
        pass

def format_time_ist(iso_string):
    try:
        dt_utc = datetime.strptime(iso_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    except:
        return "Unknown Time"

def display_bookie(api_key):
    mapping = {
        'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet',
        'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'
    }
    return mapping.get(api_key, api_key.title())

def remove_vig(odds1, odds2):
    imp1, imp2 = 1 / odds1, 1 / odds2
    margin = imp1 + imp2
    return (1 / (imp1 / margin)), (1 / (imp2 / margin))

def calculate_kelly(soft_odds, true_odds, bankroll, kelly_fraction=0.30, bookie=None):
    b = soft_odds - 1.0
    p = 1.0 / true_odds
    q = 1.0 - p
    safe_kelly = ((b * p - q) / b) * kelly_fraction
    if safe_kelly <= 0:
        return 0
    if safe_kelly > 0.05:
        safe_kelly = 0.05
    stake = max(20, bankroll * safe_kelly)
    if bookie and bookie in BOOK_CAPS:
        stake = min(stake, BOOK_CAPS[bookie])
    return stake

def calculate_green_up(back_stake, back_odds, lay_odds):
    target_lay_stake = (back_stake * back_odds) / lay_odds
    guaranteed_profit = target_lay_stake - back_stake
    return target_lay_stake, guaranteed_profit

# ==========================================
#  FEATURE 11: CONFIDENCE SCORE
# ==========================================
def compute_confidence(soft_odds, true_odds):
    soft_imp = 1 / soft_odds
    true_imp = 1 / true_odds
    diff = abs(soft_imp - true_imp)
    score = max(0, min(100, int((diff / true_imp) * 500)))
    return score

# ==========================================
#  DATA EXTRACTION
# ==========================================
def extract_hybrid_data(bookmakers_list, target_bookies):
    ev_lines = {}
    arb_lines = {}
    for bookie in bookmakers_list:
        b_name = bookie['key']
        if b_name not in target_bookies:
            continue
        for market in bookie.get('markets', []):
            if market['key'] in ['totals', 'spreads']:
                m_type = market['key'].upper()
                for outcome in market['outcomes']:
                    point = str(outcome.get('point', '0'))
                    name = outcome['name']
                    price = outcome['price']
                    if b_name == 'betfair_ex_eu':
                        price = 1 + (price - 1) * 0.97
                    line_key = f"{m_type}_{point}"

                    if line_key not in ev_lines:
                        ev_lines[line_key] = {'pinnacle': {}, 'best_soft': {}, 'all_soft': {}}
                    if b_name == 'pinnacle':
                        ev_lines[line_key]['pinnacle'][name] = price
                    elif b_name != 'pinnacle':
                        if name not in ev_lines[line_key]['best_soft'] or price > ev_lines[line_key]['best_soft'][name]['price']:
                            ev_lines[line_key]['best_soft'][name] = {'price': price, 'bookie': b_name}
                        if name not in ev_lines[line_key]['all_soft']:
                            ev_lines[line_key]['all_soft'][name] = {}
                        ev_lines[line_key]['all_soft'][name][b_name] = price

                    if line_key not in arb_lines:
                        arb_lines[line_key] = {}
                    if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                        arb_lines[line_key][name] = {'price': price, 'bookie': b_name}
    return ev_lines, arb_lines

# ==========================================
#  FEATURE 8: BOOKMAKER-SPECIFIC EV BREAKDOWN
# ==========================================
def build_ev_breakdown(line_data, side, true_odds):
    pinny_odds = line_data['pinnacle'].get(side, None)
    rows = []
    if pinny_odds:
        rows.append({'bookie': 'pinnacle', 'odds': round(pinny_odds, 3), 'ev_pct': 0.0, 'is_best': False})
    all_soft = line_data.get('all_soft', {}).get(side, {})
    best_ev = -999
    best_bookie = None
    for bk, odds in all_soft.items():
        ev_pct = ((odds / true_odds) - 1) * 100
        rows.append({'bookie': bk, 'odds': round(odds, 3), 'ev_pct': round(ev_pct, 2), 'is_best': False})
        if ev_pct > best_ev:
            best_ev = ev_pct
            best_bookie = bk
    for row in rows:
        if row['bookie'] == best_bookie:
            row['is_best'] = True
    return rows

# ==========================================
#  MARKET EVALUATION
# ==========================================
def evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport):
    found_evs, found_arbs = [], []

    for line_key, data in ev_lines.items():
        pinny, softs = data['pinnacle'], data['best_soft']
        if len(pinny) == 2:
            s1, s2 = list(pinny.keys())[0], list(pinny.keys())[1]
            t_odds1, t_odds2 = remove_vig(pinny[s1], pinny[s2])
            for side, true_odds in [(s1, t_odds1), (s2, t_odds2)]:
                if side in softs and softs[side]['price'] > true_odds:
                    ev_pct = ((softs[side]['price'] / true_odds) - 1) * 100
                    if ev_pct >= MIN_EV_THRESHOLD:
                        stake = calculate_kelly(softs[side]['price'], true_odds, TOTAL_BANKROLL, bookie=softs[side]['bookie'])
                        t_lay, g_profit = calculate_green_up(stake, softs[side]['price'], true_odds)
                        confidence = compute_confidence(softs[side]['price'], true_odds)
                        ev_breakdown = build_ev_breakdown(data, side, true_odds)
                        found_evs.append({
                            'pct': ev_pct,
                            'match': match_name,
                            'time': match_time,
                            'sport': sport,
                            'line': line_key,
                            'selection': side,
                            'odds': softs[side]['price'],
                            'true': true_odds,
                            'bookie': softs[side]['bookie'],
                            'stake': stake,
                            'target_lay': t_lay,
                            'green_profit': g_profit,
                            'confidence': confidence,
                            'ev_breakdown': ev_breakdown,
                            'clv_pct': None,
                        })

    for line_key, outcomes in arb_lines.items():
        keys = list(outcomes.keys())

        if len(keys) == 2:
            k1, k2 = keys[0], keys[1]
            margin = (1 / outcomes[k1]['price']) + (1 / outcomes[k2]['price'])
            if margin < 1.0:
                arb_pct = (1 - margin) * 100
                if arb_pct >= MIN_ARB_THRESHOLD:
                    found_arbs.append({
                        'pct': arb_pct,
                        'match': match_name,
                        'time': match_time,
                        'sport': sport,
                        'line': line_key,
                        'ways': 2,
                        's1': k1, 's1_data': outcomes[k1],
                        's2': k2, 's2_data': outcomes[k2],
                        'stk1': (TOTAL_BANKROLL / margin) / outcomes[k1]['price'],
                        'stk2': (TOTAL_BANKROLL / margin) / outcomes[k2]['price'],
                        'profit': (TOTAL_BANKROLL / margin) - TOTAL_BANKROLL
                    })

        elif len(keys) == 3:
            k1, k2, k3 = keys[0], keys[1], keys[2]
            margin = (1 / outcomes[k1]['price']) + (1 / outcomes[k2]['price']) + (1 / outcomes[k3]['price'])
            if margin < 1.0:
                arb_pct = (1 - margin) * 100
                if arb_pct >= MIN_ARB_THRESHOLD:
                    found_arbs.append({
                        'pct': arb_pct,
                        'match': match_name,
                        'time': match_time,
                        'sport': sport,
                        'line': line_key,
                        'ways': 3,
                        's1': k1, 's1_data': outcomes[k1],
                        's2': k2, 's2_data': outcomes[k2],
                        's3': k3, 's3_data': outcomes[k3],
                        'stk1': (TOTAL_BANKROLL / margin) / outcomes[k1]['price'],
                        'stk2': (TOTAL_BANKROLL / margin) / outcomes[k2]['price'],
                        'stk3': (TOTAL_BANKROLL / margin) / outcomes[k3]['price'],
                        'profit': (TOTAL_BANKROLL / margin) - TOTAL_BANKROLL
                    })

    return found_evs, found_arbs

# ==========================================
#  API FETCHING
# ==========================================
def fetch_odds_with_retry(url, params):
    global requests_remaining, requests_used_total, scan_starting_used
    while True:
        if not API_KEYS:
            return None
        params['apiKey'] = get_active_api_key()
        res = requests.get(url, params=params)

        if 'x-requests-remaining' in res.headers:
            requests_remaining = res.headers['x-requests-remaining']
        if 'x-requests-used' in res.headers:
            requests_used_total = res.headers['x-requests-used']
            if scan_starting_used is None:
                scan_starting_used = int(requests_used_total) - 2

        if res.status_code == 401:
            if rotate_api_key():
                continue
            else:
                return None
        elif res.status_code == 429:
            if 'quota' in res.json().get('message', '').lower():
                if rotate_api_key():
                    continue
                else:
                    return None
            else:
                time.sleep(2)
                continue
        elif res.status_code == 200:
            return res.json()
        else:
            return None

# ==========================================
#  FEATURE 7: PARALLEL SPORT FETCHING
# ==========================================
def fetch_sport_events(sport):
    url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds'
    params = {
        'regions': 'eu',
        'bookmakers': MY_BOOKIES,
        'markets': 'totals,spreads',
        'oddsFormat': 'decimal'
    }
    events = fetch_odds_with_retry(url, params)
    time.sleep(1.5)
    return sport, events

def fetch_all_sports_parallel(sports):
    results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_sport_events, sport): sport for sport in sports}
        for future in as_completed(futures):
            sport, events = future.result()
            results[sport] = events
    return results

# ==========================================
#  FEATURE 3: EV DISTRIBUTION ANALYTICS
# ==========================================
def compute_ev_analytics(all_evs):
    buckets = {'0-2%': 0, '2-5%': 0, '5-10%': 0, '10%+': 0}
    for ev in all_evs:
        p = ev['pct']
        if p < 2:
            buckets['0-2%'] += 1
        elif p < 5:
            buckets['2-5%'] += 1
        elif p < 10:
            buckets['5-10%'] += 1
        else:
            buckets['10%+'] += 1
    avg_ev = (sum(e['pct'] for e in all_evs) / len(all_evs)) if all_evs else 0
    max_ev = max((e['pct'] for e in all_evs), default=0)
    return buckets, round(avg_ev, 3), round(max_ev, 3)

# ==========================================
#  DASHBOARD GENERATION WITH NEW UI/UX
# ==========================================
def generate_web_dashboard(evs, arbs, current_time, bankroll_state=None):
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)

    buckets, avg_ev, max_ev = compute_ev_analytics(evs)

    # Initial HTML construction for the bankroll/analytics
    bankroll_html = ''
    if bankroll_state:
        bankroll_html = f"""
        <div class="card glass-effect fade-in-up" style="margin-bottom:20px;font-size:14px; animation-delay: 0.1s;">
            <div class="card-header"> 📊 Daily Bankroll Tracker ({bankroll_state.get('date','')})</div>
            Starting Bankroll: <span class="highlight">₹{bankroll_state.get('starting_bankroll',0):.0f}</span><br>
            Total Stakes Recommended: <span class="highlight-stake">₹{bankroll_state.get('total_stakes',0):.0f}</span><br>
            Theoretical ARB Profit: <span class="profit-highlight">₹{bankroll_state.get('theoretical_arb_profit',0):.2f}</span><br>
            Theoretical EV Exposure: <span class="highlight">₹{bankroll_state.get('theoretical_ev_exposure',0):.2f}</span>
        </div>
        """

    analytics_html = f"""
    <div id="content-analytics" class="tab-content">
        <div class="card fade-in-up">
            <div class="card-header">📈 EV Distribution (Current Scan)</div>
            <div style="margin-bottom:12px;">
                <div class="progress-row">
                    <span class="progress-label">0–2%</span>
                    <div class="progress-bar-bg">
                        <div class="progress-fill" style="width:{min(100, buckets['0-2%']*10)}%;background:linear-gradient(90deg, #3b82f6, #60a5fa);"></div>
                    </div>
                    <span class="progress-count">{buckets['0-2%']}</span>
                </div>
                <div class="progress-row">
                    <span class="progress-label">2–5%</span>
                    <div class="progress-bar-bg">
                        <div class="progress-fill" style="width:{min(100, buckets['2-5%']*10)}%;background:linear-gradient(90deg, #10b981, #34d399);"></div>
                    </div>
                    <span class="progress-count">{buckets['2-5%']}</span>
                </div>
                <div class="progress-row">
                    <span class="progress-label">5–10%</span>
                    <div class="progress-bar-bg">
                        <div class="progress-fill" style="width:{min(100, buckets['5-10%']*10)}%;background:linear-gradient(90deg, #f59e0b, #fbbf24);"></div>
                    </div>
                    <span class="progress-count">{buckets['5-10%']}</span>
                </div>
                <div class="progress-row">
                    <span class="progress-label">10%+</span>
                    <div class="progress-bar-bg">
                        <div class="progress-fill" style="width:{min(100, buckets['10%+']*10)}%;background:linear-gradient(90deg, #ef4444, #f87171);"></div>
                    </div>
                    <span class="progress-count">{buckets['10%+']}</span>
                </div>
            </div>
            <div style="margin-top:16px;font-size:15px;color:#cbd5e1; padding-top: 10px; border-top: 1px dashed #334155;">
                Average EV: <span class="highlight">{avg_ev:.2f}%</span> &nbsp;|&nbsp; Highest EV: <span class="highlight">{max_ev:.2f}%</span>
            </div>
        </div>
    </div>
    """

    evs_json = json.dumps([{
        'pct': e['pct'], 'match': e['match'], 'time': e['time'], 'sport': e['sport'],
        'line': e['line'], 'selection': e['selection'], 'odds': e['odds'], 'true': e['true'],
        'bookie': e['bookie'], 'stake': e['stake'], 'confidence': e.get('confidence', 0),
        'clv_pct': e.get('clv_pct'), 'ev_breakdown': e.get('ev_breakdown', [])
    } for e in evs])

    arbs_json = json.dumps([{
        'pct': a['pct'], 'match': a['match'], 'time': a['time'], 'sport': a['sport'],
        'line': a['line'], 'ways': a.get('ways', 2),
        's1': a['s1'], 's1_price': a['s1_data']['price'], 's1_bookie': a['s1_data']['bookie'],
        's2': a['s2'], 's2_price': a['s2_data']['price'], 's2_bookie': a['s2_data']['bookie'],
        's3': a.get('s3', ''), 's3_price': a.get('s3_data', {}).get('price', 0), 's3_bookie': a.get('s3_data', {}).get('bookie', ''),
        'stk1': a['stk1'], 'stk2': a['stk2'], 'stk3': a.get('stk3', 0), 'profit': a['profit']
    } for a in arbs])

    credits_burned = int(requests_used_total) - scan_starting_used if scan_starting_used is not None and str(requests_used_total).isdigit() else "Unknown"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Arb Sniper Live Dashboard</title>
    
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet">
    
    <style>
        :root {{
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --bg-hover: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent-blue: #3b82f6;
            --accent-green: #22c55e;
            --accent-gold: #fbbf24;
            --accent-red: #ef4444;
            --border-color: #334155;
        }}

        body {{ font-family: 'Inter', -apple-system, sans-serif; background-color: var(--bg-main); color: var(--text-main); margin: 0; padding: 20px; max-width: 850px; margin: auto; }}
        h1 {{ color: var(--accent-blue); text-align: center; font-size: 32px; font-weight: 800; letter-spacing: -1px; margin-bottom: 5px; text-shadow: 0 0 20px rgba(59, 130, 246, 0.3); }}
        .time {{ text-align: center; color: var(--text-muted); font-size: 14px; margin-bottom: 25px; font-weight: 600; letter-spacing: 0.5px; }}
        
        /* Stunning Buttons */
        .btn-run {{ 
            background: linear-gradient(135deg, #10b981 0%, #059669 100%); 
            color: white; border: none; padding: 14px 28px; font-size: 16px; border-radius: 8px; cursor: pointer; 
            font-weight: 800; width: 100%; margin-bottom: 12px; transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.3);
            text-transform: uppercase; letter-spacing: 1px;
            animation: pulse-glow 2s infinite;
        }}
        .btn-run:hover {{ transform: translateY(-2px); box-shadow: 0 8px 25px rgba(16, 185, 129, 0.5); }}
        .btn-run:active {{ transform: translateY(1px); }}
        
        .btn-export {{ background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); color: white; border: none; padding: 12px 20px; font-size: 14px; border-radius: 8px; cursor: pointer; font-weight: 600; width: 100%; margin-bottom: 25px; transition: all 0.2s ease; }}
        .btn-export:hover {{ box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4); transform: translateY(-1px); }}

        /* Sleek Control Bar */
        .controls-bar {{ display: flex; align-items: center; gap: 15px; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px 20px; margin-bottom: 20px; flex-wrap: wrap; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        .controls-bar label {{ color: var(--text-muted); font-size: 14px; font-weight: 600; }}
        .controls-bar input[type=range] {{ flex: 1; min-width: 100px; accent-color: var(--accent-blue); cursor: pointer; }}
        .controls-bar span {{ color: var(--accent-gold); font-weight: 800; font-family: 'JetBrains Mono', monospace; font-size: 16px; min-width: 45px; text-align: right; }}
        
        .toggle-label {{ display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--text-main); cursor: pointer; font-weight: 600; padding: 6px 12px; background: var(--bg-main); border-radius: 6px; border: 1px solid var(--border-color); transition: 0.2s; }}
        .toggle-label:hover {{ border-color: var(--accent-blue); }}
        .toggle-label input {{ accent-color: var(--accent-blue); width: 18px; height: 18px; cursor: pointer; }}

        /* Smooth Tabs */
        .tabs {{ display: flex; background: var(--bg-card); border-radius: 10px; padding: 6px; margin-bottom: 25px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        .tab {{ flex: 1; text-align: center; padding: 12px; cursor: pointer; font-size: 15px; font-weight: 600; color: var(--text-muted); border-radius: 6px; transition: all 0.3s ease; }}
        .tab:hover {{ color: var(--text-main); }}
        .tab.active {{ color: #fff; background: var(--bg-hover); box-shadow: 0 2px 8px rgba(0,0,0,0.2); }}
        .tab-content {{ display: none; opacity: 0; transition: opacity 0.4s ease-out; }}
        .tab-content.active {{ display: block; opacity: 1; animation: fadeIn 0.5s ease-out forwards; }}

        /* Interactive Cards */
        .card {{ 
            background: linear-gradient(145deg, #1e293b 0%, #0f172a 100%); 
            border: 1px solid var(--border-color); 
            border-radius: 12px; 
            padding: 20px; 
            margin-bottom: 20px; 
            font-size: 15px; 
            line-height: 1.6; 
            box-shadow: 0 4px 10px rgba(0,0,0,0.2);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }}
        .card:hover {{ 
            transform: translateY(-4px) scale(1.01); 
            box-shadow: 0 12px 24px rgba(0,0,0,0.4); 
            border-color: #475569; 
        }}
        
        /* Add a subtle highlight accent line to the left of cards */
        .card.ev-card::before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--accent-blue); border-radius: 12px 0 0 12px; }}
        .card.arb-card::before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--accent-gold); border-radius: 12px 0 0 12px; }}

        .card-header {{ font-weight: 800; font-size: 18px; margin-bottom: 15px; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; display: flex; justify-content: space-between; align-items: center; letter-spacing: -0.5px; }}
        .detail-block {{ margin-bottom: 15px; color: #cbd5e1; }}
        
        /* Highlight typography */
        .highlight {{ color: var(--accent-blue); font-weight: 800; }}
        .highlight-edge {{ color: var(--text-main); background: linear-gradient(90deg, #3b82f6, #2563eb); padding: 4px 10px; border-radius: 6px; font-size: 14px; text-shadow: 0 1px 2px rgba(0,0,0,0.2); display: inline-block; }}
        .highlight-arb-edge {{ color: var(--bg-main); background: linear-gradient(90deg, #fbbf24, #f59e0b); padding: 4px 10px; border-radius: 6px; font-size: 14px; display: inline-block; }}
        
        .highlight-stake {{ color: var(--accent-gold); font-weight: 800; font-family: 'JetBrains Mono', monospace; font-size: 16px; background: rgba(251, 191, 36, 0.1); padding: 2px 6px; border-radius: 4px; }}
        .profit-highlight {{ color: var(--accent-green); font-weight: 800; font-size: 18px; font-family: 'JetBrains Mono', monospace; text-shadow: 0 0 10px rgba(34, 197, 94, 0.2); }}
        
        /* Badges */
        .badge {{ padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }}
        .conf-high {{ background: linear-gradient(135deg, #10b981, #059669); color: white; }}
        .conf-med {{ background: linear-gradient(135deg, #f59e0b, #d97706); color: white; }}
        .conf-low {{ background: linear-gradient(135deg, #ef4444, #dc2626); color: white; }}
        
        /* Tables */
        table.breakdown {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 12px; font-size: 13px; border-radius: 8px; overflow: hidden; border: 1px solid var(--border-color); }}
        table.breakdown th, table.breakdown td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }}
        table.breakdown th {{ background: rgba(51, 65, 85, 0.5); color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 1px; }}
        table.breakdown tr:last-child td {{ border-bottom: none; }}
        table.breakdown tr:hover td {{ background: rgba(255,255,255,0.03); }}
        
        /* Progress bars for analytics */
        .progress-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
        .progress-label {{ width: 60px; color: var(--text-muted); font-weight: 600; font-size: 13px; }}
        .progress-bar-bg {{ flex: 1; background: #0f172a; border-radius: 8px; height: 20px; overflow: hidden; border: 1px solid var(--border-color); box-shadow: inset 0 2px 4px rgba(0,0,0,0.3); }}
        .progress-fill {{ height: 100%; border-radius: 6px; transition: width 1s cubic-bezier(0.4, 0, 0.2, 1); }}
        .progress-count {{ width: 35px; text-align: right; font-weight: 800; font-family: 'JetBrains Mono', monospace; }}

        .empty-state {{ text-align: center; color: var(--text-muted); padding: 40px; font-weight: 600; background: var(--bg-card); border-radius: 12px; border: 2px dashed var(--border-color); font-size: 16px; }}
        .telemetry {{ text-align: center; margin-top: 50px; padding-top: 25px; border-top: 1px solid var(--border-color); font-size: 13px; color: var(--text-muted); line-height: 1.8; }}
        
        details summary {{ cursor: pointer; color: var(--accent-blue); margin-top: 12px; font-size: 14px; font-weight: 600; padding: 8px 0; outline: none; }}
        details summary:hover {{ text-decoration: underline; }}
        details summary::-webkit-details-marker {{ color: var(--accent-blue); }}

        /* Animations */
        @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
        @keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(20px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes pulse-glow {{ 
            0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.4); }} 
            70% {{ box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }} 
            100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }} 
        }}
        
        .fade-in-up {{ opacity: 0; animation: fadeUp 0.6s cubic-bezier(0.4, 0, 0.2, 1) forwards; }}
    </style>
</head>
<body>
    <h1> Arb Sniper Terminal</h1>
    <div class="time">Last Sweep: {current_time}</div>
    
    {bankroll_html}
    
    <button class="btn-run" onclick="triggerScan()"> Launch Cloud Scan Now</button>
    <button class="btn-export" onclick="exportCSV()"> Download CSV Report</button>

    <div class="controls-bar fade-in-up" style="animation-delay: 0.2s;">
        <label for="kellySlider"> Kelly Fraction:</label>
        <input type="range" id="kellySlider" min="0" max="100" value="30" oninput="updateKelly(this.value)">
        <span id="kellyValue">30%</span>
        
        <div style="flex-grow:1;"></div>
        
        <label class="toggle-label">
            <input type="checkbox" id="top5Toggle" onchange="renderData()">
            Top 5 Only
        </label>
    </div>

    <div class="tabs fade-in-up" style="animation-delay: 0.3s;">
        <div class="tab active" id="tab-ev" onclick="switchTab('ev')">💎 EV Edges (<span id="ev-count">0</span>)</div>
        <div class="tab" id="tab-arb" onclick="switchTab('arb')">🏆 Arbitrage (<span id="arb-count">0</span>)</div>
        <div class="tab" id="tab-analytics" onclick="switchTab('analytics')">📊 Analytics</div>
    </div>

    <div id="content-ev" class="tab-content active"></div>
    <div id="content-arb" class="tab-content"></div>
    {analytics_html}

    <div class="telemetry">
        <strong>SYSTEM TELEMETRY</strong><br>
        Active Key: #{current_key_index + 1} | Monthly Quota: {requests_remaining}/500 | Scan Cost: ~{credits_burned} credits
    </div>

    <script>
        const ALL_EVS = {evs_json};
        const ALL_ARBS = {arbs_json};
        const bookCaps = {BOOK_CAPS};
        const totalBankroll = {TOTAL_BANKROLL};

        function displayBookie(api_key) {{
            const mapping = {{'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet', 'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'}};
            return mapping[api_key] || (api_key.charAt(0).toUpperCase() + api_key.slice(1));
        }}

        function switchTab(tab) {{
            ['ev','arb','analytics'].forEach(t => {{
                document.getElementById('content-'+t).classList.remove('active');
                document.getElementById('tab-'+t).classList.remove('active');
            }});
            document.getElementById('content-'+tab).classList.add('active');
            document.getElementById('tab-'+tab).classList.add('active');
        }}

        function renderData() {{
            let isTop5 = document.getElementById('top5Toggle').checked;
            let sliderVal = parseInt(document.getElementById('kellySlider').value) / 100;
            
            let evLimit = isTop5 ? 5 : ALL_EVS.length;
            let arbLimit = isTop5 ? 5 : ALL_ARBS.length;
            
            document.getElementById('ev-count').innerText = ALL_EVS.length;
            document.getElementById('arb-count').innerText = ALL_ARBS.length;

            // --- EV RENDER ---
            let evHtml = '';
            for(let i=0; i<Math.min(evLimit, ALL_EVS.length); i++) {{
                let ev = ALL_EVS[i];
                let true_prob = 1 / ev.true;
                let b = ev.odds - 1.0;
                
                let raw_k = ((b * true_prob - (1-true_prob)) / b) * sliderVal;
                let max_k = (0.05 / 0.30) * sliderVal; 
                if (raw_k > max_k) raw_k = max_k;
                
                let stake = Math.max(20, totalBankroll * raw_k);
                if (raw_k <= 0) stake = 0;
                let cap = bookCaps[ev.bookie] || 1000;
                stake = Math.min(stake, cap);

                let confClass = ev.confidence >= 75 ? 'conf-high' : (ev.confidence >= 50 ? 'conf-med' : 'conf-low');

                let bdHtml = '<table class="breakdown"><tr><th>Bookmaker</th><th>Odds</th><th>EV%</th></tr>';
                let sortedBreakdown = Object.entries(ev.ev_breakdown || []).sort((a,b) => b[1].ev_pct - a[1].ev_pct);
                
                for(let row of ev.ev_breakdown || []) {{
                    let hl = row.is_best ? 'style="background:rgba(59, 130, 246, 0.15);"' : '';
                    let bestIcon = row.is_best ? '⭐ ' : '';
                    bdHtml += `<tr ${{hl}}><td>${{bestIcon}}${{displayBookie(row.bookie)}}</td><td><span class="highlight-stake" style="background:transparent;padding:0;">${{row.odds.toFixed(3)}}</span></td><td><strong style="color:${{row.ev_pct > 0 ? 'var(--accent-green)' : 'var(--text-muted)'}}">${{row.ev_pct > 0 ? '+' : ''}}${{row.ev_pct.toFixed(2)}}%</strong></td></tr>`;
                }}
                bdHtml += '</table>';

                let clvStr = ev.clv_pct !== null ? `&nbsp;|&nbsp; Historic CLV: <span class="highlight" style="color:${{ev.clv_pct > 0 ? 'var(--accent-green)' : 'var(--accent-red)'}}">${{ev.clv_pct > 0 ? '+' : ''}}${{ev.clv_pct.toFixed(2)}}%</span>` : '';
                
                // Add cascade animation delay
                let delay = 0.1 + (i * 0.05);

                evHtml += `
                <div class="card ev-card fade-in-up" style="animation-delay: ${{delay}}s;">
                    <div class="card-header"> 
                        <span><span class="highlight-edge">${{ev.pct.toFixed(2)}}% EV</span> &nbsp;${{ev.match}}</span>
                        <span class="badge ${{confClass}}">Conf: ${{ev.confidence.toFixed(0)}}/100</span>
                    </div>
                    <div class="detail-block" style="display:flex; justify-content:space-between;">
                        <div>
                            🏆 <span style="color:#e2e8f0; font-weight:600;">${{ev.sport.replace('_', ' ').toUpperCase()}}</span><br>
                            📅 ${{ev.time}}<br>
                            📈 Line: <span class="highlight">${{ev.line}}</span>
                        </div>
                        <div style="text-align:right; background:rgba(0,0,0,0.2); padding:10px; border-radius:8px; border:1px solid var(--border-color);">
                            <span style="font-size:12px; color:var(--text-muted); text-transform:uppercase;">Rec. Stake</span><br>
                            <span class="highlight-stake stake-display" data-base-stake="${{ev.stake.toFixed(2)}}" style="font-size:20px;">₹${{stake.toFixed(0)}}</span><br>
                            <span style="font-size:11px; color:var(--text-muted);">Cap: ₹${{cap}}</span>
                        </div>
                    </div>
                    <div class="detail-block" style="background:rgba(59, 130, 246, 0.05); padding:12px; border-radius:8px; border-left:3px solid var(--accent-blue);">
                        👉 Bet <span class="highlight">${{ev.selection.toUpperCase()}} ${(ev.line.split('_')[1] || '').replace('0', '')} @ <span style="font-size:18px;">${{ev.odds.toFixed(2)}}</span></span> on <strong>${{displayBookie(ev.bookie)}}</strong>
                    </div>
                    <div style="font-size:13px; color:var(--text-muted); margin-top:15px;">
                        🧠 True Odds: <strong>${{ev.true.toFixed(3)}}</strong> ${{clvStr}}
                        <details><summary>🔍 View Bookmaker Odds Breakdown</summary>${{bdHtml}}</details>
                    </div>
                </div>`;
            }}
            document.getElementById('content-ev').innerHTML = evHtml || '<div class="empty-state">❌ No massive EV edges found right now.</div>';

            // --- ARB RENDER ---
            let arbHtml = '';
            for(let i=0; i<Math.min(arbLimit, ALL_ARBS.length); i++) {{
                let arb = ALL_ARBS[i];
                let is3way = arb.ways === 3;
                
                let stks = `
                    <div style="margin-bottom:6px;">🔵 <span class="highlight-stake">₹${{arb.stk1.toFixed(0)}}</span> on <span class="highlight">${{arb.s1.toUpperCase()}} @ ${{arb.s1_price.toFixed(2)}}</span> [${{displayBookie(arb.s1_bookie)}}]</div>
                    <div>🔴 <span class="highlight-stake">₹${{arb.stk2.toFixed(0)}}</span> on <span class="highlight">${{arb.s2.toUpperCase()}} @ ${{arb.s2_price.toFixed(2)}}</span> [${{displayBookie(arb.s2_bookie)}}]</div>
                `;
                if(is3way) {{
                    stks += `<div style="margin-top:6px;">🟢 <span class="highlight-stake">₹${{arb.stk3.toFixed(0)}}</span> on <span class="highlight">${{arb.s3.toUpperCase()}} @ ${{arb.s3_price.toFixed(2)}}</span> [${{displayBookie(arb.s3_bookie)}}]</div>`;
                }}
                
                let delay = 0.1 + (i * 0.05);

                arbHtml += `
                <div class="card arb-card fade-in-up" style="animation-delay: ${{delay}}s;">
                    <div class="card-header"> 
                        <span><span class="highlight-arb-edge">${{arb.pct.toFixed(2)}}% ARB</span> &nbsp;${{arb.match}}</span>
                        <span class="badge" style="background:#475569; color:white;">${{is3way ? '3-Way' : '2-Way'}}</span>
                    </div>
                    <div class="detail-block">
                        🏆 <span style="color:#e2e8f0; font-weight:600;">${{arb.sport.replace('_', ' ').toUpperCase()}}</span><br>
                        📅 ${{arb.time}}<br>
                        📈 Line: <span class="highlight">${{arb.line}}</span>
                    </div>
                    <div class="detail-block" style="background:rgba(251, 191, 36, 0.05); padding:12px; border-radius:8px; border-left:3px solid var(--accent-gold);">
                        ${{stks}}
                    </div>
                    <div style="display:flex; align-items:center; justify-content:space-between; margin-top:15px; border-top:1px dashed var(--border-color); padding-top:12px;">
                        <span style="color:var(--text-muted); font-size:14px; font-weight:600; text-transform:uppercase;">Guaranteed Profit</span>
                        <span class="profit-highlight" style="font-size:22px;">+ ₹${{arb.profit.toFixed(0)}}</span>
                    </div>
                </div>`;
            }}
            document.getElementById('content-arb').innerHTML = arbHtml || '<div class="empty-state">❌ No Arbitrage opportunities found right now.</div>';
        }}

        function updateKelly(val) {{
            document.getElementById('kellyValue').textContent = val + '%';
            renderData(); // Re-render to update stakes across the board and trigger animations
        }}

        function exportCSV() {{
            let rows = [['Type','Match','Sport','Line','Selection','Odds','EV%/ARB%','Stake','Bookmaker']];
            ALL_EVS.forEach(e => {{
                rows.push(['EV', e.match, e.sport, e.line, e.selection, e.odds, e.pct.toFixed(2), e.stake.toFixed(0), e.bookie]);
            }});
            ALL_ARBS.forEach(a => {{
                rows.push(['ARB', a.match, a.sport, a.line, a.s1+'/'+a.s2, a.s1_price+'/'+a.s2_price, a.pct.toFixed(2), (a.stk1+a.stk2).toFixed(0), a.s1_bookie+'+'+a.s2_bookie]);
            }});
            const csv = rows.map(r => r.map(c => '"' + String(c).replace(/"/g,'""') + '"').join(',')).join('\\n');
            const blob = new Blob([csv], {{type:'text/csv'}});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = 'arb_sniper_export.csv'; a.click();
            URL.revokeObjectURL(url);
        }}

        function triggerScan() {{
            let pat = localStorage.getItem('gh_dispatch_token');
            if (!pat) {{
                pat = prompt("Enter your GitHub PAT (ghp_...) to authorize this scan:\\n(This is safely stored only in your local browser, never public)");
                if (!pat) return;
                localStorage.setItem('gh_dispatch_token', pat);
            }}
            fetch('https://api.github.com/repos/nikunj7711/arb-sniper/actions/workflows/sniper.yml/dispatches', {{
                method: 'POST',
                headers: {{
                    'Accept': 'application/vnd.github.v3+json',
                    'Authorization': 'token ' + pat,
                    'Content-Type': 'application/json'
                }},
                body: JSON.stringify({{ ref: 'main' }})
            }})
            .then(response => {{
                if(response.ok) {{
                    alert("✅ Engine Fired! The cloud server is scanning 5 sports simultaneously. Wait 2-3 minutes, then Hard Refresh this page.");
                }} else {{
                    alert("❌ Authorization failed! Your token might be wrong or expired. Resetting token...");
                    localStorage.removeItem('gh_dispatch_token');
                }}
            }})
            .catch(error => console.error('Error:', error));
        }}

        // Initialize Render on Load
        window.onload = function() {{ renderData(); }};
    </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(" Web Dashboard successfully updated with Cinematic UI (index.html)")

# ==========================================
#  MAIN SCANNER
# ==========================================
def run_hybrid_scanner():
    global scan_starting_used
    my_bookies_list = MY_BOOKIES.split(',')
    scan_starting_used = None

    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_str = ist_now.strftime('%d %b %Y, %I:%M:%S %p IST')

    print(f"\n [{current_time_str}] ALL-SPORTS Sweep (EV + ARB) active...")
    all_evs, all_arbs = [], []

    sport_events = fetch_all_sports_parallel(TARGET_SPORTS)

    for sport, events in sport_events.items():
        if not events:
            continue
        for event in events:
            match_name = f"{event['home_team']} vs {event['away_team']}"
            match_time = format_time_ist(event['commence_time'])
            ev_lines, arb_lines = extract_hybrid_data(event.get('bookmakers', []), my_bookies_list)
            new_evs, new_arbs = evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport)
            all_evs.extend(new_evs)
            all_arbs.extend(new_arbs)

    history_log = append_to_history(all_evs, all_arbs)
    all_evs = compute_clv(all_evs, history_log)
    bankroll_state = update_bankroll_state(all_evs, all_arbs)
    
    alert_cache = load_alert_cache()
    alert_cache = prune_alert_cache(alert_cache)

    if all_arbs:
        all_arbs.sort(key=lambda x: x['pct'], reverse=True)
        for arb in all_arbs:
            sel = arb['s1']
            odds = arb['s1_data']['price']
            if is_duplicate_alert(alert_cache, arb['match'], arb['line'], sel, odds):
                continue
            ways = arb.get('ways', 2)
            third_leg = ''
            if ways == 3:
                third_leg = f"\n ₹{arb.get('stk3',0):.0f} on {arb['s3'].upper()} @ {arb['s3_data']['price']:.2f} [{display_bookie(arb['s3_data']['bookie'])}]"
            msg = (f"  {arb['pct']:.2f}% ARB | {arb['match']}\n {arb['sport'].replace('_', ' ').title()}\n"
                   f" {arb['time']}\n {arb['line']}\n\n"
                   f" ₹{arb['stk1']:.0f} on {arb['s1'].upper()} @ {arb['s1_data']['price']:.2f} [{display_bookie(arb['s1_data']['bookie'])}]\n"
                   f" ₹{arb['stk2']:.0f} on {arb['s2'].upper()} @ {arb['s2_data']['price']:.2f} [{display_bookie(arb['s2_data']['bookie'])}]{third_leg}\n\n"
                   f" Profit: ₹{arb['profit']:.0f}")
            send_phone_alert(msg, arb['pct'], arb['match'], "ARB")
            mark_alert_sent(alert_cache, arb['match'], arb['line'], sel, odds)

    if all_evs:
        all_evs.sort(key=lambda x: x['pct'], reverse=True)
        for ev in all_evs:
            if is_duplicate_alert(alert_cache, ev['match'], ev['line'], ev['selection'], ev['odds']):
                continue
            clv_line = f"\n CLV: {ev['clv_pct']:+.2f}%" if ev.get('clv_pct') is not None else ''
            msg = (f"  {ev['pct']:.2f}% EV | {ev['match']}\n {ev['sport'].replace('_', ' ').title()}\n"
                   f" {ev['time']}\n {ev['line']}\n\n"
                   f" BET EXACTLY: ₹{ev['stake']:.0f}\n"
                   f" {ev['selection'].upper()} {ev['line'].split('_')[1]} @ {ev['odds']:.2f} on {display_bookie(ev['bookie'])}\n\n"
                   f" True Odds: {ev['true']:.2f} | Confidence: {ev.get('confidence',0)}/100{clv_line}")
            send_phone_alert(msg, ev['pct'], ev['match'], "EV")
            mark_alert_sent(alert_cache, ev['match'], ev['line'], ev['selection'], ev['odds'])

    save_alert_cache(alert_cache)
    generate_web_dashboard(all_evs, all_arbs, current_time_str, bankroll_state=bankroll_state)

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
