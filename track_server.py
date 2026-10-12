"""INVENTRA order tracking site (runs on the internet, for the business's customers).

A customer opens the tracking link the business sent them (…/o/<order>-<code>), or types their order number with the
phone number or e-mail address the business has for them, and sees where the order is: received, being prepared,
dispatched, on the way (including the border for cross-border orders) and delivered. They can confirm they received it
and leave a short rating. Nothing about prices or other customers is shown.

Data: the same Supabase database the INVENTRA desktop apps use (customer_order, despatching, order_tracking,
cross_border_shipments, customers). Staff update deliveries in the desktop app: Dispatch > Order tracking.

Drivers open /driver on their phones, sign in with their name and the PIN the office set (Order Tracking > Tracking
website & drivers) and record each trip: loaded, left, arrived, delivered (name, condition, signature, photo) or failed.

Settings (environment variables): SUPABASE_URL, SUPABASE_KEY, COMPANY_NAME, COMPANY_PHONE, COMPANY_EMAIL,
COMPANY_WHATSAPP (optional, e.g. 27821234567), TZ (default Africa/Johannesburg)."""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("TZ", "Africa/Johannesburg")
if hasattr(time, "tzset"):
    time.tzset()

import tracking_domain as TD  # noqa: E402
from warehouse import Warehouse, WarehouseError  # noqa: E402
from supplier import SupplierError, SupplierPortal  # noqa: E402
from quotes_web import QuoteError, QuotePortal  # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE_FILE = HERE / "track_page.html"
CACHE_SECONDS = 20
LOOKUP_LIMIT, LOOKUP_WINDOW = 8, 15 * 60          # wrong order/contact pairs per address before a pause
log = logging.getLogger("inventra.track")


class TrackError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _order_key(order_id: str):
    return int(order_id) if order_id.isdigit() else order_id


