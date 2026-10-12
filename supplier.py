"""The supplier portal (/s/<supplierid>-<code> on the tracking site). Each supplier has one private link (Purchasing >
Suppliers > Portal link): they see their open purchase orders, confirm or change the delivery date, upload an invoice
or delivery note, and answer quote requests online (the answer goes straight into Purchasing > Quotes).
Mixed into the tracking site's Tracker (needs self.db, self.secret, self._limit, self._failed)."""

import base64
import re
from datetime import date, datetime

import tracking_domain as TD

BUCKET = "supplier-docs"
DOC_TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


class SupplierError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("R", "").strip()) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class SupplierPortal:
    def _supplier(self, key: str, ip: str) -> dict:
        self._limit("sup:" + ip)
        sid = TD.supplier_from_key(self.secret, key)
        rows = self.db.table("addsuppliers").select("*").eq("supplierid", sid).execute().data if sid else []
        if not rows:
            self._failed("sup:" + ip)
            raise SupplierError("This link isn't valid. Ask the company for your portal link.", 404)
        return rows[0]

    def _all(self, table):
        try:
            return self.db.table(table).select("*").execute().data or []
        except Exception:
            return []

    def portal(self, key: str, ip: str) -> dict:
        s = self._supplier(key, ip)
        sid = str(s.get("supplierid"))
        items = {}
        for it in self._all("purchaseorderitems"):
            items.setdefault(str(it.get("purchaseorderid")), []).append(it)
        pos = []
        for p in self._all("purchaseorders"):
            if str(p.get("supplierid")) != sid or p.get("status") not in ("Pending", "Partial"):
                continue
            notes = str(p.get("notes") or "")
            pos.append({"po": p.get("ponumber"), "date": str(p.get("orderdate") or "")[:10],
                        "expected": str(p.get("amended_date") or p.get("expected_date") or "")[:10],
                        "status": p.get("status"), "confirmed": "Supplier confirmed" in notes,
                        "lines": [{"sku": i.get("sku"), "description": i.get("description") or i.get("sku"),
                                   "qty": max(0.0, (_num(i.get("ordered_qty") or i.get("qty")) or 0) - (_num(i.get("received_qty")) or 0))}
                                  for i in items.get(str(p.get("purchaseorderid")), [])]})
        rfqs = []
        for r in self._all("rfqs"):
            if r.get("status") not in ("Sent", "Quotes in") or not any(str(x.get("supplierid")) == sid for x in r.get("suppliers") or []):
                continue
            mine = next((q for q in r.get("quotes") or [] if str(q.get("supplierid")) == sid), None)
            rfqs.append({"rfq_id": r["rfq_id"], "title": r.get("title"), "needed_by": str(r.get("needed_by") or "")[:10],
                         "notes": r.get("notes") or "", "lines": [{"sku": l["sku"], "description": l.get("description"),
                                                                     "qty": l.get("qty"), "spec": l.get("spec") or ""}
                                                                    for l in r.get("lines") or []], "quote": mine})
        return {"supplier": s.get("suppliername"), "pos": pos, "rfqs": rfqs}

    def portal_po(self, body: dict, ip: str) -> dict:
        s = self._supplier(body.get("key"), ip)
        rows = [p for p in self._all("purchaseorders") if str(p.get("ponumber")) == str(body.get("po"))
                and str(p.get("supplierid")) == str(s.get("supplierid"))]
        if not rows:
            raise SupplierError("That order isn't yours.", 403)
        p = rows[0]
        try:
            when = datetime.strptime(str(body.get("date") or ""), "%Y-%m-%d").date()
        except ValueError:
            raise SupplierError("Choose the delivery date.")
        if when < date.today():
            raise SupplierError("The delivery date can't be in the past.")
        note = re.sub(r"\s+", " ", str(body.get("note") or ""))[:200]
        stamp = f"[{date.today():%d %b %Y}] Supplier confirmed on the portal: delivery by {when:%d %b %Y}" + \
                (f" ({note})" if note else "")
        change = {"notes": f"{(p.get('notes') or '').strip()}\n{stamp}".strip(), "updated_at": datetime.now().isoformat(timespec="seconds")}
        if not p.get("expected_date"):
            change["expected_date"] = when.isoformat()
        elif str(p.get("expected_date"))[:10] != when.isoformat():
            change["amended_date"] = when.isoformat()
        self.db.table("purchaseorders").update(change).eq("purchaseorderid", p["purchaseorderid"]).execute()
        return dict(self.portal(body.get("key"), ip), message=f"Thank you: delivery of {p.get('ponumber')} confirmed for "
                                                              f"{when:%d %b %Y}.")

    def portal_doc(self, body: dict, ip: str) -> dict:
        s = self._supplier(body.get("key"), ip)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(body.get("name") or "document"))[-80:]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in DOC_TYPES:
            raise SupplierError("Send a PDF or a photo (JPG or PNG).")
        raw = base64.b64decode(str(body.get("data") or "").split(",")[-1] or b"")
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise SupplierError("The file is empty or larger than 8 MB.")
        po = re.sub(r"[^A-Za-z0-9_-]+", "_", str(body.get("po") or "general"))
        key = f"{s.get('supplierid')}/{po}/{datetime.now():%Y%m%d%H%M%S}_{name}"
        try:
            self.db.storage.from_(BUCKET).upload(key, raw, {"content-type": DOC_TYPES[ext]})
        except Exception:
            raise SupplierError("The document couldn't be stored right now. Please e-mail it instead.", 503)
        rows = [p for p in self._all("purchaseorders") if str(p.get("ponumber")) == str(body.get("po"))
                and str(p.get("supplierid")) == str(s.get("supplierid"))]
        if rows:
            p = rows[0]
            self.db.table("purchaseorders").update({"notes": f"{(p.get('notes') or '').strip()}\n[{date.today():%d %b %Y}] "
                                                             f"Supplier uploaded {name} (supplier-docs/{key})".strip()}) \
                .eq("purchaseorderid", p["purchaseorderid"]).execute()
        return {"ok": True, "message": f"{name} received. Thank you."}

    def portal_quote(self, body: dict, ip: str) -> dict:
        s = self._supplier(body.get("key"), ip)
        sid = str(s.get("supplierid"))
        rows = [r for r in self._all("rfqs") if r.get("rfq_id") == body.get("rfq_id")]
        if not rows:
            raise SupplierError("That request isn't open any more.", 404)
        r = rows[0]
        if r.get("status") not in ("Sent", "Quotes in") or not any(str(x.get("supplierid")) == sid for x in r.get("suppliers") or []):
            raise SupplierError("That request isn't open for you any more.", 409)
        declined = bool(body.get("declined"))
        prices = {}
        for l in r.get("lines") or []:
            v = _num((body.get("prices") or {}).get(l["sku"]))
            if v is not None:
                if v < 0:
                    raise SupplierError("A price can't be negative.")
                prices[l["sku"]] = v
        if not declined and not prices:
            raise SupplierError("Enter a unit price for at least one item (or tick that you can't quote).")
        valid = str(body.get("valid_until") or "")[:10] or None
        if valid:
            try:
                datetime.strptime(valid, "%Y-%m-%d")
            except ValueError:
                raise SupplierError("Valid until: choose a date.")
        q = {"supplierid": s.get("supplierid"), "suppliername": s.get("suppliername"),
             "received_at": datetime.now().isoformat(timespec="seconds"), "reference": str(body.get("reference") or "")[:60],
             "valid_until": valid, "prices": prices, "vat_included": bool(body.get("vat_included")),
             "delivery_fee": _num(body.get("delivery_fee")) or 0.0,
             "lead_days": int(_num(body.get("lead_days")) or 0) if body.get("lead_days") not in (None, "") else None,
             "payment_terms": str(body.get("payment_terms") or "")[:40],
             "warranty_months": int(_num(body.get("warranty_months")) or 0) if body.get("warranty_months") not in (None, "") else None,
             "notes": str(body.get("notes") or "")[:300], "declined": declined, "via": "portal"}
        quotes = [x for x in r.get("quotes") or [] if str(x.get("supplierid")) != sid] + [q]
        status = r["status"]
        if all(any(str(x.get("supplierid")) == str(i.get("supplierid")) for x in quotes) for i in r.get("suppliers") or []) \
                or sum(1 for x in quotes if not x.get("declined")) >= 2:
            status = "Quotes in"
        self.db.table("rfqs").update({"quotes": quotes, "status": status,
                                      "updated_at": datetime.now().isoformat(timespec="seconds")}).eq("rfq_id", r["rfq_id"]).execute()
        return dict(self.portal(body.get("key"), ip), message="Thank you: your quote was received." if not declined
                    else "Thank you for letting us know.")
