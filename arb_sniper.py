#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v4.0 — ELITE QUANT EDITION                     ║
║  19-Key Rotation | BC.Game Deep Parser | Full Arb Engine | Pro Dashboard     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, math, time, hashlib, requests, logging, itertools, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

# ─── Constants ─────────────────────────────────────────────────────────────────
ODDS_BASE      = "https://api.the-odds-api.com/v4"
BCGAME_URL     = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL       = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE     = "api_state.json"
OUTPUT_HTML    = "index.html"
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "arb2026")

KELLY_FRACTION = 0.30
MIN_ARB_PROFIT = 0.001   # 0.1%
MIN_EV_EDGE    = 0.005   # 0.5%
BANK_SIZE      = 10000   # default bank in Rs

ALLOWED_BOOKS = {
    "pinnacle","onexbet","bet365","unibet","betway","stake",
    "marathonbet","parimatch","betfair","dafabet","bovada",
    "draftkings","fanduel","betmgm"
}

SPORTS_LIST = [
    "soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga","soccer_italy_serie_a",
    "soccer_france_ligue_one","soccer_uefa_champs_league","soccer_uefa_europa_league",
    "soccer_brazil_campeonato","soccer_argentina_primera_division","soccer_turkey_super_league",
    "basketball_nba","basketball_euroleague","icehockey_nhl","tennis_atp_french_open",
    "tennis_wta_french_open","mma_mixed_martial_arts","americanfootball_nfl",
    "cricket_ipl","cricket_international_championship","boxing_boxing","baseball_mlb",
    "aussierules_afl","rugby_union_world_cup","golf_masters_tournament_winner"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "eu,uk,us,au"


# ═══════════════════════════════════════════════════════════════════════════════
# KEY ROTATION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class KeyRotator:
    def __init__(self):
        raw = os.environ.get("ODDS_API_KEYS", "")
        self.keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self.keys:
            log.warning("ODDS_API_KEYS env var not set or empty!")
        self._lock  = threading.Lock()
        self._quota = {k: 500 for k in self.keys}
        log.info(f"KeyRotator: {len(self.keys)} keys loaded.")

    def get(self) -> str:
        """Return the key with the highest remaining quota."""
        with self._lock:
            if not self.keys:
                return "MISSING_KEY"
            return max(self.keys, key=lambda k: self._quota.get(k, 0))

    def update(self, key: str, remaining: int, used: int):
        with self._lock:
            self._quota[key] = remaining

    def mark_exhausted(self, key: str):
        with self._lock:
            self._quota[key] = 0
            log.warning(f"Key ...{key[-6:]} exhausted/invalid.")

    def total_remaining(self) -> int:
        with self._lock:
            return max(0, sum(self._quota.values()))

    def status(self) -> list:
        with self._lock:
            return [
                {"key": f"{k[:4]}...{k[-4:]}", "remaining": self._quota.get(k, 0)}
                for k in self.keys
            ]

ROTATOR = KeyRotator()


# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    defaults = {
        "remaining_requests": 500, "used_today": 0,
        "last_reset": str(datetime.now(timezone.utc).date()),
        "total_events_scanned": 0, "last_arb_count": 0, "last_ev_count": 0
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# ODDS API — CONCURRENT FETCH WITH KEY ROTATION
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_sport_odds(sport: str, market: str) -> list:
    key = ROTATOR.get()
    if key == "MISSING_KEY" or ROTATOR._quota.get(key, 0) <= 3:
        return []

    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": key, "regions": REGIONS, "markets": market,
        "oddsFormat": "decimal", "dateFormat": "iso"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        remaining = int(r.headers.get("X-Requests-Remaining", ROTATOR._quota.get(key, 0)))
        used      = int(r.headers.get("X-Requests-Used", 0))
        ROTATOR.update(key, remaining, used)

        if r.status_code == 422:
            return []
        if r.status_code in (429, 401):
            ROTATOR.mark_exhausted(key)
            return []
        if r.status_code != 200:
            return []

        data = r.json()
        if not isinstance(data, list):
            return []

        filtered = []
        for ev in data:
            bms = [b for b in ev.get("bookmakers", []) if b.get("key") in ALLOWED_BOOKS]
            if bms:
                ev["bookmakers"] = bms
                filtered.append(ev)

        log.info(f"  {sport}/{market}: {len(filtered)} events | key ...{key[-6:]} -> {remaining} left")
        return filtered

    except requests.exceptions.Timeout:
        log.warning(f"Timeout: {sport}/{market}")
        return []
    except Exception as e:
        log.error(f"Error {sport}/{market}: {e}")
        return []

def fetch_all_odds(state: dict) -> list:
    if not ROTATOR.keys:
        log.error("No API keys. Set ODDS_API_KEYS secret.")
        return []
    if ROTATOR.total_remaining() <= 0:
        log.error("All keys exhausted.")
        return []

    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    log.info(f"Fetching {len(tasks)} combos across {len(ROTATOR.keys)} keys...")
    all_events = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_sport_odds, s, m): (s, m) for s, m in tasks}
        for fut in as_completed(futures):
            try:
                all_events.extend(fut.result())
            except Exception as e:
                log.error(f"Future error: {e}")

    state["remaining_requests"]    = ROTATOR.total_remaining()
    state["total_events_scanned"]  = len(all_events)
    log.info(f"Total events collected: {len(all_events)}")
    return all_events


