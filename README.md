# Invoice Processor · Zamp PS-1

Takes a vendor invoice PDF, matches it to a purchase order, and decides **approve**, **send back to the vendor**, or **hold for human review**, with every step explained and audited.

> **The LLM reads, code decides, the human arbitrates.**
> Claude reads the invoice, pairs line descriptions and writes the explanation. Every decision that touches money is deterministic code, so it is consistent and auditable. A person sees anything the system cannot be sure of.

Design rationale: [`DESIGN.md`](DESIGN.md). Test set and expected outcomes: [`test_invoices/EXPECTED.md`](test_invoices/EXPECTED.md).

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo 'ANTHROPIC_API_KEY = "sk-ant-..."' > .streamlit/secrets.toml
python reset_demo.py          # clean state: 5 vendors, 6 open POs, no invoices
streamlit run app.py
```

The app has three pages:

| Page | What it shows |
|---|---|
| **Run invoices** | Upload PDFs or pick test files. A live view shows each of the nine stages turning green, amber or red as it runs, with every check logged underneath. |
| **Human review** | POs on hold with an "on hold since" timer and how long each waiting invoice has waited, the waiting queue, and the review screen: the document beside what the AI read (with the exact text it read each field from), two separate indicators (*Reading confidence* vs *Match vs PO*), the findings, and approve or reject with a reason code and **required notes**. On approval the reviewer confirms which PO lines and quantities the invoice bills (prefilled with the system's matches, or chosen by hand when it could not match), and sees whether the PO will become partially or fully invoiced. A decision releases the hold and reruns waiting invoices immediately. |
| **Dashboard** | Invoices processed, auto-approval and exception rates, value approved, overbilling and duplicates caught, status and reason breakdowns, filterable history with a drill-down into any invoice's decision and audit trail, and PO billing progress. |

Unseen invoices (`test_invoices/unseen/`, new layouts, a phone photo, a two-page invoice, a quotation) are in the test picker too; see `EXPECTED.md`.

`python reset_demo.py --run` resets and processes the seven core test invoices from the command line; add `--extended` for the 17 edge-case invoices (one per remaining reason code; see `test_invoices/EXPECTED.md`). In the app, use **Run core demo set**, then **Run edge-case set**.

### Regression test

```bash
python tests/test_regression.py
```

Runs all 24 test invoices on a throwaway database, offline (cached AI results only, no API spend, about 2 seconds), and checks every outcome and reason code against `test_invoices/EXPECTED.md`. It also checks the reviewer flow (approving 03a reruns 03b) and the final PO statuses. 39 checks; exit code 0 means all pass. Run it after any rule change.

### Demo order (5 minutes)

1. **Reset**, then **Run core demo set**. The seven invoices run one at a time:
   - 01: approved
   - 02a: approved; 02b: sent back (S5, 45 monitors but only 40 remain)
   - 03a: human review (H1, +4.1%); 03b waits behind it
   - 04: scanned, human review (H3)
   - 05: exact duplicate, sent back (S2)
2. **Human review → INT-0004 → Approve (R-A2)**. PO-1003 is released, and 03b reruns live and is approved.
3. **INT-0006 (scan)**: Reading *Low* and Match vs PO *Failed* side by side. Scans never go straight back to a vendor: in testing Haiku misread one character of the GSTIN on this scan, which is why scans are now read by Sonnet 5.5 and always checked by a person.
4. **Dashboard**: open any row for its full audit trail.

## How an invoice is processed

Nine stages, strictly one invoice at a time. Every stage can exit early with a reason code, and the final outcome is the **most severe** finding. Lines never offset each other.

| # | Stage | Who | What |
|---|---|---|---|
| 1 | Receive | code | Internal ID (`INT-0001`) and SHA-256 file fingerprint |
| 2 | Read | code + **LLM** | Text PDF or scan? Claude transcribes every field **with the exact text it read it from**; "not found" instead of guesses. Extraction never sees the PO. It also says what the document is: anything but a tax invoice or bill of supply is read in full and goes to a human (H14), never back to the vendor. |
| 3 | Reading confidence | code | Qty × price = amount; lines = subtotal; subtotal + tax = total; each key field's quote is found in the PDF's text layer. Scans are always Low. |
| 4 | Required fields | code | 7 fields. Missing: send back (S1) if confidence is High, else human (H3). |
| 5 | Duplicates | code | Same file (S2, or S10 if the original was rejected); same vendor + number + amount as an approved invoice (S2); number already approved, different amount (S3); unchanged resend of a rejected invoice (S10), while a changed one is processed as a corrected version; number one character off (H9); same number still under review → wait |
| 6 | PO | code | Exists (S9)? A PO written without its prefix ("P.O. 1011") is matched on its digits if it belongs to the same vendor. Fully invoiced (S4)? On hold → **wait and rerun after the decision** |
| 7 | Vendor | code | Name vs PO vendor and aliases, rapidfuzz ≥90 pass, 75–90 human (H6), <75 S7; bank account + IFSC vs vendor master (H7); GSTIN (informational) |
| 8 | Lines | **LLM** + code | Claude pairs each invoice line with a PO line, **choosing only from that PO's line IDs** or UNMATCHED. Code checks quantity ≤ remaining (S5), every unit price and the expected total against the tolerance bands, the invoice date (PO date to PO date + 3 months, H10) and the implied GST rate. |
| 9 | Decide | code | Most severe wins. **Nothing goes back to a vendor unless reading confidence is High**; otherwise a human sees it first. One exception: a byte-identical resend (same file hash) is a duplicate whatever the reading quality. |
| — | Note | **LLM** | A plain-English note that only narrates what code decided (template fallback if the AI is down) |
| — | Update | code | Approve: add quantities to PO lines. Human review: put the PO on hold with a timer. Every value, comparison, rule, decision and override goes to the audit trail. |

**Tolerance bands** (all in [`data/config.json`](data/config.json)):

| Over the PO price | Outcome |
|---|---|
| ≤ 2% **and** ≤ Rs 5,000 | approve |
| up to 7% | human review (H1) |
| > 7% | send back (S6), or human (H3) if read from a scan |

| Under the PO price | Outcome |
|---|---|
| ≤ 2% **and** ≤ Rs 10,000 | approve with a note (A2) |
| explained by a stated discount | approve (A3) |
| otherwise | human review (H2) |

Checked per line and on the invoice total against the expected total (invoiced qty × PO price + tax).

## Project layout

```
app.py                  Streamlit entry point (navigation, reset)
ui/                     run_page (live view) · review_page · dashboard_page · common
engine/
  pipeline.py           the nine stages, combine rule, holds, waiting queue, reviewer actions
  checks.py             deterministic checks: normalisation, arithmetic, quotes, vendor, dates, tax, bands
  llm.py                the three Claude calls, structured JSON outputs, on-disk cache
  db.py                 SQLite schema, seed, per-line PO billing, audit log
  pdf_reader.py         file hash, text-vs-scan detection, page render
