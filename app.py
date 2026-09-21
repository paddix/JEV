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
        timeout=30,
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
  :root { --blue:#0069ff; --ink:#0b1736; --muted:#5b6b8c; --bg:#f4f7fb; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width:860px; margin:0 auto; padding:32px 20px 60px; }
  h1 { font-size:26px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; font-size:14px; }
  textarea { width:100%; min-height:130px; padding:14px; border:1px solid #d3ddef; border-radius:10px;
             font-size:15px; font-family:inherit; resize:vertical; }
  textarea:focus { outline:2px solid var(--blue); border-color:transparent; }
  .row { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; align-items:center; }
  button { background:var(--blue); color:#fff; border:0; border-radius:8px; padding:11px 22px;
           font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .sample, .nav { background:#fff; border:1px solid #d3ddef; color:var(--ink); font-weight:400; font-size:13px; }
  .nav { font-weight:600; }
  .histinfo { font-size:13px; color:var(--muted); }
  .cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr)); gap:14px; margin-top:26px; }
  .card { background:#fff; border:1px solid #e1e8f5; border-radius:12px; padding:16px; }
  .card h3 { margin:0 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  .big { font-size:22px; font-weight:700; }
  .tag { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; margin-top:6px; }
  .ok { background:#e5f3e8; color:#1a7f37; } .warn { background:#fdeeee; color:#c62828; }
  .bars { margin-top:10px; }
  .bar { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); margin-top:4px; }
  .bar .track { flex:1; height:8px; background:#edf1f8; border-radius:4px; overflow:hidden; }
  .bar .fill { height:100%; background:var(--blue); border-radius:4px; }
  .meta { margin-top:18px; font-size:12px; color:var(--muted); }
  .err { margin-top:18px; color:#c62828; font-size:14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Jev Ticket Triage</h1>
  <p class="sub">Support tickets triaged by TypeSafe AI's Jev — typed decisions with calibrated probabilities, no text generation. Running on DigitalOcean App Platform.</p>

  <textarea id="msg" placeholder="Paste a customer message here..."></textarea>
  <div class="row">
    <button id="go" onclick="run()">Triage ticket</button>
    <button class="sample" onclick="sample(0)">Sample: integration failure</button>
    <button class="sample" onclick="sample(1)">Sample: billing question</button>
    <button class="sample" onclick="sample(2)">Sample: angry outage</button>
    <span style="flex:1"></span>
    <button class="nav" id="prev" onclick="nav(1)">&#9664; Previous</button>
    <button class="nav" id="next" onclick="nav(-1)" disabled>Next &#9654;</button>
    <span class="histinfo" id="histinfo"></span>
  </div>

  <div id="out"></div>
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
      <div class="card"><h3>Priority</h3><div class="big">${d.priority}</div>
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
