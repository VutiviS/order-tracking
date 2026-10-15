"""Live vehicle positions from GPS (/api/gps on the tracking site).

Works with anything that speaks the common "OsmAnd" protocol: the free Traccar Client app on the driver's phone (it
keeps sending with the screen locked), and most GPS tracking devices / telematics services that can forward to a web
address. Each vehicle has a private tracker id (Transport > Vehicle tracking > Connect a GPS tracker); the device or
app sends it as `id` with lat, lon, speed (knots), bearing and timestamp.

Every position is kept (vehicle_positions: history and the route driven) and the vehicle's latest one is on the fleet
record. While the vehicle is out on a delivery trip the trip gets it too, so customers see the vehicle on their
tracking page. Mixed into the tracking site's Tracker (needs self.db)."""

import threading
import time
from datetime import datetime

KNOTS_TO_KMH = 1.852
MIN_SECONDS = 15                     # store at most one position every 15 seconds per vehicle


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _when(ts) -> str:
    if ts in (None, ""):
        return datetime.now().replace(microsecond=0).isoformat()
    try:
        t = float(ts)
        if t > 1e12:                          # milliseconds
            t /= 1000
        return datetime.fromtimestamp(t).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().replace(tzinfo=None,
                                                                                               microsecond=0).isoformat()
        except ValueError:
            return datetime.now().replace(microsecond=0).isoformat()


class GpsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class GpsFeed:
    _gps_lock = threading.Lock()
    _gps_last = {}

    def _fleet_by(self, column, value):
        rows = self.db.table("fleet_vehicles").select("*").execute().data or []
        v = str(value or "").replace(" ", "").upper()
        return next((r for r in rows if str(r.get(column) or "").replace(" ", "").upper() == v and v), None)

    def record_position(self, vehicle: dict, lat, lng, speed_kmh=None, heading=None, accuracy=None, at=None,
                        source="gps", trip_id=None) -> dict:
        at = at or datetime.now().replace(microsecond=0).isoformat()
        pos = {"lat": round(lat, 6), "lng": round(lng, 6), "speed": speed_kmh, "heading": heading, "acc": accuracy,
               "at": at, "source": source}
        try:
            self.db.table("vehicle_positions").insert({"fleet_id": vehicle["fleet_id"], "at": at, "lat": pos["lat"],
                                                       "lng": pos["lng"], "speed_kmh": speed_kmh, "heading": heading,
                                                       "accuracy": accuracy, "source": source,
                                                       "trip_id": trip_id}).execute()
        except Exception:
            pass                                   # history table not created yet: the latest position still counts
        try:
            self.db.table("fleet_vehicles").update({"last_position": pos}).eq("fleet_id", vehicle["fleet_id"]).execute()
        except Exception:
            pass
        return pos

    def gps(self, params: dict) -> dict:
        dev = str(params.get("id") or params.get("deviceid") or params.get("device_id") or "").strip()
        lat, lng = _f(params.get("lat")), _f(params.get("lon") or params.get("lng"))
        if not dev or lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise GpsError("id, lat and lon are needed")
        v = self._fleet_by("tracker_id", dev)
        if not v:
            raise GpsError("Unknown device", 404)
        now = time.time()
        with self._gps_lock:
            if now - self._gps_last.get(v["fleet_id"], 0) < MIN_SECONDS:
                return {"ok": True, "stored": False}
            self._gps_last[v["fleet_id"]] = now
        speed = _f(params.get("speed"))
        kmh = round(speed * KNOTS_TO_KMH, 1) if speed is not None else None
        heading = _f(params.get("bearing") or params.get("heading") or params.get("course"))
        at = _when(params.get("timestamp") or params.get("time"))
        reg = str(v.get("registration") or "").replace(" ", "").upper()
        trip = None
        try:
            trips = self.db.table("delivery_trips").select("*").eq("status", "Departed").execute().data or []
            trip = next((t for t in trips if str(t.get("vehicle") or "").split("·")[0].replace(" ", "").upper() == reg), None)
        except Exception:
            trips = []
        pos = self.record_position(v, lat, lng, kmh, heading, _f(params.get("accuracy")), at, "tracker",
                                   trip["trip_id"] if trip else None)
        if trip:
            trail = (list(trip.get("positions") or []) + [pos])[-300:]
            self.db.table("delivery_trips").update({"last_position": pos, "positions": trail}).eq(
                "trip_id", trip["trip_id"]).execute()
        return {"ok": True, "stored": True}
