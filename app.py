"""
Jev Ticket Triage — a simple support-ticket triage app powered by
TypeSafe AI's Jev (System One) model, ready to deploy on DigitalOcean App Platform.

How it works:
  1. Paste a customer message into the UI (or POST it to /api/triage).
  2. One call to Jev evaluates three typed questions in parallel:
       - department  (Choice): which team should handle this
       - frustration (Score):  how upset the customer is
       - is_urgent   (Noul):   is this time-sensitive
  3. Plain Python then branches on the structured answers:
     confidence gates auto-routing, and urgency + frustration set priority.

Env vars:
  TYPESAFE_API_KEY  — your TypeSafe API key (required for live calls).
  JEV_MODEL         — optional, defaults to "jev-latest".
  MOCK_JEV=1        — optional, run without an API key using a canned response.
"""

import os
import time
from collections import deque

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

# The typed questions we ask Jev about every ticket.
QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle this ticket",
        "criteria": {
            "billing": "Payments, invoices, refunds, or subscription issues",
            "technical": "Bugs, errors, outages, or integration problems",
            "sales": "Pricing, plans, upgrades, or account questions",
            "abuse": "Spam, security incidents, or policy violations",
        },
    },
    "frustration": {
        "type": "score",
        "instructions": "How frustrated the customer appears",
        "criteria": [
            "Calm, just stating facts",
            "Frustrated but civil",
            "Very angry, strong language",
        ],
    },
    "is_urgent": {
        "type": "noul",
        "instructions": "The message conveys urgency or time-sensitivity",
    },
}

# If Jev's routing confidence is below this, send the ticket to a human.
ROUTING_CONFIDENCE_THRESHOLD = 0.60

# Recent triage results, newest first. In-memory: fine for a single instance.
HISTORY = deque(maxlen=200)


