"""Warehouse work on a phone or a cheap scanner (/warehouse on the tracking site). Staff sign in with their INVENTRA
username and password. Everything is checked before it is saved (the right product, quantity and bin), and every
movement records who did it and when.

  pick      orders waiting to be dispatched: the pick list with bins, scan each product or serial number, then
            "packed" (the customer sees "being prepared"; Dispatch fills in the scanned serial numbers);
  receive   check a delivery against its purchase order: scan what arrived; shortages and extras are flagged
            (they become exceptions in the Control Tower);
  move      put away or move stock between bins (the bin ledger the desktop apps use);
  count     count a bin: the difference becomes a stock count waiting for approval in Stock Control;
  look up   scan anything (product, serial, bin) to see what and where it is.

Mixed into the tracking site's Tracker (it needs self.db, self._rows, self._limit, self._failed)."""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime

import inventra_bins
import inventra_stock
import tracking_domain as TD

TOKEN_DAYS = 14
TASKS = "warehouse_tasks"
ROLE_CAN = {
    "pick": {"Administrator", "Dispatch officer", "Logistics officer", "Stock controller"},
    "receive": {"Administrator", "Stock controller", "Logistics officer"},
    "move": {"Administrator", "Stock controller", "Production officer"},
    "count": {"Administrator", "Stock controller"},
}


class WarehouseError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _secret() -> bytes:
    return hashlib.sha256(("inventra-warehouse:" + os.environ.get("SUPABASE_KEY", "")).encode()).digest()


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return raw + "." + hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]


def _roles(u: dict) -> list:
    r = u.get("roles")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except ValueError:
            r = [x.strip() for x in r.split(",")]
    return [str(x) for x in r or []]


def num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


