"""Unseen invoices: layouts and quirks the system was never tuned on.

Writes test_invoices/unseen/. These have no cached AI results and no expected
outcomes: they exist to see how the live system copes with something new.

  u1  minimal layout, real rupee signs, dotted date, "Order Reference"   Northwind / PO-1008
  u2  phone photo of a printed invoice (skewed, uneven light)              Blue River / PO-1007
  u3  two pages: header and terms on page 1, lines and totals on page 2   Shree Ganesh / PO-1009
  u4  Indian GST column layout (taxable, CGST, SGST per line) + HSN table Deccan / PO-1010
  u5  PO written without its prefix ("P.O. No: 1011"), US-style date      Apex / PO-1011
  u6  a quotation, not an invoice (no invoice number)                     Apex / PO-1006
  u7  billed in US dollars                                               Northwind / PO-1002

Run:  python scripts/generate_unseen.py
"""

import io
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageEnhance, ImageFilter
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

rl_config.invariant = 1
pdfmetrics.registerFont(TTFont("AU", "/System/Library/Fonts/Geneva.ttf"))
pdfmetrics.registerFont(TTFont("TB", "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf"))

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test_invoices" / "unseen"
V = {v["vendor_id"]: v for v in json.loads((ROOT / "data" / "vendors.json").read_text())}
BUYER = json.loads((ROOT / "data" / "config.json").read_text())["buyer"]


def S(name, **kw):
    base = dict(fontName="AU", fontSize=9, leading=12)
    base.update(kw)
    return ParagraphStyle(name, **base)


def rs(x: float, sym: str = "₹") -> str:
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{sym}{whole}.{frac}"


def doc(path, story, **kw):
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=16 * mm, **kw).build(story)


# ---------------------------------------------------------------- u1 minimal, rupee signs

def u1(path):
    v = V["V-003"]
    qty, price = 30, 4800
    sub = qty * price
    igst = sub * 0.18
    story = [
        Paragraph("Northwind Electronics LLP", S("h", fontName="TB", fontSize=14, leading=18)),
        Paragraph(f"No. 88, 3rd Cross, Koramangala, Bengaluru 560034 · GSTIN {v['gstin']}", S("a", fontSize=8, textColor=colors.grey)),
        Spacer(1, 10 * mm),
        Table([["Invoice", "NW/INV/2026-27/0077"], ["Date", "02.10.2026"], ["Order Reference", "PO-1008"],
               ["Customer", BUYER["name"]], ["Customer GSTIN", BUYER["gstin"]]],
              colWidths=[40 * mm, 80 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 9),
                                                    ("TEXTCOLOR", (0, 0), (0, -1), colors.grey)]),
        Spacer(1, 8 * mm),
        Table([["Description", "Quantity", "Price", "Amount"],
               ["Jabra Evolve2 30 headset (wired, USB-A)", f"{qty} pcs", rs(price), rs(sub)]],
              colWidths=[90 * mm, 25 * mm, 28 * mm, 31 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("TEXTCOLOR", (0, 0), (-1, 0), colors.grey),
                     ("LINEBELOW", (0, 0), (-1, 0), 0.3, colors.grey), ("ALIGN", (1, 0), (-1, -1), "RIGHT")]),
        Spacer(1, 6 * mm),
        Table([["Subtotal", rs(sub)], ["IGST 18%", rs(igst)], ["Total due", rs(sub + igst)]],
              colWidths=[40 * mm, 35 * mm], hAlign="RIGHT",
              style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, -1), (-1, -1), "AU", 11),
                     ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black)]),
        Spacer(1, 14 * mm),
        Paragraph(f"Pay to {v['bank']['bank_name']} · {v['bank']['account_number']} · {v['bank']['ifsc']}", S("p", fontSize=8)),
        Paragraph("Thank you for your business.", S("t", fontSize=8, textColor=colors.grey)),
    ]
    doc(path, story, title="Invoice")


# ---------------------------------------------------------------- u2 phone photo

