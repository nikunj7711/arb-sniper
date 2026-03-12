import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  1. CONFIGURATION (YOUR RULES)
# ==========================================
# Hardcoded for online compiler; use os.getenv('ODDS_API_KEYS') for GitHub
_raw_keys = '41308dc8cb155421b36bf4e58a0fe50b'
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' 
TOTAL_BANKROLL = 1500                   
MIN_EV_THRESHOLD = 1.5                  
MIN_ARB_THRESHOLD = 0.5                 

MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'

# Enhanced list to ensure BC.Game matches have counterparts
TARGET_SPORTS = [
    'soccer_epl', 
    'soccer_spain_la_liga', 
    'soccer_uefa_champs_league',
    'basketball_nba', 
    'icehockey_nhl',
    'soccer_italy_serie_a',
    'upcoming'  # Pulls the next 8 matches from EVERY sport to maximize merge potential
]

BOOK_CAPS = {
    'betway': 300, 'stake': 500, 'onexbet': 400, 'marathonbet': 400,  
    'pinnacle': 1000, 'bet365': 400, 'unibet': 350, 'bcgame': 1000 
}

# ==========================================
#  2. MEMORY & AUTO-RECOVERY
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

api_state = load_json('api_state.json', {})
if 'active_index' not in api_state: api_state['active_index'] = 0
if 'stats' not in api_state: api_state['stats'] = {}

if len(API_KEYS) > 0 and api_state['active_index'] >= len(API_KEYS):
    api_state['active_index'] = 0
    api_state['stats'] = {}

alert_cache = load_json('alert_cache.json', {})
now_ts = time.time()
alert_cache = {k: v for k, v in alert_cache.items() if isinstance(v, (int, float)) and (now_ts - v < 6*3600)}

def is_duplicate_alert(alert_key):
    if alert_key in alert_cache: return True
    alert_cache[alert_key] = time.time()
    return False

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

