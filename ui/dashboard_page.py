"""Dashboard: history, status and outputs across runs."""

import statistics
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from engine import db
from engine.checks import inr
from ui.common import STATUS_LABEL, invoice_detail, load_invoices, reason_text

BAR = "#2a78d6"  # single-series bars: one hue (reference palette, categorical slot 1)
DUPLICATE_CODES = {"S2", "S3", "S10"}
DECIDED = ("approved", "reviewer_approved", "sent_back", "reviewer_rejected", "human_review")


def caught_value(inv: dict) -> float:
    """Money the system stopped from going out unchecked: full value of duplicates,
    plus the over-price and over-quantity amounts (with tax) on flagged invoices."""
    if inv["status"] not in ("sent_back", "reviewer_rejected", "human_review"):
        return 0.0
    if inv["reason_code"] in DUPLICATE_CODES:
        return inv["grand_total"] or 0.0
    total = 0.0
    for r in (inv["decision"] or {}).get("line_results", []):
        tax = 1 + r.get("tax_rate", 0) / 100
        total += max(0.0, r["diff_abs"]) * tax
        total += max(0.0, r["qty"] - r["remaining"]) * r["po_unit_price"] * tax
    return round(total, 2)


def processing_seconds() -> list[float]:
    with db.tx() as c:
        rows = c.execute("""SELECT MIN(ts) AS a, MAX(ts) AS b FROM audit_events
                            WHERE stage NOT IN ('Review','Queue') GROUP BY invoice_id""").fetchall()
    return [(datetime.fromisoformat(r["b"]) - datetime.fromisoformat(r["a"])).total_seconds() for r in rows if r["a"]]


