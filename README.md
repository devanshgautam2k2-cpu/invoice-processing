# Invoice Processor · Zamp PS-1

**Live app:** [invoice-processing-devansh.streamlit.app](https://invoice-processing-devansh.streamlit.app/) · **Demo video (5 min):** [Loom](https://www.loom.com/share/26d51acf67f04c0cb3565bc78ddf17cd)

Built with Python, Streamlit, SQLite and the Claude API. On the live app, click **Reset demo data** in the sidebar first, then **Run core demo set**. If the app has been idle it shows a sleep screen; it takes about 30 seconds to wake up.

Takes a vendor invoice PDF, matches it to a purchase order, and decides **approve**, **send back to the vendor**, or **hold for human review**, with every step explained and audited.

> **The LLM reads, code decides, the human arbitrates.**
> Claude reads the invoice, pairs line descriptions and writes the explanation. Every decision that touches money is deterministic code, so it is consistent and auditable. A person sees anything the system cannot be sure of.

Design rationale: [`DESIGN.md`](DESIGN.md). Test set and expected outcomes: [`test_invoices/EXPECTED.md`](test_invoices/EXPECTED.md).

## What it handles

Every scenario below has a test invoice and is checked by the regression test (`tests/test_regression.py`). Expected outcomes per file: [`test_invoices/EXPECTED.md`](test_invoices/EXPECTED.md). Approve means **every** check passed; nothing is ever filed under the "closest" code.

| Scenario | Outcome | Test file |
|---|---|---|
| **Matches the PO exactly** | Approve (A1) | `01` |
| Small shortfall, within 2% and Rs 10,000 | Approve with a note (A2) | `e08` |
| Shortfall explained by a printed discount | Approve (A3) | `e09` |
| **Split invoicing**: part of a PO billed now, the rest later | Approve, PO partially invoiced | `02a` |
| Quantity beyond what remains on the PO line | Send back (S5) | `02b` |
| Price 2-7% over the PO | Human review (H1), PO on hold | `03a` |
| **Another invoice on a held PO** | Waits, then reruns automatically after the decision | `03b` |
| Price more than 7% over (text PDF) | Send back (S6) | `e06` |
| Underbilled beyond 2% / Rs 10,000, no discount | Human review (H2) | `e10` |
| **Scanned image or phone photo** | Human review (H3); read by Sonnet 5.5, never sent back | `04`, `u2` |
| Byte-identical resend | Send back (S2) | `05` |
| Invoice number already approved | Send back (S3) | `e02` |
| Unchanged resend of a rejected invoice | Send back (S10) | shown by re-running 02b |
| Near-duplicate number (one character off, same amount and date) | Human review (H9) | `e14` |
| PO already fully invoiced | Send back (S4) | `e01` |
| PO number not found, nothing fits | Send back (S9) | `e03` |
| **PO number typo**: another PO of the vendor fits every line | Human review (H8), candidate held and preselected | `e19` |
| PO written without its prefix ("P.O. No: 1011") | Matched on digits, same vendor only | `u5` |
| A different vendor billing someone else's PO | Send back (S7) | `e04` |
| Vendor name close but unclear | Human review (H6) | `e12` |
| **Reviewer confirms the name as an alias** | Later invoices under that name pass | `e18` |
| Bank details changed | Human review (H7): verify via a known contact | `e13` |
| Items not on the PO | Send back (S8) | `e05` |
| Required field missing (e.g. GSTIN) | Send back (S1), or human if the read is Low | `e07` |
| Totals don't add up | Human review (H5) | `e11` |
| Invoice dated before the PO or more than 3 months after it | Human review (H10) | `e15` |
| Tax rate that isn't a valid GST rate | Human review (H11) | `e16` |
| **Tax differs from the PO's rate** (e.g. 28% charged where the PO says 18%) | Human review (H17) with both rates and the rupee gap; the total is compared before tax, so a tax difference alone never sends it back | `u8` |
| A field not verifiable in the PDF text (e.g. PO as a stamp image) | Human review (H12) | `e17` |
| **Not an invoice** (quotation, proforma, credit note) | Human review (H14), never paid or sent back | `u6` |
| **Billed in another currency** | Converted to INR at the invoice-date rate, checked, human approves (H16) | `u7` |
| Anything unrecognised, or an unexpected error | Human review (H15), never left stuck | simulated in the regression test |
| AI service unavailable | Human review (H13) | shown with an invalid key |
| New layouts: real rupee signs, two pages, Indian GST columns | Read and checked like any other invoice | `u1`, `u3`, `u4` |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .streamlit
echo 'ANTHROPIC_API_KEY = "sk-ant-..."' > .streamlit/secrets.toml
python reset_demo.py          # clean state: 5 vendors, 13 open POs, no invoices
streamlit run app.py
```

The app has three pages:

| Page | What it shows |
|---|---|
| **Run invoices** | Upload PDFs or pick test files. A live view shows each of the nine stages turning green, amber or red as it runs, with every check logged underneath. |
| **Human review** | **Holds and queue:** POs on hold with an "on hold since" timer, and the invoices waiting behind them.<br>**Review screen:** the document beside what the AI read (with the text each field came from), *Reading confidence* and *Match vs PO* side by side, and the findings.<br>**Decision:** approve or reject with a reason code and required notes; on approval, confirm which PO lines and quantities are billed.<br>**Learning:** on a close vendor name (H6), tick "confirm as an alias" so later invoices under that name pass.<br>A decision releases the hold and reruns waiting invoices immediately. |
| **Dashboard** | Invoices processed, auto-approval and exception rates, value approved, overbilling and duplicates caught, status and reason breakdowns, filterable history with a drill-down into any invoice's decision and audit trail, and PO billing progress. |

Unseen invoices (`test_invoices/unseen/`: new layouts, a phone photo, a two-page invoice, a quotation, a USD invoice) are in the test picker too; see `EXPECTED.md`.

`python reset_demo.py --run` resets and processes the seven core test invoices from the command line; add `--extended` for the 19 edge-case invoices (one per remaining reason code, plus the learned-alias follow-up; see `test_invoices/EXPECTED.md`). In the app, use **Run core demo set**, then **Run edge-case set**.

### Regression test

```bash
python tests/test_regression.py
```

Runs all 34 test invoices (core, edge-case and unseen) on a throwaway database, offline (cached AI results only, no API spend, a few seconds), and checks every outcome and reason code against `test_invoices/EXPECTED.md`. It also checks the reviewer flow (approving 03a reruns 03b), the final PO statuses, alias learning (with the tick e18 is approved on the learned alias; without it, H6 again), H8, and a simulated crash that must end in H15 rather than a stuck invoice. 56 checks; exit code 0 means all pass. Run it after any rule change.

### Demo order (5 minutes)

1. **Reset**, then **Run core demo set**. The seven invoices run one at a time:
   - 01: approved
   - 02a: approved; 02b: sent back (S5, 45 monitors but only 40 remain)
   - 03a: human review (H1, +4.1%); 03b waits behind it
   - 04: scanned, human review (H3)
   - 05: exact duplicate, sent back (S2)
2. **Human review → INT-0004 → Approve (R-A2)**. PO-1003 is released, and 03b reruns live and is approved.
3. **INT-0006 (scan)**: Reading *Low* and Match vs PO *Failed* side by side. Scans never go straight back to a vendor: in testing Haiku misread one character of the GSTIN on this scan, which is why scans are now read by Sonnet 5.5 and always checked by a person.
4. **Run edge-case set**, then **Human review → INT-0019 (H6, "Sri Ganesh Office Solution")**: tick "confirm as an alias" and approve. INT-0025, the same name waiting behind it, reruns and passes the vendor check on the learned alias.
5. **Dashboard**: open any row for its full audit trail.

## How an invoice is processed

Nine stages, strictly one invoice at a time. Every stage can exit early with a reason code, and the final outcome is the **most severe** finding. Lines never offset each other.

| # | Stage | Who | What |
|---|---|---|---|
| 1 | Receive | code | Internal ID (`INT-0001`) and SHA-256 file fingerprint |
| 2 | Read | code + **LLM** | Text PDF or scan? Claude transcribes every field **with the exact text it read it from**; "not found" instead of guesses. Extraction never sees the PO. It also says what the document is: anything but a tax invoice or bill of supply is read in full and goes to a human (H14), never back to the vendor. **Currency:** an invoice in another currency is converted to INR at the invoice-date rate (Frankfurter / ECB, saved for replays, config fallback), every check runs on the INR values, and a person gives final approval (H16). |
| 3 | Reading confidence | code | Qty × price = amount; lines = subtotal; subtotal + tax = total; each key field's quote is found in the PDF's text layer. Scans are always Low. |
| 4 | Required fields | code | 7 fields. Missing: send back (S1) if confidence is High, else human (H3). |
| 5 | Duplicates | code | Same file (S2, or S10 if the original was rejected); same vendor + number + amount as an approved invoice (S2); number already approved, different amount (S3); unchanged resend of a rejected invoice (S10), while a changed one is processed as a corrected version; number one character off (H9); same number still under review → wait |
| 6 | PO | code + **LLM** | Exists? A PO written without its prefix ("P.O. 1011") is matched on its digits if it belongs to the same vendor. Not found → the vendor's open POs are checked as **candidates** (Claude pairs the lines; code checks quantities and prices): a fit goes to a human with the candidate named and held (H8, "possible typo"); no fit is sent back (S9). Fully invoiced (S4)? On hold → **wait and rerun after the decision** |
| 7 | Vendor | code | Name vs PO vendor and aliases, rapidfuzz ≥90 pass, 75–90 human (H6), <75 S7; bank account + IFSC vs vendor master (H7); GSTIN (informational) |
| 8 | Lines | **LLM** + code | Claude pairs each invoice line with a PO line, **choosing only from that PO's line IDs** or UNMATCHED. Code checks quantity ≤ remaining (S5), every unit price and the expected total against the tolerance bands, the invoice date (PO date to PO date + 3 months, H10) and the implied GST rate. |
| 9 | Decide | code | Anything outside what the system understands (an unexpected error, an unrecognised document type or currency, a text PDF the AI found hard to read) goes to a person as **H15 Unclassified**, never to the "closest" rule and never left stuck. Most severe wins. **Nothing goes back to a vendor unless reading confidence is High**; otherwise a human sees it first. One exception: a byte-identical resend (same file hash) is a duplicate whatever the reading quality. |
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
test_invoices/          7 core test PDFs + EXPECTED.md; extended/ holds 19 edge-case PDFs; unseen/ holds 8
reset_demo.py           restore a clean demo state (optionally run the test set)
tests/test_regression.py   all 34 invoices vs EXPECTED.md, offline (56 checks)
```

## Choices worth knowing

- **Models** (exact strings in `data/config.json`: `claude-haiku-4-5`, `claude-sonnet-5-5`): Claude Haiku 4.5 reads text PDFs (every key field is then verified against the PDF text), pairs lines and writes notes. **Claude Sonnet 5.5 reads image-only PDFs** (scans, phone photos): in testing Haiku misread a GSTIN on a scan that Sonnet and Opus 5.5 read correctly. Swap models in `config.json`.
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
| Three-way match (goods receipt) | Two-way: PO vs invoice | Add GRN quantities as the "remaining" ceiling; also catches disguised duplicates |
| GSTIN matching | Compared and shown, informational only | Make a mismatch a review reason |
| Financial year in the invoice-number key | Vendor + number | Add FY (numbering restarts each April) |
| Partial approvals / short-paying | Whole invoice approved or sent back | Approve good lines, dispute the rest |
| Concurrent database | SQLite, one writer | Postgres with row locks per PO, so only invoices on the same PO serialise |
| Full tax engine | Implied-rate sanity check (foreign currency is converted and sent to a person) | HSN-level tax rules |
| Clustering "Other" review reasons | Stored as free text | LLM groups them into candidate new reason codes |
| Vendor channel for corrected invoices | Corrected resend with the same number is detected and processed | Vendor portal to withdraw or replace an invoice |
| Learning from reviewer decisions | **Built:** a reviewer can confirm a close vendor name as an alias (explicit tick, one alias, logged, removable) | Bank-detail changes with maker-checker. **Patterns:** at least 7 matching decisions for the same vendor or PO and reason produce a *suggested* rule change for a finance lead to accept or decline, never an automatic one |
| Separate logins per AP reviewer | A free-text "Reviewer" name on each decision, which anyone can type | Each AP person signs in (SSO); every approval, rejection, alias and reset is recorded against their account, so it is always clear who made which change and it cannot be confused later. Adds roles and maker-checker for large amounts and bank changes |

Test data (POs, vendors, invoices) is self-created as the brief allows. All names, GSTINs and bank details are fictional.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo. `.streamlit/secrets.toml` and `*.db` are git-ignored; `data/llm_cache/` is committed.
2. On share.streamlit.io: **New app**, pick the repo, main file `app.py`, Python 3.12.
3. **Settings → Secrets**: `ANTHROPIC_API_KEY = "sk-ant-..."`.
4. The SQLite file lives on the app's local disk and resets when the app restarts. Use **Reset demo data** in the sidebar before a demo.
