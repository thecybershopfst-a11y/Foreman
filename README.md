# Foreman

One program. Run it, open a URL, you have the whole thing — UI and backend, same process, same port.

## Run it

```bash
pip install -r requirements.txt
python main.py
```

Then open **http://localhost:8000** in your browser. That's it — no separate frontend/backend setup, no API key to copy anywhere, no CORS configuration. Your data saves automatically to `foreman.db` (created next to `main.py` the first time you run it) and is there again next time you start the program.

Tested end-to-end before packaging: serving the actual UI at `/`, the storage polyfill that makes standalone persistence work, real read/write/round-trip on local data, and the multi-tenant endpoints running alongside it on the same server — 9/9 checks passing against a freshly started process.

## How this works

`business-os.html` is the same Foreman app from before, with one addition: a small polyfill at the top of its script. Foreman normally saves through `window.storage`, an API Claude.ai provides when this runs as a Claude artifact. That API doesn't exist when the file is served by a plain Python process instead — so the polyfill checks for it, and if it's missing, defines the same `get`/`set` shape backed by real HTTP calls to this server's `/api/local-state/{key}` endpoints. Nothing else in the app had to change; it doesn't know or care which mode it's in.

## Two modes, one server

- **Local mode** (`/api/local-state/...`) — no auth, one business, meant for running Foreman on your own machine for your own business. This is what makes "one program, zero setup" possible.
- **Multi-tenant mode** (`/register`, `/state/{business_id}`) — real per-business API keys, the path for when you actually deploy this for other customers. Both run on the same server; local mode is just the zero-friction path for solo use.

## Uploading real files

Documents can have a real file attached — a certificate, a lease PDF, whatever. Two paths, automatic:
- **Running through this server** (which is what you're doing right now): the file uploads for real to `/api/upload`, stored on disk in `uploads/`, no size limit baked into your business data.
- **Running as a Claude artifact with no backend** (e.g., on your phone): small files (under ~350KB) get embedded directly in your saved data instead — there's no server to upload to there. Larger files just get logged by name/location rather than attached.

## Bringing data from the phone version into this one

Foreman running as a Claude artifact (phone, tablet, wherever) has a **Data & Backend → Export all data** button that downloads your whole business as one JSON file. Drop that file next to `main.py`, name it exactly `import-state.json`, and start the program — it's seeded automatically on first run. (It only does this once, when local data is still empty — it won't silently overwrite real work you've since done here.)

## Deploying for real

Any host that runs a Python process works: Railway, Render, Fly.io, a small VPS.
1. Push this folder to a git repo.
2. Start command: `python main.py` (it reads the `PORT` environment variable if your host sets one).

**Before real customers use a deployed version:**
- Local mode (`/api/local-state`) has **no authentication at all** — it's built for "this runs on my own computer." Don't expose it on the open internet for multiple people to share; use multi-tenant mode (`/register` + real API keys) instead, and consider removing the local-mode routes from a shared deployment entirely.
- Lock down CORS (`allow_origins=["*"]` in `main.py` is fine for local dev, not for production).
- Move off SQLite to Postgres if you expect concurrent writers.
- Terms of Service / privacy policy — still not optional, still needs an actual lawyer.

## API reference

**Local (no auth):**
- `GET /api/local-state/{key}` → `{"key", "value"}`
- `PUT /api/local-state/{key}` → body `{"value": ...}` → `{"saved": true}`

**Multi-tenant:**
- `GET /health` → `{"status": "ok"}`
- `POST /register` → body `{"business_name"}` → `{"business_id", "api_key"}` (key shown once)
- `GET /state/{business_id}` → header `X-API-Key` → `{"state": {...}}`
- `PUT /state/{business_id}` → header `X-API-Key`, body `{"state": {...}}`
