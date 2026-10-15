"""Invert FunPay's live buyer-price calculator without fixed fee assumptions."""

from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")
MAX_PRICE = Decimal("1000000000")
MAX_REQUESTS = 8
_RUB = re.compile(r"₽|\b(?:RUB|RUR|РУБ(?:\.|ЛЕЙ|ЛЬ|ЛЯ)?)\b|\bР\.", re.IGNORECASE)


class PricingError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.kind = "pricing"
        self.uncertain = False


def _amount(raw) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise PricingError("Enter a positive numeric buyer price in RUB.")
    text = html.unescape(str(raw)).strip()
    text = re.sub(r"[\s\u00a0\u202f]", "", text)
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        text = text.replace(grouping_separator, "").replace(decimal_separator, ".")
    else:
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,12})?", text):
        raise PricingError("Enter a positive numeric buyer price in RUB, for example 100 or 100.50.")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise PricingError("The buyer price is invalid.") from None
    if not value.is_finite() or value <= 0:
        raise PricingError("The buyer price must be greater than zero.")
    return value


def _rub(unit) -> bool:
    return bool(_RUB.search(html.unescape(str(unit or ""))))


def buyer_rub_amount(raw: dict) -> Decimal:
    """Get the RUB listing amount, never compare numeric prices across currencies.

    minPrice is the calculator's minimum price including commission, rather than
    a minimum permissible seller price. Only use it when its currency is RUB.
    Otherwise use the cheapest method explicitly quoted in RUB.
    """
    if not isinstance(raw, dict) or raw.get("error") or raw.get("errors"):
        raise PricingError("FunPay could not calculate the buyer price. No offer was saved.")
    minimum = raw.get("minPrice")
    if isinstance(minimum, str) and _rub(minimum):
        number = _RUB.sub("", html.unescape(minimum)).strip(" .")
        try:
            return _amount(number)
        except PricingError:
            pass
    methods = raw.get("methods")
    amounts = []
    if isinstance(methods, list):
        for method in methods:
            if not isinstance(method, dict) or not _rub(method.get("unit")):
                continue
            try:
                amounts.append(_amount(method.get("price")))
            except PricingError:
                continue
    if not amounts:
        raise PricingError("FunPay did not return a usable buyer price in RUB. No offer was saved.")
    return min(amounts)


def _display(value: Decimal, *, cents: bool = False) -> str:
    if cents:
        return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")
    text = format(value, "f")
    integer, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0")
    return integer + "." + fraction.ljust(2, "0")


def _cent_index(value: Decimal) -> int:
    value = max(CENT, min(MAX_PRICE, value))
    return int((value / CENT).to_integral_value(rounding=ROUND_HALF_UP))


