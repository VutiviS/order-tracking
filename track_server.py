"""INVENTRA order tracking site (runs on the internet, for the business's customers).

A customer opens the tracking link the business sent them (…/o/<order>-<code>), or types their order number with the
phone number or e-mail address the business has for them, and sees where the order is: received, being prepared,
dispatched, on the way (including the border for cross-border orders) and delivered. They can confirm they received it
and leave a short rating. Nothing about prices or other customers is shown.

Data: the same Supabase database the INVENTRA desktop apps use (customer_order, despatching, order_tracking,
cross_border_shipments, customers). Staff update deliveries in the desktop app: Dispatch > Order tracking.

Settings (environment variables): SUPABASE_URL, SUPABASE_KEY, COMPANY_NAME, COMPANY_PHONE, COMPANY_EMAIL,
COMPANY_WHATSAPP (optional, e.g. 27821234567), TZ (default Africa/Johannesburg)."""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("TZ", "Africa/Johannesburg")
if hasattr(time, "tzset"):
    time.tzset()

import tracking_domain as TD  # noqa: E402

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


STATIC = {"/manifest.json": "application/manifest+json", "/icon-192.png": "image/png", "/icon-512.png": "image/png",
          "/apple-touch-icon.png": "image/png", "/logo.png": "image/png"}


def company() -> dict:
    return {"name": os.environ.get("COMPANY_NAME") or "INVENTRA", "phone": os.environ.get("COMPANY_PHONE") or "",
            "email": os.environ.get("COMPANY_EMAIL") or "", "whatsapp": os.environ.get("COMPANY_WHATSAPP") or ""}


class Handler(BaseHTTPRequestHandler):
    tracker: Tracker = None
    startup_error = None
    server_version = "INVENTRA-track/1.0"

    @classmethod
    def ensure(cls):
        if cls.tracker is None:
            try:
                cls.tracker = Tracker()
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
        if isinstance(e, TrackError):
            return self._send(e.status, {"error": str(e)})
        log.exception("tracking request failed")
        return self._send(503, {"error": "Tracking is not available right now. Please try again in a minute."})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html") or path.startswith("/o/"):
            return self._page()
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
            body = json.loads(self.rfile.read(min(n, 5_000)) or b"{}")
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