# ═══════════════════════════════════════════════════════════════════════════════
# BC.GAME SCRAPER
# ═══════════════════════════════════════════════════════════════════════════════
def _deep_find_list(obj, keys: list):
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v
        for v in obj.values():
            found = _deep_find_list(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_list(item, keys)
            if found is not None:
                return found
    return None

def _extract_bc_events(raw) -> list:
    if isinstance(raw, list) and raw:
        return raw
    if isinstance(raw, dict):
        for k in ["data","events","list","items","result","content","rows","matches","games"]:
            v = raw.get(k)
            if isinstance(v, list) and v:
                return v
            if isinstance(v, dict):
                for k2 in ["events","list","items","matches","games","data"]:
                    v2 = v.get(k2)
                    if isinstance(v2, list) and v2:
                        return v2
        found = _deep_find_list(raw, ["events","matches","games","list","items"])
        if found:
            return found
    return []

def _parse_bc_event(ev: dict):
    home = (ev.get("homeTeam") or ev.get("home") or ev.get("team1") or
            ev.get("homeName") or ev.get("home_team") or ev.get("teamHome") or "")
    away = (ev.get("awayTeam") or ev.get("away") or ev.get("team2") or
            ev.get("awayName") or ev.get("away_team") or ev.get("teamAway") or "")
    sport  = ev.get("sportName") or ev.get("sport") or ev.get("sportTitle") or ev.get("category") or "Unknown"
    start  = ev.get("startTime") or ev.get("startAt") or ev.get("time") or ev.get("kickOff") or ""

    outcomes = []
    raw_mkts = ev.get("markets") or ev.get("odds") or ev.get("marketList") or ev.get("betTypes") or []
    if isinstance(raw_mkts, list):
        for mkt in raw_mkts:
            if not isinstance(mkt, dict): continue
            outs = mkt.get("outcomes") or mkt.get("selections") or mkt.get("runners") or mkt.get("bets") or []
            for o in outs:
                if not isinstance(o, dict): continue
                name = o.get("name") or o.get("label") or o.get("selectionName") or ""
                price = None
                for pk in ["price","odds","odd","value","coefficient","rate","oddsValue"]:
                    if pk in o:
                        try: price = float(o[pk]); break
                        except: pass
                if name and price and price > 1.01:
                    outcomes.append({"name": name, "price": price})
    elif isinstance(raw_mkts, dict):
        for k, v in raw_mkts.items():
            try:
                price = float(v)
                if price > 1.01:
                    outcomes.append({"name": k, "price": price})
            except: pass

    if not home or not away or not outcomes:
        return None
    return {
        "id": f"bcgame_{abs(hash(home+away+str(start)))}",
        "sport_title": sport,
        "home_team": home,
        "away_team": away,
        "commence_time": str(start),
        "bookmakers": [{
            "key": "bcgame", "title": "BC.Game",
            "last_update": datetime.now(timezone.utc).isoformat(),
            "markets": [{"key": "h2h", "outcomes": outcomes}]
        }]
    }

def fetch_bcgame_events() -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://bc.game",
        "Referer": "https://bc.game/",
        "Cache-Control": "no-cache"
    }
    try:
        r = requests.get(BCGAME_URL, headers=headers, timeout=25)
        log.info(f"BC.Game status={r.status_code} size={len(r.content)}B content-type={r.headers.get('Content-Type','?')}")
        if r.status_code != 200:
            return []
        try:
            raw = r.json()
        except Exception as e:
            log.error(f"BC.Game JSON parse error: {e} | preview: {r.text[:200]}")
            return []

        raw_evs = _extract_bc_events(raw)
        converted = []
        for ev in raw_evs[:400]:
            parsed = _parse_bc_event(ev)
            if parsed:
                converted.append(parsed)
        log.info(f"BC.Game: {len(converted)} events successfully parsed.")
        return converted
    except requests.exceptions.Timeout:
        log.error("BC.Game timeout")
        return []
    except Exception as e:
        log.error(f"BC.Game error: {e}")
        return []

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    merged = 0
    for bc_ev in bc_events:
        bh, ba = bc_ev["home_team"], bc_ev["away_team"]
        best_ev, best_score = None, 0.0
        for ev in odds_events:
            s = (similarity(bh, ev.get("home_team","")) + similarity(ba, ev.get("away_team",""))) / 2
            if s > best_score:
                best_score, best_ev = s, ev
        if best_score > 0.72 and best_ev:
            best_ev["bookmakers"].extend(bc_ev["bookmakers"])
            merged += 1
        else:
            odds_events.append(bc_ev)
    log.info(f"BC.Game merge: {merged} integrated, {len(bc_events)-merged} standalone.")
    return odds_events


# ═══════════════════════════════════════════════════════════════════════════════
# QUANT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def remove_vig(outcomes: list) -> dict:
    raw = {}
    for o in outcomes:
        try: raw[o["name"]] = 1.0 / float(o["price"])
        except: pass
    total = sum(raw.values())
    return {k: v/total for k,v in raw.items()} if total > 0 else {}

def kelly_stake(edge: float, odds: float, bank: float) -> float:
    b = odds - 1.0
    if b <= 0: return 0.0
    p = 1.0 / (odds / (1.0 + edge))
    kf = (b*p - (1-p)) / b
    return round(KELLY_FRACTION * kf * bank, 2) if kf > 0 else 0.0

def round10(x: float) -> float:
    return round(round(x/10)*10, 2)

def calc_stakes(odds_list: list, total: float = 1000.0) -> list:
    impl = sum(1.0/o for o in odds_list)
    if impl >= 1.0: return [0.0]*len(odds_list)
    return [(1.0/o)/impl*total for o in odds_list]


# ═══════════════════════════════════════════════════════════════════════════════
# ARB SCANNER
# ═══════════════════════════════════════════════════════════════════════════════
def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        home  = ev.get("home_team","?")
        away  = ev.get("away_team","?")
        sport = ev.get("sport_title","Unknown")
        com   = ev.get("commence_time","")

        for mkey in MARKETS:
            best: dict = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mkey: continue
                    for o in mkt.get("outcomes", []):
                        name = str(o.get("name",""))
                        pt   = o.get("point")
                        if pt is not None:
                            try: name = f"{name}_{abs(float(pt))}"
                            except: pass
                        try: price = float(o.get("price", 0))
                        except: continue
                        if price <= 1.0: continue
                        if name not in best or price > best[name][0]:
                            best[name] = (price, bm.get("title","?"), bm.get("key","?"))

            ol = list(best.items())
            if len(ol) < 2: continue

            # 2-way
            for combo in itertools.combinations(ol, 2):
                prices = [x[1][0] for x in combo]
                impl   = sum(1.0/p for p in prices)
                if impl < 1.0:
                    pct = (1.0/impl - 1.0)*100
                    if pct >= MIN_ARB_PROFIT*100:
                        stakes = calc_stakes(prices, BANK_SIZE)
                        arbs.append({
                            "ways": 2, "market": mkey.upper(), "sport": sport,
                            "match": f"{home} vs {away}", "commence": com,
                            "profit_pct": round(pct, 3),
                            "profit_amt": round((1.0/impl-1.0)*BANK_SIZE, 2),
                            "outcomes": [{
                                "name": x[0], "odds": round(x[1][0],3),
                                "book": x[1][1], "book_key": x[1][2],
                                "stake": round(s,2), "stake_rounded": round10(s)
                            } for x,s in zip(combo, stakes)]
                        })

            # 3-way
            for combo in itertools.combinations(ol, 3):
                prices = [x[1][0] for x in combo]
                impl   = sum(1.0/p for p in prices)
                if impl < 1.0:
                    pct = (1.0/impl - 1.0)*100
                    if pct >= MIN_ARB_PROFIT*100:
                        stakes = calc_stakes(prices, BANK_SIZE)
                        arbs.append({
                            "ways": 3, "market": mkey.upper(), "sport": sport,
                            "match": f"{home} vs {away}", "commence": com,
                            "profit_pct": round(pct, 3),
                            "profit_amt": round((1.0/impl-1.0)*BANK_SIZE, 2),
                            "outcomes": [{
                                "name": x[0], "odds": round(x[1][0],3),
                                "book": x[1][1], "book_key": x[1][2],
                                "stake": round(s,2), "stake_rounded": round10(s)
                            } for x,s in zip(combo, stakes)]
                        })

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    log.info(f"Arbitrage: {len(arbs)} found.")
    return arbs[:200]


