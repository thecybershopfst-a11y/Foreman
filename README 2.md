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

## Data engineer audit (this deployment, checked against Render's actual docs)

**Critical — fixed:** Render's free web services have an ephemeral filesystem. Confirmed directly from Render's own documentation: any local file (including a SQLite database) is wiped on every restart, redeploy, *and* every spin-down from 15 minutes of inactivity. On the free tier, that's not a rare edge case — it's the normal behavior between visits. **This means your data was very likely already being silently lost.**

*Fix:* optional Postgres support via a `DATABASE_URL` environment variable. Set it and every write survives restarts — verified by killing the running process entirely and confirming the data was still there after a full restart against the same database, with the local SQLite file deleted first so there was no way to cheat the test. Don't set it, and the app falls back to local SQLite exactly as before (correct for running on your own machine; still wrong for Render's free tier without this fix).

**A free way to get that Postgres database:** [neon.tech](https://neon.tech) — no credit card, a free project gives you a connection string immediately. Copy it, add it to Render under Environment as `DATABASE_URL`, redeploy. (Render's *own* free Postgres works too, but expires after 30 days — Neon's free tier doesn't.)

**High — fixed:** the local-state and file-upload endpoints had no authentication at all. That was a reasonable default for something running only on your own machine; it stops being reasonable the moment the app is on a public URL, since anyone with the link could read or overwrite your real business data.

*Fix:* an optional `FOREMAN_ACCESS_CODE` environment variable. Set it, and your browser shows its native login prompt the first time you visit — no code changes to remember, the browser handles it and stays logged in after that. Leave it unset and everything works exactly as before, open, correct for genuinely private local use. Verified: no credentials is rejected, the wrong code is rejected, the right code is allowed, and `/health` stays open regardless so uptime monitors still work.

## Selling this — giving customers their own copy

`render.yaml` in this folder is a Render Blueprint — it's what makes a single link deploy an entire separate, working copy of Foreman (its own web service, its own database, fully isolated from yours or any other customer's) under whoever clicks it. No manual GitHub-then-Render walkthrough needed for each customer — that's exactly the multi-step process from before, now collapsed into one click.

**Set it up once:**
1. Push this whole folder — including the new `render.yaml` and `get-foreman.html` — to your GitHub repo (same "Add file → Upload files" as before).
2. Your deploy link is: `https://render.com/deploy?repo=` followed by your repo's URL, e.g. `https://render.com/deploy?repo=https://github.com/yourname/foreman`
3. Open `get-foreman.html` and replace `REPLACE_WITH_YOUR_GITHUB_REPO_URL` with your actual repo URL (that's the only edit it needs).
4. Optionally, paste this into your `README.md` to get the same button right on your GitHub repo page:
   ```
   [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yourname/foreman)
   ```

**What happens when a customer clicks it:** Render deploys their own private instance — a web service plus a real database, both under *their* Render account, not yours. A secure access code is generated automatically, so it's protected from the moment it's live, with zero setup on their part. AI features stay off until they choose to add their own Anthropic key, exactly like your own deployment — nothing breaks either way.

**Hosting `get-foreman.html` somewhere you can actually link to:** GitHub Pages does this for free. In your repo's Settings → Pages, set the source to your main branch, and GitHub gives you a real URL like `https://yourname.github.io/foreman/get-foreman.html` — that's the link to put in an email, a text, wherever you're telling customers about this.

**What "buying a license" means here, concretely:** each customer gets a fully separate, working copy — not a login to something you host and maintain for everyone. That also means you're not on the hook for their hosting costs or uptime; they own their own Render account and instance. If you'd rather host and manage every customer's data yourself (a subscription model instead), that's the multi-tenant mode described above (`/register` + real API keys) — a different, bigger undertaking than what's built here today.

## Updating your OWN existing deployment with the data-loss and security fixes

1. On GitHub, in your `foreman` repository, use "Add file → Upload files" and upload the new `main.py` and `requirements.txt` from this update (they overwrite the old ones).
2. On Render, go to your service → **Environment**, and add:
   - `DATABASE_URL` — your Neon (or other Postgres) connection string, to actually fix data loss.
   - `FOREMAN_ACCESS_CODE` — any password you choose, to actually lock down who can see your data.
3. Render redeploys automatically. The first visit after that, your browser will ask for a username (anything works) and password (your access code) — that's the fix working.

## Deploying for real

Any host that runs a Python process works: Railway, Render, Fly.io, a small VPS.
1. Push this folder to a git repo.
2. Start command: `python main.py` (it reads the `PORT` environment variable if your host sets one).

**Before customers use a deployed version:**
- `DATABASE_URL` and `FOREMAN_ACCESS_CODE` (above) are no longer optional at that point — set both.
- Local mode (`/api/local-state`) is still fundamentally single-business — fine for you, or for one customer's own private deployment. For multiple customers sharing one deployment, use multi-tenant mode (`/register` + real API keys) instead.
- Lock down CORS (`allow_origins=["*"]` in `main.py` is fine for local dev, not for production).
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
