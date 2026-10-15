"""Buyer RUB job tests with an offline calculator and in-memory HTTP requests."""

from __future__ import annotations

import copy
from decimal import Decimal
import http.client
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import app
from funpay import FunPayError
from capacity_fixtures import EmptyOffersManager


def item(item_id="buyer-item", price="100.00", **extra):
    return {"id": item_id, "title_ru": "Предмет", "title_en": "Item",
            "description_ru": "Описание", "description_en": "Description", "price": price, **extra}


class PriceClient:
    def __init__(self, *, entered=None, release=None, error=None, quote_overrides=None):
        self.calculations = []
        self.published = []
        self.entered = entered
        self.release = release
        self.error = error
        self.quote_overrides = quote_overrides or {}

    def connect(self, key, category):
        self.category_id = str(category)
        self.user_id = 777
        return {"account": "Fixture Seller", "category": {"id": str(category), "name": "Fixture Items"},
                "currency": "USD", "fields": []}

    def quote_buyer_rub(self, target):
        target = format(Decimal(str(target).replace(",", ".")), ".2f")
        self.calculations.append(target)
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(2):
            raise AssertionError("test failed to release quote calculator")
        if self.error:
            raise self.error
        return {"target_rub": target, "seller_price": format(Decimal(target) / 100, ".2f"),
                "buyer_price_rub": target, "seller_currency": "USD", "exact": True,
                "note": "Fixture conversion", **self.quote_overrides}

    def publish(self, value, fields, active=True):
        self.published.append((copy.deepcopy(value), copy.deepcopy(fields), active))
        return {"url": "https://funpay.com/lots/offer?id=99", "seller_price": value["price"],
                "buyer_price_rub": value.get("buyer_price_rub")}


