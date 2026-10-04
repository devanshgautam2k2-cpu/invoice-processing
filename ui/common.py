"""Shared UI pieces: badges, invoice detail view, live stage tracker."""

import json

import pandas as pd
import streamlit as st

from engine import checks, db, pdf_reader
from engine.checks import inr
from engine.pipeline import REASONS, REVIEW_REASONS

STATUS_LABEL = {
    "approved": ("Approved", "green", "✅"),
    "reviewer_approved": ("Approved by reviewer", "green", "✅"),
    "sent_back": ("Sent back", "red", "↩️"),
    "reviewer_rejected": ("Rejected by reviewer", "red", "⛔"),
    "human_review": ("Human review", "orange", "🧑‍💼"),
    "waiting": ("Waiting", "violet", "⏳"),
    "queued": ("Queued", "gray", "•"),
    "processing": ("Processing", "blue", "⚙️"),
}
EVENT_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️"}
PIPELINE_STAGES = ["1 Receive", "2 Read", "3 Reading confidence", "4 Required fields", "5 Duplicates",
                   "6 PO", "7 Vendor", "8 Lines", "9 Decide"]
STAGE_RANK = {"info": 0, "pass": 1, "warn": 2, "fail": 3}
STAGE_COLOUR = {"pass": "green", "warn": "orange", "fail": "red", "info": "blue"}
READING_CODES = {"H3", "H5", "H12", "H13"}


def badge(status: str, reason_code: str | None = None) -> str:
    label, colour, icon = STATUS_LABEL.get(status, (status, "gray", ""))
    code = f" `{reason_code}`" if reason_code else ""
    return f":{colour}-background[{icon} **{label}**]{code}"


def reason_text(code: str | None) -> str:
    if not code:
        return ""
    return (REASONS.get(code) or REVIEW_REASONS.get(code) or ("", code))[1]


