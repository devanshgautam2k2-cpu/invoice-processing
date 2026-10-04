"""The three places the LLM is used: read the invoice, pair line descriptions, narrate the decision.

The LLM never sees the PO during extraction and never decides money. Every call is
cached on disk by a hash of its inputs, so a rehearsed demo gives identical results
and still works if the API is slow or down.
"""

import base64
import hashlib
import json
import os

import anthropic

from .config import CACHE_DIR, api_key, config

PROMPT_VERSION = "v3"
NOTE_PROMPT_VERSION = "v3"


class LLMUnavailable(Exception):
    """The AI service could not be reached or returned something unusable (-> H13)."""


# ---------------------------------------------------------------- plumbing

def _client() -> anthropic.Anthropic:
    key = api_key()
    if not key:
        raise LLMUnavailable("No ANTHROPIC_API_KEY configured")
    return anthropic.Anthropic(api_key=key, max_retries=config()["llm"]["max_retries"])


def _cache_path(kind: str, key: dict):
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]
    return CACHE_DIR / f"{kind}_{digest}.json"


def _cached_call(kind: str, key: dict, make_request) -> tuple[dict, bool]:
    """Return (result, from_cache)."""
    path = _cache_path(kind, key)
    if path.exists():
        return json.loads(path.read_text()), True
    if os.environ.get("INVOICE_OFFLINE"):  # regression tests: never spend on the API
        raise LLMUnavailable(f"offline mode: no cached {kind} result")
    result = make_request()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result, False


def _json_call(model: str, system: str, content: list, schema: dict, max_tokens: int = 8000) -> dict:
    try:
        resp = _client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            # Haiku 4.5 honours temperature 0; Sonnet 5.5 / Opus 5.5 reject any non-default sampling setting.
            **({"extra_body": {"temperature": config()["llm"]["temperature"]}} if model.startswith("claude-haiku") else {}),
        )
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable(f"Could not reach the AI service: {e}") from e
    except anthropic.APIStatusError as e:
        detail = (e.body or {}).get("error", {}).get("message", "") if isinstance(e.body, dict) else ""
        raise LLMUnavailable(f"AI service error {e.status_code}" + (f" ({detail})" if detail else "")) from e
    if resp.stop_reason != "end_turn":
        raise LLMUnavailable(f"AI response incomplete (stop_reason={resp.stop_reason})")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as e:
        raise LLMUnavailable("AI returned malformed JSON") from e


def _str() -> dict:
    # Structured outputs cap union types, so "not found" is an empty string, not null.
    return {"type": "string"}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ---------------------------------------------------------------- 1. extraction

FIELD = _obj({"value": _str(), "quote": _str()})

SCALAR_FIELDS = [
    "vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number",
    "buyer_name", "buyer_gstin", "bank_name", "bank_account_number", "bank_ifsc",
    "currency", "subtotal", "grand_total",
]

DOCUMENT_TYPES = ["tax_invoice", "bill_of_supply", "proforma_invoice", "quotation", "credit_note", "debit_note",
                  "purchase_order", "delivery_challan", "receipt", "other"]

EXTRACTION_SCHEMA = _obj({
    "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
    "document_type_quote": _str(),
    **{f: FIELD for f in SCALAR_FIELDS},
    "line_items": {"type": "array", "items": _obj({
        "description": {"type": "string"},
        "hsn": _str(),
        "quantity": _str(),
        "unit_price": _str(),
        "amount": _str(),
        "quote": {"type": "string"},
    })},
    "tax_lines": {"type": "array", "items": _obj({
        "label": {"type": "string"},
        "rate_percent": _str(),
        "amount": _str(),
        "quote": {"type": "string"},
    })},
    "discounts": {"type": "array", "items": _obj({
        "label": {"type": "string"},
        "percent": _str(),
        "amount": _str(),
        "quote": {"type": "string"},
    })},
    "reader_notes": _str(),
})

