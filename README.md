# Jev Ticket Triage

A simple support-ticket triage app powered by [TypeSafe AI's Jev](https://typesafe.ai) — the new System One model that returns typed decisions with calibrated probabilities instead of generated text — deployed on DigitalOcean App Platform.

Paste a customer message and one Jev call answers three typed questions in parallel:

| Question | Primitive | What it returns |
|---|---|---|
| Which team should handle this? | Choice | `choice` + probabilities + confidence |
| How frustrated is the customer? | Score | 0–2 score on a rubric + confidence |
| Is this urgent? | Noul | probability (0–1) the statement is true |

The app then branches on the structured answers in plain Python: tickets are auto-routed only when Jev's routing confidence is ≥ 0.60 (otherwise they go to `human_review`), and priority (P1–P3) is computed as `0.7 × urgency + 0.3 × frustration`. That confidence gate is the core Jev pattern — the model tells you when it isn't sure, so your code can decide what to do about it.

## Run locally

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY=your_key_here   # from https://console.typesafe.ai/keys
python app.py
# open http://localhost:8080
```

No key handy? `MOCK_JEV=1 python app.py` runs the whole app with a canned Jev-shaped response.

## API

```bash
curl -s -X POST http://localhost:8080/api/triage \
  -H "Content-Type: application/json" \
  -d '{"message": "My site is down and I am losing sales. Fix this now!"}'
```

Returns the routing decision plus Jev's raw answers:

```json
{
  "queue": "technical",
  "auto_routed": true,
  "priority": "P1",
  "priority_score": 0.85,
  "answers": { "department": {...}, "frustration": {...}, "is_urgent": {...} }
}
```

## Deploy to DigitalOcean App Platform

1. Push this folder to a GitHub repo (e.g. `jev-ticket-triage`).
2. Edit `.do/app.yaml` and set `github.repo` to your repo.
3. Create the app:

   ```bash
   doctl apps create --spec .do/app.yaml
   ```

   Or in the UI: **Create → App Platform → GitHub repo**, then paste this spec under *Edit App Spec*.
4. Set the `TYPESAFE_API_KEY` secret (UI: app **Settings → App-Level Environment Variables**, mark as encrypted), or:

   ```bash
   doctl apps update <APP_ID> --spec .do/app.yaml   # after putting the key in the spec's value field
   ```
5. App Platform builds with the Python buildpack, runs gunicorn, health-checks `/healthz`, and gives you a live `*.ondigitalocean.app` URL. Pushes to `main` auto-deploy.

## Files

- `app.py` — Flask app: UI + `/api/triage` endpoint + Jev call
- `requirements.txt` — flask, requests, gunicorn
- `.do/app.yaml` — App Platform spec

## Notes

- Jev is in early access; get a key at https://console.typesafe.ai/keys.
- Model defaults to `jev-latest`; override with the `JEV_MODEL` env var.
- Jev pricing: $0.042/MTok input, output free — each triage call is a fraction of a cent, with ~70–500ms latency.