class Tracker:
    def __init__(self):
        if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
            raise RuntimeError("SUPABASE_URL and/or SUPABASE_KEY are not set on the server")
        from supabase import create_client
        self.db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        self.secret = os.environ["SUPABASE_KEY"]
        self.cache = {}
        self.failures = {}
        self.lock = threading.Lock()

    def _rows(self, table, column, value, like=False):
        try:
            q = self.db.table(table).select("*")
            q = q.ilike(column, f"%{value}%") if like else q.eq(column, value)
            return q.execute().data or []
        except Exception as e:
            text = str(e)
            if "PGRST205" in text or "does not exist" in text or "404" in text:
                return []                      # optional table not created yet
            raise

    def order(self, order_id: str, fresh=False) -> dict:
        """(order, despatch rows, tracking row, shipments) for one order, cached a few seconds."""
        hit = self.cache.get(order_id)
        if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        key = _order_key(order_id)
        order = TD.group_order(self._rows("customer_order", "order_id", key))
        if not order:
            raise TrackError("We can't find that order. Check the order number on your quote, invoice or message.", 404)
        despatch = self._rows("despatching", "order_id", key)
        track = (self._rows(TD.TABLE, "order_id", order_id) or [None])[0]
        ships = [s for s in self._rows("cross_border_shipments", "reference", order_id, like=True)
                 if order_id in [w.strip() for w in str(s.get("reference") or "").replace(",", " ").split()]]
        data = (order, despatch, track, ships)
        self.cache[order_id] = (time.time(), data)
        return data

    def view(self, order_id: str, fresh=False) -> dict:
        order, despatch, track, ships = self.order(order_id, fresh)
        v = TD.view(order, despatch, track, ships, datetime.now())
        v["key"] = f"{order_id}-{TD.track_code(self.secret, order_id).replace('-', '')}"
        return v

    def from_link(self, key: str) -> dict:
        oid, code = TD.split_link_key(key)
        if not oid or not TD.code_ok(self.secret, oid, code):
            raise TrackError("This tracking link isn't valid. Type your order number and phone number instead.", 404)
        return self.view(oid)

    def _limit(self, ip: str):
        with self.lock:
            recent = [t for t in self.failures.get(ip, []) if time.time() - t < LOOKUP_WINDOW]
            self.failures[ip] = recent
            if len(recent) >= LOOKUP_LIMIT:
                raise TrackError("Too many tries. Please wait 15 minutes, or use the tracking link we sent you.", 429)

    def _failed(self, ip: str):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())

    def lookup(self, body: dict, ip: str) -> dict:
        """Order number + the phone number or e-mail on the customer's record."""
        self._limit(ip)
        oid = TD.clean_order_id(body.get("order"))
        contact = str(body.get("contact") or "").strip()
        if not oid or not contact:
            raise TrackError("Type your order number and your phone number or e-mail address.")
        wrong = TrackError("That order number and phone number / e-mail don't match our records. Check both, or use "
                           "the tracking link we sent you.", 404)
        try:
            order = self.order(oid)[0]
        except TrackError:
            self._failed(ip)
            raise wrong
        customers = self._rows("customers", "company_name", order.get("client_name") or "")
        if not any(TD.contact_matches(c, contact) for c in customers):
            self._failed(ip)
            raise wrong
        return self.view(oid)

    def confirm(self, body: dict, ip: str) -> dict:
        """The customer confirms they received the order (and may rate it)."""
        oid, code = TD.split_link_key(body.get("key"))
        if not oid or not TD.code_ok(self.secret, oid, code):
            raise TrackError("This tracking link isn't valid.", 404)
        self._limit(ip)
        order, despatch, track, ships = self.order(oid, fresh=True)
        if TD.stage_of(order, despatch, track, ships) not in ("dispatched", "in_transit", "delivered"):
            raise TrackError("Your order hasn't been dispatched yet.", 409)
        if (track or {}).get("confirmed_at"):
            return self.view(oid, fresh=True)
        name = str(body.get("name") or "").strip()[:80]
        if len(name) < 2:
            raise TrackError("Please type your name.")
        try:
            rating = int(body.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        now = datetime.now().replace(microsecond=0)
        row = TD.add_event(track, "confirmed", str(body.get("feedback") or "")[:300], by="customer", when=now)
        row.update(order_id=oid, confirmed_at=now.isoformat(), confirmed_by=name,
                   rating=rating if 1 <= rating <= 5 else None, feedback=str(body.get("feedback") or "").strip()[:500])
        if not row.get("delivered_at"):
            row["received_by"] = row.get("received_by") or name
        row.pop("created_at", None)
        try:
            self.db.table(TD.TABLE).upsert(row, on_conflict="order_id").execute()
        except Exception as e:
            log.warning("confirm failed: %s", str(e)[:200])
            raise TrackError("We couldn't save that right now. Please try again later or call us.", 503)
        self.cache.pop(oid, None)
        return self.view(oid, fresh=True)


# =============================================================================
# DRIVERS: sign in with name + PIN on the phone, record trips and deliveries (proof of delivery)
# =============================================================================

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402

DRIVER_DAYS = 14
PROOF_BUCKET = "delivery-proofs"


def _driver_secret() -> bytes:
    return hashlib.sha256(("inventra-driver:" + os.environ.get("SUPABASE_KEY", "")).encode()).digest()


def driver_token(driver: dict) -> str:
    payload = {"d": driver["id"], "x": int(time.time()) + DRIVER_DAYS * 86400,
               "h": hashlib.sha256(str(driver.get("pin_hash") or "").encode()).hexdigest()[:12]}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return raw + "." + hmac.new(_driver_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]


class Drivers:
    """Mixed into Tracker: the driver side of the site."""

    def _driver(self, token: str) -> dict:
        try:
            raw, sig = str(token).split(".", 1)
            if not hmac.compare_digest(sig, hmac.new(_driver_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]):
                raise ValueError
            p = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            if p["x"] < time.time():
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise TrackError("Please sign in again.", 401)
        rows = self.db.table("fleet_drivers").select("*").eq("id", p["d"]).execute().data or []
        if not rows or rows[0].get("active") is False or \
                hashlib.sha256(str(rows[0].get("pin_hash") or "").encode()).hexdigest()[:12] != p["h"]:
            raise TrackError("Please sign in again.", 401)
        return rows[0]

    def driver_login(self, body: dict, ip: str) -> dict:
        self._limit("driver:" + ip)
        name = " ".join(str(body.get("name") or "").split()).lower()
        pin = str(body.get("pin") or "")
        rows = [d for d in self.db.table("fleet_drivers").select("*").execute().data or []
                if " ".join(str(d.get("name") or "").split()).lower() == name and d.get("active") is not False]
        d = next((d for d in rows if d.get("pin_hash") and TD.pin_ok(d["pin_hash"], pin)), None)
        if not d:
            self._failed("driver:" + ip)
            raise TrackError("Name or PIN not right. Ask the office to set your PIN.", 403)
        return {"token": driver_token(d), "name": d.get("name")}

    def driver_trips(self, body: dict) -> dict:
        d = self._driver(body.get("token"))
        recent = (datetime.now().date() - timedelta(days=1)).isoformat()
        trips = [t for t in self.db.table("delivery_trips").select("*").eq("driver", d.get("name")).execute().data or []
                 if t.get("status") not in ("Completed", "Cancelled") or str(t.get("trip_date"))[:10] >= recent
                 or str(t.get("updated_at") or "")[:10] >= recent]
        trips.sort(key=lambda t: (str(t.get("trip_date")), str(t.get("trip_id"))))
        out = []
        for t in trips:
            stops = []
            for oid in t.get("orders") or []:
                oid = str(oid)
                try:
                    order, despatch, track, ships = self.order(oid, fresh=True)
                except TrackError:
                    continue
                cust = (self._rows("customers", "company_name", order.get("client_name") or "") or [{}])[0]
                v = TD.view(order, despatch, track, ships)
                stops.append({"order_id": oid, "client": order.get("client_name"), "address": cust.get("physical_address") or "",
                              "phone": cust.get("phone_number") or "", "stage": v["stage"], "stage_label": v["stage_label"],
                              "done": v["stage"] == "delivered" or bool((track or {}).get("pod")),
                              "failed": bool(v["problem"]) and v["stage"] != "delivered",
                              "items": [{"sku": x.get("sku"), "description": x.get("product_description") or x.get("sku"),
                                         "qty": TD.num(x.get("dispatched_quantity"))} for x in despatch
                                        if (x.get("sku") or "") != TD.DELIVERY_SKU]})
            out.append({"trip_id": t["trip_id"], "date": str(t.get("trip_date"))[:10], "vehicle": t.get("vehicle") or "",
                        "status": t.get("status"), "start_km": t.get("start_km"), "stops": stops})
        return {"name": d.get("name"), "trips": out}

    def _trip(self, d: dict, trip_id: str) -> dict:
        rows = self.db.table("delivery_trips").select("*").eq("trip_id", trip_id).execute().data or []
        if not rows or rows[0].get("driver") != d.get("name"):
            raise TrackError("That trip isn't yours.", 403)
        return rows[0]

    def _save_track(self, oid: str, row: dict):
        row = {k: v for k, v in row.items() if k != "created_at"}
        row["order_id"] = oid
        self.db.table(TD.TABLE).upsert(row, on_conflict="order_id").execute()
        self.cache.pop(oid, None)

    def driver_trip(self, body: dict) -> dict:
        d = self._driver(body.get("token"))
        t = self._trip(d, str(body.get("trip_id") or ""))
        action = body.get("action")
        nxt = {"loaded": ("Planned", "Loaded"), "departed": ("Loaded", "Departed"), "completed": ("Departed", "Completed")}
        if action not in nxt:
            raise TrackError("Unknown step.")
        if t.get("status") not in (nxt[action][0], nxt[action][1]):
            raise TrackError(f"The trip is {str(t.get('status')).lower()}: that step doesn't fit.", 409)
        now = datetime.now().replace(microsecond=0)
        ev = list(t.get("events") or []) + [{"kind": action, "at": now.isoformat(), "by": d.get("name"),
                                             "lat": body.get("lat"), "lng": body.get("lng")}]
        change = {"status": nxt[action][1], "events": ev, "updated_at": now.isoformat()}
        km = TD.num(body.get("km"))
        if km and action == "loaded":
            change["start_km"] = km
        if km and action == "completed":
            change["end_km"] = km
        self.db.table("delivery_trips").update(change).eq("trip_id", t["trip_id"]).execute()
        if action in ("loaded", "departed"):
            note = (t.get("vehicle") or "") if action == "loaded" else f"Driver {d.get('name')}"
            for oid in t.get("orders") or []:
                track = (self._rows(TD.TABLE, "order_id", str(oid)) or [None])[0]
                row = TD.add_event(track, action, note, by=d.get("name"), when=now)
                row["trip_id"] = t["trip_id"]
                self._save_track(str(oid), row)
        return self.driver_trips(body)

    def _photo(self, oid: str, data_url: str) -> str:
        if not data_url:
            return None
        head, _, b64 = str(data_url).partition(",")
        raw = base64.b64decode(b64 or "")
        if len(raw) > 6 * 1024 * 1024:
            raise TrackError("The photo is too big.")
        key = f"{oid}/{datetime.now():%Y%m%d%H%M%S}.jpg"
        try:
            self.db.storage.from_(PROOF_BUCKET).upload(key, raw, {"content-type": "image/jpeg"})
        except Exception as e:
            log.warning("photo upload failed: %s", str(e)[:200])
            return None                      # the delivery is still recorded
        return key

    def driver_stop(self, body: dict) -> dict:
        d = self._driver(body.get("token"))
        t = self._trip(d, str(body.get("trip_id") or ""))
        oid = TD.clean_order_id(body.get("order_id"))
        if oid not in [str(x) for x in t.get("orders") or []]:
            raise TrackError("That order isn't on this trip.", 403)
        action = body.get("action")
        now = datetime.now().replace(microsecond=0)
        track = (self._rows(TD.TABLE, "order_id", oid) or [None])[0]
        note = str(body.get("note") or "").strip()[:300]
        where = {"lat": body.get("lat"), "lng": body.get("lng")}
        if action == "arrived":
            row = TD.add_event(track, "arrived", "", by=d.get("name"), when=now)
        elif action == "failed":
            if not note:
                raise TrackError("Say why it couldn't be delivered (the customer sees it).")
            row = TD.add_event(track, "failed", note, by=d.get("name"), when=now)
        elif action == "delivered":
            name = str(body.get("received_by") or "").strip()[:80]
            cond = body.get("condition") if body.get("condition") in TD.POD_CONDITIONS else "Good"
            if len(name) < 2:
                raise TrackError("Type the name of the person who received it.")
            issues = [{"sku": i.get("sku"), "description": str(i.get("description") or "")[:120],
                       "qty": TD.num(i.get("qty")), "kind": cond.lower()}
                      for i in body.get("issues") or [] if TD.num(i.get("qty")) > 0][:30]
            if cond in ("Short", "Damaged") and not issues:
                raise TrackError("Enter how many were short or damaged.")
            if cond != "Good" and not note:
                raise TrackError("Write a short note about the problem.")
            sig = str(body.get("signature") or "")
            if sig and (not sig.startswith("data:image/png;base64,") or len(sig) > 200_000):
                sig = ""
            pod = {"received_by": name, "condition": cond, "issues": issues, "note": note, "signature": sig,
                   "photo_path": self._photo(oid, body.get("photo")), "at": now.isoformat(), "by": d.get("name"),
                   "trip_id": t["trip_id"], **where}
            if cond == "Rejected":
                row = TD.add_event(track, "failed", f"Rejected by the customer: {note}", by=d.get("name"), when=now)
            else:
                row = TD.add_event(track, "delivered", note, by=d.get("name"), when=now, received_by=name)
            row["pod"] = pod
        else:
            raise TrackError("Unknown step.")
        row["trip_id"] = t["trip_id"]
        self._save_track(oid, row)
        return self.driver_trips(body)


STATIC = {"/manifest.json": "application/manifest+json", "/icon-192.png": "image/png", "/icon-512.png": "image/png",
          "/apple-touch-icon.png": "image/png", "/logo.png": "image/png"}


def company() -> dict:
    return {"name": os.environ.get("COMPANY_NAME") or "INVENTRA", "phone": os.environ.get("COMPANY_PHONE") or "",
            "email": os.environ.get("COMPANY_EMAIL") or "", "whatsapp": os.environ.get("COMPANY_WHATSAPP") or ""}


class FullTracker(QuotePortal, SupplierPortal, Warehouse, Drivers, Tracker):
    pass


class Handler(BaseHTTPRequestHandler):
    tracker: Tracker = None
    startup_error = None
    server_version = "INVENTRA-track/1.0"

    @classmethod
    def ensure(cls):
        if cls.tracker is None:
            try:
                cls.tracker = FullTracker()
                cls.startup_error = None
            except Exception as e:
                cls.startup_error = f"{type(e).__name__}: {e}"[:200]
                log.error("tracking site is not set up correctly: %s", cls.startup_error)
        return cls.tracker

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html"))
                         else "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _page(self):
        c = company()
        page = PAGE_FILE.read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"]).replace(
            "{{COMPANY_JSON}}", json.dumps(c).replace("</", "<\\/"))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _fail(self, e):
        if isinstance(e, (TrackError, WarehouseError, SupplierError, QuoteError)):
            return self._send(e.status, {"error": str(e)})
        log.exception("tracking request failed")
        return self._send(503, {"error": "Tracking is not available right now. Please try again in a minute."})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html") or path.startswith("/o/"):
            return self._page()
        if path.startswith("/q/"):
            c = company()
            page = (HERE / "quote_page.html").read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"]).replace(
                "{{COMPANY_JSON}}", json.dumps(c).replace("</", "<\\/"))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/quote":
            key = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("k", "")
            if not self.ensure():
                return self._send(503, {"error": "Not available right now."})
            try:
                return self._send(200, self.tracker.quote_view(key, self._ip()))
            except Exception as e:
                return self._fail(e)
        if path.startswith("/s/"):
            c = company()
            page = (HERE / "supplier_page.html").read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"])
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/supplier":
            key = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("k", "")
            if not self.ensure():
                return self._send(503, {"error": "Not available right now."})
            try:
                return self._send(200, self.tracker.portal(key, self._ip()))
            except Exception as e:
                return self._fail(e)
        if path in ("/warehouse", "/warehouse/"):
            c = company()
            page = (HERE / "warehouse_page.html").read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"])
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/driver", "/driver/"):
            c = company()
            page = (HERE / "driver_page.html").read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"])
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path in STATIC:
            f = HERE / path.lstrip("/")
            return self._send(200, f.read_bytes(), STATIC[path]) if f.is_file() else self._send(404, {"error": "Not found"})
        if path == "/api/track":
            key = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("k", "")
            if not self.ensure():
                return self._send(503, {"error": "Tracking is being set up. Please try again later."})
            try:
                return self._send(200, self.tracker.from_link(key))
            except Exception as e:
                return self._fail(e)
        if path == "/api/health":
            t = self.ensure()
            if not t:
                return self._send(503, {"ok": False, "stage": "setup", "error": self.startup_error})
            try:
                t.db.table("customer_order").select("order_id").limit(1).execute()
                try:
                    t.db.table(TD.TABLE).select("order_id").limit(1).execute()
                    tracking = True
                except Exception:
                    tracking = False        # "Fix database access" not run yet: dispatch dates still show
                return self._send(200, {"ok": True, "db": "ok", "tracking_table": tracking,
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "version": self.server_version})
            except Exception as e:
                return self._send(503, {"ok": False, "stage": "database", "error": str(e)[:200]})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 8_000_000)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "Bad request"})
        if not self.ensure():
            return self._send(503, {"error": "Tracking is being set up. Please try again later."})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/lookup":
                return self._send(200, self.tracker.lookup(body, self._ip()))
            if path == "/api/quote/accept":
                return self._send(200, self.tracker.quote_accept(body, self._ip()))
            if path == "/api/quote/decline":
                return self._send(200, self.tracker.quote_decline(body, self._ip()))
            if path == "/api/supplier/po":
                return self._send(200, self.tracker.portal_po(body, self._ip()))
            if path == "/api/supplier/doc":
                return self._send(200, self.tracker.portal_doc(body, self._ip()))
            if path == "/api/supplier/quote":
                return self._send(200, self.tracker.portal_quote(body, self._ip()))
            if path.startswith("/api/wh/"):
                act = path[len("/api/wh/"):]
                t = self.tracker
                fn = {"login": lambda: t.wh_login(body, self._ip()), "lookup": lambda: t.wh_lookup(body),
                      "bins": lambda: t.wh_bins(body), "orders": lambda: t.wh_orders(body), "pick": lambda: t.wh_pick(body),
                      "po": lambda: t.wh_po(body), "receive": lambda: t.wh_receive(body), "move": lambda: t.wh_move(body),
                      "count": lambda: t.wh_count(body)}.get(act)
                if not fn:
                    return self._send(404, {"error": "Not found"})
                return self._send(200, fn())
            if path == "/api/driver/login":
                return self._send(200, self.tracker.driver_login(body, self._ip()))
            if path == "/api/driver/trips":
                return self._send(200, self.tracker.driver_trips(body))
            if path == "/api/driver/trip":
                return self._send(200, self.tracker.driver_trip(body))
            if path == "/api/driver/stop":
                return self._send(200, self.tracker.driver_stop(body))
            if path == "/api/confirm":
                return self._send(200, dict(self.tracker.confirm(body, self._ip()),
                                            message="Thank you! We've noted that you received your order."))
            return self._send(404, {"error": "Not found"})
        except Exception as e:
            return self._fail(e)


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT") or 8770)
    Handler.ensure()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("tracking site listening on port %s", port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
