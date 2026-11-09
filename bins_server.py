"""INVENTRA Bins: the phone app for storage bins (its own website, published to GitHub and run by Render).

  * Scan a bin label (phone camera, or a USB / Bluetooth scanner): what is in the bin - binned, moved out,
    dispatched, available now, expiring soon / expired - and its movements. The same figures as Stock Control >
    Scan a bin (inventra_bin_report, copied here by sync_web_files.py).
  * Bin labels: the bin code, a Code 128 barcode and a QR code, as a PDF to print or share (inventra_bin_labels,
    the same labels as the desktop).
  * Every bin: the list, with what each one holds, to open or label.

  * Check a bin: "This bin is 100% correct", or what is different (counts, missing serial numbers, anything else
    found) - saved for Stock Control (table bin_checks), and the list of the inaccurate bins to print, with or without
    the quantities and serial numbers (inventra_bin_checks).

Staff sign in with their INVENTRA username and password (app_users). The app never changes stock: a bin check is only
a record of what was found.

    python bins_server.py            (PORT, default 8780)

Settings on the host (Render > Environment): SUPABASE_URL, SUPABASE_KEY (the same as the other websites) and, once the
database is locked (Settings > Security), INVENTRA_ACCESS_KEY. Optional: INVENTRA_EXPIRY_WARNING_DAYS (default 30)."""

import base64
import hashlib
import hmac
import io
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

# the same database as the INVENTRA desktop apps (their built-in public key) when the host has no settings of its own
DEFAULT_SUPABASE_URL = "https://lnimjatrcvpxnumkxqzs.supabase.co"
DEFAULT_SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxuaW1qYXRyY3ZweG51bWt4cXpzIiw"
    "icm9sZSI6ImFub24iLCJpYXQiOjE3NjIzNzY2NDYsImV4cCI6MjA3Nzk1MjY0Nn0.P-KgfRbbR1WDXQ31lViUYlHA0NpdCLd9YgUcV2DbmLA"
)
PRIVATE_EDITION = (os.environ.get("INVENTRA_EDITION") or "").strip().lower() == "private"
for _name, _value in (("SUPABASE_URL", DEFAULT_SUPABASE_URL), ("SUPABASE_KEY", DEFAULT_SUPABASE_KEY)):
    if not (os.environ.get(_name) or "").strip():
        if PRIVATE_EDITION:
            raise SystemExit(f"INVENTRA Private: set {_name} to the customer's own database.")
        os.environ[_name] = _value
    else:
        os.environ[_name] = os.environ[_name].strip().strip('"').strip("'")
ACCESS_KEY = (os.environ.get("INVENTRA_ACCESS_KEY") or "").strip()

import inventra_bin_checks  # noqa: E402  (copies of the desktop's files: sync_web_files.py)
import inventra_bin_labels  # noqa: E402
import inventra_bin_report  # noqa: E402
import inventra_bins  # noqa: E402

HERE = Path(__file__).resolve().parent
log = logging.getLogger("inventra.bins")
VERSION = "INVENTRA-bins/1.0"
TOKEN_DAYS = 14
FRESH_FOR = 30                      # seconds the bin figures are reused (a row of bins scans quickly)
TRIES, TRY_WINDOW = 8, 15 * 60      # wrong passwords per address / account before a pause
ROLES_VIEW = {"Administrator", "Manager (view only)", "Procurement officer", "Logistics officer", "Stock controller",
              "Dispatch officer", "Production officer"}
ROLES_LABELS = ROLES_VIEW - {"Manager (view only)"}

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; worker-src 'self' blob:; "
       "manifest-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
                    ("Permissions-Policy", "camera=(self), geolocation=(), microphone=(), payment=(), usb=()"),
                    ("Cross-Origin-Opener-Policy", "same-origin")]
STATIC = {"/icon-192.png": "image/png", "/icon-512.png": "image/png", "/apple-touch-icon.png": "image/png",
          "/favicon.svg": "image/svg+xml", "/logo.svg": "image/svg+xml"}


class BinsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _roles(u: dict) -> list:
    r = u.get("roles")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except ValueError:
            r = [x.strip() for x in r.split(",")]
    return [str(x) for x in r or []]


