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

TARGET_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_uefa_champs_league',
    'basketball_nba', 'icehockey_nhl', 'soccer_italy_serie_a',
    'upcoming' 
]

BOOK_CAPS = {
    'betway': 300, 'stake': 500, 'onexbet': 400, 'marathonbet': 400,  
    'pinnacle': 1000, 'bet365': 400, 'unibet': 350, 'bcgame': 1000 
}

# ==========================================
#  2. KEY ROTATION & MEMORY
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
            print(f"🔄 Key #{failed_idx + 1} Exhausted. Rotating to Key #{api_state['active_index'] + 1}")
            save_json('api_state.json', api_state)
        return api_state['active_index'] < len(API_KEYS)

def update_key_telemetry(idx, rem):
    with api_lock:
        if str(idx) not in api_state['stats']: api_state['stats'][str(idx)] = {}
        api_state['stats'][str(idx)]['remaining'] = int(rem)

# ==========================================
#  4. DATA FETCHERS
# ==========================================
def fetch_bcgame_custom():
    headers = {'accept': '*/*', 'origin': 'https://bc.game', 'user-agent': 'Mozilla/5.0'}
    print("🥷 Fetching Zero-Cost BC.Game Feed...")
    try:
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        version = requests.get(map_url, headers=headers, timeout=10).json()['top_events_versions'][0]
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        std_events = []
        for match in events.values():
            desc, mks = match.get('desc', {}), match.get('markets', {})
            comps = desc.get('competitors', [])
            if len(comps) < 2: continue
            
            h2h = []
            m1 = mks.get("1", {}).get("", {})
            if m1:
                if "1" in m1: h2h.append({'name': comps[0]['name'], 'price': float(m1["1"]["k"])})
                if "2" in m1: h2h.append({'name': comps[1]['name'], 'price': float(m1["2"]["k"])})
                if "3" in m1: h2h.append({'name': 'Draw', 'price': float(m1["3"]["k"])})
            
            totals = []
            m18 = mks.get("18", {})
            for pk, pd in m18.items():
                val = pk.replace("total=", "")
                if "12" in pd: totals.append({'name': 'Over', 'price': float(pd["12"]["k"]), 'point': float(val)})
                if "13" in pd: totals.append({'name': 'Under', 'price': float(pd["13"]["k"]), 'point': float(val)})
            
            if h2h or totals:
                std_events.append({
                    'home_team': comps[0]['name'], 'away_team': comps[1]['name'],
                    'commence_time': datetime.fromtimestamp(desc.get('scheduled', 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    'bookmakers': [{'key': 'bcgame', 'title': 'BC.Game', 'markets': [{'key': 'h2h', 'outcomes': h2h}, {'key': 'totals', 'outcomes': totals}]}]
                })
        return std_events
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
            if res.status_code == 200:
                rem = res.headers.get('x-requests-remaining')
                if rem: update_key_telemetry(idx, rem)
                return res.json()
            if res.status_code in [401, 429]:
                if rotate_api_key(idx): continue
            return None
        except: return None

def fetch_all_sports():
    results = {}
    print("📡 Fetching Odds-API Master Data...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_odds_with_retry, f'https://api.the-odds-api.com/v4/sports/{sp}/odds', {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h,totals'}): sp for sp in TARGET_SPORTS}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results

# ==========================================
#  5. DATA PROCESSING
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

def process_markets(results):
    all_evs, all_arbs = [], []
    for sport, events in results.items():
        if not events: continue
        for event in events:
            home, away = event.get('home_team', 'A'), event.get('away_team', 'B')
            match_name = f"{home} vs {away}"
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

            for lk, d in ev_lines.items():
                if len(d['pin']) in [2, 3]:
                    keys = list(d['pin'].keys())
                    true_odds_vals = remove_vig(*[d['pin'][k] for k in keys])
                    for idx, side in enumerate(keys):
                        true_odds = true_odds_vals[idx]
                        if side in d['softs']:
                            best_bk = max(d['softs'][side], key=d['softs'][side].get)
                            best_p = d['softs'][side][best_bk]
                            if best_p > true_odds:
                                ev_pct = ((best_p / true_odds) - 1) * 100
                                if ev_pct >= MIN_EV_THRESHOLD:
                                    all_evs.append({'pct': ev_pct, 'match': match_name, 'sport': sport.upper(), 'line': lk, 'sel': side, 'odds': best_p, 'trueO': true_odds, 'bk': best_bk, 'stk': calculate_kelly(best_p, true_odds, TOTAL_BANKROLL, best_bk)})

            for lk, outs in arb_lines.items():
                keys = list(outs.keys())
                if len(keys) in [2, 3]:
                    margin = sum(1/outs[k]['price'] for k in keys)
                    if margin < 1.0 and (1-margin)*100 >= MIN_ARB_THRESHOLD:
                        arb = {'pct': (1-margin)*100, 'match': match_name, 'sport': sport.upper(), 'line': lk, 'profit': (TOTAL_BANKROLL/margin)-TOTAL_BANKROLL, 'sides': []}
                        for k in keys: arb['sides'].append({'sel': k, 'pr': outs[k]['price'], 'bk': outs[k]['bookie'], 'stk': (TOTAL_BANKROLL/margin)/outs[k]['price']})
                        all_arbs.append(arb)
    return all_evs, all_arbs

# ==========================================
#  6. WEBSITE GENERATOR (RESTORED BEAUTY)
# ==========================================
def generate_web(evs, arbs):
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    build_time = ist_now.strftime('%d %b, %I:%M %p IST')
    
    def build_legs(a):
        html = ""
        for s in a['sides']:
            html += f"<div style='display:flex; justify-content:space-between; background:#09090b; padding:8px; border-radius:6px; margin-bottom:4px;'><span>{s['sel']} @ <b>{s['pr']}</b> ({s['bk'].upper()})</span> <span>₹{s['stk']:.0f}</span></div>"
        return html

    arb_html = ""
    for a in arbs:
        arb_html += f"<div class='card'><div style='display:flex; justify-content:space-between; margin-bottom:8px;'><span class='badge arb-badge'>{a['pct']:.2f}% ARB</span> <button class='copy-btn' onclick='navigator.clipboard.writeText(\"{a['match']}\")'>COPY NAME</button></div><b>{a['match']}</b><br><small style='color:#a1a1aa;'>{a['line']}</small><br><br>{build_legs(a)}<div style='text-align:right; margin-top:8px; color:#10b981; font-weight:bold;'>Profit: ₹{a['profit']:.0f}</div></div>"

    ev_html = ""
    for e in evs[:30]:
        ev_html += f"<div class='card'><div style='display:flex; justify-content:space-between; margin-bottom:8px;'><span class='badge ev-badge'>{e['pct']:.2f}% EV</span> <button class='copy-btn' onclick='navigator.clipboard.writeText(\"{e['match']}\")'>COPY NAME</button></div><b>{e['match']}</b><br><small style='color:#a1a1aa;'>{e['line']} | {e['bk'].upper()} @ {e['odds']}</small><br><br><div style='display:flex; justify-content:space-between;'><span>Stake: <b>₹{e['stk']:.0f}</b></span> <span style='color:#a1a1aa; font-size:12px;'>True: {e['trueO']:.3f}</span></div></div>"

    HTML = f"""<!DOCTYPE html>
    <html><head><meta name='viewport' content='width=device-width, initial-scale=1'>
    <style>
        body {{ background: #09090b; color: #fff; font-family: sans-serif; padding: 15px; max-width: 600px; margin: auto; }}
        .card {{ background: #18181b; border: 1px solid #27272a; padding: 15px; border-radius: 12px; margin-bottom: 12px; }}
        .copy-btn {{ background: #3f3f46; color: #fff; border: none; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 11px; font-weight: bold; }}
        .badge {{ font-size: 11px; padding: 3px 8px; border-radius: 5px; font-weight: bold; }}
        .arb-badge {{ background: #f59e0b; color: #000; }}
        .ev-badge {{ background: #06b6d4; color: #000; }}
        h2, h3 {{ margin-bottom: 15px; }}
    </style></head><body>
    <h2 style='color:#06b6d4;'>⚡ ARB SNIPER</h2>
    <p style='font-size:12px; color:#a1a1aa; margin-bottom:20px;'>Last Sync: {build_time}</p>
    
    <h3 style='border-left: 4px solid #f59e0b; padding-left: 10px;'>🔒 ARBITRAGE ({len(arbs)})</h3>
    {arb_html if arbs else "<p style='color:#3f3f46;'>No Arbitrage found.</p>"}

    <h3 style='border-left: 4px solid #06b6d4; padding-left: 10px; margin-top:30px;'>💎 VALUE BETS ({len(evs)})</h3>
    {ev_html if evs else "<p style='color:#3f3f46;'>No Value bets found.</p>"}
    </body></html>"""
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(HTML)

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
            # Clean names AGGRESSIVELY: Remove (Holis), (E), FC, Real, etc.
            def clean(n):
                for junk in ['(holis)', '(e)', 'fc ', ' fc', 'real ', ' real', ' vs ', 'as ']:
                    n = n.lower().replace(junk, '')
                return n.strip()
            
            bc_h, bc_a = clean(bc['home_team']), clean(bc['away_team'])
            linked = False
            for events in results.values():
                if not events: continue
                for ev in events:
                    api_h, api_a = clean(ev.get('home_team', '')), clean(ev.get('away_team', ''))
                    
                    # Logic: If names match directly OR share a unique long word (like "Atalanta")
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
