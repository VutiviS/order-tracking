"""Customer order tracking: the rules shared by the desktop app (Dispatch > Order tracking) and the online tracking
site (inventra-track-online/). Pure Python, no database: both sides pass in the rows they read.

KEEP THIS FILE IDENTICAL to inventra-track-online/tracking_domain.py (copy it after every change).

Where an order is comes from what the business already records:
  received     the order exists (customer_order)
  preparing    staff marked it "being prepared / packed" (order_tracking)
  dispatched   it was dispatched (despatching)
  in_transit   staff marked it on the way, or its cross-border shipment is moving / at the border
  delivered    staff recorded the delivery, the customer confirmed it, or the shipment was delivered

Each order has a secret tracking code made from its number (HMAC with the database key), so a tracking link can't be
guessed from an order number alone. Customers without the link can look an order up with its order number."""

import base64
import hashlib
import hmac
import math
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

DELIVERY_SKU = "DELIVERY"
TABLE = "order_tracking"
STAGES = [("received", "Order received"), ("preparing", "Being prepared"), ("dispatched", "Dispatched"),
          ("in_transit", "On the way"), ("delivered", "Delivered")]
STAGE_KEYS = [k for k, _ in STAGES]
STAGE_LABELS = dict(STAGES)
# what staff can record (desktop), with the words the customer sees
STAFF_EVENTS = {
    "preparing": "Your order is being prepared",
    "loaded": "Loaded on the delivery vehicle",
    "departed": "Out for delivery",
    "arrived": "The driver has arrived",
    "in_transit": "Your order is on the way",
    "delayed": "Your delivery is delayed",
    "failed": "We couldn't deliver your order",
    "delivered": "Delivered",
    "note": "Update",
}
BORDER_WORDS = {"In transit": ("in_transit", "On the way to the border"),
                "At the border": ("in_transit", "At the border"),
                "Held by customs": ("in_transit", "Waiting for customs clearance"),
                "Cleared customs": ("in_transit", "Cleared customs"),
                "Delivered": ("delivered", "Delivered"),
                "Closed": ("delivered", "Delivered")}


def num(v) -> float:
    try:
        return float(str(v).replace("R", "").replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _dt(v) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def when_text(v) -> str:
    d = _dt(v)
    if not d:
        return ""
    has_time = bool(d.hour or d.minute)
    return d.strftime("%a %d %b %Y, %H:%M") if has_time else d.strftime("%a %d %b %Y")


# ---- the tracking code ----

def clean_order_id(text) -> str:
    return re.sub(r"[^0-9A-Za-z\-]", "", str(text or "")).strip()


def track_code(secret: str, order_id) -> str:
    """8 characters like 'K7Q2-M9XD' (no 0/O/1/I confusion: base32)."""
    mac = hmac.new(str(secret).encode(), ("inventra-track:" + clean_order_id(order_id)).encode(), hashlib.sha256).digest()
    raw = base64.b32encode(mac).decode()[:8]
    return raw[:4] + "-" + raw[4:]


def code_ok(secret: str, order_id, code) -> bool:
    given = re.sub(r"[^0-9A-Za-z]", "", str(code or "")).upper()
    want = track_code(secret, order_id).replace("-", "")
    return len(given) == 8 and hmac.compare_digest(given, want)


def track_link(base_url: str, secret: str, order_id) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}/o/{clean_order_id(order_id)}-{track_code(secret, order_id).replace('-', '')}" if base else ""


def split_link_key(key: str):
    """'/o/261001-K7Q2M9XD' -> ('261001', 'K7Q2M9XD')."""
    key = str(key or "").strip().strip("/")
    if "-" not in key:
        return None, None
    oid, code = key.rsplit("-", 1)
    return clean_order_id(oid), code


# ---- is this the customer? ----

def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def contact_matches(customer: dict, text: str) -> bool:
    """The phone number (last 9 digits, so 082… and +2782… both match) or the e-mail on the customer record."""
    t = str(text or "").strip()
    if not t or not customer:
        return False
    if "@" in t:
        emails = [e.strip().lower() for e in re.split(r"[;,\s]+", str(customer.get("email") or "")) if e.strip()]
        return t.lower() in emails
    d = _digits(t)
    if len(d) < 9:
        return False
    phones = [_digits(p) for p in re.split(r"[;,/]+", str(customer.get("phone_number") or ""))]
    return any(len(p) >= 9 and p[-9:] == d[-9:] for p in phones)


