"""
Sports Parlay Builder - single-file web app (7+ legs, any sports you add).

Pulls live odds from The Odds API, removes the sportsbook vig, builds a consensus
win probability per pick, and assembles the best parlay across your sports list.

Render settings:
  Build command : pip install flask gunicorn requests
  Start command : gunicorn app:app
Environment variables:
  ODDS_API_KEY   your key from the-odds-api.com
  ACCESS_CODE    optional passcode (protects your free credits)
  CACHE_MINUTES  optional, default 30
"""
import json
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

BASE = "https://api.the-odds-api.com/v4/sports"
API_KEY = os.environ.get("ODDS_API_KEY", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
CACHE_TTL = int(os.environ.get("CACHE_MINUTES", "30")) * 60
MIN_LEGS = 7
VALID_MARKETS = {"h2h", "spreads", "totals"}

DEFAULT_SPORTS = {
    "nfl": "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "mls": "soccer_usa_mls",
    "epl": "soccer_epl",
    "laliga": "soccer_spain_la_liga",
    "seriea": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one",
    "ucl": "soccer_uefa_champs_league",
}

_lock = threading.Lock()
_cache = {}
_active = {"ts": 0.0, "keys": None}
_credits = {"remaining": None}


# ------------------------------------------------------------------ odds data
def active_keys():
    """In-season sport keys (this list call doesn't use odds credits)."""
    if _active["keys"] is None or time.time() - _active["ts"] > 6 * 3600:
        try:
            r = requests.get(BASE, params={"apiKey": API_KEY}, timeout=20)
            if r.status_code == 200:
                _active["keys"] = {s["key"] for s in r.json() if s.get("active")}
                _active["ts"] = time.time()
        except requests.RequestException:
            pass
    return _active["keys"]


def get_events(sport_key, markets, regions, warnings):
    ck = (sport_key, markets, regions)
    with _lock:
        hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], 0
    try:
        r = requests.get(
            f"{BASE}/{sport_key}/odds/",
            params={"apiKey": API_KEY, "regions": regions, "markets": markets,
                    "oddsFormat": "decimal", "dateFormat": "iso"},
            timeout=25,
        )
    except requests.RequestException:
        warnings.append(f"{sport_key}: network error")
        return [], 0
    rem = r.headers.get("x-requests-remaining")
    if rem:
        _credits["remaining"] = rem
    if r.status_code != 200:
        warnings.append(f"{sport_key}: HTTP {r.status_code}")
        return [], 0
    events = r.json()
    with _lock:
        _cache[ck] = (time.time(), events)
    return events, len(markets.split(",")) * len(regions.split(","))


def demo_events(names):
    random.seed(7)
    now = datetime.now(timezone.utc)
    events = []
    for i in range(40):
        sport = names[i % len(names)]
        p = random.uniform(0.35, 0.82)
        books = []
        for b in ["BookA", "BookB", "BookC", "BookD"]:
            q = min(0.95, max(0.05, p + random.uniform(-0.03, 0.03)))
            books.append({"title": b, "markets": [{"key": "h2h", "outcomes": [
                {"name": f"{sport}-H{i}", "price": round(1 / (q * 1.05), 3)},
                {"name": f"{sport}-A{i}", "price": round(1 / ((1 - q) * 1.05), 3)}]}]})
        events.append({"id": f"d{i}", "home_team": f"{sport}-H{i}",
                       "away_team": f"{sport}-A{i}",
                       "commence_time": (now + timedelta(hours=random.randint(1, 20)))
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "bookmakers": books, "_sport": sport})
    return events


# ------------------------------------------------------------------- analysis
def build_legs(events, max_hours, min_books):
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=max_hours)
    legs = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not (now <= start <= cutoff):
            continue
        agg = defaultdict(lambda: {"probs": [], "best": (0.0, "")})
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                outs = mk.get("outcomes", [])
                if len(outs) < 2 or any(o["price"] <= 1 for o in outs):
                    continue
                inv = [1.0 / o["price"] for o in outs]
                total = sum(inv)  # > 1 because of the vig
                for o, imp in zip(outs, inv):
                    a = agg[(mk["key"], o["name"], o.get("point"))]
                    a["probs"].append(imp / total)
                    if o["price"] > a["best"][0]:
                        a["best"] = (o["price"], bk["title"])
        for (market, name, point), a in agg.items():
            if len(a["probs"]) < min_books:
                continue
            prob = sum(a["probs"]) / len(a["probs"])
            odds, book = a["best"]
            legs.append({
                "event_id": ev["id"],
                "game": f'{ev["away_team"]} @ {ev["home_team"]}',
                "sport": ev.get("_sport", ""), "start": start, "market": market,
                "pick": name + (f" {point:+g}" if market == "spreads" else
                                f" {point:g}" if market == "totals" else ""),
                "prob": prob, "odds": odds, "book": book,
                "edge": prob * odds - 1,
            })
    return legs


