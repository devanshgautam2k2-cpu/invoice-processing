"""Generate the PS-1 test invoice PDFs into test_invoices/.

Three layouts so extraction has to cope with different labels and structure:
  classic    - plain grid table, "PO No." label        (Apex Tech, Shree Ganesh)
  modern     - colour band header, "Your Ref" label     (Northwind)
  letterhead - centred serif letterhead, "Purchase Order" label (Deccan Furniture)

Vendor GSTIN and bank details come from data/vendors.json, buyer details from
data/config.json. GST is 18%: CGST 9% + SGST 9% when the vendor is in the same
state as the buyer (GSTIN prefix 27), otherwise IGST 18%.

PDFs are built with reportlab's invariant mode, so re-running the script gives
byte-identical files (stable file hashes for the extraction cache).

Also writes test_invoices/extended/: one invoice per remaining reason code.

Run:  python scripts/generate_invoices.py
"""

import io
import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

rl_config.invariant = 1

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "test_invoices"

GST_RATE = 18
BUYER_STATE = "27"

VENDORS = {v["vendor_id"]: v for v in json.loads((DATA / "vendors.json").read_text())}
BUYER = json.loads((DATA / "config.json").read_text())["buyer"]

VENDOR_ADDRESS = {
    "V-001": "Unit 12, Kalyani Nagar Tech Hub, Pune 411006, Maharashtra",
    "V-002": "Shop 7, Laxmi Road, Sadashiv Peth, Pune 411030, Maharashtra",
    "V-003": "No. 88, 3rd Cross, Koramangala, Bengaluru 560034, Karnataka",
    "V-004": "Plot 41, MIDC Bhosari, Pune 411026, Maharashtra",
    "V-005": "B-27, Okhla Industrial Area Phase II, New Delhi 110020",
}


@dataclass
class Line:
    description: str
    hsn: str
    qty: int
    unit_price: float

    @property
    def amount(self) -> float:
        return self.qty * self.unit_price


@dataclass
class Invoice:
    filename: str
    vendor_id: str
    invoice_number: str
    invoice_date: str  # display string, formatted per layout
    po_number: str
    layout: str
    lines: list[Line] = field(default_factory=list)
    # Knobs for the extended edge cases (defaults reproduce the core set byte for byte)
    tax_rate: float = GST_RATE
    discount_pct: float = 0
    total_override: float | None = None   # print a wrong grand total
    show_gstin: bool = True               # omit the supplier GSTIN
    vendor_overrides: dict = field(default_factory=dict)  # name / bank / unknown vendor
    po_stamp: bool = False                # PO number as a stamped image, not text

    @property
    def vendor(self) -> dict:
        return {**VENDORS.get(self.vendor_id, {}), **self.vendor_overrides}

    @property
    def address(self) -> str:
        return self.vendor.get("address") or VENDOR_ADDRESS[self.vendor_id]

    @property
    def subtotal(self) -> float:
        return sum(l.amount for l in self.lines)

    @property
    def discount(self) -> float:
        return round(self.subtotal * self.discount_pct / 100, 2)

    @property
    def taxable(self) -> float:
        return self.subtotal - self.discount

    @property
    def intra_state(self) -> bool:
        return self.vendor["gstin"][:2] == BUYER_STATE

    def tax_rows(self) -> list[tuple[str, float]]:
        r = self.tax_rate
        if self.intra_state:
            half = round(self.taxable * r / 200, 2)
            return [(f"CGST @ {r / 2:g}%", half), (f"SGST @ {r / 2:g}%", half)]
        return [(f"IGST @ {r:g}%", round(self.taxable * r / 100, 2))]

    @property
    def grand_total(self) -> float:
        if self.total_override is not None:
            return self.total_override
        return self.taxable + sum(v for _, v in self.tax_rows())

    def summary_rows(self, sub_label: str, taxable_label: str) -> list[tuple[str, float]]:
        """Totals block: subtotal, optional discount, tax rows (grand total added by the layout)."""
        if not self.discount:
            return [(sub_label, self.subtotal)] + self.tax_rows()
        return ([("Sub Total", self.subtotal), (f"Less: Trade Discount @ {self.discount_pct:g}%", self.discount),
                 (taxable_label, self.taxable)] + self.tax_rows())

    def gstin_text(self, prefix: str) -> str:
        return f"{prefix}{self.vendor['gstin']}" if self.show_gstin else ""


