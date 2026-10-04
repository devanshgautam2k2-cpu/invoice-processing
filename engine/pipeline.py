"""The nine-stage invoice pipeline. The LLM reads, code decides, the human arbitrates.

Each stage logs to the audit trail and to an optional on_event callback (the live run view).
Findings carry a reason code; the final outcome is the most severe one found, and nothing
is sent back to a vendor unless reading confidence is High.
"""

import json
import re
import threading
from collections import defaultdict
from datetime import date

from rapidfuzz.distance import Levenshtein

from . import checks, db, fx, llm
from .checks import inr
from .pdf_reader import file_hash, source_type

REASONS = {
    "A1": ("approve", "All checks passed"),
    "A2": ("approve", "Small shortfall within 2% / Rs 10,000 (noted)"),
    "A3": ("approve", "Shortfall explained by a stated discount"),
    "S1": ("send_back", "Required field missing"),
    "S2": ("send_back", "Exact duplicate of an earlier invoice"),
    "S3": ("send_back", "Invoice number already approved for this vendor"),
    "S4": ("send_back", "PO already fully invoiced"),
    "S5": ("send_back", "Quantity exceeds what remains on the PO line"),
    "S6": ("send_back", "Price or total more than 7% over the PO"),
    "S7": ("send_back", "Vendor does not match the PO"),
    "S8": ("send_back", "Line items do not match the PO"),
    "S9": ("send_back", "PO number not found"),
    "S10": ("send_back", "Unchanged resend of a rejected invoice"),
    "H1": ("human_review", "Overbilled above the approve limit, up to 7%"),
    "H2": ("human_review", "Underbilled beyond 2% / Rs 10,000 with no stated discount"),
    "H3": ("human_review", "Low reading confidence; shows what the AI inferred"),
    "H5": ("human_review", "Totals do not reconcile"),
    "H6": ("human_review", "Vendor name is a close but unclear match"),
    "H7": ("human_review", "Bank details differ from vendor records"),
    "H8": ("human_review", "PO not found; candidate PO from the same vendor matches"),
    "H9": ("human_review", "Near-duplicate invoice number"),
    "H10": ("human_review", "Invoice date outside the allowed window"),
    "H11": ("human_review", "Implied tax rate is not a valid GST rate"),
    "H12": ("human_review", "A field could not be verified against the document text"),
    "H13": ("human_review", "AI service unavailable; invoice could not be read"),
    "H14": ("human_review", "Not a tax invoice (quotation, proforma, credit note, ...); read and passed to a human"),
    "H15": ("human_review", "Unclassified: outside what the system understands; needs a person"),
    "H16": ("human_review", "Foreign currency: converted to INR for the checks; a person gives final approval"),
    "H17": ("human_review", "Tax differs from the PO's tax rate; a person checks it with the vendor"),
}
SEVERITY = {"approve": 1, "human_review": 2, "send_back": 3}
OUTCOME_STATUS = {"approve": "approved", "human_review": "human_review", "send_back": "sent_back"}

REVIEW_REASONS = {
    "R-A1": ("approve", "Figures verified as correct"),
    "R-A2": ("approve", "Price variance accepted (agreed change)"),
    "R-A3": ("approve", "Discount or credit confirmed with the vendor"),
    "R-A4": ("approve", "Vendor identity confirmed (name variant)"),
    "R-R1": ("reject", "Overbilled"),
    "R-R2": ("reject", "Wrong or missing items"),
    "R-R3": ("reject", "Duplicate"),
    "R-R4": ("reject", "Vendor or bank details could not be verified"),
    "R-R5": ("reject", "Incorrect invoice details"),
    "OTHER": (None, "Other (details required)"),
}
APPROVED = ("approved", "reviewer_approved")
REJECTED = ("sent_back", "reviewer_rejected")

STAGES = ["1 Receive", "2 Read", "3 Reading confidence", "4 Required fields", "5 Duplicates",
          "6 PO", "7 Vendor", "8 Lines", "9 Decide", "Note", "Update"]

_lock = threading.Lock()  # strictly one invoice at a time


class Stop(Exception):
    """A stage found something that makes later stages meaningless."""


class Wait(Exception):
    """The PO is on hold behind another invoice's review: park this one and rerun later."""

    def __init__(self, waiting_on: int, message: str):
        super().__init__(message)
        self.waiting_on, self.message = waiting_on, message


class Run:
    def __init__(self, conn, invoice_id: int, on_event=None):
        self.c, self.id, self.on_event = conn, invoice_id, on_event
        self.findings: list[dict] = []
        self.confidence = "High"
        self.confidence_reasons: list[str] = []
        self.facts: dict = {}
        self.force_human = False  # a non-invoice: never sent back, a human decides

    def emit(self, stage: str, status: str, message: str, data=None):
        db.log(self.c, self.id, stage, status, message, data)
        self.c.commit()
        if self.on_event:
            self.on_event(stage, status, message, data)

    def flag(self, code: str, stage: str, message: str, certain: bool = False, **data):
        """certain=True: the finding does not depend on reading quality (e.g. byte-identical file)."""
        self.findings.append({"code": code, "outcome": REASONS[code][0], "stage": stage,
                              "message": message, "certain": certain, **data})

    def low_confidence(self, reason: str):
        self.confidence = "Low"
        self.confidence_reasons.append(reason)


# ---------------------------------------------------------------- queue

def enqueue(filename: str, pdf: bytes) -> int:
    with db.tx() as c:
        cur = c.execute("INSERT INTO invoices (filename, file_hash, pdf, received_at) VALUES (?,?,?,?)",
                        (filename, file_hash(pdf), pdf, db.now()))
        inv_id = cur.lastrowid
        c.execute("UPDATE invoices SET internal_id=? WHERE id=?", (f"INT-{inv_id:04d}", inv_id))
    return inv_id


