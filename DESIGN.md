# Zamp Case Study PS-1: Invoice Processing Logic (v3)

2026-10-03 · @Devansh

## Overview and decisions so far

The process takes a vendor invoice PDF, matches it to a purchase order (PO), and outputs approve, reject or hold for human review, with every step explained. Submission is due Monday, 5 October 2026, 11:00 AM IST.

**Setup**

- A PO data set lists every purchase the company has made, item by item.
- Invoices arrive from vendors as PDFs and are processed one by one.

**Decisions made**

| Topic | Decision |
|---|---|
| Split invoicing | Track billing per PO line; a PO can be cleared by several invoices |
| PO states | Open, Partially invoiced, Fully invoiced (completed), On hold |
| Vendor | One PO belongs to one vendor; the invoice vendor must match the PO vendor |
| Duplicates | Two separate checks: duplicate check and remaining-balance check |
| PO on hold + new invoice | Option B: wait for the human decision, then rerun (saves human time) |
| Mismatched PO ID, matching items | Not refused directly: if the PO number does not match but a PO with the same vendor, items and amounts is found, a human reviews it (they may confirm with the vendor) and applies the invoiced quantities to that PO line by line. No candidate PO found: send back. |
| Waiting days to batch invoices | Rejected: delays valid payments, deliveries can be weeks apart. Instead, each on-hold PO shows how many invoices are waiting behind it and how long each has waited. |
| Relying on "1/3"-style labels | Rejected: vendors do not include them reliably |
| Guiding principle | The LLM reads, code decides, the human arbitrates |

## PO data model and states

Billing is tracked per PO line, so one PO can be cleared by several invoices and closes only when every line is fully billed. This replaces the earlier rule that one approved invoice completes the PO, which broke the brief's split-invoice case.

Each PO line stores:

| Field | Example (PO-1042, line 1) |
|---|---|
| Item | Dell Latitude 5440 |
| Qty ordered | 50 |
| Unit price | ₹80,000 |
| Qty invoiced so far | 20 |
| Remaining qty | 30 |

An invoice is checked line by line against the remaining quantity and unit price. Any unclear invoice puts the whole PO on hold until a human decides.

## Pipeline stages: where the LLM is used and why

The LLM is used in three places, all needing interpretation of messy input; every decision touching money is deterministic code, so it is consistent and auditable.

| # | Stage | LLM? | Reasoning |
|---|---|---|---|
| 1 | Receive invoice, assign internal ID | No | Bookkeeping. Unique ID + file hash (fingerprint of the exact file), more reliable than PDF metadata |
| 2 | Detect text PDF vs scanned | No | A PDF library checks for a text layer: deterministic, instant, free |
| 3 | Extract fields (vendor, invoice no., date, PO ref, line items, tax, total) | Yes | Layouts, labels and scans vary; rules cannot cover them. Temperature 0, fixed output format, explicit "not found" instead of guesses. Mirrors Zamp's bank-statement approach |
| 4 | Normalise dates, currency, number formats | No | Rule-based conversion; an LLM could silently get it wrong |
| 5 | Arithmetic checks (lines sum to total, tax adds up) | No | Maths must be exact. Also catches LLM misreads: numbers that do not add up go to a human |
| 6 | Look up PO and its status | No | Database lookup by ID |
| 7 | Vendor check | No | Normalised name comparison + similarity score; auditable. Unclear match goes to a human |
| 8 | Match invoice lines to PO lines | Partly | LLM pairs lines with different descriptions (fuzzy mapping), but can only return a PO line ID from a shortlist code supplies, or "UNMATCHED"; code compares quantities and prices |
| 9 | Duplicate detection | No | File hash + same vendor, invoice number and amount. Deterministic and explainable |
| 10 | Apply rules, decide, update PO state | No | Money decisions must give the same output for the same input and be explainable rule by rule |
| 11 | Write the decision explanation | Yes | Turns rule results into plain English for the AP person. It only narrates facts code already decided |
| 12 | Human review | No | Human decides from a fixed reason list plus "Other" |
| 13 | Group "Other" reasons into categories (later) | Yes | Clustering free text suits an LLM. Future improvement, not built now |
| 14 | Dashboard metrics | No | Counts and sums from the database |

Interview line: AI reads the invoice, matches line descriptions and explains decisions; code makes every money decision and checks the AI's work.

## End-to-end flow

Every invoice runs through the same nine stages in order; each stage can exit early with a reason code, and the final outcome is the most severe one found.

**Every check, by stage** (planned checks are marked)

