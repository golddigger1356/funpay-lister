from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pricing import PricingError, buyer_rub_amount, quote_buyer_rub


CENT = Decimal("0.01")


class Calculator:
    def __init__(self, formula, *, minimum=None, include_min=True):
        self.formula = formula
        self.minimum = minimum
        self.include_min = include_min
        self.calls = []

    def __call__(self, net):
        self.calls.append(net)
        if len(self.calls) > 8:
            raise AssertionError("More than eight remote calculation calls")
        amount = self.formula(Decimal(net))
        if self.minimum is not None:
            amount = max(self.minimum, amount)
        price = format(amount.quantize(CENT, rounding=ROUND_HALF_UP), "f")
        response = {"methods": [{"name": "RUB method", "price": price, "unit": "₽", "sort": 0}, {"name": "Expensive method", "price": str(Decimal(price) + 5), "unit": "RUB", "sort": 1}]}
        if self.include_min:
            response["minPrice"] = price + " ₽"
        return response


class PricingTests(unittest.TestCase):
    def assert_observed_result(self, calc, result):
        self.assertIn(result["seller_price"], calc.calls)
        self.assertEqual(result["seller_price"], calc.calls[-1])
        self.assertLessEqual(len(calc.calls), 8)
        self.assertGreaterEqual(Decimal(result["seller_price"]), CENT)

    def test_localized_min_price_and_currency(self):
        for text in ("1 234,50 ₽", "1\u00a0234.50 RUB", "1\u202f234,50 руб.", "1,234.50 RUB", "1.234,50 ₽", "1234.50 рублей"):
            self.assertEqual(buyer_rub_amount({"minPrice": text}), Decimal("1234.50"))

    def test_foreign_minimum_never_compared_as_numeric_rubles(self):
        response = {"minPrice": "1.50 $", "methods": [{"price": "2.00", "unit": "€"}, {"price": "200,00", "unit": "₽"}, {"price": "180.50", "unit": "RUB"}]}
        self.assertEqual(buyer_rub_amount(response), Decimal("180.50"))

    def test_ruble_min_price_is_server_listing_price(self):
        self.assertEqual(buyer_rub_amount({"minPrice": "100 ₽", "methods": [{"price": "105", "unit": "RUB"}]}), Decimal("100"))

    def test_missing_rub_and_error_responses_rejected(self):
        for raw in ({}, {"methods": [{"price": "1.00", "unit": "$"}]}, {"error": "denied", "minPrice": "100 ₽"}, {"errors": [["price", "bad"]]}, {"minPrice": "NaN RUB"}, {"methods": [{"price": "-1", "unit": "RUB"}]}, [], None):
            with self.assertRaises(PricingError):
                buyer_rub_amount(raw)

    def test_exact_linear_ten_percent_live_verified(self):
        calc = Calculator(lambda net: net * Decimal("1.1"))
        result = quote_buyer_rub(calc, "100", "RUB")
        self.assertTrue(result["exact"])
        self.assertEqual(result["seller_price"], "90.91")
        self.assertEqual(result["buyer_price_rub"], "100.00")
        self.assert_observed_result(calc, result)

    def test_fixed_fee_uses_observed_slope(self):
        calc = Calculator(lambda net: net + Decimal("5"))
        result = quote_buyer_rub(calc, "100", "RUB")
        self.assertTrue(result["exact"])
        self.assertEqual(result["seller_price"], "95.00")
        self.assert_observed_result(calc, result)

    def test_minimum_unattainable_target(self):
        calc = Calculator(lambda net: net * Decimal("1.1"), minimum=Decimal("5"))
        result = quote_buyer_rub(calc, "1", "RUB")
        self.assertFalse(result["exact"])
        self.assertEqual(result["buyer_price_rub"], "5.00")
        self.assertIn("minimum seller price", result["note"])
        self.assert_observed_result(calc, result)

    def test_non_linear_tier_near_gap_checks_adjacent_cents(self):
        calc = Calculator(lambda net: net * (Decimal("1.1") if net < 50 else Decimal("1.2")))
        result = quote_buyer_rub(calc, "55", "RUB")
        self.assertFalse(result["exact"])
        self.assertEqual(result["buyer_price_rub"], "54.99")
        self.assertEqual(result["seller_price"], "49.99")
        self.assert_observed_result(calc, result)

    def test_smooth_non_linear_curve_is_verified(self):
        calc = Calculator(lambda net: net + net * net / 1000)
        result = quote_buyer_rub(calc, "100", "RUB")
        self.assertTrue(result["exact"])
        self.assertEqual(result["buyer_price_rub"], "100.00")
        self.assert_observed_result(calc, result)

    def test_rounding_gap_tie_prefers_at_or_below_target(self):
        calc = Calculator(lambda net: net * 2)
        result = quote_buyer_rub(calc, "0.03", "RUB")
        self.assertFalse(result["exact"])
        self.assertEqual(result["seller_price"], "0.01")
        self.assertEqual(result["buyer_price_rub"], "0.02")
        self.assert_observed_result(calc, result)

    def test_non_rub_seller_currency_uses_live_conversion(self):
        calc = Calculator(lambda net: net * 100)
        result = quote_buyer_rub(calc, "100", "USD")
        self.assertTrue(result["exact"])
        self.assertEqual(result["seller_price"], "1.00")
        self.assertEqual(result["seller_currency"], "USD")
        self.assert_observed_result(calc, result)

    def test_no_rounded_false_exact_for_four_decimal_quote(self):
        calc = Calculator(lambda net: net)
        def four_decimal(net):
            calc.calls.append(net)
            return {"minPrice": "100.0040 RUB"}
        result = quote_buyer_rub(four_decimal, "100", "RUB")
        self.assertFalse(result["exact"])
        self.assertEqual(result["buyer_price_rub"], "100.004")
        self.assert_observed_result(calc, result)

    def test_invalid_input_never_calls_remote_calculator(self):
        for target in ("", "0", "-1", "NaN", "Infinity", "1e2", None, True, "1000000001", "x"):
            calc = Calculator(lambda net: net)
            with self.assertRaises(PricingError):
                quote_buyer_rub(calc, target, "RUB")
            self.assertFalse(calc.calls)
        calc = Calculator(lambda net: net)
        with self.assertRaises(PricingError):
            quote_buyer_rub(calc, "100", "Account currency")
        self.assertFalse(calc.calls)

    def test_callback_exception_stops_and_propagates(self):
        marker = RuntimeError("Stop on a transport failure")
        calls = []
        def fail(net):
            calls.append(net)
            raise marker
        with self.assertRaises(RuntimeError) as raised:
            quote_buyer_rub(fail, "100", "RUB")
        self.assertIs(raised.exception, marker)
        self.assertEqual(len(calls), 1)

    def test_request_budget_even_for_discontinuous_curve(self):
        calc = Calculator(lambda net: Decimal("10") if net < 1 else Decimal("10000000"))
        result = quote_buyer_rub(calc, "100", "RUB")
        self.assertFalse(result["exact"])
        self.assert_observed_result(calc, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
