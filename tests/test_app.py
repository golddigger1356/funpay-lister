"""Local-only regression tests. No FunPay requests or account credentials."""

from __future__ import annotations

import copy
import http.client
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import app
from funpay import FunPayError
from capacity_fixtures import EmptyOffersManager


def item(item_id="one", price="12.50"):
    return {"id": item_id, "title_ru": " Товар ", "title_en": " Item ",
            "description_ru": " Описание ", "description_en": " Description ", "price": price}


def connection(fields=None):
    return {"account": {"id": "123", "name": "Test"}, "category": {"id": "456"},
            "currency": "RUB", "fields": fields or []}


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def connect(self, key, category):
        self.category_id = str(category)
        self.user_id = 777
        return connection()

    def publish(self, value, fields, active=True):
        self.calls.append((copy.deepcopy(value), copy.deepcopy(fields), active))
        result = self.responses.pop(0) if self.responses else {"url": "https://funpay.com/lots/offer?id=7"}
        if isinstance(result, BaseException):
            raise result
        return result

    def quote_buyer_rub(self, target):
        return {"target_rub": target, "seller_price": target, "buyer_price_rub": target,
                "seller_currency": "RUB", "exact": True, "note": "Fixture quote"}


def configured(client=None):
    instance = app.Lister()
    instance.client = client or FakeClient()
    instance.connection = connection()
    instance.identity = "account:456"
    instance.manager = EmptyOffersManager(instance.client, category_id="456")
    return instance


def quote_for(instance, prices=("12.50",)):
    response = instance.quote({"price_mode": "buyer_rub", "prices": list(prices)})
    deadline = time.monotonic() + 2
    while instance.get_quote(response["quote_id"])["state"] == "running":
        if time.monotonic() > deadline:
            raise AssertionError("fixture quote failed to finish")
        time.sleep(0.005)
    if instance.get_quote(response["quote_id"])["state"] != "completed":
        raise AssertionError("fixture quote failed")
    return response["quote_id"]


def run_direct(instance, values, fields=None, active=True):
    values = app.clean_items(values)
    instance.stop_event = threading.Event()
    instance.job = {"job_id": "test-job", "state": "running", "account": connection()["account"],
                    "total": len(values), "completed": 0,
                    "items": [{"id": value["id"], "state": "queued", "message": "Waiting", "url": None}
                              for value in values]}
    instance._run(values, fields or {}, 0, active)
    return instance.get_job("test-job")


class InputTests(unittest.TestCase):
    def test_prices_and_text_are_normalized(self):
        for raw, expected in [("12", "12.00"), (" 12,5 ", "12.50"), (12.5, "12.50"),
                              ("0.01", "0.01"), ("1000000000", "1000000000.00"),
                              ("1.200", "1.20")]:
            with self.subTest(raw=raw):
                value = app.clean_items([item(price=raw)])[0]
                self.assertEqual(value["price"], expected)
                self.assertEqual(value["title_ru"], "Товар")
                self.assertEqual(value["description_en"], "Description")

    def test_invalid_prices_are_rejected(self):
        for raw in ["", "0", "-1", "NaN", "sNaN", "Infinity", "-Infinity", "1.001",
                    "1000000000.01", "1e1000", "wat", None, True]:
            with self.subTest(raw=raw), self.assertRaises(app.RequestError):
                app.clean_items([item(price=raw)])

    def test_duplicate_item_ids_are_rejected(self):
        with self.assertRaises(app.RequestError):
            app.clean_items([item(), item()])

    def test_item_limits_and_required_titles(self):
        for raw in [[], "item", [item()] * (app.MAX_ITEMS + 1), [None],
                    [dict(item(), title_en="  ")], [dict(item(), title_ru="bad\x00title")]]:
            with self.subTest(raw=type(raw).__name__), self.assertRaises(app.RequestError):
                app.clean_items(raw)


