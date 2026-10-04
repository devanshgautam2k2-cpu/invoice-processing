"""Streamlit UI for the PS-1 invoice processor.

Run locally:  streamlit run app.py
"""

import streamlit as st

from engine import db
from ui import dashboard_page, review_page, run_page

st.set_page_config(page_title="Invoice Processor", page_icon="🧾", layout="wide")


@st.cache_resource
def _init():
    db.init_db()
    return True


_init()

pending = review_page.pending_count()
nav = st.navigation([
    st.Page(run_page.page, title="Run invoices", icon=":material/play_circle:", default=True),
    st.Page(review_page.page, title=f"Human review ({pending})" if pending else "Human review",
            icon=":material/fact_check:", url_path="review"),
    st.Page(dashboard_page.page, title="Dashboard", icon=":material/monitoring:", url_path="dashboard"),
])

with st.sidebar:
    st.caption("Zamp PS-1 · The LLM reads, code decides, the human arbitrates.")
    if st.button("Reset demo data", help="Restore POs and vendors to their starting state and clear all invoices"):
        db.init_db(reset=True)
        st.session_state.clear()
        st.rerun()

nav.run()