def u2(path):
    v = V["V-005"]
    qty, price = 5, 4500
    sub = qty * price
    igst = sub * 0.18
    tmp = io.BytesIO()
    story = [
        Paragraph("BLUE RIVER LOGISTICS PVT LTD", S("h", fontName="TB", fontSize=15, leading=19, alignment=TA_CENTER)),
        Paragraph(f"B-27, Okhla Industrial Area Phase II, New Delhi 110020 | GSTIN: {v['gstin']}", S("a", fontSize=8, alignment=TA_CENTER)),
        Spacer(1, 6 * mm),
        Paragraph("TAX INVOICE", S("ti", fontName="TB", fontSize=12, alignment=TA_CENTER)),
        Spacer(1, 4 * mm),
        Table([[f"Invoice No: BRL/PNQ/2026/0340", "Date: 01/10/2026"], [f"PO Ref: PO-1007", f"Bill To: {BUYER['name']}"]],
              colWidths=[85 * mm, 85 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 10)]),
        Spacer(1, 5 * mm),
        Table([["Sr", "Service", "SAC", "Days", "Rate", "Amount"],
               ["1", "Warehouse loading/unloading labour", "9967", str(qty), rs(price, "Rs."), rs(sub, "Rs.")],
               ["", "", "", "", "Taxable", rs(sub, "Rs.")], ["", "", "", "", "IGST 18%", rs(igst, "Rs.")],
               ["", "", "", "", "TOTAL", rs(sub + igst, "Rs.")]],
              colWidths=[10 * mm, 72 * mm, 16 * mm, 14 * mm, 26 * mm, 32 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 10), ("GRID", (0, 0), (-1, 1), 0.6, colors.black),
                     ("FONT", (4, -1), (-1, -1), "TB", 10), ("ALIGN", (3, 0), (-1, -1), "RIGHT")]),
        Spacer(1, 8 * mm),
        Paragraph(f"Bank: {v['bank']['bank_name']}, A/c {v['bank']['account_number']}, IFSC {v['bank']['ifsc']}", S("b", fontSize=9)),
    ]
    SimpleDocTemplate(tmp, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                      bottomMargin=18 * mm).build(story)
    page = pymupdf.open(stream=tmp.getvalue(), filetype="pdf")[0]
    pix = page.get_pixmap(dpi=130)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    w, h = img.size
    canvas = Image.new("RGB", (int(w * 1.12), int(h * 1.08)), (92, 84, 74))  # desk behind the paper
    canvas.paste(img, (int(w * 0.06), int(h * 0.04)))
    W, H = canvas.size
    # perspective: paper photographed at a slight angle
    coeffs = _perspective([(0, 0), (W, 0), (W, H), (0, H)],
                          [(W * 0.03, H * 0.01), (W * 0.985, H * 0.035), (W * 0.96, H * 0.99), (W * 0.0, H * 0.975)])
    photo = canvas.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=(92, 84, 74))
    shade = Image.linear_gradient("L").resize((W, H)).rotate(90, expand=False)
    photo = Image.composite(photo, ImageEnhance.Brightness(photo).enhance(0.72), shade)  # uneven light
    photo = photo.filter(ImageFilter.GaussianBlur(0.9))
    buf = io.BytesIO()
    photo.save(buf, format="JPEG", quality=60)
    out = pymupdf.open()
    pg = out.new_page(width=595 * W / w, height=842 * H / h)
    pg.insert_image(pg.rect, stream=buf.getvalue())
    out.set_metadata({"title": "IMG_20261001_183412", "creator": "", "producer": "", "creationDate": "", "modDate": ""})
    out.save(path, garbage=4, deflate=True, no_new_id=True)


def _perspective(src, dst):
    """Coefficients for PIL's PERSPECTIVE transform mapping dst quad back to src quad."""
    import numpy as np
    a = []
    for (x, y), (u, v_) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        a.append([0, 0, 0, x, y, 1, -v_ * x, -v_ * y])
    b = [c for p in src for c in p]
    return np.linalg.solve(np.array(a, dtype=float), np.array(b, dtype=float)).tolist()


# ---------------------------------------------------------------- u3 two pages

