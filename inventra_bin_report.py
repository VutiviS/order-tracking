"""What is in one bin, and what happened to it: the screen you get when you scan a bin label.

For a bin code (as printed on the label, any case) it works out, from the same records as the Storage bins screen
(inventra_bins) and the stock figures (inventra_stock):

  per product in the bin
    binned       units put into this bin (received into it, allocated, moved in, put away from a production run)
    moved out    units moved from this bin to another bin, or taken out of it
    dispatched   units that left the warehouse from this bin (dispatched, written off, used in production) -
                 the oldest stock leaves first, the same rule the bin contents use
    available    units in the bin now
    expiry       the expiry date of the stock in the bin: from the deliveries received into this bin (Procurement >
                 Receive); when none of them has a date, the product's earliest expiry date (marked so)
    status       Expired / Expiring soon (within the expiry warning days of Procurement) / OK / no date

  the movements of the bin, newest first: binned, moved in / out, taken out, put away from production, and the
  dispatches its units left in."""

import json
import os as _os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import inventra_bins
import inventra_stock

CORE = ("invoiceitems", "despatching")
TABLES = ("stock_bins", "bins", "stock_bin_moves", "production_runs", "despatching", "invoiceitems", "invoices",
          "stock_items", "products", "customerreturns", "supplier_returns", "inventory_removals", "stock_adjustments")
DEFAULT_WARN_DAYS = 30


def warning_days() -> int:
    """Procurement's 'expiry warning' period (Procurement > Settings / Expiry alerts), 30 days if not set. (On the
    website, where that settings file isn't, INVENTRA_EXPIRY_WARNING_DAYS can set it.)"""
    env = _os.environ.get("INVENTRA_EXPIRY_WARNING_DAYS", "").strip()
    if env.isdigit():
        return int(env)
    try:
        path = Path(__file__).resolve().parent / "inventra_settings.json"
        return max(0, int(json.loads(path.read_text(encoding="utf-8")).get("expiry_warning_days", DEFAULT_WARN_DAYS)))
    except Exception:
        return DEFAULT_WARN_DAYS


def load(read: Callable[[str], List[dict]]) -> Dict[str, List[dict]]:
    """Every table the bin view needs, read side by side. Tables that don't exist yet count as empty."""
    def get(name):
        try:
            return read(name) or []
        except Exception:
            if name in CORE:
                raise
            return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(TABLES, pool.map(get, TABLES)))


def _d(value) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(value)[:10]).date() if value else None
    except ValueError:
        return None


def _when(value) -> str:
    return str(value or "")[:16].replace("T", " ")


def _status(expiry: Optional[date], warn: int, today: date):
    if not expiry:
        return "none", None
    days = (expiry - today).days
    return ("expired" if days < 0 else "expiring" if days <= warn else "ok"), days


STATUS_TEXT = {"expired": "Expired", "expiring": "Expiring soon", "ok": "OK", "none": "No expiry date"}


def known_codes(tables: Dict[str, List[dict]]) -> Dict[str, str]:
    """UPPER code -> code as written, for every bin in either bin list (and bins only seen in the bin ledger)."""
    out = {}
    for b in tables.get("bins", []) + tables.get("stock_bins", []):
        code = str(b.get("bin_code") or "").strip()
        if code:
            out[code.upper()] = code
    for m in tables.get("stock_bin_moves", []):
        code = str(m.get("bin_code") or "").strip()
        if code:
            out.setdefault(code.upper(), code)
    return out