# ---- the order, from its customer_order rows ----

def group_order(rows: List[dict]) -> Optional[dict]:
    if not rows:
        return None
    r0 = rows[0]
    lines = []
    for r in rows:
        price, value = num(r.get("price")), num(r.get("value"))
        qty = max(1, round(value / price)) if price > 0 and value > 0 else 1
        if (r.get("sku") or "") == DELIVERY_SKU:
            continue
        lines.append({"description": r.get("product_description") or r.get("sku") or "Item", "qty": qty})
    return {"order_id": str(r0.get("order_id") or ""), "client_name": r0.get("client_name") or "",
            "order_date": str(r0.get("order_date") or "")[:10], "otime": str(r0.get("otime") or "")[:5],
            "status": r0.get("status") or "", "lines": lines,
            "cancel_reason": r0.get("reason_cancellation") or r0.get("cancel_reason") or "",
            "priority": r0.get("priority") or "Normal", "deliver_by": r0.get("deliver_by"),
            "delivery_address": r0.get("delivery_address") or "",
            "delivery_lat": r0.get("delivery_lat"), "delivery_lng": r0.get("delivery_lng"),
            "value": sum(num(r.get("value")) for r in rows)}


# ---- where is it? ----

def stage_of(order: dict, despatch: List[dict], track: Optional[dict], shipments: List[dict]) -> str:
    track = track or {}
    if str(order.get("status") or "").lower() in ("cancelled", "canceled"):
        return "cancelled"
    kinds = {e.get("kind") for e in track.get("events") or []}
    ship_states = {s.get("status") for s in shipments}
    if track.get("delivered_at") or track.get("confirmed_at") or "delivered" in kinds or \
            ship_states & {"Delivered", "Closed"}:
        return "delivered"
    if kinds & {"in_transit", "failed", "delayed", "departed", "arrived"} or \
            ship_states & {"In transit", "At the border", "Held by customs", "Cleared customs"}:
        return "in_transit"
    if despatch or str(order.get("status") or "").lower() == "dispatched":
        return "dispatched"
    if "preparing" in kinds:
        return "preparing"
    return "received"