def u3(path):
    v = V["V-002"]
    lines = [("Cello Butterflow Ball Pen, blue (box of 25)", "9608", 60, 250),
             ("Camlin Whiteboard Marker, assorted (box of 10)", "9608", 100, 220)]
    sub = sum(q * p for _, _, q, p in lines)
    half = sub * 0.09
    story = [
        Paragraph("Shree Ganesh Office Solutions", S("h", fontName="TB", fontSize=16, leading=20)),
        Paragraph(f"Shop 7, Laxmi Road, Sadashiv Peth, Pune 411030 · GSTIN: {v['gstin']} · {v['contact_email']}", S("a", fontSize=8)),
        Spacer(1, 8 * mm),
        Paragraph("TAX INVOICE  ·  Page 1 of 2", S("ti", fontName="TB", fontSize=11)),
        Spacer(1, 4 * mm),
        Table([["Invoice No.", "SGOS/1004"], ["Invoice Date", "03/10/2026"], ["Your PO", "PO-1009"],
               ["Bill To", f"{BUYER['name']}, {BUYER['address']}"], ["Buyer GSTIN", BUYER["gstin"]]],
              colWidths=[35 * mm, 135 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (0, -1), "TB", 9)]),
        Spacer(1, 10 * mm),
        Paragraph("Terms and conditions", S("tc", fontName="TB", fontSize=10)),
        Paragraph("1. Payment within 30 days of invoice date. 2. Goods once sold will not be taken back. "
                  "3. Interest at 18% p.a. on overdue amounts. 4. Subject to Pune jurisdiction. "
                  "5. Please quote the invoice number on all payments.", S("tcb", fontSize=8.5, leading=12)),
        Spacer(1, 6 * mm),
        Paragraph(f"Remittance: {v['bank']['bank_name']}, A/c No. {v['bank']['account_number']}, IFSC {v['bank']['ifsc']}", S("r", fontSize=9)),
        Spacer(1, 6 * mm),
        Paragraph("Item details continue on page 2.", S("c", fontSize=9, textColor=colors.grey)),
        PageBreak(),
        Paragraph("TAX INVOICE SGOS/1004  ·  Page 2 of 2", S("ti2", fontName="TB", fontSize=11)),
        Spacer(1, 5 * mm),
    ]
    rows = [["#", "Item", "HSN", "Qty", "Rate", "Amount"]]
    for i, (d, hsn, q, p) in enumerate(lines, 1):
        rows.append([str(i), d, hsn, str(q), rs(p, "Rs "), rs(q * p, "Rs ")])
    rows += [["", "", "", "", "Sub Total", rs(sub, "Rs ")], ["", "", "", "", "CGST 9%", rs(half, "Rs ")],
             ["", "", "", "", "SGST 9%", rs(half, "Rs ")], ["", "", "", "", "Invoice Total", rs(sub + 2 * half, "Rs ")]]
    story.append(Table(rows, colWidths=[8 * mm, 78 * mm, 16 * mm, 14 * mm, 26 * mm, 30 * mm],
                       style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (-1, 0), "TB", 9),
                              ("GRID", (0, 0), (-1, len(lines)), 0.4, colors.grey), ("FONT", (4, -1), (-1, -1), "TB", 10),
                              ("ALIGN", (3, 0), (-1, -1), "RIGHT")]))
    story += [Spacer(1, 12 * mm), Paragraph("For Shree Ganesh Office Solutions — Authorised Signatory", S("s", alignment=TA_RIGHT))]
    doc(path, story, title="SGOS/1004")


# ---------------------------------------------------------------- u4 GST column layout

def u4(path):
    v = V["V-004"]
    qty, price = 10, 15800
    taxable = qty * price
    c = s_ = taxable * 0.09
    hdr = ["Description", "HSN", "Qty", "Rate", "Taxable\nValue", "CGST\n%", "CGST\nAmt", "SGST\n%", "SGST\nAmt", "Total"]
    row = ["Steel Storage Cabinet DF-SC4", "9403", str(qty), f"{price:,.0f}", f"{taxable:,.0f}", "9", f"{c:,.0f}", "9",
           f"{s_:,.0f}", f"{taxable + c + s_:,.0f}"]
    story = [
        Table([[Paragraph("DECCAN FURNITURE WORKS", S("h", fontName="TB", fontSize=15, leading=19)),
                Paragraph("<b>GST INVOICE</b><br/>Original for Recipient", S("g", alignment=TA_RIGHT))]],
              colWidths=[110 * mm, 64 * mm]),
        Paragraph(f"Plot 41, MIDC Bhosari, Pune 411026 · GSTIN {v['gstin']} · State: Maharashtra (27)", S("a", fontSize=8)),
        Spacer(1, 6 * mm),
        Table([["Invoice No: DFW/26-27/118", "Invoice Date: 03-Oct-2026", "Buyer's Order No: PO-1010"],
               [f"Buyer: {BUYER['name']}", f"Buyer GSTIN: {BUYER['gstin']}", "Place of Supply: Maharashtra"]],
              colWidths=[58 * mm, 58 * mm, 58 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 8.5),
                                                            ("BOX", (0, 0), (-1, -1), 0.5, colors.black),
                                                            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.grey)]),
        Spacer(1, 5 * mm),
        Table([hdr, row, ["Total", "", str(qty), "", f"{taxable:,.0f}", "", f"{c:,.0f}", "", f"{s_:,.0f}", f"{taxable + c + s_:,.0f}"]],
              colWidths=[44 * mm, 12 * mm, 10 * mm, 15 * mm, 19 * mm, 10 * mm, 16 * mm, 10 * mm, 16 * mm, 22 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 8), ("FONT", (0, 0), (-1, 0), "TB", 7.5),
                     ("FONT", (0, -1), (-1, -1), "TB", 8), ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
                     ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]),
        Spacer(1, 5 * mm),
        Paragraph("HSN-wise tax summary", S("hs", fontName="TB", fontSize=9)),
        Table([["HSN", "Taxable Value", "CGST", "SGST", "Total Tax"],
               ["9403", f"{taxable:,.2f}", f"{c:,.2f}", f"{s_:,.2f}", f"{c + s_:,.2f}"]],
              colWidths=[25 * mm, 35 * mm, 30 * mm, 30 * mm, 30 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                     ("ALIGN", (1, 0), (-1, -1), "RIGHT")]),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>Amount chargeable (in words):</b> INR One Lakh Eighty Six Thousand Four Hundred Forty Only", S("w", fontSize=8.5)),
        Spacer(1, 6 * mm),
        Paragraph(f"Bank: {v['bank']['bank_name']} · A/c {v['bank']['account_number']} · IFSC {v['bank']['ifsc']}", S("b", fontSize=8.5)),
    ]
    doc(path, story, title="DFW/26-27/118")