def report(tables: Dict[str, List[dict]], scanned: str, warn: Optional[int] = None,
           today: Optional[date] = None) -> Optional[dict]:
    """Everything the bin view shows for one scanned code, or None when there is no such bin."""
    today = today or date.today()
    warn = warning_days() if warn is None else warn
    want = "".join(str(scanned or "").split()).upper()
    codes = known_codes(tables)
    if not want or want not in codes:
        return None
    code = codes[want]

    # the bin itself (Stock Control's list first, Procurement's list for bins only it has)
    info = {"code": code, "zone": "", "description": "", "active": True, "listed": False}
    for b in tables.get("bins", []):
        if str(b.get("bin_code") or "").strip().upper() == want:
            info.update(description=inventra_bins.real_description(b.get("description")), listed=True,
                        active=str(b.get("status") or "Active").lower() != "inactive")
    for b in tables.get("stock_bins", []):
        if str(b.get("bin_code") or "").strip().upper() == want:
            info.update(zone=b.get("zone") or "",
                        description=inventra_bins.real_description(b.get("description")) or info["description"],
                        active=b.get("active") is not False, listed=True)

    names = {str(p.get("sku")): str(p.get("productdescription") or p.get("sku") or "") for p in tables["products"]}
    holdings = inventra_stock.on_hand(tables["invoiceitems"], tables["despatching"], tables["customerreturns"],
                                      tables["supplier_returns"], tables["inventory_removals"],
                                      tables["production_runs"],
                                      [a for a in tables["stock_adjustments"] if a.get("status") == "Approved"])

    def on_hand(sku):
        return max(0.0, holdings[sku].qty) if sku in holdings else 0.0

    runs, ledger, receipts = tables["production_runs"], tables["stock_bin_moves"], tables["stock_items"]
    contents = inventra_bins.contents(runs, ledger, on_hand, receipts=receipts)
    now = {}
    for c in contents:
        if str(c["bin"]).upper() == want:
            now[c["sku"]] = now.get(c["sku"], 0.0) + c["qty"]

    # what went in and out of this bin, product by product, and the dated movements
    per: Dict[str, dict] = {}
    history: List[dict] = []

    def row(sku):
        return per.setdefault(sku, {"sku": sku, "description": names.get(sku, sku), "binned": 0.0, "moved_out": 0.0,
                                    "first_in": None})
    for m in ledger:
        if str(m.get("bin_code") or "").strip().upper() != want or not m.get("sku"):
            continue
        q = inventra_bins.num(m.get("qty"))
        if not q:
            continue
        sku, note = str(m["sku"]), str(m.get("note") or "")
        r = row(sku)
        when = _when(m.get("created_at"))
        if q > 0:
            r["binned"] += q
            r["first_in"] = min(filter(None, [r["first_in"], when])) if when else r["first_in"]
            kind = "Moved in" if note.lower().startswith("moved from") else "Binned"
        else:
            r["moved_out"] += -q
            kind = "Moved out" if note.lower().startswith("moved to") else "Taken out"
        history.append({"when": when, "movement": kind, "sku": sku, "description": names.get(sku, sku), "qty": q,
                        "detail": note})
    for rc in inventra_bins.receipts_into_bins(receipts):
        if rc["bin"].strip().upper() != want:
            continue
        r = row(rc["sku"])
        r["binned"] += rc["qty"]
        when = rc["date"]
        r["first_in"] = min(filter(None, [r["first_in"], when])) if when else r["first_in"]
        history.append({"when": when, "movement": "Received into bin", "sku": rc["sku"],
                        "description": names.get(rc["sku"], rc["sku"]), "qty": rc["qty"],
                        "detail": " · ".join(x for x in (f"Invoice {rc['invoice']}" if rc["invoice"] else "",
                                                         rc["supplier"]) if x)})
    for run in runs:
        for a in inventra_bins.allocations_of(run):
            if str(a["bin"]).strip().upper() != want:
                continue
            sku = str(run.get("product_sku") or "")
            r = row(sku)
            r["binned"] += a["qty"]
            when = _when(run.get("run_date"))
            r["first_in"] = min(filter(None, [r["first_in"], when])) if when else r["first_in"]
            history.append({"when": when, "movement": "Binned", "sku": sku, "description": names.get(sku, sku),
                            "qty": a["qty"], "detail": f"Put away from production run {run.get('run_id') or ''}".strip()})
    for sku in now:
        row(sku)

    # units that left the warehouse from this bin, shown against the dispatches they left in (oldest first)
    by_sku_desp: Dict[str, List[dict]] = {}
    for d in tables["despatching"]:
        by_sku_desp.setdefault(str(d.get("sku") or ""), []).append(d)
    for sku, r in per.items():
        left = max(0.0, r["binned"] - r["moved_out"] - now.get(sku, 0.0))
        r["dispatched"] = left
        if left <= 1e-9:
            continue
        for d in sorted(by_sku_desp.get(sku, []), key=lambda x: str(x.get("dispatch_date") or "")):
            if left <= 1e-9:
                break
            when = _when(d.get("dispatch_date"))
            if r["first_in"] and when and when < r["first_in"][:len(when)]:
                continue                                    # before anything was put in this bin
            q = min(inventra_bins.num(d.get("dispatched_quantity")), left)
            if q <= 0:
                continue
            left -= q
            order = d.get("order_id") or d.get("orderid") or d.get("order_number") or ""
            client = d.get("client") or d.get("client_name") or d.get("customer") or ""
            history.append({"when": when, "movement": "Dispatched", "sku": sku, "description": names.get(sku, sku),
                            "qty": -q, "detail": " · ".join(str(x) for x in (f"Order {order}" if order else "", client)
                                                            if x) or "Left the warehouse"})
        if left > 1e-9:                                     # written off, used in production, returned to supplier
            history.append({"when": "", "movement": "Left the warehouse", "sku": sku,
                            "description": names.get(sku, sku), "qty": -left,
                            "detail": "Written off, used in production or returned to the supplier"})

    # expiry of what is in the bin
    lots = inventra_stock.lots(holdings, tables["invoiceitems"], tables["invoices"],
                               [s for s in tables["stock_items"] if (s.get("product_type") or "inventory") == "inventory"],
                               names)
    products = []
    for sku, r in per.items():
        have = now.get(sku, 0.0)
        mine = [l for l in lots if l["sku"] == sku and l.get("quantity", 0) > 0 and _d(l.get("expiry_date"))]
        here = [l for l in mine if str(l.get("bin_code") or "").strip().upper() == want]
        source = "this bin" if here else ("product" if mine else "")
        dated = sorted(here or mine, key=lambda l: _d(l["expiry_date"]))
        expiry = _d(dated[0]["expiry_date"]) if dated else None
        status, days = _status(expiry, warn, today) if have > 1e-9 else ("none", None)
        soon = expired = 0.0
        if have > 1e-9 and dated:
            left = have
            for l in dated:                                 # the stock that expires first is used first
                if left <= 1e-9:
                    break
                q = min(l["quantity"], left)
                left -= q
                s, _dd = _status(_d(l["expiry_date"]), warn, today)
                if s == "expired":
                    expired += q
                elif s == "expiring":
                    soon += q
        products.append(dict(r, available=have, expiry=expiry.isoformat() if expiry else "", expiry_days=days,
                             expiry_source=source, status=status if have > 1e-9 else "empty",
                             status_text=(STATUS_TEXT[status] if have > 1e-9 else "Nothing left in this bin"),
                             expiring_qty=soon, expired_qty=expired))
    products.sort(key=lambda p: (p["available"] <= 1e-9, {"expired": 0, "expiring": 1}.get(p["status"], 2),
                                 p["description"].lower()))
    history.sort(key=lambda h: h["when"] or "", reverse=True)
    totals = {"products": sum(1 for p in products if p["available"] > 1e-9),
              "available": sum(p["available"] for p in products), "binned": sum(p["binned"] for p in products),
              "moved_out": sum(p["moved_out"] for p in products), "dispatched": sum(p["dispatched"] for p in products),
              "expiring": sum(p["expiring_qty"] for p in products), "expired": sum(p["expired_qty"] for p in products)}
    return {"bin": info, "products": products, "history": history, "totals": totals, "warn_days": warn,
            "as_of": datetime.now().strftime("%d %b %Y %H:%M")}
