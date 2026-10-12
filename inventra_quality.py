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
