"""INVENTRA order tracking site (runs on the internet, for the business's customers).

A customer opens the tracking link the business sent them (…/o/<order>-<code>), or types their order number with the
order number, and sees where the order is: received, being prepared,
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

# The same database connection the INVENTRA desktop apps use (their built-in public key), so the site works even when
# SUPABASE_URL / SUPABASE_KEY haven't been set on the host. Settings on the host still win when they are there.
DEFAULT_SUPABASE_URL = "https://lnimjatrcvpxnumkxqzs.supabase.co"
DEFAULT_SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxuaW1qYXRyY3ZweG51bWt4cXpzIiw"
    "icm9sZSI6ImFub24iLCJpYXQiOjE3NjIzNzY2NDYsImV4cCI6MjA3Nzk1MjY0Nn0.P-KgfRbbR1WDXQ31lViUYlHA0NpdCLd9YgUcV2DbmLA"
)
for _name, _value in (("SUPABASE_URL", DEFAULT_SUPABASE_URL), ("SUPABASE_KEY", DEFAULT_SUPABASE_KEY)):
    if not (os.environ.get(_name) or "").strip():
        os.environ[_name] = _value
    else:
        os.environ[_name] = os.environ[_name].strip().strip('"').strip("'")
if hasattr(time, "tzset"):
    time.tzset()

# The private access key (INVENTRA_ACCESS_KEY, set on the host, never in this code): sent with every database request
# (the locked database answers nothing without it) and used to sign links and sign-ins. Without it the public
# database key is used, as before.
ACCESS_KEY = (os.environ.get("INVENTRA_ACCESS_KEY") or "").strip()


# what browsers may do with these pages: only our own scripts plus the pinned map / QR / scanner libraries, no
# framing by other sites, location and camera only for this site, always HTTPS
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data: blob: https:; connect-src 'self'; media-src 'self' blob:; worker-src 'self' blob:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
                    ("Permissions-Policy", "geolocation=(self), camera=(self), microphone=(), payment=(), usb=()"),
                    ("Cross-Origin-Opener-Policy", "same-origin")]


def signing_key() -> str:
    return ACCESS_KEY or os.environ.get("SUPABASE_KEY", "")


def db_options():
    from supabase import ClientOptions
    return ClientOptions(headers={"x-inventra-key": ACCESS_KEY} if ACCESS_KEY else {})


import tracking_domain as TD  # noqa: E402
from warehouse import Warehouse, WarehouseError  # noqa: E402
from supplier import SupplierError, SupplierPortal  # noqa: E402
from quotes_web import QuoteError, QuotePortal  # noqa: E402
from gps import GpsError, GpsFeed  # noqa: E402
from delivery_proof import ProofError, ProofMixin, arrived_at, receiver_can_sign, signature_ok  # noqa: E402

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
        self.db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"], options=db_options())
        self.secret = signing_key()
        # links printed before the access key existed: still open the tracking page (read only)
        self.legacy_secret = os.environ["SUPABASE_KEY"] if ACCESS_KEY else None
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
        trip = None
        try:
            trips = self.db.table("delivery_trips").select("*").execute().data or []
        except Exception:
            trips = []
        tid = (track or {}).get("trip_id")
        mine = [t for t in trips if (tid and t.get("trip_id") == tid) or order_id in [str(o) for o in t.get("orders") or []]]
        if mine:
            rank = {"Departed": 3, "Loaded": 2, "Planned": 1}     # the trip really carrying it wins over an old plan
            trip = max(mine, key=lambda t: (t.get("status") not in ("Completed", "Cancelled"), rank.get(t.get("status"), 0),
                                            str(t.get("trip_date") or "")))
            done = []
            for o in trip.get("orders") or []:
                if str(o) == order_id:
                    continue
                tr = (self._rows(TD.TABLE, "order_id", str(o)) or [None])[0] or {}
                if tr.get("delivered_at") or (tr.get("events") or [{}])[-1].get("kind") in ("failed", "delivered"):
                    done.append(str(o))
            trip = dict(trip, _done=done)
        data = (order, despatch, track, ships, trip)
        self.cache[order_id] = (time.time(), data)
        return data

    def view(self, order_id: str, fresh=False) -> dict:
        order, despatch, track, ships, trip = self.order(order_id, fresh)
        now = datetime.now()
        v = TD.view(order, despatch, track, ships, now, trip=trip)
        v["key"] = f"{order_id}-{TD.track_code(self.secret, order_id).replace('-', '')}"
        v["can_sign"] = receiver_can_sign(track, v.get("stage"), now)     # the driver is at the door
        return v

    def from_link(self, key: str) -> dict:
        oid, code = TD.split_link_key(key)
        old = getattr(self, "legacy_secret", None)
        legacy = bool(old) and bool(oid) and TD.code_ok(old, oid, code)
        if not oid or not (TD.code_ok(self.secret, oid, code) or legacy):
            raise TrackError("This tracking link isn't valid. Type your order number instead.", 404)
        return self.view(oid)           # an old link opens the page; it carries the new link for signing

    def _limit(self, ip: str):
        with self.lock:
            recent = [t for t in self.failures.get(ip, []) if time.time() - t < LOOKUP_WINDOW]
            self.failures[ip] = recent
            if len(recent) >= LOOKUP_LIMIT:
                raise TrackError("Too many tries. Please wait 15 minutes, or use the tracking link we sent you.", 429)

    def _failed(self, ip: str):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())
            if len(self.failures) > 5000:              # forget old entries so the list can't be flooded
                cut = time.time() - LOOKUP_WINDOW
                for k in [k for k, v in self.failures.items() if not v or v[-1] < cut]:
                    self.failures.pop(k, None)

    def lookup(self, body: dict, ip: str) -> dict:
        """The order number is enough (wrong numbers count towards a pause, so numbers can't be guessed in bulk)."""
        self._limit(ip)
        oid = TD.clean_order_id(body.get("order"))
        if not oid:
            raise TrackError("Type your order number.")
        try:
            self.order(oid)
        except TrackError:
            self._failed(ip)
            raise TrackError("We can't find that order number. Check it on your quote, invoice or message.", 404)
        return self.view(oid)

    def confirm(self, body: dict, ip: str) -> dict:
        """The customer confirms they received the order (and may rate it)."""
        oid, code = TD.split_link_key(body.get("key"))
        if not oid or not TD.code_ok(self.secret, oid, code):
            raise TrackError("This tracking link isn't valid.", 404)
        self._limit(ip)
        order, despatch, track, ships, _trip = self.order(oid, fresh=True)
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
    return hashlib.sha256(("inventra-driver:" + signing_key()).encode()).digest()


def driver_token(driver: dict) -> str:
    payload = {"d": driver["id"], "x": int(time.time()) + DRIVER_DAYS * 86400,
               "h": hashlib.sha256(str(driver.get("pin_hash") or "").encode()).hexdigest()[:12]}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return raw + "." + hmac.new(_driver_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]


def tapped_at(body: dict, now: datetime) -> datetime:
    """When the driver tapped the button. A step saved on the phone while it had no signal is sent later with how long
    ago it was tapped ("ago", seconds; the phone's own clock may be wrong, so its time isn't used); up to 24 hours."""
    try:
        ago = float(body.get("ago") or 0)
    except (TypeError, ValueError):
        ago = 0
    return (now - timedelta(seconds=ago)).replace(microsecond=0) if 0 < ago <= 86400 else now


def op_of(body: dict) -> str:
    """The phone's id for one tap: the same step sent twice (no answer the first time) is saved once."""
    return str(body.get("op") or "")[:40]


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
        self._limit("driver-account:" + name)          # guesses spread over many addresses still stop
        pin = str(body.get("pin") or "")
        rows = [d for d in self.db.table("fleet_drivers").select("*").execute().data or []
                if " ".join(str(d.get("name") or "").split()).lower() == name and d.get("active") is not False]
        d = next((d for d in rows if d.get("pin_hash") and TD.pin_ok(d["pin_hash"], pin)), None)
        if not d:
            self._failed("driver:" + ip)
            self._failed("driver-account:" + name)
            self.activity(name or "?", "Driver sign-in failed", f"Name typed: {body.get('name') or ''}",
                          kind="sign_in_failed")
            raise TrackError("Name or PIN not right. Ask the office to set your PIN.", 403)
        self.activity(d.get("name") or name, "Driver signed in on the phone", kind="sign_in")
        return {"token": driver_token(d), "name": d.get("name")}

    def _rows_in(self, table, column, values) -> list:
        """Every row whose column is one of values, in as few requests as possible (missing table: none)."""
        out = []
        values = list(values)
        try:
            for i in range(0, len(values), 150):
                out += self.db.table(table).select("*").in_(column, values[i:i + 150]).execute().data or []
        except Exception as e:
            text = str(e)
            if "PGRST205" in text or "does not exist" in text or "404" in text:
                return []
            raise
        return out

    def _orders_for_driver(self, trips: list) -> dict:
        """order id -> (order, despatch, track, shipments, customer) for every order on the trips: 5 requests in all,
        however many stops (one order at a time took 10 requests a stop, 40 seconds for a day's trips)."""
        oids = sorted({str(o) for t in trips for o in t.get("orders") or [] if str(o).isdigit()})
        if not oids:
            return {}
        keys = [int(o) for o in oids]
        lines, desp = {}, {}
        for r in self._rows_in("customer_order", "order_id", keys):
            lines.setdefault(str(r.get("order_id")), []).append(r)
        for r in self._rows_in("despatching", "order_id", keys):
            desp.setdefault(str(r.get("order_id")), []).append(r)
        tracks = {str(r.get("order_id")): r for r in self._rows_in(TD.TABLE, "order_id", oids)}
        try:
            ships_all = self.db.table("cross_border_shipments").select("*").execute().data or []
        except Exception:
            ships_all = []
        try:
            custs = self.db.table("customers").select("company_name,physical_address,phone_number").execute().data or []
        except Exception:
            custs = []
        by_name = {}
        for c in custs:
            by_name.setdefault(str(c.get("company_name") or "").strip(), c)
            by_name.setdefault(str(c.get("company_name") or "").strip().lower(), c)
        out = {}
        for oid in oids:
            order = TD.group_order(lines.get(oid, []))
            if not order:
                continue
            ships = [s for s in ships_all
                     if oid in [w.strip() for w in str(s.get("reference") or "").replace(",", " ").split()]]
            name = str(order.get("client_name") or "").strip()
            out[oid] = (order, desp.get(oid, []), tracks.get(oid), ships, by_name.get(name) or by_name.get(name.lower()) or {})
        return out

    def driver_trips(self, body: dict, d: dict = None) -> dict:
        d = d or self._driver(body.get("token"))
        recent = (datetime.now().date() - timedelta(days=1)).isoformat()
        trips = [t for t in self.db.table("delivery_trips").select("*").eq("driver", d.get("name")).execute().data or []
                 if t.get("status") not in ("Completed", "Cancelled") or str(t.get("trip_date"))[:10] >= recent
                 or str(t.get("updated_at") or "")[:10] >= recent]
        trips.sort(key=lambda t: (str(t.get("trip_date")), str(t.get("trip_id"))))
        orders = self._orders_for_driver(trips)
        out = []
        for t in trips:
            stops = []
            plan_rows = {str(x.get("order_id")): x for x in t.get("plan") or []}
            for oid in t.get("orders") or []:
                oid = str(oid)
                p = plan_rows.get(oid) or {}
                if not oid.isdigit():          # a collection of returned goods or a stop added by hand
                    collect = p.get("kind") == "Collection"
                    stops.append({"order_id": oid, "client": p.get("client") or oid, "address": p.get("address") or "",
                                  "phone": p.get("phone") or "", "kind": p.get("kind") or "Stop",
                                  "stage": "done" if p.get("done") else ("failed" if p.get("failed") else "planned"),
                                  "stage_label": ("Collected" if collect else "Done") if p.get("done") else
                                  ("Not done" if p.get("failed") else ("Collect" if collect else "Stop")),
                                  "done": bool(p.get("done")), "failed": bool(p.get("failed")), "items": [],
                                  "lat": p.get("lat"), "lng": p.get("lng")})
                    continue
                if oid not in orders:
                    continue
                order, despatch, track, ships, cust = orders[oid]
                v = TD.view(order, despatch, track, ships)
                stops.append({"order_id": oid, "client": order.get("client_name"), "address": cust.get("physical_address") or "",
                              "phone": cust.get("phone_number") or "", "stage": v["stage"], "stage_label": v["stage_label"],
                              "done": v["stage"] == "delivered" or bool((track or {}).get("pod")),
                              "failed": bool(v["problem"]) and v["stage"] != "delivered",
                              "items": [{"sku": x.get("sku"), "description": x.get("product_description") or x.get("sku"),
                                         "qty": TD.num(x.get("dispatched_quantity"))} for x in despatch
                                        if (x.get("sku") or "") != TD.DELIVERY_SKU],
                              "kind": "Delivery", "lat": p.get("lat"), "lng": p.get("lng"),
                              "link": f"{oid}-{TD.track_code(self.secret, oid).replace('-', '')}",
                              "arrived": bool(arrived_at(track))})
                if stops[-1]["lat"] is None and order.get("delivery_lat") is not None:
                    stops[-1].update(lat=order.get("delivery_lat"), lng=order.get("delivery_lng"))
                if not stops[-1]["address"] and p.get("address"):
                    stops[-1]["address"] = p["address"]
            plan = {str(x.get("order_id")): x.get("eta") for x in t.get("plan") or []}
            for st in stops:
                st["eta"] = plan.get(st["order_id"], "")
            left = next((e.get("at") for e in reversed(t.get("events") or []) if e.get("kind") == "departed"), None)
            out.append({"trip_id": t["trip_id"], "date": str(t.get("trip_date"))[:10], "vehicle": t.get("vehicle") or "",
                        "status": t.get("status"), "start_km": t.get("start_km"), "left_at": left, "stops": stops})
        return {"name": d.get("name"), "trips": out, "today": datetime.now().date().isoformat(),
                "server_time": datetime.now().replace(microsecond=0).isoformat()}

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
        op = op_of(body)
        if op and any(e.get("op") == op for e in t.get("events") or []):
            return self.driver_trips(body, d)                      # already saved (sent again after no answer)
        if t.get("status") not in (nxt[action][0], nxt[action][1]):
            raise TrackError(f"The trip is {str(t.get('status')).lower()}: that step doesn't fit.", 409)
        now = tapped_at(body, datetime.now().replace(microsecond=0))
        ev = list(t.get("events") or []) + [{"kind": action, "at": now.isoformat(), "by": d.get("name"),
                                             "lat": body.get("lat"), "lng": body.get("lng"), **({"op": op} if op else {})}]
        change = {"status": nxt[action][1], "events": ev, "updated_at": now.isoformat()}
        km = TD.num(body.get("km"))
        if km and action == "loaded":
            change["start_km"] = km
        if km and action == "completed":
            change["end_km"] = km
        self.db.table("delivery_trips").update(change).eq("trip_id", t["trip_id"]).execute()
        self.activity(d.get("name") or "driver", {"loaded": "Trip loaded", "departed": "Left on the trip",
                                                  "completed": "Trip done, back at base"}[action] + f": {t['trip_id']}",
                      f"Odometer {km:g} km" if km else "", {"lat": body.get("lat"), "lng": body.get("lng")})
        if action in ("loaded", "departed"):
            note = (t.get("vehicle") or "") if action == "loaded" else f"Driver {d.get('name')}"
            for oid in t.get("orders") or []:
                if not str(oid).isdigit():
                    continue
                track = (self._rows(TD.TABLE, "order_id", str(oid)) or [None])[0]
                row = TD.add_event(track, action, note, by=d.get("name"), when=now)
                row["trip_id"] = t["trip_id"]
                self._save_track(str(oid), row)
        return self.driver_trips(body, d)

    def driver_position(self, body: dict) -> dict:
        """The driver's phone reports where the vehicle is while the trip is out (Departed)."""
        d = self._driver(body.get("token"))
        t = self._trip(d, str(body.get("trip_id") or ""))
        if t.get("status") != "Departed":
            return {"ok": True, "sharing": False}
        try:
            lat, lng = float(body.get("lat")), float(body.get("lng"))
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except (TypeError, ValueError):
            raise TrackError("No location.")
        now = datetime.now().replace(microsecond=0).isoformat()
        pos = {"lat": round(lat, 6), "lng": round(lng, 6), "acc": body.get("accuracy"), "speed": body.get("speed"),
               "at": now}
        trail = (list(t.get("positions") or []) + [pos])[-300:]
        self.db.table("delivery_trips").update({"last_position": pos, "positions": trail}).eq("trip_id", t["trip_id"]).execute()
        try:                                   # the fleet record and the history get it too
            v = self._fleet_by("registration", str(t.get("vehicle") or "").split("·")[0].strip())
            if v:
                spd = body.get("speed")
                self.record_position(v, lat, lng, round(float(spd) * 3.6, 1) if spd not in (None, "") else None,
                                     body.get("heading"), body.get("accuracy"), now, "driver phone", t["trip_id"])
        except Exception:
            pass
        return {"ok": True, "sharing": True, "at": now}

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
        if action not in ("arrived", "failed", "delivered"):
            raise TrackError("Unknown step.")
        now = tapped_at(body, datetime.now().replace(microsecond=0))
        op = op_of(body)
        note = str(body.get("note") or "").strip()[:300]
        if action == "failed" and not note:
            raise TrackError("Say why it couldn't be done (the customer sees it).")
        if not oid.isdigit():
            p = next((x for x in t.get("plan") or [] if str(x.get("order_id")) == oid), {})
            if op and op in (p.get("ops") or []):
                return self.driver_trips(body, d)
            return self._other_stop(d, t, oid, action, note, now, self.located(body, t, oid), op)
        track = (self._rows(TD.TABLE, "order_id", oid) or [None])[0]
        if op and any(e.get("op") == op for e in (track or {}).get("events") or []):
            return self.driver_trips(body, d)                      # already saved (sent again after no answer)
        loc = self.located(body, t, oid)               # the phone's GPS, read automatically: required for every step
        where = {"lat": loc.get("lat"), "lng": loc.get("lng")}
        if action == "arrived":
            row = TD.add_event(track, "arrived", "", by=d.get("name"), when=now)
            title = f"Arrived at order {oid}"
        elif action == "failed":
            row = TD.add_event(track, "failed", note, by=d.get("name"), when=now)
            title = f"Delivery failed for order {oid}: {note}"
        else:
            name = " ".join(str(body.get("received_by") or "").split())[:80]
            cond = body.get("condition") if body.get("condition") in TD.POD_CONDITIONS else "Good"
            if len(name) < 2:
                raise TrackError("Type the full name of the person receiving the goods.")
            issues = [{"sku": i.get("sku"), "description": str(i.get("description") or "")[:120],
                       "qty": TD.num(i.get("qty")), "kind": cond.lower()}
                      for i in body.get("issues") or [] if TD.num(i.get("qty")) > 0][:30]
            if cond in ("Short", "Damaged") and not issues:
                raise TrackError("Enter how many were short or damaged.")
            if cond != "Good" and not note:
                raise TrackError("Write a short note about the problem.")
            sig = str(body.get("signature") or "")
            if not signature_ok(sig):
                if cond != "Rejected":
                    raise TrackError("The receiver must sign on the screen before the delivery can be saved.")
                sig = ""
            pod = {"received_by": name, "condition": cond, "issues": issues, "note": note, "signature": sig,
                   "photo_path": self._photo(oid, body.get("photo")), "at": now.isoformat(), "by": d.get("name"),
                   "signed_on": "driver's phone", "location": loc, "trip_id": t["trip_id"], **where}
            if cond == "Rejected":
                row = TD.add_event(track, "failed", f"Rejected by the customer: {note}", by=d.get("name"), when=now)
                title = f"Order {oid} refused by {name}"
            else:
                row = TD.add_event(track, "delivered", note, by=d.get("name"), when=now, received_by=name)
                title = f"Delivered order {oid}, signed by {name}"
            row["pod"] = pod
        row["events"][-1].update(lat=loc.get("lat"), lng=loc.get("lng"), loc=loc.get("status"),
                                 distance_m=loc.get("distance_m"), **({"op": op} if op else {}))
        row["trip_id"] = t["trip_id"]
        self._save_track(oid, row)
        self.activity(d.get("name") or "driver", title, TD.location_text(loc), loc)
        return self.driver_trips(body, d)

    def _other_stop(self, d: dict, t: dict, oid: str, action: str, note: str, now: datetime, loc=None,
                    op: str = "") -> dict:
        """A collection (returned goods) or a stop added by hand: its progress is kept on the trip's plan; a collected
        return moves on to Collected in Returns."""
        if action not in ("arrived", "failed", "delivered"):
            raise TrackError("Unknown step.")
        if action == "failed" and not note:
            raise TrackError("Say why it couldn't be done.")
        plan = [dict(x) for x in t.get("plan") or []]
        p = next((x for x in plan if str(x.get("order_id")) == oid), None)
        if p is None:
            p = {"order_id": oid}
            plan.append(p)
        p.update({"arrived": now.isoformat()} if action == "arrived" else
                 {"failed": action == "failed", "done": action == "delivered", "at": now.isoformat(), "note": note,
                  "by": d.get("name")})
        if op:
            p["ops"] = (list(p.get("ops") or []) + [op])[-10:]
        if loc:
            p["location" if action != "arrived" else "arrived_location"] = loc
            verb = {"arrived": "Arrived at", "failed": "Could not do", "delivered": "Done:"}[action]
            self.activity(d.get("name") or "driver", f"{verb} {p.get('client') or oid}", TD.location_text(loc), loc)
        self.db.table("delivery_trips").update({"plan": plan, "updated_at": now.isoformat()}).eq(
            "trip_id", t["trip_id"]).execute()
        if action == "delivered" and p.get("kind") == "Collection":
            try:
                r = (self.db.table("return_requests").select("*").eq("rma_id", oid).execute().data or [None])[0]
                if r and r.get("status") in ("Approved", "Collection booked"):
                    hist = list(r.get("history") or []) + [{"at": now.isoformat(), "by": d.get("name"),
                                                            "action": "Collected", "note": note or f"Trip {t['trip_id']}"}]
                    self.db.table("return_requests").update({"status": "Collected", "history": hist,
                                                             "updated_at": now.isoformat()}).eq("rma_id", oid).execute()
            except Exception as e:
                log.warning("return %s not updated: %s", oid, str(e)[:200])
        return self.driver_trips({}, d)


STATIC = {"/manifest.json": "application/manifest+json", "/icon-192.png": "image/png", "/icon-512.png": "image/png",
          "/apple-touch-icon.png": "image/png", "/logo.png": "image/png", "/logo.svg": "image/svg+xml",
          "/logo-light.svg": "image/svg+xml", "/logo-full.svg": "image/svg+xml", "/logo-full-light.svg": "image/svg+xml",
          "/favicon.svg": "image/svg+xml", "/driver_sw.js": "text/javascript; charset=utf-8"}


def company() -> dict:
    return {"name": os.environ.get("COMPANY_NAME") or "INVENTRA", "phone": os.environ.get("COMPANY_PHONE") or "",
            "email": os.environ.get("COMPANY_EMAIL") or "", "whatsapp": os.environ.get("COMPANY_WHATSAPP") or ""}


class FullTracker(ProofMixin, GpsFeed, QuotePortal, SupplierPortal, Warehouse, Drivers, Tracker):
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
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html", "text/javascript"))
                         else "public, max-age=86400")
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)

    def _page(self):
        c = company()
        page = PAGE_FILE.read_text(encoding="utf-8").replace("{{COMPANY}}", c["name"]).replace(
            "{{COMPANY_JSON}}", json.dumps(c).replace("</", "<\\/"))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _ip(self):
        """The visitor's real address for the try limits. The first X-Forwarded-For entry is whatever the visitor
        typed (so it can't be trusted); the host's own proxy puts the real one in CF-Connecting-IP / True-Client-IP
        or adds it as the LAST entry."""
        for h in ("CF-Connecting-IP", "True-Client-IP"):
            if (self.headers.get(h) or "").strip():
                return self.headers[h].strip()
        xff = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        return xff[-1] if xff else self.client_address[0]

    def _fail(self, e):
        if isinstance(e, (TrackError, WarehouseError, SupplierError, QuoteError, GpsError, ProofError)):
            return self._send(e.status, {"error": str(e)})
        log.exception("tracking request failed")
        return self._send(503, {"error": "Tracking is not available right now. Please try again in a minute."})

    def _gps(self, params):
        if not self.ensure():
            return self._send(503, {"error": "Not ready"})
        ip = self._ip()
        try:
            self.tracker._limit("gps:" + ip)                 # unknown tracker ids can't be guessed in bulk
            return self._send(200, self.tracker.gps(params))
        except Exception as e:
            if getattr(e, "status", 0) == 404:
                self.tracker._failed("gps:" + ip)
            return self._fail(e)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/api/gps", "/api/gps/"):
            from urllib.parse import parse_qsl
            return self._gps(dict(parse_qsl(query)))
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
                try:
                    import web_files
                    files = web_files.fingerprints(HERE)
                except Exception:
                    files = None
                try:                        # does the (locked) database accept this site? (Settings > Security)
                    access_ok = bool(t.db.rpc("inventra_access").execute().data)
                except Exception:
                    access_ok = None        # the security update hasn't been run yet
                return self._send(200, {"ok": True, "db": "ok", "tracking_table": tracking,
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "version": self.server_version, "files": files,
                                        "access_key": bool(ACCESS_KEY), "access_ok": access_ok})
            except Exception as e:
                return self._send(503, {"ok": False, "stage": "database", "error": str(e)[:200]})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] in ("/api/gps", "/api/gps/"):          # GPS devices and the Traccar Client app
            from urllib.parse import parse_qsl
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, 20_000)).decode("utf-8", "replace") if n else ""
            params = dict(parse_qsl(self.path.partition("?")[2]))
            try:
                params.update(json.loads(raw) if raw.strip().startswith("{") else dict(parse_qsl(raw)))
            except ValueError:
                pass
            if isinstance(params.get("location"), dict):          # newer Traccar Client sends JSON like this
                loc = params["location"]
                c = loc.get("coords") or {}
                params.update(id=params.get("device_id") or params.get("id"), lat=c.get("latitude"),
                              lon=c.get("longitude"), speed=(c.get("speed") or 0) / 0.514444 if c.get("speed") else None,
                              bearing=c.get("heading"), accuracy=c.get("accuracy"), timestamp=loc.get("timestamp"))
            return self._gps(params)
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
                res = fn()
                if act in ("pick", "receive", "move", "count"):          # the user activity log
                    try:
                        u = t._who(body.get("token"))
                        t.activity(u.get("full_name") or u.get("username") or "?", str(res.get("message") or act),
                                   app="Warehouse phone", kind="phone")
                    except Exception:
                        pass
                return self._send(200, res)
            if path == "/api/driver/login":
                return self._send(200, self.tracker.driver_login(body, self._ip()))
            if path == "/api/driver/trips":
                return self._send(200, self.tracker.driver_trips(body))
            if path == "/api/driver/trip":
                return self._send(200, self.tracker.driver_trip(body))
            if path == "/api/driver/position":
                return self._send(200, self.tracker.driver_position(body))
            if path == "/api/driver/stop":
                return self._send(200, self.tracker.driver_stop(body))
            if path == "/api/confirm":
                return self._send(200, dict(self.tracker.confirm(body, self._ip()),
                                            message="Thank you! We've noted that you received your order."))
            if path == "/api/sign":                    # the receiver signs on their own phone
                return self._send(200, dict(self.tracker.receiver_sign(body, self._ip()),
                                            message="Thank you! Your signature was recorded."))
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
