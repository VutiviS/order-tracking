"""Stock on hand, worked out from the source documents (shared by Stock
Control, Dispatch, Finance and the Production Planner).

    on hand = received on supplier invoices
            + made on the production line (good units)
            + returned by customers
            − dispatched to customers
            − returned to suppliers
            − written off (expired / damaged)
            − used up as parts in production
            ± approved stock-count adjustments (counted on the shelf, approved by a second person)

Only INV_ SKUs are stock (inventra_sku.py); a DELIVERY line on a despatch is
a charge, never stock. Serial numbers are compared without regard to case
or spaces: they were received as "abc123" and scanned out as "ABC123".

The stock ledger table is NOT used: older database triggers write extra rows
into it, so its balances can't be trusted."""

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from inventra_sku import is_inventory_sku


def num(value) -> float:
    try:
        return float(str(value).replace("R", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def serials_of(value) -> List[str]:
    """Serials stored as a JSON list, a JSON string, or comma-separated text."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if x and str(x).strip() not in ("", "[]")]
    text = str(value).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if x and str(x).strip()]
        return [str(parsed).strip()]
    except (ValueError, TypeError):
        return [x.strip() for x in text.split(",") if x.strip() and x.strip() != "[]"]


def key(serial: str) -> str:
    return "".join(str(serial).split()).upper()


@dataclass
class Holding:
    sku: str
    received: float = 0.0
    produced: float = 0.0
    returned_in: float = 0.0
    dispatched: float = 0.0
    returned_out: float = 0.0
    written_off: float = 0.0
    consumed: float = 0.0
    adjusted: float = 0.0                                    # approved stock-count corrections (+ or -)
    serials: Dict[str, str] = field(default_factory=dict)   # KEY -> serial as received

    @property
    def qty(self) -> float:
        return (self.received + self.produced + self.returned_in - self.dispatched - self.returned_out
                - self.written_off - self.consumed + self.adjusted)

    @property
    def loose(self) -> float:
        """Units on hand without a serial number."""
        return max(0.0, self.qty - len(self.serials))


def on_hand(invoice_items: Iterable[dict], dispatched: Iterable[dict], customer_returns: Iterable[dict] = (),
            supplier_returns: Iterable[dict] = (), removals: Iterable[dict] = (),
            production_runs: Iterable[dict] = (), adjustments: Iterable[dict] = ()) -> Dict[str, Holding]:
    stock: Dict[str, Holding] = {}

    def h(sku) -> Optional[Holding]:
        sku = str(sku or "").strip()
        if not is_inventory_sku(sku):
            return None
        if sku not in stock:
            stock[sku] = Holding(sku)
        return stock[sku]

    for it in invoice_items:
        x = h(it.get("sku"))
        if x:
            x.received += num(it.get("qty"))
            for s in serials_of(it.get("serials")):
                x.serials[key(s)] = s
    for r in production_runs:
        x = h(r.get("product_sku"))
        if x:
            x.produced += num(r.get("good_qty"))
        for m in materials_of(r):
            y = h(m.get("sku"))
            if y:
                y.consumed += num(m.get("qty"))
    for r in customer_returns:
        x = h(r.get("sku"))
        if x:
            x.returned_in += num(r.get("qty") or 1)
            for s in serials_of(r.get("serials")):
                x.serials[key(s)] = s
    for r in dispatched:
        x = h(r.get("sku"))
        if x:
            x.dispatched += num(r.get("dispatched_quantity"))
            for s in serials_of(r.get("serials")):
                x.serials.pop(key(s), None)
    for r in supplier_returns:
        x = h(r.get("sku"))
        if x:
            x.returned_out += num(r.get("qty") or 1)
            for s in serials_of(r.get("serials")):
                x.serials.pop(key(s), None)
    for r in removals:
        x = h(r.get("sku"))
        if x:
            x.written_off += num(r.get("quantity") or r.get("qty") or 1)
            for s in serials_of(r.get("serial_numbers") or r.get("serials")):
                x.serials.pop(key(s), None)
    for r in adjustments:
        x = h(r.get("sku"))
        if x and (r.get("status") or "Approved") == "Approved":
            x.adjusted += num(r.get("qty_change"))
    return stock


def materials_of(run: dict) -> List[dict]:
    m = run.get("materials_used")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    return [x for x in (m or []) if isinstance(x, dict)]


def lots(stock: Dict[str, Holding], invoice_items: Iterable[dict], invoices: Iterable[dict],
         stock_rows: Iterable[dict], names: Optional[Dict[str, str]] = None) -> List[dict]:
    """What is on the shelf, one row per delivery ("lot"), using the true quantities above.

    Each invoice line of a stock item is a lot (with the bin, expiry date and notes the stock controller
    recorded in stock_items, where there is a row). Units made on the production line or returned by
    customers form one more lot. What has left the warehouse is taken from the oldest lots first, so the
    newest deliveries are what remains. A SKU with more out than in gets one row with the negative
    quantity and a `problem`, so it can be investigated instead of silently showing nothing."""
    names = names or {}
    inv_by_id = {str(i.get("invoiceid")): i for i in invoices}
    extra_rows: Dict[tuple, dict] = {}
    for r in stock_rows:
        extra_rows.setdefault((str(r.get("sku")), str(r.get("invoice_number") or "")), r)
    by_sku: Dict[str, List[dict]] = {}
    for it in invoice_items:
        sku = str(it.get("sku") or "").strip()
        if sku not in stock:
            continue
        inv = inv_by_id.get(str(it.get("invoiceid")), {})
        number = str(inv.get("invoice_number") or "")
        row = extra_rows.get((sku, number), {})
        by_sku.setdefault(sku, []).append({
            "sku": sku, "product_description": row.get("product_description") or it.get("productdescription")
            or names.get(sku, ""), "received_qty": num(it.get("qty")), "invoice_number": number,
            "supplier_name": inv.get("suppliername") or row.get("supplier_name") or "",
            "received_date": str(inv.get("invoicedate") or row.get("received_date") or "")[:10],
            "bin_code": row.get("bin_code"), "expiry_date": str(row.get("expiry_date") or "")[:10] or None,
            "notes": row.get("notes"), "stock_id": row.get("stock_id"), "expiry_actions": row.get("expiry_actions"),
            "serials_in": [key(s) for s in serials_of(it.get("serials"))], "source": "Supplier invoice"})
    out = []
    for sku, h in stock.items():
        qty = h.qty
        desc = names.get(sku, "")
        if qty < 0:
            out.append({"sku": sku, "product_description": desc, "quantity": qty, "bin_code": None, "expiry_date": None,
                        "received_date": "", "invoice_number": "", "supplier_name": "", "serial_numbers": [],
                        "stock_id": None, "source": "—",
                        "problem": f"{-qty:g} more went out than came in (in {h.received + h.produced + h.returned_in:g}, "
                                   f"out {h.dispatched + h.returned_out + h.written_off + h.consumed:g}) — count the "
                                   "shelf and record a stock count (Stock Control) to correct it"})
            continue
        remaining = qty
        candidates = sorted(by_sku.get(sku, []), key=lambda l: l["received_date"], reverse=True)   # newest first
        own = h.produced + h.returned_in + max(0.0, h.adjusted)
        if own > 0:
            candidates.insert(0, {"sku": sku, "product_description": desc, "received_qty": own, "invoice_number": "",
                                  "supplier_name": "", "received_date": "", "bin_code": None, "expiry_date": None,
                                  "notes": None, "stock_id": None, "serials_in": [],
                                  "source": "Stock count" if h.adjusted > 0 and not (h.produced or h.returned_in) else
                                  "Production" if h.produced and not h.returned_in else
                                  "Customer returns" if not h.produced else "Production / returns"})
        left_serials = dict(h.serials)
        for lot in candidates:
            if remaining <= 0:
                break
            take = min(lot["received_qty"], remaining)
            remaining -= take
            mine = [left_serials.pop(k) for k in list(lot.pop("serials_in")) if k in left_serials]
            out.append(dict(lot, quantity=take, serial_numbers=mine, problem=""))
        if remaining > 0:                                # on hand but no matching delivery record
            out.append({"sku": sku, "product_description": desc, "quantity": remaining, "bin_code": None,
                        "expiry_date": None, "received_date": "", "invoice_number": "", "supplier_name": "",
                        "serial_numbers": list(left_serials.values()), "stock_id": None, "source": "Unrecorded",
                        "problem": ""})
        elif left_serials and out and out[-1]["sku"] == sku:
            out[-1]["serial_numbers"] += list(left_serials.values())
    return out


def quantities(stock: Dict[str, Holding]) -> Dict[str, float]:
    return {s: h.qty for s, h in stock.items()}


def reorder_status(qty: float, reorder_point: float) -> str:
    if qty <= 0:
        return "Out of stock"
    if reorder_point and qty <= reorder_point:
        return "Reorder now"
    return "OK"


def load(repo_table, safe=True) -> Dict[str, Holding]:
    """Read every source table through repo_table(name) -> rows (missing
    optional tables count as empty) and work out stock on hand."""
    def rows(name, select="*"):
        try:
            return repo_table(name, select)
        except Exception:
            if not safe or name in ("invoiceitems", "despatching"):
                raise
            return []
    from concurrent.futures import ThreadPoolExecutor
    wanted = [("invoiceitems", "sku, qty, serials"), ("despatching", "sku, dispatched_quantity, serials"),
              ("customerreturns", "*"), ("supplier_returns", "*"), ("inventory_removals", "*"), ("production_runs", "*"),
              ("stock_adjustments", "*")]
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:      # the tables are read side by side
        got = dict(zip([w[0] for w in wanted], pool.map(lambda w: rows(*w), wanted)))
    return on_hand(got["invoiceitems"], got["despatching"], got["customerreturns"], got["supplier_returns"],
                   got["inventory_removals"], got["production_runs"],
                   [a for a in got["stock_adjustments"] if a.get("status") == "Approved"])