def load_invoices(where: str = "1=1", params=()) -> list[dict]:
    with db.tx() as c:
        rows = c.execute(f"""
            SELECT i.*, v.legal_name AS vendor_name FROM invoices i
            LEFT JOIN vendors v ON v.vendor_id = i.vendor_id
            WHERE {where} ORDER BY i.id DESC""", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["decision"] = json.loads(d["decision"]) if d["decision"] else None
        d["extracted"] = json.loads(d["extracted"]) if d["extracted"] else None
        out.append(d)
    return out


def latest_review(invoice_id: int) -> dict | None:
    with db.tx() as c:
        r = c.execute("SELECT * FROM reviews WHERE invoice_id=? ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- live stage tracker

class StageTracker:
    """A row of stage chips plus a running log, updated as pipeline events arrive."""

    def __init__(self, container, title: str):
        self.box = container.container(border=True)
        self.box.markdown(f"**{title}**")
        self.chips_ph = self.box.empty()
        self.log = self.box.container(height=220)
        self.state: dict[str, str] = {}
        self._draw()

    def _draw(self):
        parts = []
        for s in PIPELINE_STAGES:
            st_ = self.state.get(s)
            label = s.split(" ", 1)[1]
            if st_ is None:
                parts.append(f":gray-background[{label}]")
            else:
                parts.append(f":{STAGE_COLOUR[st_]}-background[{EVENT_ICON[st_]} {label}]")
        self.chips_ph.markdown(" → ".join(parts))

    def event(self, stage, status, message, data):
        if stage in PIPELINE_STAGES:
            prev = self.state.get(stage)
            if prev is None or STAGE_RANK[status] >= STAGE_RANK[prev]:
                self.state[stage] = status
            self._draw()
        self.log.markdown(f"{EVENT_ICON.get(status, '')} `{stage}` {message}")


def run_with_live_view(container, work):
    """Call work(on_event, on_invoice) and draw one tracker per invoice as it is processed."""
    trackers = {}

    def on_invoice(invoice_id):
        with db.tx() as c:
            inv = db.invoice(c, invoice_id)
        rerun = " · rerun" if inv["run_count"] >= 1 else ""
        trackers["current"] = StageTracker(container, f"{inv['internal_id']} · {inv['filename']}{rerun}")

    def on_event(stage, status, message, data):
        if "current" in trackers:
            trackers["current"].event(stage, status, message, data)

    return work(on_event, on_invoice)


# ---------------------------------------------------------------- invoice detail

def match_summary(decision: dict) -> tuple[str, list[str]]:
    issues = [f"{f['code']}: {f['message']}" for f in decision["findings"]
              if f.get("match_issue") or (f["code"] not in READING_CODES and not f["code"].startswith("A"))]
    return ("Failed" if issues else "Passed"), issues


def indicators(inv: dict):
    d = inv["decision"]
    if not d:
        return
    conf = d["reading_confidence"]
    why = "; ".join(d["confidence_reasons"]) or "text PDF, totals reconcile, key fields found in the document text"
    match, issues = match_summary(d)
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown(f"**Reading:** :{'green' if conf == 'High' else 'orange'}-background[{conf}]")
        st.caption(f"Did we read it correctly? {why}")
    with c2.container(border=True):
        st.markdown(f"**Match vs PO:** :{'green' if match == 'Passed' else 'red'}-background[{match}]")
        st.caption("Is the invoice correct? " + (f"{len(issues)} issue(s): " + ", ".join(sorted({i.split(':')[0] for i in issues}))
                                                 + " (see Findings)" if issues else
                                                 "quantities, prices, vendor and totals all within tolerance"))


def fields_table(inv: dict) -> pd.DataFrame:
    ex = inv["extracted"]
    if not ex:
        return pd.DataFrame()
    verified = {}
    if inv["source_type"] == "text":
        for q in checks.quote_checks(ex, pdf_reader.text_layer(inv["pdf"])):
            verified[q["field"]] = "✅" if q["ok"] else "❌"
    rows = [{"Field": "document type", "Value read": (ex.get("document_type") or "-").replace("_", " "),
             "Read from": ex.get("document_type_quote") or "", "Verified in text": ""}]
    for f in ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "subtotal", "grand_total",
              "bank_name", "bank_account_number", "bank_ifsc", "buyer_name", "buyer_gstin"]:
        v = ex.get(f) or {}
        rows.append({"Field": f.replace("_", " "), "Value read": v.get("value") or "— not found —",
                     "Read from": v.get("quote") or "",
                     "Verified in text": verified.get(f, "n/a (scan)" if inv["source_type"] == "scanned" else "")})
    return pd.DataFrame(rows)


def lines_table(inv: dict) -> pd.DataFrame:
    d = inv["decision"]
    if not d or not d["line_results"]:
        return pd.DataFrame()
    band_label = {"exact": "Exact", "over_ok": "Over, within limit", "over_review": "Over, review band",
                  "over_reject": "Over > 7%", "under_ok": "Under, within limit", "under_review": "Under, review band"}
    return pd.DataFrame([{
        "PO line": r["po_line"], "Item": r["description"], "Invoiced qty": r["qty"],
        "Remaining before": r["remaining"], "Invoice price": inr(r["unit_price"]), "PO price": inr(r["po_unit_price"]),
        "Variance": f"{r['pct']:+.2f}%", "Result": band_label.get(r["band"], r["band"]) +
        (" · qty over" if r["qty"] > r["remaining"] else ""),
    } for r in d["line_results"]])


def audit_trail(invoice_id: int):
    with db.tx() as c:
        events = c.execute("SELECT ts, stage, status, message FROM audit_events WHERE invoice_id=? ORDER BY id",
                           (invoice_id,)).fetchall()
    for e in events:
        st.markdown(f"{EVENT_ICON.get(e['status'], '')} `{db.fmt_ts(e['ts'])[12:17]}` **{e['stage']}** · {e['message']}")


def invoice_detail(inv: dict, show_document: bool = True):
    st.markdown(f"### {inv['internal_id']} · {inv['vendor_invoice_number'] or inv['filename']}")
    st.markdown(badge(inv["status"], inv["reason_code"]) + f" {reason_text(inv['reason_code'])}")
    exp = (inv["decision"] or {}).get("expected_total")
    total = inv["grand_total"]
    var = f" ({(total - exp) / exp * 100:+.2f}% vs expected {inr(exp)})" if exp and total and abs(total - exp) >= 0.01 else ""
    st.markdown(f"**Vendor** {inv['vendor_name'] or '?'} · **PO** {inv['po_number'] or '-'} · "
                f"**Total** {inr(total) if total else '-'}{var}")
    if inv["note"]:
        st.info(inv["note"], icon="📝")
    rv = latest_review(inv["id"])
    if rv:
        st.success(f"Reviewer {rv['action']}d · {rv['reason_code']}: {reason_text(rv['reason_code'])}"
                   + (f" · “{rv['details']}”" if rv["details"] else "") + f" · {rv['reviewer']} · {db.fmt_ts(rv['ts'])}",
                   icon="🧑‍💼")
    indicators(inv)

    tabs = st.tabs(["Findings", "Lines vs PO", "What the AI read", "Audit trail"] + (["Document"] if show_document else []))
    with tabs[0]:
        d = inv["decision"]
        if d and d["findings"]:
            for f in d["findings"]:
                st.markdown(f"- `{f['code']}` {f['message']}")
        elif d:
            st.write("No issues found.")
        else:
            st.write(inv["note"] or "Not decided yet.")
    with tabs[1]:
        df = lines_table(inv)
        st.dataframe(df, hide_index=True, width="stretch") if not df.empty else st.write("No matched lines.")
    with tabs[2]:
        df = fields_table(inv)
        if not df.empty:
            st.dataframe(df, hide_index=True, width="stretch")
            lines = inv["extracted"].get("line_items", [])
            if lines:
                st.caption("Line items as read")
                st.dataframe(pd.DataFrame(lines), hide_index=True, width="stretch")
            notes = inv["extracted"].get("reader_notes")
            if notes:
                st.caption(f"Reader notes: {notes}")
    with tabs[3]:
        audit_trail(inv["id"])
    if show_document:
        with tabs[4]:
            st.image(pdf_reader.page_image(inv["pdf"]), width="stretch")
