"""Storage bins shared by Stock Control and the Production Planner.

One list of bins (table stock_bins, plus bins created in Procurement's own list, table bins) and three places where
stock is put in them:
  * deliveries        - stock_items.bin_code: the bin a delivery was received into (Procurement > Receive)
  * production runs   - production_runs.allocations: where a run's good units went
                        [{"bin": "A-60", "qty": 8}]
  * the bin ledger    - stock_bin_moves: any stock on hand put into a bin from
                        Stock Control (qty > 0), taken out or moved (qty < 0)

What is in a bin = everything put there, minus what was taken out, minus units
that have since left the warehouse (dispatched, written off ... - the OLDEST
leave first, so the bins add up to the stock really on hand).
'Unallocated' = stock on hand that is not in any bin yet.

Pure functions plus BinRepoMixin (add it to a module's repository)."""

import json
from typing import Callable, Dict, List

BINS_TABLE = "stock_bins"
LEGACY_BINS_TABLE = "bins"                    # Procurement's list (Receive creates a bin there when a new code is typed)
LEDGER_TABLE = "stock_bin_moves"
RUNS_TABLE = "production_runs"
RECEIPTS_TABLE = "stock_items"
UNALLOCATED = "Unallocated"


def num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_missing(e: Exception) -> bool:
    t = str(e).lower()
    return "does not exist" in t or "pgrst205" in t or "42p01" in t or "could not find the table" in t


def allocations_of(run: dict) -> List[dict]:
    """The run's put-away list (jsonb may come back as text)."""
    raw = run.get("allocations")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    return [{"bin": str(a.get("bin")), "qty": num(a.get("qty"))}
            for a in (raw or []) if isinstance(a, dict) and a.get("bin") and num(a.get("qty")) > 0]


# ---- new production: made in house, checked by quality, then put away ----
# A production run's good units go into the New production bin, never straight onto a shelf with older stock. Quality
# checks the run (passed / rejected units); only then can the passed units be put away into a storage bin.
NEW_PROD = "NEW-PROD"
NEW_PROD_BIN = {"bin_code": NEW_PROD, "zone": "Production",
                "description": "New production: waiting for the quality check and put-away", "active": True}
QA_PENDING, QA_PASSED, QA_PARTIAL, QA_FAILED = "pending", "passed", "partial", "failed"
QA_COLUMNS = ["qa_status", "qa_passed_qty", "qa_rejected_qty", "qa_by", "qa_at", "qa_notes"]
QA_LABELS = {QA_PENDING: "Waiting for quality check", QA_PASSED: "Passed", QA_PARTIAL: "Passed in part",
             QA_FAILED: "Rejected"}


def new_prod_qty(run: dict) -> float:
    """The run's units put in the New production bin."""
    return sum(a["qty"] for a in allocations_of(run) if a["bin"] == NEW_PROD)


def qa_status(run: dict) -> str:
    """'pending' until quality has checked a run that went into the New production bin; '' for older runs (put
    straight into bins before the check existed)."""
    s = str(run.get("qa_status") or "").strip().lower()
    if s in QA_LABELS:
        return s
    return QA_PENDING if new_prod_qty(run) > 0 else ""


def quality_result(good: float, passed: float, rejected: float) -> str:
    if rejected <= 1e-9:
        return QA_PASSED
    return QA_FAILED if passed <= 1e-9 else QA_PARTIAL


def checked_run(run: dict, passed: float, rejected: float, by: str, notes: str, at: str) -> dict:
    """The run row to save after the quality check. Rejected units are scrap: they come off the good units (and so off
    stock) and out of the New production bin; the passed units wait there to be put away."""
    good = num(run.get("good_qty"))
    if passed < 0 or rejected < 0:
        raise ValueError("Units can't be less than 0.")
    if abs(passed + rejected - good) > 1e-6:
        raise ValueError(f"Passed plus rejected must be the {good:g} good units the run made.")
    if rejected > 1e-9 and not str(notes or "").strip():
        raise ValueError("Say why units were rejected.")
    allocs = allocations_of(run)
    left = rejected
    for a in allocs:                                   # the rejects leave the New production bin
        if a["bin"] == NEW_PROD and left > 0:
            take = min(a["qty"], left)
            a["qty"] -= take
            left -= take
    return {"run_id": run["run_id"], "good_qty": round(good - rejected, 4),
            "scrap_qty": round(num(run.get("scrap_qty")) + rejected, 4),
            "allocations": [{"bin": a["bin"], "qty": round(a["qty"], 4)} for a in allocs if a["qty"] > 1e-9],
            "qa_status": quality_result(good, passed, rejected), "qa_passed_qty": round(passed, 4),
            "qa_rejected_qty": round(rejected, 4), "qa_by": by or None, "qa_at": at,
            "qa_notes": str(notes or "").strip() or None}


