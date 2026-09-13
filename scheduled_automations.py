"""
Foreman Scheduled Automations — runs the same rule-based automations already
built into the app (overdue invoices, low stock, stalled deals, expiring
documents, recurring billing, won-deal handoffs), but server-side, on a
schedule, so they fire even when nobody has the app open.

DELIBERATELY NOT AI. No Anthropic calls, no API key, no per-run cost. Every
rule here is a plain date/number comparison — a direct Python port of the
AUTOMATIONS array in business-os.html, kept in sync with it rule for rule.
This only creates tasks; it never sends anything, charges anything, or
deletes anything.

Respects the same on/off toggles you set in the app's Automations tab
(state.automations), and uses the exact same de-duplication scheme
(sourceId) as the browser version, so running both doesn't create doubles.

Configured via two environment variables (set as GitHub Secrets, not in
this file):
    FOREMAN_URL          e.g. https://your-app.onrender.com
    FOREMAN_ACCESS_CODE  only needed if you've set one on the server
"""

import os
import sys
import json
import uuid
from datetime import datetime, timezone

import requests

FOREMAN_URL = os.environ.get("FOREMAN_URL", "").rstrip("/")
ACCESS_CODE = os.environ.get("FOREMAN_ACCESS_CODE")
STATE_KEY = "business-os-core-v1"

AUTH = ("foreman", ACCESS_CODE) if ACCESS_CODE else None


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_between(date_str, from_str=None):
    """Days from from_str (default: now) to date_str. Negative = in the past."""
    target = datetime.fromisoformat(date_str.replace("Z", "+00:00")) if "T" in date_str else datetime.strptime(date_str, "%Y-%m-%d")
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (target - now).days


def money(n):
    try:
        return f"${float(n):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def fetch_state():
    r = requests.get(f"{FOREMAN_URL}/api/local-state/{STATE_KEY}", auth=AUTH, timeout=30)
    r.raise_for_status()
    value = r.json().get("value")
    return json.loads(value) if value else {}


def save_state(state):
    r = requests.put(f"{FOREMAN_URL}/api/local-state/{STATE_KEY}", auth=AUTH, json={"value": json.dumps(state)}, timeout=30)
    r.raise_for_status()


def enabled(state, automation_id):
    automations = state.get("automations", {})
    return automations.get(automation_id, True)  # default on, matching the app


def task_exists(state, source_id):
    return any(t.get("sourceId") == source_id for t in state.get("tasks", []))


def add_task(state, title, priority, due, source_automation, source_id):
    state.setdefault("tasks", []).append({
        "id": uuid.uuid4().hex,
        "title": title,
        "priority": priority,
        "due": due,
        "status": "Open",
        "sourceAutomation": source_automation,
        "sourceId": source_id,
    })


def add_audit(state, action, detail):
    state.setdefault("auditLog", []).insert(0, {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "detail": detail,
    })
    if len(state["auditLog"]) > 300:
        state["auditLog"] = state["auditLog"][:300]


def run_followup_overdue(state):
    if not enabled(state, "followup_overdue"):
        return []
    created = []
    for c in state.get("customers", []):
        fu = c.get("followUp")
        if not fu or days_between(fu) > 0:
            continue
        sid = f"followup_{c['id']}_{fu}"
        if task_exists(state, sid):
            continue
        add_task(state, f"Follow up with {c['name']}", "High", fu, "followup_overdue", sid)
        created.append(c["name"])
    return created


def run_doc_expiring(state):
    if not enabled(state, "doc_expiring"):
        return []
    created = []
    for d in state.get("documents", []):
        exp = d.get("expiry")
        if not exp:
            continue
        days_left = days_between(exp)
        if days_left > 30:
            continue
        sid = f"docexp_{d['id']}_{exp}"
        if task_exists(state, sid):
            continue
        priority = "Critical" if days_left < 0 else "High"
        add_task(state, f"Renew {d['name']} (expires {exp})", priority, exp, "doc_expiring", sid)
        created.append(d["name"])
    return created