def inr(x: float) -> str:
    """Indian digit grouping: 941640 -> 9,41,640.00"""
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{whole}.{frac}"


def words_total(x: float) -> str:
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
            "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
            "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two(n):
        return ones[n] if n < 20 else (tens[n // 10] + (" " + ones[n % 10] if n % 10 else ""))

    def three(n):
        h, r = divmod(n, 100)
        return ((ones[h] + " Hundred" + (" " if r else "")) if h else "") + (two(r) if r else "")

    n = int(round(x))
    parts = []
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, div)
        if q:
            parts.append(f"{three(q)} {name}")
    if n:
        parts.append(three(n))
    return "Rupees " + " ".join(parts) + " Only"


# ---------------------------------------------------------------- layouts

def _style(name, **kw):
    base = dict(fontName="Helvetica", fontSize=9, leading=12)
    base.update(kw)
    return ParagraphStyle(name, **base)


def build_classic(inv: Invoice, path: Path):
    v = inv.vendor
    s = _style("s")
    title = _style("t", fontName="Helvetica-Bold", fontSize=16, leading=20)
    right = _style("r", alignment=TA_RIGHT)

    story = [
        Table(
            [[Paragraph(v["legal_name"], title), Paragraph("<b>TAX INVOICE</b>", _style("ti", fontSize=13, alignment=TA_RIGHT))],
             [Paragraph(f"{inv.address}<br/>" + (f"{inv.gstin_text('GSTIN: ')}<br/>" if inv.show_gstin else "")
                        + f"Email: {v['contact_email']}", s),
              Paragraph(f"Invoice No.: <b>{inv.invoice_number}</b><br/>Invoice Date: {inv.invoice_date}<br/>PO No.: <b>{inv.po_number}</b>", right)]],
            colWidths=[110 * mm, 70 * mm],
            style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
        ),
        Spacer(1, 6 * mm),
        Paragraph("<b>Bill To</b>", s),
        Paragraph(f"{BUYER['name']}<br/>{BUYER['address']}<br/>GSTIN: {BUYER['gstin']}", s),
        Spacer(1, 6 * mm),
    ]

    rows = [["#", "Description", "HSN/SAC", "Qty", "Rate (Rs.)", "Amount (Rs.)"]]
    for i, l in enumerate(inv.lines, 1):
        rows.append([str(i), Paragraph(l.description, s), l.hsn, str(l.qty), inr(l.unit_price), inr(l.amount)])
    for label, amt in inv.summary_rows("Sub Total", "Taxable Value"):
        rows.append(["", "", "", "", label, inr(amt)])
    rows.append(["", "", "", "", "Grand Total", inr(inv.grand_total)])
    n_lines = len(inv.lines)
    t = Table(rows, colWidths=[8 * mm, 82 * mm, 20 * mm, 14 * mm, 28 * mm, 28 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("FONT", (4, -1), (-1, -1), "Helvetica-Bold", 10),
        ("GRID", (0, 0), (-1, n_lines), 0.5, colors.black),
        ("BOX", (4, n_lines + 1), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [t, Spacer(1, 4 * mm),
              Paragraph(f"Amount in words: <i>{words_total(inv.grand_total)}</i>", s),
              Spacer(1, 8 * mm),
              Paragraph("<b>Bank Details</b>", s),
              Paragraph(f"Bank: {v['bank']['bank_name']}<br/>A/c No.: {v['bank']['account_number']}<br/>IFSC: {v['bank']['ifsc']}", s),
              Spacer(1, 10 * mm),
              Paragraph(f"For {v['legal_name']}<br/><br/>Authorised Signatory", right)]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=15 * mm, bottomMargin=15 * mm,
                      title=f"Invoice {inv.invoice_number}", author=v["legal_name"]).build(story)


def build_modern(inv: Invoice, path: Path):
    v = inv.vendor
    accent = colors.HexColor("#1F4E79")
    s = _style("s")
    white = _style("w", textColor=colors.white, fontSize=10)
    big = _style("b", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.white)
    small_grey = _style("g", fontSize=8, textColor=colors.HexColor("#555555"))

    header = Table(
        [[Paragraph(v["aliases"][0].upper(), big), Paragraph("INVOICE", _style("iv", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.white, alignment=TA_RIGHT))],
         [Paragraph(f"{v['legal_name']} | {inv.address}", white), ""]],
        colWidths=[130 * mm, 50 * mm],
        style=[("BACKGROUND", (0, 0), (-1, -1), accent), ("SPAN", (0, 1), (1, 1)),
               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, 0), 8),
               ("BOTTOMPADDING", (0, -1), (-1, -1), 8)],
    )
    meta = Table(
        [["Bill No.", inv.invoice_number, "Billed To", BUYER["name"]],
         ["Dated", inv.invoice_date, "", BUYER["address"]],
         ["Your Ref", po_stamp_image(inv.po_number) if inv.po_stamp else inv.po_number, "Buyer GSTIN", BUYER["gstin"]],
         ["Supplier GSTIN" if inv.show_gstin else "", inv.gstin_text(""), "Place of Supply", "Maharashtra (27)"]],
        colWidths=[28 * mm, 40 * mm, 28 * mm, 84 * mm],
        style=[("FONT", (0, 0), (-1, -1), "Helvetica", 9),
               ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
               ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9),
               ("TEXTCOLOR", (0, 0), (0, -1), accent), ("TEXTCOLOR", (2, 0), (2, -1), accent)],
    )
    rows = [["Item", "HSN", "Units", "Unit Cost", "Line Total"]]
    for l in inv.lines:
        rows.append([Paragraph(l.description, s), l.hsn, str(l.qty), inr(l.unit_price), inr(l.amount)])
    t = Table(rows, colWidths=[86 * mm, 20 * mm, 18 * mm, 28 * mm, 28 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), accent), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF3F8")]),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, -1), (-1, -1), 1, accent),
    ]))
    sums = [[k, inr(a)] for k, a in inv.summary_rows("Taxable Value", "Taxable Value")]
    sums.append(["Amount Payable (INR)", inr(inv.grand_total)])
    totals = Table(sums, colWidths=[50 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 11), ("TEXTCOLOR", (0, -1), (-1, -1), accent),
        ("LINEABOVE", (0, -1), (-1, -1), 1, accent),
    ]))
    pay = Table(
        [["Remit to", f"{v['legal_name']}, {v['bank']['bank_name']}"],
         ["Account", v["bank"]["account_number"]], ["IFSC", v["bank"]["ifsc"]]],
        colWidths=[25 * mm, 100 * mm],
        style=[("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
               ("BOX", (0, 0), (-1, -1), 0.5, accent)],
    )
    story = [header, Spacer(1, 6 * mm), meta, Spacer(1, 6 * mm), t, Spacer(1, 3 * mm), totals,
             Spacer(1, 3 * mm), Paragraph(words_total(inv.grand_total), small_grey), Spacer(1, 8 * mm),
             pay, Spacer(1, 6 * mm),
             Paragraph(f"Payment due within 30 days. Queries: {v['contact_email']}", small_grey)]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=12 * mm, bottomMargin=15 * mm,
                      title=f"Invoice {inv.invoice_number}", author=v["legal_name"]).build(story)