def new_production(cont: List[dict], runs: List[dict]) -> dict:
    """What is in the New production bin and what may be put away.
    Returns {"runs": [run rows, newest first, with _status, _left (units still in the bin), _label],
             "awaiting": {sku: units waiting for the quality check},
             "ready": {sku: units passed and waiting to be put away}}.
    Units not yet checked are counted first: whatever left the bin (put away, dispatched) can only have been checked
    units, so unchecked units are never offered for put-away."""
    in_bin: Dict[str, float] = {}
    for c in cont:
        if c["bin"] == NEW_PROD:
            in_bin[c["sku"]] = in_bin.get(c["sku"], 0.0) + c["qty"]
    mine = [r for r in runs if r.get("product_sku") and (new_prod_qty(r) > 0 or qa_status(r) == QA_FAILED
                                                         and r.get("qa_rejected_qty"))]
    mine.sort(key=lambda r: (str(r.get("run_date") or "")[:10], str(r.get("run_id") or "")), reverse=True)
    left = dict(in_bin)
    here: Dict[str, float] = {}
    for first in (True, False):                     # unchecked runs first, then the checked ones (newest first)
        for r in mine:
            if (qa_status(r) == QA_PENDING) == first:
                sku = r["product_sku"]
                here[r["run_id"]] = q = min(new_prod_qty(r), max(0.0, left.get(sku, 0.0)))
                left[sku] = left.get(sku, 0.0) - q
    awaiting: Dict[str, float] = {}
    ready: Dict[str, float] = {}
    out = []
    for r in mine:
        sku, status, q = r["product_sku"], qa_status(r), here.get(r["run_id"], 0.0)
        if status == QA_PENDING:
            awaiting[sku] = awaiting.get(sku, 0.0) + q
            label = "Waiting for quality check"
        elif q > 1e-9:
            ready[sku] = ready.get(sku, 0.0) + q
            label = "Passed · put away" if status == QA_PASSED else "Passed in part · put away"
        else:
            label = "Rejected" if status == QA_FAILED else "Put away"
        out.append(dict(r, _status=status, _left=q, _label=label))
    return {"runs": out, "awaiting": {k: v for k, v in awaiting.items() if v > 1e-9},
            "ready": {k: v for k, v in ready.items() if v > 1e-9}}


def receipts_into_bins(stock_items: List[dict]) -> List[dict]:
    """Deliveries received straight into a bin (stock_items with a bin code): {sku, bin, qty, date, invoice, supplier}.
    (Receive used to record the bin only on the delivery, so that stock showed as 'not in a bin' in Stock Control.)"""
    out = []
    for s in stock_items or []:
        code = str(s.get("bin_code") or "").strip()
        q = num(s.get("quantity"))
        if code and q > 0 and s.get("sku") and (s.get("product_type") or "inventory") == "inventory":
            out.append({"sku": str(s["sku"]), "bin": code, "qty": q,
                        "date": str(s.get("received_date") or s.get("created_at") or "")[:10],
                        "invoice": s.get("invoice_number") or "", "supplier": s.get("supplier_name") or ""})
    return out


def _entries(runs: List[dict], ledger: List[dict], receipts: List[dict] = ()) -> List[dict]:
    ents = []
    for r in receipts_into_bins(receipts):
        ents.append({"sku": r["sku"], "bin": r["bin"], "qty": r["qty"], "key": (r["date"], 0, str(r["invoice"]))})
    for r in runs:
        for a in allocations_of(r):
            ents.append({"sku": r.get("product_sku"), "bin": a["bin"], "qty": a["qty"],
                         "key": (str(r.get("run_date") or "")[:10], 0, str(r.get("run_id") or ""))})
    for m in ledger:
        if m.get("sku") and m.get("bin_code") and num(m.get("qty")):
            ents.append({"sku": m["sku"], "bin": m["bin_code"], "qty": num(m["qty"]),
                         "key": (str(m.get("created_at") or "")[:10], 1, str(m.get("created_at") or ""))})
    return sorted(ents, key=lambda e: e["key"])