# ═══════════════════════════════════════════════════════════════════════════════
# EV SCANNER
# ═══════════════════════════════════════════════════════════════════════════════
def scan_ev_bets(events: list) -> list:
    bets = []
    for ev in events:
        home  = ev.get("home_team","?")
        away  = ev.get("away_team","?")
        sport = ev.get("sport_title","Unknown")
        com   = ev.get("commence_time","")

        for mkey in MARKETS:
            pin_out = None
            for bm in ev.get("bookmakers",[]):
                if bm.get("key") == "pinnacle":
                    for m in bm.get("markets",[]):
                        if m.get("key") == mkey:
                            pin_out = m.get("outcomes",[])
            if not pin_out or len(pin_out) < 2: continue

            true_probs = remove_vig(pin_out)
            if not true_probs: continue

            for bm in ev.get("bookmakers",[]):
                if bm.get("key") == "pinnacle": continue
                for m in bm.get("markets",[]):
                    if m.get("key") != mkey: continue
                    for o in m.get("outcomes",[]):
                        name = o.get("name","")
                        if name not in true_probs: continue
                        try: price = float(o["price"])
                        except: continue
                        if price <= 1.0: continue
                        tp   = true_probs[name]
                        to   = 1.0/tp
                        edge = (price - to)/to
                        if edge >= MIN_EV_EDGE:
                            ks = kelly_stake(edge, price, BANK_SIZE)
                            bets.append({
                                "market": mkey.upper(), "sport": sport,
                                "match": f"{home} vs {away}", "commence": com,
                                "outcome": name, "book": bm.get("title","?"),
                                "book_key": bm.get("key","?"),
                                "offered_odds": round(price,3),
                                "true_odds": round(to,3),
                                "true_prob_pct": round(tp*100,2),
                                "edge_pct": round(edge*100,3),
                                "kelly_stake": ks,
                                "kelly_stake_rounded": round10(ks)
                            })
    bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"EV bets: {len(bets)} found.")
    return bets[:300]