def update_key_telemetry(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  3. THE MATH ENGINE
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
#  4. CLOUD FETCHING (THE HUNTERS)
# ==========================================
def fetch_bcgame_custom():
    headers = {
        'accept': '*/*',
        'origin': 'https://bc.game',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    print("🥷 Injecting custom BC.Game Zero-Cost Pipeline...")
    try:
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        version = requests.get(map_url, headers=headers, timeout=10).json()['top_events_versions'][0]
        
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        standardized_events = []
        for event_id, match in events.items():
            desc = match.get('desc', {})
            markets = match.get('markets', {})
            
            comps = desc.get('competitors', [])
            if len(comps) < 2: continue
            home_team = comps[0].get('name')
            away_team = comps[1].get('name')
            
            commence_time = datetime.fromtimestamp(desc.get('scheduled', 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            translated_markets = []
            
            # Winner Market
            h2h_outcomes = []
            market_1 = markets.get("1", {}).get("", {})
            if market_1:
                if "1" in market_1: h2h_outcomes.append({'name': home_team, 'price': float(market_1["1"]["k"])})
                if "2" in market_1: h2h_outcomes.append({'name': away_team, 'price': float(market_1["2"]["k"])})
                if "3" in market_1: h2h_outcomes.append({'name': 'Draw', 'price': float(market_1["3"]["k"])})
            if h2h_outcomes:
                translated_markets.append({'key': 'h2h', 'outcomes': h2h_outcomes})

            # Over/Under Market
            market_18 = markets.get("18", {})
            total_outcomes = []
            for point_key, point_data in market_18.items():
                point_val = point_key.replace("total=", "")
                if "12" in point_data: total_outcomes.append({'name': 'Over', 'price': float(point_data["12"]["k"]), 'point': float(point_val)})
                if "13" in point_data: total_outcomes.append({'name': 'Under', 'price': float(point_data["13"]["k"]), 'point': float(point_val)})
            if total_outcomes:
                translated_markets.append({'key': 'totals', 'outcomes': total_outcomes})
            
            if translated_markets:
                standardized_events.append({
                    'home_team': home_team,
                    'away_team': away_team,
                    'commence_time': commence_time,
                    'bookmakers': [{'key': 'bcgame', 'title': 'BC.Game', 'markets': translated_markets}]
                })
        return standardized_events
    except Exception as e:
        print(f"⚠️ BC.Game Scraper Error: {e}")
        return []

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

        if res.status_code == 200:
            return res.json()
        elif res.status_code in [401, 429]:
            print(f"⚠️ Odds-API Quota Error {res.status_code}. Key Exhausted.")
            if rotate_api_key(idx): continue
            else: return None
        return None

def fetch_all_sports():
    results = {}
    print("📡 Fetching Odds-API Master Data...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(
            fetch_odds_with_retry, 
            f'https://api.the-odds-api.com/v4/sports/{sp}/odds', 
            {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals,spreads'} 
        ): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            sp = futures[future]
            data = future.result()
            results[sp] = data
            if data: print(f"  ✅ {sp.upper()}: {len(data)} matches loaded.")
            else: print(f"  ❌ {sp.upper()}: FAILED.")
    return results

def format_ist_time(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%d %b, %I:%M %p")
    except: return "Unknown Time"

# ==========================================
#  5. DATA PROCESSING
# ==========================================
def process_markets(results):
    all_evs, all_arbs = [], []
    now_utc = datetime.now(timezone.utc)
    
    for sport, events in results.items():
        if not events: continue
        for event in events:
            is_live = False
            try:
                dt_commence = datetime.strptime(event['commence_time'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if dt_commence < now_utc: is_live = True
            except: pass
            
            home, away = event.get('home_team', 'A'), event.get('away_team', 'B')
            match_name, match_time = f"{home} vs {away}", format_ist_time(event['commence_time'])
            ev_lines, arb_lines = {}, {}
            
            for bookie in event.get('bookmakers', []):
                b_name = bookie['key']
                for market in bookie.get('markets', []):
                    m_type = market['key'].upper()
                    for outcome in market['outcomes']:
                        name, price = outcome['name'], outcome['price']
                        point = str(outcome.get('point', ''))
                        line_key = f"{m_type} {point}".strip()

                        if line_key not in ev_lines: ev_lines[line_key] = {'pin': {}, 'softs': {}}
                        if line_key not in arb_lines: arb_lines[line_key] = {}
                        
                        if b_name == 'pinnacle': ev_lines[line_key]['pin'][name] = price
                        else:
                            if name not in ev_lines[line_key]['softs']: ev_lines[line_key]['softs'][name] = {}
                            ev_lines[line_key]['softs'][name][b_name] = price
                                
                        if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                            arb_lines[line_key][name] = {'price': price, 'bookie': b_name}

            # EV & ARB Math Logic
            for lk, d in ev_lines.items():
                pinny, softs = d['pin'], d['softs']
                if len(pinny) in [2, 3]: 
                    keys = list(pinny.keys())
                    true_odds_vals = remove_vig(*[pinny[k] for k in keys])
                    for idx, side in enumerate(keys):
                        true_odds = true_odds_vals[idx]
                        if side in softs:
                            best_bk = max(softs[side], key=softs[side].get)
                            best_p = softs[side][best_bk]
                            if best_p > true_odds:
                                ev_pct = ((best_p / true_odds) - 1) * 100
                                if ev_pct >= MIN_EV_THRESHOLD:
                                    all_evs.append({'pct': ev_pct, 'match': match_name, 'home': home, 'away': away, 'time': match_time, 'sport': sport.upper(), 'line': lk, 'ways': len(pinny), 'sel': side, 'odds': best_p, 'trueO': true_odds, 'bk': best_bk, 'stk': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk), 'conf': 100, 'is_live': is_live})

            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                if len(keys) in [2, 3]:
                    margin = sum(1/outs[k]['price'] for k in keys)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {'pct': (1-margin)*100, 'match': match_name, 'home': home, 'away': away, 'time': match_time, 'sport': sport.upper(), 'line': lk, 'ways': len(keys), 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': [], 'is_live': is_live}
                        for k in keys: arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': (TOTAL_BANKROLL/margin)/outs[k]['price']})
                        all_arbs.append(arb)

    return all_evs, all_arbs

# ==========================================
#  6. WEBSITE GENERATOR
# ==========================================
def generate_web(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    build_time = ist_now.strftime('%d %b %Y, %I:%M %p IST')
    
    def build_ev_card(e):
        return f"<div style='background:#18181b; border:1px solid #27272a; padding:15px; border-radius:12px; margin-bottom:10px;'><b>{e['pct']:.2f}% EV</b> | {e['match']}<br><small>{e['line']} - {e['bk'].upper()} @ {e['odds']}</small></div>"
    
    def build_arb_card(a):
        return f"<div style='background:#18181b; border:1px solid #27272a; padding:15px; border-radius:12px; margin-bottom:10px; border-left:4px solid #f59e0b;'><b>{a['pct']:.2f}% ARB</b> | {a['match']}<br><small>Profit: ₹{a['profit']:.0f}</small></div>"

    HTML = f"<html><body style='background:#09090b; color:#fff; font-family:sans-serif; padding:20px;'><h1>⚡ ARB SNIPER DASHBOARD</h1><p>Last Sync: {build_time}</p><h2>🔒 Arbitrage Locks</h2>{''.join([build_arb_card(a) for a in arbs])}<h2>💎 Value Bets</h2>{''.join([build_ev_card(e) for e in evs[:20]])}</body></html>"
    with open("index.html", "w", encoding="utf-8") as f: f.write(HTML)

# ==========================================
#  7. THE TRIGGER (MAIN)
# ==========================================
if __name__ == "__main__":
    print(f"🚀 Engine Booting... {len(API_KEYS)} Keys Loaded.")
    
    results = fetch_all_sports()
    bc_data = fetch_bcgame_custom()
    
    if bc_data:
        print(f"\n🔄 Merging {len(bc_data)} BC.Game matches...")
        merge_count = 0
        for bc_match in bc_data:
            # Clean names for better matching
            bc_h = bc_match['home_team'].lower().replace('fc ', '').replace(' fc', '').strip()
            bc_a = bc_match['away_team'].lower().replace('fc ', '').replace(' fc', '').strip()
            
            found = False
            for sport, events in results.items():
                if not events: continue
                for event in events:
                    api_h = event.get('home_team', '').lower().replace('fc ', '').replace(' fc', '').strip()
                    api_a = event.get('away_team', '').lower().replace('fc ', '').replace(' fc', '').strip()
                    
                    # Token-word matching logic
                    h_match = any(w in api_h for w in bc_h.split() if len(w) > 3) or bc_h[:5] == api_h[:5]
                    a_match = any(w in api_a for w in bc_a.split() if len(w) > 3) or bc_a[:5] == api_a[:5]
                    
                    if h_match and a_match:
                        if 'bookmakers' not in event: event['bookmakers'] = []
                        event['bookmakers'].append(bc_match['bookmakers'][0])
                        merge_count += 1
                        print(f"  🔗 LINKED: {bc_match['home_team']} <-> {event['home_team']}")
                        found = True; break
                if found: break
        print(f"✅ Injected {merge_count} BC.Game matches!\n")

    evs, arbs = process_markets(results)
    generate_web(evs, arbs)
    print(f"✅ Sync Complete. EV: {len(evs)} | ARB: {len(arbs)}")
