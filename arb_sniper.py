import requests
import time
import os
import json
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
                'type': 'EV', 'timestamp': ist_now_str, 'match': ev['match'],
                'sport': ev['sport'], 'line': ev['line'], 'bookmaker': ev['bookie'],
                'odds': round(ev['odds'], 3), 'true_odds': round(ev['true'], 3),
                'selection': ev['selection'], 'ev_pct': round(ev['pct'], 3),
                'stake': round(ev['stake'], 2), 'clv_pct': None,
            }
            new_entries.append(entry)
            scan_keys.add(key)
    for arb in arbs:
        key = f"{arb['match']}|{arb['line']}|{arb['s1']}|{arb['s1_data']['price']:.2f}|ARB"
        if key not in scan_keys:
            entry = {
                'type': 'ARB', 'timestamp': ist_now_str, 'match': arb['match'],
                'sport': arb['sport'], 'line': arb['line'],
                'bookmaker': f"{arb['s1_data']['bookie']}+{arb['s2_data']['bookie']}",
                'odds': round(arb['s1_data']['price'], 3), 'selection': arb['s1'],
                'arb_pct': round(arb['pct'], 3),
                'stake': round(arb['stk1'] + arb['stk2'], 2), 'clv_pct': None,
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
        emoji = "🔒" if alert_type == "ARB" else "⚡"
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
#  FEATURE 8: BOOKMAKER EV BREAKDOWN
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
                            'pct': ev_pct, 'match': match_name, 'time': match_time, 'sport': sport,
                            'line': line_key, 'selection': side, 'odds': softs[side]['price'],
                            'true': true_odds, 'bookie': softs[side]['bookie'],
                            'stake': stake, 'target_lay': t_lay, 'green_profit': g_profit,
                            'confidence': confidence, 'ev_breakdown': ev_breakdown, 'clv_pct': None,
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
                        'pct': arb_pct, 'match': match_name, 'time': match_time, 'sport': sport,
                        'line': line_key, 'ways': 2,
                        's1': k1, 's1_data': outcomes[k1], 's2': k2, 's2_data': outcomes[k2],
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
                        'pct': arb_pct, 'match': match_name, 'time': match_time, 'sport': sport,
                        'line': line_key, 'ways': 3,
                        's1': k1, 's1_data': outcomes[k1], 's2': k2, 's2_data': outcomes[k2],
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
            if rotate_api_key(): continue
            else: return None
        elif res.status_code == 429:
            if 'quota' in res.json().get('message', '').lower():
                if rotate_api_key(): continue
                else: return None
            else:
                time.sleep(2); continue
        elif res.status_code == 200:
            return res.json()
        else:
            return None

# ==========================================
#  FEATURE 7: PARALLEL SPORT FETCHING
# ==========================================
def fetch_sport_events(sport):
    url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds'
    params = {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'totals,spreads', 'oddsFormat': 'decimal'}
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
#  FEATURE 3: EV + ARB DISTRIBUTION ANALYTICS
# ==========================================
def compute_ev_analytics(all_evs):
    buckets = {'0-2%': 0, '2-5%': 0, '5-10%': 0, '10%+': 0}
    for ev in all_evs:
        p = ev['pct']
        if p < 2: buckets['0-2%'] += 1
        elif p < 5: buckets['2-5%'] += 1
        elif p < 10: buckets['5-10%'] += 1
        else: buckets['10%+'] += 1
    avg_ev = (sum(e['pct'] for e in all_evs) / len(all_evs)) if all_evs else 0
    max_ev = max((e['pct'] for e in all_evs), default=0)
    return buckets, round(avg_ev, 3), round(max_ev, 3)

def compute_arb_analytics(all_arbs):
    arb_buckets = {'0-1%': 0, '1-2%': 0, '2-5%': 0, '5%+': 0}
    sport_counts = {}
    ways_counts = {2: 0, 3: 0}
    total_profit = 0.0
    total_stake = 0.0
    bookie_pairs = {}
    for arb in all_arbs:
        p = arb['pct']
        if p < 1: arb_buckets['0-1%'] += 1
        elif p < 2: arb_buckets['1-2%'] += 1
        elif p < 5: arb_buckets['2-5%'] += 1
        else: arb_buckets['5%+'] += 1
        sport = arb['sport'].replace('_', ' ').title()
        sport_counts[sport] = sport_counts.get(sport, 0) + 1
        ways = arb.get('ways', 2)
        ways_counts[ways] = ways_counts.get(ways, 0) + 1
        total_profit += arb.get('profit', 0)
        total_stake += arb.get('stk1', 0) + arb.get('stk2', 0) + arb.get('stk3', 0)
        pair = f"{arb['s1_data']['bookie']} + {arb['s2_data']['bookie']}"
        bookie_pairs[pair] = bookie_pairs.get(pair, 0) + 1
    avg_arb = (sum(a['pct'] for a in all_arbs) / len(all_arbs)) if all_arbs else 0
    max_arb = max((a['pct'] for a in all_arbs), default=0)
    top_sports = sorted(sport_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    top_pairs = sorted(bookie_pairs.items(), key=lambda x: x[1], reverse=True)[:4]
    return arb_buckets, round(avg_arb, 3), round(max_arb, 3), round(total_profit, 2), round(total_stake, 2), top_sports, top_pairs, ways_counts

def compute_sport_ev_breakdown(all_evs):
    sport_ev = {}
    for ev in all_evs:
        sport = ev['sport'].replace('_', ' ').title()
        if sport not in sport_ev:
            sport_ev[sport] = {'count': 0, 'total_pct': 0.0, 'max_pct': 0.0}
        sport_ev[sport]['count'] += 1
        sport_ev[sport]['total_pct'] += ev['pct']
        sport_ev[sport]['max_pct'] = max(sport_ev[sport]['max_pct'], ev['pct'])
    return sorted(sport_ev.items(), key=lambda x: x[1]['count'], reverse=True)

def compute_bookie_ev_breakdown(all_evs):
    bookie_ev = {}
    for ev in all_evs:
        b = display_bookie(ev['bookie'])
        if b not in bookie_ev:
            bookie_ev[b] = {'count': 0, 'total_pct': 0.0}
        bookie_ev[b]['count'] += 1
        bookie_ev[b]['total_pct'] += ev['pct']
    return sorted(bookie_ev.items(), key=lambda x: x[1]['count'], reverse=True)

# ==========================================
#  DASHBOARD GENERATION
# ==========================================
def generate_web_dashboard(evs, arbs, current_time, bankroll_state=None):
    evs.sort(key=lambda x: x['pct'], reverse=True)
    arbs.sort(key=lambda x: x['pct'], reverse=True)
    buckets, avg_ev, max_ev = compute_ev_analytics(evs)
    arb_buckets, avg_arb, max_arb, total_arb_profit, total_arb_stake, top_arb_sports, top_bookie_pairs, ways_counts = compute_arb_analytics(arbs)
    sport_ev_data = compute_sport_ev_breakdown(evs)
    bookie_ev_data = compute_bookie_ev_breakdown(evs)

    def ev_breakdown_html(breakdown):
        if not breakdown:
            return ''
        rows = ''
        for row in breakdown:
            best_cls = ' class="breakdown-best"' if row['is_best'] else ''
            sign = '+' if row['ev_pct'] > 0 else ''
            rows += f'<tr{best_cls}><td>{display_bookie(row["bookie"])}</td><td>{row["odds"]:.3f}</td><td>{sign}{row["ev_pct"]:.2f}%</td></tr>'
        return f'<div class="breakdown-wrap"><table class="breakdown-table"><thead><tr><th>Book</th><th>Odds</th><th>EV%</th></tr></thead><tbody>{rows}</tbody></table></div>'

    def sport_icon(sport):
        icons = {'soccer': '⚽', 'basketball': '🏀', 'icehockey': '🏒', 'tennis': '🎾', 'baseball': '⚾', 'football': '🏈'}
        for k, v in icons.items():
            if k in sport: return v
        return '🎯'

    def conf_bar(score):
        color = '#00ff88' if score >= 70 else ('#ffd700' if score >= 40 else '#ff4d6d')
        label = 'HIGH' if score >= 70 else ('MED' if score >= 40 else 'LOW')
        return f'<div class="conf-bar-wrap"><span class="conf-label" style="color:{color}">{label}</span><div class="conf-bar-track"><div class="conf-bar-fill" style="width:{score}%;background:{color};box-shadow:0 0 8px {color}88;"></div></div><span class="conf-num" style="color:{color}">{score}</span></div>'

    evs_json = json.dumps([{
        'pct': e['pct'], 'match': e['match'], 'time': e['time'], 'sport': e['sport'],
        'line': e['line'], 'selection': e['selection'], 'odds': e['odds'], 'true': e['true'],
        'bookie': e['bookie'], 'stake': e['stake'], 'confidence': e.get('confidence', 0),
        'clv_pct': e.get('clv_pct')
    } for e in evs])

    arbs_json = json.dumps([{
        'pct': a['pct'], 'match': a['match'], 'time': a['time'], 'sport': a['sport'],
        'line': a['line'], 'ways': a.get('ways', 2),
        's1': a['s1'], 's1_price': a['s1_data']['price'], 's1_bookie': a['s1_data']['bookie'],
        's2': a['s2'], 's2_price': a['s2_data']['price'], 's2_bookie': a['s2_data']['bookie'],
        's3': a.get('s3', ''), 's3_price': a.get('s3_data', {}).get('price', 0), 's3_bookie': a.get('s3_data', {}).get('bookie', ''),
        'stk1': a['stk1'], 'stk2': a['stk2'], 'stk3': a.get('stk3', 0), 'profit': a['profit']
    } for a in arbs])

    # Build EV cards
    ev_cards_inner = ''
    if not evs:
        ev_cards_inner = '<div class="empty-state"><div class="empty-icon">📡</div><div>Radar scanning... No EV edges detected.</div><div class="empty-sub">Markets are efficient right now.</div></div>'
    else:
        for i, ev in enumerate(evs):
            icon = sport_icon(ev['sport'])
            clean_sport = ev['sport'].replace('_', ' ').title()
            rank_class = 'rank-gold' if i == 0 else ('rank-silver' if i == 1 else ('rank-bronze' if i == 2 else ''))
            ev_color = '#ff4d6d' if ev['pct'] >= 10 else ('#ffd700' if ev['pct'] >= 5 else '#00ff88')
            clv_html = ''
            if ev.get('clv_pct') is not None:
                clv_c = '#00ff88' if ev['clv_pct'] >= 0 else '#ff4d6d'
                clv_html = f'<span class="clv-badge" style="color:{clv_c};border-color:{clv_c}88;">CLV {ev["clv_pct"]:+.2f}%</span>'
            bd_html = ev_breakdown_html(ev.get('ev_breakdown', []))
            ev_cards_inner += f'''<div class="card ev-card" data-pct="{ev['pct']}" data-stake="{ev['stake']:.2f}" style="animation-delay:{i*0.07}s">
  <div class="card-rank-stripe {rank_class}"></div>
  <div class="card-header">
    <div class="card-header-left"><span class="sport-icon">{icon}</span><div><div class="match-name">{ev['match']}</div><div class="match-meta">{clean_sport} · {ev['time']}</div></div></div>
    <div class="ev-badge" style="color:{ev_color};border-color:{ev_color}44;text-shadow:0 0 14px {ev_color}88;">{ev['pct']:.2f}%</div>
  </div>
  <div class="card-body">
    <div class="line-pill">{ev['line']}</div>
    <div class="bet-row">
      <div class="bet-info"><span class="bet-label">BET ON</span><span class="bet-selection">{ev['selection'].upper()}</span><span class="bet-point">@ {ev['line'].split('_')[1]}</span><span class="bet-odds">× {ev['odds']:.2f}</span></div>
      <div class="bookie-tag">{display_bookie(ev['bookie'])}</div>
    </div>
    <div class="stake-row">
      <div class="stake-block"><span class="stake-label">KELLY STAKE</span><span class="stake-amount stake-display" data-base-stake="{ev['stake']:.2f}">₹{ev['stake']:.0f}</span></div>
      <div class="true-odds-block"><span class="stake-label">TRUE ODDS</span><span class="true-val">{ev['true']:.3f}</span></div>
    </div>
    <div class="conf-section"><span class="stake-label">CONFIDENCE</span>{conf_bar(ev.get('confidence', 0))}{clv_html}</div>
    {bd_html}
  </div>
</div>'''

    # Build ARB cards
    arb_cards_inner = ''
    if not arbs:
        arb_cards_inner = '<div class="empty-state"><div class="empty-icon">🔒</div><div>No arbitrage windows open.</div><div class="empty-sub">Books are aligned.</div></div>'
    else:
        for i, arb in enumerate(arbs):
            icon = sport_icon(arb['sport'])
            clean_sport = arb['sport'].replace('_', ' ').title()
            ways = arb.get('ways', 2)
            ways_color = '#00c8ff' if ways == 2 else '#bf5af2'
            legs_html = f'''<div class="leg-row"><div class="leg-side">{arb['s1'].upper()}</div><div class="leg-odds">@ {arb['s1_data']['price']:.2f}</div><div class="leg-stake stake-display" data-base-stake="{arb['stk1']:.2f}">₹{arb['stk1']:.0f}</div><div class="leg-book">{display_bookie(arb['s1_data']['bookie'])}</div></div><div class="leg-row"><div class="leg-side">{arb['s2'].upper()}</div><div class="leg-odds">@ {arb['s2_data']['price']:.2f}</div><div class="leg-stake stake-display" data-base-stake="{arb['stk2']:.2f}">₹{arb['stk2']:.0f}</div><div class="leg-book">{display_bookie(arb['s2_data']['bookie'])}</div></div>'''
            if ways == 3 and 's3' in arb:
                legs_html += f'<div class="leg-row"><div class="leg-side">{arb["s3"].upper()}</div><div class="leg-odds">@ {arb["s3_data"]["price"]:.2f}</div><div class="leg-stake stake-display" data-base-stake="{arb.get("stk3",0):.2f}">₹{arb.get("stk3",0):.0f}</div><div class="leg-book">{display_bookie(arb["s3_data"]["bookie"])}</div></div>'
            arb_cards_inner += f'''<div class="card arb-card" style="animation-delay:{i*0.07}s">
  <div class="card-rank-stripe arb-stripe"></div>
  <div class="card-header">
    <div class="card-header-left"><span class="sport-icon">{icon}</span><div><div class="match-name">{arb['match']}</div><div class="match-meta">{clean_sport} · {arb['time']}</div></div></div>
    <div class="arb-badge"><span style="color:{ways_color};font-family:\'Space Mono\',monospace;font-size:22px;font-weight:700;">{arb['pct']:.2f}%</span><span class="ways-tag" style="background:{ways_color}22;color:{ways_color};border-color:{ways_color}44">{ways}W</span></div>
  </div>
  <div class="card-body">
    <div class="line-pill arb-line-pill">{arb['line']}</div>
    <div class="legs-table">{legs_html}</div>
    <div class="profit-row"><span class="profit-label">GUARANTEED PROFIT</span><span class="profit-val">₹{arb['profit']:.0f}</span></div>
  </div>
</div>'''

    bk = bankroll_state or {}
    bankroll_html = f'''
<!-- BANKROLL EDITOR MODAL -->
<div id="bk-modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(7,9,15,0.85);z-index:1000;backdrop-filter:blur(6px);align-items:center;justify-content:center;">
  <div style="background:#0d1117;border:1px solid #253044;border-radius:20px;padding:32px 28px;width:min(380px,90vw);position:relative;box-shadow:0 0 60px #00c8ff18,0 24px 80px rgba(0,0,0,0.6);">
    <div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#00c8ff,#00ff88,#ffd700);border-radius:20px 20px 0 0;"></div>
    <div style="font-family:'Space Mono',monospace;font-size:10px;letter-spacing:3px;color:#64748b;text-transform:uppercase;margin-bottom:6px;">Configure</div>
    <div style="font-size:22px;font-weight:800;color:#e2e8f0;margin-bottom:24px;">Edit Bankroll</div>
    <div style="margin-bottom:16px;">
      <label style="font-family:'Space Mono',monospace;font-size:9px;letter-spacing:1.5px;color:#64748b;display:block;margin-bottom:8px;text-transform:uppercase;">Total Bankroll (₹)</label>
      <div style="position:relative;">
        <span style="position:absolute;left:14px;top:50%;transform:translateY(-50%);color:#ffd700;font-size:18px;font-weight:700;">₹</span>
        <input id="bk-input" type="number" min="100" step="100" value="{bk.get('starting_bankroll', TOTAL_BANKROLL):.0f}"
          style="width:100%;background:#141923;border:1px solid #253044;border-radius:10px;padding:12px 14px 12px 36px;font-size:22px;font-weight:700;font-family:'Space Mono',monospace;color:#ffd700;outline:none;transition:border-color 0.2s ease;"
          onfocus="this.style.borderColor='#ffd700';this.style.boxShadow='0 0 16px #ffd70022'"
          onblur="this.style.borderColor='#253044';this.style.boxShadow='none'">
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:6px;">
      <button onclick="saveBankroll()" style="background:linear-gradient(135deg,#ffd70020,#ff950015);border:1px solid #ffd700;color:#ffd700;padding:12px;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Outfit',sans-serif;transition:all 0.2s ease;" onmouseover="this.style.background='linear-gradient(135deg,#ffd70030,#ff950025)'" onmouseout="this.style.background='linear-gradient(135deg,#ffd70020,#ff950015)'">💾 Save & Apply</button>
      <button onclick="closeBkModal()" style="background:#141923;border:1px solid #253044;color:#94a3b8;padding:12px;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Outfit',sans-serif;transition:all 0.2s ease;" onmouseover="this.style.borderColor='#94a3b8'" onmouseout="this.style.borderColor='#253044'">Cancel</button>
    </div>
    <div style="font-size:11px;color:#64748b;font-family:'Space Mono',monospace;text-align:center;margin-top:8px;">Saved in browser · affects Kelly stake sizing</div>
  </div>
</div>

<div class="bankroll-bar" onclick="openBkModal()" title="Click to edit bankroll" style="cursor:pointer;">
  <div class="bk-stat">
    <div class="bk-label">BANKROLL</div>
    <div class="bk-val" id="bk-display">₹<span id="bk-amount">{bk.get('starting_bankroll', TOTAL_BANKROLL):.0f}</span></div>
  </div>
  <div class="bk-divider"></div>
  <div class="bk-stat"><div class="bk-label">STAKES TODAY</div><div class="bk-val bk-yellow">₹{bk.get('total_stakes', 0):.0f}</div></div>
  <div class="bk-divider"></div>
  <div class="bk-stat"><div class="bk-label">ARB PROFIT</div><div class="bk-val bk-green">₹{bk.get('theoretical_arb_profit', 0):.0f}</div></div>
  <div class="bk-divider"></div>
  <div class="bk-stat"><div class="bk-label">EV EXPOSURE</div><div class="bk-val bk-blue">₹{bk.get('theoretical_ev_exposure', 0):.0f}</div></div>
  <div style="position:absolute;top:10px;right:12px;font-size:14px;opacity:0.4;">✏️</div>
</div>'''

    b02 = buckets['0-2%']; b25 = buckets['2-5%']; b510 = buckets['5-10%']; b10p = buckets['10%+']
    max_b = max(b02, b25, b510, b10p, 1)

    ab01 = arb_buckets['0-1%']; ab12 = arb_buckets['1-2%']; ab25 = arb_buckets['2-5%']; ab5p = arb_buckets['5%+']
    max_ab = max(ab01, ab12, ab25, ab5p, 1)

    def sport_rows_html(data, color):
        if not data:
            return '<div style="color:#64748b;font-family:Space Mono,monospace;font-size:11px;padding:8px 0;">No data</div>'
        max_c = max(d[1]['count'] for d in data) if data else 1
        out = ''
        for sport, info in data:
            pct_w = int(info['count'] / max_c * 100)
            avg = info['total_pct'] / info['count'] if info['count'] else 0
            out += f'<div class="an-sport-row"><div class="an-sport-name">{sport}</div><div class="an-sport-bar-track"><div style="width:{pct_w}%;background:{color};height:100%;border-radius:4px;animation:barFill 0.8s ease both;"></div></div><div class="an-sport-stats"><span style="color:{color}">{info["count"]}</span><span style="color:#64748b">avg {avg:.1f}%</span></div></div>'
        return out

    def bookie_rows_html(data):
        if not data:
            return '<div style="color:#64748b;font-family:Space Mono,monospace;font-size:11px;padding:8px 0;">No data</div>'
        max_c = max(d[1]['count'] for d in data) if data else 1
        colors = ['#00c8ff','#00ff88','#ffd700','#bf5af2','#ff4d6d']
        out = ''
        for i, (bookie, info) in enumerate(data):
            c = colors[i % len(colors)]
            pct_w = int(info['count'] / max_c * 100)
            avg = info['total_pct'] / info['count'] if info['count'] else 0
            out += f'<div class="an-sport-row"><div class="an-sport-name" style="color:{c}">{bookie}</div><div class="an-sport-bar-track"><div style="width:{pct_w}%;background:{c};height:100%;border-radius:4px;animation:barFill 0.8s ease both;"></div></div><div class="an-sport-stats"><span style="color:{c}">{info["count"]}</span><span style="color:#64748b">avg {avg:.1f}%</span></div></div>'
        return out

    def arb_pair_rows_html(pairs):
        if not pairs:
            return '<div style="color:#64748b;font-family:Space Mono,monospace;font-size:11px;padding:8px 0;">No data</div>'
        max_c = max(c for _, c in pairs) if pairs else 1
        out = ''
        for pair, count in pairs:
            pct_w = int(count / max_c * 100)
            out += f'<div class="an-sport-row"><div class="an-sport-name" style="font-size:11px;">{pair}</div><div class="an-sport-bar-track"><div style="width:{pct_w}%;background:linear-gradient(90deg,#bf5af2,#00c8ff);height:100%;border-radius:4px;animation:barFill 0.8s ease both;"></div></div><div class="an-sport-stats"><span style="color:#bf5af2">{count}</span></div></div>'
        return out

    two_way_count = ways_counts.get(2, 0)
    three_way_count = ways_counts.get(3, 0)
    total_ways = max(two_way_count + three_way_count, 1)
    roi_pct = (total_arb_profit / total_arb_stake * 100) if total_arb_stake > 0 else 0

    analytics_html = f'''<div id="content-analytics" class="tab-content">

  <!-- TOP KPI ROW -->
  <div class="an-kpi-row">
    <div class="an-kpi-card">
      <div class="an-kpi-icon" style="color:#00c8ff">⚡</div>
      <div class="an-kpi-val" style="color:#00c8ff">{len(evs)}</div>
      <div class="an-kpi-label">EV Edges</div>
    </div>
    <div class="an-kpi-card">
      <div class="an-kpi-icon" style="color:#00ff88">{avg_ev:.2f}%</div>
      <div class="an-kpi-val" style="color:#00ff88">{max_ev:.2f}%</div>
      <div class="an-kpi-label">Avg / Peak EV</div>
    </div>
    <div class="an-kpi-card">
      <div class="an-kpi-icon" style="color:#bf5af2">🔒</div>
      <div class="an-kpi-val" style="color:#bf5af2">{len(arbs)}</div>
      <div class="an-kpi-label">ARB Opps</div>
    </div>
    <div class="an-kpi-card">
      <div class="an-kpi-icon" style="color:#ffd700">₹</div>
      <div class="an-kpi-val" style="color:#ffd700">₹{total_arb_profit:.0f}</div>
      <div class="an-kpi-label">ARB Profit</div>
    </div>
    <div class="an-kpi-card">
      <div class="an-kpi-icon" style="color:#ff9500">📈</div>
      <div class="an-kpi-val" style="color:#ff9500">{roi_pct:.2f}%</div>
      <div class="an-kpi-label">ARB ROI</div>
    </div>
  </div>

  <!-- EV + ARB HISTOGRAMS -->
  <div class="an-two-col">
    <div class="analytics-card">
      <div class="an-title">⚡ EV Distribution</div>
      <div class="histogram">
        <div class="hist-row"><span class="hist-label">0–2%</span><div class="hist-track"><div class="hist-bar" style="width:{b02/max_b*100:.0f}%;background:linear-gradient(90deg,#58a6ff,#00c8ff);"></div></div><span class="hist-count">{b02}</span></div>
        <div class="hist-row"><span class="hist-label">2–5%</span><div class="hist-track"><div class="hist-bar" style="width:{b25/max_b*100:.0f}%;background:linear-gradient(90deg,#00ff88,#00c8ff);"></div></div><span class="hist-count">{b25}</span></div>
        <div class="hist-row"><span class="hist-label">5–10%</span><div class="hist-track"><div class="hist-bar" style="width:{b510/max_b*100:.0f}%;background:linear-gradient(90deg,#ffd700,#ff9500);"></div></div><span class="hist-count">{b510}</span></div>
        <div class="hist-row"><span class="hist-label">10%+</span><div class="hist-track"><div class="hist-bar" style="width:{b10p/max_b*100:.0f}%;background:linear-gradient(90deg,#ff4d6d,#ff6b35);"></div></div><span class="hist-count">{b10p}</span></div>
      </div>
      <div class="an-mini-stats">
        <div><span class="an-ms-label">AVG</span><span style="color:#00ff88">{avg_ev:.2f}%</span></div>
        <div><span class="an-ms-label">PEAK</span><span style="color:#ffd700">{max_ev:.2f}%</span></div>
        <div><span class="an-ms-label">COUNT</span><span style="color:#00c8ff">{len(evs)}</span></div>
      </div>
    </div>

    <div class="analytics-card">
      <div class="an-title">🔒 ARB Distribution</div>
      <div class="histogram">
        <div class="hist-row"><span class="hist-label">0–1%</span><div class="hist-track"><div class="hist-bar" style="width:{ab01/max_ab*100:.0f}%;background:linear-gradient(90deg,#94a3b8,#64748b);"></div></div><span class="hist-count">{ab01}</span></div>
        <div class="hist-row"><span class="hist-label">1–2%</span><div class="hist-track"><div class="hist-bar" style="width:{ab12/max_ab*100:.0f}%;background:linear-gradient(90deg,#bf5af2,#8b3dbb);"></div></div><span class="hist-count">{ab12}</span></div>
        <div class="hist-row"><span class="hist-label">2–5%</span><div class="hist-track"><div class="hist-bar" style="width:{ab25/max_ab*100:.0f}%;background:linear-gradient(90deg,#00c8ff,#bf5af2);"></div></div><span class="hist-count">{ab25}</span></div>
        <div class="hist-row"><span class="hist-label">5%+</span><div class="hist-track"><div class="hist-bar" style="width:{ab5p/max_ab*100:.0f}%;background:linear-gradient(90deg,#00ff88,#00c8ff);"></div></div><span class="hist-count">{ab5p}</span></div>
      </div>
      <div class="an-mini-stats">
        <div><span class="an-ms-label">AVG</span><span style="color:#bf5af2">{avg_arb:.2f}%</span></div>
        <div><span class="an-ms-label">PEAK</span><span style="color:#00c8ff">{max_arb:.2f}%</span></div>
        <div><span class="an-ms-label">COUNT</span><span style="color:#00ff88">{len(arbs)}</span></div>
      </div>
    </div>
  </div>

  <!-- SPORT BREAKDOWNS -->
  <div class="an-two-col">
    <div class="analytics-card">
      <div class="an-title">⚽ EV by Sport</div>
      {sport_rows_html(sport_ev_data, 'linear-gradient(90deg,#00c8ff,#00ff88)')}
    </div>
    <div class="analytics-card">
      <div class="an-title">🔒 ARB by Sport</div>
      {sport_rows_html([(s, dict(count=c, total_pct=0, max_pct=0)) for s, c in top_arb_sports], 'linear-gradient(90deg,#bf5af2,#00c8ff)')}
    </div>
  </div>

  <!-- BOOKIE + ARB PAIRS -->
  <div class="an-two-col">
    <div class="analytics-card">
      <div class="an-title">📚 Top EV Bookmakers</div>
      {bookie_rows_html(bookie_ev_data)}
    </div>
    <div class="analytics-card">
      <div class="an-title">🔗 Top ARB Book Pairs</div>
      {arb_pair_rows_html(top_bookie_pairs)}
    </div>
  </div>

  <!-- ARB STRUCTURE + STAKE BREAKDOWN -->
  <div class="an-two-col">
    <div class="analytics-card">
      <div class="an-title">📐 ARB Structure</div>
      <div class="an-donut-wrap">
        <div class="an-donut" id="an-donut" style="--two-pct:{two_way_count/total_ways*100:.0f}"></div>
        <div class="an-donut-legend">
          <div class="an-dl-row"><span style="background:#00c8ff" class="an-dl-dot"></span><span class="an-dl-label">2-way</span><span class="an-dl-val" style="color:#00c8ff">{two_way_count}</span></div>
          <div class="an-dl-row"><span style="background:#bf5af2" class="an-dl-dot"></span><span class="an-dl-label">3-way</span><span class="an-dl-val" style="color:#bf5af2">{three_way_count}</span></div>
        </div>
      </div>
      <div class="an-mini-stats" style="margin-top:16px;">
        <div><span class="an-ms-label">TOTAL STAKE</span><span style="color:#ffd700">₹{total_arb_stake:.0f}</span></div>
        <div><span class="an-ms-label">PROFIT</span><span style="color:#00ff88">₹{total_arb_profit:.0f}</span></div>
        <div><span class="an-ms-label">ROI</span><span style="color:#ff9500">{roi_pct:.2f}%</span></div>
      </div>
    </div>
    <div class="analytics-card" id="an-confidence-panel">
      <div class="an-title">🎯 EV Confidence Spread</div>
      <div id="an-conf-chart"></div>
    </div>
  </div>

</div>'''

    credits_burned = int(requests_used_total) - scan_starting_used if scan_starting_used is not None and str(requests_used_total).isdigit() else "?"

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>ARB SNIPER ⚡</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Outfit:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{{--bg:#07090f;--surface:#0d1117;--surface2:#141923;--border:#1e2736;--border2:#253044;--cyan:#00c8ff;--green:#00ff88;--gold:#ffd700;--red:#ff4d6d;--purple:#bf5af2;--blue:#58a6ff;--orange:#ff9500;--text:#e2e8f0;--text-muted:#64748b;--text-dim:#94a3b8;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
html{{scroll-behavior:smooth;}}
body{{font-family:'Outfit',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;position:relative;}}
body::before{{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 60% at 10% 10%,#00c8ff08 0%,transparent 60%),radial-gradient(ellipse 60% 50% at 90% 80%,#00ff8806 0%,transparent 60%),radial-gradient(ellipse 50% 40% at 50% 50%,#bf5af208 0%,transparent 70%);pointer-events:none;z-index:0;animation:bgPulse 8s ease-in-out infinite alternate;}}
body::after{{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,200,255,0.012) 2px,rgba(0,200,255,0.012) 4px);pointer-events:none;z-index:0;}}
@keyframes bgPulse{{0%{{opacity:0.6;}}100%{{opacity:1;}}}}
.container{{max-width:820px;margin:0 auto;padding:16px 16px 60px;position:relative;z-index:1;}}

/* HEADER */
.header{{text-align:center;padding:32px 0 20px;}}
.header-logo{{font-family:'Space Mono',monospace;font-size:10px;letter-spacing:6px;color:var(--cyan);text-transform:uppercase;opacity:0.6;margin-bottom:6px;animation:fadeDown 0.6s ease both;}}
.header-title{{font-size:38px;font-weight:800;letter-spacing:-1px;background:linear-gradient(135deg,#00c8ff 0%,#00ff88 50%,#ffd700 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;animation:fadeDown 0.6s 0.1s ease both;line-height:1.1;}}
.header-subtitle{{font-family:'Space Mono',monospace;font-size:10px;color:var(--text-muted);margin-top:8px;letter-spacing:2px;animation:fadeDown 0.6s 0.2s ease both;}}
.live-dot{{display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-right:6px;box-shadow:0 0 10px var(--green);animation:livePulse 1.5s ease-in-out infinite;}}
.scan-time{{font-family:'Space Mono',monospace;font-size:11px;color:var(--text-muted);margin-top:6px;animation:fadeDown 0.6s 0.3s ease both;}}
@keyframes livePulse{{0%,100%{{transform:scale(1);opacity:1;}}50%{{transform:scale(1.5);opacity:0.5;}}}}
@keyframes fadeDown{{from{{opacity:0;transform:translateY(-14px);}}to{{opacity:1;transform:translateY(0);}}}}

/* BANKROLL */
.bankroll-bar{{display:flex;background:var(--surface);border:1px solid var(--border2);border-radius:14px;padding:14px 20px;margin-bottom:14px;gap:4px;align-items:center;animation:fadeDown 0.6s 0.4s ease both;position:relative;overflow:hidden;}}
.bankroll-bar::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--cyan)44,transparent);}}
.bk-stat{{flex:1;text-align:center;}}
.bk-label{{font-family:'Space Mono',monospace;font-size:8px;letter-spacing:1.5px;color:var(--text-muted);text-transform:uppercase;margin-bottom:4px;}}
.bk-val{{font-size:17px;font-weight:700;color:var(--text);}}
.bk-yellow{{color:var(--gold);text-shadow:0 0 16px #ffd70055;}}
.bk-green{{color:var(--green);text-shadow:0 0 16px #00ff8855;}}
.bk-blue{{color:var(--cyan);text-shadow:0 0 16px #00c8ff55;}}
.bk-divider{{width:1px;height:36px;background:var(--border2);flex-shrink:0;}}

/* BUTTONS */
.btn-row{{display:flex;gap:10px;margin-bottom:14px;animation:fadeDown 0.6s 0.5s ease both;}}
.btn-scan{{flex:2;background:linear-gradient(135deg,#00c8ff18,#00ff8810);border:1px solid var(--cyan);color:var(--cyan);padding:14px 20px;font-size:15px;font-weight:700;font-family:'Outfit',sans-serif;border-radius:12px;cursor:pointer;letter-spacing:0.5px;transition:all 0.25s ease;text-shadow:0 0 12px var(--cyan);box-shadow:0 0 20px #00c8ff0e,inset 0 1px 0 #00c8ff22;position:relative;overflow:hidden;}}
.btn-scan::after{{content:'';position:absolute;top:-50%;left:-60%;width:40%;height:200%;background:linear-gradient(105deg,transparent,rgba(0,200,255,0.12),transparent);transform:skewX(-20deg);transition:left 0.5s ease;}}
.btn-scan:hover::after{{left:130%;}}
.btn-scan:hover{{background:linear-gradient(135deg,#00c8ff28,#00ff8820);box-shadow:0 0 32px #00c8ff20,inset 0 1px 0 #00c8ff44;transform:translateY(-2px);}}
.btn-scan:active{{transform:translateY(0);}}
.btn-scan:disabled{{opacity:0.5;cursor:not-allowed;}}
.btn-export{{flex:1;background:linear-gradient(135deg,#ffd70010,#ff950010);border:1px solid var(--gold);color:var(--gold);padding:14px 16px;font-size:14px;font-weight:700;font-family:'Outfit',sans-serif;border-radius:12px;cursor:pointer;transition:all 0.25s ease;text-shadow:0 0 12px var(--gold);}}
.btn-export:hover{{background:linear-gradient(135deg,#ffd70022,#ff950022);transform:translateY(-2px);box-shadow:0 0 20px #ffd70018;}}

/* CONTROLS */
.controls-bar{{background:var(--surface);border:1px solid var(--border2);border-radius:14px;padding:14px 20px;margin-bottom:14px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;animation:fadeDown 0.6s 0.55s ease both;}}
.ctrl-group{{display:flex;align-items:center;gap:10px;flex:1;min-width:200px;}}
.ctrl-label{{font-family:'Space Mono',monospace;font-size:10px;letter-spacing:1px;color:var(--text-muted);white-space:nowrap;}}
.kelly-slider{{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;background:linear-gradient(90deg,var(--cyan) var(--pct,30%),var(--border2) var(--pct,30%));outline:none;cursor:pointer;}}
.kelly-slider::-webkit-slider-thumb{{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--bg);border:2px solid var(--cyan);box-shadow:0 0 12px var(--cyan),0 0 24px #00c8ff44;cursor:pointer;transition:transform 0.15s ease;}}
.kelly-slider::-webkit-slider-thumb:active{{transform:scale(1.3);}}
.kelly-val{{font-family:'Space Mono',monospace;font-size:15px;font-weight:700;color:var(--cyan);min-width:40px;text-align:right;text-shadow:0 0 10px var(--cyan);}}
.toggle-wrap{{display:flex;align-items:center;gap:10px;cursor:pointer;}}
.toggle-switch{{position:relative;width:42px;height:24px;flex-shrink:0;}}
.toggle-switch input{{opacity:0;width:0;height:0;position:absolute;}}
.toggle-track{{position:absolute;inset:0;background:var(--border2);border-radius:12px;transition:all 0.3s ease;cursor:pointer;border:1px solid var(--border);}}
.toggle-track::after{{content:'';position:absolute;left:4px;top:4px;width:14px;height:14px;background:var(--text-muted);border-radius:50%;transition:all 0.3s ease;}}
.toggle-switch input:checked+.toggle-track{{background:var(--cyan);border-color:var(--cyan);box-shadow:0 0 14px #00c8ff44;}}
.toggle-switch input:checked+.toggle-track::after{{transform:translateX(18px);background:white;}}
.toggle-lbl{{font-size:13px;color:var(--text-dim);font-weight:600;}}

/* TABS */
.tabs{{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:4px;margin-bottom:18px;gap:3px;animation:fadeDown 0.6s 0.6s ease both;}}
.tab{{flex:1;text-align:center;padding:10px 6px;cursor:pointer;font-size:13px;font-weight:700;color:var(--text-muted);border-radius:10px;transition:all 0.25s ease;white-space:nowrap;}}
.tab:hover{{color:var(--text-dim);background:var(--surface2);}}
.tab.active{{color:#000;background:linear-gradient(135deg,#00c8ff,#0099dd);box-shadow:0 2px 14px #00c8ff44;}}
.tab.active-arb{{color:#000;background:linear-gradient(135deg,#bf5af2,#8b3dbb);box-shadow:0 2px 14px #bf5af244;}}
.tab.active-analytics{{color:#000;background:linear-gradient(135deg,#ffd700,#ff9500);box-shadow:0 2px 14px #ffd70044;}}
.tab-badge{{display:inline-block;background:rgba(0,0,0,0.22);border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px;font-weight:800;}}
.tab-content{{display:none;}}
.tab-content.active{{display:block;}}

/* CARDS */
.card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;margin-bottom:16px;overflow:hidden;position:relative;animation:cardIn 0.45s ease both;transition:border-color 0.25s ease,transform 0.2s ease,box-shadow 0.2s ease;}}
.card:hover{{border-color:var(--border2);transform:translateY(-3px);box-shadow:0 10px 40px rgba(0,0,0,0.5),0 0 0 1px var(--border2);}}
@keyframes cardIn{{from{{opacity:0;transform:translateY(18px);}}to{{opacity:1;transform:translateY(0);}}}}
.card-rank-stripe{{height:3px;width:100%;}}
.rank-gold{{background:linear-gradient(90deg,#ffd700,#ff9500,#ffd700);background-size:200%;animation:shimmer 2s linear infinite;}}
.rank-silver{{background:linear-gradient(90deg,#94a3b8,#e2e8f0,#94a3b8);background-size:200%;animation:shimmer 2.5s linear infinite;}}
.rank-bronze{{background:linear-gradient(90deg,#b56b27,#e8a870,#b56b27);background-size:200%;animation:shimmer 3s linear infinite;}}
.arb-stripe{{background:linear-gradient(90deg,#bf5af2,#00c8ff,#bf5af2);background-size:200%;animation:shimmer 2.5s linear infinite;}}
@keyframes shimmer{{0%{{background-position:-200% center;}}100%{{background-position:200% center;}}}}
.card-header{{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;gap:12px;}}
.card-header-left{{display:flex;align-items:flex-start;gap:10px;flex:1;min-width:0;}}
.sport-icon{{font-size:24px;flex-shrink:0;margin-top:1px;}}
.match-name{{font-size:15px;font-weight:700;color:var(--text);line-height:1.3;overflow-wrap:break-word;}}
.match-meta{{font-size:10px;color:var(--text-muted);margin-top:3px;font-family:'Space Mono',monospace;letter-spacing:0.5px;}}
.ev-badge{{font-family:'Space Mono',monospace;font-size:22px;font-weight:700;flex-shrink:0;border:1px solid currentColor;border-radius:10px;padding:6px 12px;text-align:center;min-width:82px;background:rgba(0,0,0,0.3);line-height:1;}}
.arb-badge{{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0;}}
.ways-tag{{font-size:10px;font-weight:800;font-family:'Space Mono',monospace;padding:2px 7px;border-radius:6px;border:1px solid;letter-spacing:1px;}}
.card-body{{padding:0 16px 16px;}}
.line-pill{{display:inline-block;background:linear-gradient(135deg,#00c8ff10,#00ff8810);border:1px solid #00c8ff30;color:var(--cyan);font-family:'Space Mono',monospace;font-size:11px;padding:4px 11px;border-radius:20px;letter-spacing:1px;margin-bottom:12px;}}
.arb-line-pill{{background:linear-gradient(135deg,#bf5af210,#00c8ff10);border-color:#bf5af230;color:var(--purple);}}
.bet-row{{display:flex;align-items:center;justify-content:space-between;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:10px;gap:8px;flex-wrap:wrap;}}
.bet-info{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}}
.bet-label{{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:1.5px;color:var(--text-muted);}}
.bet-selection{{font-size:17px;font-weight:800;color:var(--text);letter-spacing:0.5px;}}
.bet-point{{font-size:13px;color:var(--text-dim);font-family:'Space Mono',monospace;}}
.bet-odds{{font-size:15px;font-weight:700;color:var(--gold);text-shadow:0 0 10px #ffd70066;}}
.bookie-tag{{background:linear-gradient(135deg,#ffd70010,#ff950010);border:1px solid #ffd70030;color:var(--gold);font-size:11px;font-weight:700;padding:4px 10px;border-radius:8px;white-space:nowrap;}}
.stake-row{{display:flex;gap:10px;margin-bottom:10px;}}
.stake-block,.true-odds-block{{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}}
.stake-label{{font-family:'Space Mono',monospace;font-size:8px;letter-spacing:1.5px;color:var(--text-muted);text-transform:uppercase;display:block;margin-bottom:3px;}}
.stake-amount{{font-size:24px;font-weight:800;color:var(--gold);text-shadow:0 0 16px #ffd70055;font-family:'Space Mono',monospace;transition:all 0.25s ease;display:block;}}
.true-val{{font-size:20px;font-weight:700;color:var(--text-dim);font-family:'Space Mono',monospace;display:block;}}
.conf-section{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;}}
.conf-bar-wrap{{display:flex;align-items:center;gap:7px;flex:1;min-width:150px;}}
.conf-label{{font-family:'Space Mono',monospace;font-size:10px;font-weight:700;letter-spacing:1px;min-width:32px;}}
.conf-bar-track{{flex:1;height:6px;background:var(--border2);border-radius:3px;overflow:hidden;}}
.conf-bar-fill{{height:100%;border-radius:3px;animation:barFill 0.9s ease both;}}
@keyframes barFill{{from{{width:0!important;}}}}
.conf-num{{font-family:'Space Mono',monospace;font-size:11px;font-weight:700;min-width:24px;text-align:right;}}
.clv-badge{{font-family:'Space Mono',monospace;font-size:11px;font-weight:700;padding:3px 9px;border-radius:8px;border:1px solid;white-space:nowrap;}}
.breakdown-wrap{{margin-top:10px;border:1px solid var(--border);border-radius:10px;overflow:hidden;}}
.breakdown-table{{width:100%;border-collapse:collapse;font-size:12px;}}
.breakdown-table th{{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:1px;color:var(--text-muted);padding:8px 12px;text-align:left;background:var(--surface2);text-transform:uppercase;}}
.breakdown-table td{{padding:7px 12px;border-top:1px solid var(--border);color:var(--text-dim);}}
.breakdown-table tr:hover td{{background:var(--surface2);}}
.breakdown-best td{{color:var(--cyan)!important;font-weight:700;}}
.legs-table{{background:var(--surface2);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:10px;}}
.leg-row{{display:grid;grid-template-columns:1fr auto auto auto;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);}}
.leg-row:last-child{{border-bottom:none;}}
.leg-side{{font-size:14px;font-weight:800;color:var(--text);letter-spacing:0.5px;}}
.leg-odds{{font-family:'Space Mono',monospace;font-size:13px;font-weight:700;color:var(--gold);text-shadow:0 0 8px #ffd70055;}}
.leg-stake{{font-family:'Space Mono',monospace;font-size:15px;font-weight:700;color:var(--cyan);text-shadow:0 0 8px #00c8ff55;transition:all 0.25s ease;}}
.leg-book{{font-size:11px;color:var(--text-muted);background:var(--surface);border:1px solid var(--border2);padding:3px 8px;border-radius:6px;white-space:nowrap;}}
.profit-row{{display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#00ff8810,#00c8ff08);border:1px solid #00ff8830;border-radius:10px;padding:12px 16px;}}
.profit-label{{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:1.5px;color:var(--text-muted);text-transform:uppercase;}}
.profit-val{{font-family:'Space Mono',monospace;font-size:26px;font-weight:700;color:var(--green);text-shadow:0 0 20px #00ff8866;}}
.empty-state{{text-align:center;padding:60px 20px;background:var(--surface);border:1px dashed var(--border2);border-radius:16px;animation:cardIn 0.5s ease both;}}
.empty-icon{{font-size:48px;margin-bottom:14px;opacity:0.45;animation:float 3s ease-in-out infinite;}}
@keyframes float{{0%,100%{{transform:translateY(0);}}50%{{transform:translateY(-10px);}}}}
.empty-state>div:nth-child(2){{font-size:16px;font-weight:600;color:var(--text-dim);margin-bottom:6px;}}
.empty-sub{{font-size:12px;color:var(--text-muted);font-family:'Space Mono',monospace;letter-spacing:1px;}}
.analytics-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
@media(max-width:540px){{.analytics-grid{{grid-template-columns:1fr;}}}}
.analytics-card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px;animation:cardIn 0.5s 0.1s ease both;}}
.an-title{{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--text-muted);text-transform:uppercase;margin-bottom:16px;}}
.histogram{{display:flex;flex-direction:column;gap:12px;}}
.hist-row{{display:flex;align-items:center;gap:8px;}}
.hist-label{{font-family:'Space Mono',monospace;font-size:10px;color:var(--text-muted);width:36px;flex-shrink:0;}}
.hist-track{{flex:1;height:14px;background:var(--border2);border-radius:7px;overflow:hidden;}}
.hist-bar{{height:100%;border-radius:7px;animation:barFill 1s ease both;min-width:3px;}}
.hist-count{{font-family:'Space Mono',monospace;font-size:12px;font-weight:700;color:var(--text-dim);width:20px;text-align:right;}}
.an-two-col{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}}
@media(max-width:560px){{.an-two-col{{grid-template-columns:1fr;}}}}
.an-kpi-row{{display:flex;gap:10px;overflow-x:auto;margin-bottom:16px;padding-bottom:2px;}}
.an-kpi-row::-webkit-scrollbar{{height:3px;}}
.an-kpi-card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 16px;flex:1;min-width:110px;text-align:center;animation:cardIn 0.4s ease both;transition:all 0.2s ease;}}
.an-kpi-card:hover{{border-color:var(--border2);transform:translateY(-2px);}}
.an-kpi-icon{{font-family:'Space Mono',monospace;font-size:11px;font-weight:700;margin-bottom:4px;opacity:0.8;}}
.an-kpi-val{{font-family:'Space Mono',monospace;font-size:20px;font-weight:800;line-height:1;margin-bottom:4px;}}
.an-kpi-label{{font-size:10px;color:var(--text-muted);font-family:'Space Mono',monospace;letter-spacing:0.5px;}}
.an-mini-stats{{display:flex;gap:0;margin-top:14px;border-top:1px solid var(--border);padding-top:12px;}}
.an-mini-stats>div{{flex:1;text-align:center;}}
.an-mini-stats>div+div{{border-left:1px solid var(--border);}}
.an-ms-label{{font-family:'Space Mono',monospace;font-size:8px;letter-spacing:1px;color:var(--text-muted);display:block;margin-bottom:3px;text-transform:uppercase;}}
.an-sport-row{{display:flex;align-items:center;gap:8px;margin-bottom:9px;}}
.an-sport-name{{font-size:11px;color:var(--text-dim);width:90px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.an-sport-bar-track{{flex:1;height:12px;background:var(--border2);border-radius:6px;overflow:hidden;}}
.an-sport-stats{{display:flex;gap:6px;font-family:'Space Mono',monospace;font-size:10px;min-width:70px;justify-content:flex-end;}}
.an-donut-wrap{{display:flex;align-items:center;justify-content:center;gap:20px;padding:10px 0;}}
.an-donut{{width:80px;height:80px;border-radius:50%;background:conic-gradient(#00c8ff calc(var(--two-pct,50)*1%),#bf5af2 calc(var(--two-pct,50)*1%));box-shadow:0 0 20px #00c8ff22,0 0 40px #bf5af211;position:relative;}}
.an-donut::after{{content:'';position:absolute;inset:18px;background:var(--surface);border-radius:50%;}}
.an-dl-row{{display:flex;align-items:center;gap:8px;margin-bottom:8px;}}
.an-dl-dot{{width:10px;height:10px;border-radius:3px;flex-shrink:0;}}
.an-dl-label{{font-size:12px;color:var(--text-dim);flex:1;}}
.an-dl-val{{font-family:'Space Mono',monospace;font-size:14px;font-weight:700;}}
.an-conf-bar-row{{display:flex;align-items:center;gap:8px;margin-bottom:8px;}}
.an-conf-range{{font-family:'Space Mono',monospace;font-size:9px;color:var(--text-muted);width:46px;flex-shrink:0;}}
.analytics-card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px;animation:cardIn 0.5s 0.1s ease both;}}
.telemetry{{margin-top:40px;padding:14px 20px;background:var(--surface);border:1px solid var(--border);border-radius:14px;font-family:'Space Mono',monospace;font-size:10px;color:var(--text-muted);letter-spacing:1px;display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;justify-content:center;position:relative;overflow:hidden;}}
.telemetry::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--border2),transparent);}}
.tele-item{{white-space:nowrap;}}
.tele-val{{color:var(--cyan);font-weight:700;}}
::-webkit-scrollbar{{width:5px;}}
::-webkit-scrollbar-track{{background:var(--bg);}}
::-webkit-scrollbar-thumb{{background:var(--border2);border-radius:3px;}}
@media(max-width:480px){{.header-title{{font-size:28px;}}.ev-badge{{font-size:18px;min-width:70px;}}.leg-row{{grid-template-columns:1fr auto auto;}}.leg-book{{display:none;}}.stake-amount{{font-size:20px;}}.bk-val{{font-size:14px;}}.bankroll-bar{{padding:12px 10px;}}}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-logo">Quantitative Betting Intelligence</div>
    <div class="header-title">⚡ ARB SNIPER</div>
    <div class="header-subtitle"><span class="live-dot"></span>LIVE MARKET SCANNER · IST</div>
    <div class="scan-time">Last Sweep: {current_time}</div>
  </div>

  {bankroll_html}

  <div class="btn-row">
    <button class="btn-scan" onclick="triggerScan()">⚡ Launch Cloud Scan</button>
    <button class="btn-export" onclick="exportCSV()">⬇ Export CSV</button>
  </div>

  <div class="controls-bar">
    <div class="ctrl-group">
      <span class="ctrl-label">KELLY %</span>
      <input type="range" class="kelly-slider" id="kellySlider" min="0" max="100" value="30" oninput="updateKelly(this.value)" style="--pct:30%">
      <span class="kelly-val" id="kellyValue">30%</span>
    </div>
    <label class="toggle-wrap" for="top5Toggle">
      <div class="toggle-switch">
        <input type="checkbox" id="top5Toggle" onchange="applyTop5()">
        <div class="toggle-track"></div>
      </div>
      <span class="toggle-lbl">Top 5 Only</span>
    </label>
  </div>

  <div class="tabs">
    <div class="tab active" id="tab-ev" onclick="switchTab('ev')">⚡ EV Edges <span class="tab-badge">{len(evs)}</span></div>
    <div class="tab" id="tab-arb" onclick="switchTab('arb')">🔒 Arbitrage <span class="tab-badge">{len(arbs)}</span></div>
    <div class="tab" id="tab-analytics" onclick="switchTab('analytics')">📊 Analytics</div>
  </div>

  <div id="content-ev" class="tab-content active">
    <div id="ev-cards">{ev_cards_inner}</div>
  </div>
  <div id="content-arb" class="tab-content">
    <div id="arb-cards">{arb_cards_inner}</div>
  </div>
  {analytics_html}

  <div class="telemetry">
    <span class="tele-item">🔑 KEY <span class="tele-val">#{current_key_index + 1}</span></span>
    <span class="tele-item">📡 QUOTA <span class="tele-val">{requests_remaining}/500</span></span>
    <span class="tele-item">⚡ SCAN COST <span class="tele-val">~{credits_burned} credits</span></span>
    <span class="tele-item">🏦 BANKROLL <span class="tele-val">₹{TOTAL_BANKROLL}</span></span>
  </div>
</div>

<script>
const ALL_EVS={evs_json};
const ALL_ARBS={arbs_json};

function switchTab(tab){{
  const tabMap={{ev:'active',arb:'active-arb',analytics:'active-analytics'}};
  ['ev','arb','analytics'].forEach(t=>{{
    document.getElementById('content-'+t).classList.remove('active');
    const el=document.getElementById('tab-'+t);
    el.classList.remove('active','active-arb','active-analytics');
  }});
  document.getElementById('content-'+tab).classList.add('active');
  document.getElementById('tab-'+tab).classList.add(tabMap[tab]);
}}

function triggerScan(){{
  let pat=localStorage.getItem('gh_dispatch_token');
  if(!pat){{
    pat=prompt("Enter your GitHub PAT (ghp_...) to authorize this scan:\\n(Stored only in your browser — never public)");
    if(!pat) return;
    localStorage.setItem('gh_dispatch_token',pat);
  }}
  const btn=document.querySelector('.btn-scan');
  btn.textContent='⏳ Firing engine...';
  btn.disabled=true;
  fetch('https://api.github.com/repos/nikunj7711/arb-sniper/actions/workflows/sniper.yml/dispatches',{{
    method:'POST',
    headers:{{'Accept':'application/vnd.github.v3+json','Authorization':'token '+pat,'Content-Type':'application/json'}},
    body:JSON.stringify({{ref:'main'}})
  }})
  .then(r=>{{
    btn.textContent='⚡ Launch Cloud Scan';
    btn.disabled=false;
    if(r.ok){{alert("✅ Engine fired! Scan running — wait 2–3 mins then Ctrl+F5 to refresh.");}}
    else{{alert("❌ Auth failed. Token may be expired. Clearing stored token...");localStorage.removeItem('gh_dispatch_token');}}
  }})
  .catch(()=>{{btn.textContent='⚡ Launch Cloud Scan';btn.disabled=false;}});
}}

let currentKelly=0.30;
function updateKelly(val){{
  currentKelly=val/100;
  document.getElementById('kellyValue').textContent=val+'%';
  document.getElementById('kellySlider').style.setProperty('--pct',val+'%');
  document.querySelectorAll('.stake-display').forEach(el=>{{
    const base=parseFloat(el.getAttribute('data-base-stake'));
    el.textContent='₹'+Math.round(base*(currentKelly/0.30));
  }});
}}

function applyTop5(){{
  const top5=document.getElementById('top5Toggle').checked;
  document.querySelectorAll('#ev-cards .ev-card').forEach((c,i)=>{{c.style.display=(top5&&i>=5)?'none':'';}});
  document.querySelectorAll('#arb-cards .arb-card').forEach((c,i)=>{{c.style.display=(top5&&i>=5)?'none':'';}});
}}

function exportCSV(){{
  let rows=[['Type','Match','Sport','Line','Selection','Odds','EV%/ARB%','Stake','Bookmaker','Confidence','CLV%']];
  ALL_EVS.forEach(e=>{{rows.push(['EV',e.match,e.sport,e.line,e.selection,e.odds,e.pct.toFixed(2),e.stake.toFixed(0),e.bookie,e.confidence,e.clv_pct??'']);}});
  ALL_ARBS.forEach(a=>{{rows.push(['ARB',a.match,a.sport,a.line,a.s1+'/'+a.s2,a.s1_price+'/'+a.s2_price,a.pct.toFixed(2),(a.stk1+a.stk2).toFixed(0),a.s1_bookie+'+'+a.s2_bookie,'','']);}});
  const csv=rows.map(r=>r.map(c=>'"'+String(c??'').replace(/"/g,'""')+'"').join(',')).join('\\n');
  const blob=new Blob([csv],{{type:'text/csv'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='arb_sniper_export.csv';a.click();
  URL.revokeObjectURL(url);
}}

// ── BANKROLL EDITOR ──
let activeBankroll = parseFloat(localStorage.getItem('custom_bankroll') || '{bk.get("starting_bankroll", TOTAL_BANKROLL):.0f}');
function openBkModal(){{
  document.getElementById('bk-modal-overlay').style.display='flex';
  document.getElementById('bk-input').value = activeBankroll;
  setTimeout(()=>document.getElementById('bk-input').focus(),100);
}}
function closeBkModal(){{
  document.getElementById('bk-modal-overlay').style.display='none';
}}
function saveBankroll(){{
  const val = parseFloat(document.getElementById('bk-input').value);
  if(isNaN(val)||val<100){{ alert('Please enter a valid bankroll (min ₹100)'); return; }}
  activeBankroll = val;
  localStorage.setItem('custom_bankroll', val);
  document.getElementById('bk-amount').textContent = Math.round(val).toLocaleString('en-IN');
  // Recompute all stakes based on new bankroll ratio
  const ratio = val / {bk.get("starting_bankroll", TOTAL_BANKROLL):.0f};
  document.querySelectorAll('.stake-display').forEach(el=>{{
    const base = parseFloat(el.getAttribute('data-base-stake'));
    const kelly = parseFloat(document.getElementById('kellySlider').value)/100;
    el.textContent = '₹' + Math.round(base * ratio * (kelly/0.30));
  }});
  closeBkModal();
  // Flash the bankroll display
  const disp = document.getElementById('bk-display');
  disp.style.transition='color 0.2s';
  disp.style.color='#00ff88';
  setTimeout(()=>{{disp.style.color='';disp.style.transition='';disp.style.color='var(--text)';}},800);
}}
document.addEventListener('keydown', e=>{{ if(e.key==='Escape') closeBkModal(); }});
document.getElementById('bk-modal-overlay').addEventListener('click', e=>{{ if(e.target===document.getElementById('bk-modal-overlay')) closeBkModal(); }});
// Init bankroll from localStorage
(function(){{
  const stored = localStorage.getItem('custom_bankroll');
  if(stored){{
    activeBankroll = parseFloat(stored);
    document.getElementById('bk-amount').textContent = Math.round(activeBankroll).toLocaleString('en-IN');
  }}
}})();

// ── CONFIDENCE CHART (analytics tab) ──
(function buildConfChart(){{
  const container = document.getElementById('an-conf-chart');
  if(!container) return;
  const bands = [{{label:'0–29',min:0,max:29,color:'#ff4d6d'}},{{label:'30–59',min:30,max:59,color:'#ffd700'}},{{label:'60–79',min:60,max:79,color:'#00c8ff'}},{{label:'80–100',min:80,max:100,color:'#00ff88'}}];
  const counts = bands.map(b=>ALL_EVS.filter(e=>e.confidence>=b.min&&e.confidence<=b.max).length);
  const maxC = Math.max(...counts,1);
  let html='';
  bands.forEach((b,i)=>{{
    const w = Math.round(counts[i]/maxC*100);
    html+=`<div class="an-conf-bar-row">
      <span class="an-conf-range">${{b.label}}</span>
      <div class="an-sport-bar-track"><div style="width:${{w}}%;background:${{b.color}};height:100%;border-radius:4px;animation:barFill 0.8s ${{i*0.1}}s ease both;box-shadow:0 0 8px ${{b.color}}55;"></div></div>
      <span style="font-family:'Space Mono',monospace;font-size:11px;color:${{b.color}};min-width:20px;text-align:right;">${{counts[i]}}</span>
    </div>`;
  }});
  const total = counts.reduce((a,b)=>a+b,0);
  const highConf = counts[2]+counts[3];
  const highPct = total>0?Math.round(highConf/total*100):0;
  html += `<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:12px;display:flex;gap:16px;">
    <div style="flex:1;text-align:center;"><div style="font-family:'Space Mono',monospace;font-size:8px;color:#64748b;margin-bottom:3px;letter-spacing:1px;">HIGH CONF</div><div style="font-family:'Space Mono',monospace;font-size:18px;font-weight:700;color:#00ff88">${{highPct}}%</div></div>
    <div style="flex:1;text-align:center;"><div style="font-family:'Space Mono',monospace;font-size:8px;color:#64748b;margin-bottom:3px;letter-spacing:1px;">SCORED</div><div style="font-family:'Space Mono',monospace;font-size:18px;font-weight:700;color:#00c8ff">${{total}}</div></div>
  </div>`;
  container.innerHTML = html;
}})();
</script>
</body>
</html>'''

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(" Web Dashboard successfully updated (index.html)")


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
                third_leg = f"\n \u20b9{arb.get('stk3',0):.0f} on {arb['s3'].upper()} @ {arb['s3_data']['price']:.2f} [{display_bookie(arb['s3_data']['bookie'])}]"
            msg = (f"\U0001f512 {arb['pct']:.2f}% ARB | {arb['match']}\n {arb['sport'].replace('_',' ').title()}\n"
                   f" {arb['time']}\n {arb['line']}\n\n"
                   f" \u20b9{arb['stk1']:.0f} on {arb['s1'].upper()} @ {arb['s1_data']['price']:.2f} [{display_bookie(arb['s1_data']['bookie'])}]\n"
                   f" \u20b9{arb['stk2']:.0f} on {arb['s2'].upper()} @ {arb['s2_data']['price']:.2f} [{display_bookie(arb['s2_data']['bookie'])}]{third_leg}\n\n"
                   f" Profit: \u20b9{arb['profit']:.0f}")
            send_phone_alert(msg, arb['pct'], arb['match'], "ARB")
            mark_alert_sent(alert_cache, arb['match'], arb['line'], sel, odds)

    if all_evs:
        all_evs.sort(key=lambda x: x['pct'], reverse=True)
        for ev in all_evs:
            if is_duplicate_alert(alert_cache, ev['match'], ev['line'], ev['selection'], ev['odds']):
                continue
            clv_line = f"\n CLV: {ev['clv_pct']:+.2f}%" if ev.get('clv_pct') is not None else ''
            msg = (f"\u26a1 {ev['pct']:.2f}% EV | {ev['match']}\n {ev['sport'].replace('_',' ').title()}\n"
                   f" {ev['time']}\n {ev['line']}\n\n"
                   f" BET EXACTLY: \u20b9{ev['stake']:.0f}\n"
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
