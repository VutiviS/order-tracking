"""Proof of delivery with a checked location.

The receiver signs on a phone: the driver's phone (handed over at the door), or their own phone on the tracking page
(the "Sign for your delivery" card appears once the driver has tapped "I've arrived"). Either way the phone's GPS is
read automatically at the moment of signing, never typed, and compared with the delivery address (and with the
vehicle's GPS tracker when it has one), so a delivery can't be recorded from somewhere else without it showing.
Every step is also written to the user activity log. Mixed into the tracking site's Tracker (needs self.db)."""

import logging
from datetime import datetime, timedelta

import tracking_domain as TD

log = logging.getLogger("inventra-track")
SIGN_HOURS = 3                 # the receiver can sign on their own phone this long after the driver arrived
SIG_MAX = 200_000


class ProofError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def fix_from(body: dict):
    """The location the phone sent with the request ({"lat", "lng", "acc"}), or None."""
    try:
        lat, lng = float(body.get("lat")), float(body.get("lng"))
    except (TypeError, ValueError):
        return None
    return {"lat": lat, "lng": lng, "acc": body.get("accuracy") or body.get("acc")}


def signature_ok(sig: str) -> bool:
    return bool(sig) and sig.startswith("data:image/png;base64,") and len(sig) <= SIG_MAX


def arrived_at(track: dict):
    """When the driver last tapped "I've arrived" for this order, if nothing happened at the stop since."""
    for e in reversed((track or {}).get("events") or []):
        if e.get("kind") in ("delivered", "failed"):
            return None
        if e.get("kind") == "arrived":
            return TD._dt(e.get("at"))
    return None


def receiver_can_sign(track: dict, stage: str, now: datetime) -> bool:
    at = arrived_at(track)
    return stage not in ("delivered", "cancelled") and not (track or {}).get("pod") and bool(at) and \
        now - at <= timedelta(hours=SIGN_HOURS)


class ProofMixin:
    def expected_point(self, trip, oid: str):
        """Where the order should be delivered: the route plan's point, else the map pin saved with the order."""
        p = next((x for x in (trip or {}).get("plan") or [] if str(x.get("order_id")) == str(oid)), {})
        if p.get("lat") is not None and p.get("lng") is not None:
            return (p["lat"], p["lng"])
        try:
            rows = self.db.table("customer_order").select("delivery_lat,delivery_lng").eq(
                "order_id", int(oid) if str(oid).isdigit() else oid).execute().data or []
        except Exception:
            rows = []
        r = next((x for x in rows if x.get("delivery_lat") is not None and x.get("delivery_lng") is not None), None)
        return (r["delivery_lat"], r["delivery_lng"]) if r else None

    def vehicle_position(self, trip):
        reg = str((trip or {}).get("vehicle") or "").split("·")[0].replace(" ", "").upper()
        if not reg:
            return None
        try:
            rows = self.db.table("fleet_vehicles").select("registration,last_position").execute().data or []
        except Exception:
            return None
        v = next((r for r in rows if str(r.get("registration") or "").replace(" ", "").upper() == reg), None)
        return (v or {}).get("last_position")

    def located(self, body: dict, trip, oid: str, need=True) -> dict:
        """The checked location for a stop; refuses when the phone sent none (need=True)."""
        fix = fix_from(body)
        if fix is None and need:
            raise ProofError("Your phone's location is needed. Allow location for this page (tap the lock / "
                             "\"aA\" next to the address > Location > Allow), switch location on, and try again.")
        return TD.check_location(fix, self.expected_point(trip, oid), self.vehicle_position(trip))

    def activity(self, who: str, title: str, detail: str = "", loc=None, app="Driver phone", kind="phone"):
        """One line in the user activity log (Control Tower > User activity)."""
        loc = loc or {}
        try:
            self.db.table("activity_log").insert({
                "at": datetime.now().isoformat(timespec="seconds"), "username": who, "full_name": who, "app": app,
                "kind": kind, "title": title[:300], "detail": detail[:1000], "table_name": "",
                "device": "phone", "source": "web", "lat": loc.get("lat"), "lng": loc.get("lng")}).execute()
        except Exception as e:
            log.info("activity not logged: %s", str(e)[:120])

    # ---- the receiver signs on their own phone (tracking page) ----
    def receiver_sign(self, body: dict, ip: str) -> dict:
        oid, code = TD.split_link_key(body.get("key"))
        if not oid or not TD.code_ok(self.secret, oid, code):
            raise ProofError("This tracking link isn't valid.", 404)
        self._limit(ip)
        order, despatch, track, ships, trip = self.order(oid, fresh=True)
        now = datetime.now().replace(microsecond=0)
        if not receiver_can_sign(track, TD.stage_of(order, despatch, track, ships), now):
            raise ProofError("You can sign here once the driver is at your door and has marked that they arrived.", 409)
        name = " ".join(str(body.get("name") or "").split())[:80]
        if len(name) < 2:
            raise ProofError("Type your full name.")
        sig = str(body.get("signature") or "")
        if not signature_ok(sig):
            raise ProofError("Please sign in the box.")
        cond = body.get("condition") if body.get("condition") in ("Good", "Short", "Damaged") else "Good"
        note = str(body.get("note") or "").strip()[:300]
        if cond != "Good" and not note:
            raise ProofError("Say what was short or damaged.")
        loc = self.located(body, trip, oid)
        pod = {"received_by": name, "condition": cond, "issues": [], "note": note, "signature": sig,
               "at": now.isoformat(), "by": "receiver", "signed_on": "receiver's phone", "location": loc,
               "trip_id": (trip or {}).get("trip_id"), "lat": loc.get("lat"), "lng": loc.get("lng")}
        row = TD.add_event(track, "delivered", "Signed for on the receiver's phone" + (f": {note}" if note else ""),
                           by=name, when=now, received_by=name)
        row["pod"] = pod
        if trip:
            row["trip_id"] = trip.get("trip_id")
        self._save_track(oid, row)
        self.activity(name, f"Receiver signed for order {oid} on their own phone",
                      f"{cond} · {TD.location_text(loc)}", loc, app="Tracking website (receiver)")
        return self.view(oid, fresh=True)
