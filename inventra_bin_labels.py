"""Bin labels: the bin code in large type, a Code 128 barcode and a QR code, on A4 label sheets.

Scanning a label - with any USB / Bluetooth barcode scanner (it types the code and presses Enter) or a phone camera -
gives the plain bin code, so the same label works in Stock Control > Scan a bin, in Track & trace and in the phone
warehouse app (Put away / move, Count a bin, Look up).

Sizes
  sheet   21 per A4 - Avery L7160 / J8160 (63.5 x 38.1 mm), the same sheets as the serial-number labels
  shelf    8 per A4 - Avery L7165 / J8165 (99.1 x 67.7 mm), for shelf edges and bin fronts
  sign     2 per A4 - big signs for racking and floor locations, readable from a distance

Barcode and QR code are drawn by reportlab as vectors, so they stay sharp on any printer."""

from datetime import datetime
from typing import Iterable, List

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 210 * mm, 297 * mm
NAVY = (5 / 255, 43 / 255, 99 / 255)

# name: (columns, rows, label width, label height, left margin, top margin, gap across, gap down, per sheet text)
SIZES = {
    "sheet": (3, 7, 63.5 * mm, 38.1 * mm, 7.2 * mm, 15.15 * mm, 2.5 * mm, 0, "21 per A4 sheet (Avery L7160)"),
    "shelf": (2, 4, 99.1 * mm, 67.7 * mm, 4.65 * mm, 13.1 * mm, 2.5 * mm, 0, "8 per A4 sheet (Avery L7165)"),
    "sign": (1, 2, 190 * mm, 136 * mm, 10 * mm, 10 * mm, 0, 5 * mm, "2 big signs per A4 page"),
}


def per_sheet(size: str) -> int:
    cols, rows = SIZES[size][:2]
    return cols * rows


def _fit_font(c: canvas.Canvas, text: str, font: str, size: float, width: float, smallest: float = 6) -> float:
    while size > smallest and c.stringWidth(text, font, size) > width:
        size -= 0.5
    return size


def _cut(c: canvas.Canvas, text: str, font: str, size: float, width: float) -> str:
    text = str(text or "")
    while text and c.stringWidth(text, font, size) > width:
        text = text[:-1]
    return text


def _wrap(c: canvas.Canvas, text: str, font: str, size: float, width: float, lines: int) -> List[str]:
    out, cur = [], ""
    for word in str(text or "").split():
        trial = f"{cur} {word}".strip()
        if c.stringWidth(trial, font, size) <= width:
            cur = trial
            continue
        if cur:
            out.append(cur)
        cur = word
        if len(out) == lines:
            break
    if cur and len(out) < lines:
        out.append(cur)
    return [_cut(c, x, font, size, width) for x in out[:lines]]


def _qr(c: canvas.Canvas, value: str, x: float, y: float, size: float):
    w = QrCodeWidget(value, barLevel="M", barBorder=1)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def _barcode(c: canvas.Canvas, value: str, x: float, y: float, width: float, height: float):
    """Code 128 across `width` (bars no thinner than 0.25 mm, so every scanner reads it), centred."""
    probe = code128.Code128(value, barWidth=1, barHeight=height, quiet=0, humanReadable=False)
    bar = max(0.25 * mm, min(1.2 * mm, width / max(1.0, probe.width)))
    bc = code128.Code128(value, barWidth=bar, barHeight=height, quiet=0, humanReadable=False)
    bc.drawOn(c, x + max(0.0, (width - bc.width) / 2), y)


def _label(c: canvas.Canvas, x: float, y: float, w: float, h: float, b: dict, link_base: str = ""):
    code = str(b.get("code") or "").strip()
    pad = h * 0.07
    gap = h * 0.04
    bar_h = h * 0.27
    qr = h - 2 * pad - bar_h - gap                       # the QR code: the square above the barcode, on the left
    top = y + h - pad
    # the QR code opens the bins phone app on this bin (any phone camera); the barcode is the code itself (scanners)
    from inventra_bins import bin_link
    _qr(c, bin_link(link_base, code) if link_base else code, x + pad, top - qr, qr)
    tx = x + pad + qr + h * 0.05
    tw = x + w - pad - tx
    # a navy rule, then the bin code as large as it fits (no company name on bin labels)
    c.setFillColorRGB(*NAVY)
    c.rect(tx, top - h * 0.025, tw, h * 0.025, stroke=0, fill=1)
    big = _fit_font(c, code, "Helvetica-Bold", h * 0.36 / mm * 2.83, tw, smallest=7)
    c.setFillColorRGB(0, 0, 0)
    code_y = top - h * 0.025 - big * 1.02
    c.setFont("Helvetica-Bold", big)
    c.drawString(tx, code_y, _cut(c, code, "Helvetica-Bold", big, tw))
    # zone and description underneath
    line = max(5.2, h * 0.075 / mm * 2.83)
    c.setFont("Helvetica", line)
    c.setFillColorRGB(.22, .22, .28)
    ly = code_y - line * 1.45
    zone = str(b.get("zone") or "").strip()
    if zone:
        c.drawString(tx, ly, _cut(c, f"Zone {zone}", "Helvetica", line, tw))
        ly -= line * 1.2
    from inventra_bins import real_description          # ('Auto-created for …' is not printed)
    for part in _wrap(c, real_description(b.get("description")), "Helvetica", line, tw, 3):   # as many as fit
        if ly < top - qr - line * 0.1:
            break
        c.drawString(tx, ly, part)
        ly -= line * 1.2
    # the barcode across the bottom, and a small hint
    hint = max(4.4, h * 0.05 / mm * 2.83)
    _barcode(c, code, x + pad, y + pad + hint * 1.1, w - 2 * pad, bar_h - hint * 1.1)
    c.setFont("Helvetica", hint)
    c.setFillColorRGB(.42, .42, .48)
    c.drawCentredString(x + w / 2, y + pad * 0.6,
                        _cut(c, ("Scan the QR code with a phone to open this bin" if link_base else
                                 "Scan to see what is in this bin") + f" · printed {datetime.now():%d %b %Y}",
                             "Helvetica", hint, w - 2 * pad))


def bin_labels_pdf(path: str, bins: Iterable[dict], size: str = "sheet", skip: int = 0, copies: int = 1,
                   outline: bool = False, link_base: str = "") -> str:
    """bins: [{"code", "zone", "description"}]. skip: labels already used on the first sheet (start further on).
    copies: labels per bin. outline: draw the label edges (for plain paper). link_base: the bins phone app's address
    (https://...): the QR codes then open the app on their bin; without it they hold the bin code."""
    cols, rows, lw, lh, left, top, gx, gy, _text = SIZES[size]
    n = cols * rows
    c = canvas.Canvas(path, pagesize=(PAGE_W, PAGE_H))
    c.setTitle("Bin labels")
    slot = max(0, int(skip)) % n
    items = [b for b in bins if str(b.get("code") or "").strip() for _ in range(max(1, int(copies)))]
    for i, b in enumerate(items):
        if i and slot % n == 0:
            c.showPage()
        r, col = divmod(slot % n, cols)
        x = left + col * (lw + gx)
        y = PAGE_H - top - (r + 1) * lh - r * gy
        if outline or size == "sign":
            c.setStrokeColorRGB(.78, .78, .8)
            c.setLineWidth(0.6)
            c.rect(x, y, lw, lh, stroke=1, fill=0)
        _label(c, x, y, lw, lh, b, link_base)
        slot += 1
    c.save()
    return path