class Warehouse:
    # ---- signing in ----
    def wh_login(self, body: dict, ip: str) -> dict:
        self._limit("wh:" + ip)
        name = str(body.get("username") or "").strip().lower()
        rows = [u for u in self.db.table("app_users").select("*").execute().data or []
                if str(u.get("username") or "").lower() == name and u.get("active") is not False]
        u = rows[0] if rows else None
        if not u or not TD.pin_ok(u.get("password_hash") or "", str(body.get("password") or "")):
            self._failed("wh:" + ip)
            raise WarehouseError("Username or password not right (the same as for INVENTRA).", 403)
        can = sorted(k for k, roles in ROLE_CAN.items() if set(_roles(u)) & roles)
        if not can:
            raise WarehouseError("Your job role has no warehouse work. Ask your administrator.", 403)
        tag = hashlib.sha256(str(u.get("password_hash")).encode()).hexdigest()[:12]
        token = _sign({"u": u["username"], "x": int(time.time()) + TOKEN_DAYS * 86400, "h": tag})
        return {"token": token, "name": u.get("full_name") or u["username"], "can": can}

    def _who(self, token: str, need: str = None) -> dict:
        try:
            raw, sig = str(token).split(".", 1)
            if not hmac.compare_digest(sig, hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]):
                raise ValueError
            p = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            if p["x"] < time.time():
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise WarehouseError("Please sign in again.", 401)
        rows = self.db.table("app_users").select("*").eq("username", p["u"]).execute().data or []
        if not rows or rows[0].get("active") is False or \
                hashlib.sha256(str(rows[0].get("password_hash")).encode()).hexdigest()[:12] != p["h"]:
            raise WarehouseError("Please sign in again.", 401)
        u = rows[0]
        if need and not set(_roles(u)) & ROLE_CAN[need]:
            raise WarehouseError("Your job role can't do this.", 403)
        return u

    # ---- shared figures ----
    def _products(self) -> dict:
        return {str(p.get("sku")): p for p in self.db.table("products").select("*").execute().data or [] if p.get("sku")}

    def _stock(self) -> dict:
        return inventra_stock.load(lambda name, select="*": self.db.table(name).select(select).execute().data or [])

    def _bins_state(self, holdings, products):
        qty = {k: h.qty for k, h in holdings.items()}
        return inventra_bins.build_state(self._all("stock_bins"),
                                         self._all("production_runs"), self._all("stock_bin_moves"),
                                         lambda s: qty.get(s, 0.0),
                                         {k: p.get("productdescription") or k for k, p in products.items()})

    def _all(self, table):
        try:
            return self.db.table(table).select("*").execute().data or []
        except Exception:
            return []

    def _identify(self, code: str, products: dict, holdings: dict) -> dict:
        """What a scanned code is: a product (SKU or its barcode/identifier), a serial number or a bin."""
        c = str(code or "").strip()
        if not c:
            raise WarehouseError("Nothing scanned.")
        up = c.upper()
        for sku, p in products.items():
            if up in (sku.upper(), str(p.get("identifier") or "").strip().upper(), str(p.get("model") or "").strip().upper()) \
                    and up:
                return {"type": "product", "sku": sku, "description": p.get("productdescription") or sku}
        for sku, h in holdings.items():
            if up in h.serials:
                return {"type": "serial", "sku": sku, "serial": h.serials[up],
                        "description": (products.get(sku) or {}).get("productdescription") or sku, "in_stock": True}
        for it in self._all("invoiceitems"):
            for s in inventra_stock.serials_of(it.get("serials")):
                if s.upper() == up:
                    return {"type": "serial", "sku": it.get("sku"), "serial": s, "in_stock": False,
                            "description": (products.get(str(it.get("sku"))) or {}).get("productdescription") or it.get("sku")}
        for b in self._all("stock_bins"):
            if str(b.get("bin_code") or "").upper() == up:
                return {"type": "bin", "bin": b["bin_code"], "description": b.get("description") or ""}
        raise WarehouseError(f"{c} isn't a product, serial number or bin we know.", 404)

    def wh_lookup(self, body: dict) -> dict:
        self._who(body.get("token"))
        products, holdings = self._products(), self._stock()
        what = self._identify(body.get("code"), products, holdings)
        state = self._bins_state(holdings, products)
        if what["type"] in ("product", "serial"):
            what["on_hand"] = holdings[what["sku"]].qty if what["sku"] in holdings else 0.0
            what["where"] = inventra_bins.where_is(state, what["sku"]) or "not in a bin"
        else:
            what["holds"] = [{"sku": c["sku"], "description": state["names"].get(c["sku"], c["sku"]), "qty": c["qty"]}
                             for c in state["contents"] if c["bin"] == what["bin"]]
        return what

    def wh_bins(self, body: dict) -> dict:
        self._who(body.get("token"))
        return {"bins": sorted(b["bin_code"] for b in self._all("stock_bins") if b.get("active") is not False)}

    # ---- picking and packing ----
    def wh_orders(self, body: dict) -> dict:
        self._who(body.get("token"), "pick")
        rows = self.db.table("customer_order").select("*").eq("status", "Active").execute().data or []
        done = {str(d.get("order_id")) for d in self._all("despatching")}
        packed = {str(t.get("ref")) for t in self._all(TASKS) if t.get("kind") == "pick" and t.get("status") == "Packed"}
        products, holdings = self._products(), self._stock()
        state = self._bins_state(holdings, products)
        fefo = {}                                  # first expired, first out: the lot to take first
        for it in self._all("stock_items"):
            if it.get("expiry_date") and num(it.get("quantity")) > 0:
                k = str(it.get("sku"))
                if k not in fefo or str(it["expiry_date"]) < str(fefo[k]["expiry_date"]):
                    fefo[k] = it
        by = defaultdict(list)
        for r in rows:
            if str(r.get("order_id")) not in done:
                by[str(r.get("order_id"))].append(r)
        out = []
        for oid, lines in sorted(by.items()):
            o = TD.group_order(lines)
            items = []
            for r in lines:
                sku = str(r.get("sku") or "")
                if not sku or sku == TD.DELIVERY_SKU:
                    continue
                price, value = num(r.get("price")), num(r.get("value"))
                qty = max(1, round(value / price)) if price > 0 and value > 0 else 1
                p = products.get(sku) or {}
                h = holdings.get(sku)
                items.append({"sku": sku, "description": p.get("productdescription") or r.get("product_description") or sku,
                              "qty": qty, "where": inventra_bins.where_is(state, sku) or "not in a bin",
                              "serialized": str(p.get("serialized") or "").lower() in ("yes", "true", "1"),
                              "on_hand": h.qty if h else 0.0,
                              "fefo": (f"take first: lot expiring {str(fefo[sku]['expiry_date'])[:10]}"
                                       + (f" in {fefo[sku].get('bin_code')}" if fefo[sku].get("bin_code") else "")
                                       if sku in fefo else "")})
            out.append({"order_id": oid, "client": o["client_name"], "date": o["order_date"], "items": items,
                        "packed": oid in packed})
        return {"orders": out}

    def wh_pick(self, body: dict) -> dict:
        u = self._who(body.get("token"), "pick")
        oid = TD.clean_order_id(body.get("order_id"))
        orders = {o["order_id"]: o for o in self.wh_orders(body)["orders"]}
        o = orders.get(oid)
        if not o:
            raise WarehouseError("That order isn't waiting to be dispatched.", 404)
        products, holdings = self._products(), self._stock()
        picked = {str(l.get("sku")): l for l in body.get("lines") or []}
        problems, lines = [], []
        seen = set()
        for it in o["items"]:
            got = picked.get(it["sku"]) or {}
            qty = num(got.get("qty"))
            serials = [str(s).strip() for s in got.get("serials") or [] if str(s).strip()]
            for s in serials:
                h = holdings.get(it["sku"])
                if s.upper() in seen:
                    problems.append(f"Serial {s} was scanned twice.")
                seen.add(s.upper())
                if not h or s.upper() not in h.serials:
                    problems.append(f"Serial {s} isn't in stock for {it['description']}.")
            if it["serialized"] and len(serials) != qty:
                problems.append(f"{it['description']}: scan a serial number for every unit ({len(serials)} of {qty:g}).")
            if qty != it["qty"]:
                problems.append(f"{it['description']}: {qty:g} picked, the order needs {it['qty']:g}.")
            lines.append({"sku": it["sku"], "description": it["description"], "qty": qty, "serials": serials})
        if problems and not body.get("accept_short"):
            raise WarehouseError(" ".join(problems), 409)
        now = datetime.now().replace(microsecond=0)
        self.db.table(TASKS).insert({"task_id": "PICK-" + uuid.uuid4().hex[:8].upper(), "kind": "pick", "ref": oid,
                                     "status": "Packed", "lines": lines, "discrepancies": problems,
                                     "notes": str(body.get("note") or "")[:300], "by": u["username"],
                                     "by_name": u.get("full_name"), "created_at": now.isoformat(),
                                     "updated_at": now.isoformat()}).execute()
        track = (self._rows(TD.TABLE, "order_id", oid) or [None])[0]
        row = TD.add_event(track, "preparing", "Picked and packed", by=u.get("full_name") or u["username"], when=now)
        row["order_id"] = oid
        try:
            self.db.table(TD.TABLE).upsert({k: v for k, v in row.items() if k != "created_at"},
                                           on_conflict="order_id").execute()
        except Exception:
            pass                                   # tracking table not set up yet: the pick is still recorded
        return {"ok": True, "message": f"Order {oid} packed" + (" (with differences noted)" if problems else "") + "."}

    # ---- goods received check ----
    def wh_po(self, body: dict) -> dict:
        self._who(body.get("token"), "receive")
        num_ = str(body.get("po") or "").strip()
        pos = [p for p in self._all("purchaseorders") if str(p.get("ponumber") or "").strip().lower() == num_.lower()]
        if not pos:
            raise WarehouseError(f"Purchase order {num_} not found.", 404)
        po = pos[0]
        items = [i for i in self._all("purchaseorderitems") if str(i.get("purchaseorderid")) == str(po.get("purchaseorderid"))]
        sup = next((s.get("suppliername") for s in self._all("addsuppliers") if str(s.get("supplierid")) == str(po.get("supplierid"))), "")
        return {"po": po.get("ponumber"), "supplier": sup, "status": po.get("status"),
                "lines": [{"sku": i.get("sku"), "description": i.get("description") or i.get("sku"),
                           "open": max(0.0, num(i.get("ordered_qty") or i.get("qty")) - num(i.get("received_qty")))}
                          for i in items]}

    def wh_receive(self, body: dict) -> dict:
        u = self._who(body.get("token"), "receive")
        po = self.wh_po(body)
        got = {str(l.get("sku")): l for l in body.get("lines") or []}
        lines, diffs = [], []
        for l in po["lines"]:
            g = got.get(l["sku"]) or {}
            q = num(g.get("qty"))
            damaged = num(g.get("damaged"))
            lines.append({"sku": l["sku"], "description": l["description"], "expected": l["open"], "received": q,
                          "damaged": damaged, "serials": [str(s) for s in g.get("serials") or []]})
            if q != l["open"]:
                diffs.append(f"{l['description']}: {q:g} received, {l['open']:g} expected")
            if damaged:
                diffs.append(f"{l['description']}: {damaged:g} damaged")
        extra = [k for k in got if k not in {l["sku"] for l in po["lines"]}]
        for k in extra:
            diffs.append(f"{k}: not on the order ({num(got[k].get('qty')):g} received)")
        now = datetime.now().replace(microsecond=0).isoformat()
        self.db.table(TASKS).insert({"task_id": "GRN-" + uuid.uuid4().hex[:8].upper(), "kind": "receipt", "ref": po["po"],
                                     "status": "Checked with differences" if diffs else "Checked: all correct",
                                     "lines": lines, "discrepancies": diffs, "notes": str(body.get("note") or "")[:300],
                                     "by": u["username"], "by_name": u.get("full_name"), "created_at": now,
                                     "updated_at": now}).execute()
        return {"ok": True, "differences": diffs,
                "message": "Delivery checked: all correct." if not diffs else
                           f"Delivery checked with {len(diffs)} difference(s): the office has been told."}

    # ---- put away and move ----
    def wh_move(self, body: dict) -> dict:
        u = self._who(body.get("token"), "move")
        products, holdings = self._products(), self._stock()
        what = self._identify(body.get("code"), products, holdings)
        if what["type"] not in ("product", "serial"):
            raise WarehouseError("Scan the product (or its serial number) first.")
        sku = what["sku"]
        qty = 1.0 if what["type"] == "serial" else num(body.get("qty"))
        to_bin = str(body.get("to_bin") or "").strip().upper()
        from_bin = str(body.get("from_bin") or "").strip().upper()
        bins = {str(b.get("bin_code")).upper(): b["bin_code"] for b in self._all("stock_bins") if b.get("active") is not False}
        if to_bin not in bins:
            raise WarehouseError(f"Bin {to_bin or '(none)'} doesn't exist or isn't active.")
        if qty <= 0:
            raise WarehouseError("How many?")
        state = self._bins_state(holdings, products)
        src = from_bin and bins.get(from_bin)
        if from_bin and not src:
            raise WarehouseError(f"Bin {from_bin} doesn't exist.")
        have = sum(c["qty"] for c in state["contents"] if c["sku"] == sku and
                   c["bin"] == (src or inventra_bins.UNALLOCATED))
        if qty > have + 1e-9:
            raise WarehouseError(f"Only {have:g} of {what['description']} are {'in ' + src if src else 'not in a bin yet'}.")
        note = f"Phone · {u.get('full_name') or u['username']}" + (f" · serial {what.get('serial')}" if what.get("serial") else "")
        stamp = datetime.now().isoformat(timespec="seconds")
        moves = [{"sku": sku, "bin_code": bins[to_bin], "qty": qty, "note": note, "created_at": stamp}]
        if src:
            moves.insert(0, {"sku": sku, "bin_code": src, "qty": -qty, "note": note, "created_at": stamp})
        self.db.table("stock_bin_moves").insert(moves).execute()
        return {"ok": True, "message": f"{qty:g} × {what['description']} {'moved from ' + src + ' to' if src else 'put in'} "
                                       f"{bins[to_bin]}."}

    # ---- count a bin ----
    def wh_count(self, body: dict) -> dict:
        u = self._who(body.get("token"), "count")
        products, holdings = self._products(), self._stock()
        bins = {str(b.get("bin_code")).upper(): b["bin_code"] for b in self._all("stock_bins")}
        b = bins.get(str(body.get("bin") or "").strip().upper())
        if not b:
            raise WarehouseError("Scan the bin first.")
        state = self._bins_state(holdings, products)
        books = {c["sku"]: c["qty"] for c in state["contents"] if c["bin"] == b}
        counted = {str(k): num(v) for k, v in (body.get("counts") or {}).items()}
        rows, diffs = [], []
        now = datetime.now().replace(microsecond=0).isoformat()
        for sku in sorted(set(books) | set(counted)):
            sys_q, got = books.get(sku, 0.0), counted.get(sku, 0.0)
            if abs(got - sys_q) < 1e-9:
                continue
            p = products.get(sku) or {}
            diffs.append(f"{p.get('productdescription') or sku}: counted {got:g}, books {sys_q:g}")
            rows.append({"sku": sku, "description": p.get("productdescription") or "", "bin_code": b, "system_qty": sys_q,
                         "counted_qty": got, "qty_change": got - sys_q,
                         "unit_cost": num(p.get("costpriceexclvat")) or num(p.get("unitprice")),
                         "reason": "Bin count on the phone", "notes": str(body.get("note") or "")[:300],
                         "status": "Pending", "counted_by": u["username"], "counted_by_name": u.get("full_name"),
                         "counted_at": now})
        if rows:
            self.db.table("stock_adjustments").insert(rows).execute()
        self.db.table(TASKS).insert({"task_id": "CNT-" + uuid.uuid4().hex[:8].upper(), "kind": "count", "ref": b,
                                     "status": "Differences sent for approval" if rows else "Matches the books",
                                     "lines": [{"sku": k, "counted": v} for k, v in counted.items()],
                                     "discrepancies": diffs, "by": u["username"], "by_name": u.get("full_name"),
                                     "created_at": now, "updated_at": now}).execute()
        return {"ok": True, "differences": diffs,
                "message": f"Bin {b}: matches the books." if not rows else
                           f"Bin {b}: {len(rows)} difference(s) sent to Stock Control for approval."}
