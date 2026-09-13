"""
Foreman — one program: a single Python server that serves the Foreman UI
AND its backend on the same port. Run this one file, open the URL it
prints, and you have the whole thing — no separate frontend/backend
wiring, no CORS dance, no manual "paste the API key in" step for local use.

WHAT THIS IS:
- Serves business-os.html at "/" — open it in a browser, that's the app.
- A simple, unauthenticated "local business" state store at
  /api/local-state/{key}, which the page's storage polyfill (see the
  <script> block near the top of business-os.html) uses automatically
  when window.storage isn't available (i.e., whenever this ISN'T running
  inside a Claude artifact). This is what makes data persist across
  restarts of this server with zero setup.
- The original multi-tenant /register + /state/{business_id} endpoints
  are still here too, unchanged — that's the path for when you actually
  deploy this for more than one customer later. The local-state
  endpoints are the "just works on my own machine" path; the
  multi-tenant ones are the "real SaaS" path. Same server, both modes.

WHAT THIS IS NOT (yet):
- Not production-hardened auth (API keys are simple bearer tokens, not
  OAuth/JWT with rotation). Fine for a single-owner MVP; revisit before
  handling other people's customer data at scale.
- The local-state endpoints have NO auth at all — they're meant for
  "this server runs on my own computer for my own business." Don't
  expose this mode on the open internet as-is.
- Not deployed anywhere. This runs locally until you deploy it
  (Railway, Render, Fly.io, a VPS — anywhere that runs a Python
  process).

RUNNING IT:
    pip install -r requirements.txt
    python main.py

Then open the URL it prints (http://localhost:8000 by default).
"""

import sqlite3
import json
import secrets
import hashlib
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Body, UploadFile, File, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

DB_PATH = "foreman.db"
HTML_PATH = Path(__file__).parent / "business-os.html"
IMPORT_PATH = Path(__file__).parent / "import-state.json"
UPLOADS_DIR = Path(__file__).parent / "uploads"
LOCAL_BUSINESS_ID = "local"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB — generous, since this is real disk storage, not the JSON blob
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # unset = AI features stay off, honestly
ACCESS_CODE = os.environ.get("FOREMAN_ACCESS_CODE")  # unset = local-state mode stays open (fine for genuinely local use)

# DATABASE_URL, if set, points at a real Postgres instance and makes this
# survive Render's free-tier ephemeral filesystem (SQLite files on that tier
# get wiped on every restart/spin-down — confirmed in Render's own docs).
# Not set = falls back to a local SQLite file, which is genuinely persistent
# when this runs somewhere with a real filesystem (your own machine, a VPS,
# a paid Render instance with a disk) but NOT on Render's free web service.
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)
if IS_POSTGRES:
    import psycopg2
    import psycopg2.extras


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_local_business()
    UPLOADS_DIR.mkdir(exist_ok=True)
    maybe_import_state_file()
    if IS_POSTGRES:
        print("[Foreman] Using Postgres for storage — data survives restarts/spin-downs, including on Render's free tier.")
    else:
        print("[Foreman] Using local SQLite — fine on your own machine, but WILL be wiped on Render's free web service every time it spins down (15 min idle) or redeploys. Set DATABASE_URL (e.g. a free Neon database) to fix that for a public deployment.")
    yield


app = FastAPI(title="Foreman", version="0.3.0", lifespan=lifespan)

# CORS wide open for local development. Lock this down to your actual
# front-end origin before deploying for real — see README.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS businesses (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_key_hash TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        db.commit()


class _DBWrapper:
    """
    Makes a Postgres connection quack like the sqlite3 connection the rest of
    this file was written against — same .execute(sql, params).fetchone()/
    .fetchall() chaining, same dict-style row["column"] access, same
    .commit(). Translates sqlite's '?' placeholders to Postgres's '%s'.
    This keeps every query in the file identical for both backends; only
    this wrapper and get_db() know the difference exists.
    """
    def __init__(self, conn):
        self._conn = conn
        self._cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("?", "%s"), params)
        return self._cursor

    def commit(self):
        self._conn.commit()

    def close(self):
        self._cursor.close()
        self._conn.close()


@contextmanager
def get_db():
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        wrapper = _DBWrapper(conn)
        try:
            yield wrapper
        finally:
            wrapper.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_basic_auth = HTTPBasic(auto_error=False)


def require_access(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """
    Real protection for a real public URL. If FOREMAN_ACCESS_CODE isn't set,
    this is a no-op — local-state mode stays open, same as before, which is
    fine for something running on your own machine that nothing else can
    reach. Once this is on the open internet (Render, any host), set that
    env var and the browser will show its native login prompt on first
    visit — no code in the HTML needed, browsers remember it after that.
    """
    if not ACCESS_CODE:
        return
    if not credentials or not secrets.compare_digest(credentials.password, ACCESS_CODE):
        raise HTTPException(status_code=401, detail="Access code required", headers={"WWW-Authenticate": "Basic"})


def authenticate(business_id: str, x_api_key: str | None):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM businesses WHERE id = ?", (business_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Business not found")
    if row["api_key_hash"] != hash_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return row


def ensure_local_business():
    with get_db() as db:
        row = db.execute("SELECT id FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO businesses (id, name, api_key_hash, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (LOCAL_BUSINESS_ID, "Local business", "", "{}", now_iso(), now_iso()),
            )
            db.commit()


