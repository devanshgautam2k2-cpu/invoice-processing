"""Currency: recognise the invoice currency and convert foreign amounts to INR for checking.

Rates come from Frankfurter (European Central Bank reference rates) for the invoice date,
are saved in data/fx_cache.json so a replay gives the same answer, and fall back to the
rates in config.json if the service can't be reached. A converted invoice always goes to a
person (H16), with the rate, its date and its source in the note.
"""

import json
import os
import re
import urllib.request
from datetime import date

from .config import DATA_DIR, config

FX_CACHE = DATA_DIR / "fx_cache.json"
RUPEE_WORDS = {"", "inr", "rs", "rs.", "₹", "rupee", "rupees", "indian rupee", "indian rupees"}
SYMBOLS = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
           "aed": "AED", "dhs": "AED", "sgd": "SGD", "s$": "SGD", "¥": "JPY", "jpy": "JPY"}


class FxUnavailable(Exception):
    pass


def currency_code(raw: str | None) -> str | None:
    """'Rs.' / '₹' / '' -> 'INR'; '$' / 'USD' -> 'USD'; anything unrecognised -> None."""
    s = (raw or "").strip().lower()
    if s in RUPEE_WORDS or "rupee" in s or s.startswith("rs"):
        return "INR"
    if s in SYMBOLS:
        return SYMBOLS[s]
    m = re.fullmatch(r"[a-z]{3}", s)
    return s.upper() if m else None


def _load_cache() -> dict:
    return json.loads(FX_CACHE.read_text()) if FX_CACHE.exists() else {}


def rate_to_inr(code: str, on: date | None) -> tuple[float, str, str]:
    """(rate, rate date, source) for 1 unit of `code` in INR."""
    day = (on or date.today()).isoformat()
    key = f"{code}:{day}"
    cache = _load_cache()
    if key in cache:
        r = cache[key]
        return r["rate"], r["date"], r["source"]
    if not os.environ.get("INVOICE_OFFLINE"):
        try:
            url = f"https://api.frankfurter.dev/v1/{day}?base={code}&symbols=INR"
            req = urllib.request.Request(url, headers={"User-Agent": "invoice-processor/1.0"})  # default UA gets 403
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read())
            r = {"rate": float(body["rates"]["INR"]), "date": body["date"],
                 "source": "Frankfurter (European Central Bank reference rate)"}
            cache[key] = r
            FX_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))
            return r["rate"], r["date"], r["source"]
        except Exception:
            pass  # fall through to the configured fallback
    fallback = config()["currency"]["fallback_rates_to_inr"]
    if code in fallback:
        return float(fallback[code]), day, "fallback rate from config.json (exchange-rate service unreachable)"
    raise FxUnavailable(f"No exchange rate available for {code}")


def convert(n: dict, rate: float) -> None:
    """Scale every amount on the normalised invoice in place (quantities unchanged)."""
    r = lambda x: round(x * rate, 2) if x is not None else None  # noqa: E731
    for l in n["lines"]:
        l["unit_price"], l["amount"] = r(l["unit_price"]), r(l["amount"])
    for t in n["taxes"]:
        t["amount"] = r(t["amount"])
    for d in n["discounts"]:
        d["amount"] = r(d["amount"])
    n["subtotal"], n["grand_total"] = r(n["subtotal"]), r(n["grand_total"])