def quote_buyer_rub(calc, target, seller_currency: str) -> dict:
    """Find a cent-denominated seller amount and verify it through ``calc``.

    ``calc(net_string)`` must return the live /lots/calc JSON response and must
    not create an offer. All returned amounts come from actual callback results;
    callback failures propagate unchanged. At most eight callback calls are made.
    Currency conversion, fixed charges, tiers, and rounding stay on FunPay's side.
    """
    wanted = _amount(target)
    if wanted > MAX_PRICE:
        raise PricingError("The buyer price cannot exceed 1,000,000,000 RUB.")
    currency = str(seller_currency or "").strip().upper()
    if currency not in {"RUB", "USD", "EUR", "UAH"}:
        raise PricingError("FunPay's seller currency could not be identified. Reconnect before calculating prices.")
    if not callable(calc):
        raise PricingError("The live FunPay price calculator is unavailable.")

    maximum_index = int(MAX_PRICE / CENT)
    samples: dict[int, Decimal] = {}
    calls = 0
    last_index = None

    def sample(index: int) -> Decimal:
        nonlocal calls, last_index
        index = max(1, min(maximum_index, index))
        if calls >= MAX_REQUESTS:
            raise PricingError("The live price calculation request limit was reached.")
        calls += 1
        quoted = buyer_rub_amount(calc(_display(Decimal(index) * CENT, cents=True)))
        samples[index] = quoted
        last_index = index
        return quoted

    def best_index() -> int:
        # If equally close, choose a buyer amount that does not exceed the target.
        return min(samples, key=lambda index: (
            abs(samples[index] - wanted), samples[index] > wanted, index,
        ))

    def result(index: int) -> dict:
        buyer = samples[index]
        exact = buyer == wanted
        note = "Matches the requested buyer listing price. Checkout can vary by payment method."
        if not exact:
            note = f"Closest amount available at {_display(buyer)} RUB from the prices checked. Checkout can vary by payment method."
            if samples.get(1, Decimal("0")) > wanted:
                note = "The requested amount is below the buyer price FunPay quoted at the minimum seller price. " + note
        return {"target_rub": _display(wanted), "seller_price": _display(Decimal(index) * CENT, cents=True),
                "buyer_price_rub": _display(buyer), "seller_currency": currency,
                "exact": exact, "note": note}

    initial_index = _cent_index(wanted)
    initial_quote = sample(initial_index)
    if initial_quote == wanted:
        return result(initial_index)
    # This ratio is only a starting sample, never an assumed commission formula.
    candidate = _cent_index(wanted * Decimal(initial_index) * CENT / initial_quote)
    if candidate == initial_index:
        candidate += -1 if initial_quote > wanted else 1
        candidate = max(1, min(maximum_index, candidate))
    if candidate not in samples:
        sample(candidate)
        if samples[candidate] == wanted:
            return result(candidate)

    # Reserve one request to verify the selected pair again if the final sampled
    # candidate was different. Exact matches are already the latest actual quote.
    while calls < MAX_REQUESTS - 1:
        best = best_index()
        below = [index for index, quoted in samples.items() if quoted < wanted]
        above = [index for index, quoted in samples.items() if quoted > wanted]
        lower = max(below) if below else None
        upper = min(above) if above else None
        bracket = lower is not None and upper is not None and lower < upper
        if bracket and upper - lower <= 1:
            break
        if samples.get(1, Decimal("0")) > wanted:
            break
        if maximum_index in samples and samples[maximum_index] < wanted:
            break

        nearest = sorted(samples, key=lambda index: abs(samples[index] - wanted))
        first = nearest[0]
        second = next((index for index in nearest[1:] if samples[index] != samples[first]), None)
        if second is not None:
            # Interpolate from the two nearest observed quotes, including fixed
            # charges. Constrain it to any known opposite-side price bracket.
            slope = (samples[first] - samples[second]) / Decimal(first - second)
            if slope > 0:
                estimate = Decimal(first) + (wanted - samples[first]) / slope
                candidate = int(estimate.to_integral_value(rounding=ROUND_HALF_UP))
            elif bracket:
                candidate = (lower + upper) // 2
            else:
                candidate = 1 if not below else maximum_index
        elif not below:
            candidate = 1
        elif not above:
            candidate = maximum_index
        else:
            candidate = best
        candidate = max(1, min(maximum_index, candidate))
        if bracket:
            candidate = max(lower + 1, min(upper - 1, candidate))
        if candidate in samples:
            # Check adjacent valid seller cents when the equation falls on an
            # already-observed amount; this resolves rounding plateaus/gaps.
            direction = -1 if samples[best] > wanted else 1
            neighbors = (best + direction, best - direction)
            candidate = next((index for index in neighbors
                              if 1 <= index <= maximum_index and index not in samples
                              and (not bracket or lower < index < upper)), None)
            if candidate is None and bracket:
                candidate = (lower + upper) // 2
                if candidate in samples:
                    candidate = next((index for index in range(lower + 1, min(upper, lower + 8)) if index not in samples), None)
            if candidate is None or candidate in samples:
                break
        sample(candidate)
        if samples[candidate] == wanted:
            return result(candidate)

    selected = best_index()
    if selected != last_index and calls < MAX_REQUESTS:
        sample(selected)
    # Use this last observed seller/buyer pair even if the live quote changed.
    # A changed quote is displayed as inexact instead of trusting an old result.
    return result(selected)
