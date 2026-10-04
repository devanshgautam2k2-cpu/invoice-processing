"""Restore the demo to a clean starting state.

    python reset_demo.py          # fresh DB: vendors + POs from data/, no invoices
    python reset_demo.py --run    # ...then process the 7 test invoices in order
    python reset_demo.py --run --extended   # ...then also the 17 edge-case invoices

The extraction cache (data/llm_cache/) is kept, so the rehearsed demo gives
identical results every time. Delete that folder to force fresh AI reads.
"""

import argparse

from engine import db, pipeline
from engine.config import DB_PATH, ROOT


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="process the test invoices after resetting")
    ap.add_argument("--extended", action="store_true", help="with --run: also process test_invoices/extended/")
    args = ap.parse_args()

    db.init_db(reset=True)
    with db.tx() as c:
        pos = c.execute("SELECT COUNT(*) FROM pos").fetchone()[0]
        vendors = c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    print(f"Reset {DB_PATH.name}: {vendors} vendors, {pos} POs (all Open), no invoices.")

    if args.run:
        files = sorted((ROOT / "test_invoices").glob("*.pdf"))
        if args.extended:
            files += sorted((ROOT / "test_invoices" / "extended").glob("*.pdf"))
        for f in files:
            pipeline.enqueue(f.name, f.read_bytes())
        for r in pipeline.process_queue():
            print(f"  {r['internal_id']}  {r['outcome'].replace('_', ' '):13s} {r['reason_code'] or ''}")


if __name__ == "__main__":
    main()
