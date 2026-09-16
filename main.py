"""
FOREMAN_BUILD_MARKER: 2026-09-15-gemini-3.6-flash-fix
(This line only exists so we can confirm which version is actually live —
check for it directly on GitHub or via curl before assuming a deploy
worked. Safe to ignore otherwise.)

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
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Body, UploadFile, File, Depends, Request
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # free tier, no credit card — tried first if set
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")  # sends the real purchase-delivery email
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # a Gmail App Password, not your real password
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


DEMO_HTML_PATH = Path(__file__).parent / "foreman-demo.html"


@app.get("/demo", response_class=HTMLResponse)
def serve_demo():
    """
    Public, no login required — this is the live demo for prospective
    customers, not your real business. It always boots fresh into sample
    data and never saves anything (see the DEMO MODE block at the top of
    foreman-demo.html's script).
    """
    if not DEMO_HTML_PATH.exists():
        raise HTTPException(status_code=500, detail="foreman-demo.html not found next to main.py")
    return DEMO_HTML_PATH.read_text(encoding="utf-8")


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


class ContactForm(BaseModel):
    name: str
    email: str
    message: str


@app.post("/api/contact")
def contact_form(form: ContactForm):
    """
    The public marketing site's contact form posts here. No login required —
    strangers submitting this is the whole point. Never exposes your real
    email anywhere on the public page; instead, this creates a real Lead in
    your own CRM, so "replying" just means opening Foreman and looking at
    Customers. Nothing here sends an email or notifies anyone automatically —
    that's a deliberate choice, not a missing feature: you check when you
    check, on your own time.
    """
    ensure_local_business()
    with get_db() as db:
        row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
        blob = json.loads(row["state_json"]) if row else {}
        core = json.loads(blob.get("business-os-core-v1") or "{}")
        core.setdefault("customers", [])
        core.setdefault("auditLog", [])

        existing = next((c for c in core["customers"] if c.get("email") == form.email), None)
        if existing:
            existing["notes"] = (existing.get("notes", "") + f"\n\n[{now_iso()[:10]}] {form.message}").strip()
        else:
            core["customers"].append({
                "id": uuid.uuid4().hex,
                "name": form.name,
                "company": "",
                "status": "Lead",
                "email": form.email,
                "phone": "",
                "followUp": "",
                "notes": f"Website contact form: {form.message}",
                "_added": now_iso(),
            })

        core["auditLog"].insert(0, {
            "id": uuid.uuid4().hex,
            "ts": now_iso(),
            "action": "Contact form (website)",
            "detail": f"{form.name} <{form.email}>: {form.message[:120]}",
        })
        if len(core["auditLog"]) > 300:
            core["auditLog"] = core["auditLog"][:300]

        blob["business-os-core-v1"] = json.dumps(core)
        db.execute(
            "UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(blob), now_iso(), LOCAL_BUSINESS_ID),
        )
        db.commit()

    return {"received": True}


@app.get("/api/stripe/webhook/status")
def stripe_webhook_status():
    """
    A direct diagnostic, so we don't have to guess or dig through Stripe's or
    Render's UI to find out whether the secret actually made it onto the
    server. Never reveals the working secret itself — just enough to confirm
    whether it's set at all, and to spot-check it against what it should be
    without exposing it in full.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        return {"configured": False, "message": "STRIPE_WEBHOOK_SECRET is NOT set on this server right now."}
    return {
        "configured": True,
        "length": len(secret),
        "starts_with": secret[:8],
        "ends_with": secret[-4:],
        "message": "STRIPE_WEBHOOK_SECRET is set. Compare starts_with/ends_with/length to the real value to confirm it's correct.",
    }


@app.get("/api/ai/status")
def ai_status():
    """
    Same idea as the Stripe diagnostic — check what's actually configured
    on the server for AI features before assuming a real API call will work.
    Never reveals a full key, just enough to spot-check it (length is the
    most useful check: it catches trailing/leading whitespace from a
    copy-paste, the exact bug that cost real time on the Stripe secret).
    """
    def describe(name, value):
        if not value:
            return {"configured": False}
        return {"configured": True, "length": len(value), "starts_with": value[:8], "ends_with": value[-4:]}

    gemini = describe("GEMINI_API_KEY", GEMINI_API_KEY)
    anthropic = describe("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
    which_active = "gemini" if GEMINI_API_KEY else ("anthropic" if ANTHROPIC_API_KEY else None)
    return {
        "gemini_api_key": gemini,
        "anthropic_api_key": anthropic,
        "active_provider": which_active,
        "message": (
            f"Using {which_active} for AI features." if which_active
            else "No AI provider configured — Marketing/Supervisor/Document generation will show a clear message instead of failing silently."
        ),
    }


class SaleWebhook(BaseModel):
    customer_name: str
    customer_email: str | None = None
    amount: float
    product_name: str = "Foreman license"
    source: str = "website"


def _build_purchase_email_html(name: str, order_id: str, amount: float) -> str:
    first_name = (name or "there").split()[0]
    return f"""<!DOCTYPE html>
<html><body style="margin:0; padding:0; background:#EEF1F0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF1F0; padding:32px 0;">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#FFFFFF; border-radius:14px; overflow:hidden; border:1px solid #D7DCDD;">
  <tr><td style="background:#0D1015; padding:32px 40px;">
    <div style="font-family:Georgia,serif; font-weight:800; font-size:1.4rem; color:#fff; letter-spacing:-.01em;">Foreman</div>
  </td></tr>
  <tr><td style="padding:40px;">
    <h1 style="font-size:1.5rem; margin:0 0 8px; color:#12161C;">Your license is ready, {first_name}.</h1>
    <p style="color:#5B6670; font-size:1rem; line-height:1.6; margin:0 0 28px;">
      Thanks for purchasing Foreman — Standard License. This email has everything you need to get your own copy running. No coding, about 3 minutes start to finish.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F7F5F1; border-radius:10px; margin-bottom:28px;">
      <tr><td style="padding:20px 24px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="font-size:.85rem; color:#5B6670; padding-bottom:6px;">Order</td>
            <td style="font-size:.85rem; color:#5B6670; text-align:right; padding-bottom:6px;">#{order_id}</td>
          </tr>
          <tr>
            <td style="font-size:.95rem; color:#12161C; font-weight:600;">Foreman — Standard License</td>
            <td style="font-size:.95rem; color:#12161C; font-weight:600; text-align:right;">${amount:,.2f} CAD</td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h2 style="font-size:1rem; color:#12161C; margin:0 0 6px;">Step 1 — Deploy your copy</h2>
    <p style="color:#5B6670; font-size:.95rem; line-height:1.6; margin:0 0 16px;">
      Click below and sign in with (or create) a free Render account. This creates a private copy of Foreman — yours alone, not shared with any other customer.
    </p>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="background:#AD7A2E; border-radius:8px;">
        <a href="https://render.com/deploy?repo=https://github.com/thecybershopfst-a11y/Foreman"
           style="display:inline-block; padding:14px 28px; color:#fff; font-weight:700; font-size:.95rem; text-decoration:none;">
          Deploy my copy of Foreman &rarr;
        </a>
      </td></tr>
    </table>
    <h2 style="font-size:1rem; color:#12161C; margin:0 0 6px;">Step 2 — Find your access code</h2>
    <p style="color:#5B6670; font-size:.95rem; line-height:1.6; margin:0 0 28px;">
      Once deployed, Render generates a private password automatically (<code style="background:#F1E4CC; padding:2px 6px; border-radius:4px;">FOREMAN_ACCESS_CODE</code>) — find it under your new service's <strong>Environment</strong> tab. That's your login; any username works alongside it.
    </p>
    <h2 style="font-size:1rem; color:#12161C; margin:0 0 6px;">Step 3 — Open your copy</h2>
    <p style="color:#5B6670; font-size:.95rem; line-height:1.6; margin:0 0 28px;">
      Your URL will look like <code style="background:#F1E4CC; padding:2px 6px; border-radius:4px;">your-name.onrender.com</code> — Render shows it on the same page once deployment finishes (usually under a minute).
    </p>
    <hr style="border:none; border-top:1px solid #D7DCDD; margin:32px 0;">
    <p style="color:#5B6670; font-size:.9rem; line-height:1.6; margin:0 0 4px;">
      Questions, or want a hand with setup? Just reply to this email, or call <strong>902-321-1375</strong>.
    </p>
    <p style="color:#5B6670; font-size:.9rem; line-height:1.6; margin:0;">
      Want to talk it through live? <a href="https://calendly.com/thecybershop-fst/30min" style="color:#AD7A2E;">Book a Foreman Setup Call &rarr;</a>
    </p>
  </td></tr>
  <tr><td style="background:#0D1015; padding:24px 40px; text-align:center;">
    <p style="color:#6C7580; font-size:.78rem; margin:0;">&copy; 2026 Foreman. Your license is yours to keep &mdash; no recurring fee, ever.</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _send_purchase_email(name: str, to_email: str, order_id: str, amount: float):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured on this server.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Foreman license is ready"
    msg["From"] = f"Foreman <{GMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg.attach(MIMEText(_build_purchase_email_html(name, order_id, amount), "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_email], msg.as_string())


@app.get("/api/email/status")
def email_status():
    """
    Same diagnostic pattern as /api/ai/status and /api/stripe/webhook/status —
    confirms what's actually configured without exposing the real password.
    """
    configured = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)
    return {
        "configured": configured,
        "gmail_address": GMAIL_ADDRESS if GMAIL_ADDRESS else None,
        "app_password_length": len(GMAIL_APP_PASSWORD) if GMAIL_APP_PASSWORD else None,
        "message": (
            "Ready to send real purchase-delivery emails." if configured
            else "GMAIL_ADDRESS and/or GMAIL_APP_PASSWORD not set — delivery emails will fail (sales still get recorded either way)."
        ),
    }


def _record_sale(customer_name: str, customer_email: str | None, amount: float, product_name: str, source: str):
    """
    Shared by both the generic sale webhook and the real Stripe webhook —
    same logic either way, so a $299 sale looks identical in your business
    data whether it came from a manual POST or a real Stripe payment.
    """
    ensure_local_business()
    with get_db() as db:
        row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
        blob = json.loads(row["state_json"]) if row else {}
        core = json.loads(blob.get("business-os-core-v1") or "{}")

        core.setdefault("customers", [])
        core.setdefault("finance", {"transactions": []})
        core.setdefault("invoices", [])
        core.setdefault("auditLog", [])

        today = now_iso()[:10]

        customer = None
        if customer_email:
            customer = next((c for c in core["customers"] if c.get("email") == customer_email), None)
        if not customer:
            customer = next((c for c in core["customers"] if c.get("name") == customer_name), None)

        if customer:
            customer["status"] = "Repeat Customer" if customer.get("status") == "Customer" else customer.get("status", "Customer")
        else:
            customer = {
                "id": uuid.uuid4().hex,
                "name": customer_name,
                "company": "",
                "status": "Customer",
                "email": customer_email or "",
                "phone": "",
                "followUp": "",
                "notes": f"Purchased via {source}",
                "_added": now_iso(),
            }
            core["customers"].append(customer)

        core["finance"]["transactions"].append({
            "id": uuid.uuid4().hex,
            "type": "income",
            "category": "Product sales",
            "amount": amount,
            "date": today,
            "notes": f"{product_name} — {customer_name} (via {source})",
        })

        core["invoices"].append({
            "id": uuid.uuid4().hex,
            "customer": customer_name,
            "dueDate": today,
            "status": "Paid",
            "created": now_iso(),
            "items": [{"id": uuid.uuid4().hex, "desc": product_name, "amount": amount}],
        })

        core["auditLog"].insert(0, {
            "id": uuid.uuid4().hex,
            "ts": now_iso(),
            "action": "Sale (website)",
            "detail": f"{customer_name} purchased {product_name} for ${amount:,.2f} via {source}",
        })
        if len(core["auditLog"]) > 300:
            core["auditLog"] = core["auditLog"][:300]

        blob["business-os-core-v1"] = json.dumps(core)
        db.execute(
            "UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(blob), now_iso(), LOCAL_BUSINESS_ID),
        )
        db.commit()


@app.post("/api/webhook/sale")
def webhook_sale(sale: SaleWebhook, x_webhook_secret: str | None = Header(default=None)):
    """
    Generic sale webhook — for manual testing, or any payment processor that
    isn't Stripe. For your real Stripe Payment Link, use /api/stripe/webhook
    instead (below), which verifies Stripe's real signature.

    Protected by a separate secret (FOREMAN_WEBHOOK_SECRET) from your login
    access code, since this one gets called by a server, not a person typing
    in a browser.
    """
    webhook_secret = os.environ.get("FOREMAN_WEBHOOK_SECRET")
    if webhook_secret:
        if not x_webhook_secret or not secrets.compare_digest(x_webhook_secret, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
    _record_sale(sale.customer_name, sale.customer_email, sale.amount, sale.product_name, sale.source)
    return {"recorded": True, "customer": sale.customer_name, "amount": sale.amount}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """
    The real one. Point your Stripe webhook endpoint (Dashboard -> Developers
    -> Webhooks) at this URL, subscribed to the checkout.session.completed
    event. Stripe signs every request with STRIPE_WEBHOOK_SECRET (shown when
    you create the endpoint in Stripe) — this verifies that signature before
    trusting anything in the payload, so a stranger can't POST fake sales
    here even without knowing a secret of your own choosing.
    """
    import stripe as stripe_lib

    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=501, detail="STRIPE_WEBHOOK_SECRET not configured on this server yet.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe_lib.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe_lib.error.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid Stripe signature: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"].to_dict()
        customer_details = session.get("customer_details") or {}
        name = customer_details.get("name") or "Stripe customer"
        email = customer_details.get("email")
        amount = (session.get("amount_total") or 0) / 100  # Stripe amounts are in cents
        order_id = session.get("id", "")[-8:].upper() if session.get("id") else "N/A"
        _record_sale(name, email, amount, "Foreman — Standard License", "Stripe")
        if email:
            try:
                _send_purchase_email(name, email, order_id, amount)
            except Exception as e:
                # A failed email must never look like a failed sale — the sale is
                # already recorded above. Log it to the audit trail so it's
                # visible in the app, rather than silently lost.
                ensure_local_business()
                with get_db() as db:
                    row = db.execute("SELECT state_json FROM businesses WHERE id = ?", (LOCAL_BUSINESS_ID,)).fetchone()
                    blob = json.loads(row["state_json"]) if row else {}
                    core = json.loads(blob.get("business-os-core-v1") or "{}")
                    core.setdefault("auditLog", [])
                    core["auditLog"].insert(0, {
                        "id": uuid.uuid4().hex,
                        "ts": now_iso(),
                        "action": "Delivery email FAILED",
                        "detail": f"Sale recorded for {name} <{email}>, but the delivery email failed to send: {e}",
                    })
                    blob["business-os-core-v1"] = json.dumps(core)
                    db.execute("UPDATE businesses SET state_json = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(blob), now_iso(), LOCAL_BUSINESS_ID))
                    db.commit()

    return {"received": True}


class AIRequest(BaseModel):
    prompt: str


def _call_gemini(prompt: str) -> str:
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise HTTPException(status_code=e.code, detail=f"Gemini API error: {detail}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach Gemini's API: {e.reason}")
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail=f"Gemini returned an unexpected response shape: {json.dumps(data)[:300]}")


def _call_anthropic(prompt: str) -> str:
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
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
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


@app.post("/api/ai/generate")
def ai_generate(req: AIRequest):
    """
    Proxies AI-drafted content (Marketing, Supervisor, Document generation)
    through a real API key held server-side — never exposed to the browser,
    never in the HTML, never in the repo.

    Tries Gemini first (GEMINI_API_KEY) — Google's free tier, no credit card,
    genuinely $0 at this app's usage volume. Falls back to Anthropic
    (ANTHROPIC_API_KEY) if that's set instead or as well — higher quality,
    real per-use cost. Fails honestly, with a clear message, if neither is
    configured. That's the current default: AI features stay off until
    someone deliberately turns one on.
    """
    if GEMINI_API_KEY:
        return {"text": _call_gemini(req.prompt)}
    if ANTHROPIC_API_KEY:
        return {"text": _call_anthropic(req.prompt)}
    raise HTTPException(
        status_code=501,
        detail="AI features aren't turned on for this installation — no Gemini or Anthropic API key has been configured on this server."
    )


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