class BatchTests(unittest.TestCase):
    def test_connect_failure_releases_busy_flag(self):
        class FailingClient(FakeClient):
            def connect(self, key, category):
                raise FunPayError("Login required", kind="auth")

        instance = app.Lister(client_factory=FailingClient)
        with self.assertRaises(FunPayError):
            instance.connect({"golden_key": "test-key", "category": "456"})
        self.assertFalse(instance.busy())
        self.assertIsNone(instance.client)

    def test_connect_records_category_and_keeps_credential_out_of_response(self):
        instance = app.Lister(client_factory=FakeClient, manager_factory=EmptyOffersManager)
        result = instance.connect({"golden_key": "test-key", "category": "456"})
        self.assertEqual({key: result[key] for key in connection()}, connection())
        self.assertEqual(result["cleanup"]["deleted"], 0)
        self.assertNotIn("test-key", json.dumps(result))
        self.assertTrue(instance.identity.endswith(":456"))
        self.assertNotIn("test-key", instance.identity)

    def test_busy_operations_cannot_change_active_batch(self):
        instance = configured()
        instance.job = {"state": "running"}
        for operation in [lambda: instance.connect({"golden_key": "test-key", "category": "456"}),
                          instance.disconnect, lambda: instance.start({"items": [item()]})]:
            with self.assertRaises(app.RequestError) as error:
                operation()
            self.assertEqual(error.exception.status, 409)

    def test_successful_queue_preserves_order_fields_visibility_and_outcomes(self):
        client = FakeClient()
        instance = configured(client)
        result = run_direct(instance, [item("one"), item("two", "2.25"), item("three")],
                            {"fields[region]": "EU"}, active=False)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["completed"], 3)
        self.assertEqual([r["state"] for r in result["items"]], ["published"] * 3)
        self.assertEqual([c[0]["id"] for c in client.calls], ["one", "two", "three"])
        self.assertTrue(all(c[1] == {"fields[region]": "EU"} and c[2] is False for c in client.calls))
        self.assertEqual(instance.outcomes[(instance.identity, "two")], "published")

    def test_published_and_uncertain_items_cannot_be_retried(self):
        for state in ["published", "uncertain"]:
            instance = configured()
            instance.outcomes[(instance.identity, "one")] = state
            with self.subTest(state=state), self.assertRaises(app.RequestError) as error:
                instance.start({"items": [item()]})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(instance.client.calls, [])

    def test_failed_validation_can_be_retried(self):
        instance = configured(FakeClient([FunPayError("Invalid category value")]))
        first = run_direct(instance, [item()])
        self.assertEqual(first["items"][0]["state"], "failed")
        result = instance.start({"items": [item()], "quote_id": quote_for(instance)})
        self.wait_job(instance, result["job_id"])
        self.assertEqual(instance.get_job(result["job_id"])["items"][0]["state"], "published")
        self.assertEqual(len(instance.client.calls), 2)

    def test_nonfatal_validation_failure_continues_queue(self):
        instance = configured(FakeClient([FunPayError("Invalid price"), {"message": "Saved"}]))
        job = run_direct(instance, [item("one"), item("two")])
        self.assertEqual([r["state"] for r in job["items"]], ["failed", "published"])
        self.assertEqual(job["state"], "completed")

    def test_fatal_errors_cancel_remaining_items(self):
        errors = [FunPayError("Login required", kind="auth"),
                  FunPayError("Slow down", kind="rate_limit"),
                  FunPayError("Connection unavailable", kind="network"),
                  FunPayError("Schema changed", kind="schema"),
                  FunPayError("Account blocked", kind="blocked"),
                  FunPayError("No acknowledgement", uncertain=True), RuntimeError("Unexpected")]
        for error in errors:
            instance = configured(FakeClient([error]))
            job = run_direct(instance, [item("one"), item("two")])
            with self.subTest(error=repr(error)):
                expected = "uncertain" if isinstance(error, RuntimeError) or error.uncertain else "failed"
                self.assertEqual([r["state"] for r in job["items"]], [expected, "cancelled"])
                self.assertEqual(job["completed"], 1)
                self.assertEqual(job["state"], "stopped")
                self.assertEqual(len(instance.client.calls), 1)

    def test_stop_finishes_active_request_then_cancels_queue(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingClient(FakeClient):
            def publish(self, value, fields, active=True):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release active request")
                return super().publish(value, fields, active)

        instance = configured(BlockingClient())
        response = instance.start({"items": [item("one"), item("two")], "quote_id": quote_for(instance)})
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(instance.get_job(response["job_id"])["items"][0]["state"], "publishing")
            instance.stop(response["job_id"])
            self.assertEqual(instance.get_job(response["job_id"])["state"], "running")
        finally:
            release.set()
        self.wait_job(instance, response["job_id"])
        job = instance.get_job(response["job_id"])
        self.assertEqual([r["state"] for r in job["items"]], ["published", "cancelled"])
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(len(instance.client.calls), 1)

    def test_unknown_category_fields_are_rejected(self):
        instance = configured()
        with self.assertRaises(app.RequestError):
            instance.start({"items": [item()], "fields": {"csrf_token": "override"}})
        self.assertEqual(instance.client.calls, [])

    def test_required_and_enumerated_fields_are_validated(self):
        instance = configured()
        instance.connection = connection([{"name": "fields[region]", "label": "Region", "required": True,
                                            "value": "", "options": [{"value": "EU"}, {"value": "US"}]}])
        for fields in [{}, {"fields[region]": ""}, {"fields[region]": "other"}]:
            with self.subTest(fields=fields), self.assertRaises(app.RequestError):
                instance.start({"items": [item()], "fields": fields})

    def test_invalid_delay_and_visibility_are_rejected(self):
        instance = configured()
        for delay in [0, -1, 2.99, 61, "NaN", "Infinity", None, "bad"]:
            with self.subTest(delay=delay), self.assertRaises(app.RequestError):
                instance.start({"items": [item()], "delay": delay})
        with self.assertRaises(app.RequestError):
            instance.start({"items": [item()], "active": "true"})

    def test_job_snapshots_are_copies(self):
        instance = configured()
        run_direct(instance, [item()])
        snapshot = instance.get_job("test-job")
        snapshot["items"][0]["state"] = "queued"
        self.assertEqual(instance.get_job("test-job")["items"][0]["state"], "published")

    @staticmethod
    def wait_job(instance, job_id):
        deadline = time.monotonic() + 2
        while instance.get_job(job_id)["state"] == "running":
            if time.monotonic() > deadline:
                raise AssertionError("batch failed to finish in two seconds")
            time.sleep(0.005)


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(server_port=8765, lister=configured())

    def request(self, method, path, body=None, headers=None, token=True):
        request_headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if token:
            request_headers["X-Lister-Token"] = self.server.lister.token
        if body is not None:
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        request_headers.update(headers or {})
        request_head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"

        class MemorySocket:
            def __init__(self, data):
                self.data = data
                self.output = bytearray()

            def makefile(self, mode, *args):
                return io.BytesIO(self.data)

            def sendall(self, data):
                self.output.extend(data)

        request = MemorySocket(request_head.encode() + (body or b""))
        app.Handler(request, ("127.0.0.1", 1234), self.server)
        response = http.client.HTTPResponse(MemorySocket(bytes(request.output)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def test_local_config_and_static_assets_are_available_without_token(self):
        status, headers, payload = self.request("GET", "/api/config", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["token"], self.server.lister.token)
        for path in ["/", "/index.html", "/styles.css", "/app.js"]:
            with self.subTest(path=path):
                status, headers, payload = self.request("GET", path, token=False)
                self.assertEqual(status, 200)
                self.assertTrue(payload)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_host_guard_applies_to_config_static_and_post(self):
        for method, path, body in [("GET", "/api/config", None), ("GET", "/", None),
                                   ("POST", "/api/start", {"items": [item()]})]:
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, body, {"Host": "attacker.example"})
                self.assertEqual(status, 403)

    def test_foreign_origins_are_rejected_even_with_valid_token(self):
        for method, path, body in [("GET", "/api/config", None),
                                   ("POST", "/api/start", {"items": [item()]})]:
            with self.subTest(method=method):
                status, _, _ = self.request(method, path, body, {"Origin": "https://attacker.example"})
                self.assertEqual(status, 403)

    def test_local_origin_is_accepted(self):
        status, _, _ = self.request("GET", "/api/config", headers={"Origin": f"http://localhost:{self.server.server_port}"})
        self.assertEqual(status, 200)

    def test_post_and_job_reads_require_token(self):
        for token in [False, True]:
            extra = {"X-Lister-Token": "wrong"} if token else {}
            for method, path, body in [("POST", "/api/start", {"items": [item()]}),
                                       ("GET", "/api/job?id=missing", None)]:
                with self.subTest(method=method, token=token):
                    status, _, _ = self.request(method, path, body, extra, token=token)
                    self.assertEqual(status, 403)

    def test_authenticated_unknown_field_request_is_rejected(self):
        status, _, payload = self.request("POST", "/api/start", {"items": [item()], "fields": {"offer_id": "99"}})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(payload))

    def test_unknown_routes_and_path_traversal_are_not_served(self):
        for path in ["/missing", "/../app.py", "/%2e%2e/app.py", "/app.py"]:
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_post_rejects_wrong_content_type_or_non_object_json(self):
        status, _, _ = self.request("POST", "/api/start", {"items": [item()]}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/start", [])
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