def _password_ok(stored: str, password: str) -> bool:
    """INVENTRA's password hashes: pbkdf2$<iterations>$<salt hex>$<digest hex> (the same check as the desktop)."""
    try:
        _algo, iterations, salt, digest = str(stored).split("$")
        test = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


class Bins:
    def __init__(self):
        from supabase import ClientOptions, create_client
        self.db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"],
                                options=ClientOptions(headers={"x-inventra-key": ACCESS_KEY} if ACCESS_KEY else {}))
        self.secret = hashlib.sha256(("inventra-bins:" + (ACCESS_KEY or os.environ["SUPABASE_KEY"])).encode()).digest()
        self.lock = threading.Lock()
        self.failures = {}
        self.cache = {"at": 0.0, "tables": None}

    # ---- signing in ----
    def _limit(self, key):
        with self.lock:
            now = time.time()
            hits = [t for t in self.failures.get(key, []) if now - t < TRY_WINDOW]
            self.failures[key] = hits
            if len(hits) >= TRIES:
                raise BinsError("Too many tries. Wait a few minutes, then try again.", 429)

    def _failed(self, key):
        with self.lock:
            self.failures.setdefault(key, []).append(time.time())

    def _sign(self, payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        return raw + "." + hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:40]

    def login(self, body: dict, ip: str) -> dict:
        name = str(body.get("username") or "").strip().lower()
        self._limit("ip:" + ip)
        self._limit("user:" + name)
        rows = [u for u in self.db.table("app_users").select("*").execute().data or []
                if str(u.get("username") or "").lower() == name and u.get("active") is not False]
        u = rows[0] if rows else None
        if not u or not _password_ok(u.get("password_hash") or "", str(body.get("password") or "")):
            self._failed("ip:" + ip)
            self._failed("user:" + name)
            raise BinsError("Username or password not right (the same as for INVENTRA).", 403)
        roles = set(_roles(u))
        if not roles & ROLES_VIEW:
            raise BinsError("Your job role doesn't work with the bins. Ask your administrator.", 403)
        tag = hashlib.sha256(str(u.get("password_hash")).encode()).hexdigest()[:12]
        token = self._sign({"u": u["username"], "x": int(time.time()) + TOKEN_DAYS * 86400, "h": tag})
        return {"token": token, "name": u.get("full_name") or u["username"], "labels": bool(roles & ROLES_LABELS)}

    def who(self, token, need_labels: bool = False) -> dict:
        try:
            raw, sig = str(token).split(".", 1)
            if not hmac.compare_digest(sig, hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:40]):
                raise ValueError
            p = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            if p["x"] < time.time():
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise BinsError("Please sign in again.", 401)
        rows = self.db.table("app_users").select("*").eq("username", p["u"]).execute().data or []
        if not rows or rows[0].get("active") is False or \
                hashlib.sha256(str(rows[0].get("password_hash")).encode()).hexdigest()[:12] != p["h"]:
            raise BinsError("Please sign in again.", 401)            # password changed or account switched off
        roles = set(_roles(rows[0]))
        if not roles & (ROLES_LABELS if need_labels else ROLES_VIEW):
            raise BinsError("Your job role can't do this.", 403)
        return rows[0]

    # ---- the figures ----
    def tables(self, fresh: bool = False) -> dict:
        c = self.cache
        if fresh or c["tables"] is None or time.time() - c["at"] > FRESH_FOR:
            c["tables"] = inventra_bin_report.load(self._read)
            c["at"] = time.time()
        return c["tables"]

    def _read(self, name):
        rows, start = [], 0
        while True:                                         # every row, a page of 1,000 at a time
            part = self.db.table(name).select("*").range(start, start + 999).execute().data or []
            rows += part
            if len(part) < 1000:
                return rows
            start += 1000

    def bin_list(self, body: dict) -> dict:
        self.who(body.get("token"))
        t = self.tables(fresh=bool(body.get("fresh")))
        every = inventra_bins.merge_bin_lists(t.get("stock_bins", []), t.get("bins", []))
        units = {}
        for code in {b["bin_code"] for b in every}:
            r = inventra_bin_report.report(t, code)
            if r:
                units[code.upper()] = r["totals"]
        out = []
        for b in sorted(every, key=lambda b: str(b["bin_code"])):
            tot = units.get(str(b["bin_code"]).upper(), {})
            out.append({"code": b["bin_code"], "zone": b.get("zone") or "", "description": b.get("description") or "",
                        "active": b.get("active") is not False, "available": tot.get("available", 0),
                        "products": tot.get("products", 0), "expiring": tot.get("expiring", 0),
                        "expired": tot.get("expired", 0)})
        return {"bins": out, "warn_days": inventra_bin_report.warning_days()}

    def one_bin(self, body: dict) -> dict:
        self.who(body.get("token"))
        code = inventra_bins.bin_code_from_scan(body.get("code"))           # a QR link or the code itself
        if not code:
            raise BinsError("Scan a bin label.")
        t = self.tables()
        r = inventra_bin_report.report(t, code)
        if r is None:                                       # a bin made a moment ago: read again
            t = self.tables(fresh=True)
            r = inventra_bin_report.report(t, code)
        if r is None:
            raise BinsError(f"There is no bin {code.upper()}. Check the label.", 404)
        exp = inventra_bin_checks.expected(t, r["bin"]["code"])
        r["check"] = exp["lines"] if exp else []             # what a check compares with (with the serials)
        last = [c for c in self._checks() if str(c.get("bin_code") or "").upper() == r["bin"]["code"].upper()]
        if last:
            c = max(last, key=lambda c: str(c.get("checked_at") or ""))
            r["last_check"] = {"result": c.get("result"), "when": str(c.get("checked_at") or "")[:16].replace("T", " "),
                               "by": c.get("checked_by_name") or c.get("checked_by"), "status": c.get("status"),
                               "summary": inventra_bin_checks.summary(c) if c.get("result") != "accurate" else ""}
        return r

    # ---- checking a bin ----
    def _checks(self) -> list:
        try:
            return self.db.table(inventra_bin_checks.TABLE).select("*").execute().data or []
        except Exception:
            return []                                       # (the table isn't there until "Fix database access")

    def save_check(self, body: dict) -> dict:
        """'This bin is 100% correct' (all_correct) or what is different (counts {sku: {counted, found_serials,
        extra_serials}}, extra, note). Only a record: stock is never changed here."""
        u = self.who(body.get("token"), need_labels=True)
        code = inventra_bins.bin_code_from_scan(body.get("code"))
        exp = inventra_bin_checks.expected(self.tables(fresh=True), code)
        if exp is None:
            raise BinsError(f"There is no bin {str(code).upper()}.", 404)
        counts = body.get("counts") if isinstance(body.get("counts"), dict) else {}
        try:
            row = inventra_bin_checks.make_check(exp, counts, u["username"], u.get("full_name") or u["username"],
                                                 note=body.get("note"), all_correct=bool(body.get("all_correct")),
                                                 extra=body.get("extra"))
        except ValueError as e:
            raise BinsError(str(e))
        try:
            self.db.table(inventra_bin_checks.TABLE).insert(row).execute()
        except Exception as e:
            low = str(e).lower()
            if "pgrst205" in low or "42p01" in low or "could not find the table" in low:
                raise BinsError("Bin checks can't be saved yet: ask your administrator to run the database update "
                                "(INVENTRA > Settings > Fix database access).", 503)
            raise
        ok = row["result"] == inventra_bin_checks.ACCURATE
        return {"ok": True, "result": row["result"],
                "message": f"Bin {row['bin_code']}: marked 100% correct." if ok else
                           f"Bin {row['bin_code']}: discrepancy reported ({inventra_bin_checks.summary(row)}). "
                           "Stock Control sees it under Bin checks."}

    def inaccurate(self, body: dict) -> dict:
        self.who(body.get("token"))
        rows = inventra_bin_checks.inaccurate(self._checks())
        return {"bins": [{"code": c.get("bin_code"), "when": str(c.get("checked_at") or "")[:16].replace("T", " "),
                          "by": c.get("checked_by_name") or c.get("checked_by"), "summary": inventra_bin_checks.summary(c),
                          "note": c.get("note") or ""} for c in rows]}

    def inaccurate_pdf(self, body: dict):
        """-> (pdf bytes, file name): the inaccurate bins, with quantities and serials (detailed) or without them."""
        self.who(body.get("token"))
        rows = inventra_bin_checks.inaccurate(self._checks())
        t = self.tables()
        zones = {str(b["bin_code"]).upper(): b.get("zone") or ""
                 for b in inventra_bins.merge_bin_lists(t.get("stock_bins", []), t.get("bins", []))}
        detailed = bool(body.get("detailed", True))
        data = inventra_bin_checks.report_pdf(rows, detailed=detailed, zones=zones,
                                              company=os.environ.get("COMPANY_NAME", ""))
        return data, ("Inaccurate bins.pdf" if detailed else "Inaccurate bins - recount sheet.pdf")

    def labels(self, body: dict, link_base: str = ""):
        """-> (pdf bytes, file name). body: codes [..] | zone | all, size sheet|shelf|sign, copies, skip.
        link_base: this app's address - each label's QR code opens the app on its bin."""
        self.who(body.get("token"), need_labels=True)
        t = self.tables()
        every = inventra_bins.merge_bin_lists(t.get("stock_bins", []), t.get("bins", []))
        size = body.get("size") if body.get("size") in inventra_bin_labels.SIZES else "sheet"
        if body.get("codes"):
            known = {str(b["bin_code"]).upper(): b for b in every}
            chosen = [known.get(str(c).strip().upper(), {"bin_code": str(c).strip().upper()})
                      for c in body["codes"][:500] if str(c).strip()]
        elif body.get("zone"):
            chosen = [b for b in every if str(b.get("zone") or "") == str(body["zone"]) and b.get("active") is not False]
        else:
            chosen = [b for b in every if b.get("active") is not False]
        if not chosen:
            raise BinsError("No bins chosen.")
        try:
            copies = max(1, min(20, int(body.get("copies") or 1)))
            skip = max(0, min(50, int(body.get("skip") or 0)))
        except (TypeError, ValueError):
            raise BinsError("Labels per bin and labels already used must be whole numbers.")
        out = io.BytesIO()
        inventra_bin_labels.bin_labels_pdf(out, [{"code": b["bin_code"], "zone": b.get("zone") or "",
                                                  "description": b.get("description") or ""} for b in chosen],
                                           size, skip=skip if size != "sign" else 0, copies=copies, link_base=link_base)
        name = chosen[0]["bin_code"] if len(chosen) == 1 else f"{len(chosen)} bins"
        return out.getvalue(), f"Bin labels {name}.pdf".replace("/", "-").replace('"', "")



