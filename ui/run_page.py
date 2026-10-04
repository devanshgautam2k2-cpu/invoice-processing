"""Live run view: queue invoices and watch each stage as it runs."""

import streamlit as st

from engine import pipeline
from engine.checks import inr
from engine.config import ROOT
from ui.common import badge, load_invoices, reason_text, run_with_live_view

TEST_DIR = ROOT / "test_invoices"
EXT_DIR = TEST_DIR / "extended"
UNSEEN_DIR = TEST_DIR / "unseen"  # never tuned on, nothing cached: a live AI read


def page():
    st.title("Run invoices")
    st.write("Invoices are processed **strictly one at a time**, in arrival order. "
             "Each one runs through nine stages; the live view shows every check as it happens.")

    test_files = sorted(p.name for p in TEST_DIR.glob("*.pdf"))
    ext_files = sorted(p.name for p in EXT_DIR.glob("*.pdf"))
    unseen_files = sorted(p.name for p in UNSEEN_DIR.glob("*.pdf"))
    paths = ({n: TEST_DIR / n for n in test_files} | {n: EXT_DIR / n for n in ext_files}
             | {n: UNSEEN_DIR / n for n in unseen_files})
    c1, c2 = st.columns(2)
    with c1:
        uploads = st.file_uploader("Upload vendor invoice PDFs", type="pdf", accept_multiple_files=True)
    with c2:
        picked = st.multiselect("Or choose from the test set", test_files + ext_files + unseen_files,
                                help="u1-u6 are unseen invoices: new layouts, no cached AI results")
        st.caption("Expected outcomes for each test file are in `test_invoices/EXPECTED.md`.")

    b1, b2, b3 = st.columns([1, 1.6, 1.6])
    run_selected = b1.button("Process", type="primary", disabled=not (uploads or picked), width="stretch")
    run_all = b2.button(f"Run core demo set ({len(test_files)})", width="stretch",
                        help="Happy path, split invoice, hold and wait, scan, duplicate")
    run_ext = b3.button(f"Run edge-case set ({len(ext_files)})", width="stretch",
                        help="One invoice per remaining reason code. Run after the core set.")

    live = st.container()
    if run_selected or run_all or run_ext:
        names = test_files if run_all else ext_files if run_ext else picked
        for f in (uploads or []) if run_selected else []:
            pipeline.enqueue(f.name, f.getvalue())
        for name in names:
            pipeline.enqueue(name, paths[name].read_bytes())
        live.subheader("Live run")
        results = run_with_live_view(live, lambda ev, oi: pipeline.process_queue(on_event=ev, on_invoice=oi))
        st.session_state["last_run"] = [r["invoice_id"] for r in results]
        summary = {}
        for r in results:
            summary[r["outcome"]] = summary.get(r["outcome"], 0) + 1
        st.toast("Run complete: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in summary.items()))

    _results_section()


def _results_section():
    invoices = load_invoices()
    if not invoices:
        st.info("No invoices yet. Pick test invoices above, or run the full demo set.")
        return
    st.subheader("Latest results")
    last = set(st.session_state.get("last_run", []))
    for inv in invoices[:12]:
        with st.container(border=True):
            cols = st.columns([2.2, 2.4, 1.3, 1.6])
            new = " 🆕" if inv["id"] in last else ""
            cols[0].markdown(f"**{inv['internal_id']}**{new}  \n{inv['filename']}")
            cols[1].markdown(badge(inv["status"], inv["reason_code"]) + f"  \n{reason_text(inv['reason_code'])}")
            cols[2].markdown(f"PO  \n**{inv['po_number'] or '-'}**")
            cols[3].markdown(f"Total  \n**{inr(inv['grand_total']) if inv['grand_total'] else '-'}**")
            if inv["note"]:
                st.caption(inv["note"])
    if len(invoices) > 12:
        st.caption(f"Showing the 12 most recent of {len(invoices)}. Full history is on the Dashboard.")