def pick_parlay(legs, n_legs, min_prob, allow_neg_ev, per_sport_min):
    pool = [l for l in legs if l["prob"] >= min_prob]
    if not allow_neg_ev:
        pool = [l for l in pool if l["edge"] >= -0.005]
    pool.sort(key=lambda l: (l["prob"], l["edge"]), reverse=True)

    chosen, used = [], set()

    def take(l):
        chosen.append(l)
        used.add(l["event_id"])  # one leg per game keeps picks independent

    by_sport = defaultdict(list)
    for l in pool:
        by_sport[l["sport"]].append(l)
    for _ in range(per_sport_min):          # pass 1: represent every sport
        for ls in by_sport.values():
            if len(chosen) >= n_legs:
                break
            for l in ls:
                if l["event_id"] not in used:
                    take(l)
                    break
    for l in pool:                          # pass 2: best legs overall
        if len(chosen) >= n_legs:
            break
        if l["event_id"] not in used:
            take(l)

    chosen.sort(key=lambda l: l["prob"], reverse=True)
    alts = [l for l in pool if l["event_id"] not in used][:6]
    return chosen, alts


def american(dec):
    return f"+{round((dec - 1) * 100)}" if dec >= 2 else f"-{round(100 / (dec - 1))}"


# --------------------------------------------------------------------- routes
def authorized():
    return not ACCESS_CODE or request.headers.get("X-Access-Code", "") == ACCESS_CODE


@app.post("/api/parlay")
def api_parlay():
    if not authorized():
        return jsonify(error="Wrong access code."), 401
    d = request.get_json(force=True, silent=True) or {}
    sports = d.get("sports") or {}
    if not isinstance(sports, dict) or not sports or len(sports) > 30:
        return jsonify(error="Pick between 1 and 30 sports."), 400
    try:
        markets = [m for m in d.get("markets", ["h2h"]) if m in VALID_MARKETS] or ["h2h"]
        mk = ",".join(markets)
        legs_n = max(MIN_LEGS, min(int(d.get("legs", 7)), 20))
        hours = max(1, min(float(d.get("hours", 24)), 168))
        min_prob = max(0.5, min(float(d.get("min_prob", 0.55)), 0.95))
        per_sport = max(0, min(int(d.get("per_sport_min", 1)), 3))
    except (TypeError, ValueError):
        return jsonify(error="Bad settings."), 400
    allow_neg = bool(d.get("allow_negative_ev", False))
    demo = bool(d.get("demo", False))

    warnings, events, spent, skipped = [], [], 0, []
    if demo:
        events = demo_events(list(sports))
    else:
        if not API_KEY:
            return jsonify(error="ODDS_API_KEY is not set on the server."), 500
        active = active_keys()
        for name, key in sports.items():
            if active is not None and key not in active:
                skipped.append(name)      # out of season, costs nothing
                continue
            evs, cost = get_events(key, mk, "us", warnings)
            spent += cost
            for ev in evs:
                ev["_sport"] = name
            events.extend(evs)

    legs = build_legs(events, hours, 2 if demo else 3)
    parlay, alts = pick_parlay(legs, legs_n, min_prob, allow_neg, per_sport)

    def ser(l):
        return {"sport": l["sport"], "game": l["game"], "market": l["market"],
                "pick": l["pick"], "prob": round(l["prob"], 4), "odds": l["odds"],
                "american": american(l["odds"]), "book": l["book"],
                "edge": round(l["edge"], 4), "start": l["start"].isoformat()}

    p = o = 1.0
    for l in parlay:
        p *= l["prob"]
        o *= l["odds"]
    return jsonify(
        legs=[ser(l) for l in parlay], alternates=[ser(l) for l in alts],
        requested=legs_n, combined_prob=round(p, 5) if parlay else 0,
        combined_odds=american(o) if parlay else "", multiplier=round(o, 2),
        ev=round(p * o - 1, 4) if parlay else 0,
        games=len(events), candidates=len(legs), skipped=skipped,
        warnings=warnings, credits_spent=spent,
        credits_remaining=_credits["remaining"], cache_minutes=CACHE_TTL // 60,
    )


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/manifest.json")
def manifest():
    return jsonify(name="Sports Parlay Builder", short_name="Parlays", start_url="/",
                   display="standalone", background_color="#0f1720",
                   theme_color="#0f1720")


