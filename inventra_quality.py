"""Quality and non-conformance (shared by Stock Control, Dispatch and the Control Tower). Pure: no database, no UI.

A quality case (table `quality_cases`) records a problem with goods: where it was found (incoming inspection, in
stock, a customer return, a delivery), the supplier, product, batch and serial numbers, how many, the defect and its
severity. Suspect goods are QUARANTINED: Dispatch can't send them (their quantity is taken off what is available and
their serial numbers are refused) until a second person with approval rights decides what happens:
  release to stock, return to the supplier, or write off.
The case also holds the root cause, the corrective action, who is responsible and by when, and its full history."""

import uuid
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

TABLE = "quality_cases"
SOURCES = ["Incoming inspection", "Found in stock", "Customer return", "Delivery damage", "Production"]
DEFECTS = ["Damaged", "Faulty / not working", "Wrong item", "Wrong quantity", "Expired / short dated",
           "Missing parts or papers", "Other"]
SEVERITIES = ["Critical", "Major", "Minor"]
STATUSES = ["Open", "Quarantined", "Released", "Returned to supplier", "Written off", "Closed"]
OPEN = ("Open", "Quarantined")
DECISIONS = {"release": "Released", "return": "Returned to supplier", "write_off": "Written off"}


def num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def new_case_id(existing: List[str], today: Optional[date] = None) -> str:
    today = today or date.today()
    prefix = f"QC-{today:%y%m}-"
    nums = [int(x[len(prefix):]) for x in existing if str(x).startswith(prefix) and str(x)[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def serial_list(v) -> List[str]:
    if isinstance(v, list):
        return [str(s).strip() for s in v if str(s).strip()]
    return [s.strip() for s in str(v or "").replace(";", ",").replace("\n", ",").split(",") if s.strip()]


def quarantined(cases: List[dict]) -> Tuple[Dict[str, float], Set[str]]:
    """(units in quarantine per SKU, serial numbers in quarantine, upper case) from the open cases."""
    qty: Dict[str, float] = defaultdict(float)
    serials: Set[str] = set()
    for c in cases or []:
        if c.get("status") != "Quarantined":
            continue
        s = serial_list(c.get("serials"))
        qty[str(c.get("sku") or "")] += num(c.get("qty")) or len(s)
        serials |= {x.upper() for x in s}
    return dict(qty), serials


def history(action: str, note: str, user: dict) -> dict:
    return {"at": datetime.now().isoformat(timespec="seconds"), "by": user.get("full_name") or user.get("username") or "",
            "action": action, "note": note}


def problems(case: dict) -> List[str]:
    out = []
    if not case.get("sku"):
        out.append("Choose the product.")
    if num(case.get("qty")) <= 0 and not serial_list(case.get("serials")):
        out.append("Say how many units (or list the serial numbers).")
    if not str(case.get("title") or "").strip():
        out.append("Describe the problem in a few words.")
    return out


def decision_problems(case: dict, decision: str, note: str, user: dict, can_approve: bool) -> List[str]:
    out = []
    if case.get("status") != "Quarantined":
        out.append("Only quarantined goods need a decision.")
    if decision not in DECISIONS:
        out.append("Choose release, return to supplier or write off.")
    if not can_approve:
        out.append("Releasing, returning or writing off quarantined goods needs approval rights (Finance officer or "
                   "Administrator).")
    if str(case.get("created_by") or "").lower() == str(user.get("username") or "").lower():
        out.append("You opened this case, so someone else must decide (segregation of duties).")
    if not (note or "").strip():
        out.append("Write the reason for the decision: it stays on record.")
    if decision == "release" and case.get("inspection_result") == "Fail" and len((note or "").strip()) < 15:
        out.append("The goods failed inspection: explain why they can be released (for example re-tested and "
                   "working).")
    return out


def decide(case: dict, decision: str, note: str, user: dict) -> dict:
    c = dict(case)
    c["status"] = DECISIONS[decision]
    c["decision"] = {"action": decision, "note": note.strip(), "by": user.get("username"),
                     "by_name": user.get("full_name"), "at": datetime.now().isoformat(timespec="seconds")}
    c["history"] = list(c.get("history") or []) + [history(DECISIONS[decision], note.strip(), user)]
    c["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return c


# ---- less typing: what each defect usually means, and cases started from what INVENTRA already knows ----
DUE_DAYS = {"Critical": 2, "Major": 7, "Minor": 14}
GUIDE = {
    "Damaged": {"severity": "Major",
                "causes": ["Damaged in transit (poor packing)", "Damaged in our warehouse (handling / storage)",
                           "Damaged on delivery to the customer", "Arrived damaged from the supplier"],
                "actions": ["Claim from the supplier / courier and ask for better packing",
                            "Train the team on handling; store on racks, not the floor",
                            "Inspect and photograph goods on arrival", "Add padding / pallets for this product"]},
    "Faulty / not working": {"severity": "Critical",
                             "causes": ["Manufacturing fault (supplier)", "Dead on arrival", "Failed after installation",
                                        "Wrong setup or misuse"],
                             "actions": ["Return to the supplier for replacement or credit", "Test units on receipt",
                                         "Check the rest of the batch", "Give installers a setup checklist"]},
    "Wrong item": {"severity": "Major",
                   "causes": ["Supplier sent the wrong item", "Picked the wrong item in our warehouse",
                              "Wrong product code on the order"],
                   "actions": ["Scan every item when picking (phone scanner)", "Return and ask the supplier to replace",
                               "Check product codes on the purchase order"]},
    "Wrong quantity": {"severity": "Minor",
                       "causes": ["Supplier short-shipped", "Counted wrong when receiving", "Lost or stolen in transit"],
                       "actions": ["Count against the delivery note on arrival", "Ask the supplier for the balance or a credit",
                                   "Seal and check parcels"]},
    "Expired / short dated": {"severity": "Major",
                              "causes": ["Old stock not sold first (FEFO not followed)", "Supplier sent short-dated stock"],
                              "actions": ["Pick by expiry date (first expiry, first out)",
                                          "Agree a minimum shelf life with the supplier"]},
    "Missing parts or papers": {"severity": "Minor",
                                "causes": ["Supplier packed incomplete", "Parts lost in our warehouse"],
                                "actions": ["Check the contents list on arrival", "Ask the supplier for the missing parts"]},
    "Other": {"severity": "Major", "causes": [], "actions": []},
}


def guide(defect: str) -> dict:
    return GUIDE.get(defect) or GUIDE["Other"]


def due_for(severity: str, today: Optional[date] = None) -> str:
    from datetime import timedelta
    return ((today or date.today()) + timedelta(days=DUE_DAYS.get(severity, 7))).isoformat()


def auto_title(c: dict) -> str:
    """'Faulty / not working: IP bullet camera HW1200 (reditron)' when nobody typed a title."""
    what = c.get("description") or c.get("sku") or "goods"
    who = c.get("supplier") or c.get("client") or ""
    return f"{c.get('defect') or 'Problem'}: {what}" + (f" ({who})" if who else "")


def defect_from_text(text: str) -> str:
    t = (text or "").lower()
    for words, d in ((("faulty", "defect", "not work", "dead", "broken", "fault", "doesn't work", "does not work"),
                      "Faulty / not working"),
                     (("damag", "crack", "dent", "scratch", "wet"), "Damaged"),
                     (("wrong item", "wrong product", "incorrect"), "Wrong item"),
                     (("short", "missing quantity", "quantity"), "Wrong quantity"),
                     (("expir", "date"), "Expired / short dated"), (("missing", "incomplete"), "Missing parts or papers")):
        if any(w in t for w in words):
            return d
    return ""


def supplier_record(cases: List[dict], supplier: str, days: int = 90, today: Optional[date] = None) -> str:
    """'reditron: 3 quality cases in the last 90 days (2 Faulty / not working)' — shown while a case is filled in."""
    from datetime import timedelta
    if not supplier:
        return ""
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    mine = [c for c in cases if str(c.get("supplier") or "").lower() == supplier.lower()
            and str(c.get("created_at") or "")[:10] >= since]
    if not mine:
        return f"{supplier}: no quality cases in the last {days} days."
    top = defaultdict(int)
    for c in mine:
        top[c.get("defect") or "Other"] += 1
    d, n = max(top.items(), key=lambda kv: kv[1])
    return f"{supplier}: {len(mine)} quality case{'s' if len(mine) != 1 else ''} in the last {days} days ({n} {d})."


def prefill(base: dict, today: Optional[date] = None) -> dict:
    """Fill what can be worked out: severity and due date from the defect, the title, the units from the serials."""
    c = dict(base)
    g = guide(c.get("defect") or "Other")
    c.setdefault("severity", g["severity"])
    c["severity"] = c.get("severity") or g["severity"]
    c["due_date"] = c.get("due_date") or due_for(c["severity"], today)
    if not num(c.get("qty")) and serial_list(c.get("serials")):
        c["qty"] = len(serial_list(c.get("serials")))
    c["title"] = c.get("title") or auto_title(c)
    c.setdefault("inspection_result", "Fail")
    return c


def from_serial(trace, text: str) -> Optional[dict]:
    """A case started from a serial number: product, supplier and invoice it came on, where it is now (inventra_trace)."""
    if trace is None or not str(text or "").strip():
        return None
    from inventra_stock import key as skey
    s = trace.serials.get(skey(text))
    if s is None:
        return None
    first_in = next((e for e in s.events if e.kind in ("received", "produced")), None) or (s.events[0] if s.events else None)
    supplier = first_in.party if first_in and first_in.kind == "received" else ""
    holder = s.holder if s.place == "client" else ""
    source = "Customer return" if holder else ("Incoming inspection" if len(s.events) <= 1 else "Found in stock")
    details = [f"Serial {s.shown}: now {s.status.lower()} ({s.where})."]
    if first_in:
        details.append(f"Came in {first_in.stamp()} from {first_in.party or '?'} on {first_in.ref or 'no reference'}.")
    return {"sku": s.sku, "description": trace.names.get(s.sku, ""), "serials": [s.shown], "qty": 1, "supplier": supplier,
            "client": holder, "reference": first_in.ref if first_in else "", "batch": first_in.ref if first_in else "",
            "source": source, "details": " ".join(details)}


def suggestions(tables: Dict[str, List[dict]], cases: List[dict], trace=None) -> List[dict]:
    """Problems INVENTRA already recorded that have no quality case yet, as ready-made cases: customer returns for a
    fault or damage, and deliveries signed for as short, damaged or refused."""
    done = {str(c.get("source_key") or "") for c in cases}
    out = []

    def origin(serials):
        if trace is None or not serials:
            return "", ""
        f = from_serial(trace, serials[0]) or {}
        return f.get("supplier") or "", f.get("reference") or ""
    for r in tables.get("customerreturns") or []:
        key = f"customerreturns:{r.get('id')}"
        defect = defect_from_text(f"{r.get('reason')} {r.get('comments')}")
        if key in done or not defect:
            continue
        serials = serial_list(r.get("serials"))
        supplier, ref = origin(serials)
        out.append(prefill({"source_key": key, "source": "Customer return", "defect": defect, "sku": r.get("sku"),
                            "description": r.get("product_description"), "qty": num(r.get("qty")) or len(serials),
                            "serials": serials, "client": r.get("client_name") or "", "supplier": supplier,
                            "reference": f"Order {r.get('order_id')}" if r.get("order_id") else ref,
                            "details": f"Returned by {r.get('client_name') or 'a client'} on {str(r.get('datereturned') or '')[:10]}: "
                                       f"{r.get('reason') or ''}. {r.get('comments') or ''}".strip(),
                            "_why": f"Customer return: {r.get('reason')}", "_when": str(r.get("datereturned") or "")[:10]}))
    for t in tables.get("order_tracking") or []:
        pod = t.get("pod") or {}
        cond = pod.get("condition")
        if cond not in ("Short", "Damaged", "Rejected"):
            continue
        items = pod.get("issues") or [{"sku": "", "description": "", "qty": 0}]
        for i in items:
            key = f"pod:{t.get('order_id')}:{i.get('sku') or ''}"
            if key in done:
                continue
            defect = {"Short": "Wrong quantity", "Damaged": "Damaged"}.get(cond) or defect_from_text(pod.get("note")) or "Other"
            out.append(prefill({"source_key": key, "source": "Delivery damage", "defect": defect, "sku": i.get("sku"),
                                "description": i.get("description"), "qty": num(i.get("qty")), "serials": [],
                                "client": pod.get("received_by") or "", "reference": f"Order {t.get('order_id')}",
                                "details": f"Delivery of order {t.get('order_id')} signed as {cond.lower()} by "
                                           f"{pod.get('received_by') or '?'}: {pod.get('note') or ''}".strip(),
                                "_why": f"Delivery {cond.lower()}: order {t.get('order_id')}",
                                "_when": str(pod.get("at") or "")[:10]}))
    return sorted(out, key=lambda c: c.get("_when") or "", reverse=True)


def analysis(cases: List[dict]) -> Dict[str, List[dict]]:
    """Defects grouped by supplier, by product and by defect type (count of cases and units)."""
    out = {}
    for key, label in (("supplier", "Supplier"), ("sku", "Product"), ("defect", "Defect")):
        g = defaultdict(lambda: {"cases": 0, "units": 0.0, "open": 0})
        for c in cases:
            k = str(c.get(key) or "Not recorded")
            if key == "sku" and c.get("description"):
                k = f"{c.get('sku')} · {c.get('description')}"
            g[k]["cases"] += 1
            g[k]["units"] += num(c.get("qty")) or len(serial_list(c.get("serials")))
            g[k]["open"] += c.get("status") in OPEN
        out[label] = sorted(({"name": k, **v} for k, v in g.items()), key=lambda r: -r["cases"])
    return out