| Stage | Check | Done by | If it fails |
|---|---|---|---|
| 1 Receive | Assign sequence number (INT-0037) and file hash | Code | - |
| 1 Receive | Process strictly one invoice at a time | Code | - |
| 2 Read | Detect text-based PDF vs scanned image | Code | Scan: low reading confidence, goes to a human |
| 2 Read | Extract vendor, invoice no., date, PO, GSTIN, lines, tax, discounts, total; "not found" instead of guesses | LLM | - |
| 2 Read | Normalise dates, currency, number formats | Code | - |
| 3 Reading confidence | Line qty x unit price = line total | Code | Reading Low (H5) |
| 3 Reading confidence | Line totals + tax = invoice total | Code | Reading Low (H5) |
| 3 Reading confidence | Format sanity: real date, well-formed amounts | Code | Reading Low |
| 3 Reading confidence | OCR and LLM agree on key fields (scans only; next step) | Code | Human (H4) |
| 4 Required fields | Vendor name, invoice no., date, PO no., line items, total, GSTIN present | Code | High: send back (S1); Low: human (H3) |
| 4 Required fields | Second-pass search of the whole document before declaring missing | LLM | - |
| 5 Duplicates | Same file hash as an earlier invoice | Code | Send back (S2) |
| 5 Duplicates | Same vendor + invoice no. + amount as an **approved** invoice | Code | Send back (S2) |
| 5 Duplicates | Invoice no. already in this vendor's approved list | Code | Send back (S3) |
| 5 Duplicates | Reused number of a rejected invoice: unchanged or corrected | Code | Unchanged: send back (S10); corrected: continue |
| 5 Duplicates | Same items and amount as an approved invoice, new number | Code | Informational note only |
| 6 PO | PO number exists | Code | Send back (S9); scan: human |
| 6 PO | PO not fully invoiced | Code | Send back (S4) |
| 6 PO | PO not on hold | Code | Wait in queue, rerun after the decision |
| 7 Vendor | Invoice vendor = PO vendor (normalised, aliases from vendor master) | Code | Clear mismatch: S7; close name: H6 |
| 7 Vendor | Bank details match the vendor master | Code | Human (H7); verify via a known contact |
| 7 Vendor | GSTIN matches our vendor list (planned) | Code | - |
| 8 Lines | Pair invoice lines with PO lines despite different descriptions | LLM | Items mismatch: S8, or human on a scan |
| 8 Lines | Quantity within the remaining quantity on the PO line | Code | Send back (S5) |
| 8 Lines | Over PO price: up to 2% / Rs 5,000 approve; up to 7% review; over 7% send back | Code | H1 / S6 |
| 8 Lines | Under PO price: up to 2% / Rs 10,000 approve; beyond, review | Code | H2 |
| 8 Lines | A stated discount explains a shortfall | LLM + code | Explained: approve (A3) |
| 8 Lines | Invoice total vs expected total (qty x PO price + tax), same tolerances | Code | Same bands |
| 9 Combine | Most severe outcome wins; no offsetting between lines; one combined note | Code | - |
| 9 Combine | Send-back only when reading confidence is High | Code | Otherwise human |
| After | Write the plain-English decision note (narrates only, decides nothing) | LLM | - |
| After | Update PO lines; set hold and "on hold since" timer; rerun waiting invoices | Code | - |
| After | Save the audit trail: values, comparisons, rules, decision, time, overrides | Code | - |
| Planned | Three-way match with goods receipt | Code | - |
| Planned | Financial year in the invoice-number key | Code | - |

Added since this table: source-quote validation (stage 3), candidate PO search for unmatched PO numbers (stage 6), near-duplicate invoice numbers (stage 5), date and tax sanity (stage 8), shortlist-constrained line matching (stage 8).

## Happy path and approval checks

An invoice is approved when its PO ID matches an existing PO that is not on hold or fully invoiced, and every check below passes.

1. The PO ID on the invoice matches a PO in the data set.
2. The PO is Open or Partially invoiced (not On hold, not Fully invoiced).
3. The invoice vendor matches the PO vendor.
4. It is not a duplicate of an earlier invoice.
5. Each invoiced line quantity is within the PO line's remaining quantity.
6. Each unit price matches the PO line price, within tolerance.
7. Line items, tax and total add up.
8. Required fields are present: vendor, invoice number, date, PO reference, total.

On approval, the invoiced quantities are added to the PO lines. When every line is fully billed, the PO becomes Fully invoiced and cannot be referenced again.

If the PO ID has already been fully invoiced, the system returns: "We already have invoices covering this PO in full." This is a duplicate or over-billing, potentially the vendor trying to overcharge.

Decided in the next section: the exact tolerance (for example ±2%) and when a failed check rejects or goes to a human.

## Tolerance and amount rules

Every invoice, split or full, is checked the same way: unit price per line, quantity against what remains on the PO, and the invoice total against an expected total.

**Always checked**

- Quantity per line must not exceed the **remaining** quantity on that PO line.
- Unit price per line is compared with the PO unit price, using the tolerances below.
- The invoice total is compared with the **expected total**: invoiced quantities × PO unit prices, plus tax. This works for both split and full invoices.
- Line items plus tax must add up to the stated invoice total.

**Over the PO price** (per line and on the expected total)