def bar_chart(df: pd.DataFrame, cat: str, val: str, title: str, sort: list[str] | None = None):
    base = alt.Chart(df).encode(
        y=alt.Y(f"{cat}:N", sort=sort or "-x", title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X(f"{val}:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=True, gridOpacity=0.3)),
        tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q")],
    )
    bars = base.mark_bar(color=BAR, cornerRadiusEnd=4, size=18)
    labels = base.mark_text(align="left", dx=4, color="#8a8a86").encode(text=f"{val}:Q")  # mid-grey: legible on light and dark
    chart = (bars + labels).properties(title=title, height=alt.Step(30))
    st.altair_chart(chart, width="stretch")


def page():
    st.title("Dashboard")
    invoices = load_invoices()
    if not invoices:
        st.info("No invoices processed yet. Go to Run invoices to start.")
        _po_section()
        return

    decided = [i for i in invoices if i["status"] in DECIDED]
    auto_ok = [i for i in invoices if i["status"] == "approved"]
    exceptions = [i for i in decided if i["status"] != "approved"]
    approved_value = sum(i["grand_total"] or 0 for i in invoices if i["status"] in ("approved", "reviewer_approved"))
    caught = sum(caught_value(i) for i in invoices)
    secs = processing_seconds()

    k = st.columns(3) + st.columns(3)
    k[0].metric("Invoices processed", len(decided) + sum(i["status"] == "waiting" for i in invoices))
    k[1].metric("Auto-approved", f"{len(auto_ok)}", f"{len(auto_ok) / len(decided) * 100:.0f}% straight through" if decided else None,
                delta_color="off")
    k[2].metric("Exception rate", f"{len(exceptions) / len(decided) * 100:.0f}%" if decided else "-",
                help="Share of decided invoices that were not auto-approved (sent back or human review)")
    k[3].metric("Value approved", inr(approved_value).replace(".00", ""))
    k[4].metric("Overbilling & duplicates caught", inr(caught).replace(".00", ""),
                help="Full value of duplicate invoices, plus over-price and over-quantity amounts (incl. tax) on "
                     "invoices sent back, rejected or in review")
    med = statistics.median(secs) if secs else None
    k[5].metric("Median time per invoice", "-" if med is None else ("< 1s" if med < 1 else f"{med:.0f}s"),
                help="From receipt to decision, including AI calls (cached calls are near-instant)")

    c1, c2 = st.columns([1, 1.4])
    with c1:
        order = ["Approved", "Approved by reviewer", "Human review", "Waiting", "Sent back", "Rejected by reviewer"]
        counts = pd.Series([STATUS_LABEL.get(i["status"], (i["status"],))[0] for i in invoices]).value_counts()
        df = pd.DataFrame({"Outcome": counts.index, "Invoices": counts.values})
        bar_chart(df, "Outcome", "Invoices", "Invoices by current status", sort=[o for o in order if o in counts.index])
    with c2:
        codes = pd.Series([f"{i['reason_code']} · {reason_text(i['reason_code'])}" for i in invoices if i["reason_code"]])
        if not codes.empty:
            vc = codes.value_counts()
            bar_chart(pd.DataFrame({"Reason": vc.index, "Invoices": vc.values}), "Reason", "Invoices",
                      "System decision reasons")

    st.subheader("Invoice history")
    hist = pd.DataFrame([{
        "Invoice": i["internal_id"], "File": i["filename"], "Vendor": (i["vendor_name"] or "?").replace(" Pvt Ltd", ""),
        "Vendor inv. no.": i["vendor_invoice_number"], "PO": i["po_number"], "Total (Rs)": i["grand_total"],
        "Status": STATUS_LABEL.get(i["status"], (i["status"],))[0], "Reason": i["reason_code"] or "",
        "Reading": i["reading_confidence"] or "", "Caught (Rs)": caught_value(i) or None,
        "Received": db.fmt_ts(i["received_at"]), "Decided": db.fmt_ts(i["decided_at"]), "Runs": i["run_count"],
    } for i in invoices])
    f1, f2 = st.columns([2, 3])
    status_filter = f1.multiselect("Status", sorted(hist["Status"].unique()), placeholder="All statuses")
    vendor_filter = f2.multiselect("Vendor", sorted(hist["Vendor"].unique()), placeholder="All vendors")
    mask = pd.Series(True, index=hist.index)
    if status_filter:
        mask &= hist["Status"].isin(status_filter)
    if vendor_filter:
        mask &= hist["Vendor"].isin(vendor_filter)
    view = hist[mask].reset_index(drop=True)
    sel = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                       column_config={"Total (Rs)": st.column_config.NumberColumn(format="%,.0f"),
                                      "Caught (Rs)": st.column_config.NumberColumn(format="%,.0f")})
    st.caption("Select a row to see the full decision, what the AI read, and the audit trail.")
    if sel.selection.rows:
        chosen = view.iloc[sel.selection.rows[0]]["Invoice"]
        inv = next(i for i in invoices if i["internal_id"] == chosen)
        with st.container(border=True):
            invoice_detail(inv)

    _po_section()


def _po_section():
    st.subheader("Purchase orders")
    with db.tx() as c:
        rows = c.execute("""
            SELECT p.po_id, v.legal_name AS vendor, p.status, p.on_hold_since,
                   SUM(l.qty_invoiced * l.unit_price) AS billed, SUM(l.qty_ordered * l.unit_price) AS ordered,
                   GROUP_CONCAT(l.description || ': ' || CAST(l.qty_invoiced AS INT) || '/' || CAST(l.qty_ordered AS INT), ' · ') AS lines
            FROM pos p JOIN vendors v USING(vendor_id) JOIN po_lines l USING(po_id)
            GROUP BY p.po_id ORDER BY p.po_id""").fetchall()
    st.dataframe(pd.DataFrame([{
        "PO": r["po_id"], "Vendor": r["vendor"], "Status": r["status"],
        "Billed": r["billed"] / r["ordered"], "Billed value (Rs, ex-tax)": r["billed"], "Lines billed / ordered": r["lines"],
        "On hold for": db.age(r["on_hold_since"]) if r["on_hold_since"] else "",
    } for r in rows]), hide_index=True, width="stretch", column_config={
        "Billed": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
        "Billed value (Rs, ex-tax)": st.column_config.NumberColumn(format="%,.0f"),
    })
