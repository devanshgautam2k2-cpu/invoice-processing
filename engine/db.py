"""SQLite storage: vendor master, POs with per-line billing, invoices, audit trail."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    aliases TEXT NOT NULL,            -- JSON list
    gstin TEXT,
    bank_name TEXT,
    account_number TEXT,
    ifsc TEXT,
    contact_email TEXT
);

CREATE TABLE IF NOT EXISTS pos (
    po_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    po_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Open',   -- Open | Partially invoiced | Fully invoiced | On hold
    on_hold_since TEXT,
    hold_invoice_id INTEGER                -- invoice whose review holds the PO
);

CREATE TABLE IF NOT EXISTS po_lines (
    line_id TEXT PRIMARY KEY,
    po_id TEXT NOT NULL REFERENCES pos(po_id),
    line_no INTEGER NOT NULL,
    description TEXT NOT NULL,
    qty_ordered REAL NOT NULL,
    unit_price REAL NOT NULL,
    tax_rate REAL NOT NULL,
    qty_invoiced REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_id TEXT UNIQUE,               -- INT-0001
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    pdf BLOB NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', -- queued | processing | approved | sent_back | human_review | waiting | reviewer_approved | reviewer_rejected
    reason_code TEXT,
    reading_confidence TEXT,               -- High | Low
    source_type TEXT,                      -- text | scanned
    vendor_id TEXT,
    vendor_invoice_number TEXT,
    invoice_date TEXT,
    po_number TEXT,
    grand_total REAL,
    extracted TEXT,                        -- JSON: raw LLM extraction
    decision TEXT,                         -- JSON: findings, line results, allocations
    note TEXT,                             -- plain-English explanation
    decided_at TEXT,
    waiting_since TEXT,                    -- set while parked behind a held PO
    waiting_on INTEGER,                    -- the invoice whose review holds that PO
    run_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reviews (       -- human decisions on human_review invoices
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    action TEXT NOT NULL,                  -- approve | reject
    reason_code TEXT NOT NULL,             -- R-A1..R-A4, R-R1..R-R5, OTHER
    details TEXT,
    reviewer TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (   -- invoice qty applied to PO lines on approval
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    line_id TEXT NOT NULL REFERENCES po_lines(line_id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendor_aliases (  -- names a reviewer confirmed for a vendor
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    alias TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    source_invoice_id INTEGER REFERENCES invoices(id),
    removed_by TEXT,
    removed_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices(id),
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,                  -- pass | fail | warn | info
    message TEXT NOT NULL,
    data TEXT                              -- JSON
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_ts(ts: str | None) -> str:
    """ISO UTC -> '03 Oct 2026, 23:41 IST'."""
    if not ts:
        return "-"
    return datetime.fromisoformat(ts).astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def age(ts: str | None) -> str:
    """Time since ts, e.g. '2h 05m' or '3d 4h'."""
    if not ts:
        return "-"
    secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds())
    d, rem = divmod(max(secs, 0), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(reset: bool = False):
    """Create tables; seed vendors and POs from data/ if empty (or when reset)."""
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with tx() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
            _seed(c)


def _seed(c: sqlite3.Connection):
    for v in json.loads((DATA_DIR / "vendors.json").read_text()):
        c.execute(
            "INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?)",
            (v["vendor_id"], v["legal_name"], json.dumps(v["aliases"]), v["gstin"],
             v["bank"]["bank_name"], v["bank"]["account_number"], v["bank"]["ifsc"], v["contact_email"]),
        )
    for po in json.loads((DATA_DIR / "pos_seed.json").read_text()):
        c.execute("INSERT INTO pos (po_id, vendor_id, po_date, currency) VALUES (?,?,?,?)",
                  (po["po_id"], po["vendor_id"], po["po_date"], po["currency"]))
        for n, l in enumerate(po["lines"], 1):
            c.execute(
                "INSERT INTO po_lines (line_id, po_id, line_no, description, qty_ordered, unit_price, tax_rate) VALUES (?,?,?,?,?,?,?)",
                (l["line_id"], po["po_id"], n, l["description"], l["qty"], l["unit_price"], l["tax_rate"]),
            )


# ---------------------------------------------------------------- reads

def vendor(c, vendor_id: str) -> dict | None:
    r = c.execute("SELECT * FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["aliases"] = json.loads(d["aliases"])
    d["learned_aliases"] = [dict(a) for a in c.execute(
        "SELECT * FROM vendor_aliases WHERE vendor_id=? AND removed_at IS NULL ORDER BY id", (vendor_id,))]
    d["aliases"] += [a["alias"] for a in d["learned_aliases"]]
    return d


def add_alias(c, vendor_id: str, alias: str, added_by: str, invoice_id: int | None) -> int:
    existing = c.execute("SELECT id FROM vendor_aliases WHERE vendor_id=? AND alias=? AND removed_at IS NULL",
                         (vendor_id, alias)).fetchone()
    if existing:
        return existing["id"]
    cur = c.execute("INSERT INTO vendor_aliases (vendor_id, alias, added_by, added_at, source_invoice_id) VALUES (?,?,?,?,?)",
                    (vendor_id, alias, added_by, now(), invoice_id))
    return cur.lastrowid


def remove_alias(c, alias_id: int, removed_by: str):
    c.execute("UPDATE vendor_aliases SET removed_by=?, removed_at=? WHERE id=? AND removed_at IS NULL",
              (removed_by, now(), alias_id))


def po(c, po_id: str) -> dict | None:
    r = c.execute("SELECT * FROM pos WHERE po_id=?", (po_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["lines"] = [dict(x) for x in c.execute("SELECT * FROM po_lines WHERE po_id=? ORDER BY line_no", (po_id,))]
    for l in d["lines"]:
        l["qty_remaining"] = l["qty_ordered"] - l["qty_invoiced"]
    return d


def line_po(c, line_id: str) -> str | None:
    r = c.execute("SELECT po_id FROM po_lines WHERE line_id=?", (line_id,)).fetchone()
    return r["po_id"] if r else None


def invoice(c, invoice_id: int) -> dict | None:
    r = c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    return dict(r) if r else None


def log(c, invoice_id: int | None, stage: str, status: str, message: str, data=None):
    c.execute(
        "INSERT INTO audit_events (invoice_id, ts, stage, status, message, data) VALUES (?,?,?,?,?,?)",
        (invoice_id, now(), stage, status, message, json.dumps(data, default=str) if data is not None else None),
    )


# ---------------------------------------------------------------- PO state

def refresh_po_status(c, po_id: str):
    """Recompute Open / Partially / Fully invoiced from line billing. Holds are managed separately."""
    p = po(c, po_id)
    if p["status"] == "On hold":
        return
    billed = sum(l["qty_invoiced"] for l in p["lines"])
    if all(l["qty_remaining"] <= 0 for l in p["lines"]):
        status = "Fully invoiced"
    elif billed > 0:
        status = "Partially invoiced"
    else:
        status = "Open"
    c.execute("UPDATE pos SET status=? WHERE po_id=?", (status, po_id))


def apply_allocations(c, invoice_id: int, allocations: list[dict]):
    """Add approved invoice quantities to the PO lines."""
    for a in allocations:
        c.execute("UPDATE po_lines SET qty_invoiced = qty_invoiced + ? WHERE line_id=?", (a["qty"], a["line_id"]))
        c.execute("INSERT INTO allocations VALUES (?,?,?,?,?)",
                  (invoice_id, a["line_id"], a["qty"], a["unit_price"], now()))


def set_hold(c, po_id: str, invoice_id: int):
    c.execute("UPDATE pos SET status='On hold', on_hold_since=?, hold_invoice_id=? WHERE po_id=?",
              (now(), invoice_id, po_id))


def release_hold(c, po_id: str):
    c.execute("UPDATE pos SET status='Open', on_hold_since=NULL, hold_invoice_id=NULL WHERE po_id=?", (po_id,))
    refresh_po_status(c, po_id)