| Variance | Outcome |
|---|---|
| Up to 2% or ₹5,000, whichever is lower | Approve |
| Above that, up to 7% | Human review |
| Over 7%, high confidence | Send back to vendor (reject), with a note covering every line and the total |
| Over 7%, low confidence | Human review: "Possible overbilling of more than 7%. Here is what the AI read and inferred so far; please confirm." |

No cap on the 7% line: anything between the approve limit and 7% already goes to a human, so no large amount is paid without a person seeing it.

Bands apply to **every line's unit price**, not only the invoice total: a single unit more than 2% over is never auto-approved, 2–7% goes to a human, and more than 7% is sent back even if it is one line. Because quantities can never exceed what remains on the PO, split invoices cannot add up to more than 2% over the PO value without a person seeing it. Only the Rs 5,000 absolute cap is per invoice, and it can never exceed that 2%.

**Under the PO price**

| Variance | Outcome |
|---|---|
| Up to 2% or ₹10,000, whichever is lower | Approve, with a note |
| Beyond that, fully explained by a stated discount | Approve, with a note: e.g. "15% below PO price; explained by a stated 15% discount." |
| Beyond that, no discount explains it, high confidence | Human review: "Items and details match, but the invoice is undervalued by more than 2% / ₹10,000. This may lead to separate invoices for the same order." |
| Beyond that, no discount explains it, low confidence | Human review: "Amounts appear underreported, and the figures could not be fully verified (reason given). Please check." |

Underbilling is never auto-rejected: paying less is not the risk, a hidden error is. Without a stated discount, no invoice is auto-approved more than ₹10,000 or 2% short, whichever is smaller; ₹5,00,000 is where both limits meet (₹4,90,000 billed). The limit applies to each invoice's expected total, not the whole PO, and every line must also sit within the band. A shortfall needs a visible reason (e.g. a discount) to be cleared beyond this, so every payment below the agreed price can be justified.

**Discount check:** the LLM extracts any discount lines ("Discount 10%", "Less: rebate"); code checks whether PO price minus the stated discount equals the invoiced price.

**Confidence comes from checks, not from the AI's own estimate.** An LLM's self-reported confidence is not reliable, so it never decides a rejection alone.

| Signal | Why it is trustworthy |
|---|---|
| Text-based PDF, not a scan | Text read directly, not inferred from pixels |
| Line qty × price = line totals | The numbers agree with each other |
| Line totals + tax = invoice total | Independent cross-check |
| Key fields found in the document text (source quotes) | Nothing guessed |

High confidence = all signals pass. A missing required field is **not** a reading-confidence signal: it is handled by its own stage (S1 on a confident read, H3 otherwise). Counting it here as well would make S1 impossible, since every missing field would make the read Low. Any failure = low confidence, so the invoice goes to a human with the reason named. The model's own confidence can be shown as an extra signal only.

**Source-quote validation (text PDFs)**

For every field, the LLM returns the value and the exact text it read it from, e.g. `"po_number": {"value": "PO-0412", "quote": "Your Ref: PO-0412"}`. Code then checks that the quote appears in the PDF's text layer and that the value matches the quote. A field that fails is marked low confidence and goes to a human (H12).

- This covers text fields arithmetic cannot check: PO number, invoice number, vendor name, GSTIN, date. A misread PO number would otherwise pass every arithmetic check.
- It proves the text exists, not that it is the right field (e.g. subtotal quoted as total); the arithmetic checks cover that.
- Scans have no text layer, so quote validation runs on text PDFs only; scans go to a human anyway.
- The quote is shown to the reviewer as evidence ("read from: 'Your Ref: PO-0412'").

**Reading confidence and PO match are kept separate**

Two different questions, never blended into one score: did we read the invoice correctly, and is the invoice correct?

|  | Question | Checked against |
|---|---|---|
| Reading confidence | Did we read it correctly? | The invoice only: source type, internal arithmetic, format sanity, two independent reads agreeing (next step) |
| Match result | Is the invoice correct? | The PO and vendor records |

- Extraction never sees the PO, so the AI cannot "read" what it expects and hide a real overbilling.
- A low-confidence read always goes to a human, even when it fully matches the PO; a mismatch is never used to judge reading quality, because the mismatch may be real.
- The reviewer sees both indicators, e.g. "Reading: High (text PDF, totals reconcile)" and "Match vs PO: Failed (line 3 is 9% over)".

**Next step, not in this build: s****econd read for scanned invoices (OCR + LLM)**

1. OCR (e.g. Tesseract) reads the scan: raw text plus a confidence score per word.
2. The LLM extracts the fields from the image separately.
3. Code compares the key fields only: total, invoice number, PO number, date, line amounts. Agreement raises reading confidence; disagreement sends the invoice to a human with both readings shown.

Why it is a next step: in this build every scanned invoice is low reading confidence and goes to a human, so OCR would not change any decision. What it would improve: when OCR and the LLM agree on every key field and the totals reconcile, a clean scan could count as high confidence and be approved or sent back without a human, cutting review volume for vendors who only send scans. It would also show the reviewer exactly which field the two reads disagree on.

