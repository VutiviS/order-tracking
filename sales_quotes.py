"""Customer quotes that become orders (Sales > Quotes, and the customer's page on the tracking site /q/<id>-<code>).
Pure Python, shared by the desktop app and the website: KEEP IDENTICAL to inventra-track-online/sales_quotes.py
(python sync_web_files.py copies it).

A quote (table `sales_quotes`): quote_id, client_name, lines [{sku, description, qty, price (excl VAT)}],
valid_until, notes, task_type, status, order_id, accepted_by, accepted_at, customer_ref, decline_reason, created_by...
Statuses: Draft -> Sent -> Accepted (an order is created at once) | Declined | Expired."""

import base64
import hashlib
import hmac
import random
from datetime import date, datetime, timedelta
from typing import List, Optional

VAT_RATE = 0.15
TABLE = "sales_quotes"
PER_LINE_MARKER = "Order created in Sales (per line)"      # the same marker Sales orders carry
OPEN = ("Draft", "Sent")


def num(v) -> float:
    try:
        return float(str(v).replace("R", "").replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def new_quote_id(existing: List[str], today: Optional[date] = None) -> str:
    today = today or date.today()
    prefix = f"Q-{today:%y%m}-"
    nums = [int(x[len(prefix):]) for x in existing if str(x).startswith(prefix) and str(x)[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def totals(q: dict) -> dict:
    sub = round(sum(num(l.get("price")) * num(l.get("qty")) for l in q.get("lines") or []), 2)
    vat = round(sub * VAT_RATE, 2)
    return {"subtotal": sub, "vat": vat, "total": round(sub + vat, 2)}


def expired(q: dict, today: Optional[date] = None) -> bool:
    today = today or date.today()
    try:
        return q.get("status") in OPEN and datetime.strptime(str(q.get("valid_until"))[:10], "%Y-%m-%d").date() < today
    except ValueError:
        return False


def problems(q: dict) -> List[str]:
    out = []
    if not str(q.get("client_name") or "").strip():
        out.append("Choose the customer.")
    if not q.get("lines"):
        out.append("Add at least one product.")
    if any(num(l.get("qty")) <= 0 or num(l.get("price")) < 0 for l in q.get("lines") or []):
        out.append("Every line needs a quantity and a price.")
    try:
        datetime.strptime(str(q.get("valid_until"))[:10], "%Y-%m-%d")
    except ValueError:
        out.append("Say until when the quote is valid.")
    return out


def default_validity(today: Optional[date] = None, days: int = 14) -> str:
    return ((today or date.today()) + timedelta(days=days)).isoformat()


# ---- the customer's private link ----

def code(secret: str, quote_id: str) -> str:
    mac = hmac.new(str(secret).encode(), ("inventra-quote:" + str(quote_id)).encode(), hashlib.sha256).digest()
    return base64.b32encode(mac).decode()[:8]


def link(base_url: str, secret: str, quote_id: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}/q/{quote_id}-{code(secret, quote_id)}" if base else ""


def from_key(secret: str, key: str) -> Optional[str]:
    key = str(key or "").strip().strip("/")
    if "-" not in key:
        return None
    qid, c = key.rsplit("-", 1)
    return qid if hmac.compare_digest(c.upper(), code(secret, qid)) else None


# ---- accepting: the order the customer's acceptance creates ----

def new_order_id(when: Optional[datetime] = None) -> int:
    """The same numeric format as Sales orders (yymmddHHMMSS + 3 random digits)."""
    return int(f"{(when or datetime.now()):%y%m%d%H%M%S}{random.randint(100, 999)}")


def order_rows(q: dict, order_id: int, accepted_by: str, customer_ref: str = "", when: Optional[datetime] = None) -> List[dict]:
    """customer_order rows (one per product line, the format Dispatch reads) for an accepted quote."""
    when = when or datetime.now()
    note = f"Quote {q['quote_id']} accepted by {accepted_by}" + (f" (their reference {customer_ref})" if customer_ref else "")
    base = {"order_id": order_id, "client_name": q["client_name"], "task_type": q.get("task_type") or "Supply",
            "supervisor_name": q.get("supervisor_name") or "", "technician_name": q.get("technician_name") or "",
            "job_instructions": ((q.get("notes") or "") + ("\n" if q.get("notes") else "") + note)[:900],
            "order_date": when.strftime("%Y-%m-%d"), "status": "Active", "otime": when.strftime("%H:%M:%S"),
            "reason_activation": PER_LINE_MARKER}
    return [dict(base, sku=l["sku"], product_description=str(l.get("description") or "")[:250], price=num(l["price"]),
                 value=num(l["price"]) * int(num(l["qty"]))) for l in q.get("lines") or []]


def accept(q: dict, name: str, customer_ref: str = "", when: Optional[datetime] = None):
    """(updated quote, order rows) - raises ValueError when the quote can't be accepted."""
    when = when or datetime.now()
    if q.get("status") not in OPEN:
        raise ValueError({"Accepted": "This quote was already accepted.", "Declined": "This quote was declined.",
                          "Expired": "This quote has expired."}.get(q.get("status"), "This quote can't be accepted."))
    if expired(q, when.date()):
        raise ValueError("This quote has expired. Ask us for a new one.")
    if len(str(name or "").strip()) < 2:
        raise ValueError("Type your name to accept.")
    order_id = new_order_id(when)
    rows = order_rows(q, order_id, name.strip(), customer_ref.strip(), when)
    done = dict(q, status="Accepted", order_id=str(order_id), accepted_by=name.strip()[:80],
                customer_ref=customer_ref.strip()[:60], accepted_at=when.isoformat(timespec="seconds"),
                updated_at=when.isoformat(timespec="seconds"))
    return done, rows
