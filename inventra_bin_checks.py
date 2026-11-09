"""Bin checks: someone at the bin says whether it is 100% correct, or what is different - and the list of the bins
that are not, to print.

Used by the bins phone app (inventra-bins-online: "This bin is 100% correct" / "Report a discrepancy") and by
Stock Control > Bin checks on the desktop. Copied into the online apps by sync_web_files.py.

  expected(tables, code)     what should be in the bin now: per product the quantity and, for products tracked by
                             serial number, the serials (inventra_bin_report's figures)
  make_check(...)            the record to save (table bin_checks): every product's expected and counted quantity,
                             the serials missing and the ones found that shouldn't be there, anything else found
  inaccurate(checks)         the bins whose latest check found a difference that hasn't been resolved
  report_pdf(...)            the list of those bins to print: with quantities and serials, or without them (a recount
                             sheet: the bins and products only, with a box to write the count in)
"""

import io
import json
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

TABLE = "bin_checks"
ACCURATE, DISCREPANCY = "accurate", "discrepancy"
OPEN, RESOLVED = "open", "resolved"
_SERIAL_NOTE = re.compile(r"serial\s+([^\s·,;]+)", re.I)


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _key(serial) -> str:
    return "".join(str(serial).split()).upper()


def _serials(value) -> List[str]:
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    t = str(value or "").strip()
    if not t:
        return []
    try:
        got = json.loads(t)
        if isinstance(got, list):
            return [str(s).strip() for s in got if str(s).strip()]
    except ValueError:
        pass
    return [s.strip() for s in re.split(r"[,;\n]", t) if s.strip()]


def serials_in_bin(tables: Dict[str, List[dict]], code: str, sku: str, on_hand_keys) -> List[str]:
    """The serial numbers of `sku` that should be in bin `code`: received into the bin (Procurement > Receive) or moved
    into it on the warehouse phone page (the move notes the serial), less those moved out - and only serials still on
    hand (not dispatched, returned or written off)."""
    want, here = str(code).strip().upper(), {}
    for s in tables.get("stock_items", []):
        if str(s.get("sku")) == sku and str(s.get("bin_code") or "").strip().upper() == want:
            for x in _serials(s.get("serial_numbers") or s.get("serials")):
                here[_key(x)] = x
    for m in sorted(tables.get("stock_bin_moves", []), key=lambda m: str(m.get("created_at") or "")):
        if str(m.get("sku")) != sku or str(m.get("bin_code") or "").strip().upper() != want:
            continue
        found = _SERIAL_NOTE.search(str(m.get("note") or ""))
        if not found:
            continue
        x = found.group(1)
        if _num(m.get("qty")) > 0:
            here[_key(x)] = x
        else:
            here.pop(_key(x), None)
    return sorted((x for k, x in here.items() if k in on_hand_keys), key=str.upper)


def expected(tables: Dict[str, List[dict]], code: str) -> Optional[dict]:
    """{"bin": {...}, "lines": [{sku, description, expected, serialized, serials}]} - what the check compares with -
    or None when there is no such bin."""
    import inventra_bin_report
    r = inventra_bin_report.report(tables, code)
    if r is None:
        return None
    serialized = {str(p.get("sku")) for p in tables.get("products", []) if p.get("serialized")}
    holdings = inventra_bin_report._context(tables)["holdings"]
    lines = []
    for p in r["products"]:
        if p["available"] <= 1e-9:
            continue
        sku = str(p["sku"])
        keys = set(holdings[sku].serials) if sku in holdings else set()
        tracked = sku in serialized or bool(keys)
        lines.append({"sku": sku, "description": p["description"], "expected": round(p["available"], 4),
                      "serialized": tracked,
                      "serials": serials_in_bin(tables, r["bin"]["code"], sku, keys) if tracked else []})
    return {"bin": r["bin"], "lines": lines}


