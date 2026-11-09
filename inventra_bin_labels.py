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
GREY = (.25, .27, .32)          # the description
MUTED = (.45, .47, .53)         # captions

# name: (columns, rows, label width, label height, left margin, top margin, gap across, gap down, per sheet text)
SIZES = {
    "sheet": (3, 7, 63.5 * mm, 38.1 * mm, 7.2 * mm, 15.15 * mm, 2.5 * mm, 0, "21 per A4 sheet (Avery L7160)"),
    "shelf": (2, 4, 99.1 * mm, 67.7 * mm, 4.65 * mm, 13.1 * mm, 2.5 * mm, 0, "8 per A4 sheet (Avery L7165)"),
    "sign": (1, 2, 190 * mm, 136 * mm, 10 * mm, 10 * mm, 0, 5 * mm, "2 big signs per A4 page"),
}


# each label's box: (distance inside the label's edge, line width in points, corner radius)
BOX = {"sheet": (1.4 * mm, 1.8, 1.8 * mm), "shelf": (1.8 * mm, 2.2, 2.4 * mm), "sign": (3.2 * mm, 4.0, 4.5 * mm)}


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
    # a white border of 4 squares all round (the QR standard): phone camera apps - the iPhone's especially - often
    # don't read a QR code that sits closer than that to other print
    w = QrCodeWidget(value, barLevel="M", barBorder=4)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def _barcode(c: canvas.Canvas, value: str, x: float, y: float, width: float, height: float):
    """Code 128 across `width` (bars no thinner than 0.25 mm, so every scanner reads it), centred, with the blank margin
    of 10 bar widths at each end that scanners need to find where the code starts and stops. Pure black bars (dark
    grey ones read less reliably on some scanners)."""
    from reportlab.lib.colors import black
    probe = code128.Code128(value, barWidth=1, barHeight=height, quiet=0, humanReadable=False)
    bar = max(0.25 * mm, min(1.2 * mm, width / (max(1.0, probe.width) + 20)))
    bc = code128.Code128(value, barWidth=bar, barHeight=height, quiet=0, humanReadable=False, barFillColor=black)
    c.setFillColorRGB(0, 0, 0)
    bc.drawOn(c, x + max(0.0, (width - bc.width) / 2), y)


def _spaced(c: canvas.Canvas, x: float, y: float, text: str, font: str, size: float, spacing: float):
    """Small capitals with a little air between the letters (a caption)."""
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setCharSpace(spacing)
    t.textOut(text)
    t.setCharSpace(0)                    # (letter spacing stays in force in a PDF until it is set back)
    c.drawText(t)


def _label(c: canvas.Canvas, x: float, y: float, w: float, h: float, b: dict, link_base: str = ""):
    """One label: the QR code on the left; on the right a 'BIN' caption with the zone as a navy tag, the bin code as
    large as it fits (navy) and the description; across the bottom the barcode with a one-line caption."""
    code = str(b.get("code") or "").strip()
    pad = h * 0.07
    gap = h * 0.045
    foot = max(4.2, h * 0.048)                           # the caption under the barcode (points)
    bar_h = h * 0.20
    qr = h - 2 * pad - bar_h - foot * 1.5 - gap          # the QR code: the square above the barcode, on the left
    top = y + h - pad
    # the QR code opens the bins phone app on this bin (any phone camera); the barcode is the code itself (scanners)
    from inventra_bins import bin_link
    _qr(c, bin_link(link_base, code) if link_base else code, x + pad, top - qr, qr)
    tx = x + pad + qr + h * 0.045
    tw = x + w - pad - tx
    text_bottom = top - qr                               # the text stays beside the QR code
    # 1) the caption row: BIN on the left, the zone as a navy tag on the right
    cap = max(4.6, h * 0.062)
    cap_y = top - cap * 0.95
    c.setFillColorRGB(*MUTED)
    _spaced(c, tx, cap_y, "BIN", "Helvetica-Bold", cap, cap * 0.22)
    zone = str(b.get("zone") or "").strip().upper()
    if zone:
        room = tw - c.stringWidth("BIN", "Helvetica-Bold", cap) - cap * 2.2
        zone = _cut(c, zone, "Helvetica-Bold", cap, room - cap * 1.2)
        if zone:
            zw = c.stringWidth(zone, "Helvetica-Bold", cap) + cap * 1.2
            c.setFillColorRGB(*NAVY)
            c.roundRect(tx + tw - zw, cap_y - cap * 0.38, zw, cap * 1.45, cap * 0.35, stroke=0, fill=1)
            c.setFillColorRGB(1, 1, 1)
            c.setFont("Helvetica-Bold", cap)
            c.drawString(tx + tw - zw + cap * 0.6, cap_y, zone)
    # 2) the bin code, as large as fits the width and the room between the caption and the description
    from inventra_bins import real_description          # ('Auto-created for …' is not printed)
    desc = real_description(b.get("description"))
    line = max(5.0, h * 0.07)
    room_h = (cap_y - cap * 0.6) - text_bottom - (line * 1.25 * (2 if desc else 0))
    big = _fit_font(c, code, "Helvetica-Bold", max(7.0, min(h * 0.34, room_h / 0.9)), tw, smallest=7)
    code_y = cap_y - cap * 0.6 - big * 0.80
    c.setFillColorRGB(*NAVY)
    c.setFont("Helvetica-Bold", big)
    c.drawString(tx, code_y, _cut(c, code, "Helvetica-Bold", big, tw))
    # 3) the description underneath, on as many lines as fit beside the QR code
    if desc:
        c.setFont("Helvetica", line)
        c.setFillColorRGB(*GREY)
        ly = code_y - big * 0.24 - line * 1.15
        for part in _wrap(c, desc, "Helvetica", line, tw, 3):
            if ly < text_bottom:
                break
            c.drawString(tx, ly, part)
            ly -= line * 1.2
    # 4) the barcode across the bottom, and its caption: how to scan on the left, the print date on the right
    by = y + pad + foot * 1.5
    _barcode(c, code, x + pad, by, w - 2 * pad, bar_h)
    c.setFont("Helvetica", foot)
    c.setFillColorRGB(*MUTED)
    hint = "Scan with a phone camera" if link_base else "Scan to see what is in this bin"
    date = f"{datetime.now():%d %b %Y}"
    c.drawString(x + pad, y + pad * 0.9, _cut(c, hint, "Helvetica", foot, w - 2 * pad - c.stringWidth(date, "Helvetica",
                                                                                                          foot) - foot))
    c.drawRightString(x + w - pad, y + pad * 0.9, date)


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
        if outline:                              # (plain paper: where to cut)
            c.setStrokeColorRGB(.85, .85, .87)
            c.setLineWidth(0.4)
            c.rect(x, y, lw, lh, stroke=1, fill=0)
        # every label in a box of its own (the same on every label: uniform rows on the shelf). The box sits a little
        # inside the label's edge, so a sheet that feeds slightly off-centre still shows the whole box, and the
        # label's content is laid out inside the box
        inset, line, radius = BOX[size]
        c.setStrokeColorRGB(0, 0, 0)            # pure black: shows clearly on every printer, even when toner is low
        c.setLineWidth(line)
        c.roundRect(x + inset, y + inset, lw - 2 * inset, lh - 2 * inset, radius, stroke=1, fill=0)
        _label(c, x + inset, y + inset, lw - 2 * inset, lh - 2 * inset, b, link_base)
        slot += 1
    c.save()
    return path