def process_queue(on_event=None, on_invoice=None) -> list[dict]:
    """Process every queued invoice in arrival order, one at a time."""
    results = []
    with _lock:
        while True:
            with db.tx() as c:
                row = c.execute("SELECT id FROM invoices WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                break
            if on_invoice:
                on_invoice(row["id"])
            results.append(_process(row["id"], on_event))
    return results


# ---------------------------------------------------------------- the pipeline

def _process(invoice_id: int, on_event=None) -> dict:
    conn = db.connect()
    try:
        conn.execute("UPDATE invoices SET status='processing', run_count=run_count+1 WHERE id=?", (invoice_id,))
        conn.commit()
        run = Run(conn, invoice_id, on_event)
        inv = db.invoice(conn, invoice_id)
        ctx = {"inv": inv}
        try:
            for stage_fn in (stage_receive, stage_read, stage_document_type, stage_confidence, stage_required,
                             stage_currency, stage_duplicates, stage_po, stage_vendor, stage_lines):
                stage_fn(run, ctx)
        except Stop:
            pass
        except Wait as w:
            return park(run, ctx, w)
        except Exception as e:  # anything unforeseen: never left stuck, a person decides
            run.force_human = True
            run.flag("H15", "Error", f"Processing stopped on an unexpected error ({type(e).__name__}: {e}). "
                                     "Everything read so far is shown; please review the document directly.")
            run.emit("Error", "fail", f"Unexpected error: {type(e).__name__}: {e}")
        except llm.LLMUnavailable as e:
            run.flag("H13", "2 Read", f"The invoice could not be read: {e}")
            run.low_confidence("AI service unavailable")
            run.emit("2 Read", "fail", f"AI service unavailable: {e}")
        try:
            return decide_and_record(run, ctx)
        except Exception as e:  # even recording failed: park it with a person, never leave it "processing"
            conn.rollback()
            note = (f"Sent to human review (H15): the decision could not be recorded "
                    f"({type(e).__name__}: {e}). Please review the document directly.")
            conn.execute("UPDATE invoices SET status='human_review', reason_code='H15', note=?, decided_at=? WHERE id=?",
                         (note, db.now(), invoice_id))
            db.log(conn, invoice_id, "Error", "fail", note)
            conn.commit()
            return {"invoice_id": invoice_id, "internal_id": ctx["inv"]["internal_id"], "outcome": "human_review",
                    "reason_code": "H15", "note": note, "confidence": "Low"}
    finally:
        conn.close()


def stage_receive(run: Run, ctx: dict):
    inv = ctx["inv"]
    if inv["run_count"] > 1:
        run.emit("1 Receive", "info", f"Rerun #{inv['run_count'] - 1}: the review this invoice was waiting on is decided")
    run.emit("1 Receive", "pass", f"Received {inv['filename']} as {inv['internal_id']}",
             {"internal_id": inv["internal_id"], "file_hash": inv["file_hash"][:16] + "...",
              "received_at": inv["received_at"]})


def stage_read(run: Run, ctx: dict):
    inv = ctx["inv"]
    kind, text = source_type(inv["pdf"])
    ctx["source_type"], ctx["text_layer"] = kind, text
    run.emit("2 Read", "pass" if kind == "text" else "warn",
             "Text-based PDF: text read directly from the file" if kind == "text"
             else "Scanned image: no text layer, the AI reads the picture",
             {"source_type": kind, "text_chars": len(text.strip())})
    extracted, cached = llm.extract_invoice(inv["pdf"], inv["file_hash"], scanned=kind == "scanned")
    n = checks.normalise(extracted)
    ctx["extracted"], ctx["n"] = extracted, n
    run.c.execute(
        "UPDATE invoices SET source_type=?, extracted=?, vendor_invoice_number=?, invoice_date=?, po_number=?, grand_total=? WHERE id=?",
        (kind, json.dumps(extracted), n["invoice_number"], str(n["invoice_date"] or ""), n["po_number"],
         n["grand_total"], run.id))
    model = checks.config()["llm"]["scan_extraction_model" if kind == "scanned" else "extraction_model"]
    run.emit("2 Read", "pass", f"AI ({model}) extracted {len(n['lines'])} line(s)" + (" (cached)" if cached else ""), {
        "vendor": n["vendor_name"], "invoice_number": n["invoice_number"], "invoice_date": n["invoice_date_raw"],
        "po_number": n["po_number"], "grand_total": n["grand_total"],
        "lines": [{k: l[k] for k in ("description", "qty", "unit_price", "amount")} for l in n["lines"]],
    })


def stage_document_type(run: Run, ctx: dict):
    """Only a tax invoice is processed to a decision; anything else is read in full and handed to a human."""
    S = "2 Read"
    doc_type = ctx["extracted"].get("document_type") or "other"
    quote = (ctx["extracted"].get("document_type_quote") or "").strip()
    ctx["document_type"] = doc_type
    if doc_type in checks.config()["document_types"]["proceed"]:
        run.emit(S, "pass", f"Document type: {doc_type.replace('_', ' ')}" + (f' (read from "{quote}")' if quote else ""))
        return
    run.force_human = True
    if doc_type == "other":
        run.flag("H15", S, "The AI could not tell what kind of document this is"
                           + (f' (it says "{quote}")' if quote else "")
                           + ". Everything readable was collected; a person decides.")
        run.emit(S, "warn", "Unrecognised document type; it will go to a human")
        return
    run.flag("H14", S, f"This document is a {doc_type.replace('_', ' ')}, not a tax invoice"
                       + (f' (it says "{quote}")' if quote else "")
                       + ". Everything readable was collected; a person decides what to do with it.")
    run.emit(S, "warn", f"Not a tax invoice: {doc_type.replace('_', ' ')}"
                        + (f' (read from "{quote}")' if quote else "") + "; it will go to a human, never back to the vendor")


def stage_confidence(run: Run, ctx: dict):
    S = "3 Reading confidence"
    n = ctx["n"]
    if ctx["source_type"] == "scanned":
        run.low_confidence("scanned image, not a text PDF")
        run.flag("H3", S, "Scanned invoice: figures were read from an image and cannot be verified "
                          "against a text layer; please confirm what the AI read.")
    arith = checks.arithmetic_checks(n)
    failed = [a for a in arith if not a["ok"]]
    for a in arith:
        run.emit(S, "pass" if a["ok"] else "fail", f"{a['check']}: {a['detail']}")
    if failed:
        run.low_confidence("the invoice's own numbers do not add up")
        run.flag("H5", S, "; ".join(f"{a['check']} ({a['detail']})" for a in failed))
    if ctx["source_type"] == "text":
        quotes = checks.quote_checks(ctx["extracted"], ctx["text_layer"])
        bad = [q for q in quotes if not q["ok"]]
        for q in quotes:
            run.emit(S, "pass" if q["ok"] else "fail", f"{q['check']}: {q['detail']}")
        if bad:
            run.low_confidence("some fields could not be found in the document text")
            run.flag("H12", S, "Could not verify: " + ", ".join(q["field"] for q in bad))
    notes = (ctx["extracted"].get("reader_notes") or "").strip()
    if notes:
        run.emit(S, "info", f"Reader notes: {notes}")
        if ctx["source_type"] == "text":  # a scan is already H3; a text PDF the AI found hard to read is unusual
            run.force_human = True
            run.flag("H15", S, f"The AI flagged this text PDF as hard to read: {notes}")
    run.emit(S, "pass" if run.confidence == "High" else "warn",
             f"Reading confidence: {run.confidence}" +
             (f" ({'; '.join(run.confidence_reasons)})" if run.confidence_reasons else
              " (text PDF, totals reconcile, every key field found in the document text)"),
             {"confidence": run.confidence})


def stage_required(run: Run, ctx: dict):
    S = "4 Required fields"
    missing = checks.missing_required(ctx["n"])
    if not missing:
        run.emit(S, "pass", "All 7 required fields present")
        return
    code = "S1" if run.confidence == "High" else "H3"
    run.flag(code, S, "Missing required field(s): " + ", ".join(missing), missing=missing)
    run.emit(S, "fail", "Missing: " + ", ".join(missing))
    if {"po_number", "line_items"} & set(missing):
        raise Stop


def _resolve_vendor(run: Run, n: dict) -> str | None:
    """Vendor from the master list by GSTIN, else by best name score (invoice numbers are unique per vendor)."""
    vendors = [db.vendor(run.c, r["vendor_id"]) for r in run.c.execute("SELECT vendor_id FROM vendors")]
    for v in vendors:
        if n["vendor_gstin"] and n["vendor_gstin"] == v["gstin"]:
            return v["vendor_id"]
    scored = [(checks.vendor_score(n["vendor_name"] or "", v)[0], v["vendor_id"]) for v in vendors]
    best = max(scored)
    return best[1] if best[0] >= checks.config()["vendor_match"]["fuzzy_review"] else None


def _same_contents(a: dict, b: dict) -> bool:
    """Plain equality of the normalised fields that matter (not a fuzzy percentage)."""
    def key(n):
        return (n["invoice_number"], n["grand_total"],
                tuple((l["description"], l["qty"], l["unit_price"]) for l in n["lines"]))
    return key(a) == key(b)


def stage_currency(run: Run, ctx: dict):
    """INR proceeds as is. Another currency is converted to INR so every check can run, then a person decides."""
    S = "2 Read"
    n = ctx["n"]
    raw = ((ctx["extracted"].get("currency") or {}).get("value") or "").strip()
    code = fx.currency_code(raw)
    if code == "INR":
        return
    run.force_human = True
    if code is None:
        run.flag("H15", S, f"Currency '{raw}' not recognised; amounts were not converted. Please check the currency.")
        run.emit(S, "warn", f"Unrecognised currency '{raw}'")
        return
    try:
        rate, rate_date, source = fx.rate_to_inr(code, n["invoice_date"])
    except fx.FxUnavailable as e:
        run.flag("H15", S, f"Invoice is in {code} and no exchange rate was available ({e}); amounts not converted.")
        run.emit(S, "warn", f"No {code} rate available")
        return
    original_total = n["grand_total"]
    fx.convert(n, rate)
    ctx["fx"] = {"currency": code, "rate": rate, "rate_date": rate_date, "source": source,
                 "original_total": original_total}
    run.c.execute("UPDATE invoices SET grand_total=? WHERE id=?", (n["grand_total"], run.id))
    total_txt = (f"{code} {original_total:,.2f} = {inr(n['grand_total'])}" if original_total is not None else "")
    run.flag("H16", S, f"Invoice is in {code}. Converted at 1 {code} = Rs {rate:,.4f} ({source}, {rate_date}). "
                       f"{total_txt}. All checks ran on the INR values; please give final approval.")
    run.emit(S, "warn", f"Foreign currency {code}: converted at {rate:,.4f} INR ({rate_date}); {total_txt}")


def stage_duplicates(run: Run, ctx: dict):
    S = "5 Duplicates"
    inv, n = ctx["inv"], ctx["n"]
    vendor_id = _resolve_vendor(run, n)
    run.c.execute("UPDATE invoices SET vendor_id=? WHERE id=?", (vendor_id, run.id))
    earlier = [dict(r) for r in run.c.execute(
        "SELECT * FROM invoices WHERE id != ? AND status NOT IN ('queued','processing') ORDER BY id", (run.id,))]

    # 1. Byte-identical file: certain, whatever the reading quality
    for e in earlier:
        if e["file_hash"] == inv["file_hash"]:
            if e["status"] in REJECTED:
                run.flag("S10", S, f"Unchanged resend of {e['internal_id']}, which was rejected "
                                   f"({e['reason_code']}: {_reason_text(e['reason_code'])}). Same reason applies.",
                         certain=True, original=e["internal_id"])
            else:
                run.flag("S2", S, f"Exact duplicate: identical file to {e['internal_id']} ({e['filename']}, "
                                  f"status {e['status'].replace('_', ' ')})", certain=True, original=e["internal_id"])
            run.emit(S, "fail", f"Same file hash as {e['internal_id']}")
            raise Stop
    run.emit(S, "pass", "No earlier invoice with the same file fingerprint")

    if not vendor_id or not n["invoice_number"]:
        run.emit(S, "info", "Vendor not identified in the vendor master; number checks skipped")
        return

    # 2. Same vendor + invoice number
    norm = lambda x: (x or "").replace(" ", "").upper()
    same_no = [e for e in earlier if e["vendor_id"] == vendor_id and norm(e["vendor_invoice_number"]) == norm(n["invoice_number"])]
    for e in same_no:
        if e["status"] in APPROVED:
            if e["grand_total"] == n["grand_total"]:
                run.flag("S2", S, f"Duplicate: same vendor, invoice number {n['invoice_number']} and amount "
                                  f"{inr(n['grand_total'])} as approved {e['internal_id']}", original=e["internal_id"])
            else:
                run.flag("S3", S, f"An invoice with number {n['invoice_number']} has already been approved "
                                  f"for this vendor ({e['internal_id']})", original=e["internal_id"])
            run.emit(S, "fail", f"Invoice number {n['invoice_number']} already approved as {e['internal_id']}")
            raise Stop
        if e["status"] in ("human_review", "waiting"):
            raise Wait(e["id"] if e["status"] == "human_review" else e["waiting_on"],
                       f"An earlier invoice with the same number ({e['internal_id']}) is awaiting a decision")
        if e["status"] in REJECTED:
            if e["extracted"] and _same_contents(checks.normalise(json.loads(e["extracted"])), n):
                run.flag("S10", S, f"Unchanged resend of rejected {e['internal_id']} "
                                   f"({e['reason_code']}: {_reason_text(e['reason_code'])}). Same reason applies.",
                         original=e["internal_id"])
                run.emit(S, "fail", f"Same contents as rejected {e['internal_id']}")
                raise Stop
            run.emit(S, "info", f"Reuses the number of rejected {e['internal_id']}; contents changed, "
                                "so treated as a corrected version")
    if not same_no:
        run.emit(S, "pass", f"Invoice number {n['invoice_number']} not seen before for this vendor")

    # 3. Near-duplicate: number one edit away, same vendor, amount and date
    max_dist = checks.config()["near_duplicate"]["max_invoice_number_edit_distance"]
    for e in earlier:
        if (e["vendor_id"] == vendor_id and e["grand_total"] == n["grand_total"]
                and e["invoice_date"] == str(n["invoice_date"])
                and 0 < Levenshtein.distance(norm(e["vendor_invoice_number"]), norm(n["invoice_number"])) <= max_dist):
            run.flag("H9", S, f"Near-duplicate of {e['internal_id']}: invoice number {e['vendor_invoice_number']} vs "
                              f"{n['invoice_number']}, same vendor, amount and date", original=e["internal_id"])
            run.emit(S, "warn", f"Near-duplicate of {e['internal_id']}")

    # 4. Same items and amount as an approved invoice under a new number: note only
    for e in earlier:
        if (e["status"] in APPROVED and e["vendor_id"] == vendor_id and e["grand_total"] == n["grand_total"]
                and e["extracted"]):
            other = checks.normalise(json.loads(e["extracted"]))
            if [(l["qty"], l["unit_price"]) for l in other["lines"]] == [(l["qty"], l["unit_price"]) for l in n["lines"]]:
                run.emit(S, "info", f"Same items and amount as {e['internal_id']} (different number); "
                                    "processed normally, noted for the reviewer")


def _reason_text(code: str | None) -> str:
    return (REASONS.get(code) or REVIEW_REASONS.get(code) or ("", code or "unknown"))[1]


def stage_po(run: Run, ctx: dict):
    S = "6 PO"
    n = ctx["n"]
    p = db.po(run.c, n["po_number"])
    if not p and n["po_number"]:
        p = _po_by_digits(run, n["po_number"])
        if p:
            n["po_number"] = p["po_id"]
    if not p:
        candidates = _candidate_pos(run, ctx)
        if candidates:
            ids = [c["po_id"] for c in candidates]
            ctx["candidate_pos"], ctx["allocations"] = ids, candidates[0]["allocations"]
            which = (f"{ids[0]} from the same vendor matches all items and amounts within tolerance"
                     if len(ids) == 1 else
                     f"{len(ids)} POs from the same vendor match all items and amounts ({', '.join(ids)})")
            run.flag("H8", S, f"PO number {n['po_number']} not found. {which}; possible typo in the PO number. "
                              "Please confirm the PO before approving.", candidates=ids)
            run.emit(S, "warn", f"PO {n['po_number']} not found; candidate: {', '.join(ids)}")
        else:
            run.flag("S9", S, f"PO number {n['po_number']} not found")
            run.emit(S, "fail", f"PO {n['po_number']} not found, and no open PO from this vendor matches the invoice")
        raise Stop
    ctx["po"] = p
    if p["status"] == "On hold" and p["hold_invoice_id"] != run.id:
        holder = db.invoice(run.c, p["hold_invoice_id"])
        raise Wait(p["hold_invoice_id"], f"{p['po_id']} is on hold since {db.fmt_ts(p['on_hold_since'])} "
                                         f"while {holder['internal_id']} is in human review")
    if p["status"] == "Fully invoiced":
        run.flag("S4", S, f"We already have invoices covering {p['po_id']} in full.")
        run.emit(S, "fail", f"{p['po_id']} is already fully invoiced")
        raise Stop
    run.emit(S, "pass", f"{p['po_id']} found, status {p['status']}", {
        "po_date": p["po_date"],
        "lines": [{"line": l["line_id"], "description": l["description"], "ordered": l["qty_ordered"],
                   "remaining": l["qty_remaining"], "unit_price": l["unit_price"]} for l in p["lines"]]})


def _candidate_pos(run: Run, ctx: dict) -> list[dict]:
    """H8: the vendor's open POs whose lines this invoice fits: every line paired (LLM, from that PO's own
    line ids), quantity within what remains, unit price not more than 7% over. Code decides the fit."""
    n = ctx["n"]
    vendor_id = db.invoice(run.c, run.id)["vendor_id"]  # identified in stage 5
    if not vendor_id or not n["lines"] or any(l["qty"] is None or l["unit_price"] is None for l in n["lines"]):
        return []
    review_max = checks.config()["tolerance_over"]["review_max_pct"]
    found = []
    for row in run.c.execute("SELECT po_id FROM pos WHERE vendor_id=? AND status != 'Fully invoiced' ORDER BY po_id",
                             (vendor_id,)).fetchall():
        p = db.po(run.c, row["po_id"])
        if p["status"] == "On hold":
            continue  # under someone else's review: not offered as a candidate
        shortlist = [{"line_id": l["line_id"], "description": l["description"]} for l in p["lines"]]
        matches, _ = llm.match_lines([l["description"] for l in n["lines"]], shortlist)
        by_inv = {m["invoice_line"]: m["po_line_id"] for m in matches}
        lines = {l["line_id"]: l for l in p["lines"]}
        qty = defaultdict(float)
        fits, allocations = True, []
        for l in n["lines"]:
            lid = by_inv.get(l["n"], "UNMATCHED")
            if lid == "UNMATCHED":
                fits = False
                break
            qty[lid] += l["qty"]
            pct = (l["unit_price"] - lines[lid]["unit_price"]) / lines[lid]["unit_price"] * 100
            if pct > review_max:
                fits = False
                break
            allocations.append({"line_id": lid, "qty": l["qty"], "unit_price": l["unit_price"]})
        if fits and all(q <= lines[lid]["qty_remaining"] for lid, q in qty.items()):
            found.append({"po_id": p["po_id"], "allocations": allocations})
            run.emit("6 PO", "info", f"Candidate {p['po_id']}: every invoice line matches a line with enough "
                                     "remaining, prices within tolerance")
        else:
            run.emit("6 PO", "info", f"{p['po_id']} checked as a candidate: does not fit this invoice")
    return found


def _po_by_digits(run: Run, written: str) -> dict | None:
    """'P.O. No 1011', 'PO 1011', '1011' -> PO-1011: same digits, and the PO must belong to this invoice's vendor."""
    digits = re.sub(r"\D", "", written)
    if not digits:
        return None
    same = [r["po_id"] for r in run.c.execute("SELECT po_id FROM pos") if re.sub(r"\D", "", r["po_id"]) == digits]
    vendor_id = db.invoice(run.c, run.id)["vendor_id"]  # identified in stage 5
    if len(same) != 1:
        return None
    p = db.po(run.c, same[0])
    if p["vendor_id"] != vendor_id:
        run.emit("6 PO", "info", f"'{written}' has the digits of {p['po_id']}, but that PO belongs to another vendor; not matched")
        return None
    run.emit("6 PO", "info", f"PO written as '{written}' matched to {p['po_id']} (same digits, same vendor)")
    run.c.execute("UPDATE invoices SET po_number=? WHERE id=?", (p["po_id"], run.id))
    return p


def stage_vendor(run: Run, ctx: dict):
    S = "7 Vendor"
    n, p = ctx["n"], ctx["po"]
    v = db.vendor(run.c, p["vendor_id"])
    ctx["vendor"] = v
    vcfg = checks.config()["vendor_match"]
    score, matched = checks.vendor_score(n["vendor_name"] or "", v)
    learned = next((a for a in v["learned_aliases"] if a["alias"] == matched), None)
    if score >= vcfg["fuzzy_pass"] and learned:
        run.emit(S, "pass", f"Invoice vendor '{n['vendor_name']}' matches a confirmed alias of {v['legal_name']} "
                            f"(added by {learned['added_by']} on {db.fmt_ts(learned['added_at'])})")
    elif score >= vcfg["fuzzy_pass"]:
        run.emit(S, "pass", f"Invoice vendor '{n['vendor_name']}' matches PO vendor {v['legal_name']} "
                            f"(score {score:.0f} vs '{matched}')")
    elif score >= vcfg["fuzzy_review"]:
        run.flag("H6", S, f"Vendor '{n['vendor_name']}' is a close but unclear match for {v['legal_name']} (score {score:.0f})")
        run.emit(S, "warn", f"Close but unclear vendor match (score {score:.0f})")
    else:
        run.flag("S7", S, f"Invoice is from '{n['vendor_name']}' but {p['po_id']} belongs to {v['legal_name']}")
        run.emit(S, "fail", f"Vendor mismatch (score {score:.0f})")

    acct, ifsc = n["bank_account_number"], n["bank_ifsc"]
    if acct or ifsc:
        if acct == v["account_number"] and ifsc == v["ifsc"]:
            run.emit(S, "pass", f"Bank details match vendor master ({v['bank_name']}, a/c ending {acct[-4:]})")
        else:
            run.flag("H7", S, f"Bank details on the invoice (a/c {acct or '?'}, IFSC {ifsc or '?'}) differ from "
                              f"vendor records (a/c ending {v['account_number'][-4:]}, IFSC {v['ifsc']}). "
                              "Verify through a known vendor contact, not by replying to the invoice.")
            run.emit(S, "fail", "Bank details differ from vendor master")
    else:
        run.emit(S, "info", "No bank details on the invoice")

    if n["vendor_gstin"] and n["vendor_gstin"] != v["gstin"]:
        run.emit(S, "warn", f"GSTIN on invoice {n['vendor_gstin']} differs from vendor master {v['gstin']} "
                            "(GSTIN matching is informational in this version)")
    elif n["vendor_gstin"]:
        run.emit(S, "pass", f"GSTIN {n['vendor_gstin']} matches vendor master")


def stage_lines(run: Run, ctx: dict):
    S = "8 Lines"
    n, p = ctx["n"], ctx["po"]
    po_lines = {l["line_id"]: l for l in p["lines"]}

    # Date and tax sanity
    if n["invoice_date"] is None:
        run.emit(S, "info", "Date check skipped: no invoice date was read (already flagged as a missing field)")
    else:
        ok, msg = checks.date_check(n["invoice_date"], date.fromisoformat(p["po_date"]))
        run.emit(S, "pass" if ok else "fail", f"Date check: {msg}")
        if not ok:
            run.flag("H10", S, msg.capitalize())
    ok, implied, msg = checks.tax_rate_check(n)
    run.emit(S, "pass" if ok else "fail", f"Tax check: {msg}")
    if not ok:
        run.flag("H11", S, msg.capitalize())

    # Pair lines: the LLM may only answer with a PO line id from this shortlist, or UNMATCHED
    shortlist = [{"line_id": l["line_id"], "description": l["description"]} for l in p["lines"]]
    matches, cached = llm.match_lines([l["description"] for l in n["lines"]], shortlist)
    by_inv = {m["invoice_line"]: m for m in matches}
    grouped = defaultdict(list)
    for l in n["lines"]:
        m = by_inv.get(l["n"], {"po_line_id": "UNMATCHED", "reason": "no answer from line matcher"})
        if m["po_line_id"] == "UNMATCHED":
            code = "S8" if run.confidence == "High" else "H3"
            run.flag(code, S, f"Invoice line {l['n']} '{l['description']}' does not match any line on {p['po_id']}",
                     match_issue=True)
            run.emit(S, "fail", f"Line {l['n']} '{l['description']}' -> no PO line ({m['reason']})")
        else:
            grouped[m["po_line_id"]].append(l)
            run.emit(S, "pass", f"Line {l['n']} '{l['description']}' -> {m['po_line_id']} "
                                f"'{po_lines[m['po_line_id']]['description']}'" + (" (cached)" if cached else ""),
                     {"reason": m["reason"]})

    stated_discount_pcts = [d["percent"] for d in n["discounts"] if d["percent"]]
    line_results, allocations = [], []
    for line_id, inv_lines in grouped.items():
        pl = po_lines[line_id]
        qty = sum(l["qty"] for l in inv_lines)
        unit = inv_lines[0]["unit_price"]
        res = {"po_line": line_id, "description": pl["description"], "qty": qty, "remaining": pl["qty_remaining"],
               "unit_price": unit, "po_unit_price": pl["unit_price"], "tax_rate": pl["tax_rate"]}
        # Quantity within what remains
        if qty > pl["qty_remaining"]:
            run.flag("S5", S, f"This invoice bills {qty:g} x '{pl['description']}' ({line_id}), but only "
                              f"{pl['qty_remaining']:g} of the {pl['qty_ordered']:g} ordered are still unbilled "
                              f"({pl['qty_invoiced']:g} already invoiced)", line=line_id)
            run.emit(S, "fail", f"{line_id}: quantity {qty:g} exceeds remaining {pl['qty_remaining']:g}")
        else:
            run.emit(S, "pass", f"{line_id}: quantity {qty:g} within remaining {pl['qty_remaining']:g}")
        # Unit price vs PO price
        band = checks.price_band(unit, pl["unit_price"], qty)
        res.update(band)
        _flag_band(run, S, band, f"{line_id} unit price {inr(unit)} vs PO {inr(pl['unit_price'])}",
                   stated_discount_pcts, line=line_id)
        line_results.append(res)
        allocations.append({"line_id": line_id, "qty": qty, "unit_price": unit})

    # Invoice total vs expected total (invoiced qty x PO price, plus the PO's tax)
    if grouped and n["grand_total"] is not None:
        qty_on = {lid: sum(l["qty"] for l in ls) for lid, ls in grouped.items()}
        expected_taxable = sum(q * po_lines[lid]["unit_price"] for lid, q in qty_on.items())
        expected_tax = sum(q * po_lines[lid]["unit_price"] * po_lines[lid]["tax_rate"] / 100 for lid, q in qty_on.items())
        expected = expected_taxable + expected_tax
        ctx["expected_total"] = round(expected, 2)
        taxable = checks.taxable_value(n)
        tax = sum(t["amount"] or 0 for t in n["taxes"])
        po_rate = expected_tax / expected_taxable * 100 if expected_taxable else 0.0
        inv_rate = tax / taxable * 100 if taxable else 0.0
        if abs(inv_rate - po_rate) > checks.config()["tax"]["rounding_tolerance_pct"]:
            # A tax difference is never a send-back: compare the goods before tax, and a person checks the tax.
            tax_gap = tax - taxable * po_rate / 100
            run.force_human = True
            run.flag("H17", S, f"Invoice charges tax at {inv_rate:.2f}% ({inr(tax)}) where the PO's rate is {po_rate:g}% "
                               f"({inr(taxable * po_rate / 100)} on this taxable value): {inr(tax_gap)} "
                               f"{'more' if tax_gap > 0 else 'less'}. Please confirm the correct tax with the vendor.")
            run.emit(S, "warn", f"Tax {inv_rate:.2f}% vs PO {po_rate:g}%: compared before tax; a person checks the tax")
            band = checks.price_band(taxable, expected_taxable)
            _flag_band(run, S, band, f"Taxable value {inr(taxable)} vs expected {inr(expected_taxable)} (before tax)",
                       stated_discount_pcts, line="total")
        else:
            band = checks.price_band(n["grand_total"], expected)
            _flag_band(run, S, band, f"Invoice total {inr(n['grand_total'])} vs expected {inr(expected)}",
                       stated_discount_pcts, line="total")
    ctx["line_results"], ctx["allocations"] = line_results, allocations


def _flag_band(run: Run, stage: str, band: dict, what: str, discounts: list[float], line: str):
    b, pct, diff = band["band"], band["pct"], band["diff_abs"]
    detail = f"{what}: {pct:+.2f}% ({inr(diff)})"
    if b == "exact":
        run.emit(stage, "pass", f"{what}: exact match")
    elif b == "over_ok":
        run.emit(stage, "pass", f"{detail}, within the approve limit")
    elif b == "over_review":
        run.flag("H1", stage, f"{detail}: above the approve limit, within 7%", line=line)
        run.emit(stage, "warn", f"{detail}: needs human review")
    elif b == "over_reject":
        code = "S6" if run.confidence == "High" else "H3"
        msg = f"{detail}: more than 7% over" + ("" if code == "S6" else
                                               "; possible overbilling, but the figures could not be fully verified")
        run.flag(code, stage, msg, line=line, match_issue=True)
        run.emit(stage, "fail", msg)
    elif b == "under_ok":
        run.flag("A2", stage, f"{detail}: small shortfall, within tolerance", line=line)
        run.emit(stage, "pass", f"{detail}: small shortfall, within tolerance")
    else:  # under_review
        if any(abs(-pct - d) <= 0.1 for d in discounts):
            run.flag("A3", stage, f"{detail}: explained by a stated discount", line=line)
            run.emit(stage, "pass", f"{detail}: explained by a stated discount")
        else:
            run.flag("H2", stage, f"{detail}: undervalued beyond 2% / Rs 10,000 with no stated discount", line=line)
            run.emit(stage, "warn", f"{detail}: undervalued, needs human review")


# ---------------------------------------------------------------- decide, explain, record

def combine(findings: list[dict], confidence: str, force_human: bool = False) -> tuple[str, str]:
    """Most severe outcome wins; a send-back needs High reading confidence and a real tax invoice,
    otherwise a human sees it. Certain findings (a byte-identical file) are exempt."""
    if not findings:
        return "approve", "A1"
    effective = []
    for f in findings:
        outcome = f["outcome"]
        if outcome == "send_back" and (confidence != "High" or force_human) and not f.get("certain"):
            outcome = "human_review"
        effective.append((SEVERITY[outcome], outcome, f["code"]))
    top = max(e[0] for e in effective)
    outcome = next(e[1] for e in effective if e[0] == top)
    code = next(e[2] for e in effective if e[0] == top)
    return outcome, code


def decide_and_record(run: Run, ctx: dict) -> dict:
    outcome, code = combine(run.findings, run.confidence, run.force_human)
    run.emit("9 Decide", {"approve": "pass", "human_review": "warn", "send_back": "fail"}[outcome],
             f"Outcome: {outcome.replace('_', ' ').upper()} ({code}: {REASONS[code][1]})",
             {"findings": run.findings, "reading_confidence": run.confidence})

    n = ctx.get("n") or {}
    facts = {
        "invoice": ctx["inv"]["internal_id"],
        "vendor_invoice_number": n.get("invoice_number"),
        "vendor": n.get("vendor_name"),
        "po": n.get("po_number"),
        "invoice_total": inr(n["grand_total"]) if n.get("grand_total") is not None else None,
        "expected_total": inr(ctx["expected_total"]) if ctx.get("expected_total") is not None else None,
        "outcome": outcome,
        "reason_code": code,
        "reason": REASONS[code][1],
        "document_type": ctx.get("document_type"),
        "currency_conversion": ctx.get("fx"),
        "reading_confidence": run.confidence,
        "confidence_reasons": run.confidence_reasons,
        "issues": [{"code": f["code"], "message": f["message"]} for f in run.findings],
    }
    try:
        note, cached = llm.write_note(facts)
        run.emit("Note", "pass", "Decision note written" + (" (cached)" if cached else ""))
    except llm.LLMUnavailable:
        note = _template_note(facts)
        run.emit("Note", "warn", "AI unavailable; used the template note")

    decision = {"outcome": outcome, "reason_code": code, "findings": run.findings,
                "reading_confidence": run.confidence, "confidence_reasons": run.confidence_reasons,
                "line_results": ctx.get("line_results", []), "allocations": ctx.get("allocations", []),
                "expected_total": ctx.get("expected_total"), "vendor_id": (ctx.get("vendor") or {}).get("vendor_id"),
                "candidate_pos": ctx.get("candidate_pos", [])}
    run.c.execute(
        "UPDATE invoices SET status=?, reason_code=?, reading_confidence=?, vendor_id=COALESCE(?, vendor_id), decision=?, note=?, decided_at=? WHERE id=?",
        (OUTCOME_STATUS[outcome], code, run.confidence, decision["vendor_id"], json.dumps(decision), note,
         db.now(), run.id))

    if outcome == "approve":
        db.apply_allocations(run.c, run.id, decision["allocations"])
        db.refresh_po_status(run.c, ctx["po"]["po_id"])
        p = db.po(run.c, ctx["po"]["po_id"])
        run.emit("Update", "pass", f"Quantities added to {p['po_id']}; PO is now {p['status']}",
                 {l["line_id"]: f"{l['qty_invoiced']:g}/{l['qty_ordered']:g}" for l in p["lines"]})
    elif outcome == "human_review" and ctx.get("po"):
        db.set_hold(run.c, ctx["po"]["po_id"], run.id)
        run.emit("Update", "warn", f"{ctx['po']['po_id']} put On hold until a reviewer decides; "
                                   "later invoices on it will wait and rerun automatically")
    elif outcome == "human_review" and ctx.get("candidate_pos"):
        for po_id in ctx["candidate_pos"]:
            db.set_hold(run.c, po_id, run.id)
        run.emit("Update", "warn", f"Candidate PO(s) {', '.join(ctx['candidate_pos'])} put On hold until a reviewer "
                                   "confirms which PO this invoice belongs to")
    else:
        run.emit("Update", "info", "PO unchanged")
    run.c.commit()
    return {"invoice_id": run.id, "internal_id": ctx["inv"]["internal_id"], "outcome": outcome,
            "reason_code": code, "note": note, "confidence": run.confidence}


def _template_note(f: dict) -> str:
    head = {"approve": "Approved", "human_review": "Sent to human review", "send_back": "Sent back to the vendor"}
    issues = " ".join(i["message"].rstrip(".") + "." for i in f["issues"])
    return f"{head[f['outcome']]} ({f['reason_code']}: {f['reason']}). {issues}".strip()


# ---------------------------------------------------------------- waiting and human review

def park(run: Run, ctx: dict, w: Wait) -> dict:
    """Option B: wait for the human decision on the held PO, then rerun automatically."""
    holder = db.invoice(run.c, w.waiting_on)
    note = (f"Waiting: {w.message}. This invoice will be rerun automatically as soon as a reviewer "
            f"decides {holder['internal_id']}; no action needed on it now.")
    run.c.execute("UPDATE invoices SET status='waiting', waiting_since=COALESCE(waiting_since, ?), waiting_on=?, "
                  "note=? WHERE id=?", (db.now(), w.waiting_on, note, run.id))
    run.emit("6 PO", "warn", f"Waiting in queue: {w.message}", {"waiting_on": holder["internal_id"]})
    run.c.commit()
    return {"invoice_id": run.id, "internal_id": ctx["inv"]["internal_id"], "outcome": "waiting",
            "reason_code": None, "note": note, "confidence": run.confidence}


def alias_candidate(inv: dict) -> tuple[str, str] | None:
    """(vendor_id, name) a reviewer may confirm as an alias: only for an H6 (close but unclear name) invoice."""
    decision = json.loads(inv["decision"]) if isinstance(inv["decision"], str) else inv["decision"]
    extracted = json.loads(inv["extracted"]) if isinstance(inv["extracted"], str) else inv["extracted"]
    if not decision or not extracted or not decision.get("vendor_id"):
        return None
    if not any(f["code"] == "H6" for f in decision["findings"]):
        return None
    name = ((extracted.get("vendor_name") or {}).get("value") or "").strip()
    return (decision["vendor_id"], name) if name else None


def review(invoice_id: int, action: str, reason_code: str, details: str = "", reviewer: str = "AP reviewer",
           allocations: list[dict] | None = None, add_alias: bool = False,
           on_event=None, on_invoice=None) -> list[dict]:
    """Record a human decision, release the PO hold, and rerun invoices waiting behind it.

    Notes are required on every decision. On approval the reviewer states which PO lines the
    invoice bills (allocations: [{line_id, qty, unit_price}]); the PO then becomes partially or
    fully invoiced from those quantities. Default: the lines the system matched.

    add_alias (approval of an H6 invoice only): the reviewer confirms the invoice's vendor name as
    an alias of the PO's vendor. One alias, one vendor, logged and removable; later invoices
    under that name pass the vendor check. Never automatic.
    """
    if action not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")
    if reason_code not in REVIEW_REASONS:
        raise ValueError(f"unknown reason code {reason_code}")
    expected = REVIEW_REASONS[reason_code][0]
    if expected and expected != action:
        raise ValueError(f"{reason_code} is a reason to {expected}, not to {action}")
    if not details.strip():
        raise ValueError("Notes are required: say what you checked and why you decided this way")

    with _lock:
        with db.tx() as c:
            inv = db.invoice(c, invoice_id)
            if inv["status"] != "human_review":
                raise ValueError(f"{inv['internal_id']} is not awaiting review (status {inv['status']})")
            decision = json.loads(inv["decision"])
            applied = []
            if action == "approve":
                applied = [a for a in (allocations if allocations is not None else decision.get("allocations", []))
                           if a["qty"] > 0]
                if not applied:
                    raise ValueError("Choose the PO line(s) and quantities this invoice bills before approving")
                _validate_allocations(c, invoice_id, applied)
                db.apply_allocations(c, invoice_id, applied)
            alias = alias_candidate(inv) if add_alias else None
            if add_alias and (action != "approve" or not alias):
                raise ValueError("An alias can only be added when approving an invoice flagged H6 (close vendor name)")
            c.execute("INSERT INTO reviews (invoice_id, action, reason_code, details, reviewer, ts) VALUES (?,?,?,?,?,?)",
                      (invoice_id, action, reason_code, details, reviewer, db.now()))
            c.execute("UPDATE invoices SET status=?, decided_at=? WHERE id=?",
                      ("reviewer_approved" if action == "approve" else "reviewer_rejected", db.now(), invoice_id))
            msg = f"Reviewer {action}d ({reason_code}: {REVIEW_REASONS[reason_code][1]}) - {details}"
            if applied:
                msg += "; billed " + ", ".join(f"{a['qty']:g} on {a['line_id']}" for a in applied)
            if alias:
                db.add_alias(c, alias[0], alias[1], reviewer, invoice_id)
                msg += f"; confirmed '{alias[1]}' as an alias of {db.vendor(c, alias[0])['legal_name']}"
            touched = {db.line_po(c, a["line_id"]) for a in applied}
            for r in c.execute("SELECT po_id FROM pos WHERE hold_invoice_id=?", (invoice_id,)).fetchall():
                db.release_hold(c, r["po_id"])  # its own PO, or every H8 candidate it held
                touched.add(r["po_id"])
            for po_id in sorted(touched):
                db.refresh_po_status(c, po_id)
                msg += f"; {po_id} now {db.po(c, po_id)['status']}"
            db.log(c, invoice_id, "Review", "pass" if action == "approve" else "fail", msg)
            waiting = c.execute("SELECT id, internal_id FROM invoices WHERE status='waiting' AND waiting_on=?",
                                (invoice_id,)).fetchall()
            for w in waiting:
                c.execute("UPDATE invoices SET status='queued' WHERE id=?", (w["id"],))
                db.log(c, w["id"], "Queue", "info", f"Requeued: {inv['internal_id']} was decided by a reviewer")
    return process_queue(on_event=on_event, on_invoice=on_invoice)


def _validate_allocations(c, invoice_id: int, allocations: list[dict]):
    """Reviewer allocations must hit real lines of one PO, fit what remains, and not touch a PO held by another invoice."""
    pos = {db.line_po(c, a["line_id"]) for a in allocations}
    if None in pos:
        raise ValueError("Unknown PO line in the allocation")
    if len(pos) > 1:
        raise ValueError("An invoice can only bill lines of one PO")
    p = db.po(c, pos.pop())
    if p["status"] == "On hold" and p["hold_invoice_id"] != invoice_id:
        holder = db.invoice(c, p["hold_invoice_id"])
        raise ValueError(f"{p['po_id']} is on hold while {holder['internal_id']} is reviewed; decide that one first")
    lines = {l["line_id"]: l for l in p["lines"]}
    for a in allocations:
        line = lines[a["line_id"]]
        if a["qty"] > line["qty_remaining"]:
            raise ValueError(f"Cannot approve: {a['qty']:g} on {a['line_id']} exceeds the {line['qty_remaining']:g} remaining")