**Rule: nothing goes back to the vendor unless reading confidence is high.** Any send-back (over 7%, vendor mismatch, item mismatch) with less than high reading confidence goes to a human first, however large the error looks. A wrong send-back caused by our misread damages the vendor relationship and delays payment; a human check costs minutes.

*One exception:* a **byte-identical file** (same file hash as an earlier invoice) is sent back as a duplicate (S2, or S10 if the original was rejected) even when reading confidence is Low, e.g. the same scan sent twice. That decision does not depend on how well we read the document, only on the file's fingerprint, so a misread cannot cause it.

**Combining line outcomes into one invoice decision**

| Situation | Invoice outcome |
|---|---|
| Any line or the total over by more than 7% | Send back to vendor, with one note covering every line and the total, so the vendor fixes everything in one resubmission |
| Underbilling beyond 2% / ₹10,000 on any line or the total, nothing over 7% | Human review |
| Overbilling between the approve limit and 7%, nothing over 7% | Human review |
| Everything within tolerance | Approve (with notes for any small shortfalls) |

- Lines never offset each other: overbilling on one line is not cancelled by underbilling on another, and a passing total never overrides a flagged line.
- No partial approvals (short-paying is out of scope): a send-back covers the whole invoice.
- Every flagged issue appears in one combined note, not just the one that decided the outcome.
- Approve: invoiced quantities are added to the PO lines. Human review: the PO goes on hold. Send-back: the PO is unchanged and not put on hold.

**Other major mismatches follow the same confidence rule**

Vendor, item and other detail mismatches are treated like over-7% price variances: a confident mismatch goes back to the vendor, an uncertain one goes to a human. All checks feed the same "most severe wins" ranking.

| Mismatch | Text-based invoice (high confidence) | Scanned or blurry invoice (low confidence) |
|---|---|---|
| Vendor clearly different from the PO vendor | Send back to vendor, with a note | Human review, with what the AI read |
| Line items clearly different from the PO | Send back to vendor, with a note | Human review, with what the AI read |
| Bank details differ from the vendor's records | Human review: verify through a known vendor contact, never by replying to the invoice | Human review: same |

Confidence is decided by the source and the reconciliation checks, not by the AI's self-reported score: a scanned or blurry invoice is always low confidence for any send-back.

## Vendor rules

One PO goes to one vendor; buying from three vendors means three POs, so the invoice vendor must match the PO vendor.

| Situation | Example | Outcome |
|---|---|---|
| Exact or normalised match | "Acme Supplies Pvt Ltd" vs "ACME Supplies" | Pass (normalise case, "Pvt Ltd", punctuation) |
| Close but unclear match | Parent company, subsidiary, renamed or acquired vendor | Hold for human review |
| Clearly different vendor | Invoice from Vendor B quoting Vendor A's PO | Reject: wrong PO quoted or possible fraud |

Invoice numbers are only unique per vendor (two vendors can both send "INV-001"), so the vendor is part of the duplicate check.

## Sample vendor master data

Five fictional approved vendors for test data; POs reference vendors by ID, and bank details and GSTINs are checked against this list.

| Vendor ID | Legal name | Known aliases | GSTIN | Bank | Account (last 4) | IFSC |
|---|---|---|---|---|---|---|
| V-001 | Apex Tech Supplies Pvt Ltd | Apex Tech, APEX TECH SUPPLIES | 27AABCA1234F1Z5 | HDFC Bank | 4417 | HDFC0001234 |
| V-002 | Shree Ganesh Office Solutions | SG Office Solutions, Shree Ganesh Office | 27AAKFS5678G1Z2 | ICICI Bank | 8820 | ICIC0004567 |
| V-003 | Northwind Electronics LLP | Northwind Electronics, NW Electronics | 29AAJFN9012H1Z8 | Axis Bank | 3051 | UTIB0007890 |
| V-004 | Deccan Furniture Works | Deccan Furniture, DFW | 27AAEFD3456J1Z4 | State Bank of India | 6694 | SBIN0012345 |
| V-005 | Blue River Logistics Pvt Ltd | Blue River Logistics, BRL | 07AABCB7890K1Z1 | Kotak Mahindra Bank | 2208 | KKBK0002345 |

All names, GSTINs and bank details are made up for the case study.

## Required fields checklist

If any of these 7 fields is genuinely missing on a high-confidence (text-based) invoice, it is sent back to the vendor; on a scanned or blurry invoice it goes to a human instead.

| # | Required field | Why it is needed |
|---|---|---|
| 1 | Vendor name | Matching to the PO and vendor records |
| 2 | Invoice number | Duplicate detection depends on it |
| 3 | Invoice date | Payment terms, record-keeping |
| 4 | PO number | No PO reference means no match |
| 5 | Line items (description, qty, unit price) | Line-level matching and split invoicing |
| 6 | Total amount | The amount to pay; sent back even if lines are present, as the system never pays on its own calculation |
| 7 | Vendor GSTIN | A valid GST tax invoice must carry the supplier's GSTIN |