# ═══════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATION
# ═══════════════════════════════════════════════════════════════════════════════
def send_push(arbs: list, evs: list):
    if not arbs and not evs: return
    if arbs:
        t   = arbs[0]
        msg = f"TOP ARB: {t['match']} | +{t['profit_pct']}% | {t['ways']}-way {t['market']} | {len(evs)} EV bets"
    else:
        t   = evs[0]
        msg = f"TOP EV: {t['match']} | +{t['edge_pct']}% edge | {t['book']} | {len(evs)} total"
    try:
        r = requests.post(NTFY_URL, data=msg.encode("utf-8"), headers={
            "Title": "Arb Sniper Alert", "Priority": "high",
            "Tags": "zap,moneybag", "Content-Type": "text/plain; charset=utf-8"
        }, timeout=10)
        log.info(f"Push: {r.status_code}")
    except Exception as e:
        log.error(f"Push error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# HTML DASHBOARD GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════
def generate_html(arbs, evs, raw_bc, state, key_status) -> str:
    IST      = timezone(timedelta(hours=5, minutes=30))
    ist_now  = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    pass_hash = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()
    total_quota = sum(k["remaining"] for k in key_status)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>Arb Sniper v4.0</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<style>
:root{{
  --bg0:#08080a;--bg1:#0f0f12;--bg2:#17171c;--bg3:#1e1e26;--bg4:#27272f;
  --border:#2e2e38;--accent:#22d3ee;--purple:#a78bfa;--green:#4ade80;
  --red:#f87171;--yellow:#fbbf24;--orange:#fb923c;
  --txt:#e2e2e8;--txt2:#9898a8;--txt3:#5a5a6a;
  --font:'JetBrains Mono',monospace;
}}
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@600;700;800&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
body{{background:var(--bg0);color:var(--txt);font-family:var(--font);min-height:100vh;overflow-x:hidden}}
::-webkit-scrollbar{{width:4px;height:4px}}::-webkit-scrollbar-track{{background:var(--bg1)}}::-webkit-scrollbar-thumb{{background:var(--bg4);border-radius:2px}}

/* LOCK */
#lock{{position:fixed;inset:0;z-index:9999;background:var(--bg0);display:flex;align-items:center;justify-content:center}}
.lbox{{background:var(--bg2);border:1px solid var(--border);border-radius:20px;padding:36px 32px;width:90%;max-width:340px;display:flex;flex-direction:column;align-items:center;gap:18px;box-shadow:0 0 80px rgba(34,211,238,.07)}}
.lock-icon{{font-size:44px;color:var(--accent);animation:glow 2s infinite alternate}}
@keyframes glow{{from{{text-shadow:0 0 8px var(--accent)}}to{{text-shadow:0 0 24px var(--accent),0 0 48px rgba(34,211,238,.3)}}}}
.lock-title{{font-family:'Syne',sans-serif;font-size:20px;font-weight:800;letter-spacing:3px}}
.lock-sub{{font-size:10px;color:var(--txt3);letter-spacing:2px;text-transform:uppercase}}
#linput{{background:var(--bg3);border:1px solid var(--border);border-radius:10px;color:var(--txt);font-size:18px;padding:14px;width:100%;text-align:center;letter-spacing:6px;outline:none;transition:border .2s}}
#linput:focus{{border-color:var(--accent)}}
#lbtn{{background:linear-gradient(135deg,var(--accent),var(--purple));color:#000;font-family:'Syne',sans-serif;font-weight:800;border:none;border-radius:10px;padding:14px;width:100%;font-size:13px;cursor:pointer;letter-spacing:1.5px}}
#lerr{{color:var(--red);font-size:11px;display:none}}

/* APP */
#app{{display:none}}
.topbar{{background:var(--bg1);border-bottom:1px solid var(--border);padding:0 20px;height:52px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:200}}
.logo{{font-family:'Syne',sans-serif;font-size:15px;font-weight:800;background:linear-gradient(120deg,var(--accent),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.topbar-r{{display:flex;align-items:center;gap:10px}}
.live-dot{{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:blink 1.4s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.top-btn{{background:none;border:1px solid var(--border);border-radius:6px;color:var(--txt2);font-family:var(--font);font-size:11px;padding:5px 10px;cursor:pointer;display:flex;align-items:center;gap:5px;transition:all .2s}}
.top-btn:hover{{border-color:var(--accent);color:var(--accent)}}

/* STATS */
.statsbar{{background:var(--bg1);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none}}
.statsbar::-webkit-scrollbar{{display:none}}
.ss{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:8px 14px;min-width:95px;white-space:nowrap}}
.ss-l{{font-size:9px;color:var(--txt3);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:3px}}
.ss-v{{font-size:15px;font-weight:700;font-family:'Syne',sans-serif}}

/* TABS */
.tabs{{display:flex;gap:4px;padding:12px 20px;background:var(--bg1);border-bottom:1px solid var(--border);overflow-x:auto;scrollbar-width:none}}
.tabs::-webkit-scrollbar{{display:none}}
.tab{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--txt3);font-family:var(--font);font-size:11px;padding:9px 14px;cursor:pointer;white-space:nowrap;font-weight:600;display:flex;align-items:center;gap:6px;transition:all .2s}}
.tab.active{{background:rgba(34,211,238,.08);color:var(--accent);border-color:rgba(34,211,238,.35)}}
.tbadge{{background:var(--accent);color:#000;font-size:9px;padding:1px 5px;border-radius:8px;font-weight:800}}
.tbadge.p{{background:var(--purple)}}
.tbadge.y{{background:var(--yellow);color:#000}}
.tc{{display:none;padding:16px 20px 40px}}
.tc.active{{display:block}}

/* FILTER BAR */
.fbar{{display:flex;gap:8px;margin-bottom:16px;align-items:center;flex-wrap:wrap}}
.fsel{{background:var(--bg3);border:1px solid var(--border);border-radius:7px;color:var(--txt2);font-family:var(--font);font-size:11px;padding:7px 10px;outline:none;cursor:pointer}}
.fsel:focus{{border-color:var(--accent)}}
.fpill{{display:flex;align-items:center;gap:8px;background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:7px 12px;font-size:11px;color:var(--txt2)}}
input[type=range]{{accent-color:var(--accent);width:90px;cursor:pointer}}
.fsearch{{background:var(--bg3);border:1px solid var(--border);border-radius:7px;color:var(--txt);font-family:var(--font);font-size:11px;padding:7px 12px;outline:none;flex:1;min-width:140px}}
.fsearch:focus{{border-color:var(--accent)}}

/* CARDS */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;transition:border-color .2s,box-shadow .2s;position:relative;overflow:hidden}}
.card:hover{{border-color:rgba(34,211,238,.25);box-shadow:0 4px 24px rgba(0,0,0,.4)}}
.cstripe{{position:absolute;top:0;left:0;right:0;height:3px}}
.cs-g{{background:linear-gradient(90deg,var(--green),#86efac)}}
.cs-c{{background:linear-gradient(90deg,var(--accent),var(--purple))}}
.cs-y{{background:linear-gradient(90deg,var(--yellow),var(--orange))}}
.ch{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;margin-top:4px}}
.ctype{{font-size:9px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;padding:3px 8px;border-radius:5px}}
.ctype.arb{{background:rgba(74,222,128,.1);color:var(--green);border:1px solid rgba(74,222,128,.25)}}
.ctype.ev{{background:rgba(34,211,238,.1);color:var(--accent);border:1px solid rgba(34,211,238,.25)}}
.ctype.bc{{background:rgba(167,139,250,.1);color:var(--purple);border:1px solid rgba(167,139,250,.25)}}
.cprofit{{font-family:'Syne',sans-serif;font-weight:800;font-size:20px}}
.cmatch{{font-size:13px;font-weight:600;margin-bottom:6px;color:var(--txt);line-height:1.3}}
.cmeta{{font-size:10px;color:var(--txt3);margin-bottom:12px;display:flex;flex-wrap:wrap;gap:8px}}
.ctable{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:12px}}
.ctable th{{color:var(--txt3);text-align:left;padding:0 6px 7px;border-bottom:1px solid var(--border);font-weight:500;font-size:9px;letter-spacing:1px;text-transform:uppercase}}
.ctable td{{padding:7px 6px;border-bottom:1px solid rgba(46,46,56,.7)}}
.ctable tr:last-child td{{border:none}}
.btag{{background:var(--bg4);border:1px solid var(--border);border-radius:4px;padding:2px 5px;font-size:9px;font-weight:700;color:var(--txt2)}}
.oval{{color:var(--yellow);font-weight:700;font-size:12px}}
.stake-x{{font-size:9px;color:var(--txt3);display:block}}
.stake-m{{font-size:13px;font-weight:700;color:var(--txt)}}
.cfoot{{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}}
.cfoot-l{{font-size:10px;color:var(--txt3)}}
.cbtn{{background:none;border:1px solid var(--purple);color:var(--purple);font-family:var(--font);font-size:10px;padding:5px 10px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:4px;transition:all .2s}}
.cbtn:hover{{background:var(--purple);color:#000}}
.empty{{text-align:center;padding:60px 20px;color:var(--txt3);grid-column:1/-1}}
.empty i{{font-size:36px;margin-bottom:12px;opacity:.3;display:block}}

/* CALCULATOR */
.calc-wrap{{max-width:580px}}
.csec{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px}}
.csec-title{{font-family:'Syne',sans-serif;font-size:14px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.ctabs{{display:flex;gap:6px;margin-bottom:14px}}
.ctab{{background:var(--bg3);border:1px solid var(--border);border-radius:7px;color:var(--txt2);font-family:var(--font);font-size:11px;padding:8px 16px;cursor:pointer;transition:all .2s}}
.ctab.active{{background:rgba(167,139,250,.12);color:var(--purple);border-color:rgba(167,139,250,.4)}}
.cig{{margin-bottom:12px}}
.cil{{font-size:10px;color:var(--txt3);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:5px}}
.cinput{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--txt);font-family:var(--font);font-size:14px;padding:11px 14px;width:100%;outline:none;transition:border .2s}}
.cinput:focus{{border-color:var(--purple)}}
.run-btn{{width:100%;background:linear-gradient(135deg,var(--purple),var(--accent));color:#000;font-family:'Syne',sans-serif;font-weight:800;border:none;border-radius:10px;padding:13px;font-size:13px;cursor:pointer;letter-spacing:1px;margin-top:4px}}
.cresult{{background:var(--bg0);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:14px}}
.crrow{{display:flex;justify-content:space-between;padding:7px 0;font-size:12px;border-bottom:1px solid rgba(46,46,56,.6)}}
.crrow:last-child{{border:none;font-weight:700;font-size:13px}}

/* API TABLE */
.atable{{width:100%;border-collapse:collapse;font-size:12px}}
.atable th{{background:var(--bg3);color:var(--txt3);text-align:left;padding:10px 14px;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;border-bottom:1px solid var(--border)}}
.atable td{{padding:10px 14px;border-bottom:1px solid var(--border)}}
.atable tr:hover td{{background:rgba(30,30,38,.5)}}
.qbar{{background:var(--bg4);border-radius:3px;height:5px;margin-top:4px;overflow:hidden}}
.qfill{{height:100%;border-radius:3px}}

/* MODAL */
.modal-bg{{position:fixed;inset:0;z-index:800;background:rgba(0,0,0,.75);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center}}
.modal-bg.open{{display:flex}}
.modal{{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:28px;width:92%;max-width:460px;max-height:90vh;overflow-y:auto}}
.modal-title{{font-family:'Syne',sans-serif;font-size:15px;font-weight:700;display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}}
.closex{{background:none;border:none;color:var(--txt3);font-size:18px;cursor:pointer}}

/* ODDS CONVERTER GRID */
.ogrid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}

@media(max-width:600px){{
  .topbar{{padding:0 14px}}.tc{{padding:12px 14px 28px}}.grid{{grid-template-columns:1fr}}.ogrid{{grid-template-columns:1fr}}
}}
</style>
</head>
<body>

<div id="lock">
  <div class="lbox">
    <i class="fas fa-crosshairs lock-icon"></i>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">v4.0 — Elite Edition</div>
    <input id="linput" type="password" placeholder="••••••••" autocomplete="current-password"/>
    <button id="lbtn" onclick="unlock()"><i class="fas fa-unlock-alt"></i>&nbsp; UNLOCK</button>
    <div id="lerr"><i class="fas fa-triangle-exclamation"></i> Invalid password</div>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> ARB SNIPER v4.0</div>
    <div class="topbar-r">
      <div class="live-dot"></div>
      <span style="font-size:10px;color:var(--txt3)">{ist_now}</span>
      <button class="top-btn" onclick="logout()"><i class="fas fa-right-from-bracket"></i></button>
    </div>
  </div>

  <div class="statsbar">
    <div class="ss"><div class="ss-l">Arbs</div><div class="ss-v" style="color:var(--green)" id="ss-arb">0</div></div>
    <div class="ss"><div class="ss-l">+EV Bets</div><div class="ss-v" style="color:var(--accent)" id="ss-ev">0</div></div>
    <div class="ss"><div class="ss-l">BC Events</div><div class="ss-v" style="color:var(--purple)" id="ss-bc">0</div></div>
    <div class="ss"><div class="ss-l">Top Arb</div><div class="ss-v" style="color:var(--green)" id="ss-toparb">—</div></div>
    <div class="ss"><div class="ss-l">Top EV</div><div class="ss-v" style="color:var(--accent)" id="ss-topev">—</div></div>
    <div class="ss"><div class="ss-l">Profit/Rs1K</div><div class="ss-v" style="color:var(--yellow)" id="ss-profit">—</div></div>
    <div class="ss"><div class="ss-l">Events</div><div class="ss-v">{state.get('total_events_scanned',0)}</div></div>
    <div class="ss"><div class="ss-l">API Quota</div><div class="ss-v" style="color:var(--yellow)">{total_quota}</div></div>
    <div class="ss"><div class="ss-l">Keys</div><div class="ss-v">{len(key_status)}</div></div>
  </div>

  <div class="tabs">
    <button class="tab active" id="tb-arb" onclick="swTab('arb',this)"><i class="fas fa-percent"></i> Arbitrage <span class="tbadge" id="cnt-arb">0</span></button>
    <button class="tab" id="tb-ev"  onclick="swTab('ev',this)"><i class="fas fa-chart-line"></i> +EV <span class="tbadge p" id="cnt-ev">0</span></button>
    <button class="tab" id="tb-bc"  onclick="swTab('bc',this)"><i class="fas fa-gamepad"></i> BC.Game <span class="tbadge y" id="cnt-bc">0</span></button>
    <button class="tab" id="tb-calc" onclick="swTab('calc',this)"><i class="fas fa-calculator"></i> Calculator</button>
    <button class="tab" id="tb-api" onclick="swTab('api',this)"><i class="fas fa-server"></i> API Keys</button>
  </div>

  <div id="tc-arb" class="tc active">
    <div class="fbar">
      <input class="fsearch" id="arb-q" placeholder="Search match / sport..." oninput="renderArbs()"/>
      <select class="fsel" id="arb-sport" onchange="renderArbs()"><option value="">All Sports</option></select>
      <select class="fsel" id="arb-ways" onchange="renderArbs()"><option value="">All Ways</option><option value="2">2-Way</option><option value="3">3-Way</option></select>
      <div class="fpill">Min<input type="range" id="arb-min" min="0" max="5" step="0.1" value="0" oninput="document.getElementById('arb-minv').textContent=this.value+'%';renderArbs()"/><span id="arb-minv">0%</span></div>
    </div>
    <div class="grid" id="grid-arb"></div>
  </div>

  <div id="tc-ev" class="tc">
    <div class="fbar">
      <input class="fsearch" id="ev-q" placeholder="Search match / book..." oninput="renderEvs()"/>
      <select class="fsel" id="ev-sport" onchange="renderEvs()"><option value="">All Sports</option></select>
      <select class="fsel" id="ev-book"  onchange="renderEvs()"><option value="">All Books</option></select>
      <div class="fpill">Min Edge<input type="range" id="ev-min" min="0" max="20" step="0.5" value="0" oninput="document.getElementById('ev-minv').textContent=this.value+'%';renderEvs()"/><span id="ev-minv">0%</span></div>
    </div>
    <div class="grid" id="grid-ev"></div>
  </div>

  <div id="tc-bc" class="tc">
    <div class="fbar">
      <input class="fsearch" id="bc-q" placeholder="Search BC.Game..." oninput="renderBc()"/>
      <select class="fsel" id="bc-sport" onchange="renderBc()"><option value="">All Sports</option></select>
    </div>
    <div class="grid" id="grid-bc"></div>
  </div>

  <div id="tc-calc" class="tc">
    <div class="calc-wrap">

      <div class="csec">
        <div class="csec-title"><i class="fas fa-percent" style="color:var(--green)"></i> Arbitrage Calculator</div>
        <div class="ctabs">
          <button class="ctab active" id="ct2" onclick="swCalc(2,this)">2-Way</button>
          <button class="ctab" id="ct3" onclick="swCalc(3,this)">3-Way</button>
        </div>
        <div id="calc2">
          <div class="cig"><div class="cil">Outcome 1 Odds</div><input class="cinput" id="c2o1" type="number" step="0.01" placeholder="e.g. 2.15"/></div>
          <div class="cig"><div class="cil">Outcome 2 Odds</div><input class="cinput" id="c2o2" type="number" step="0.01" placeholder="e.g. 2.05"/></div>
          <div class="cig"><div class="cil">Total Stake (Rs)</div><input class="cinput" id="c2s" type="number" value="10000"/></div>
          <button class="run-btn" onclick="runCalc(2)"><i class="fas fa-bolt"></i>&nbsp;CALCULATE</button>
        </div>
        <div id="calc3" style="display:none">
          <div class="cig"><div class="cil">Home Odds</div><input class="cinput" id="c3o1" type="number" step="0.01" placeholder="e.g. 2.50"/></div>
          <div class="cig"><div class="cil">Draw Odds</div><input class="cinput" id="c3o2" type="number" step="0.01" placeholder="e.g. 3.20"/></div>
          <div class="cig"><div class="cil">Away Odds</div><input class="cinput" id="c3o3" type="number" step="0.01" placeholder="e.g. 2.80"/></div>
          <div class="cig"><div class="cil">Total Stake (Rs)</div><input class="cinput" id="c3s" type="number" value="10000"/></div>
          <button class="run-btn" onclick="runCalc(3)"><i class="fas fa-bolt"></i>&nbsp;CALCULATE</button>
        </div>
        <div id="calc-res" class="cresult" style="display:none"></div>
      </div>

      <div class="csec">
        <div class="csec-title"><i class="fas fa-brain" style="color:var(--accent)"></i> Kelly Criterion Calculator</div>
        <div class="cig"><div class="cil">Your Win Probability (%)</div><input class="cinput" id="kp" type="number" step="0.1" placeholder="e.g. 55.0"/></div>
        <div class="cig"><div class="cil">Decimal Odds Offered</div><input class="cinput" id="ko" type="number" step="0.01" placeholder="e.g. 2.10"/></div>
        <div class="cig"><div class="cil">Bank Size (Rs)</div><input class="cinput" id="kb" type="number" value="10000"/></div>
        <button class="run-btn" onclick="runKelly()"><i class="fas fa-calculator"></i>&nbsp;CALC KELLY</button>
        <div id="kelly-res" class="cresult" style="display:none"></div>
      </div>

      <div class="csec">
        <div class="csec-title"><i class="fas fa-arrows-rotate" style="color:var(--yellow)"></i> Odds Converter</div>
        <div class="ogrid">
          <div class="cig"><div class="cil">Decimal</div><input class="cinput" id="od" type="number" step="0.001" placeholder="2.000" oninput="convOdds('d')"/></div>
          <div class="cig"><div class="cil">Fractional</div><input class="cinput" id="of" type="text" placeholder="1/1" oninput="convOdds('f')"/></div>
          <div class="cig"><div class="cil">American</div><input class="cinput" id="oa" type="number" placeholder="+100" oninput="convOdds('a')"/></div>
          <div class="cig"><div class="cil">Implied %</div><input class="cinput" id="oi" type="number" step="0.01" placeholder="50.00" oninput="convOdds('i')"/></div>
        </div>
      </div>

    </div>
  </div>

  <div id="tc-api" class="tc">
    <div class="csec">
      <div class="csec-title"><i class="fas fa-key" style="color:var(--accent)"></i> API Key Status — {len(key_status)} Keys</div>
      <table class="atable">
        <thead><tr><th>#</th><th>Key (masked)</th><th>Remaining</th><th>Quota Bar</th></tr></thead>
        <tbody id="key-tbody"></tbody>
      </table>
    </div>
    <div class="csec">
      <div class="csec-title"><i class="fas fa-chart-pie" style="color:var(--purple)"></i> Run Statistics</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px">
        <div style="background:var(--bg3);border-radius:8px;padding:12px"><div style="color:var(--txt3);font-size:9px;margin-bottom:4px;letter-spacing:1px">LAST SYNC</div><div style="font-weight:700">{ist_now}</div></div>
        <div style="background:var(--bg3);border-radius:8px;padding:12px"><div style="color:var(--txt3);font-size:9px;margin-bottom:4px;letter-spacing:1px">EVENTS SCANNED</div><div style="color:var(--green);font-weight:700">{state.get('total_events_scanned',0)}</div></div>
        <div style="background:var(--bg3);border-radius:8px;padding:12px"><div style="color:var(--txt3);font-size:9px;margin-bottom:4px;letter-spacing:1px">TOTAL KEYS</div><div style="color:var(--accent);font-weight:700">{len(key_status)}</div></div>
        <div style="background:var(--bg3);border-radius:8px;padding:12px"><div style="color:var(--txt3);font-size:9px;margin-bottom:4px;letter-spacing:1px">COMBINED QUOTA</div><div style="color:var(--yellow);font-weight:700">{total_quota}</div></div>
      </div>
    </div>
  </div>
</div>

<div class="modal-bg" id="qm">
  <div class="modal">
    <div class="modal-title"><span><i class="fas fa-calculator"></i> Quick Calc</span><button class="closex" onclick="closeModal()"><i class="fas fa-xmark"></i></button></div>
    <div id="qm-body"></div>
  </div>
</div>

<script>
const ARBS   = {json.dumps(arbs)};
const EVS    = {json.dumps(evs)};
const BC_RAW = {json.dumps(raw_bc)};
const KEYS   = {json.dumps(key_status)};
const PH     = "{pass_hash}";

// AUTH
if(localStorage.getItem('sauth')===PH) boot();
function unlock(){{
  const h=CryptoJS.SHA256(document.getElementById('linput').value).toString();
  if(h===PH){{localStorage.setItem('sauth',PH);boot();}}
  else{{document.getElementById('lerr').style.display='block';document.getElementById('linput').value='';document.getElementById('linput').style.borderColor='var(--red)';setTimeout(()=>document.getElementById('linput').style.borderColor='',1500);}}
}}
document.getElementById('linput').addEventListener('keydown',e=>{{if(e.key==='Enter')unlock()}});
function logout(){{localStorage.removeItem('sauth');location.reload();}}
function boot(){{
  document.getElementById('lock').style.display='none';
  document.getElementById('app').style.display='block';
  init();
}}

// TABS
function swTab(id,btn){{
  document.querySelectorAll('.tc').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tc-'+id).classList.add('active');
  if(btn)btn.classList.add('active');
}}

// INIT
function init(){{
  const aS=[...new Set(ARBS.map(a=>a.sport))].sort();
  const eS=[...new Set(EVS.map(e=>e.sport))].sort();
  const eB=[...new Set(EVS.map(e=>e.book_key))].sort();
  const bS=[...new Set(BC_RAW.map(b=>b.sport_title||'Unknown'))].sort();
  const add=(id,arr)=>arr.forEach(v=>document.getElementById(id).add(new Option(v,v)));
  add('arb-sport',aS); add('ev-sport',eS); add('ev-book',eB); add('bc-sport',bS);
  
  document.getElementById('ss-arb').textContent   = ARBS.length;
  document.getElementById('ss-ev').textContent    = EVS.length;
  document.getElementById('ss-bc').textContent    = BC_RAW.length;
  document.getElementById('ss-toparb').textContent= ARBS.length?'+'+ARBS[0].profit_pct+'%':'--';
  document.getElementById('ss-topev').textContent = EVS.length?'+'+EVS[0].edge_pct+'%':'--';
  document.getElementById('ss-profit').textContent= ARBS.length?'Rs'+ARBS[0].profit_amt:'--';
  document.getElementById('cnt-arb').textContent  = ARBS.length;
  document.getElementById('cnt-ev').textContent   = EVS.length;
  document.getElementById('cnt-bc').textContent   = BC_RAW.length;
  renderArbs(); renderEvs(); renderBc(); renderKeys();
}}

// HELPERS
const fd=d=>{{try{{return new Date(d).toLocaleString('en-IN',{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});}}catch{{return String(d)}}}};
const si=s=>{{s=(s||'').toLowerCase();if(s.includes('soccer')||s.includes('football'))return'fa-futbol';if(s.includes('basket'))return'fa-basketball';if(s.includes('hockey'))return'fa-hockey-puck';if(s.includes('tennis'))return'fa-table-tennis-paddle-ball';if(s.includes('mma')||s.includes('box'))return'fa-hand-fist';if(s.includes('cricket'))return'fa-cricket-bat-ball';if(s.includes('baseball'))return'fa-baseball';if(s.includes('golf'))return'fa-golf-ball-tee';return'fa-trophy';}};
const bs=k=>({{pinnacle:'PIN',bet365:'B365',betway:'BW',draftkings:'DK',fanduel:'FD',betmgm:'MGM',unibet:'UNI',stake:'STK',marathonbet:'MAR',parimatch:'PAR',betfair:'BF',dafabet:'DAF',bovada:'BOV',onexbet:'1XB',bcgame:'BCG'}})[k]||(k||'').toUpperCase().slice(0,4);

// RENDER ARBS
function renderArbs(){{
  const q=document.getElementById('arb-q').value.toLowerCase();
  const sp=document.getElementById('arb-sport').value;
  const wy=document.getElementById('arb-ways').value;
  const mn=parseFloat(document.getElementById('arb-min').value)||0;
  const data=ARBS.filter(a=>(!sp||a.sport===sp)&&(!wy||String(a.ways)===wy)&&a.profit_pct>=mn&&(!q||a.match.toLowerCase().includes(q)||a.sport.toLowerCase().includes(q)));
  document.getElementById('cnt-arb').textContent=data.length;
  const g=document.getElementById('grid-arb');
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-magnifying-glass"></i>No arbitrage opportunities match filters.<br><small>Try lowering the minimum % or broadening search.</small></div>';return;}}
  g.innerHTML=data.map((a,i)=>{{
    const rows=a.outcomes.map(o=>`<tr><td><span class="btag">${{bs(o.book_key)}}</span> ${{o.name}}</td><td><span class="oval">${{o.odds}}</span></td><td><span class="stake-x">exact Rs${{o.stake}}</span><span class="stake-m">Rs${{o.stake_rounded}}</span></td></tr>`).join('');
    const oa=JSON.stringify(a.outcomes.map(o=>o.odds));
    return `<div class="card"><div class="cstripe cs-g"></div>
      <div class="ch"><span class="ctype arb"><i class="fas fa-percent"></i> ${{a.ways}}-WAY ARB · ${{a.market}}</span><span class="cprofit" style="color:var(--green)">+${{a.profit_pct}}%</span></div>
      <div class="cmatch"><i class="fas ${{si(a.sport)}}"></i> ${{a.match}}</div>
      <div class="cmeta"><span><i class="fas fa-calendar"></i> ${{fd(a.commence)}}</span><span><i class="fas fa-tag"></i> ${{a.sport.replace(/_/g,' ')}}</span></div>
      <table class="ctable"><thead><tr><th>Outcome / Book</th><th>Odds</th><th>Stake on Rs1000</th></tr></thead><tbody>${{rows}}</tbody></table>
      <div class="cfoot"><span class="cfoot-l"><i class="fas fa-coins"></i> Profit: Rs${{a.profit_amt}} / Rs1000</span><button class="cbtn" onclick='openQC(${{oa}},${{a.ways}})'><i class="fas fa-calculator"></i> Calc</button></div>
    </div>`;
  }}).join('');
}}

// RENDER EVS
function renderEvs(){{
  const q=document.getElementById('ev-q').value.toLowerCase();
  const sp=document.getElementById('ev-sport').value;
  const bk=document.getElementById('ev-book').value;
  const mn=parseFloat(document.getElementById('ev-min').value)||0;
  const data=EVS.filter(v=>(!sp||v.sport===sp)&&(!bk||v.book_key===bk)&&v.edge_pct>=mn&&(!q||v.match.toLowerCase().includes(q)||(v.book_key||'').includes(q)));
  document.getElementById('cnt-ev').textContent=data.length;
  const g=document.getElementById('grid-ev');
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-chart-line"></i>No value bets match filters.</div>';return;}}
  g.innerHTML=data.map(v=>`
    <div class="card"><div class="cstripe cs-c"></div>
      <div class="ch"><span class="ctype ev"><i class="fas fa-chart-line"></i> +EV · ${{v.market}}</span><span class="cprofit" style="color:var(--accent)">+${{v.edge_pct}}%</span></div>
      <div class="cmatch"><i class="fas ${{si(v.sport)}}"></i> ${{v.match}}</div>
      <div class="cmeta"><span><i class="fas fa-calendar"></i> ${{fd(v.commence)}}</span><span>${{v.sport.replace(/_/g,' ')}}</span></div>
      <table class="ctable">
        <tr><td>Outcome</td><td colspan=2><strong>${{v.outcome}}</strong></td></tr>
        <tr><td>Bookmaker</td><td colspan=2><span class="btag">${{bs(v.book_key)}}</span> ${{v.book}}</td></tr>
        <tr><td>Offered Odds</td><td colspan=2><span class="oval">${{v.offered_odds}}</span></td></tr>
        <tr><td>True Odds</td><td colspan=2>${{v.true_odds}} <small style="color:var(--txt3)">(${{v.true_prob_pct}}%)</small></td></tr>
        <tr><td>Kelly Stake (30%)</td><td colspan=2><span class="stake-x">exact Rs${{v.kelly_stake}}</span><span class="stake-m">Rs${{v.kelly_stake_rounded}}</span></td></tr>
      </table>
    </div>`).join('');
}}

// RENDER BC
function renderBc(){{
  const q=document.getElementById('bc-q').value.toLowerCase();
  const sp=document.getElementById('bc-sport').value;
  const data=BC_RAW.filter(b=>(!sp||b.sport_title===sp)&&(!q||(b.home_team+' '+b.away_team).toLowerCase().includes(q)));
  document.getElementById('cnt-bc').textContent=data.length;
  const g=document.getElementById('grid-bc');
  if(!data.length){{g.innerHTML='<div class="empty"><i class="fas fa-gamepad"></i>No BC.Game events available.<br><small>The endpoint may be temporarily down or returning an unexpected format.</small></div>';return;}}
  g.innerHTML=data.slice(0,100).map(b=>{{
    const outs=b.bookmakers[0].markets[0].outcomes;
    return `<div class="card"><div class="cstripe cs-y"></div>
      <div class="ch"><span class="ctype bc"><i class="fas fa-gamepad"></i> BC.GAME</span></div>
      <div class="cmatch">${{b.home_team}} vs ${{b.away_team}}</div>
      <div class="cmeta"><span><i class="fas fa-calendar"></i> ${{fd(b.commence_time)}}</span><span>${{b.sport_title}}</span></div>
      <table class="ctable">${{outs.map(o=>`<tr><td>${{o.name}}</td><td><span class="oval">${{o.price}}</span></td></tr>`).join('')}}</table>
    </div>`;
  }}).join('');
}}

// RENDER KEYS
function renderKeys(){{
  document.getElementById('key-tbody').innerHTML=KEYS.map((k,i)=>{{
    const pct=Math.max(0,Math.min(100,(k.remaining/500)*100));
    const col=pct>50?'var(--green)':pct>15?'var(--yellow)':'var(--red)';
    return `<tr><td style="color:var(--txt3)">#${{i+1}}</td><td style="font-family:monospace;color:var(--accent)">${{k.key}}</td><td style="font-weight:700;color:${{col}}">${{k.remaining}}</td><td style="width:110px"><div class="qbar"><div class="qfill" style="width:${{pct}}%;background:${{col}}"></div></div></td></tr>`;
  }}).join('');
}}

// QUICK CALC MODAL
function openQC(oa, ways){{
  if(ways===2){{document.getElementById('c2o1').value=oa[0]||'';document.getElementById('c2o2').value=oa[1]||'';swCalc(2,document.getElementById('ct2'));}}
  else{{document.getElementById('c3o1').value=oa[0]||'';document.getElementById('c3o2').value=oa[1]||'';document.getElementById('c3o3').value=oa[2]||'';swCalc(3,document.getElementById('ct3'));}}
  const stake=10000;
  const impl=oa.reduce((s,o)=>s+1/o,0);
  const pct=(1/impl-1)*100;
  const stakes=oa.map(o=>(1/o)/impl*stake);
  const profit=stake*(1/impl-1);
  document.getElementById('qm-body').innerHTML=`
    <div class="cresult">
      ${{oa.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>Rs${{stakes[i].toFixed(2)}}</span></div>`).join('')}}
      <div class="crrow"><span>Implied</span><span>${{(impl*100).toFixed(3)}}%</span></div>
      ${{pct>0?`<div class="crrow" style="color:var(--green)"><span>PROFIT on Rs10,000</span><span>+Rs${{profit.toFixed(2)}} (+${{pct.toFixed(3)}}%)</span></div>`:`<div class="crrow" style="color:var(--red)"><span>NOT ARB</span><span>Over-round ${{Math.abs(pct).toFixed(3)}}%</span></div>`}}
    </div>
    <button class="run-btn" style="margin-top:12px" onclick="closeModal();swTab('calc',document.getElementById('tb-calc'))"><i class="fas fa-arrow-right"></i> Full Calculator</button>`;
  document.getElementById('qm').classList.add('open');
}}
function closeModal(){{document.getElementById('qm').classList.remove('open');}}
document.getElementById('qm').addEventListener('click',e=>{{if(e.target===e.currentTarget)closeModal();}});

// ARB CALCULATOR
function swCalc(n,btn){{
  document.querySelectorAll('.ctab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('calc2').style.display=n===2?'block':'none';
  document.getElementById('calc3').style.display=n===3?'block':'none';
  document.getElementById('calc-res').style.display='none';
}}
function runCalc(n){{
  let odds=[],stake=0;
  if(n===2){{odds=[+document.getElementById('c2o1').value,+document.getElementById('c2o2').value];stake=+document.getElementById('c2s').value||10000;}}
  else{{odds=[+document.getElementById('c3o1').value,+document.getElementById('c3o2').value,+document.getElementById('c3o3').value];stake=+document.getElementById('c3s').value||10000;}}
  if(odds.some(o=>!o||o<=1)){{alert('Enter valid odds > 1');return;}}
  const impl=odds.reduce((s,o)=>s+1/o,0);
  const pct=(1/impl-1)*100;
  const stakes=odds.map(o=>(1/o)/impl*stake);
  const profit=stake*(1/impl-1);
  const rb=document.getElementById('calc-res');
  rb.innerHTML=[
    ...odds.map((o,i)=>`<div class="crrow"><span>Leg ${{i+1}} @ ${{o}}</span><span>Rs${{stakes[i].toFixed(2)}} <small style="color:var(--txt3)">(Rs${{Math.round(stakes[i]/10)*10}} rounded)</small></span></div>`),
    `<div class="crrow"><span>Total Stake</span><span>Rs${{stake.toFixed(2)}}</span></div>`,
    `<div class="crrow"><span>Implied Total</span><span style="color:${{impl<1?'var(--green)':'var(--red)}}">${{(impl*100).toFixed(3)}}%</span></div>`,
    pct>0?`<div class="crrow" style="color:var(--green)"><span>PROFIT</span><span>+Rs${{profit.toFixed(2)}} (+${{pct.toFixed(3)}}%)</span></div>`:`<div class="crrow" style="color:var(--red)"><span>NO ARB — Over-round</span><span>${{Math.abs(pct).toFixed(3)}}%</span></div>`
  ].join('');
  rb.style.display='block';
}}

// KELLY CALCULATOR
function runKelly(){{
  const p=parseFloat(document.getElementById('kp').value)/100;
  const o=parseFloat(document.getElementById('ko').value);
  const bank=parseFloat(document.getElementById('kb').value)||10000;
  if(!p||!o||p<=0||p>=1||o<=1){{alert('Enter valid probability (1-99) and odds > 1');return;}}
  const b=o-1,q=1-p;
  const kf=(b*p-q)/b;
  const full=kf>0?kf*bank:0;
  const frac=kf>0?0.3*kf*bank:0;
  const ev=(p*b-q)*100;
  const rb=document.getElementById('kelly-res');
  rb.innerHTML=`
    <div class="crrow"><span>Expected Value</span><span style="color:${{ev>0?'var(--green)':'var(--red)}}">${{ev>0?'+':''}}${{ev.toFixed(2)}}% per bet</span></div>
    <div class="crrow"><span>Full Kelly Stake</span><span>Rs${{full.toFixed(2)}}</span></div>
    <div class="crrow"><span>30% Fractional Kelly</span><span style="color:var(--green)">Rs${{frac.toFixed(2)}} <small style="color:var(--txt3)">(Rs${{Math.round(frac/10)*10}} rounded)</small></span></div>
    <div class="crrow"><span>% of Bankroll</span><span>${{(frac/bank*100).toFixed(2)}}%</span></div>`;
  rb.style.display='block';
}}

// ODDS CONVERTER
let _cv=false;
function convOdds(from){{
  if(_cv)return;_cv=true;
  const setAll=(dec)=>{{
    document.getElementById('od').value=dec.toFixed(3);
    const am=dec>=2?'+'+Math.round((dec-1)*100):'-'+Math.round(100/(dec-1));
    document.getElementById('oa').value=am;
    const imp=(100/dec).toFixed(2);document.getElementById('oi').value=imp;
    const[n,d]=d2f(dec);document.getElementById('of').value=n+'/'+d;
  }};
  try{{
    if(from==='d'){{const v=parseFloat(document.getElementById('od').value);if(v>1)setAll(v);}}
    else if(from==='a'){{const a=parseFloat(document.getElementById('oa').value);const v=a>0?a/100+1:100/Math.abs(a)+1;if(v>1)setAll(v);}}
    else if(from==='f'){{const p=document.getElementById('of').value.split('/');const v=p.length===2?parseFloat(p[0])/parseFloat(p[1])+1:0;if(v>1)setAll(v);}}
    else if(from==='i'){{const i=parseFloat(document.getElementById('oi').value);const v=i>0&&i<100?100/i:0;if(v>1)setAll(v);}}
  }}finally{{_cv=false;}}
}}
function d2f(d){{const t=1e-5;let h1=1,h2=0,k1=0,k2=1,b=d-1;for(let i=0;i<40;i++){{const a=Math.floor(b),ah=h1;h1=a*h1+h2;h2=ah;const ak=k1;k1=a*k1+k2;k2=ak;if(Math.abs(b-a)<t)break;b=1/(b-a);}}return[h1,k1];}}
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══ ARB SNIPER v4.0 — Starting Run ══╗")
    state = load_state()

    odds_events = fetch_all_odds(state)
    bc_events   = fetch_bcgame_events()
    raw_bc_copy = list(bc_events)
    all_events  = merge_bcgame(odds_events, bc_events)

    save_state(state)

    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)
    state["last_arb_count"] = len(arbs)
    state["last_ev_count"]  = len(evs)
    save_state(state)

    send_push(arbs, evs)

    key_status = ROTATOR.status()
    html = generate_html(arbs, evs, raw_bc_copy, state, key_status)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    log.info(f"  Arbs:        {len(arbs)}")
    log.info(f"  EV Bets:     {len(evs)}")
    log.info(f"  BC Events:   {len(raw_bc_copy)}")
    log.info(f"  Total Quota: {ROTATOR.total_remaining()}")
    log.info("╚══ Run Complete ══╝")

if __name__ == "__main__":
    main()
