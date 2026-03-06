# Writes the complete index.html SPA
import os, json, datetime

# ── KEY INJECTION (Feature B1 & B2) ──
_raw_keys = os.getenv('ODDS_API_KEYS', '')
_keys_list = [k.strip() for k in _raw_keys.split(',') if k.strip()]
INJECTED_KEYS_JS = json.dumps(_keys_list)

# ── BUILD TIMESTAMP (Feature A8) ──
ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
build_time = ist_now.strftime('%Y-%m-%d %H:%M:%S IST')

# ── PURE HTML/JS SPA (Feature A1-A6) ──
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache,no-store,must-revalidate">
<title>ARB SNIPER ⚡ | Quantitative Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700;800&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
/* ═══════════ M1: DESIGN SYSTEM ═══════════ */
:root{
  --bg:#03050b;--bg2:#060a12;--surf:#0a0f18;--surf2:#0f1520;--surf3:#151d28;
  --b1:#182030;--b2:#1e2a3a;--b3:#283848;
  --cyan:#00d8ff;--cyan2:#00aacc;
  --green:#00ff90;--green2:#00cc72;
  --gold:#ffd000;--gold2:#cc9f00;
  --red:#ff3352;--red2:#cc1f3d;
  --purple:#c47eff;--purple2:#8b2cf5;
  --orange:#ff8c00;--blue:#4fa3ff;
  --text:#dce8f5;--text2:#8aa0bc;--text3:#4a6480;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:14px;scroll-behavior:smooth}
body{font-family:'Syne',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

/* M2 & M3: Ambient Grid + Radial Glow */
body::before{content:'';position:fixed;inset:0;z-index:0;
  background-image:linear-gradient(rgba(0,216,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(0,216,255,.018) 1px,transparent 1px);
  background-size:36px 36px;pointer-events:none}
body::after{content:'';position:fixed;inset:0;z-index:0;
  background:radial-gradient(ellipse 65% 50% at 12% 18%,#00d8ff09,transparent 55%),
             radial-gradient(ellipse 50% 55% at 88% 78%,#00ff9007,transparent 55%),
             radial-gradient(ellipse 40% 40% at 50% 48%,#c47eff06,transparent 65%);
  pointer-events:none;animation:amb 14s ease-in-out infinite alternate}
@keyframes amb{0%{opacity:.5}100%{opacity:1}}

.wrap{position:relative;z-index:1;max-width:920px;margin:0 auto;padding:0 13px 90px}

/* ═══════════ HEADER ═══════════ */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 0 10px;border-bottom:1px solid var(--b2);margin-bottom:0;gap:10px;flex-wrap:wrap}
.hdr-brand{display:flex;align-items:center;gap:11px}
.brand-icon{width:38px;height:38px;background:linear-gradient(135deg,#00d8ff1a,#00ff9010);border:1px solid var(--cyan);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 0 20px #00d8ff22;animation:iconP 3s ease-in-out infinite}
@keyframes iconP{0%,100%{box-shadow:0 0 20px #00d8ff22}50%{box-shadow:0 0 36px #00d8ff44}}
.brand-name{font-size:19px;font-weight:800;letter-spacing:.8px;background:linear-gradient(90deg,var(--cyan),var(--green));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1}
.brand-sub{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2.5px;color:var(--text3);text-transform:uppercase;margin-top:1px}

/* M5: Header Ticker */
.ticker{display:flex;align-items:center;gap:14px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);flex-wrap:wrap}
.tk{display:flex;align-items:center;gap:5px}
.pip{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pip 1.4s ease-in-out infinite}
@keyframes pip{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}
.tv{font-weight:700}.tv.c{color:var(--cyan)}.tv.g{color:var(--green)}.tv.gold{color:var(--gold)}.tv.r{color:var(--red)}

/* ═══════════ CONFIG PANEL ═══════════ */
.cfg{background:var(--surf);border:1px solid var(--b2);border-radius:16px;padding:18px 20px;margin:13px 0;position:relative;overflow:hidden;animation:fadeD .5s ease both}
.cfg::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--cyan),var(--green),var(--gold))}
@keyframes fadeD{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.cfg-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px}
@media(max-width:560px){.cfg-grid{grid-template-columns:1fr}}
.cfg-g{display:flex;flex-direction:column;gap:5px}.cfg-g.wide{grid-column:1/-1}
.lbl{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--text3);text-transform:uppercase}
.inp{background:var(--surf2);border:1px solid var(--b2);border-radius:9px;padding:9px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;color:var(--text);outline:none;transition:border-color .2s,box-shadow .2s;width:100%}
.inp:focus{border-color:var(--cyan);box-shadow:0 0 0 2px #00d8ff16}
.inp.gold{color:var(--gold)}.inp.gold:focus{border-color:var(--gold);box-shadow:0 0 0 2px #ffd00016}
.cfg-foot{display:flex;align-items:center;gap:10px;margin-top:13px;flex-wrap:wrap}
.slider-g{display:flex;align-items:center;gap:10px;flex:1;min-width:180px}
.slider{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;outline:none;cursor:pointer;background:linear-gradient(90deg,var(--cyan) var(--p,30%),var(--b2) var(--p,30%))}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--bg);border:2px solid var(--cyan);box-shadow:0 0 10px var(--cyan);cursor:pointer;transition:transform .15s}
.slider::-webkit-slider-thumb:active{transform:scale(1.35)}
.kelly-v{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:800;color:var(--cyan);text-shadow:0 0 10px var(--cyan);min-width:42px;text-align:right}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:10px 18px;border-radius:10px;border:1px solid;font-family:'Syne',sans-serif;font-size:13px;font-weight:700;cursor:pointer;transition:all .2s ease;white-space:nowrap;position:relative;overflow:hidden}
.btn::after{content:'';position:absolute;top:-50%;left:-60%;width:40%;height:200%;background:linear-gradient(105deg,transparent,rgba(255,255,255,.06),transparent);transform:skewX(-20deg);transition:left .5s ease}
.btn:hover::after{left:130%}
.btn-c{background:linear-gradient(135deg,#00d8ff16,#00ff9010);border-color:var(--cyan);color:var(--cyan);text-shadow:0 0 10px var(--cyan);box-shadow:0 0 14px #00d8ff10}
.btn-r{background:linear-gradient(135deg,#ff335216,#ff8c0010);border-color:var(--red);color:var(--red);text-shadow:0 0 10px var(--red)}
.btn-gold{background:linear-gradient(135deg,#ffd00016,#ff8c0010);border-color:var(--gold);color:var(--gold);text-shadow:0 0 10px var(--gold)}
.btn-sm{padding:7px 12px;font-size:12px;border-radius:8px}

/* ═══════════ C: SCAN STATUS ═══════════ */
.scan-bar{background:var(--surf);border:1px solid var(--b2);border-radius:12px;padding:11px 15px;margin-bottom:13px;display:none;animation:fadeD .3s ease both}
.scan-bar.on{display:block}
.scan-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px;gap:8px}
.scan-sport{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--cyan);font-weight:700;letter-spacing:1px}
.scan-pct{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text3)}
.prog-track{height:3px;background:var(--b2);border-radius:2px;overflow:hidden}
.prog-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--cyan),var(--green));box-shadow:0 0 8px var(--cyan);transition:width .4s ease}
.scan-log{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);margin-top:7px;max-height:56px;overflow-y:auto;line-height:1.75}
.ll{animation:fadeIn .3s ease}.lok{color:var(--green)}.lerr{color:var(--red)}.linf{color:var(--cyan)}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}

/* ═══════════ TABS ═══════════ */
.tabs{display:flex;gap:3px;background:var(--surf);border:1px solid var(--b1);border-radius:14px;padding:4px;margin-bottom:14px;animation:fadeD .5s .15s ease both}
.tab{flex:1;text-align:center;padding:9px 5px;font-size:12px;font-weight:700;color:var(--text3);cursor:pointer;border-radius:10px;transition:all .22s;user-select:none;white-space:nowrap}
.tab:hover{color:var(--text2);background:var(--surf2)}
.tab.ev.on{color:#000;background:linear-gradient(135deg,#00d8ff,#009ec0);box-shadow:0 2px 14px #00d8ff44}
.tab.arb.on{color:#000;background:linear-gradient(135deg,#c47eff,#7e22ce);box-shadow:0 2px 14px #c47eff44}
.tab.net.on{color:#000;background:linear-gradient(135deg,#4fa3ff,#2b6cb0);box-shadow:0 2px 14px #4fa3ff44}
.tab.calc.on{color:#000;background:linear-gradient(135deg,#ffd000,#ff8c00);box-shadow:0 2px 14px #ffd00044}
.tab.an.on{color:#000;background:linear-gradient(135deg,#00ff90,#00a865);box-shadow:0 2px 14px #00ff9044}
.tbadge{display:inline-block;background:rgba(0,0,0,.24);border-radius:8px;padding:1px 6px;font-size:10px;margin-left:3px;font-weight:800}
.pane{display:none}.pane.on{display:block}

/* ═══════════ E/F: CARDS ═══════════ */
.card{background:var(--surf);border:1px solid var(--b1);border-radius:16px;margin-bottom:13px;overflow:hidden;position:relative;transition:border-color .2s,transform .18s,box-shadow .18s;animation:cardIn .4s ease both}
.card:hover{border-color:var(--b3);transform:translateY(-2px);box-shadow:0 8px 32px #00000066}
@keyframes cardIn{from{opacity:0;transform:translateY(13px)}to{opacity:1;transform:translateY(0)}}
.stripe{height:2px;width:100%;background-size:200%!important;animation:shim 2s linear infinite}
.st-ev{background:linear-gradient(90deg,var(--cyan),var(--green),var(--cyan))}
.st-arb{background:linear-gradient(90deg,var(--purple),var(--cyan),var(--purple))}
.st-gold{background:linear-gradient(90deg,var(--gold),var(--orange),var(--gold))}
.st-silver{background:linear-gradient(90deg,#94a3b8,#e2e8f0,#94a3b8)}
.st-bronze{background:linear-gradient(90deg,#b56b27,#e8a870,#b56b27)}
@keyframes shim{0%{background-position:-200%}100%{background-position:200%}}
.c-head{display:flex;align-items:flex-start;justify-content:space-between;padding:13px 15px 9px;gap:10px}
.c-head-l{display:flex;align-items:flex-start;gap:9px;flex:1;min-width:0}
.spt{font-size:22px;flex-shrink:0;margin-top:1px}
.mtitle{font-size:14px;font-weight:700;color:var(--text);line-height:1.3;overflow-wrap:break-word}
.mmeta{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);margin-top:2px;letter-spacing:.5px}
.ev-badge{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:800;border:1px solid currentColor;border-radius:9px;padding:5px 11px;min-width:76px;text-align:center;background:rgba(0,0,0,.3);flex-shrink:0;line-height:1.1}
.arb-badge{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0}
.arb-pct{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:800}
.ways{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:800;padding:2px 7px;border-radius:6px;border:1px solid;letter-spacing:1px}
.c-body{padding:0 15px 14px}
.chip{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:10px;padding:3px 10px;border-radius:14px;border:1px solid;letter-spacing:1px;margin-bottom:9px}
.chip-ev{background:linear-gradient(135deg,#00d8ff10,#00ff9008);border-color:#00d8ff28;color:var(--cyan)}
.chip-arb{background:linear-gradient(135deg,#c47eff10,#00d8ff08);border-color:#c47eff28;color:var(--purple)}
.bet-box{display:flex;align-items:center;justify-content:space-between;background:var(--surf2);border:1px solid var(--b1);border-radius:10px;padding:9px 13px;margin-bottom:9px;gap:8px;flex-wrap:wrap}
.bet-l{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.blbl{font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:2px;color:var(--text3)}
.bsel{font-size:15px;font-weight:800;letter-spacing:.5px}
.bodds{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700;color:var(--gold);text-shadow:0 0 8px #ffd00055}
.book-tag{background:linear-gradient(135deg,#ffd00010,#ff8c0008);border:1px solid #ffd00028;color:var(--gold);font-size:11px;font-weight:700;padding:3px 9px;border-radius:7px;white-space:nowrap}
.mrow{display:flex;gap:9px;margin-bottom:9px}
.mbox{flex:1;background:var(--surf2);border:1px solid var(--b1);border-radius:10px;padding:9px 11px}
.mlbl{font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:1.5px;color:var(--text3);display:block;margin-bottom:2px;text-transform:uppercase}
.mval{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:800;display:block;transition:all .25s}
.sv{color:var(--gold);text-shadow:0 0 14px #ffd00055}.tv2{color:var(--text2);font-size:17px}
.crow{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.cbar-out{display:flex;align-items:center;gap:6px;flex:1;min-width:140px}
.ctag{font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:800;letter-spacing:1px;min-width:30px}
.ctrack{flex:1;height:5px;background:var(--b2);border-radius:3px;overflow:hidden}
.cfill{height:100%;border-radius:3px;animation:fillB .9s ease both}
@keyframes fillB{from{width:0!important}}
.cscore{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:800;min-width:22px;text-align:right}
.bd-wrap{margin-top:8px;border:1px solid var(--b1);border-radius:9px;overflow:hidden}
.bd-tbl{width:100%;border-collapse:collapse;font-size:11px}
.bd-tbl th{font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:1.5px;color:var(--text3);padding:7px 11px;text-align:left;background:var(--surf2);text-transform:uppercase}
.bd-tbl td{padding:6px 11px;border-top:1px solid var(--b1);color:var(--text2)}
.bk-best td{color:var(--cyan)!important;font-weight:700}
.legs{background:var(--surf2);border:1px solid var(--b1);border-radius:10px;overflow:hidden;margin-bottom:9px}
.leg{display:grid;grid-template-columns:1fr auto auto auto;gap:9px;align-items:center;padding:9px 13px;border-bottom:1px solid var(--b1)}
.leg:last-child{border-bottom:none}
.lsel{font-size:13px;font-weight:800}
.lodds{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--gold)}
.lstake{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:800;color:var(--cyan);text-shadow:0 0 8px #00d8ff55;transition:all .25s}
.lbook{font-size:10px;color:var(--text3);background:var(--surf);border:1px solid var(--b2);padding:2px 7px;border-radius:5px;white-space:nowrap}
.prof-banner{display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#00ff900e,#00d8ff06);border:1px solid #00ff9028;border-radius:10px;padding:11px 15px}
.pb-lbl{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--text3)}
.pb-val{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:800;color:var(--green);text-shadow:0 0 18px #00ff9066}

/* ═══════════ I: NETWORK TAB ═══════════ */
.net-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.ncard{background:var(--surf2);border:1px solid var(--b2);border-radius:12px;padding:14px;position:relative;overflow:hidden}
.ncard.act{border-color:var(--cyan);background:rgba(0,216,255,0.05);box-shadow:0 0 15px #00d8ff1a}
.ncard.exh{opacity:0.5;border-color:var(--red)}
.nkey{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700;color:var(--text);margin-bottom:8px;letter-spacing:1px}
.nstat{font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:800;padding:3px 8px;border-radius:6px;position:absolute;top:12px;right:12px}
.ncard.act .nstat{background:var(--cyan);color:#000}
.ncard.exh .nstat{background:var(--red);color:#fff}
.ncard .nstat.rdy{background:var(--green);color:#000}
.nbar{height:6px;background:var(--b1);border-radius:3px;margin-top:10px;overflow:hidden}
.nfill{height:100%;background:var(--cyan);transition:width 0.4s}
.ncard.exh .nfill{background:var(--red)}

/* ═══════════ CALC & ANALYTICS ═══════════ */
.calc-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px;margin-bottom:13px}
@media(max-width:540px){.calc-grid{grid-template-columns:1fr}}
.calc-card{background:var(--surf);border:1px solid var(--b2);border-radius:16px;padding:18px;animation:cardIn .4s ease both}
.cc-title{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--text3);text-transform:uppercase;margin-bottom:13px;display:flex;align-items:center;gap:6px}
.cc-title::before{content:'';flex:1;height:1px;background:var(--b2)}
.cf{margin-bottom:10px}
.ci{background:var(--surf2);border:1px solid var(--b2);border-radius:8px;padding:8px 11px;font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600;color:var(--text);outline:none;width:100%}
.rg{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:11px}
.rb{background:var(--surf2);border:1px solid var(--b1);border-radius:9px;padding:9px 11px}
.rv{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:800;display:block}

.kpi-row{display:flex;gap:9px;overflow-x:auto;margin-bottom:13px;padding-bottom:2px}
.kpi{background:var(--surf);border:1px solid var(--b2);border-radius:13px;padding:13px 14px;flex:1;min-width:105px;text-align:center}
.ki{font-family:'JetBrains Mono',monospace;font-size:10px;margin-bottom:4px}
.kv{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:800;margin-bottom:3px}
.an-row{display:grid;grid-template-columns:1fr 1fr;gap:13px;margin-bottom:13px}
.an-card{background:var(--surf);border:1px solid var(--b1);border-radius:14px;padding:17px}
.an-title{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--text3);margin-bottom:13px}
.hrow{display:flex;align-items:center;gap:7px;margin-bottom:9px}
.hlbl{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--text3);width:34px}
.htrack{flex:1;height:12px;background:var(--b2);border-radius:6px;overflow:hidden}
.hbar{height:100%;border-radius:6px;min-width:2px}
.hcnt{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2);width:18px;text-align:right}

/* ═══════════ MISC & MODAL ═══════════ */
.empty{text-align:center;padding:56px 20px;background:var(--surf);border:1px dashed var(--b2);border-radius:16px}
.ei{font-size:44px;opacity:.35;animation:flt 3s ease-in-out infinite}
@keyframes flt{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}

.modal{display:none;position:fixed;inset:0;z-index:999;background:rgba(3,5,11,.9);backdrop-filter:blur(8px);align-items:center;justify-content:center}
.modal.open{display:flex;animation:fadeIn .2s ease}
.mod-box{background:var(--surf);border:1px solid var(--b3);border-radius:20px;padding:28px 24px;width:min(355px,92vw);position:relative}

.tele{margin-top:34px;padding:12px 17px;background:var(--surf);border:1px solid var(--b1);border-radius:13px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);display:flex;flex-wrap:wrap;gap:7px 18px;justify-content:center}
.tv3{color:var(--cyan);font-weight:700}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--b2);border-radius:3px}
</style>
</head>
<body>

<div class="modal" id="bkModal">
  <div class="mod-box">
    <h2 style="margin-bottom:15px">💰 Edit Bankroll</h2>
    <input class="inp gold" id="bkIn" type="number" min="100" style="font-size:22px;margin-bottom:15px">
    <div style="display:flex;gap:8px">
      <button class="btn btn-gold" onclick="saveBK()" style="flex:1">Apply</button>
      <button class="btn btn-c" onclick="closeBK()" style="flex:1">Cancel</button>
    </div>
  </div>
</div>

<div class="wrap">
<div class="hdr">
  <div class="hdr-brand">
    <div class="brand-icon">⚡</div>
    <div><div class="brand-name">ARB SNIPER</div><div class="brand-sub">Quantitative Terminal</div></div>
  </div>
  <div class="ticker">
    <div class="tk"><div class="pip"></div><span id="tTime" class="tv c">--:-- IST</span></div>
    <div class="tk">QUOTA <span id="tQuota" class="tv gold">--</span></div>
    <div class="tk">EV <span id="tEV" class="tv g">0</span></div>
    <div class="tk">ARB <span id="tARB" class="tv c">0</span></div>
    <div class="tk">BURN <span id="tBurn" class="tv r">0</span></div>
  </div>
</div>

<div class="cfg">
  <div class="cfg-grid">
    <div class="cfg-g wide">
      <label class="lbl">🔑 Target Bookmakers</label>
      <input class="inp" id="cfgBooks" value="pinnacle,onexbet,marathonbet,dafabet,stake,betfair_ex_eu,betway">
    </div>
    <div class="cfg-g">
      <label class="lbl">💰 Bankroll (₹) <span style="cursor:pointer" onclick="openBK()">✏️</span></label>
      <input class="inp gold" id="cfgBK" type="number" value="1500" oninput="recalcStakes()">
    </div>
    <div class="cfg-g">
      <label class="lbl">🔔 NTFY Channel</label>
      <input class="inp" id="cfgNtfy" value="nikunj_arb_alerts_2026">
    </div>
    <div class="cfg-g">
      <label class="lbl">⚡ Min EV%</label>
      <input class="inp" id="cfgEV" type="number" value="1.5" step="0.1">
    </div>
    <div class="cfg-g">
      <label class="lbl">🔒 Min ARB%</label>
      <input class="inp" id="cfgARB" type="number" value="1.0" step="0.1">
    </div>
  </div>
  <div class="cfg-foot">
    <div class="slider-g">
      <span class="lbl">KELLY</span>
      <input type="range" class="slider" id="kellyR" min="1" max="100" value="30" oninput="onKelly(this.value)" style="--p:30%">
      <span class="kelly-v" id="kellyV">30%</span>
    </div>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--text2);cursor:pointer">
      <input type="checkbox" id="autoLoop" style="accent-color:var(--cyan)"> AUTO-LOOP
    </label>
    <button class="btn btn-c" id="btnStart" onclick="startScan()">▶ Start Sweep</button>
    <button class="btn btn-r" id="btnStop" onclick="stopScan()" style="display:none">⏹ Stop</button>
  </div>
</div>

<div class="scan-bar" id="scanBar">
  <div class="scan-top">
    <span class="scan-sport" id="scanSport">Initializing…</span>
    <span class="scan-pct" id="scanPct">0/6</span>
  </div>
  <div class="prog-track"><div class="prog-fill" id="progFill" style="width:0"></div></div>
  <div class="scan-log" id="scanLog"></div>
</div>

<div class="tabs">
  <div class="tab ev on" onclick="switchTab('ev')">⚡ EV <span class="tbadge" id="bEV">0</span></div>
  <div class="tab arb" onclick="switchTab('arb')">🔒 ARB <span class="tbadge" id="bARB">0</span></div>
  <div class="tab net" onclick="switchTab('net')">📡 NET</div>
  <div class="tab calc" onclick="switchTab('calc')">🧮 CALC</div>
  <div class="tab an" onclick="switchTab('an')">📊 DATA</div>
</div>

<div class="pane on" id="pane-ev">
  <div style="display:flex;gap:8px;margin-bottom:10px">
    <button class="btn btn-sm" id="top5Btn" onclick="toggleFilter('top5')">🔝 Top 5</button>
    <button class="btn btn-sm" id="sortEvBtn" onclick="toggleFilter('sortEv')">↕ Sort %</button>
    <button class="btn btn-sm" id="hcBtn" onclick="toggleFilter('hc')">🎯 High Conf</button>
    <button class="btn btn-gold btn-sm" onclick="exportCSV()">⬇ CSV</button>
  </div>
  <div id="ev-cards"></div>
</div>

<div class="pane" id="pane-arb">
  <div style="display:flex;gap:8px;margin-bottom:10px">
    <button class="btn btn-sm" id="arb3Btn" onclick="toggleFilter('arb3')">🔱 3-Way</button>
    <button class="btn btn-sm" id="sortArbBtn" onclick="toggleFilter('sortArb')">↕ Sort %</button>
  </div>
  <div id="arb-cards"></div>
</div>

<div class="pane" id="pane-net">
  <div style="font-size:11px;color:var(--text3);margin-bottom:10px;text-align:center;letter-spacing:1px">AUTOMATIC SEQUENTIAL KEY ROTATION ENABLED</div>
  <div class="net-grid" id="netGrid"></div>
</div>

<div class="pane" id="pane-calc">
  <div class="calc-grid">
    <div class="calc-card">
      <div class="cc-title">⚡ EV Calculator</div>
      <div class="cf"><span class="lbl">Soft Book</span><input class="ci" id="cSoft" type="number" step="0.01" value="2.10" oninput="calcEV()"></div>
      <div class="cf"><span class="lbl">Pin A / B</span>
        <div style="display:flex;gap:5px"><input class="ci" id="cPinA" type="number" step="0.01" value="1.95" oninput="calcEV()"><input class="ci" id="cPinB" type="number" step="0.01" value="2.08" oninput="calcEV()"></div>
      </div>
      <div class="rg">
        <div class="rb"><span class="rl">TRUE ODDS</span><span class="rv" id="rTO">--</span></div>
        <div class="rb"><span class="rl">EV %</span><span class="rv" id="rEV" style="color:var(--cyan)">--</span></div>
      </div>
    </div>
    <div class="calc-card">
      <div class="cc-title">🔒 ARB Calculator</div>
      <div class="cf"><span class="lbl">Side A / B</span>
        <div style="display:flex;gap:5px"><input class="ci" id="cAA" type="number" step="0.01" value="2.15" oninput="calcARB()"><input class="ci" id="cAB" type="number" step="0.01" value="2.10" oninput="calcARB()"></div>
      </div>
      <div class="rg">
        <div class="rb"><span class="rl">ARB %</span><span class="rv" id="rAP" style="color:var(--purple)">--</span></div>
        <div class="rb"><span class="rl">PROFIT</span><span class="rv" id="rAG" style="color:var(--green)">--</span></div>
      </div>
    </div>
  </div>
</div>

<div class="pane" id="pane-an">
  <div class="kpi-row" id="kpiRow"></div>
  <div class="an-row">
    <div class="an-card"><div class="an-title">⚡ EV Distribution</div><div id="anEH"></div></div>
    <div class="an-card"><div class="an-title">🔒 ARB Distribution</div><div id="anAH"></div></div>
  </div>
</div>

<div class="tele">
  <span>🔑 KEY <span class="tv3" id="telKey">--</span></span>
  <span>📡 QUOTA <span class="tv3" id="telQ">--</span></span>
  <span>⚡ BURN <span class="tv3" id="telB">0</span></span>
  <span>⏱ LOOP <span class="tv3" id="telLoop">OFF</span></span>
  <span style="width:100%;text-align:center;color:#4a6480;font-size:8px">BUILD: __BUILD_TIME__</span>
</div>

</div><script>
'use strict';
/* ── CONSTANTS & STATE ── */
const INJECTED_KEYS = __INJECTED_KEYS__;
const SPORTS = [
  {k:'soccer_epl', l:'⚽ EPL'},{k:'soccer_uefa_champs_league', l:'⚽ UCL'},
  {k:'basketball_nba', l:'🏀 NBA'},{k:'icehockey_nhl', l:'🏒 NHL'},
  {k:'tennis_atp', l:'🎾 ATP'},{k:'tennis_wta', l:'🎾 WTA'}
];
const CAPS = {betway:300, stake:500, onexbet:400, marathonbet:400, dafabet:350, betfair_ex_eu:600, pinnacle:1000};
const BKNAMES = {onexbet:'1xBet', pinnacle:'Pinnacle', marathonbet:'Marathon', dafabet:'Dafabet', stake:'Stake.com', betfair_ex_eu:'Betfair', betway:'Betway'};

let STATE = {
  keys: INJECTED_KEYS.length ? INJECTED_KEYS : [],
  keyIdx: 0,
  keyStats: {}, // { idx: { rem: 500, status: 'rdy' } }
  evs: [], arbs: [],
  scanning: false, sportIdx: 0, scanT0: null, burnStart: null,
  loopTimer: null, loopSecs: 300,
  filters: { top5:false, sortEv:false, hc:false, arb3:false, sortArb:false }
};

/* ── UTILS ── */
const $ = id => document.getElementById(id);
const fmt = n => '₹' + Math.round(n).toLocaleString('en-IN');
const getVal = id => parseFloat($(id).value) || 0;
const bk = k => BKNAMES[k] || k;

function devig(o1, o2) { const m = 1/o1 + 1/o2; return [1/((1/o1)/m), 1/((1/o2)/m)]; }
function kelly(soft, trueO, bankroll, kf, book=null) {
  const b = soft-1, p = 1/trueO, q = 1-p;
  let k = ((b*p - q)/b) * kf;
  if(k<=0) return 0;
  let s = Math.max(20, bankroll * Math.min(k, 0.05));
  return book && CAPS[book] ? Math.min(s, CAPS[book]) : s;
}
function conf(soft, trueO) { return Math.max(0, Math.min(100, Math.round(Math.abs(1/soft - 1/trueO)/(1/trueO)*500))); }
function log(msg, cls='') {
  const d = document.createElement('div'); d.className = 'll '+cls; 
  d.textContent = `[${new Date().toLocaleTimeString('en-IN')}] ${msg}`;
  $('scanLog').appendChild(d); $('scanLog').scrollTop = $('scanLog').scrollHeight;
}

/* ── PERSISTENCE (N1-N5) ── */
function loadState() {
  if(localStorage.getItem('arb_bk')) $('cfgBK').value = localStorage.getItem('arb_bk');
  if(localStorage.getItem('arb_ntfy')) $('cfgNtfy').value = localStorage.getItem('arb_ntfy');
  $('autoLoop').checked = localStorage.getItem('arb_autoloop') === '1';
  STATE.keys.forEach((_, i) => STATE.keyStats[i] = { rem: 500, status: 'rdy' });
}
function saveState() {
  localStorage.setItem('arb_bk', $('cfgBK').value);
  localStorage.setItem('arb_ntfy', $('cfgNtfy').value);
  localStorage.setItem('arb_autoloop', $('autoLoop').checked ? '1' : '0');
}
$('cfgBK').addEventListener('change', saveState);
$('cfgNtfy').addEventListener('change', saveState);
$('autoLoop').addEventListener('change', () => { saveState(); if(!$('autoLoop').checked) cancelLoop(); });

/* ── UI SYNC ── */
setInterval(() => $('tTime').textContent = new Date().toLocaleTimeString('en-IN'), 1000);
function onKelly(v) { $('kellyV').textContent = v+'%'; $('kellyR').style.setProperty('--p', v+'%'); recalcStakes(); calcEV(); }
function openBK() { $('bkIn').value = getVal('cfgBK'); $('bkModal').classList.add('open'); }
function closeBK() { $('bkModal').classList.remove('open'); }
function saveBK() { $('cfgBK').value = getVal('bkIn'); saveState(); recalcStakes(); closeBK(); }

function switchTab(t) {
  document.querySelectorAll('.tab').forEach(e => e.classList.remove('on'));
  document.querySelectorAll('.pane').forEach(e => e.classList.remove('on'));
  event.target.classList.add('on');
  $('pane-'+t).classList.add('on');
  if(t==='net') renderNetwork();
  if(t==='an') renderAn();
}

/* ── SCAN ENGINE (C1-C10) ── */
function startScan() {
  if(!STATE.keys.length) {
    const k = prompt('No API keys injected. Enter manually:');
    if(!k) return; STATE.keys.push(k.trim()); STATE.keyStats[0] = {rem:500, status:'rdy'};
  }
  cancelLoop();
  STATE.scanning = true; STATE.sportIdx = 0; STATE.scanT0 = Date.now();
  STATE.evs = []; STATE.arbs = [];
  $('btnStart').style.display = 'none'; $('btnStop').style.display = '';
  $('scanBar').classList.add('on'); $('scanLog').innerHTML = '';
  $('ev-cards').innerHTML = ''; $('arb-cards').innerHTML = '';
  log('Sweep sequence initiated...', 'linf');
  runNext();
}

function stopScan() {
  STATE.scanning = false;
  $('btnStart').style.display = ''; $('btnStop').style.display = 'none';
  log('Sweep halted.', 'lerr');
  updateBadges(); renderNetwork(); renderAn();
}

async function runNext() {
  if(!STATE.scanning) return;
  if(STATE.sportIdx >= SPORTS.length) {
    log(`✅ Sweep complete! EV: ${STATE.evs.length} | ARB: ${STATE.arbs.length}`, 'lok');
    stopScan();
    if($('autoLoop').checked) startLoopCountdown();
    return;
  }
  
  const sp = SPORTS[STATE.sportIdx];
  $('scanSport').textContent = sp.l;
  $('scanPct').textContent = `${STATE.sportIdx+1}/${SPORTS.length}`;
  $('progFill').style.width = Math.round(STATE.sportIdx/SPORTS.length*100) + '%';
  log(`Fetching ${sp.l}...`);

  let success = false;
  while(!success && STATE.scanning) {
    if(STATE.keyIdx >= STATE.keys.length) { log('❌ All keys exhausted.', 'lerr'); stopScan(); return; }
    const key = STATE.keys[STATE.keyIdx];
    STATE.keyStats[STATE.keyIdx].status = 'act';
    renderNetwork();
    $('telKey').textContent = `#${STATE.keyIdx+1}`;
    
    try {
      const bks = $('cfgBooks').value.split(',').map(s=>s.trim()).join(',');
      const res = await fetch(`https://api.the-odds-api.com/v4/sports/${sp.k}/odds?apiKey=${key}&regions=eu&bookmakers=${bks}&markets=totals,spreads&oddsFormat=decimal`);
      
      const rem = res.headers.get('x-requests-remaining');
      const used = res.headers.get('x-requests-used');
      if(rem) { STATE.keyStats[STATE.keyIdx].rem = rem; $('tQuota').textContent = rem; $('telQ').textContent = rem; }
      if(used) {
        if(STATE.burnStart === null) STATE.burnStart = parseInt(used) - 1;
        $('tBurn').textContent = parseInt(used) - STATE.burnStart;
        $('telB').textContent = parseInt(used) - STATE.burnStart;
      }
      
      if(res.status === 401 || res.status === 429) {
        STATE.keyStats[STATE.keyIdx].status = 'exh';
        log(`🔄 Key #${STATE.keyIdx+1} hit limit. Rotating...`, 'lerr');
        STATE.keyIdx++;
        continue;
      }
      
      if(!res.ok) { log(`⚠ HTTP ${res.status}`, 'lerr'); break; }
      
      const data = await res.json();
      log(`📦 ${data.length} events processed.`, 'lok');
      processData(data, sp.k);
      success = true;
      
    } catch(e) {
      log(`❌ Network Error`, 'lerr'); break;
    }
  }
  
  STATE.sportIdx++;
  if(STATE.scanning) setTimeout(runNext, 1500); // C5: Inter-sport delay
}

/* ── MATH & PROCESSING (D1-D11) ── */
function processData(events, sport) {
  const minEV = getVal('cfgEV'), minARB = getVal('cfgARB'), bkv = getVal('cfgBK'), kf = getVal('kellyR')/100;
  events.forEach(ev => {
    const match = `${ev.home_team} vs ${ev.away_team}`;
    const mt = new Date(ev.commence_time).toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit'});
    let el={}, al={};
    
    (ev.bookmakers||[]).forEach(b => {
      (b.markets||[]).forEach(m => {
        m.outcomes.forEach(o => {
          let pr = o.price;
          if(b.key === 'betfair_ex_eu') pr = 1 + (pr-1)*0.97; // C7
          const lk = `${m.key.toUpperCase()}_${o.point||0}`;
          
          if(!el[lk]) el[lk] = {pin:{}, soft:{}};
          if(!al[lk]) al[lk] = {};
          
          if(b.key==='pinnacle') el[lk].pin[o.name] = pr;
          else {
            if(!el[lk].soft[o.name]) el[lk].soft[o.name] = {};
            el[lk].soft[o.name][b.key] = pr;
          }
          if(!al[lk][o.name] || pr > al[lk][o.name].pr) al[lk][o.name] = {pr, bk:b.key};
        });
      });
    });

    // Calculate EV
    Object.entries(el).forEach(([lk, d]) => {
      const sides = Object.keys(d.pin);
      if(sides.length < 2) return;
      const [t1, t2] = devig(d.pin[sides[0]], d.pin[sides[1]]);
      
      [[sides[0], t1], [sides[1], t2]].forEach(([side, trueO]) => {
        if(!d.soft[side]) return;
        const bks = Object.keys(d.soft[side]);
        const bestBk = bks.reduce((a,b) => d.soft[side][a] > d.soft[side][b] ? a : b);
        const bestPr = d.soft[side][bestBk];
        
        const evp = ((bestPr/trueO)-1)*100;
        if(evp >= minEV) {
          const stk = kelly(bestPr, trueO, bkv, kf, bestBk);
          const cf = conf(bestPr, trueO);
          let bd = bks.map(k => ({bk:k, odds:d.soft[side][k], ev:((d.soft[side][k]/trueO)-1)*100, best:k===bestBk}));
          bd.push({bk:'pinnacle', odds:d.pin[side], ev:0, best:false});
          
          const edge = {pct:evp, match, time:mt, sport, line:lk, sel:side, odds:bestPr, trueO, bk:bestBk, stk, conf:cf, bd};
          STATE.evs.push(edge); renderEV(edge, STATE.evs.length-1);
          sendAlert(edge, 'EV');
        }
      });
    });

    // Calculate ARB
    Object.entries(al).forEach(([lk, outs]) => {
      const keys = Object.keys(outs);
      [2,3].forEach(w => {
        if(keys.length < w) return;
        const kSlice = keys.slice(0,w);
        const margin = kSlice.reduce((s, k) => s + 1/outs[k].pr, 0);
        if(margin < 1.0) {
          const pct = (1-margin)*100;
          if(pct >= minARB) {
            const arb = {pct, match, time:mt, sport, line:lk, ways:w, profit:(bkv/margin)-bkv, sides: kSlice.map(k => ({sel:k, pr:outs[k].pr, bk:outs[k].bk, stk:(bkv/margin)/outs[k].pr}))};
            STATE.arbs.push(arb); renderARB(arb, STATE.arbs.length-1);
            sendAlert(arb, 'ARB');
          }
        }
      });
    });
    updateBadges();
  });
}

/* ── RENDERING (E & F) ── */
function updateBadges() { $('bEV').textContent = STATE.evs.length; $('bARB').textContent = STATE.arbs.length; $('tEV').textContent = STATE.evs.length; $('tARB').textContent = STATE.arbs.length; }

const getIcon = s => ({soccer:'⚽',basketball:'🏀',icehockey:'🏒',tennis:'🎾'})[s.split('_')[0]] || '🎯';

function renderEV(ev, i) {
  if(i===0) $('ev-cards').innerHTML = '';
  const d = document.createElement('div');
  d.className = 'card ev'; d.style.animationDelay = (i*0.05)+'s';
  d.dataset.pct = ev.pct; d.dataset.conf = ev.conf;
  
  const col = ev.pct >= 10 ? 'var(--red)' : ev.pct >= 5 ? 'var(--gold)' : 'var(--green)';
  const str = i===0 ? 'st-gold' : i===1 ? 'st-silver' : i===2 ? 'st-bronze' : 'st-ev';
  const cc = ev.conf >= 70 ? 'var(--green)' : ev.conf >= 40 ? 'var(--gold)' : 'var(--red)';
  
  let bRows = ev.bd.sort((a,b)=>b.ev-a.ev).map(r => `<tr class="${r.best?'bk-best':''}"><td>${bk(r.bk)}</td><td>${r.odds.toFixed(3)}</td><td>${r.ev>0?'+':''}${r.ev.toFixed(2)}%</td></tr>`).join('');

  d.innerHTML = `
    <div class="stripe ${str}"></div>
    <div class="c-head"><div class="c-head-l"><span class="spt">${getIcon(ev.sport)}</span><div><div class="mtitle">${ev.match}</div><div class="mmeta">${ev.sport.replace(/_/g,' ').toUpperCase()} · ${ev.time}</div></div></div>
    <div class="ev-badge" style="color:${col};border-color:${col}44">${ev.pct.toFixed(2)}%</div></div>
    <div class="c-body">
      <div class="chip chip-ev">${ev.line}</div>
      <div class="bet-box"><div class="bet-l"><span class="blbl">BET</span><span class="bsel">${ev.sel.toUpperCase()}</span><span class="bodds">× ${ev.odds.toFixed(2)}</span></div><span class="book-tag">${bk(ev.bk)}</span></div>
      <div class="mrow">
        <div class="mbox"><span class="mlbl">KELLY STAKE</span><span class="mval sv sdyn" data-soft="${ev.odds}" data-true="${ev.trueO}" data-bk="${ev.bk}">${fmt(ev.stk)}</span></div>
        <div class="mbox"><span class="mlbl">TRUE ODDS</span><span class="mval tv2">${ev.trueO.toFixed(3)}</span></div>
      </div>
      <div class="crow"><span class="mlbl">CONF</span><div class="cbar-out"><div class="ctrack"><div class="cfill" style="width:${ev.conf}%;background:${cc}"></div></div><span class="cscore" style="color:${cc}">${ev.conf}</span></div></div>
      <details style="margin-top:8px;font-size:10px;color:var(--text3);cursor:pointer"><summary>Odds Matrix</summary><table class="bd-tbl">${bRows}</table></details>
    </div>`;
  $('ev-cards').appendChild(d); applyFilters();
}

function renderARB(arb, i) {
  if(i===0) $('arb-cards').innerHTML = '';
  const d = document.createElement('div');
  d.className = 'card arb'; d.style.animationDelay = (i*0.05)+'s';
  d.dataset.pct = arb.pct; d.dataset.w = arb.ways;
  
  const col = arb.ways === 2 ? 'var(--cyan)' : 'var(--purple)';
  let legs = arb.sides.map(s => `<div class="leg"><span class="lsel">${s.sel.toUpperCase()}</span><span class="lodds">@ ${s.pr.toFixed(2)}</span><span class="lstake ldyn" data-base="${s.stk}">${fmt(s.stk)}</span><span class="lbook">${bk(s.bk)}</span></div>`).join('');

  d.innerHTML = `
    <div class="stripe st-arb"></div>
    <div class="c-head"><div class="c-head-l"><span class="spt">${getIcon(arb.sport)}</span><div><div class="mtitle">${arb.match}</div><div class="mmeta">${arb.sport.replace(/_/g,' ').toUpperCase()} · ${arb.time}</div></div></div>
    <div class="arb-badge"><span class="arb-pct" style="color:${col}">${arb.pct.toFixed(2)}%</span><span class="ways" style="color:${col};border-color:${col}44">${arb.ways}W</span></div></div>
    <div class="c-body">
      <div class="chip chip-arb">${arb.line}</div>
      <div class="legs">${legs}</div>
      <div class="prof-banner"><span class="pb-lbl">PROFIT</span><span class="pb-val">${fmt(arb.profit)}</span></div>
    </div>`;
  $('arb-cards').appendChild(d); applyFilters();
}

function recalcStakes() {
  const bkv = getVal('cfgBK'), kf = getVal('kellyR')/100;
  document.querySelectorAll('.sdyn').forEach(el => { el.textContent = fmt(kelly(parseFloat(el.dataset.soft), parseFloat(el.dataset.true), bkv, kf, el.dataset.bk)); });
  document.querySelectorAll('.ldyn').forEach(el => { el.textContent = fmt(parseFloat(el.dataset.base) * (bkv/1500)); });
  calcKelly();
}

/* ── NETWORK TAB (I) ── */
function renderNetwork() {
  $('netGrid').innerHTML = STATE.keys.map((k, i) => {
    const s = STATE.keyStats[i];
    const m = k.slice(0,4) + '••••' + k.slice(-4);
    return `<div class="ncard ${s.status}"><div class="nstat ${s.status}">${s.status.toUpperCase()}</div><div class="nkey">${m}</div><div style="font-size:10px;color:var(--text3)">QUOTA <span style="color:var(--text);font-weight:800">${s.rem}</span>/500</div><div class="nbar"><div class="nfill" style="width:${(s.rem/500)*100}%"></div></div></div>`;
  }).join('');
}

/* ── FILTERS (O) ── */
function toggleFilter(f) {
  STATE.filters[f] = !STATE.filters[f];
  const btn = $(f+'Btn');
  btn.style.borderColor = STATE.filters[f] ? 'var(--cyan)' : 'var(--b2)';
  applyFilters();
}
function applyFilters() {
  // EV
  let ecs = Array.from($('ev-cards').children);
  if(STATE.filters.sortEv) ecs.sort((a,b) => b.dataset.pct - a.dataset.pct);
  ecs.forEach((c, i) => {
    let hide = (STATE.filters.top5 && i>=5) || (STATE.filters.hc && c.dataset.conf<60);
    c.style.display = hide ? 'none' : '';
    $('ev-cards').appendChild(c);
  });
  // ARB
  let acs = Array.from($('arb-cards').children);
  if(STATE.filters.sortArb) acs.sort((a,b) => b.dataset.pct - a.dataset.pct);
  acs.forEach(c => {
    c.style.display = (STATE.filters.arb3 && c.dataset.w !== '3') ? 'none' : '';
    $('arb-cards').appendChild(c);
  });
}
function exportCSV() {
  let rows = [['Type','Match','Line','Selection','Odds','EV/ARB%','Stake','Book']];
  STATE.evs.forEach(e => rows.push(['EV', e.match, e.line, e.sel, e.odds, e.pct.toFixed(2), e.stk.toFixed(0), bk(e.bk)]));
  STATE.arbs.forEach(a => rows.push(['ARB', a.match, a.line, a.sides.map(s=>s.sel).join('/'), a.sides.map(s=>s.pr).join('/'), a.pct.toFixed(2), '', '']));
  const url = URL.createObjectURL(new Blob([rows.map(r=>r.join(',')).join('\\n')], {type:'text/csv'}));
  const a = document.createElement('a'); a.href = url; a.download = 'sniper_export.csv'; a.click();
}

/* ── CALCULATORS (J) ── */
function calcEV() {
  const s = getVal('cSoft'), pa = getVal('cPinA'), pb = getVal('cPinB');
  if(s && pa && pb) {
    const t = devig(pa, pb)[0];
    $('rTO').textContent = t.toFixed(3);
    $('rEV').textContent = (((s/t)-1)*100).toFixed(2)+'%';
  }
}
function calcARB() {
  const a = getVal('cAA'), b = getVal('cAB');
  if(a && b) {
    const m = 1/a + 1/b;
    $('rAP').textContent = ((1-m)*100).toFixed(2)+'%';
    $('rAG').textContent = fmt((getVal('cfgBK')/m)-getVal('cfgBK'));
  }
}
function calcKelly() { /* Implemented in recalcStakes flow conceptually, simplified here for space */ }
calcEV(); calcARB();

/* ── ANALYTICS (K) ── */
function renderAn() {
  const e = STATE.evs, a = STATE.arbs;
  const ap = a.reduce((s,x)=>s+x.profit,0);
  $('kpiRow').innerHTML = `
    <div class="kpi"><div class="ki">⚡ EV</div><div class="kv c">${e.length}</div></div>
    <div class="kpi"><div class="ki">⚡ MAX</div><div class="kv g">${e.length?Math.max(...e.map(x=>x.pct)).toFixed(1):0}%</div></div>
    <div class="kpi"><div class="ki">🔒 ARB</div><div class="kv p">${a.length}</div></div>
    <div class="kpi"><div class="ki">💰 PROFIT</div><div class="kv g">${fmt(ap)}</div></div>`;
  
  // Basic Histograms
  const e1 = e.filter(x=>x.pct<5).length, e2 = e.filter(x=>x.pct>=5).length;
  $('anEH').innerHTML = `<div class="hrow"><span class="hlbl">< 5%</span><div class="htrack"><div class="hbar" style="width:${e1?e1/e.length*100:0}%;background:var(--cyan)"></div></div><span class="hcnt">${e1}</span></div><div class="hrow"><span class="hlbl">> 5%</span><div class="htrack"><div class="hbar" style="width:${e2?e2/e.length*100:0}%;background:var(--gold)"></div></div><span class="hcnt">${e2}</span></div>`;
}

/* ── PUSH ALERTS & AUTO-LOOP (G & H) ── */
function sendAlert(data, type) {
  const ch = $('cfgNtfy').value; if(!ch) return;
  const key = `${type}|${data.match}|${data.line}|${data.pct.toFixed(1)}`;
  let cache = JSON.parse(localStorage.getItem('arb_alert_cache')||'{}');
  const now = Date.now();
  for(let k in cache) if(now - cache[k] > 21600000) delete cache[k]; // 6h TTL
  if(cache[key]) return;
  
  cache[key] = now; localStorage.setItem('arb_alert_cache', JSON.stringify(cache));
  
  const title = type==='EV' ? `📈 ${data.pct.toFixed(2)}% EV | ${data.match}` : `🚨 ${data.pct.toFixed(2)}% ARB | ${data.match}`;
  let msg = `🏆 ${data.sport.replace(/_/g,' ').toUpperCase()}\\n📈 ${data.line}\\n\\n`;
  if(type==='EV') msg += `💰 BET EXACTLY: ${fmt(data.stk)}\n👉 ${data.sel.toUpperCase()} @ ${data.odds.toFixed(2)} on ${bk(data.bk)}`;
  else msg += `✨ GUARANTEED PROFIT: ${fmt(data.profit)}`;

  fetch('https://ntfy.sh/', {
    method:'POST', body:JSON.stringify({topic:ch, message:msg, title:title, tags:["gem","moneybag"], priority:5})
  }).catch(()=>{});
}

function startLoopCountdown() {
  STATE.loopSecs = 300;
  $('telLoop').textContent = '05:00'; $('telLoop').style.color = 'var(--gold)';
  STATE.loopTimer = setInterval(() => {
    STATE.loopSecs--;
    const m = String(Math.floor(STATE.loopSecs/60)).padStart(2,'0');
    const s = String(STATE.loopSecs%60).padStart(2,'0');
    $('telLoop').textContent = `${m}:${s}`;
    if(STATE.loopSecs <= 0) { clearInterval(STATE.loopTimer); startScan(); }
  }, 1000);
}
function cancelLoop() {
  clearInterval(STATE.loopTimer);
  $('telLoop').textContent = 'OFF'; $('telLoop').style.color = 'var(--text3)';
}

/* ── BOOT ── */
loadState();
$('ev-cards').innerHTML = '<div class="empty"><div class="ei">📡</div><div style="color:var(--text2);font-weight:700">Awaiting Scan</div></div>';
$('arb-cards').innerHTML = '<div class="empty"><div class="ei">🔒</div><div style="color:var(--text2);font-weight:700">Awaiting Scan</div></div>';

</script>
</body>
</html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(HTML.replace('__INJECTED_KEYS__', INJECTED_KEYS_JS).replace('__BUILD_TIME__', build_time))

print(f"✅ Pure SPA Terminal Generated Successfully. ({len(_keys_list)} Keys Injected)")
