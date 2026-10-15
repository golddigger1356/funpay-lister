"""Offline tests for the final live price check before creating an offer."""

from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funpay import FunPayClient, FunPayError
from test_funpay_client import HOME, ITEM, editor


class LivePriceFixtureClient(FunPayClient):
    def __init__(self):
        super().__init__()
        self.multiplier = Decimal("1.10")
        self.calls = []
        self.forms = 0
        self.calc_error = None

    def _request(self, path, payload=None, *, readonly=False):
        self.calls.append((path, dict(payload) if payload is not None else None, readonly))
        if path == "/":
            return HOME
        if path.startswith("/lots/offerEdit"):
            self.forms += 1
            return editor("live-token-" + str(self.forms))
        if path == "/lots/calc":
            if self.calc_error:
                raise self.calc_error
            buyer = (Decimal(payload["price"]) * self.multiplier).quantize(Decimal("0.01"))
            return json.dumps({"methods": [{"unit": "₽", "price": str(buyer)}]})
        if path == "/lots/offerSave":
            return json.dumps({"done": True, "url": "/lots/offer?id=777"})
        raise AssertionError(f"Unexpected fixture request: {path}")

    @property
    def saves(self):
        return [payload for path, payload, _ in self.calls if path == "/lots/offerSave"]

    @property
    def calculations(self):
        return [entry for entry in self.calls if entry[0] == "/lots/calc"]


def buyer_item(quote):
    return {**ITEM, "price": quote["seller_price"], "buyer_target_rub": quote["target_rub"],
            "buyer_price_rub": quote["buyer_price_rub"], "seller_currency": quote["seller_currency"]}


class LiveBuyerPriceCheckTests(unittest.TestCase):
    def connected(self):
        client = LivePriceFixtureClient()
        client.connect("live-price-fixture-key-never-real", "123")
        return client

    def test_fee_change_stops_before_save_clears_cache_and_requotes_live(self):
        client = self.connected()
        original = client.quote_buyer_rub("110.00")
        self.assertEqual(original["seller_price"], "100.00")
        self.assertEqual(original["buyer_price_rub"], "110.00")
        self.assertTrue(client._buyer_quote_cache)
        calculations_before = len(client.calculations)
        client.multiplier = Decimal("1.20")

        with self.assertRaises(FunPayError) as raised:
            client.publish(buyer_item(original))
        self.assertEqual(raised.exception.kind, "pricing")
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(client.saves, [])
        self.assertEqual(client._buyer_quote_cache, {})
        self.assertEqual(len(client.calculations), calculations_before + 1)
        self.assertEqual(client.calculations[-1][1]["price"], "100.00")
        self.assertTrue(client.calculations[-1][2])

        changed = client.quote_buyer_rub("110.00")
        self.assertNotEqual(changed["seller_price"], original["seller_price"])
        self.assertEqual(changed["seller_price"], "91.67")
        self.assertEqual(changed["buyer_price_rub"], "110.00")
        result = client.publish(buyer_item(changed))
        self.assertEqual(len(client.saves), 1)
        self.assertEqual(client.saves[0]["price"], "91.67")
        self.assertEqual(result["url"], "https://funpay.com/lots/offer?id=777")
        self.assertTrue(all(readonly for _, _, readonly in client.calculations))

    def test_unchanged_buyer_price_is_checked_before_single_save(self):
        client = self.connected()
        quote = client.quote_buyer_rub("110.00")
        calculation_count = len(client.calculations)
        client.publish(buyer_item(quote))
        self.assertEqual(len(client.calculations), calculation_count + 1)
        self.assertEqual(len(client.saves), 1)
        last_paths = [path for path, _, _ in client.calls[-3:]]
        self.assertEqual(last_paths, ["/lots/offerEdit?node=123", "/lots/calc", "/lots/offerSave"])
        self.assertEqual(client.saves[0]["price"], "100.00")

    def test_calculator_unavailable_before_publish_is_known_failure_and_never_saves(self):
        client = self.connected()
        quote = client.quote_buyer_rub("110.00")
        client.calc_error = FunPayError("Could not reach FunPay", kind="network", uncertain=False)
        with self.assertRaises(FunPayError) as raised:
            client.publish(buyer_item(quote))
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(client.saves, [])
        self.assertTrue(client.calculations[-1][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