data/                   config.json (every threshold) · vendors.json · pos_seed.json · llm_cache/
scripts/generate_invoices.py   builds the test PDFs (3 layouts, reproducible bytes)
test_invoices/          7 core test PDFs + EXPECTED.md; extended/ holds 17 edge-case PDFs
reset_demo.py           restore a clean demo state (optionally run the test set)
tests/test_regression.py   all 24 invoices vs EXPECTED.md, offline
```

## Choices worth knowing

- **Models:** Claude Haiku 4.5 reads text PDFs (every key field is then verified against the PDF text), pairs lines and writes notes. **Claude Sonnet 5.5 reads image-only PDFs** (scans, phone photos): in testing Haiku misread a GSTIN on a scan that Sonnet and Opus 5.5 read correctly. Swap models in `config.json`.
- **Structured outputs** (`output_config.format` JSON schema) for every call, so the response always parses. The line matcher's `po_line_id` is an enum of the PO's own line IDs plus `UNMATCHED`, so it cannot invent a match.
- **Cache keyed by input hash** (`data/llm_cache/`, committed). The rehearsed demo gives identical results and works even if the API is slow or down. Delete the folder to force fresh reads.
- **Graceful degradation:** if the AI cannot be reached, the invoice goes to human review (H13) with a clear message. The app never crashes on it.
- **Strictly one at a time:** a process lock around the queue, so two invoices can never read the same remaining quantity and overbill a PO.

## Scope decisions

Built deliberately small; each item has a path to production.

| Not in this build | Instead | Path to production |
|---|---|---|
| Email / AP inbox ingestion | Upload or test-set picker | Mailbox or S3 listener feeding the same `enqueue()` |
| OCR second read for scans (H4) | Every scan is Low confidence → human | Tesseract or cloud OCR + LLM read; agreement on key fields would let clean scans skip review |
| Candidate-PO search for a wrong PO number (H8) | PO not found → send back (S9) | Filter the vendor's open POs and reuse the line matcher; reviewer confirms |
| Three-way match (goods receipt) | Two-way: PO vs invoice | Add GRN quantities as the "remaining" ceiling; also catches disguised duplicates |
| GSTIN matching | Compared and shown, informational only | Make a mismatch a review reason |
| Financial year in the invoice-number key | Vendor + number | Add FY (numbering restarts each April) |
| Partial approvals / short-paying | Whole invoice approved or sent back | Approve good lines, dispute the rest |
| Concurrent database | SQLite, one writer | Postgres with row locks per PO, so only invoices on the same PO serialise |
| Currency conversion, full tax engine | INR only; implied-rate sanity check | Rate service, HSN-level tax rules |
| Clustering "Other" review reasons | Stored as free text | LLM groups them into candidate new reason codes |
| Vendor channel for corrected invoices | Corrected resend with the same number is detected and processed | Vendor portal to withdraw or replace an invoice |
| Learning from reviewer decisions | Every decision and note is recorded | **Facts:** a reviewer can confirm a name variant as a vendor alias (explicit, one alias, logged, reversible); a bank change needs maker-checker. **Patterns:** at least 7 matching decisions for the same vendor or PO and reason produce a *suggested* rule change for a finance lead to accept or decline, never an automatic one |
| Auth / roles | Single reviewer name field | SSO, maker-checker for large amounts |

Test data (POs, vendors, invoices) is self-created as the brief allows. All names, GSTINs and bank details are fictional.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo. `.streamlit/secrets.toml` and `*.db` are git-ignored; `data/llm_cache/` is committed.
2. On share.streamlit.io: **New app**, pick the repo, main file `app.py`, Python 3.12.
3. **Settings → Secrets**: `ANTHROPIC_API_KEY = "sk-ant-..."`.
4. The SQLite file lives on the app's local disk and resets when the app restarts. Use **Reset demo data** in the sidebar before a demo.