def contents(runs: List[dict], ledger: List[dict], stock_qty: Callable[[str], float],
             skus=(), receipts: List[dict] = ()) -> List[dict]:
    """[{bin, sku, qty}] for every bin holding stock, plus 'Unallocated' rows.
    stock_qty(sku) = units really on hand; skus = extra SKUs to check for unallocated stock;
    receipts = stock_items rows (deliveries received into a bin)."""
    held: Dict[tuple, List[dict]] = {}
    for e in _entries(runs, ledger, receipts):
        if e["qty"] > 0:
            held.setdefault((e["sku"], e["bin"]), []).append({"qty": e["qty"], "key": e["key"]})
        else:                                           # taken out of this bin: oldest goes first
            need = -e["qty"]
            for p in held.get((e["sku"], e["bin"]), []):
                take = min(p["qty"], need)
                p["qty"] -= take
                need -= take
                if need <= 1e-9:
                    break
    by_sku: Dict[str, List[tuple]] = {}
    for (sku, b), parts in held.items():
        by_sku.setdefault(sku, []).extend((p["key"], b, p) for p in parts if p["qty"] > 1e-9)
    out = []
    for sku in set(by_sku) | set(skus):
        parts = sorted(by_sku.get(sku, []), key=lambda t: t[0])
        have = max(0.0, stock_qty(sku))
        excess = sum(p["qty"] for _k, _b, p in parts) - have
        for _k, _b, p in parts:                          # units that left the warehouse: oldest first
            if excess > 1e-9:
                take = min(p["qty"], excess)
                p["qty"] -= take
                excess -= take
        merged: Dict[str, float] = {}
        for _k, b, p in parts:
            if p["qty"] > 1e-9:
                merged[b] = merged.get(b, 0.0) + p["qty"]
        free = have - sum(merged.values())
        if free > 1e-9:
            merged[UNALLOCATED] = free
        out += [{"bin": b, "sku": sku, "qty": q} for b, q in merged.items()]
    return sorted(out, key=lambda r: (r["bin"] == UNALLOCATED, r["bin"], r["sku"]))


def bin_link(base_url: str, code: str) -> str:
    """The address a bin label's QR code opens: the bins phone app on that bin (https://<app>/b/<code>)."""
    from urllib.parse import quote
    return f"{str(base_url).strip().rstrip('/')}/b/{quote(str(code).strip(), safe='')}"


def bin_code_from_scan(text) -> str:
    """What was scanned -> the bin code. Bin-label QR codes hold the app's link (https://<app>/b/A-01-02) and the
    barcode under it the code itself; both give 'A-01-02'."""
    from urllib.parse import unquote, urlparse
    t = str(text or "").strip()
    if "/b/" in t and ("://" in t or t.startswith("/")):
        path = urlparse(t).path if "://" in t else t
        part = path.split("/b/", 1)[1].split("/", 1)[0]
        return unquote(part).strip()
    return t


def real_description(text) -> str:
    """A bin's description as people wrote it: Procurement's own note on bins it created when a new code was typed
    at Receive ('Auto-created for A-2') is not a description, and is not shown or printed."""
    t = str(text or "").strip()
    return "" if t.lower().startswith("auto-created") else t


def merge_bin_lists(bins: List[dict], legacy: List[dict] = ()) -> List[dict]:
    """Stock Control's bins plus the bins only Procurement's list has (created by Receive), in one list."""
    have = {str(b.get("bin_code") or "").upper() for b in bins}
    out = [dict(b, description=real_description(b.get("description")) or None) for b in bins]
    for b in legacy or []:
        code = str(b.get("bin_code") or "").strip()
        if code and code.upper() not in have:
            have.add(code.upper())
            out.append({"bin_code": code, "zone": None, "description": real_description(b.get("description")) or None,
                        "active": str(b.get("status") or "Active").lower() != "inactive"})
    return out


def build_state(bins: List[dict], runs: List[dict], ledger: List[dict], stock_qty: Callable[[str], float],
                names: Dict[str, str], receipts: List[dict] = ()) -> dict:
    """Everything the bin screens need."""
    cont = contents(runs, ledger, stock_qty, skus=names.keys(), receipts=receipts)
    if not any(str(b.get("bin_code") or "").upper() == NEW_PROD for b in bins):
        bins = list(bins) + [dict(NEW_PROD_BIN)]          # (made in the database with the first run that uses it)
    return {"bins": bins, "contents": cont, "names": names,
            "unallocated": {c["sku"]: c["qty"] for c in cont if c["bin"] == UNALLOCATED},
            "active": sorted((b for b in bins if b.get("active") is not False), key=lambda b: b.get("bin_code") or ""),
            "new_prod": new_production(cont, runs)}