def make_check(exp: dict, counts: Dict[str, dict], who: str, who_name: str, note: str = "", all_correct: bool = False,
               extra: str = "", source: str = "phone") -> dict:
    """The bin_checks row. counts: {sku: {"counted": n, "found_serials": [...], "extra_serials": [...]}} (ignored when
    all_correct: everything as expected). Any difference makes it a discrepancy."""
    rows, differs = [], False
    for ln in exp["lines"]:
        c = counts.get(ln["sku"], {}) if not all_correct else {}
        counted = round(_num(c.get("counted")), 4) if not all_correct and c.get("counted") not in (None, "") \
            else ln["expected"]
        found = set(_key(s) for s in (c.get("found_serials") if "found_serials" in c else ln["serials"]))
        missing = [s for s in ln["serials"] if _key(s) not in found] if not all_correct else []
        extra_s = [s for s in (c.get("extra_serials") or []) if str(s).strip()] if not all_correct else []
        diff = round(counted - ln["expected"], 4)
        if diff or missing or extra_s:
            differs = True
        rows.append({"sku": ln["sku"], "description": ln["description"], "expected": ln["expected"],
                     "counted": counted, "difference": diff, "serialized": ln["serialized"],
                     "missing_serials": missing, "extra_serials": extra_s})
    extra = str(extra or "").strip()[:500]
    if extra and not all_correct:
        differs = True
    note = str(note or "").strip()[:500]
    if not all_correct and not differs and not note:
        raise ValueError("Nothing is different: tap \"This bin is 100% correct\", or say what is wrong.")
    now = datetime.now().isoformat(timespec="seconds")
    return {"check_id": "BC-" + uuid.uuid4().hex[:10].upper(), "bin_code": exp["bin"]["code"],
            "result": DISCREPANCY if (differs or (note and not all_correct)) else ACCURATE,
            "note": note or None, "extra_found": extra or None, "lines": rows, "checked_by": who,
            "checked_by_name": who_name or who, "checked_at": now, "source": source,
            "status": OPEN if (differs or (note and not all_correct)) else RESOLVED}


def lines_of(check: dict) -> List[dict]:
    raw = check.get("lines")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    return [r for r in (raw or []) if isinstance(r, dict)]


def latest_per_bin(checks: List[dict]) -> Dict[str, dict]:
    out = {}
    for c in sorted(checks, key=lambda c: str(c.get("checked_at") or "")):
        out[str(c.get("bin_code") or "").upper()] = c
    return out


def inaccurate(checks: List[dict]) -> List[dict]:
    """The bins whose latest check found a difference not resolved yet, most recent first."""
    out = [c for c in latest_per_bin(checks).values() if c.get("result") == DISCREPANCY and c.get("status") != RESOLVED]
    return sorted(out, key=lambda c: str(c.get("checked_at") or ""), reverse=True)


def summary(check: dict) -> str:
    """'2 products differ · 1 serial missing' - for lists."""
    rows = lines_of(check)
    differ = sum(1 for r in rows if r.get("difference"))
    missing = sum(len(r.get("missing_serials") or []) for r in rows)
    extra = sum(len(r.get("extra_serials") or []) for r in rows)
    parts = [f"{differ} product{'s differ' if differ != 1 else ' differs'}" if differ else "",
             f"{missing} serial{'s' if missing != 1 else ''} missing" if missing else "",
             f"{extra} serial{'s' if extra != 1 else ''} not expected" if extra else "",
             "something else found" if check.get("extra_found") else ""]
    return " · ".join(p for p in parts if p) or (check.get("note") or "Reported as not correct")


def _fmt(v) -> str:
    v = _num(v)
    return f"{v:g}" if v == int(v) else f"{v:.2f}"


