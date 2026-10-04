"""Deterministic checks. Plain code: same input, same output, explainable rule by rule."""

import re
from datetime import date

from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
from rapidfuzz import fuzz

from .config import config
from .pdf_reader import squash


def inr(x: float, prefix: str = "Rs ") -> str:
    """Indian digit grouping: 941640 -> Rs 9,41,640.00"""
    neg, x = x < 0, abs(x)
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if neg else "") + f"{prefix}{whole}.{frac}"


# ---------------------------------------------------------------- stage 2: normalise


def parse_amount(s: str | None) -> float | None:
    """'Rs. 9,41,640.00' / 'Rs.4,500.00' / '₹1,69,920.00' / 'INR 2,400' -> number; '' -> None.

    Takes the first number in the string, so a currency prefix ending in a dot ('Rs.') is never
    mistaken for a decimal point.
    """
    if not s:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return round(float(m.group().replace(",", "")), 2)
    except ValueError:
        return None


def parse_qty(s: str | None) -> float | None:
    """'25 Nos' -> 25.0"""
    if not s:
        return None
    m = re.search(r"\d[\d,]*(\.\d+)?", s)
    return float(m.group().replace(",", "")) if m else None


def parse_percent(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def parse_date(s: str | None) -> date | None:
    """Indian invoices are day-first: 05-09-2026 is 5 September."""
    if not s:
        return None
    try:
        return dateparser.parse(s, dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def val(extracted: dict, field: str) -> str | None:
    v = (extracted.get(field) or {}).get("value")
    return v.strip() if v and v.strip() else None


def normalise(ex: dict) -> dict:
    """Turn the raw transcription into typed values. Unparseable stays None."""
    lines = []
    for i, l in enumerate(ex.get("line_items", []), 1):
        lines.append({
            "n": i,
            "description": l["description"],
            "qty": parse_qty(l.get("quantity")),
            "unit_price": parse_amount(l.get("unit_price")),
            "amount": parse_amount(l.get("amount")),
            "quote": l.get("quote"),
        })
    taxes = [{"label": t["label"], "rate": parse_percent(t.get("rate_percent")),
              "amount": parse_amount(t.get("amount"))} for t in ex.get("tax_lines", [])]
    discounts = [{"label": d["label"], "percent": parse_percent(d.get("percent")) or parse_percent(d["label"]),
                  "amount": parse_amount(d.get("amount"))} for d in ex.get("discounts", [])]
    return {
        "vendor_name": val(ex, "vendor_name"),
        "vendor_gstin": val(ex, "vendor_gstin"),
        "invoice_number": val(ex, "invoice_number"),
        "invoice_date": parse_date(val(ex, "invoice_date")),
        "invoice_date_raw": val(ex, "invoice_date"),
        "po_number": val(ex, "po_number"),
        "bank_account_number": re.sub(r"\D", "", val(ex, "bank_account_number") or "") or None,
        "bank_ifsc": (val(ex, "bank_ifsc") or "").upper().replace(" ", "") or None,
        "subtotal": parse_amount(val(ex, "subtotal")),
        "grand_total": parse_amount(val(ex, "grand_total")),
        "lines": lines,
        "taxes": taxes,
        "discounts": discounts,
    }


# ---------------------------------------------------------------- stage 3: reading confidence

def _close(a: float, b: float, tol_abs: float = 1.0) -> bool:
    return abs(a - b) <= max(tol_abs, abs(b) * 0.0001)


def arithmetic_checks(n: dict) -> list[dict]:
    """Internal consistency of the invoice itself (never compared with the PO)."""
    out = []
    for l in n["lines"]:
        if None in (l["qty"], l["unit_price"], l["amount"]):
            out.append({"check": f"Line {l['n']} numbers readable", "ok": False,
                        "detail": "quantity, unit price or amount could not be read"})
        else:
            calc = l["qty"] * l["unit_price"]
            out.append({"check": f"Line {l['n']}: qty x price = amount", "ok": _close(calc, l["amount"]),
                        "detail": f"{l['qty']:g} x {inr(l['unit_price'], '')} = {inr(calc, '')} vs printed {inr(l['amount'], '')}"})
    line_sum = sum(l["amount"] or 0 for l in n["lines"])
    disc = sum(d["amount"] or 0 for d in n["discounts"])
    if n["subtotal"] is not None:
        ok = _close(line_sum, n["subtotal"]) or (disc > 0 and _close(line_sum - disc, n["subtotal"]))
        out.append({"check": "Line amounts = subtotal", "ok": ok,
                    "detail": f"{inr(line_sum, '')}" + (f" less discount {inr(disc, '')}" if disc else "")
                              + f" vs subtotal {inr(n['subtotal'], '')}"})
    base = taxable_value(n)
    tax = sum(t["amount"] or 0 for t in n["taxes"])
    if n["grand_total"] is not None:
        calc = base + tax
        out.append({"check": "Taxable value + tax = grand total", "ok": _close(calc, n["grand_total"]),
                    "detail": f"{inr(base, '')} + {inr(tax, '')} = {inr(calc, '')} vs printed {inr(n['grand_total'], '')}"})
    return out


def taxable_value(n: dict) -> float:
    """Value GST is charged on: after any discount. The printed 'subtotal' may sit before or after the discount."""
    line_sum = sum(l["amount"] or 0 for l in n["lines"])
    disc = sum(d["amount"] or 0 for d in n["discounts"])
    sub = n["subtotal"]
    if sub is None:
        return line_sum - disc
    if disc and _close(sub, line_sum - disc):
        return sub
    return sub - disc


def quote_checks(ex: dict, text_layer: str) -> list[dict]:
    """Source-quote validation (text PDFs): the quote exists in the text layer and contains the value."""
    T = squash(text_layer)
    out = []
    for field in ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "grand_total"]:
        f = ex.get(field) or {}
        v, q = (f.get("value") or "").strip(), (f.get("quote") or "").strip()
        if not v:
            continue  # missing fields are handled by the required-fields stage
        quote_found = squash(q) in T if q else False
        value_in_quote = squash(v) in squash(q)
        # Table cells can split label and value; accept when both appear and the value is in the text layer.
        ok = (quote_found and value_in_quote) or (squash(v) in T and value_in_quote and
                                                    all(part in T for part in squash(q).split(squash(v)) if part.strip()))
        out.append({"check": f"{field} quote found in document", "ok": ok, "field": field,
                    "detail": f"read '{v}' from \"{q}\""})
    return out


# ---------------------------------------------------------------- stage 4: required fields

def missing_required(n: dict) -> list[str]:
    missing = []
    for f in config()["required_fields"]:
        if f == "line_items":
            if not n["lines"] or any(l["qty"] is None or l["unit_price"] is None for l in n["lines"]):
                missing.append("line_items")
        elif f == "invoice_date":
            if n["invoice_date"] is None:
                missing.append("invoice_date")
        elif n.get(f) in (None, ""):
            missing.append(f)
    return missing


# ---------------------------------------------------------------- stage 7: vendor

LEGAL_SUFFIXES = r"\b(pvt|private|ltd|limited|llp|inc|co|company|corp|corporation|the)\b"


def norm_name(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    s = re.sub(LEGAL_SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def vendor_score(invoice_name: str, vendor: dict) -> tuple[float, str]:
    """Best rapidfuzz score against the legal name and known aliases."""
    best, best_name = 0.0, ""
    for candidate in [vendor["legal_name"], *vendor["aliases"]]:
        score = fuzz.token_sort_ratio(norm_name(invoice_name), norm_name(candidate))
        if score > best:
            best, best_name = score, candidate
    return best, best_name


# ---------------------------------------------------------------- stage 8: dates, tax, price bands

def date_check(inv_date: date, po_date: date) -> tuple[bool, str]:
    """The invoice date must fall in [PO date, PO date + N months]."""
    cfg = config()["date_sanity"]
    latest = po_date + relativedelta(months=cfg["max_months_after_po"])
    if not cfg["allow_before_po_date"] and inv_date < po_date:
        return False, f"invoice date {inv_date} is before the PO date {po_date}"
    if inv_date > latest:
        return False, (f"invoice date {inv_date} is more than {cfg['max_months_after_po']} months after "
                       f"the PO date {po_date} (latest allowed {latest})")
    return True, f"invoice date {inv_date} is within the PO window ({po_date} to {latest})"


def tax_rate_check(n: dict) -> tuple[bool, float | None, str]:
    cfg = config()["tax"]
    base = taxable_value(n)
    tax = sum(t["amount"] or 0 for t in n["taxes"])
    if not base:
        return False, None, "no taxable value to compute a rate from"
    implied = tax / base * 100
    nearest = min(cfg["valid_gst_rates"], key=lambda r: abs(r - implied))
    ok = abs(implied - nearest) <= cfg["rounding_tolerance_pct"]
    return ok, implied, f"implied GST rate {implied:.2f}% " + (f"= valid rate {nearest}%" if ok else "is not a valid GST rate")


def price_band(invoiced: float, expected: float, qty: float = 1.0) -> dict:
    """Classify a unit price (or a total, with qty=1) against the PO value using config tolerances.

    band: exact | over_ok | over_review | over_reject | under_ok | under_review
    """
    over_cfg, under_cfg = config()["tolerance_over"], config()["tolerance_under"]
    diff_unit = invoiced - expected
    pct = diff_unit / expected * 100 if expected else 0.0
    diff_abs = diff_unit * qty
    if abs(diff_abs) < 0.01:
        band = "exact"
    elif diff_abs > 0:
        if pct <= over_cfg["approve_pct"] and diff_abs <= over_cfg["approve_abs_inr"]:
            band = "over_ok"
        elif pct <= over_cfg["review_max_pct"]:
            band = "over_review"
        else:
            band = "over_reject"
    else:
        if -pct <= under_cfg["approve_pct"] and -diff_abs <= under_cfg["approve_abs_inr"]:
            band = "under_ok"
        else:
            band = "under_review"
    return {"band": band, "pct": round(pct, 2), "diff_abs": round(diff_abs, 2)}