# ---------------------------------------------------------------- u5 PO without prefix, US date

def u5(path):
    v = V["V-001"]
    qty, price = 8, 16900
    sub = qty * price
    half = sub * 0.09
    story = [
        Table([[Paragraph("<b>APEX TECH</b><br/>Supplies Pvt Ltd", S("h", fontName="TB", fontSize=13, leading=16)),
                Paragraph("INVOICE", S("i", fontName="TB", fontSize=22, leading=26, alignment=TA_RIGHT,
                                       textColor=colors.HexColor("#2E7D32")))]], colWidths=[100 * mm, 74 * mm]),
        Spacer(1, 4 * mm),
        Table([[Paragraph(f"<b>FROM</b><br/>Unit 12, Kalyani Nagar Tech Hub<br/>Pune 411006<br/>GSTIN {v['gstin']}", S("f")),
                Paragraph(f"<b>BILL TO</b><br/>{BUYER['name']}<br/>{BUYER['address']}<br/>GSTIN {BUYER['gstin']}", S("t")),
                Paragraph("<b>Invoice #</b> ATS/26-27/0477<br/><b>Date</b> Oct 2, 2026<br/><b>P.O. No:</b> 1011<br/><b>Terms</b> Net 30", S("m"))]],
              colWidths=[58 * mm, 64 * mm, 52 * mm], style=[("VALIGN", (0, 0), (-1, -1), "TOP")]),
        Spacer(1, 8 * mm),
        Table([["QTY", "DESCRIPTION", "UNIT PRICE", "LINE TOTAL"],
               [str(qty), "Dell P2425H 24in monitor", f"{price:,.2f}", f"{sub:,.2f}"],
               ["", "", "SUBTOTAL", f"{sub:,.2f}"], ["", "", "CGST 9%", f"{half:,.2f}"], ["", "", "SGST 9%", f"{half:,.2f}"],
               ["", "", "TOTAL (INR)", f"{sub + 2 * half:,.2f}"]],
              colWidths=[16 * mm, 92 * mm, 32 * mm, 34 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (-1, 0), "TB", 8.5),
                     ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5E9")), ("FONT", (2, -1), (-1, -1), "TB", 10),
                     ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 1), (-1, 1), 0.4, colors.grey)]),
        Spacer(1, 10 * mm),
        Paragraph(f"Make all payments to {v['bank']['bank_name']}, account {v['bank']['account_number']}, IFSC {v['bank']['ifsc']}.", S("p")),
    ]
    doc(path, story, title="ATS/26-27/0477")


# ---------------------------------------------------------------- u6 a quotation, not an invoice

