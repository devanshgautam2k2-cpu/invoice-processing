"""Regression test: every test invoice must still produce the outcome in test_invoices/EXPECTED.md.

    python tests/test_regression.py

Runs on a throwaway database and offline (cached AI results only, no API spend),
so it is free, fast and gives the same answer every time. Exit code 0 = all pass.

Two runs, mirroring EXPECTED.md:
  A. Core set alone, then the reviewer approves 03a and 04: 03b must rerun and
     approve, and the POs must end in the state table's statuses.
  B. Core set + extended set in one go (03a and 04 left awaiting review): every
     file's outcome and reason code.
  C. Unseen set alone on a fresh database.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["INVOICE_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "regression.db")
os.environ["INVOICE_OFFLINE"] = "1"
sys.path.insert(0, str(ROOT))

from engine import db, pipeline  # noqa: E402

TEST_DIR = ROOT / "test_invoices"
EXPECTED_MD = TEST_DIR / "EXPECTED.md"
OUTCOME_WORDS = {"APPROVE": "approve", "SEND BACK": "send_back", "HUMAN REVIEW": "human_review", "WAITS": "waiting"}


# ---------------------------------------------------------------- read EXPECTED.md

def expected_outcomes() -> dict[str, tuple[str, str | None]]:
    """filename -> (outcome, reason code): the first bold outcome in that file's row."""
    out = {}
    for line in EXPECTED_MD.read_text().splitlines():
        file = re.search(r"`([\w.]+\.pdf)`", line)
        verdict = re.search(r"\*\*(APPROVE|SEND BACK|HUMAN REVIEW|WAITS)\s*(?:\((\w+)\))?", line)
        if file and verdict:
            out[file.group(1)] = (OUTCOME_WORDS[verdict.group(1)], verdict.group(2))
    return out


def expected_po_states() -> dict[str, str]:
    """The core run's final PO statuses (state table, after the reviewer approves 03a and 04)."""
    states = {}
    for line in EXPECTED_MD.read_text().splitlines():
        m = re.match(r"\| (PO-\d+) \| (Open|Partially invoiced|Fully invoiced|On hold) \|", line)
        if m:
            states[m.group(1)] = m.group(2)
    return states


# ---------------------------------------------------------------- helpers

class Report:
    def __init__(self):
        self.failures, self.passes = [], 0

    def check(self, label: str, got, want):
        if got == want:
            self.passes += 1
            print(f"  PASS  {label}: {got}")
        else:
            self.failures.append(label)
            print(f"  FAIL  {label}: got {got}, expected {want}")


def run(files: list[Path]) -> dict[str, dict]:
    db.init_db(reset=True)
    for f in files:
        pipeline.enqueue(f.name, f.read_bytes())
    results = pipeline.process_queue()
    with db.tx() as c:
        names = {r["id"]: r["filename"] for r in c.execute("SELECT id, filename FROM invoices")}
    return {names[r["invoice_id"]]: r for r in results}


def invoice_id(filename: str) -> int:
    with db.tx() as c:
        return c.execute("SELECT id FROM invoices WHERE filename=? ORDER BY id LIMIT 1", (filename,)).fetchone()["id"]


# ---------------------------------------------------------------- the test

def main() -> int:
    expected = expected_outcomes()
    core = sorted(TEST_DIR.glob("*.pdf"))
    extended = sorted((TEST_DIR / "extended").glob("*.pdf"))
    unseen = sorted((TEST_DIR / "unseen").glob("*.pdf"))
    rep = Report()

    missing = [f.name for f in core + extended + unseen if f.name not in expected]
    rep.check("every test PDF has a row in EXPECTED.md", missing, [])

    print("\nRun A: core set, then the reviewer approves 03a and 04")
    results = run(core)
    for f in core:
        r = results.get(f.name, {})
        rep.check(f.name, (r.get("outcome"), r.get("reason_code")), expected.get(f.name))
    reruns = []
    for name, code in [("03a_shree_ganesh_price_over.pdf", "R-A2"), ("04_deccan_scanned_over.pdf", "R-A1")]:
        try:
            reruns += pipeline.review(invoice_id(name), "approve", code, "Regression test: reviewer approves")
            rep.check(f"reviewer approves {name}", "ok", "ok")
        except ValueError as e:
            rep.check(f"reviewer approves {name}", str(e), "ok")
    rerun = next((r for r in reruns if r["internal_id"] == "INT-0005"), {})
    rep.check("03b reruns after 03a is approved", (rerun.get("outcome"), rerun.get("reason_code")), ("approve", "A1"))
    with db.tx() as c:
        for po_id, want in expected_po_states().items():
            rep.check(f"{po_id} final status", db.po(c, po_id)["status"], want)

    print("\nRun B: core + extended set (03a and 04 left awaiting review)")
    results = run(core + extended)
    for f in core + extended:
        r = results.get(f.name, {})
        rep.check(f.name, (r.get("outcome"), r.get("reason_code")), expected.get(f.name))

    print("\nRun C: unseen set on a fresh database")
    results = run(unseen)
    for f in unseen:
        r = results.get(f.name, {})
        rep.check(f.name, (r.get("outcome"), r.get("reason_code")), expected.get(f.name))

    total = rep.passes + len(rep.failures)
    print(f"\n{rep.passes}/{total} checks passed" + (f"; FAILED: {', '.join(rep.failures)}" if rep.failures else ""))
    return 1 if rep.failures else 0


def test_regression():  # pytest entry point
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
