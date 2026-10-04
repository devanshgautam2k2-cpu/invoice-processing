"""Paths and the single config file. Every threshold lives in data/config.json."""

import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "llm_cache"
DB_PATH = Path(os.environ.get("INVOICE_DB_PATH", ROOT / "invoices.db"))


@lru_cache
def config() -> dict:
    return json.loads((DATA_DIR / "config.json").read_text())


def api_key() -> str | None:
    """Env var first, then Streamlit secrets (works both inside and outside Streamlit)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        import streamlit as st

        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    secrets = ROOT / ".streamlit" / "secrets.toml"
    if secrets.exists():
        return tomllib.loads(secrets.read_text()).get("ANTHROPIC_API_KEY")
    return None