EXTRACTION_SYSTEM = """You read vendor invoices for an accounts-payable team. Your only job is to transcribe what is printed; you never judge whether the invoice is correct.

Rules:
- Copy every value exactly as printed (keep commas, currency marks, date format, unit words). Do not compute, convert, round or reformat anything.
- For every field also return "quote": the exact, contiguous text from the document the value was read from, including its label when the label is next to it (e.g. "PO No.: PO-1001"). Copy the quote character for character.
- If a field is not on the document, return an empty string "" for both value and quote. Never guess or fill in a value from context. Before returning "", search the whole document again: header, footer, small print, payment section.
- vendor_* fields describe the supplier issuing the invoice; buyer_* fields describe the customer it is billed to.
- po_number is the buyer's purchase order reference (it may be labelled PO No., Your Ref, Purchase Order, Order Ref, etc.).
- subtotal is the taxable value before tax; grand_total is the final amount payable.
- line_items: one entry per billed item row; quote = the row's text as printed. A line's amount is its value BEFORE tax (quantity x rate, after any line discount): when a row shows both a taxable value and a tax-inclusive total, use the taxable value.
- tax_lines: one entry per tax row (CGST, SGST, IGST, etc.).
- discounts: any discount, rebate or "less" line; empty list if none.
- reader_notes: anything that made reading hard (blur, skew, handwriting, cut-off text), else "".
- document_type: what this document IS, judged from its title and wording: tax_invoice (a GST tax invoice, or a plain "Invoice" asking for payment), bill_of_supply, proforma_invoice, quotation (also estimate, quote, offer), credit_note, debit_note, purchase_order, delivery_challan, receipt, or other. document_type_quote = the exact words that show it (e.g. "QUOTATION", "Tax Invoice").
- If it is not a tax invoice, still transcribe every field you can find, but leave invoice_number "" unless the document really prints an invoice number (a quote or order number is not an invoice number)."""


def extract_invoice(pdf: bytes, file_hash: str, scanned: bool = False) -> tuple[dict, bool]:
    """Scans go to the stronger reader: nothing on an image can be checked against a text layer."""
    cfg = config()["llm"]
    model = cfg["scan_extraction_model"] if scanned else cfg["extraction_model"]

    def request():
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.standard_b64encode(pdf).decode()}},
            {"type": "text", "text": "Transcribe this document into the required JSON."},
        ]
        return _json_call(model, EXTRACTION_SYSTEM, content, EXTRACTION_SCHEMA)

    return _cached_call("extract", {"file": file_hash, "model": model, "v": PROMPT_VERSION}, request)


# ---------------------------------------------------------------- 2. line matching

LINE_MATCH_SYSTEM = """You pair invoice line descriptions with purchase-order line descriptions.
Two lines match when they describe the same product or service, even if worded differently (abbreviations, reordered specs, extra colour or pack details).
Return a PO line id ONLY from the shortlist provided, or "UNMATCHED" if no PO line describes the same item. Never pair two different products because they are similar in category (e.g. a monitor is not a laptop; a stapler is not paper).
Do not consider quantities or prices; only what the item is. Give a one-sentence reason for each pairing."""


def match_lines(invoice_lines: list[str], po_lines: list[dict]) -> tuple[list[dict], bool]:
    """invoice_lines: descriptions; po_lines: [{line_id, description}] shortlist from code."""
    model = config()["llm"]["line_match_model"]
    ids = [l["line_id"] for l in po_lines] + ["UNMATCHED"]
    schema = _obj({"matches": {"type": "array", "items": _obj({
        "invoice_line": {"type": "integer"},
        "po_line_id": {"type": "string", "enum": ids},
        "reason": {"type": "string"},
    })}})
    payload = {
        "invoice_lines": [{"invoice_line": i, "description": d} for i, d in enumerate(invoice_lines, 1)],
        "po_line_shortlist": [{"po_line_id": l["line_id"], "description": l["description"]} for l in po_lines],
    }

    def request():
        content = [{"type": "text", "text": json.dumps(payload, indent=2)}]
        return _json_call(model, LINE_MATCH_SYSTEM, content, schema, max_tokens=2000)

    result, cached = _cached_call("linematch", {"payload": payload, "model": model, "v": PROMPT_VERSION}, request)
    return result["matches"], cached


# ---------------------------------------------------------------- 3. decision note

NOTE_SYSTEM = """You write the decision note an accounts-payable clerk reads for one vendor invoice.
The decision has already been made by deterministic rules; you only explain it. Never change, soften or second-guess the outcome, and never add facts that are not in the input.
Write 2-5 short sentences of plain English: lead with the outcome and the main reason, then each flagged issue with its numbers exactly as given in the input (write amounts as "Rs 9,41,640", never the rupee symbol), then what the reader should do next if anything. No headings, no bullet points."""


def write_note(facts: dict) -> tuple[str, bool]:
    model = config()["llm"]["line_match_model"]
    schema = _obj({"note": {"type": "string"}})

    def request():
        content = [{"type": "text", "text": json.dumps(facts, indent=2, default=str)}]
        return _json_call(model, NOTE_SYSTEM, content, schema, max_tokens=1000)

    result, cached = _cached_call("note", {"facts": facts, "model": model, "v": NOTE_PROMPT_VERSION}, request)
    return result["note"], cached