# =====================================================================================================================
# the web part: used by this app's own server (base "") and by the order tracking website, which runs the app at /bins
# =====================================================================================================================

_APP = {"app": None, "error": None}
_APP_LOCK = threading.Lock()


def get_app():
    """The one Bins object of this process (made on first use; None while the database can't be reached)."""
    with _APP_LOCK:
        if _APP["app"] is None:
            try:
                _APP["app"], _APP["error"] = Bins(), None
            except Exception as e:                          # e.g. no network yet: try again on the next request
                _APP["error"] = str(e)[:200]
                log.exception("bins app not ready")
        return _APP["app"]


def reply(h, status, body, ctype="application/json; charset=utf-8", extra=()):
    data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=86400" if ctype.startswith("image/") else "no-store")
    for name, value in list(SECURITY_HEADERS) + list(extra):
        h.send_header(name, value)
    if ctype.startswith("text/html"):
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Content-Security-Policy", CSP)
    h.end_headers()
    h.wfile.write(data)


def client_ip(h) -> str:
    for name in ("CF-Connecting-IP", "True-Client-IP"):
        if (h.headers.get(name) or "").strip():
            return h.headers[name].strip()
    xff = [p.strip() for p in (h.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
    return xff[-1] if xff else h.client_address[0]


def base_url(h, base: str = "") -> str:
    """This app's own address, for the labels' QR codes: BINS_APP_URL when set (e.g. your own domain), otherwise
    the address the phone used (Render passes https in X-Forwarded-Proto) plus the app's place on the site."""
    fixed = (os.environ.get("BINS_APP_URL") or "").strip().rstrip("/")
    if fixed:
        return fixed
    host = (h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or "").split(",")[0].strip()
    proto = (h.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip() or "http"
    if not host or any(ch in host for ch in " /\\\"'<>"):
        return ""
    return f"{proto}://{host}{base}"


def _templated(name: str, base: str) -> bytes:
    return (HERE / name).read_text(encoding="utf-8").replace("{{BASE}}", base).encode("utf-8")


def handle_get(h, sub: str, base: str = "") -> bool:
    """GET <base><sub>: the app's page (also /b/<bin code>, a bin label's QR link), its manifest and service worker.
    Returns False when it isn't one of the app's addresses."""
    if sub in ("", "/", "/index.html") or sub.startswith("/b/"):
        reply(h, 200, _templated("bins_page.html", base), "text/html; charset=utf-8")
        return True
    if sub == "/manifest.json":
        reply(h, 200, _templated("bins_manifest.json", base), "application/manifest+json")
        return True
    if sub == "/sw.js":
        reply(h, 200, _templated("bins_sw.js", base), "text/javascript; charset=utf-8",
              [("Service-Worker-Allowed", base + "/")])
        return True
    return False


def handle_post(h, sub: str, base: str = "") -> bool:
    """POST <base>/api/...: sign in, every bin, one bin, labels. Returns False when it isn't one of them."""
    if sub not in ("/api/login", "/api/bins", "/api/bin", "/api/labels", "/api/check", "/api/inaccurate",
                   "/api/inaccurate_pdf"):
        return False
    try:
        n = int(h.headers.get("Content-Length") or 0)
        body = json.loads(h.rfile.read(min(n, 200_000)) or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        reply(h, 400, {"error": "Bad request"})
        return True
    app = get_app()
    if not app:
        reply(h, 503, {"error": "The bins app is starting. Try again in a minute."})
        return True
    try:
        if sub == "/api/login":
            reply(h, 200, app.login(body, client_ip(h)))
        elif sub == "/api/bins":
            reply(h, 200, app.bin_list(body))
        elif sub == "/api/bin":
            reply(h, 200, app.one_bin(body))
        elif sub == "/api/check":
            reply(h, 200, app.save_check(body))
        elif sub == "/api/inaccurate":
            reply(h, 200, app.inaccurate(body))
        elif sub == "/api/inaccurate_pdf":
            data, name = app.inaccurate_pdf(body)
            reply(h, 200, data, "application/pdf", [("Content-Disposition", f'inline; filename="{name}"')])
        else:
            data, name = app.labels(body, base_url(h, base))
            reply(h, 200, data, "application/pdf", [("Content-Disposition", f'inline; filename="{name}"')])
    except BinsError as e:
        reply(h, e.status, {"error": str(e)})
    except Exception:
        log.exception("bins request failed")
        reply(h, 503, {"error": "The bins can't be read right now. Try again in a minute."})
    return True


class Handler(BaseHTTPRequestHandler):
    """This app on its own website (base "")."""
    server_version = VERSION

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if handle_get(self, path):
            return
        if path in STATIC and path not in ("/manifest.json", "/sw.js"):
            f = HERE / path.lstrip("/")
            return reply(self, 200, f.read_bytes(), STATIC[path]) if f.is_file() else \
                reply(self, 404, {"error": "Not found"})
        if path == "/api/health":
            app = get_app()
            if not app:
                return reply(self, 503, {"ok": False, "stage": "setup", "error": _APP["error"]})
            try:
                app.db.table("stock_bins").select("bin_code").limit(1).execute()
                try:
                    import web_files
                    files = web_files.fingerprints(HERE)
                except Exception:
                    files = None
                return reply(self, 200, {"ok": True, "db": "ok", "version": VERSION, "files": files,
                                         "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                         "access_key": bool(ACCESS_KEY)})
            except Exception as e:
                return reply(self, 503, {"ok": False, "stage": "database", "error": str(e)[:200]})
        return reply(self, 404, {"error": "Not found"})

    def do_POST(self):
        if not handle_post(self, self.path.split("?", 1)[0]):
            reply(self, 404, {"error": "Not found"})


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT") or 8780)
    get_app()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("INVENTRA Bins listening on port %s", port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()