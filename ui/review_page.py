"""Human review: decide flagged invoices, see PO holds and the waiting queue."""

import pandas as pd
import streamlit as st

from engine import db, pipeline
from engine.pipeline import REVIEW_REASONS
from ui.common import invoice_detail, load_invoices, reason_text, run_with_live_view


def pending_count() -> int:
    with db.tx() as c:
        return c.execute("SELECT COUNT(*) FROM invoices WHERE status='human_review'").fetchone()[0]


def page():
    st.title("Human review")
    st.write("The system sends an invoice here only when a person has information it doesn't. "
             "Each one holds its PO; deciding it releases the hold and reruns any invoice waiting behind it.")

    holds_and_queue()

    pending = load_invoices("i.status='human_review'")[::-1]  # oldest first
    st.subheader(f"Awaiting a decision ({len(pending)})")
    if not pending:
        st.success("Nothing to review.", icon="✅")
        _recent_decisions()
        return

    labels = {inv["id"]: f"{inv['internal_id']} · {inv['vendor_name'] or '?'} · {inv['po_number'] or 'no PO'} · "
                         f"{inv['reason_code']} {reason_text(inv['reason_code'])}" for inv in pending}
    chosen = st.radio("Invoice", list(labels), format_func=labels.get, label_visibility="collapsed")
    inv = next(i for i in pending if i["id"] == chosen)

    left, right = st.columns([1, 1.25], gap="large")
    with left:
        from engine import pdf_reader
        st.image(pdf_reader.page_image(inv["pdf"]), caption=inv["filename"], width="stretch")
    with right:
        invoice_detail(inv, show_document=False)
    with st.container(border=True):
        decision_form(inv)

    _recent_decisions()


def decision_form(inv: dict):
    st.markdown("#### Decision")
    action = st.segmented_control("Action", ["Approve", "Reject"], default="Approve", key=f"act_{inv['id']}")
    action = (action or "Approve").lower()
    allocations = billing_editor(inv) if action == "approve" else None

    codes = [c for c, (a, _) in REVIEW_REASONS.items() if a in (action, None)]
    with st.form(f"review_{inv['id']}"):
        code = st.selectbox("Reason", codes, format_func=lambda c: f"{c} · {REVIEW_REASONS[c][1]}")
        details = st.text_area("Notes (required)", placeholder="What you checked, who you spoke to, why you decided this way")
        reviewer = st.text_input("Reviewer", value=st.session_state.get("reviewer", "AP reviewer"))
        submitted = st.form_submit_button(f"{action.capitalize()} {inv['internal_id']}", type="primary")
    if submitted:
        st.session_state["reviewer"] = reviewer
        live = st.container()
        try:
            results = run_with_live_view(live, lambda ev, oi: pipeline.review(
                inv["id"], action, code, details, reviewer, allocations=allocations, on_event=ev, on_invoice=oi))
        except ValueError as e:
            st.error(str(e))
            return
        msg = f"{inv['internal_id']} {action}d."
        if results:
            msg += " Reran " + ", ".join(f"{r['internal_id']} → {r['outcome'].replace('_', ' ')}" for r in results)
        st.session_state["flash"] = msg
        st.rerun()