def maybe_import_state_file():
    """
    If import-state.json sits next to this file (e.g. exported from the
    Claude-artifact version via Data & Backend -> Export all data) AND the
    local business has no data yet, seed it automatically on startup. Only
    runs when local state is empty, so it never silently overwrites work
    you've already done in this running program.
    """
    if not IMPORT_PATH.exists():
        return
    with get_db() as db:
        row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
        existing = json.loads(row["state_json"]) if row else {}
        if existing.get("business-os-core-v1"):
            print(f"[Foreman] Found {IMPORT_PATH.name} but local data already exists — not overwriting. Delete it or clear local data first if you want to re-import.")
            return
        try:
            imported = json.loads(IMPORT_PATH.read_text(encoding="utf-8"))
            core_state = imported.get("state", imported)
            existing["business-os-core-v1"] = json.dumps(core_state)
            db.execute(
                "UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(existing), now_iso(), LOCAL_BUSINESS_ID),
            )
            db.commit()
            print(f"[Foreman] Imported business data from {IMPORT_PATH.name}.")
        except Exception as e:
            print(f"[Foreman] Found {IMPORT_PATH.name} but couldn't import it: {e}")


class RegisterRequest(BaseModel):
    business_name: str


class RegisterResponse(BaseModel):
    business_id: str
    api_key: str
    warning: str = "Save this API key now — it is not retrievable again."


class StatePayload(BaseModel):
    state: dict


# ---------- Serve the UI ----------

@app.get("/", response_class=HTMLResponse)
def serve_ui(_: None = Depends(require_access)):
    if not HTML_PATH.exists():
        raise HTTPException(status_code=500, detail="business-os.html not found next to main.py")
    return HTML_PATH.read_text(encoding="utf-8")


# ---------- Local single-business mode ----------
# Protected by require_access when FOREMAN_ACCESS_CODE is set (see that
# function's docstring) — open otherwise, for genuinely local/private use.

@app.get("/api/local-state/{key}")
def get_local_state(key: str, _: None = Depends(require_access)):
    ensure_local_business()
    with get_db() as db:
        row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
    blob = json.loads(row["state_json"]) if row else {}
    return {"key": key, "value": blob.get(key)}


@app.put("/api/local-state/{key}")
def put_local_state(key: str, payload: dict = Body(...), _: None = Depends(require_access)):
    ensure_local_business()
    with get_db() as db:
        row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
        blob = json.loads(row["state_json"]) if row else {}
        blob[key] = payload.get("value")
        db.execute(
            "UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(blob), now_iso(), LOCAL_BUSINESS_ID),
        )
        db.commit()
    return {"key": key, "saved": True}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), _: None = Depends(require_access)):
    """
    Real file storage on disk — this is the path the front end uses when it's
    actually running through this server (standalone mode). No size limit
    baked into the app's own JSON blob, unlike the base64 fallback used when
    no backend is reachable at all.
    """
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large — {MAX_UPLOAD_BYTES // (1024*1024)}MB limit")
    ext = Path(file.filename or "").suffix
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / stored_name
    dest.write_bytes(contents)
    return {"url": f"/api/uploads/{stored_name}", "original_name": file.filename, "size": len(contents)}


@app.get("/api/uploads/{stored_name}")
def get_upload(stored_name: str, _: None = Depends(require_access)):
    path = UPLOADS_DIR / stored_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # prevent path traversal outside the uploads directory
    if UPLOADS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    return FileResponse(path)


class AIRequest(BaseModel):
    prompt: str


@app.post("/api/ai/generate")
def ai_generate(req: AIRequest):
    """
    Proxies AI-drafted content (Marketing, Supervisor, Document generation)
    through a real Anthropic API key held server-side — never exposed to the
    browser, never in the HTML, never in the repo. Fails honestly, with a
    clear message, if no key has been configured. That's the current state
    by default: AI features stay off until someone deliberately turns them
    on by setting ANTHROPIC_API_KEY, since every call costs real money.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=501,
            detail="AI features aren't turned on for this installation — no Anthropic API key has been configured on this server."
        )
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": req.prompt}],
    }).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise HTTPException(status_code=e.code, detail=f"Anthropic API error: {detail}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach Anthropic's API: {e.reason}")
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            break
    return {"text": text}


# ---------- Multi-tenant mode (real API keys — the path for real deployment) ----------

@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso()}


@app.post("/register", response_model=RegisterResponse)
def register(req: RegisterRequest):
    business_id = secrets.token_hex(8)
    api_key = secrets.token_urlsafe(24)
    with get_db() as db:
        db.execute(
            "INSERT INTO businesses (id, name, api_key_hash, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (business_id, req.business_name, hash_key(api_key), "{}", now_iso(), now_iso()),
        )
        db.commit()
    return RegisterResponse(business_id=business_id, api_key=api_key)


@app.get("/state/{business_id}")
def get_state(business_id: str, x_api_key: str | None = Header(default=None)):
    row = authenticate(business_id, x_api_key)
    return {
        "business_id": business_id,
        "state": json.loads(row["state_json"]),
        "updated_at": row["updated_at"],
    }


@app.put("/state/{business_id}")
def put_state(business_id: str, payload: StatePayload, x_api_key: str | None = Header(default=None)):
    authenticate(business_id, x_api_key)
    updated = now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload.state), updated, business_id),
        )
        db.commit()
    return {"business_id": business_id, "updated_at": updated, "saved": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\nForeman is starting — open http://localhost:{port} in your browser.\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