def report_pdf(checks: List[dict], out=None, detailed: bool = True, company: str = "", zones: Dict[str, str] = None):
    """The inaccurate bins as a PDF (bytes, or written to `out`: a path or a file object).
    detailed: the expected and counted quantities and the serial numbers missing / not expected.
    Not detailed: a recount sheet - the bins and their products only, with a box to write the count in."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    navy, line, muted = colors.HexColor("#052B63"), colors.HexColor("#D9DFE8"), colors.HexColor("#5B6474")
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=8.6, leading=11)
    small = ParagraphStyle("s", parent=body, fontSize=7.6, leading=9.5, textColor=muted)
    head = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=navy)
    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=navy)
    esc = lambda t: str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")   # noqa: E731
    zones = zones or {}
    buf = out if out is not None else io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title="Inaccurate bins")
    W = A4[0] - 28 * mm
    story = [Paragraph("Inaccurate bins" + ("" if detailed else " · recount sheet"), title),
             Paragraph(esc(" · ".join(x for x in (company, f"{len(checks)} bin{'s' if len(checks) != 1 else ''}",
                                                  f"printed {datetime.now():%d %b %Y %H:%M}") if x)), small),
             Spacer(1, 5 * mm)]
    if not detailed:
        story.append(Paragraph("Count each product in the bin and write the number in the box. Quantities and serial "
                               "numbers are left out on purpose: count what is really there.", small))
        story.append(Spacer(1, 4 * mm))
    if not checks:
        story.append(Paragraph("Every bin checked is correct: there is nothing to recount.", body))
    for c in checks:
        code = str(c.get("bin_code") or "")
        when = str(c.get("checked_at") or "")[:16].replace("T", " ")
        bits = [f"Zone {zones.get(code.upper())}" if zones.get(code.upper()) else "",
                f"checked {when} by {c.get('checked_by_name') or c.get('checked_by') or ''}"]
        block = [Paragraph(f"Bin {esc(code)}", head), Paragraph(esc(" · ".join(b for b in bits if b)), small)]
        if c.get("note"):
            block.append(Paragraph("Note: " + esc(c["note"]), body))
        if c.get("extra_found"):
            block.append(Paragraph("Also found in the bin: " + esc(c["extra_found"]), body))
        rows = lines_of(c)
        if detailed:
            data = [["Product", "SKU", "Expected", "Counted", "Difference", "Serial numbers"]]
            for r in rows:
                serial_text = []
                if r.get("missing_serials"):
                    serial_text.append("<b>Missing:</b> " + esc(", ".join(r["missing_serials"])))
                if r.get("extra_serials"):
                    serial_text.append("<b>Not expected:</b> " + esc(", ".join(r["extra_serials"])))
                diff = _num(r.get("difference"))
                data.append([Paragraph(esc(r.get("description")), body), Paragraph(esc(r.get("sku")), small),
                             _fmt(r.get("expected")), _fmt(r.get("counted")),
                             ("+" if diff > 0 else "") + _fmt(diff) if diff else "—",
                             Paragraph("<br/>".join(serial_text) or ("—" if not r.get("serialized") else "All there"),
                                       body)])
            widths = [W * 0.27, W * 0.17, W * 0.09, W * 0.09, W * 0.1, W * 0.28]
        else:
            data = [["Product", "SKU", "Counted"]]
            for r in rows:
                data.append([Paragraph(esc(r.get("description")), body), Paragraph(esc(r.get("sku")), small), ""])
            data.append([Paragraph("<i>Anything else in the bin</i>", small), "", ""])
            widths = [W * 0.55, W * 0.27, W * 0.18]
        if len(data) > 1:
            t = Table(data, colWidths=widths, repeatRows=1)
            style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                     ("BACKGROUND", (0, 0), (-1, 0), navy), ("FONT", (0, 1), (-1, -1), "Helvetica", 8.6),
                     ("GRID", (0, 0), (-1, -1), 0.5, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
            if detailed:
                style.append(("ALIGN", (2, 0), (4, -1), "RIGHT"))
            else:
                style += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FC")]),
                          ("BOTTOMPADDING", (0, 1), (-1, -1), 9), ("TOPPADDING", (0, 1), (-1, -1), 9)]
            t.setStyle(TableStyle(style))
            block += [Spacer(1, 2 * mm), t]
        block.append(Spacer(1, 7 * mm))
        story.append(KeepTogether(block))
    doc.build(story)
    return None if out is not None else buf.getvalue()