def billing_editor(inv: dict) -> list[dict]:
    """Reviewer states what this invoice bills: PO and quantity per line. Prefilled with the system's matches."""
    st.markdown("**What this invoice bills**")
    matched = {a["line_id"]: a for a in (inv["decision"] or {}).get("allocations", [])}
    with db.tx() as c:
        exists = inv["po_number"] and db.po(c, inv["po_number"])
        if exists:
            po_id = inv["po_number"]
        else:
            rows = c.execute("""SELECT po_id, vendor_id, status FROM pos WHERE status != 'Fully invoiced'
                                ORDER BY (vendor_id = ?) DESC, po_id""", (inv["vendor_id"],)).fetchall()
            options = [r["po_id"] for r in rows]
            st.warning(f"The invoice's PO number ({inv['po_number'] or 'none'}) was not found. "
                       "Pick the PO it belongs to (the vendor's own POs are listed first).", icon="🔎")
            po_id = st.selectbox("PO", options, key=f"po_{inv['id']}") if options else None
        p = db.po(c, po_id) if po_id else None
    if not p:
        st.error("No open PO to bill against.")
        return []
    if not matched:
        st.caption("The system could not pair the invoice lines with this PO; enter the quantities yourself.")
    df = pd.DataFrame([{
        "Line": l["line_id"].rsplit("-", 1)[-1], "Item": l["description"], "Left": l["qty_remaining"],
        "PO price": l["unit_price"],
        "Bill qty": matched.get(l["line_id"], {}).get("qty", 0.0),
        "Bill price": matched.get(l["line_id"], {}).get("unit_price", l["unit_price"]),
        "_id": l["line_id"],
    } for l in p["lines"]])
    edited = st.data_editor(
        df, hide_index=True, width="stretch", key=f"alloc_{inv['id']}_{p['po_id']}",
        disabled=["Line", "Item", "Left", "PO price"], column_order=["Line", "Item", "Left", "PO price", "Bill qty", "Bill price"],
        column_config={"Line": st.column_config.TextColumn(width=40),
                       "Item": st.column_config.TextColumn(width="medium"),
                       "Left": st.column_config.NumberColumn(width=50),
                       "PO price": st.column_config.NumberColumn(width=70),
                       "Bill qty": st.column_config.NumberColumn(min_value=0, step=1, width=70,
                                                                 help="Quantity this invoice bills on the line"),
                       "Bill price": st.column_config.NumberColumn(min_value=0, format="%.2f", width=80)})
    allocations = [{"line_id": r["_id"], "qty": float(r["Bill qty"] or 0),
                    "unit_price": float(r["Bill price"] or 0)} for _, r in edited.iterrows()]
    over = [a["line_id"] for a, (_, r) in zip(allocations, edited.iterrows()) if a["qty"] > r["Left"]]
    after = [r["Left"] - a["qty"] for a, (_, r) in zip(allocations, edited.iterrows())]
    if over:
        st.error("More than remains on: " + ", ".join(over))
    elif not any(a["qty"] for a in allocations):
        st.info("Nothing billed yet. Enter quantities to approve.")
    else:
        state = "Fully invoiced" if all(x <= 0 for x in after) else "Partially invoiced"
        st.caption(f"On approval {p['po_id']} becomes **{state}**.")
    return allocations


def holds_and_queue():
    if msg := st.session_state.pop("flash", None):
        st.success(msg, icon="✅")
    with db.tx() as c:
        holds = c.execute("""
            SELECT p.po_id, v.legal_name AS vendor, p.on_hold_since, i.internal_id AS held_by, i.reason_code,
                   (SELECT COUNT(*) FROM invoices w WHERE w.status='waiting' AND w.waiting_on=p.hold_invoice_id) AS waiting,
                   (SELECT MIN(waiting_since) FROM invoices w WHERE w.status='waiting' AND w.waiting_on=p.hold_invoice_id) AS oldest_wait
            FROM pos p JOIN vendors v USING(vendor_id) LEFT JOIN invoices i ON i.id=p.hold_invoice_id
            WHERE p.status='On hold' ORDER BY p.on_hold_since""").fetchall()
        waiting = c.execute("""
            SELECT w.internal_id, w.filename, w.po_number, h.internal_id AS waiting_on, w.waiting_since
            FROM invoices w LEFT JOIN invoices h ON h.id=w.waiting_on
            WHERE w.status='waiting' ORDER BY w.waiting_since""").fetchall()

    st.subheader(f"POs on hold ({len(holds)})")
    if holds:
        st.dataframe(pd.DataFrame([{
            "PO": h["po_id"], "Vendor": h["vendor"], "Held by": f"{h['held_by']} ({h['reason_code']})",
            "On hold since": db.fmt_ts(h["on_hold_since"]), "Held for": db.age(h["on_hold_since"]),
            "Invoices waiting": h["waiting"], "Longest wait": db.age(h["oldest_wait"]) if h["oldest_wait"] else "-",
        } for h in holds]), hide_index=True, width="stretch")
    else:
        st.caption("No POs on hold.")
    st.subheader(f"Waiting queue ({len(waiting)})")
    if waiting:
        st.dataframe(pd.DataFrame([{
            "Invoice": w["internal_id"], "File": w["filename"], "PO": w["po_number"],
            "Waiting on": w["waiting_on"], "Waiting since": db.fmt_ts(w["waiting_since"]),
            "Waited": db.age(w["waiting_since"]),
        } for w in waiting]), hide_index=True, width="stretch")
        st.caption("These rerun automatically the moment the invoice they wait on is decided.")
    else:
        st.caption("Nothing waiting.")


def _recent_decisions():
    with db.tx() as c:
        rows = c.execute("""SELECT r.ts, i.internal_id, r.action, r.reason_code, r.details, r.reviewer
                            FROM reviews r JOIN invoices i ON i.id=r.invoice_id ORDER BY r.id DESC LIMIT 10""").fetchall()
    if rows:
        st.subheader("Recent reviewer decisions")
        st.dataframe(pd.DataFrame([{
            "When": db.fmt_ts(r["ts"]), "Invoice": r["internal_id"], "Decision": r["action"],
            "Reason": f"{r['reason_code']} · {reason_text(r['reason_code'])}", "Details": r["details"], "By": r["reviewer"],
        } for r in rows]), hide_index=True, width="stretch")