class PriceJobsTests(unittest.TestCase):
    def configured(self, client=None):
        client = client or PriceClient()
        instance = app.Lister(client_factory=lambda: client, manager_factory=EmptyOffersManager)
        instance.connect({"golden_key": "fixture-price-key-never-real", "category": "456"})
        return instance, client

    def request(self, instance, method, path, body=None, *, token=True):
        headers = {"Host": "127.0.0.1:8765"}
        if token:
            headers["X-Lister-Token"] = instance.token
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"

        class MemorySocket:
            def __init__(self, data):
                self.data = data
                self.output = bytearray()

            def makefile(self, mode, *args):
                return io.BytesIO(self.data)

            def sendall(self, data):
                self.output.extend(data)

        request = MemorySocket(head.encode() + payload)
        app.Handler(request, ("127.0.0.1", 1), SimpleNamespace(server_port=8765, lister=instance))
        response = http.client.HTTPResponse(MemorySocket(bytes(request.output)))
        response.begin()
        return response.status, json.loads(response.read())

    def quote(self, instance, prices=("100.00",)):
        status, response = self.request(instance, "POST", "/api/quote", {"prices": list(prices), "price_mode": "buyer_rub"})
        self.assertEqual(status, 200, response)
        self.assertIn("quote_id", response)
        result = self.wait_quote(instance, response["quote_id"])
        self.assertEqual(result["state"], "completed", result)
        return response["quote_id"], result

    def wait_quote(self, instance, quote_id):
        deadline = time.monotonic() + 2
        while True:
            status, result = self.request(instance, "GET", "/api/quote?id=" + quote_id)
            self.assertEqual(status, 200, result)
            if result["state"] != "running":
                return result
            if time.monotonic() > deadline:
                self.fail("offline quote job failed to finish")
            time.sleep(0.005)

    def wait_batch(self, instance, job_id):
        deadline = time.monotonic() + 2
        while True:
            status, result = self.request(instance, "GET", "/api/job?id=" + job_id)
            self.assertEqual(status, 200, result)
            if result["state"] != "running":
                return result
            if time.monotonic() > deadline:
                self.fail("offline batch failed to finish")
            time.sleep(0.005)

    def test_invalid_price_modes_are_rejected_before_calculation_or_publish(self):
        for mode in [None, "seller", "buyer", "RUB", "", False, []]:
            instance, client = self.configured()
            for path, body in [("/api/quote", {"prices": ["100"], "price_mode": mode}),
                               ("/api/start", {"items": [item()], "price_mode": mode})]:
                with self.subTest(mode=mode, path=path):
                    status, _ = self.request(instance, "POST", path, body)
                    self.assertEqual(status, 400)
            self.assertEqual(client.calculations, [])
            self.assertEqual(client.published, [])

    def test_buyer_mode_cannot_publish_without_completed_quote(self):
        instance, client = self.configured()
        for extra in [{}, {"quote_id": "unknown"}, {"price_mode": "buyer_rub"}]:
            with self.subTest(extra=extra):
                status, response = self.request(instance, "POST", "/api/start", {"items": [item()], **extra})
                self.assertEqual(status, 409, response)
        self.assertEqual(client.published, [])

    def test_quotes_normalize_and_deduplicate_targets(self):
        instance, client = self.configured()
        _, result = self.quote(instance, ["100", "100.00", "100,0", "50.00", "50"])
        self.assertEqual(client.calculations, ["100.00", "50.00"])
        self.assertEqual(set(result["quotes"]), {"100.00", "50.00"})
        self.assertEqual(result["quotes"]["100.00"]["seller_price"], "1.00")
        self.assertEqual(result["quotes"]["100.00"]["buyer_price_rub"], "100.00")
        self.assertGreater(result["expires_in"], 0)
        self.assertLessEqual(result["expires_in"], 300)
        self.assertEqual(client.published, [])

    def test_server_quote_wins_over_frontend_price_tampering(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        submitted = item(seller_price="0.01", buyer_target_rub="1.00", buyer_price_rub="1.00")
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [submitted], "price_mode": "buyer_rub", "quote_id": quote_id,
            "quotes": {"100.00": {"seller_price": "0.01", "buyer_price_rub": "1.00"}}})
        self.assertEqual(status, 200, response)
        job = self.wait_batch(instance, response["job_id"])
        self.assertEqual(job["items"][0]["state"], "published")
        self.assertEqual(len(client.published), 1)
        sent = client.published[0][0]
        self.assertEqual(sent["price"], "1.00")
        self.assertEqual(sent["buyer_target_rub"], "100.00")
        self.assertEqual(sent["buyer_price_rub"], "100.00")
        self.assertEqual(client.calculations, ["100.00"])

    def test_changed_target_or_unknown_quote_id_cannot_publish(self):
        for changed in [{"price": "101.00"}, {"quote_id": "different-quote"}]:
            instance, client = self.configured()
            quote_id, _ = self.quote(instance)
            status, response = self.request(instance, "POST", "/api/start", {
                "items": [item(price=changed.get("price", "100.00"))], "price_mode": "buyer_rub",
                "quote_id": changed.get("quote_id", quote_id)})
            with self.subTest(changed=changed):
                self.assertEqual(status, 409, response)
                self.assertEqual(client.published, [])

    def test_reconnect_to_other_category_invalidates_completed_quote(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        instance.disconnect()
        instance.connect({"golden_key": "fixture-price-key-never-real", "category": "789"})
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id})
        self.assertEqual(status, 409, response)
        self.assertEqual(client.published, [])

    def test_reconnect_to_another_credential_invalidates_quote_in_same_category(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        instance.disconnect()
        instance.connect({"golden_key": "other-fixture-price-key", "category": "456"})
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id})
        self.assertEqual(status, 409, response)
        self.assertEqual(client.published, [])

    def test_currency_change_invalidates_quote_before_publish(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        instance.connection["currency"] = "EUR"
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id})
        self.assertEqual(status, 409, response)
        self.assertEqual(client.published, [])

    def test_completed_quote_expires_after_five_minutes(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        self.assertEqual(app.QUOTE_TTL, 300)
        expired_time = time.monotonic() + 301
        with patch.object(app.time, "monotonic", return_value=expired_time):
            status, result = self.request(instance, "GET", "/api/quote?id=" + quote_id)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["expires_in"], 0)
            status, response = self.request(instance, "POST", "/api/start", {
                "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id})
        self.assertEqual(status, 409, response)
        self.assertEqual(client.published, [])

    def test_poll_records_preserve_net_actual_buyer_and_requested_buyer_amounts(self):
        instance, client = self.configured(PriceClient(quote_overrides={
            "seller_price": "0.99", "buyer_price_rub": "99.98", "exact": False,
            "note": "Closest achievable fixture price"}))
        quote_id, preview = self.quote(instance)
        self.assertFalse(preview["quotes"]["100.00"]["exact"])
        self.assertNotIn("identity", preview)
        self.assertNotIn("created_at", preview)
        for acceptance in [{}, {"accept_rounded_prices": False}, {"accept_rounded_prices": "true"},
                           {"accept_rounded_prices": 1}]:
            with self.subTest(acceptance=acceptance):
                status, response = self.request(instance, "POST", "/api/start", {
                    "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id, **acceptance})
                self.assertEqual(status, 409, response)
                self.assertEqual(client.published, [])
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [item()], "price_mode": "buyer_rub", "quote_id": quote_id, "accept_rounded_prices": True})
        self.assertEqual(status, 200, response)
        job = self.wait_batch(instance, response["job_id"])
        row = job["items"][0]
        self.assertEqual(row["seller_price"], "0.99")
        self.assertEqual(row["seller_currency"], "USD")
        self.assertEqual(row["buyer_price_rub"], "99.98")
        self.assertEqual(row["buyer_target_rub"], "100.00")
        self.assertEqual(row["price_mode"], "buyer_rub")
        self.assertEqual(client.published[0][0]["price"], "0.99")

    def test_omitted_mode_uses_trusted_buyer_quote_instead_of_entered_net_price(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        status, response = self.request(instance, "POST", "/api/start", {"items": [item()], "quote_id": quote_id})
        self.assertEqual(status, 200, response)
        job = self.wait_batch(instance, response["job_id"])
        self.assertEqual(job["items"][0]["state"], "published")
        self.assertEqual(job["items"][0]["price_mode"], "buyer_rub")
        self.assertEqual(client.published[0][0]["price"], "1.00")
        self.assertEqual(client.published[0][0]["buyer_target_rub"], "100.00")

    def test_explicit_seller_mode_cannot_bypass_a_valid_buyer_quote(self):
        instance, client = self.configured()
        quote_id, _ = self.quote(instance)
        status, response = self.request(instance, "POST", "/api/start", {
            "items": [item()], "price_mode": "seller", "quote_id": quote_id})
        self.assertEqual(status, 400, response)
        self.assertEqual(client.published, [])

    def test_quote_busy_guards_and_cancel_wait_for_active_calculation(self):
        entered, release = threading.Event(), threading.Event()
        instance, client = self.configured(PriceClient(entered=entered, release=release))
        status, response = self.request(instance, "POST", "/api/quote", {"prices": ["100", "200"], "price_mode": "buyer_rub"})
        self.assertEqual(status, 200, response)
        quote_id = response["quote_id"]
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(instance.busy())
            for path, body in [("/api/connect", {"golden_key": "new-fixture-key", "category": "123"}),
                               ("/api/start", {"items": [item()]}), ("/api/disconnect", {}),
                               ("/api/quote", {"prices": ["300"], "price_mode": "buyer_rub"})]:
                with self.subTest(path=path):
                    status, _ = self.request(instance, "POST", path, body)
                    self.assertEqual(status, 409)
            status, response = self.request(instance, "POST", "/api/cancel_quote", {"quote_id": quote_id})
            self.assertEqual(status, 200, response)
            self.assertTrue(instance.busy())
        finally:
            release.set()
        result = self.wait_quote(instance, quote_id)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(client.calculations, ["100.00"])
        self.assertEqual(client.published, [])
        self.assertFalse(instance.busy())

    def test_readonly_quote_failure_does_not_create_uncertain_listings(self):
        for error in [FunPayError("Price calculator unavailable", kind="network", uncertain=True), RuntimeError("unexpected fixture error")]:
            with self.subTest(error=repr(error)):
                instance, client = self.configured(PriceClient(error=error))
                status, response = self.request(instance, "POST", "/api/quote", {"prices": ["100"], "price_mode": "buyer_rub"})
                self.assertEqual(status, 200, response)
                result = self.wait_quote(instance, response["quote_id"])
                self.assertEqual(result["state"], "failed")
                self.assertTrue(result["error"])
                self.assertNotIn("uncertain", json.dumps(result).lower())
                self.assertEqual(instance.outcomes, {})
                self.assertIsNone(instance.job)
                self.assertEqual(client.published, [])
                self.assertFalse(instance.busy())

    def test_quote_endpoints_require_session_token(self):
        instance, client = self.configured()
        for method, path, body in [("POST", "/api/quote", {"prices": ["100"], "price_mode": "buyer_rub"}),
                                   ("GET", "/api/quote?id=any", None),
                                   ("POST", "/api/cancel_quote", {"quote_id": "any"})]:
            with self.subTest(path=path):
                status, _ = self.request(instance, method, path, body, token=False)
                self.assertEqual(status, 403)
        self.assertEqual(client.calculations, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