def vehicle_view(order_id: str, trip: Optional[dict], track: dict, stage: str, now: datetime) -> Optional[dict]:
    """What the customer sees about the vehicle bringing their order: registration, driver, when it was last seen and
    where (only while it is out for delivery), and how many deliveries come before theirs."""
    track = track or {}
    if stage in ("delivered", "cancelled"):
        return None
    if not trip:
        if track.get("courier") and not track.get("vehicle"):
            return {"kind": "courier", "courier": track.get("courier"), "waybill": track.get("waybill") or ""}
        last = next((e for e in reversed(track.get("events") or []) if e.get("kind") == "allocated"), None)
        if last and str(last.get("note") or "").startswith("The customer collects"):
            return {"kind": "collect"}
        return None
    reg = str(trip.get("vehicle") or track.get("vehicle") or "").split("·")[0].strip()
    driver = str(trip.get("driver") or track.get("driver") or "").split()[0] if (trip.get("driver") or track.get("driver")) else ""
    stops = [str(o) for o in trip.get("orders") or []]
    done = set(trip.get("_done") or []) | {str(p.get("order_id")) for p in trip.get("plan") or []
                                           if p.get("done") or p.get("failed")}
    me = stops.index(str(order_id)) + 1 if str(order_id) in stops else None
    before = sum(1 for o in stops[:me - 1] if o not in done) if me else None
    out = {"kind": "vehicle", "vehicle": reg, "driver": driver, "status": trip.get("status") or "Planned",
           "stop": me, "stops": len(stops), "before": before, "trip_date": str(trip.get("trip_date") or "")[:10]}
    eta = next((p.get("eta") for p in trip.get("plan") or [] if str(p.get("order_id")) == str(order_id)), None)
    if eta:
        try:
            h, m = map(int, str(eta).split(":")[:2])
            lo, hi = h * 60 + m - 30, h * 60 + m + 30
            out["eta"] = eta
            out["window"] = f"{max(0, lo) // 60:02d}:{max(0, lo) % 60:02d} and {hi // 60:02d}:{hi % 60:02d}"
        except ValueError:
            pass
    pos = trip.get("last_position") or {}
    if trip.get("status") == "Departed" and pos.get("lat") is not None and pos.get("lng") is not None:
        seen = _dt(pos.get("at"))
        mins = int((now - seen).total_seconds() // 60) if seen else None
        if mins is not None and mins <= 180:
            out.update(lat=round(float(pos["lat"]), 5), lng=round(float(pos["lng"]), 5), seen_minutes=max(0, mins),
                       last_seen="just now" if mins < 2 else f"{mins} minutes ago" if mins < 90 else
                       f"{mins // 60} h {mins % 60} min ago")
    left = next((e.get("at") for e in reversed(trip.get("events") or []) if e.get("kind") == "departed"), None)
    if left:
        out["left_at"] = when_text(left)
    plan = {str(p.get("order_id")): p for p in trip.get("plan") or []}
    mine = plan.get(str(order_id)) or {}
    if mine.get("lat") is not None and mine.get("lng") is not None:
        out["dest_lat"], out["dest_lng"] = round(float(mine["lat"]), 5), round(float(mine["lng"]), 5)
    live = live_eta(stops, me, done, plan, out, now)
    if live:
        out.update(live)
    return out


ROAD_FACTOR, CITY_KMH, STOP_MINUTES = 1.3, 40.0, 12.0


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def live_eta(stops: List[str], me: Optional[int], done: set, plan: dict, out: dict, now: datetime) -> Optional[dict]:
    """While the vehicle is out and was seen in the last 30 minutes: when it should reach this customer, from where it
    is now through the stops still before theirs (road distance ≈ straight line × 1.3, 40 km/h, 12 min per stop)."""
    if not me or out.get("lat") is None or (out.get("seen_minutes") or 0) > 30:
        return None
    route = [o for o in stops[:me - 1] if o not in done and not (plan.get(o) or {}).get("done")] + [stops[me - 1]]
    pts = [(float(plan[o]["lat"]), float(plan[o]["lng"])) for o in route
           if (plan.get(o) or {}).get("lat") is not None and (plan.get(o) or {}).get("lng") is not None]
    if len(pts) != len(route):
        return None                           # a stop without a map position: keep the planned window
    pos, km = (out["lat"], out["lng"]), 0.0
    for p in pts:
        km += _km(pos, p) * ROAD_FACTOR
        pos = p
    minutes = km / CITY_KMH * 60 + STOP_MINUTES * (len(pts) - 1)
    at = now + timedelta(minutes=minutes)
    return {"live_eta": at.strftime("%H:%M"), "live_minutes": int(round(minutes)), "live_km": round(km, 1)}


def view(order: dict, despatch: List[dict], track: Optional[dict], shipments: List[dict],
         now: Optional[datetime] = None, trip: Optional[dict] = None) -> dict:
    """Everything the customer sees about one order (nothing about prices or other customers)."""
    now = now or datetime.now()
    track = dict(track or {})
    stage = stage_of(order, despatch, track, shipments)
    events = []                                          # the timeline, oldest first (the page shows newest first)
    placed = f"{order.get('order_date') or ''} {order.get('otime') or ''}".strip()
    events.append({"at": placed, "title": "Order received", "detail": "We have your order.", "kind": "received"})
    goods = [d for d in despatch if (d.get("sku") or "") != DELIVERY_SKU]
    delivery_line = next((d for d in despatch if (d.get("sku") or "") == DELIVERY_SKU), None)
    for e in track.get("events") or []:
        if e.get("kind") == "preparing":
            events.append({"at": e.get("at"), "title": STAFF_EVENTS["preparing"], "detail": e.get("note") or "",
                           "kind": "preparing"})
    if goods or stage in ("dispatched", "in_transit", "delivered") and despatch:
        first = min((str(d.get("dispatch_date") or "") for d in despatch), default="")
        units = sum(num(d.get("dispatched_quantity")) for d in goods)
        bits = [f"{units:g} item{'s' if units != 1 else ''} left our warehouse" if units else "Your order left our warehouse"]
        courier = track.get("courier") or next((d.get("courier") for d in despatch if d.get("courier")), "")
        if courier:
            bits.append(f"with {courier}")
        if track.get("waybill"):
            bits.append(f"(waybill {track['waybill']})")
        where = str((delivery_line or {}).get("product_description") or "").replace(" (not charged to client)", "")
        if where and where.strip().lower() != "delivery":
            bits.append("· " + where)
        events.append({"at": first, "title": "Dispatched", "detail": " ".join(bits), "kind": "dispatched"})
    for s in shipments:
        for e in s.get("events") or []:
            st = e.get("status")
            if st in BORDER_WORDS and st not in ("Delivered", "Closed"):
                where = s.get("border_post") or ""
                detail = f"{where}" if st in ("At the border", "Cleared customs", "Held by customs") and where else ""
                if st == "In transit" and s.get("destination_country"):
                    detail = f"Going to {s['destination_country']}"
                events.append({"at": e.get("at"), "title": BORDER_WORDS[st][1], "detail": detail, "kind": "border"})
            elif st in ("Delivered", "Closed") and not track.get("delivered_at"):
                events.append({"at": e.get("at"), "title": "Delivered", "detail": "", "kind": "delivered"})
                break
    for e in track.get("events") or []:
        k = e.get("kind")
        if k in ("in_transit", "delayed", "failed", "note", "departed", "arrived"):
            events.append({"at": e.get("at"), "title": STAFF_EVENTS[k], "detail": e.get("note") or "",
                           "kind": "in_transit" if k in ("departed", "arrived") else k})
        elif k == "loaded":
            events.append({"at": e.get("at"), "title": STAFF_EVENTS[k], "detail": e.get("note") or "",
                           "kind": "dispatched"})
        elif k == "delivered":
            who = track.get("received_by") or e.get("received_by") or ""
            events.append({"at": e.get("at"), "title": "Delivered",
                           "detail": f"Received by {who}" if who else (e.get("note") or ""), "kind": "delivered"})
    if track.get("confirmed_at"):
        events.append({"at": track["confirmed_at"], "title": "You confirmed you received it",
                       "detail": track.get("feedback") or "", "kind": "confirmed"})
    if stage == "cancelled":
        events.append({"at": "", "title": "Order cancelled",
                       "detail": order.get("cancel_reason") or "Contact us if you have questions.", "kind": "cancelled"})
    events.sort(key=lambda e: (_dt(e.get("at")) or datetime.min))
    for e in events:
        e["when"] = when_text(e.get("at"))
    steps = []
    reached = STAGE_KEYS.index(stage) if stage in STAGE_KEYS else 0
    for i, (k, label) in enumerate(STAGES):
        at = next((e["at"] for e in events if e["kind"] == k or (k == "in_transit" and e["kind"] == "border")
                   or (k == "delivered" and e["kind"] == "confirmed")), "")
        steps.append({"key": k, "label": label, "done": stage != "cancelled" and i <= reached,
                      "current": stage == k and k != "delivered", "when": when_text(at)})
    items = ([{"description": d.get("product_description") or d.get("sku") or "Item",
               "qty": num(d.get("dispatched_quantity")) or 1} for d in goods]
             or [{"description": l["description"], "qty": l["qty"]} for l in order.get("lines") or []])
    last = events[-1] if events else {}
    pod = track.get("pod") or {}
    if pod.get("condition") and pod["condition"] != "Good":
        events.append({"at": pod.get("at"), "title": f"Delivered with a problem: {pod['condition'].lower()}",
                       "detail": "; ".join(f"{i.get('qty', '')} × {i.get('description') or i.get('sku')}: {i.get('kind')}"
                                           for i in pod.get("issues") or []) or (pod.get("note") or ""),
                       "kind": "failed", "when": when_text(pod.get("at"))})
        last = events[-1]
    problem = last.get("kind") in ("failed", "delayed") or any(
        (e.get("events") or [{}])[-1].get("status") == "Held by customs" for e in shipments if e.get("events"))
    eta = track.get("eta") or next((s.get("eta") for s in shipments if s.get("eta")), "")
    headline = {"received": "We have your order", "preparing": "Your order is being prepared",
                "dispatched": "Your order has been dispatched", "in_transit": "Your order is on the way",
                "delivered": "Your order has been delivered", "cancelled": "This order was cancelled"}[stage]
    if problem:
        headline = last.get("title") or headline
    return {"order_id": order.get("order_id"), "client": order.get("client_name") or "",
            "ordered": when_text(order.get("order_date")), "stage": stage, "stage_label":
            STAGE_LABELS.get(stage, "Cancelled"), "headline": headline, "problem": bool(problem),
            "message": track.get("public_note") or "", "eta": when_text(eta) if eta and stage != "delivered" else "",
            "courier": track.get("courier") or "", "waybill": track.get("waybill") or "",
            "received_by": track.get("received_by") or "", "steps": steps, "items": items,
            "proof": {"by": pod.get("received_by") or track.get("received_by") or "", "when": when_text(pod.get("at")),
                      "signature": pod.get("signature") or "", "photo": bool(pod.get("photo_path")),
                      "condition": pod.get("condition") or "",
                      "at_address": (pod.get("location") or {}).get("status") == LOC_OK,
                      "signed_on": pod.get("signed_on") or ""} if (pod or track.get("delivered_at")) else None,
            "events": list(reversed(events)), "confirmed": bool(track.get("confirmed_at")),
            "can_confirm": stage in ("dispatched", "in_transit", "delivered") and not track.get("confirmed_at"),
            "border": next(({"status": s.get("status"), "border_post": s.get("border_post") or "",
                             "to": s.get("destination_country") or ""} for s in shipments), None),
            "updated": now.strftime("%H:%M"),
            "delivery": vehicle_view(order.get("order_id"), trip, track, stage, now)}


def add_event(track: Optional[dict], kind: str, note: str = "", by: str = "", when: Optional[datetime] = None,
              **fields) -> dict:
    """A new copy of the order_tracking row with one more event (staff or customer)."""
    when = when or datetime.now()
    t = dict(track or {})
    ev = list(t.get("events") or [])
    ev.append({"kind": kind, "at": when.isoformat(timespec="seconds"), "note": str(note or "")[:300], "by": by,
               **{k: v for k, v in fields.items() if k == "received_by"}})
    t["events"] = ev[-60:]
    t.update({k: v for k, v in fields.items() if v is not None})
    if kind == "delivered":
        t["delivered_at"] = when.isoformat(timespec="seconds")
    t["stage"] = kind if kind in STAGE_KEYS else t.get("stage") or ""
    t["updated_at"] = when.isoformat(timespec="seconds")
    if by:
        t["updated_by"] = by
    return t


# ---- delivery performance (Order Tracking > Delivery performance) ----

POD_CONDITIONS = ["Good", "Short", "Damaged", "Rejected"]

# ---- where a delivery was signed for: picked up by the phone's GPS, never typed, and checked against the address ----
NEAR_METRES = 300            # signed within this distance of the delivery address (plus the GPS accuracy, max 200 m)
VEHICLE_METRES = 1000        # the vehicle's own GPS tracker more than this far away = the phone and the vehicle disagree
LOC_OK, LOC_FAR, LOC_NO_ADDRESS, LOC_NONE = "Verified", "Far from the address", "Address not on the map", "No location"


def check_location(fix: Optional[dict], expected: Optional[tuple], vehicle: Optional[dict] = None,
                   now: Optional[datetime] = None) -> dict:
    """fix: {"lat", "lng", "acc"} from the phone at the moment of signing; expected: (lat, lng) of the delivery address;
    vehicle: the vehicle tracker's last position {"lat", "lng", "at", "source"}. Returns what is stored with the proof."""
    now = now or datetime.now()
    out = {"at": now.isoformat(timespec="seconds"), "status": LOC_NONE}
    try:
        lat, lng = float(fix["lat"]), float(fix["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        return out
    acc = num((fix or {}).get("acc"))
    out.update(lat=round(lat, 6), lng=round(lng, 6), acc=round(acc) if acc else None)
    if expected and expected[0] is not None and expected[1] is not None:
        d = _km((lat, lng), (float(expected[0]), float(expected[1]))) * 1000
        out.update(expected={"lat": round(float(expected[0]), 6), "lng": round(float(expected[1]), 6)},
                   distance_m=round(d), status=LOC_OK if d <= NEAR_METRES + min(acc or 0, 200) else LOC_FAR)
    else:
        out["status"] = LOC_NO_ADDRESS
    v = vehicle or {}
    seen = _dt(v.get("at"))
    if v.get("lat") is not None and v.get("source") == "tracker" and seen and abs((now - seen).total_seconds()) <= 900:
        vd = _km((lat, lng), (float(v["lat"]), float(v["lng"]))) * 1000
        out["vehicle_distance_m"] = round(vd)
        if vd > VEHICLE_METRES:
            out["vehicle_warning"] = f"The vehicle's GPS tracker was {vd / 1000:.1f} km away"
    return out


def location_text(loc: Optional[dict]) -> str:
    """'Verified · 40 m from the address (±12 m)' for screens and reports."""
    loc = loc or {}
    st = loc.get("status") or LOC_NONE
    bits = [st]
    if loc.get("distance_m") is not None:
        d = loc["distance_m"]
        bits.append(f"{d} m from the address" if d < 1000 else f"{d / 1000:.1f} km from the address")
    if loc.get("acc"):
        bits[-1] += f" (±{loc['acc']} m)"
    if loc.get("vehicle_warning"):
        bits.append(loc["vehicle_warning"])
    return " · ".join(bits)


def location_problem(loc: Optional[dict]) -> bool:
    loc = loc or {}
    return loc.get("status") in (LOC_FAR, LOC_NONE) or bool(loc.get("vehicle_warning"))


def delivered_on_time(track: dict) -> Optional[bool]:
    """On time = delivered on or before the promised / expected date; None when no date was promised."""
    due, done = _dt((track or {}).get("eta")), _dt((track or {}).get("delivered_at"))
    if not due or not done:
        return None
    return done.date() <= due.date()


def delivery_stats(rows: List[dict]) -> dict:
    """rows: {"order": group_order, "despatch": [...], "track": row or None}. On time, in full, OTIF, days from order
    to delivery, failed attempts and customer ratings."""
    delivered = [r for r in rows if (r.get("track") or {}).get("delivered_at")]
    timed = [r for r in delivered if delivered_on_time(r["track"]) is not None]
    on_time = sum(1 for r in timed if delivered_on_time(r["track"]))
    in_full = sum(1 for r in delivered if ((r["track"].get("pod") or {}).get("condition") or "Good") == "Good")
    otif = sum(1 for r in timed if delivered_on_time(r["track"]) and
               ((r["track"].get("pod") or {}).get("condition") or "Good") == "Good")
    days = []
    for r in delivered:
        a, b = _dt(r["order"].get("order_date")), _dt(r["track"].get("delivered_at"))
        if a and b:
            days.append((b - a).total_seconds() / 86400)
    failed = sum(sum(1 for e in (r.get("track") or {}).get("events") or [] if e.get("kind") == "failed") for r in rows)
    ratings = [int(r["track"]["rating"]) for r in rows if (r.get("track") or {}).get("rating")]
    pct = lambda a, b: (a / b) if b else None
    return {"delivered": len(delivered), "on_time": pct(on_time, len(timed)), "in_full": pct(in_full, len(delivered)),
            "otif": pct(otif, len(timed)), "judged": len(timed), "avg_days": (sum(days) / len(days)) if days else None,
            "failed": failed, "rating": (sum(ratings) / len(ratings)) if ratings else None, "rated": len(ratings)}


# ---- driver PINs (set in Order Tracking > Tracking website & drivers; checked by the site's /driver page) ----

def hash_pin(pin: str, salt: Optional[bytes] = None, iterations: int = 120_000) -> str:
    import os as _os
    salt = salt or _os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def pin_ok(stored: str, pin: str) -> bool:
    try:
        _algo, iterations, salt, digest = str(stored).split("$")
        test = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


def pin_problem(pin: str) -> str:
    pin = str(pin or "")
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        return "Use 4 to 8 digits."
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "Too easy to guess: avoid 1111 or 1234."
    return ""


# ---- the supplier portal: one private link per supplier (/s/<supplierid>-<code>) ----

def supplier_code(secret: str, supplierid) -> str:
    mac = hmac.new(str(secret).encode(), ("inventra-supplier:" + str(supplierid)).encode(), hashlib.sha256).digest()
    return base64.b32encode(mac).decode()[:10]


def supplier_link(base_url: str, secret: str, supplierid) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}/s/{supplierid}-{supplier_code(secret, supplierid)}" if base else ""


def supplier_from_key(secret: str, key: str):
    """'/s/12-ABCDEFGHJK' -> '12' when the code is right, else None."""
    key = str(key or "").strip().strip("/")
    if "-" not in key:
        return None
    sid, code = key.rsplit("-", 1)
    return sid if hmac.compare_digest(code.upper(), supplier_code(secret, sid)) else None