def u6(path):
    v = V["V-001"]
    qty, price = 4, 24500
    sub = qty * price
    half = sub * 0.09
    story = [
        Paragraph("Apex Tech Supplies Pvt Ltd", S("h", fontName="TB", fontSize=15, leading=19)),
        Paragraph(f"Unit 12, Kalyani Nagar Tech Hub, Pune 411006 · GSTIN {v['gstin']}", S("a", fontSize=8)),
        Spacer(1, 6 * mm),
        Paragraph("QUOTATION", S("q", fontName="TB", fontSize=14, alignment=TA_CENTER)),
        Spacer(1, 4 * mm),
        Table([["Quote No.", "Q-ATS-2026-212"], ["Date", "28-09-2026"], ["Valid until", "28-10-2026"],
               ["Against PO", "PO-1006"], ["Prepared for", BUYER["name"]]],
              colWidths=[35 * mm, 100 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (0, -1), "TB", 9)]),
        Spacer(1, 6 * mm),
        Table([["Item", "Qty", "Unit price", "Amount"], ["HP LaserJet Pro M404dn", str(qty), f"{price:,.2f}", f"{sub:,.2f}"],
               ["", "", "CGST 9%", f"{half:,.2f}"], ["", "", "SGST 9%", f"{half:,.2f}"], ["", "", "Quoted total", f"{sub + 2 * half:,.2f}"]],
              colWidths=[90 * mm, 15 * mm, 30 * mm, 35 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("GRID", (0, 0), (-1, 1), 0.4, colors.grey),
                     ("ALIGN", (1, 0), (-1, -1), "RIGHT")]),
        Spacer(1, 8 * mm),
        Paragraph("This is a quotation, not a tax invoice. A tax invoice will be issued on delivery.", S("n", fontSize=9)),
    ]
    doc(path, story, title="Quotation Q-ATS-2026-212")


# ---------------------------------------------------------------- u7 billed in US dollars

def u7(path):
    v = V["V-003"]
    qty, price = 10, 192.07  # USD; about Rs 18,500 at the 2 Oct 2026 rate
    sub = round(qty * price, 2)
    igst = round(sub * 0.18, 2)
    story = [
        Paragraph("Northwind Electronics LLP", S("h", fontName="TB", fontSize=14, leading=18)),
        Paragraph(f"No. 88, 3rd Cross, Koramangala, Bengaluru 560034 · GSTIN {v['gstin']}", S("a", fontSize=8)),
        Spacer(1, 8 * mm),
        Paragraph("TAX INVOICE (billed in USD)", S("ti", fontName="TB", fontSize=12)),
        Spacer(1, 4 * mm),
        Table([["Invoice No.", "NWE-2026-1340"], ["Date", "02 Oct 2026"], ["PO Reference", "PO-1002"],
               ["Bill To", BUYER["name"]], ["Buyer GSTIN", BUYER["gstin"]], ["Currency", "USD"]],
              colWidths=[35 * mm, 100 * mm], style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (0, -1), "TB", 9)]),
        Spacer(1, 6 * mm),
        Table([["Item", "Qty", "Unit price (USD)", "Amount (USD)"],
               ["LG 27MP400 27-inch IPS Monitor", str(qty), f"${price:,.2f}", f"${sub:,.2f}"],
               ["", "", "Subtotal", f"${sub:,.2f}"], ["", "", "IGST 18%", f"${igst:,.2f}"],
               ["", "", "Total (USD)", f"${sub + igst:,.2f}"]],
              colWidths=[85 * mm, 15 * mm, 35 * mm, 35 * mm],
              style=[("FONT", (0, 0), (-1, -1), "AU", 9), ("FONT", (0, 0), (-1, 0), "TB", 9),
                     ("GRID", (0, 0), (-1, 1), 0.4, colors.grey), ("FONT", (2, -1), (-1, -1), "TB", 10),
                     ("ALIGN", (1, 0), (-1, -1), "RIGHT")]),
        Spacer(1, 8 * mm),
        Paragraph(f"Pay to {v['bank']['bank_name']}, A/c {v['bank']['account_number']}, IFSC {v['bank']['ifsc']}", S("p")),
    ]
    doc(path, story, title="NWE-2026-1340")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in [("u1_northwind_minimal_rupee.pdf", u1), ("u2_blue_river_phone_photo.pdf", u2),
                     ("u3_shree_ganesh_two_pages.pdf", u3), ("u4_deccan_gst_columns.pdf", u4),
                     ("u5_apex_po_without_prefix.pdf", u5), ("u6_apex_quotation_not_invoice.pdf", u6),
                     ("u7_northwind_billed_in_usd.pdf", u7)]:
        fn(OUT / name)
        print("wrote", name)


if __name__ == "__main__":
    main()