- Before declaring a field missing, a second pass searches the whole document (header, footer, small print) for that field.
- Missing is not the same as unclear: a field that is present but does not clearly match (e.g. a close vendor name) goes to a human.
- The LLM returns "not found" instead of guessing; the checklist itself is plain code.

## Date and tax sanity checks

Two cheap plain-code checks that catch stale, misdated or mistyped invoices.

| Check | Rule | If it fails |
|---|---|---|
| Invoice date | On or after the PO date and no later than 3 months after it | Human review (H10) |
| Tax rate | Implied rate (tax ÷ subtotal) matches a valid GST rate from the config list, within rounding | Human review (H11) |

## Duplicate detection

Two separate checks are needed, because neither catches everything alone.

1. **Duplicate check:** same vendor + same invoice number + same amount, or an identical file (same file hash). Catches resends.
2. **Remaining-balance check:** the invoice would push total billing above what the PO allows. Catches over-billing.

Why both: PO-1042 is for 50 laptops. An invoice for 20 is approved, leaving 30. The same invoice for 20 is resent by mistake. It fits within the remaining 30, so the balance check alone would approve it and pay twice; the duplicate check stops it.

- Every incoming invoice gets its own internal ID, separate from the vendor's invoice number: a sequence number plus the file hash (e.g. INT-0037, "sequence no. 37"), and internal references always quote the sequence number.
- An invoice rejected as a duplicate points to the original using that internal ID.
- Your earlier idea of a 97–98% match is replaced by these exact, explainable checks (open item: confirm).

**Order of checks for an invoice on a PO that already has invoices**

The exact-duplicate check runs first, whatever the PO state; otherwise an identical resend on a partially invoiced PO would fit the remaining quantity and be paid twice.

| Step | Check | Outcome |
|---|---|---|
| 1 | Exact duplicate: same file hash, or same vendor + invoice number + amount as an earlier invoice | Reject as duplicate, linked to the original's internal ID |
| 2 | Invoice number already in the approved-invoice list for this vendor (high reading confidence) | Send back: "An invoice with this number has already been approved" |
| 3 | Earlier invoice on this PO is on hold | Wait, then rerun after the human decision |
| 4 | Earlier invoice was rejected | PO is free; process normally (corrected-version path) |
| 5 | PO fully invoiced | Send back: duplicate / PO already fully invoiced, linked to the approved invoice |
| 6 | PO partially invoiced | Check against the remaining quantities |

- The system keeps a list of every approved invoice number per vendor for step 2.
- Same items and amount as an approved invoice, but a different invoice number: processed through the remaining-quantity check like any partial invoice. A human reviewing it would have no extra information to tell a repeat delivery from a disguised duplicate, so the system only adds an informational note ("Same items and amount as INT-0003"). Catching disguised duplicates needs proof of delivery (three-way match), planned for a later version.
- All of these checks are plain code: hashes, numbers and states, no LLM.

**Invoice number reused after an earlier invoice**

GST expects unique invoice numbers within a financial year, and corrections are normally made with a credit or debit note or a new invoice number; in practice, some vendors resend a corrected invoice with the same number.

| Earlier invoice with the same vendor + number | New invoice's contents | Outcome |
|---|---|---|
| Approved | Anything | Send back: "This invoice number has already been approved" |
| Rejected | Unchanged (same file hash, or same extracted fields) | Reject again, repeating the original rejection reason |
| Rejected | Changed (amounts or items differ) | Treat as a corrected version and process normally, with a note: "Reuses the number of rejected INT-0007; corrected version" |
| On hold | Anything | Wait for the human decision, then apply the rows above |

- "Unchanged" is checked two ways: an identical file hash, or identical extracted fields (vendor, invoice number, line items, quantities, unit prices, total) after normalisation. A plain equality check, not a fuzzy percentage.
- The approved list only blocks approved numbers, so a rejected number stays free for a genuine correction.
- Uniqueness key: vendor + invoice number (strictly, + financial year, as numbering can restart each April; the year part is not built).

**Near-duplicate by invoice number**

Same vendor, same amount and same date as an earlier invoice, but an invoice number differing by one character (e.g. INV-2083 vs INV-2084): human review (H9), with both invoices linked. Unlike "same items", this is a signal a reviewer can actually judge.

## On-hold logic and second invoices on the same PO

When an invoice cannot be confidently approved or rejected, it goes to a human and its whole PO goes On hold until the human decides.

- Human approves: the invoice is cleared and its quantities are added to the PO lines.
- Human rejects: the PO comes off hold and can be referenced again.

**Locks and processing order**