@app.get("/")
def index():
    html = (PAGE.replace("__DEFAULTS__", json.dumps(DEFAULT_SPORTS))
                .replace("__NEEDS_CODE__", "true" if ACCESS_CODE else "false"))
    return Response(html, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Parlays">
<meta name="theme-color" content="#0f1720"><link rel="manifest" href="/manifest.json">
<title>Sports Parlay Builder</title><style>
:root{--bg:#0f1720;--card:#18222e;--line:#263444;--tx:#e8eef5;--mut:#8ea0b3;--ac:#2ecc71;--bad:#e5534b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.4 -apple-system,system-ui,sans-serif;
padding:max(14px,env(safe-area-inset-top)) 14px max(30px,env(safe-area-inset-bottom))}
h1{font-size:22px;margin:6px 0 12px}h2{font-size:15px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
label{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:8px 0}
input,select{background:#0f1720;color:var(--tx);border:1px solid var(--line);border-radius:9px;padding:9px;font-size:16px;max-width:55%}
input[type=checkbox]{width:22px;height:22px;max-width:none}
button{background:var(--ac);color:#04210f;border:0;border-radius:12px;padding:14px;font-size:17px;font-weight:700;width:100%}
button.sec{background:#243344;color:var(--tx);font-size:14px;padding:10px;width:auto}
button:disabled{opacity:.5}.row{display:flex;gap:8px;flex-wrap:wrap}.chip{display:flex;align-items:center;gap:6px;background:#0f1720;
border:1px solid var(--line);border-radius:20px;padding:6px 10px;font-size:14px}.chip span.x{color:var(--bad);font-weight:700;padding-left:4px}
.leg{border-top:1px solid var(--line);padding:10px 0}.leg:first-of-type{border-top:0}.tag{font-size:12px;color:var(--mut)}
.pick{font-weight:700}.pos{color:var(--ac)}.neg{color:var(--bad)}.sum{font-size:15px}.sum b{font-size:19px}
.warn{color:#f0b429;font-size:13px;margin-top:6px}.small{font-size:12px;color:var(--mut)}
</style></head><body>
<h1>Sports Parlay Builder</h1>
<div class="card" id="codeCard" style="display:none"><label>Access code <input id="code" type="password"></label></div>
<div class="card"><h2>Your sports</h2><div class="row" id="chips"></div>
<div class="row" style="margin-top:10px"><input id="newName" placeholder="name e.g. atp" style="flex:1;max-width:none">
<input id="newKey" placeholder="API key e.g. tennis_atp_wimbledon" style="flex:2;max-width:none"></div>
<div class="row" style="margin-top:8px"><button class="sec" onclick="addSport()">Add sport</button>
<button class="sec" onclick="resetSports()">Reset list</button></div>
<div class="small" style="margin-top:6px">Find sport keys at the-odds-api.com/sports-odds-data/sports-apis.html</div></div>
<div class="card"><h2>Settings</h2>
<label>Legs (7 min)<input id="legs" type="number" min="7" max="20" value="7"></label>
<label>Games within (hours)<input id="hours" type="number" min="1" max="168" value="24"></label>
<label>Min win % per leg<input id="minp" type="number" min="50" max="95" value="55"></label>
<label>Min legs per sport<select id="per"><option>0</option><option selected>1</option><option>2</option></select></label>
<label>Moneyline<input id="m_h2h" type="checkbox" checked></label>
<label>Spreads<input id="m_spreads" type="checkbox"></label>
<label>Totals<input id="m_totals" type="checkbox"></label>
<label>Allow negative-EV legs<input id="neg" type="checkbox"></label>
<label>Demo mode (fake data, free)<input id="demo" type="checkbox"></label></div>
<button id="go" onclick="build()">Build my parlay</button>
<div id="out" style="margin-top:14px"></div>
<p class="small">Odds are reused for a while to protect your free API credits. No model guarantees a win, and every added leg multiplies the sportsbook's margin. Only bet what you can afford to lose. Help: 1-800-GAMBLER.</p>
<script>
const DEFAULTS=__DEFAULTS__, NEEDS_CODE=__NEEDS_CODE__;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let sports=JSON.parse(localStorage.getItem('sports')||'null')||Object.fromEntries(Object.entries(DEFAULTS).map(([k,v])=>[k,{key:v,on:true}]));
function save(){localStorage.setItem('sports',JSON.stringify(sports));draw()}
function draw(){$('chips').innerHTML=Object.entries(sports).map(([n,v])=>
`<div class="chip"><input type="checkbox" ${v.on?'checked':''} onchange="sports['${esc(n)}'].on=this.checked;save()"> ${esc(n.toUpperCase())}<span class="x" onclick="delete sports['${esc(n)}'];save()">&times;</span></div>`).join('')}
function addSport(){const n=$('newName').value.trim().toLowerCase(),k=$('newKey').value.trim();
if(!/^[a-z0-9_-]+$/.test(n)||!/^[a-z0-9_]+$/.test(k))return alert('Use letters, numbers and underscores only.');
sports[n]={key:k,on:true};$('newName').value=$('newKey').value='';save()}
function resetSports(){localStorage.removeItem('sports');location.reload()}
if(NEEDS_CODE){$('codeCard').style.display='';$('code').value=localStorage.getItem('code')||''}
async function build(){
 const btn=$('go'),out=$('out');btn.disabled=true;btn.textContent='Crunching odds...';out.innerHTML='';
 const chosen={};for(const [n,v] of Object.entries(sports))if(v.on)chosen[n]=v.key;
 const markets=['h2h','spreads','totals'].filter(m=>$('m_'+m).checked);
 if(NEEDS_CODE)localStorage.setItem('code',$('code').value);
 try{
  const r=await fetch('/api/parlay',{method:'POST',headers:{'Content-Type':'application/json','X-Access-Code':$('code').value},
   body:JSON.stringify({sports:chosen,markets,legs:+$('legs').value,hours:+$('hours').value,min_prob:+$('minp').value/100,
   per_sport_min:+$('per').value,allow_negative_ev:$('neg').checked,demo:$('demo').checked})});
  const d=await r.json();if(!r.ok)throw new Error(d.error||'Request failed');render(d)
 }catch(e){out.innerHTML=`<div class="card neg">${esc(e.message)}</div>`}
 btn.disabled=false;btn.textContent='Build my parlay'}
function render(d){
 let h='<div class="card">';
 if(d.legs.length<d.requested)h+=`<div class="warn">Only ${d.legs.length} qualifying legs found. Try more hours, a lower min win %, more markets, or negative-EV legs.</div>`;
 d.legs.forEach((l,i)=>{h+=`<div class="leg"><div class="tag">${i+1}. ${esc(l.sport.toUpperCase())} &middot; ${esc(l.market)} &middot; ${new Date(l.start).toLocaleString([],{weekday:'short',hour:'numeric',minute:'2-digit'})}</div>
 <div>${esc(l.game)}</div><div class="pick">${esc(l.pick)}</div>
 <div class="tag">${(l.prob*100).toFixed(1)}% win &middot; best ${esc(l.american)} @ ${esc(l.book)} &middot; <span class="${l.edge>=0?'pos':'neg'}">edge ${(l.edge*100).toFixed(1)}%</span></div></div>`});
 if(d.legs.length)h+=`<div class="sum" style="margin-top:10px">Combined win chance <b>${(d.combined_prob*100).toFixed(2)}%</b> (1 in ${Math.round(1/d.combined_prob)})<br>
 Best-price payout <b>${esc(d.combined_odds)}</b> (${d.multiplier}x) &middot; EV per $1 <span class="${d.ev>=0?'pos':'neg'}">${d.ev>=0?'+':''}${d.ev.toFixed(3)}</span>
 <div class="small">A real sportsbook parlay uses one book's odds, so your actual payout will be lower.</div></div>`;
 h+='</div>';
 if(d.alternates.length){h+='<div class="card"><h2>Alternates</h2>'+d.alternates.map(l=>`<div class="leg"><div class="tag">${esc(l.sport.toUpperCase())} &middot; ${esc(l.game)}</div>
 <div class="pick">${esc(l.pick)}</div><div class="tag">${(l.prob*100).toFixed(1)}% @ ${esc(l.american)}</div></div>`).join('')+'</div>'}
 h+=`<div class="small">${d.games} games, ${d.candidates} candidate legs. Credits used this run: ${d.credits_spent}${d.credits_remaining?`, remaining this month: ${esc(d.credits_remaining)}`:''}. Odds cached ${d.cache_minutes} min.`;
 if(d.skipped.length)h+=` Out of season (skipped, free): ${esc(d.skipped.join(', ').toUpperCase())}.`;
 h+='</div>';d.warnings.forEach(w=>h+=`<div class="warn">${esc(w)}</div>`);
 $('out').innerHTML=h}
draw();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