def call_jev(state: str) -> dict:
    """One request to Jev: state in, typed answers + probabilities out."""
    if os.environ.get("MOCK_JEV") == "1":
        return _mock_answers()

    resp = requests.post(
        JEV_API_URL,
        headers={
            "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={"state": state, "model": JEV_MODEL, "questions": QUESTIONS},
        timeout=(5, 15),  # 5s to connect, 15s to respond — well under DO's gateway timeout
    )
    resp.raise_for_status()
    return resp.json()


def triage(state: str) -> dict:
    """Call Jev, branch on its structured answers, and record the result."""
    result = call_jev(state)
    answers = result["answers"]

    department = answers["department"]
    frustration = answers["frustration"]
    urgency = answers["is_urgent"]["noul"]

    # Confidence gate: only auto-route when Jev is sure enough.
    auto_route = department["confidence"] >= ROUTING_CONFIDENCE_THRESHOLD
    queue = department["choice"] if auto_route else "human_review"

    # Priority: urgency and frustration combined, weighted toward urgency.
    priority_score = 0.7 * urgency + 0.3 * (frustration["score"] / 2.0)
    if priority_score >= 0.65:
        priority = "P1"
    elif priority_score >= 0.35:
        priority = "P2"
    else:
        priority = "P3"

    out = {
        "queue": queue,
        "auto_routed": auto_route,
        "priority": priority,
        "priority_score": round(priority_score, 3),
        "model": result.get("model", JEV_MODEL),
        "answers": answers,
        "usage": result.get("usage"),
    }
    HISTORY.appendleft({"ts": time.time(), "message": state, "result": out})
    return out


def _mock_answers() -> dict:
    """Canned Jev-shaped response so the app runs without an API key."""
    return {
        "model": "jev-mock",
        "answers": {
            "department": {
                "type": "choice",
                "choice": "technical",
                "confidence": 0.78,
                "probabilities": {"technical": 0.85, "billing": 0.15, "sales": 0.0, "abuse": 0.0},
            },
            "frustration": {
                "type": "score",
                "score": 1.0,
                "confidence": 1.0,
                "legend": {
                    "0": "Calm, just stating facts",
                    "1": "Frustrated but civil",
                    "2": "Very angry, strong language",
                },
                "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0},
            },
            "is_urgent": {"type": "noul", "noul": 1.0},
        },
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


@app.post("/api/triage")
def api_triage():
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Provide a non-empty 'message' field."}), 400
    try:
        return jsonify(triage(message))
    except requests.HTTPError as e:
        return jsonify({"error": f"Jev API error: {e.response.status_code} {e.response.text[:300]}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach the Jev API: {e.__class__.__name__}: {e}"}), 502
    except KeyError:
        return jsonify({"error": "TYPESAFE_API_KEY is not set (or set MOCK_JEV=1 to demo)."}), 500
    except Exception as e:  # last resort: always answer JSON, never an HTML error page
        return jsonify({"error": f"Unexpected server error: {e.__class__.__name__}: {e}"}), 500


@app.get("/api/history")
def api_history():
    """Step back through previous queries: /api/history?offset=0 is the newest."""
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    total = len(HISTORY)
    if total == 0:
        return jsonify({"total": 0, "offset": 0, "item": None})
    offset = min(offset, total - 1)
    return jsonify({"total": total, "offset": offset, "item": HISTORY[offset]})


@app.get("/api/diag")
def api_diag():
    """Probe each step of the path to the Jev API from inside this container."""
    import concurrent.futures
    import socket

    report = {}
    key = os.environ.get("TYPESAFE_API_KEY", "")
    report["env"] = {
        "TYPESAFE_API_KEY": f"set ({len(key)} chars)" if key.strip() else "MISSING or blank",
        "JEV_MODEL": JEV_MODEL,
    }

    # 1. DNS (getaddrinfo has no timeout of its own, so run it in a thread)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(socket.getaddrinfo, "api.typesafe.ai", 443)
        try:
            infos = fut.result(timeout=4)
            report["dns"] = {"ok": True, "ms": int((time.time() - t0) * 1000),
                             "ips": sorted({i[4][0] for i in infos})}
        except Exception as e:
            report["dns"] = {"ok": False, "ms": int((time.time() - t0) * 1000),
                             "error": f"{e.__class__.__name__}: {e}"}

    # 2. TCP connect to port 443
    t0 = time.time()
    try:
        with socket.create_connection(("api.typesafe.ai", 443), timeout=4):
            report["tcp_443"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        report["tcp_443"] = {"ok": False, "ms": int((time.time() - t0) * 1000),
                             "error": f"{e.__class__.__name__}: {e}"}

    # 3. Real API call with a tiny question
    t0 = time.time()
    try:
        r = requests.post(
            JEV_API_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"state": "diag", "model": JEV_MODEL,
                  "questions": {"u": {"type": "noul", "instructions": "Is this urgent?"}}},
            timeout=(4, 10),
        )
        report["api_call"] = {"ok": r.ok, "ms": int((time.time() - t0) * 1000),
                              "status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        report["api_call"] = {"ok": False, "ms": int((time.time() - t0) * 1000),
                              "error": f"{e.__class__.__name__}: {e}"}

    return jsonify(report)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev Ticket Triage</title>
<style>
  :root {
    --do-blue:#0080FF; --do-blue-dark:#0069E0; --do-navy:#031B4D;
    --ink:#031B4D; --muted:#5B6987; --bg:#F1F4F8; --line:#DFE7F1;
    --ok-bg:#E6F7EE; --ok-fg:#15803D; --warn-bg:#FDECEC; --warn-fg:#C53030;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         "Helvetica Neue", sans-serif; background:var(--bg); color:var(--ink);
         -webkit-font-smoothing:antialiased; }
  .hero { background:linear-gradient(120deg, #031B4D 0%, #0053CC 55%, #0080FF 100%);
          padding:48px 20px 104px; }
  .hero-inner { max-width:920px; margin:0 auto; }
  .eyebrow { display:inline-block; font-size:12px; font-weight:700; letter-spacing:.1em;
             text-transform:uppercase; color:#9DC9FF; margin-bottom:10px; }
  h1 { font-size:32px; margin:0 0 8px; color:#fff; letter-spacing:-.02em; }
  .sub { color:rgba(255,255,255,.78); margin:0; font-size:15px; line-height:1.55; max-width:660px; }
  .wrap { max-width:920px; margin:-60px auto 0; padding:0 20px 72px; }
  .panel { background:#fff; border:1px solid var(--line); border-radius:16px; padding:22px;
           box-shadow:0 12px 32px rgba(3,27,77,.10); }
  textarea { width:100%; min-height:130px; padding:14px 16px; border:1px solid #C9D6E8;
             border-radius:10px; font-size:15px; font-family:inherit; resize:vertical;
             background:#FBFDFF; color:var(--ink); }
  textarea:focus { outline:2px solid var(--do-blue); border-color:transparent; background:#fff; }
  .row { display:flex; gap:10px; margin-top:14px; flex-wrap:wrap; align-items:center; }
  button { background:var(--do-blue); color:#fff; border:0; border-radius:8px; padding:11px 22px;
           font-size:15px; font-weight:600; cursor:pointer; transition:background .15s, box-shadow .15s; }
  button:hover:not(:disabled) { background:var(--do-blue-dark); box-shadow:0 4px 12px rgba(0,128,255,.30); }
  button:disabled { opacity:.45; cursor:default; }
  .sample, .nav { background:#fff; border:1px solid #C9D6E8; color:var(--ink);
                  font-weight:500; font-size:13px; padding:9px 16px; }
  .sample:hover:not(:disabled), .nav:hover:not(:disabled) { background:#F3F8FF; box-shadow:none; }
  .nav { font-weight:600; color:var(--do-blue); }
  .histinfo { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:16px; margin-top:24px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:18px 18px 16px;
          box-shadow:0 4px 14px rgba(3,27,77,.06); }
  .card h3 { margin:0 0 8px; font-size:11px; text-transform:uppercase; letter-spacing:.1em;
             color:var(--muted); font-weight:700; }
  .big { font-size:24px; font-weight:700; letter-spacing:-.01em; }
  .pr-p1 { color:var(--warn-fg); } .pr-p2 { color:#B45309; } .pr-p3 { color:var(--ok-fg); }
  .tag { display:inline-block; padding:3px 12px; border-radius:999px; font-size:12px;
         font-weight:600; margin-top:8px; }
  .ok { background:var(--ok-bg); color:var(--ok-fg); } .warn { background:var(--warn-bg); color:var(--warn-fg); }
  .bars { margin-top:12px; }
  .bar { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); margin-top:5px; }
  .bar .track { flex:1; height:8px; background:#EAF0F8; border-radius:4px; overflow:hidden; }
  .bar .fill { height:100%; background:linear-gradient(90deg, var(--do-blue), #00A8E8); border-radius:4px; }
  .meta { margin-top:20px; font-size:12.5px; color:var(--muted); }
  .err { margin-top:20px; color:var(--warn-fg); background:var(--warn-bg);
         border:1px solid #F5C6C6; border-radius:10px; padding:12px 16px; font-size:14px; }
  .footer { margin-top:28px; font-size:12.5px; color:var(--muted); text-align:center; }
  .footer a { color:var(--do-blue); text-decoration:none; }
</style>
</head>
<body>
<div class="hero">
  <div class="hero-inner">
    <span class="eyebrow">DigitalOcean App Platform &middot; TypeSafe AI</span>
    <h1>Jev Ticket Triage</h1>
    <p class="sub">Support tickets triaged by Jev, the first System One model — typed decisions with
       calibrated probabilities, no text generation. Route, prioritize, and branch in code.</p>
  </div>
</div>
<div class="wrap">
  <div class="panel">
    <textarea id="msg" placeholder="Paste a customer message here..."></textarea>
    <div class="row">
      <button id="go" onclick="run()">Triage ticket</button>
      <button class="sample" onclick="sample(0)">Integration failure</button>
      <button class="sample" onclick="sample(1)">Billing question</button>
      <button class="sample" onclick="sample(2)">Angry outage</button>
      <span style="flex:1"></span>
      <button class="nav" id="prev" onclick="nav(1)">&#9664; Previous</button>
      <button class="nav" id="next" onclick="nav(-1)" disabled>Next &#9654;</button>
      <span class="histinfo" id="histinfo"></span>
    </div>
  </div>

  <div id="out"></div>
  <div class="footer">Decisions by <a href="https://typesafe.ai">Jev</a> &middot; running on
    <a href="https://www.digitalocean.com/products/app-platform">DigitalOcean App Platform</a></div>
</div>

<script>
const SAMPLES = [
  "Hi, I've been trying to connect my Stripe account for 3 days and the integration keeps failing. I'm losing sales. Please help ASAP.",
  "Hello! Quick question — if I upgrade to the annual plan, is the discount applied to the seats I already pay for?",
  "This is the THIRD outage this month. My whole site is down AGAIN and support keeps ignoring me. Absolutely unacceptable."
];
function sample(i){ document.getElementById('msg').value = SAMPLES[i]; histOffset = -1; updateNav(histTotal); }

let histOffset = -1;   // -1 = live mode (not browsing history)
let histTotal = 0;

function bars(probs){
  return Object.entries(probs).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`
    <div class="bar"><span style="width:74px">${k}</span>
      <div class="track"><div class="fill" style="width:${Math.round(v*100)}%"></div></div>
      <span>${Math.round(v*100)}%</span></div>`).join('');
}

function render(d, extraMeta){
  const dep = d.answers.department, fr = d.answers.frustration, urg = d.answers.is_urgent;
  document.getElementById('out').innerHTML = `
    <div class="cards">
      <div class="card"><h3>Queue</h3><div class="big">${d.queue}</div>
        <span class="tag ${d.auto_routed?'ok':'warn'}">${d.auto_routed?'auto-routed':'needs human review'}</span>
        <div class="bars">${bars(dep.probabilities)}</div>
        <div class="meta">confidence ${Math.round(dep.confidence*100)}%</div></div>
      <div class="card"><h3>Priority</h3><div class="big pr-${d.priority.toLowerCase()}">${d.priority}</div>
        <div class="meta">score ${d.priority_score}</div></div>
      <div class="card"><h3>Frustration</h3>
        <div class="big">${fr.legend ? fr.legend[String(fr.score)] ?? fr.score : fr.score}</div>
        <div class="bars">${bars(fr.probabilities)}</div></div>
      <div class="card"><h3>Urgent?</h3><div class="big">${Math.round(urg.noul*100)}%</div>
        <div class="meta">Noul: probability the ticket is time-sensitive</div></div>
    </div>
    <div class="meta">model: ${d.model}${extraMeta || ''}</div>`;
}

function updateNav(total){
  histTotal = total;
  document.getElementById('prev').disabled = !(histOffset + 1 < histTotal || (histOffset === -1 && histTotal > 0));
  document.getElementById('next').disabled = histOffset <= 0;
  document.getElementById('histinfo').textContent = histOffset >= 0 ? `${histOffset + 1} of ${histTotal}` : '';
}

async function nav(step){
  const target = histOffset === -1 ? 0 : histOffset + step;
  const r = await fetch('/api/history?offset=' + target);
  const d = await r.json();
  if (!d.item){ updateNav(d.total); return; }
  histOffset = d.offset;
  document.getElementById('msg').value = d.item.message;
  const when = new Date(d.item.ts * 1000).toLocaleString();
  render(d.item.result, ` &middot; ${when}`);
  updateNav(d.total);
}

async function run(){
  const btn = document.getElementById('go');
  const message = document.getElementById('msg').value.trim();
  if(!message) return;
  btn.disabled = true; btn.textContent = 'Asking Jev...';
  try{
    const t0 = performance.now();
    const r = await fetch('/api/triage', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({message})});
    const raw = await r.text();
    let d;
    try { d = JSON.parse(raw); }
    catch { throw new Error(`Server returned ${r.status}: ${raw.replace(/<[^>]*>/g,' ').trim().slice(0,200)}`); }
    const ms = Math.round(performance.now() - t0);
    if(!r.ok) throw new Error(d.error || 'Request failed');
    histOffset = -1;
    render(d, ` &middot; round-trip: ${ms}ms${d.usage ? ` &middot; ${d.usage.input_tokens} input tokens` : ''}`);
    const h = await (await fetch('/api/history?offset=0')).json();
    updateNav(h.total);
  }catch(e){
    document.getElementById('out').innerHTML = `<div class="err">${e.message}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = 'Triage ticket';
  }
}

// On load, show how much history exists.
fetch('/api/history?offset=0').then(r=>r.json()).then(d=>updateNav(d.total));
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