def build_letterhead(inv: Invoice, path: Path):
    v = inv.vendor
    serif = _style("se", fontName="Times-Roman", fontSize=10, leading=13)
    centre = _style("c", fontName="Times-Roman", fontSize=10, leading=13, alignment=TA_CENTER)
    head = _style("h", fontName="Times-Bold", fontSize=22, leading=26, alignment=TA_CENTER)
    story = [
        Paragraph(v["legal_name"].upper(), head),
        Paragraph(f"{inv.address}<br/>" + (f"{inv.gstin_text('GSTIN ')} &nbsp;|&nbsp; " if inv.show_gstin else "")
                  + v["contact_email"], centre),
        Spacer(1, 3 * mm),
        Table([[""]], colWidths=[180 * mm], style=[("LINEABOVE", (0, 0), (-1, 0), 1.5, colors.black)]),
        Paragraph("<b>GST INVOICE</b>", _style("gi", fontName="Times-Bold", fontSize=13, alignment=TA_CENTER, leading=18)),
        Spacer(1, 4 * mm),
        Table(
            [[Paragraph(f"<b>To:</b><br/>{BUYER['name']}<br/>{BUYER['address']}<br/>GSTIN: {BUYER['gstin']}", serif),
              Paragraph(f"<b>Invoice #</b> {inv.invoice_number}<br/><b>Date</b> {inv.invoice_date}<br/><b>Purchase Order</b> {inv.po_number}", serif)]],
            colWidths=[110 * mm, 70 * mm], style=[("VALIGN", (0, 0), (-1, -1), "TOP")]),
        Spacer(1, 6 * mm),
    ]
    rows = [["S.No", "Particulars", "HSN", "Quantity", "Price/Unit", "Value"]]
    for i, l in enumerate(inv.lines, 1):
        rows.append([str(i), Paragraph(l.description, serif), l.hsn, f"{l.qty} Nos", inr(l.unit_price), inr(l.amount)])
    for label, amt in inv.summary_rows("Taxable Amount", "Taxable Amount") + [("TOTAL", inv.grand_total)]:
        rows.append(["", "", "", "", label, inr(amt)])
    n = len(inv.lines)
    t = Table(rows, colWidths=[12 * mm, 78 * mm, 18 * mm, 20 * mm, 28 * mm, 26 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Times-Roman", 10), ("FONT", (0, 0), (-1, 0), "Times-Bold", 10),
        ("FONT", (4, -1), (-1, -1), "Times-Bold", 11),
        ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
        ("LINEBELOW", (0, n), (-1, n), 0.5, colors.black), ("LINEABOVE", (4, -1), (-1, -1), 1, colors.black),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
    ]))
    story += [t, Spacer(1, 4 * mm), Paragraph(f"<i>{words_total(inv.grand_total)}</i>", serif),
              Spacer(1, 8 * mm),
              Paragraph(f"Kindly remit to <b>{v['bank']['bank_name']}</b>, A/c {v['bank']['account_number']}, IFSC {v['bank']['ifsc']}.", serif),
              Spacer(1, 14 * mm),
              Paragraph(f"for <b>{v['legal_name']}</b><br/><br/><br/>Proprietor", _style("sg", fontName="Times-Roman", fontSize=10, leading=13, alignment=TA_RIGHT))]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=15 * mm, bottomMargin=15 * mm,
                      title=f"Invoice {inv.invoice_number}", author=v["legal_name"]).build(story)