- On hold works as a write lock on the PO: nothing can change it while a human reviews, but it can still be viewed on the dashboard.
- Invoices are processed strictly one after the other, never two at once. This prevents two invoices on the same PO both reading the same remaining quantity and overbilling it.
- Every hold shows an "on hold since" timer on the dashboard, so a review that is never actioned (a lock never released) is visible.

- Each on-hold PO also shows a count of invoices waiting behind it and how long each has waited.

**A new invoice on a PO that is on hold** (example: PO347 on hold, a new invoice also references PO347)

- Chosen: Option B. The new invoice waits until the older one is decided, then is rerun automatically. This saves the human time.
- Not chosen: Option A, sending it for human review too, marked "referencing a PO on hold".
- Possible rejection reasons on rerun: different vendor name, different item list, different amount, or an exact duplicate of an approved invoice (would double the payment).

**Exact duplicate vs corrected version**

These are two different cases; the system must tell which one the second invoice is. Either way, the second invoice is only approved if the first is successfully rejected.

- **Case 1, same invoice sent twice:** wait, human check, or AI auto-detection of the copy (the duplicate check above).
- **Case 2, corrected version:** Option A, wait for a human to reject the first, PO frees up, AI reruns the second. Option B, a way for the vendor to say the earlier invoice (e.g. #384 7689) is wrong; this is a separate product, so treat it as "what I'd build next".

## Mismatched PO ID with matching items and amount

Not refused directly. A human may have a direct line to the vendor and can resolve it over a call.

| Situation | Outcome |
|---|---|
| PO number not found, but a PO from the same vendor matches the items and amounts (within tolerance) | Human review (H8): the candidate PO is shown; the reviewer confirms and applies the invoiced quantities to that PO line by line |
| PO number not found and no candidate PO matches | Send back (S9); human if reading confidence is low |

- Candidate search is plain code: filter open POs by vendor, then compare items (using the LLM line pairing), quantities against what remains, and prices against the tolerances.
- If several POs match, all candidates are shown and the reviewer picks one.
- The reviewer never closes a PO directly; the PO closes only when every line is fully invoiced.

Example reviewer note: "PO number PO-0412 not found. PO-0421 from the same vendor matches all items and amounts within tolerance; possible typo in the PO number. Please confirm."

## Human review and system requirements

Everything happens in one system: the PO data set, incoming invoices, AI decisions and human decisions.

- The AI approves, rejects, or passes to a human for review.
- The human approves or rejects in the same system, with defined reasons, and **notes are required on every decision** (what they checked, who they spoke to). This applies to a scanned invoice too: the reviewer has the document beside what the AI read and decides with a written reason.
- **On approval the reviewer states what the invoice bills:** the PO and the quantity per PO line, prefilled with the system's matches. If the system could not match the lines (AI unavailable, PO number not found, items not paired), the reviewer picks the PO and enters the quantities. The PO then becomes Partially or Fully invoiced from those quantities. The system refuses quantities above what remains, lines from two POs, or a PO held by another invoice's review.
  - Some reasons still need defining.
  - An "Other" option with details.
  - "Other" entries may later be categorised and added to the reason list.
- The AI writes detailed notes on every step: what it did and why.
- Every decision is traceable (audit trail): extracted values, what they were compared against, which rules passed or failed, the decision, a timestamp, and any human override.
- The UI is graded: a live run view showing each stage as it runs, and a dashboard of history, status and outputs across runs.

**Reason list (draft)**

Every AI and human decision carries one of these reasons, plus the details; "Other" entries can later be grouped into new reasons.

| Code | Outcome | Reason |
|---|---|---|
| A1 | Approve | All checks passed |
| A2 | Approve | Small shortfall within 2% / ₹10,000 (noted) |
| A3 | Approve | Shortfall explained by a stated discount |
| S1 | Send back | Required field missing (names the field) |
| S2 | Send back | Exact duplicate of an earlier invoice (links the original) |
| S3 | Send back | Invoice number already approved for this vendor |
| S4 | Send back | PO already fully invoiced |
| S5 | Send back | Quantity exceeds what remains on the PO line |
| S6 | Send back | Price or total more than 7% over the PO |
| S7 | Send back | Vendor does not match the PO |
| S8 | Send back | Line items do not match the PO |
| S9 | Send back | PO number not found |
| S10 | Send back | Unchanged resend of a rejected invoice (repeats the original reason) |
| H1 | Human review | Overbilled above the approve limit, up to 7% |
| H2 | Human review | Underbilled beyond 2% / ₹10,000 with no stated discount |
| H3 | Human review | Low reading confidence (scanned or blurry); shows what the AI inferred |
| H4 | Human review | OCR and LLM readings disagree (next step) |
| H5 | Human review | Totals do not reconcile |
| H6 | Human review | Vendor name is a close but unclear match |
| H7 | Human review | Bank details differ from vendor records (verify via a known contact) |
| R-A1 | Reviewer approves | Figures verified as correct |
| R-A2 | Reviewer approves | Price variance accepted (agreed change) |
| R-A3 | Reviewer approves | Discount or credit confirmed with the vendor |
| R-A4 | Reviewer approves | Vendor identity confirmed (name variant) |
| R-R1 | Reviewer rejects | Overbilled |
| R-R2 | Reviewer rejects | Wrong or missing items |
| R-R3 | Reviewer rejects | Duplicate |
| R-R4 | Reviewer rejects | Vendor or bank details could not be verified |
| R-R5 | Reviewer rejects | Incorrect invoice details |
| OTHER | Any | Other, with free-text details |

Added reason codes:

| Code | Outcome | Reason |
|---|---|---|
| H8 | Human review | PO number not found; candidate PO from the same vendor matches (possible typo) |
| H9 | Human review | Near-duplicate: invoice number differs by one character, same vendor, amount and date |
| H10 | Human review | Invoice date outside the allowed window |
| H11 | Human review | Implied tax rate is not a valid GST rate |
| H12 | Human review | A field could not be verified against the document text (source quote failed) |
| H13 | Human review | AI service unavailable; invoice could not be read |
| H14 | Human review | Not a tax invoice (quotation, proforma, credit note, ...): read in full and passed to a human, never sent back |

## Added after testing on unseen invoices

Six invoices in layouts the system was never tuned on (`test_invoices/unseen/`) were run on the live app. Three gaps were found and fixed:

- **Document type.** The AI now states what the document is (tax invoice, bill of supply, proforma, quotation, credit or debit note, purchase order, delivery challan, receipt, other) and quotes the words that show it. A tax invoice or bill of supply is processed as before. Anything else is still read in full, every check still runs, and it goes to a human (H14), never back to the vendor: the reviewer may have context we don't. Found because a quotation whose items matched the PO exactly was approved.
- **PO number written without its prefix.** When "P.O. No: 1011" doesn't match a PO exactly, it is matched on its digits (PO-1011), but only if that PO belongs to the invoice's vendor. Found because a valid invoice was sent back as "PO not found".
- **Model per document kind.** Text PDFs are read by Claude Haiku 4.5, then every key field is verified against the PDF's text. Image-only PDFs (scans, phone photos) are read by Claude Sonnet 5.5: nothing on an image can be verified, and in testing Haiku misread a GSTIN on a scan that Sonnet and Opus 5.5 read correctly. Scans lead with H3, and are always checked by a person.
- **Amounts with a currency prefix.** "Rs.4,500.00" is now read as 4,500; the dot after "Rs" was being taken for a decimal point.

## Build and demo reliability

Design choices that make the system dependable to run and to demo live.

- **One config file** holds every threshold (2% / ₹5,000, 7%, 2% / ₹10,000, date windows, GST rates, fuzzy-match limits). The AP team can tune limits without touching code.
- **Graceful degradation:** if the AI call fails, the invoice goes to human review (H13) with a clear message; the system never crashes.
- **Demo reset script** restores POs, vendor data and history to a clean starting state before every demo run.
- **Cached extractions** for the test invoices, keyed by file hash, so the rehearsed demo gives the same result every time even if the AI service is slow.
- **README** with how to run it, the design principle, and a scope-decisions list.

## Assumptions and out of scope

Noted as deliberate scope choices, to reference in the live pitch.

- POs list each item with quantity and unit price.
- Two-way match only (PO vs invoice). Real AP often does a three-way match with proof of goods received; that is a next step.
- No manual PO closing when a vendor never bills the final part.
- On hold applies to the whole PO, not individual lines.
- Vendor notification of corrected invoices is out of scope ("what I'd build next").
- Test data (PO data set and invoice PDFs) is self-created, as the brief allows.

**Learning from reviewer decisions.** Two kinds, handled differently:

- **Confirmed facts** (one decision is enough, never automatic), **built**: when a reviewer approves an H6 invoice they can tick "confirm this name as an alias of V-002". It adds one alias to one vendor, is logged with who and when, and can be removed; later invoices under that name pass the vendor check. A changed bank account is the exception: a fraud signal, so updating the vendor master needs a second person's approval (maker-checker); next version.
- **Patterns in decisions** (needs volume): when reviewers make the same decision for the same vendor or PO with the same reason **at least 7 times** (e.g. approving Vendor X's H1 invoices at 3–4% over), the system **suggests** a rule change ("raise Vendor X's approve limit to 4%?") for a finance lead to accept or decline. Never applied automatically. Below 7, nothing is suggested: one or two approvals may be one-off agreements.

GST matching. The invoice's GSTIN will be matched against our own list of GST numbers held for each vendor, not just checked for presence.

**Scope decisions for this build** (each has a path to production): email ingestion (an upload folder stands in), a real concurrent database (simple local storage instead), currency conversion (INR only), a full tax engine (rate sanity only), multiple invoices per PDF, three-way match, vendor notification channel, cloud OCR.

## Open items

Decided (item #4, duplicate vs corrected invoice): the duplicate rule applies only when the earlier invoice was **approved**, because the check exists to stop a second payment and a rejected invoice was never paid. "Changed" means any difference in vendor, invoice number, total, or a line's item, quantity or price after normalisation; a resend that changes only the date or layout counts as unchanged (S10).

| Earlier invoice with the same vendor + number | New invoice | Outcome |
|---|---|---|
| Any status | Byte-identical file | S2 duplicate (S10 if the earlier one was rejected) |
| Approved | Same amount | S2 duplicate |
| Approved | Different amount | S3, number already approved |
| Rejected | Same contents | S10, unchanged resend, original reason repeated |
| Rejected | Changed contents | Corrected version, processed normally with a note |
| In review | Anything | Waits for that decision, then reruns |

Final decisions before building:

- **OCR second read:** skipped for this build; scans always go to a human. Listed as a next step with what it would improve.
- **Edge cases:** split + over-billing, on-hold queue + rerun, scanned invoice over 7%, exact duplicate resend, plus the happy path.
- **Stack:** Python + Streamlit + SQLite + Claude API, deployed on Streamlit Community Cloud.

Next: build. Test data (PO table, vendor table, invoice PDFs) and the happy path come first.

## Next steps to Monday 11 AM

Start building today: a process that runs only the happy path beats an unfinished build that cannot run.

**Saturday 3 Oct**

- Close the open items with quick defaults; record each as an assumption.
- Pick 3–4 edge cases with range: one from the on-hold/duplicate logic, plus messy-input cases (scanned or missing field, amount over tolerance or split invoice exceeding the PO, vendor mismatch).
- Build test data: a PO table of about 10 POs (lines with qty and unit price) and 6–8 invoice PDFs, one happy path plus one per edge case.
- Build the happy path end to end: upload, extract, validate, match, decide, show result.

**Sunday 4 Oct**

- Add edge cases one at a time.
- Build the live run view (each stage appears as it runs) and the dashboard (history, status, outputs, exception rate, value caught).
- Deploy to a live link.

**Monday 5 Oct, before 11 AM**

- Rehearse the demo order: happy path, then edge cases.
- Record the 5-minute video.
- Email Palak both links.

Stack: whatever is fastest for you. Python + Streamlit + SQLite + Claude API is the quickest path; Supabase + Lovable only if already comfortable. If Sunday goes badly, email Palak before the deadline rather than going silent.

## Interview lines to reference

Key lines from the design discussion, grouped by the question they answer.

**Design principles**

- "The LLM reads, code decides, the human arbitrates."
- "I only send things to a human when the human has information the system doesn't."
- "The system can only talk to a vendor when it's sure it read the document correctly."
- "These are financial records, so the system never makes up a decision on its own" (why a mismatched PO is refused, not guessed).
- "Start strict, loosen with evidence" (why tolerances are tight to begin with).

**When to use AI and when not**

- "I used AI in three places, reading the invoice, matching line descriptions and explaining decisions, because those need interpretation. Every decision that touches money is made by deterministic rules, so it's consistent and auditable."
- "Code checks the AI's work: if the extracted numbers don't add up, the invoice goes to a human."
- "I didn't let the AI grade itself; confidence comes from checks it can't fake."
- "I separated 'did we read it right' from 'is the invoice right', and the PO never influences extraction, so the system can't talk itself into seeing what it expects."<br><br>

**Specific decisions**

- "I deliberately don't let lines offset each other" (overbilling on one line can't hide behind underbilling on another).
- "Every payment below the agreed price can be justified": a shortfall beyond 2% / ₹10,000 needs a visible reason, like a stated discount.
- "The exact-duplicate check runs first, whatever the PO state": an identical resend on a partially invoiced PO would otherwise be paid twice.
- "Invoices are processed one at a time"; at scale, only invoices on the same PO would need to be serialised.

**How is this different from Zapier or RPA?**

- "Zapier moves data along fixed paths. My process reads messy documents like a person would, applies business judgment, and knows when to ask for help."
- Rule-based automation breaks on variation and can't explain itself; this escalates instead of failing silently.

**Zamp context**

- "I modelled extraction on your bank-statement blog: LLM for semantic mapping, deterministic code for validation."
- "The founders' point about smart people stuck copying invoice numbers is exactly the AP clerk I built this for."
- Why Zamp: the reliability and human-in-the-loop problems in "Problems we're solving", finance-first focus, and first-hand integration pain from Hevo (the CIO post).
- If data infrastructure comes up: Zamp's analytics pipeline uses CDC into Pub/Sub into ClickHouse; CDC and replication are daily work at Hevo.
- On automating jobs: the AP clerk moves from data entry to exceptions, vendor issues and catching fraud, the work that needs judgment.

**What I'd build next**

- Three-way match (goods receipt) to catch disguised duplicates.
- GST matching against our own vendor GSTIN list.
- A vendor channel to flag corrected invoices.
- Cloud OCR for messy scans; short-paying (approve good lines only); clustering "Other" review reasons into new categories.