def storage_bins(state: dict) -> List[dict]:
    """The bins stock may be put into: every active bin except the New production bin (new units only go there when a
    run is recorded, so they are never mixed with older stock)."""
    return [b for b in state["active"] if b.get("bin_code") != NEW_PROD]


def units_in(state: dict, code: str) -> float:
    return sum(c["qty"] for c in state["contents"] if c["bin"] == code)


def where_is(state: dict, sku: str) -> str:
    """'A-60: 8, Unallocated: 2' for one product."""
    return ", ".join(f"{c['bin']}: {c['qty']:g}" for c in state["contents"] if c["sku"] == sku)


class BinRepoMixin:
    """Add to a repository class (needs self._t(table))."""

    def _safe_rows(self, table, select="*"):
        try:
            return self._t(table).select(select).execute().data or []
        except Exception as e:
            if _is_missing(e):
                return []
            raise

    def bin_rows(self):
        return merge_bin_lists(self._safe_rows(BINS_TABLE), self._safe_rows(LEGACY_BINS_TABLE))

    def bin_ledger(self):
        return self._safe_rows(LEDGER_TABLE)

    def bin_runs(self):
        return self._safe_rows(RUNS_TABLE)          # (all columns: the quality check's are there once the update ran)

    def bin_receipts(self):
        return self._safe_rows(RECEIPTS_TABLE, "sku, quantity, bin_code, received_date, created_at, product_type, "
                                               "invoice_number, supplier_name")

    def bin_state(self, stock_qty: Callable[[str], float], names: Dict[str, str]) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:                 # the four lists, side by side
            rows, runs, ledger, receipts = pool.map(lambda f: f(), (self.bin_rows, self.bin_runs, self.bin_ledger,
                                                                    self.bin_receipts))
        return build_state(rows, runs, ledger, stock_qty, names, receipts)

    def save_bin(self, row: dict):
        from datetime import datetime
        self._t(BINS_TABLE).upsert(dict(row, updated_at=datetime.now().isoformat(timespec="seconds")),
                                   on_conflict="bin_code").execute()

    def delete_bin(self, code: str):
        self._t(BINS_TABLE).delete().eq("bin_code", code).execute()

    def add_bin_moves(self, rows: List[dict]):
        """rows: [{"sku", "bin_code", "qty" (+ in / − out), "note"}]"""
        if rows:
            self._t(LEDGER_TABLE).insert(rows).execute()

    def ensure_new_prod_bin(self):
        """The New production bin, made the first time a run needs it."""
        try:
            got = self._t(BINS_TABLE).select("bin_code").eq("bin_code", NEW_PROD).execute().data
        except Exception as e:
            if _is_missing(e):
                return
            raise
        if not got:
            self.save_bin(dict(NEW_PROD_BIN))

    def record_quality(self, run: dict, passed: float, rejected: float, by: str, notes: str):
        """Save the quality check of a run (checked_run: the rejected units become scrap)."""
        from datetime import datetime
        gone = self.missing_columns(RUNS_TABLE, QA_COLUMNS) if hasattr(self, "missing_columns") else []
        if gone:
            raise RuntimeError("The database can't keep quality checks yet: click Fix database access (Home > "
                               "Settings), then try again.")
        row = checked_run(run, passed, rejected, by, notes, datetime.now().isoformat(timespec="seconds"))
        row["updated_at"] = row["qa_at"]
        self._t(RUNS_TABLE).update({k: v for k, v in row.items() if k != "run_id"}).eq("run_id", row["run_id"]).execute()

    def put_away(self, items: List[dict], code: str, new_bin: dict = None):
        """Move checked new production from the New production bin into a storage bin.
        items: [{"sku", "qty"}]; new_bin: a bin to create first (Stock Control only)."""
        if new_bin:
            self.save_bin(new_bin)
        moves = []
        for it in items:
            q = round(num(it["qty"]), 4)
            if q > 0:
                moves += [{"sku": it["sku"], "bin_code": NEW_PROD, "qty": -q, "note": f"Put away to {code} (checked new production)"},
                          {"sku": it["sku"], "bin_code": code, "qty": q, "note": "Put away from new production (passed quality check)"}]
        self.add_bin_moves(moves)