def run_deal_stalled(state):
    if not enabled(state, "deal_stalled"):
        return []
    created = []
    for d in state.get("pipeline", []):
        if d.get("stage") in ("Won", "Lost"):
            continue
        changed = d.get("stageChangedAt")
        if not changed or days_between(changed) > -14:
            continue
        sid = f"stall_{d['id']}_{d['stage']}"
        if task_exists(state, sid):
            continue
        add_task(state, f"Follow up on stalled deal: {d['name']} ({d['stage']})", "Medium", today_str(), "deal_stalled", sid)
        created.append(d["name"])
    return created


def run_deal_won_handoff(state):
    if not enabled(state, "deal_won_hando"):
        return []
    created = []
    for d in state.get("pipeline", []):
        if d.get("stage") != "Won":
            continue
        sid = f"won_{d['id']}"
        if task_exists(state, sid):
            continue
        add_task(state, f"Consider a customer story: {d['name']}", "Low", today_str(), "deal_won_hando", sid)
        created.append(d["name"])
    return created


def run_inventory_reorder(state):
    if not enabled(state, "inventory_reorder"):
        return []
    created = []
    for i in state.get("inventory", []):
        threshold = i.get("reorderAt") or 0
        if threshold <= 0 or (i.get("quantity") or 0) > threshold:
            continue
        sid = f"reorder_{i['id']}_{i.get('quantity')}"
        if task_exists(state, sid):
            continue
        add_task(state, f"Reorder {i['name']} ({i.get('quantity')} left, reorder at {threshold})", "Medium", today_str(), "inventory_reorder", sid)
        created.append(i["name"])
    return created


def run_recurring_due(state):
    if not enabled(state, "recurring_due"):
        return []
    created = []
    for r in state.get("recurring", []):
        if r.get("status") != "active":
            continue
        nd = r.get("nextDate")
        if not nd or days_between(nd) > 0:
            continue
        sid = f"recurring_{r['id']}_{nd}"
        if task_exists(state, sid):
            continue
        add_task(state, f"Bill recurring revenue: {r['customer']} ({money(r['amount'])})", "Medium", nd, "recurring_due", sid)
        created.append(r["customer"])
    return created


def run_invoice_overdue(state):
    if not enabled(state, "invoice_overdue"):
        return []
    created = []
    for inv in state.get("invoices", []):
        if inv.get("status") != "Sent":
            continue
        due = inv.get("dueDate")
        if not due or days_between(due) > 0:
            continue
        sid = f"invoice_{inv['id']}_{due}"
        if task_exists(state, sid):
            continue
        total = sum(float(it.get("amount") or 0) for it in inv.get("items", []))
        add_task(state, f"Chase overdue invoice: {inv['customer']} ({money(total)})", "High", today_str(), "invoice_overdue", sid)
        created.append(inv["customer"])
    return created


RULES = [
    ("Follow-up overdue → create task", run_followup_overdue),
    ("Document expiring soon → create task", run_doc_expiring),
    ("Deal stalled → create follow-up task", run_deal_stalled),
    ("Deal won → hand off to Marketing", run_deal_won_handoff),
    ("Stock at reorder point → create task", run_inventory_reorder),
    ("Recurring payment due → create task", run_recurring_due),
    ("Invoice overdue → create task", run_invoice_overdue),
]


def main():
    if not FOREMAN_URL:
        print("FOREMAN_URL is not set — nothing to do.", file=sys.stderr)
        sys.exit(1)

    print(f"[Foreman scheduled automations] Checking {FOREMAN_URL} at {datetime.now(timezone.utc).isoformat()}")
    state = fetch_state()
    if not state:
        print("No business data found yet — nothing to check.")
        return

    any_created = False
    for name, rule in RULES:
        created = rule(state)
        if created:
            any_created = True
            add_audit(state, "Automation (scheduled)", f"{name}: created {len(created)} task(s) — {', '.join(created)}")
            print(f"  {name}: created task(s) for {created}")

    if any_created:
        save_state(state)
        print("Saved — new tasks are in the app now.")
    else:
        print("Nothing needed attention this run.")


if __name__ == "__main__":
    main()