def po_stamp_image(text: str):
    """A rubber-stamp style PO reference rendered as a picture: readable by eye, absent from the text layer."""
    from reportlab.platypus import Image as RLImage
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (300, 70), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 38)
    except OSError:
        font = ImageFont.load_default(size=38)
    d.rectangle([3, 3, 296, 66], outline=(40, 70, 160), width=4)
    d.text((150, 36), text, fill=(40, 70, 160), font=font, anchor="mm")
    img = img.rotate(-3, resample=Image.BICUBIC, fillcolor="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return RLImage(buf, width=36 * mm, height=8.4 * mm)


LAYOUTS = {"classic": build_classic, "modern": build_modern, "letterhead": build_letterhead}


def rasterise(src: Path, dst: Path, seed: int = 4):
    """Turn a text PDF into an image-only 'scan': grey, slightly rotated, noisy, JPEG-compressed."""
    rng = random.Random(seed)
    src_doc = pymupdf.open(src)
    out = pymupdf.open()
    for page in src_doc:
        pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img = img.rotate(0.8, resample=Image.BICUBIC, expand=False, fillcolor=245)
        px = img.load()
        for _ in range(img.width * img.height // 60):
            x, y = rng.randrange(img.width), rng.randrange(img.height)
            px[x, y] = max(0, min(255, px[x, y] + rng.randint(-90, 40)))
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=55)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    out.set_metadata({"title": "Scanned document", "creator": "Scanner", "producer": "Scanner",
                      "creationDate": "", "modDate": ""})
    out.save(dst, garbage=4, deflate=True, no_new_id=True)


# ---------------------------------------------------------------- test set

INVOICES = [
    Invoice("01_apex_happy_path.pdf", "V-001", "ATS/26-27/0418", "22-09-2026", "PO-1001", "classic", [
        Line("Dell Latitude 5440 Laptop - Intel i5, 16GB RAM, 512GB SSD", "8471", 10, 78000),
        Line("Logitech MK270 Wireless Keyboard & Mouse Combo", "8471", 10, 1800),
    ]),
    Invoice("02a_northwind_split_part1.pdf", "V-003", "NWE-2026-1187", "18 Sep 2026", "PO-1002", "modern", [
        Line("LG 27MP400 27\" IPS Full HD Monitor", "8528", 20, 18500),
    ]),
    Invoice("02b_northwind_split_overbilled.pdf", "V-003", "NWE-2026-1244", "29 Sep 2026", "PO-1002", "modern", [
        Line("LG 27MP400 27\" IPS Full HD Monitor", "8528", 45, 18500),
    ]),
    Invoice("03a_shree_ganesh_price_over.pdf", "V-002", "SGOS/0912", "24/09/2026", "PO-1003", "classic", [
        Line("JK Copier A4 Paper 75 GSM, 500 sheets/ream", "4802", 200, 302),
    ]),
    Invoice("03b_shree_ganesh_waiting.pdf", "V-002", "SGOS/0927", "30/09/2026", "PO-1003", "classic", [
        Line("JK Copier A4 Paper 75 GSM, 500 sheets/ream", "4802", 150, 290),
        Line("Kangaro HD-45 Stapler (Heavy Duty)", "8472", 40, 165),
    ]),
    Invoice("04_deccan_scanned_over.pdf", "V-004", "DFW-INV-2209", "26 September 2026", "PO-1004", "letterhead", [
        Line("Ergonomic Mesh Office Chair (Model DF-EC21), black", "9401", 25, 13700),
    ]),
]

# Extended set: one invoice per remaining reason code. Run after the core set
# (03a and 04 still awaiting review); none of these touch PO-1003 or PO-1004.
KAVERI = {"legal_name": "Kaveri Traders", "aliases": ["Kaveri Traders"], "gstin": "27AAHFK2211M1Z3",
          "address": "12 Market Yard, Gultekdi, Pune 411037, Maharashtra", "contact_email": "sales@kaveritraders.example",
          "bank": {"bank_name": "Yes Bank", "account_number": "019283746501", "ifsc": "YESB0000192"}}
DELL_24 = "Dell 24 Monitor P2425H (23.8\", IPS, FHD)"
EXTENDED = [
    Invoice("e01_S4_apex_po_fully_invoiced.pdf", "V-001", "ATS/26-27/0455", "30-09-2026", "PO-1001", "classic", [
        Line("Dell Latitude 5440 Laptop - Intel i5, 16GB RAM, 512GB SSD", "8471", 2, 78000)]),
    Invoice("e02_S3_apex_number_already_approved.pdf", "V-001", "ATS/26-27/0418", "01-10-2026", "PO-1011", "classic", [
        Line(DELL_24, "8528", 2, 16900)]),
    Invoice("e03_S9_northwind_po_not_found.pdf", "V-003", "NWE-2026-1301", "30 Sep 2026", "PO-1020", "modern", [
        Line("Logitech C920 HD Pro Webcam", "8525", 5, 6200)]),
    Invoice("e04_S7_kaveri_wrong_vendor.pdf", "X-KAV", "KT/0098", "28/09/2026", "PO-1011", "classic", [
        Line(DELL_24, "8528", 8, 16900)], vendor_overrides=KAVERI),
    Invoice("e05_S8_shree_ganesh_items_not_on_po.pdf", "V-002", "SGOS/0951", "27/09/2026", "PO-1009", "classic", [
        Line("Godrej Interio Visitor Chair, black", "9401", 5, 3400)]),
    Invoice("e06_S6_deccan_over_7pct.pdf", "V-004", "DFW-INV-2231", "29 September 2026", "PO-1010", "letterhead", [
        Line("Steel Storage Cabinet DF-SC4 (4 shelves), grey", "9403", 10, 17400)]),
    Invoice("e07_S1_blue_river_no_gstin.pdf", "V-005", "BRL/PNQ/2026/0311", "25 Sep 2026", "PO-1012", "modern", [
        Line("Courier - intra-city consignments, Pune", "9968", 20, 600)], show_gstin=False),
    Invoice("e08_A2_apex_small_shortfall.pdf", "V-001", "ATS/26-27/0462", "29-09-2026", "PO-1006", "classic", [
        Line("HP LaserJet Pro M404dn Mono Laser Printer", "8443", 4, 24100)]),
    Invoice("e09_A3_blue_river_stated_discount.pdf", "V-005", "BRL/PNQ/2026/0298", "24 Sep 2026", "PO-1005", "modern", [
        Line("Freight Pune to Mumbai, full truck load (per trip)", "9965", 6, 9500)], discount_pct=10),
    Invoice("e10_H2_northwind_underbilled.pdf", "V-003", "NWE-2026-1266", "26 Sep 2026", "PO-1008", "modern", [
        Line("Logitech C920 HD Pro Webcam", "8525", 30, 5700)]),
    Invoice("e11_H5_blue_river_totals_dont_add_up.pdf", "V-005", "BRL/PNQ/2026/0305", "26 Sep 2026", "PO-1007", "modern", [
        Line("Warehouse loading/unloading labour, per day", "9967", 5, 4500)], total_override=27550),
    Invoice("e12_H6_sri_ganesh_name_variant.pdf", "V-002", "SGOS/0963", "30/09/2026", "PO-1009", "classic", [
        Line("Camlin Whiteboard Marker, box of 10", "9608", 100, 220)],
        vendor_overrides={"legal_name": "Sri Ganesh Office Solution"}),
    Invoice("e13_H7_deccan_bank_changed.pdf", "V-004", "DFW-INV-2240", "1 October 2026", "PO-1010", "letterhead", [
        Line("Steel Storage Cabinet DF-SC4 (4 shelves), grey", "9403", 10, 15800)],
        vendor_overrides={"bank": {"bank_name": "HDFC Bank", "account_number": "50200087719932", "ifsc": "HDFC0009876"}}),
    Invoice("e14_H9_northwind_near_duplicate.pdf", "V-003", "NWE-2026-1188", "18 Sep 2026", "PO-1002", "modern", [
        Line("LG 27MP400 27\" IPS Full HD Monitor", "8528", 20, 18500)]),
    Invoice("e15_H10_apex_dated_before_po.pdf", "V-001", "ATS/26-27/0391", "10-09-2026", "PO-1011", "classic", [
        Line(DELL_24, "8528", 8, 16900)]),
    Invoice("e16_H11_blue_river_wrong_tax_rate.pdf", "V-005", "BRL/PNQ/2026/0322", "29 Sep 2026", "PO-1012", "modern", [
        Line("Courier - intra-city consignments, Pune", "9968", 20, 600)], tax_rate=20),
    Invoice("e17_H12_blue_river_po_stamped.pdf", "V-005", "BRL/PNQ/2026/0317", "30 Sep 2026", "PO-1005", "modern", [
        Line("Freight Pune to Mumbai, full truck load (per trip)", "9965", 6, 9500)], po_stamp=True),
]

SCANNED = {"04_deccan_scanned_over.pdf"}
DUPLICATES = {"05_apex_duplicate_of_01.pdf": "01_apex_happy_path.pdf"}


def main():
    OUT.mkdir(exist_ok=True)
    for inv in INVOICES:
        dst = OUT / inv.filename
        if inv.filename in SCANNED:
            tmp = OUT / f"_text_{inv.filename}"
            LAYOUTS[inv.layout](inv, tmp)
            rasterise(tmp, dst)
            tmp.unlink()
        else:
            LAYOUTS[inv.layout](inv, dst)
        print(f"{inv.filename:40s} {inv.vendor_id}  {inv.po_number}  total Rs {inr(inv.grand_total):>14s}")
    for copy, original in DUPLICATES.items():
        shutil.copyfile(OUT / original, OUT / copy)
        print(f"{copy:40s} byte copy of {original}")

    ext = OUT / "extended"
    ext.mkdir(exist_ok=True)
    for inv in EXTENDED:
        LAYOUTS[inv.layout](inv, ext / inv.filename)
        print(f"extended/{inv.filename:46s} {inv.po_number}  total Rs {inr(inv.grand_total):>12s}")


if __name__ == "__main__":
    main()
