"""Offline management endpoint/queue tests. No credentials or FunPay writes."""

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
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import app
from funpay import FunPayError
from test_price_jobs import PriceClient, item as listing_item


REVISION = "a" * 64


class FakeManager:
    def __init__(self, client, *, entered=None, release=None, errors=None):
        self.client = client
        self.entered = entered
        self.release = release
        self.errors = errors or {}
        self.reads = []
        self.writes = []

    def list_offers(self):
        self.reads.append(("list",))
        return {"items": [{"id": "123", "title_ru": "Предмет", "title_en": "Item", "price": "12.50", "active": True}],
                "category": {"id": "456", "name": "Fixture Items"}, "currency": "USD", "complete": True}

    def get_offer(self, offer_id):
        self.reads.append(("get", str(offer_id)))
        return {"id": str(offer_id), "revision": REVISION, "title_ru": "Предмет", "title_en": "Item",
                "description_ru": "Описание", "description_en": "Description", "seller_price": "12.50",
                "active": True, "amount": "1", "fields": [], "currency": "USD", "stock_editable": False}

    def cleanup_offer(self, offer_id):
        return {"id": str(offer_id), "state": "skipped", "message": "Fixture active listing kept"}

    def write(self, action, offer_id, changes, revision):
        self.writes.append((action, str(offer_id), copy.deepcopy(changes), revision))
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(2):
            raise AssertionError("test did not release active management operation")
        error = self.errors.get(str(offer_id))
        if error:
            raise error
        return {"id": str(offer_id), "url": f"https://funpay.com/lots/offer?id={offer_id}", "message": "Fixture save"}

    def save_offer(self, offer_id, changes, expected_revision):
        return self.write("update", offer_id, changes, expected_revision)

    def delete_offer(self, offer_id, expected_revision):
        return self.write("delete", offer_id, None, expected_revision)


class ManagementJobsTests(unittest.TestCase):
    def configured(self, client=None, **manager_options):
        client = client or PriceClient()
        manager = FakeManager(client, **manager_options)
        instance = app.Lister(client_factory=lambda: client, manager_factory=lambda _client: manager)
        instance.connect({"golden_key": "fixture-manager-key-never-real", "category": "456"})
        manager.reads.clear()
        return instance, client, manager

    def request(self, instance, method, path, body=None, *, token=True, headers=None):
        request_headers = {"Host": "127.0.0.1:8765"}
        if token:
            request_headers["X-Lister-Token"] = instance.token
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            request_headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        request_headers.update(headers or {})
        head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"

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

    def inspect(self, instance, offer_id="123"):
        status, snapshot = self.request(instance, "GET", "/api/offer?id=" + offer_id)
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["revision"], REVISION)
        return snapshot

    def start(self, instance, *, action="update", items=None, **extra):
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": action, "items": items or [{"id": "123", "revision": REVISION, "changes": {"active": False}}], **extra})
        self.assertEqual(status, 200, response)
        return response["job_id"]

    def wait_management(self, instance, job_id):
        deadline = time.monotonic() + 6
        while True:
            status, result = self.request(instance, "GET", "/api/manage_job?id=" + job_id)
            self.assertEqual(status, 200, result)
            if result["state"] != "running":
                return result
            if time.monotonic() > deadline:
                self.fail("offline management queue failed to finish")
            time.sleep(0.005)

    def completed_quote(self, instance):
        status, response = self.request(instance, "POST", "/api/quote", {"prices": ["100"], "price_mode": "buyer_rub"})
        self.assertEqual(status, 200, response)
        deadline = time.monotonic() + 2
        while True:
            status, result = self.request(instance, "GET", "/api/quote?id=" + response["quote_id"])
            self.assertEqual(status, 200, result)
            if result["state"] != "running":
                self.assertEqual(result["state"], "completed", result)
                return response["quote_id"]
            if time.monotonic() > deadline:
                self.fail("offline quote failed to finish")
            time.sleep(0.005)

    def test_management_reads_require_connection_and_expose_snapshot_revision(self):
        instance, _, manager = self.configured()
        status, result = self.request(instance, "GET", "/api/offers")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["items"][0]["id"], "123")
        self.inspect(instance)
        instance.disconnect()
        for path in ["/api/offers", "/api/offer?id=123"]:
            with self.subTest(path=path):
                status, _ = self.request(instance, "GET", path)
                self.assertEqual(status, 409)
        self.assertEqual(manager.writes, [])

    def test_invalid_ids_revisions_duplicates_and_action_fail_before_manager_call(self):
        invalid_items = [(400, []), (400, [None]), (400, [{"id": "0", "revision": REVISION}]),
                         (400, [{"id": "-1", "revision": REVISION}]), (400, [{"id": "not-an-id", "revision": REVISION}]),
                         (400, [{"id": str(2 ** 63), "revision": REVISION}]), (409, [{"id": "123"}]),
                         (409, [{"id": "123", "revision": "not-a-hash"}]),
                         (409, [{"id": "123", "revision": "g" * 64}]),
                         (400, [{"id": "123", "revision": REVISION}, {"id": "123", "revision": REVISION}]),
                         (400, [{"id": str(i + 1), "revision": REVISION} for i in range(101)])]
        for expected_status, rows in invalid_items:
            instance, _, manager = self.configured()
            status, response = self.request(instance, "POST", "/api/manage", {"action": "delete", "items": rows})
            with self.subTest(rows=rows[:2]):
                self.assertEqual(status, expected_status, response)
                self.assertEqual(manager.reads, [])
                self.assertEqual(manager.writes, [])
        instance, _, manager = self.configured()
        for action in ["remove", "create", "", None, []]:
            with self.subTest(action=action):
                status, response = self.request(instance, "POST", "/api/manage", {
                    "action": action, "items": [{"id": "123", "revision": REVISION}]})
                self.assertEqual(status, 400, response)
        self.assertEqual(manager.writes, [])

    def test_active_only_update_does_not_set_price_or_require_quote(self):
        for extra in [{}, {"price_mode": "buyer_rub"}]:
            with self.subTest(extra=extra):
                instance, client, manager = self.configured()
                self.inspect(instance)
                job = self.wait_management(instance, self.start(instance, **extra))
                self.assertEqual(job["items"][0]["state"], "updated")
                self.assertEqual(manager.writes, [("update", "123", {"active": False}, REVISION)])
                self.assertEqual(client.calculations, [])

    def test_valid_but_uninspected_or_changed_revision_is_rejected_without_write(self):
        instance, _, manager = self.configured()
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "update", "items": [{"id": "123", "revision": REVISION, "changes": {"active": False}}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.reads, [])
        self.assertEqual(manager.writes, [])

        self.inspect(instance)
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "delete", "items": [{"id": "123", "revision": "b" * 64}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.writes, [])

    def test_reconnect_invalidates_previously_inspected_revision(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        instance.disconnect()
        instance.connect({"golden_key": "other-fixture-manager-key", "category": "456"})
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "delete", "items": [{"id": "123", "revision": REVISION}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.writes, [])

    def test_invalid_changes_and_frontend_net_tampering_are_rejected_before_worker(self):
        for changes in [{}, {"active": "false"}, {"title_ru": None}, {"amount": -1}, {"amount": 1.5},
                        {"price": "NaN"}, {"fields": []}, {"fields": {"field": []}},
                        {"seller_price": "0.01"}, {"buyer_target_rub": "1"},
                        {"price": "100", "buyer_price_rub": "1"}, {"fields[payment_msg][ru]": "hidden text"}]:
            with self.subTest(changes=changes):
                instance, _, manager = self.configured()
                self.inspect(instance)
                status, response = self.request(instance, "POST", "/api/manage", {
                    "action": "update", "items": [{"id": "123", "revision": REVISION, "changes": changes}]})
                self.assertEqual(status, 400, response)
                self.assertEqual(manager.writes, [])
                self.assertFalse(instance.busy())
        instance, _, manager = self.configured()
        self.inspect(instance)
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "delete", "items": [{"id": "123", "revision": REVISION, "changes": {"active": False}}]})
        self.assertEqual(status, 400, response)
        self.assertEqual(manager.writes, [])

    def test_rejected_multirow_request_does_not_consume_valid_inspection(self):
        instance, _, manager = self.configured()
        self.inspect(instance, "123")
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "delete", "items": [{"id": "123", "revision": REVISION}, {"id": "124", "revision": REVISION}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.writes, [])
        job_id = self.start(instance, action="delete", items=[{"id": "123", "revision": REVISION}])
        self.assertEqual(self.wait_management(instance, job_id)["items"][0]["state"], "deleted")

    def test_zero_stock_is_rejected_before_management_worker(self):
        for amount in [0, "0", "000"]:
            with self.subTest(amount=amount):
                instance, _, manager = self.configured()
                self.inspect(instance)
                with patch.object(app.threading, "Thread") as worker:
                    status, response = self.request(instance, "POST", "/api/manage", {
                        "action": "update", "items": [{"id": "123", "revision": REVISION, "changes": {"amount": amount}}]})
                    self.assertEqual(status, 400, response)
                    worker.assert_not_called()
                self.assertEqual(manager.writes, [])
                self.assertFalse(instance.busy())

    def test_successful_delete_records_deleted_and_expected_revision(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        job_id = self.start(instance, action="delete", items=[{"id": "123", "revision": REVISION}])
        job = self.wait_management(instance, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["items"][0]["state"], "deleted")
        self.assertEqual(manager.writes, [("delete", "123", None, REVISION)])

    def test_update_uses_server_owned_buyer_quote_and_rejects_unquoted_price(self):
        instance, client, manager = self.configured()
        self.inspect(instance)
        quote_id = self.completed_quote(instance)
        status, response = self.request(instance, "POST", "/api/manage", {
            "action": "update", "price_mode": "buyer_rub", "quote_id": quote_id,
            "items": [{"id": "123", "revision": REVISION, "changes": {"price": "101"}}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.writes, [])
        job_id = self.start(instance, price_mode="buyer_rub", quote_id=quote_id,
                            items=[{"id": "123", "revision": REVISION, "changes": {"price": "100", "active": True}}])
        job = self.wait_management(instance, job_id)
        self.assertEqual(job["items"][0]["state"], "updated")
        changes = manager.writes[0][2]
        self.assertEqual(changes["price"], "1.00")
        self.assertEqual(changes["buyer_target_rub"], "100.00")
        self.assertEqual(changes["buyer_price_rub"], "100.00")
        self.assertEqual(client.calculations, ["100.00"])

    def test_every_management_price_change_requires_a_buyer_quote(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        for extra, expected_status in [({}, 409), ({"price_mode": "buyer_rub"}, 409), ({"price_mode": "seller"}, 400)]:
            with self.subTest(extra=extra):
                status, response = self.request(instance, "POST", "/api/manage", {
                    "action": "update", "items": [{"id": "123", "revision": REVISION, "changes": {"price": "100"}}], **extra})
                self.assertEqual(status, expected_status, response)
                self.assertEqual(manager.writes, [])
        quote_id = self.completed_quote(instance)
        job_id = self.start(instance, quote_id=quote_id, items=[{"id": "123", "revision": REVISION, "changes": {"price": "100"}}])
        self.wait_management(instance, job_id)
        self.assertEqual(manager.writes[0][2]["price"], "1.00")
        self.assertEqual(manager.writes[0][2]["buyer_target_rub"], "100.00")

    def test_explicit_seller_mode_is_rejected_even_with_quote_or_no_price_edit(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        quote_id = self.completed_quote(instance)
        for action, changes in [("update", {"price": "100"}), ("update", {"active": False}), ("delete", {})]:
            with self.subTest(action=action, changes=changes):
                status, response = self.request(instance, "POST", "/api/manage", {
                    "action": action, "price_mode": "seller", "quote_id": quote_id,
                    "items": [{"id": "123", "revision": REVISION, "changes": changes}]})
                self.assertEqual(status, 400, response)
                self.assertEqual(manager.writes, [])

    def test_expired_buyer_quote_blocks_management_price_update(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        quote_id = self.completed_quote(instance)
        expired_time = time.monotonic() + 301
        with patch.object(app.time, "monotonic", return_value=expired_time):
            status, response = self.request(instance, "POST", "/api/manage", {
                "action": "update", "price_mode": "buyer_rub", "quote_id": quote_id,
                "items": [{"id": "123", "revision": REVISION, "changes": {"price": "100"}}]})
        self.assertEqual(status, 409, response)
        self.assertEqual(manager.writes, [])

    def test_rounded_management_price_requires_explicit_acceptance(self):
        client = PriceClient(quote_overrides={"seller_price": "0.99", "buyer_price_rub": "99.98", "exact": False})
        instance, _, manager = self.configured(client=client)
        self.inspect(instance)
        quote_id = self.completed_quote(instance)
        request = {"action": "update", "price_mode": "buyer_rub", "quote_id": quote_id,
                   "items": [{"id": "123", "revision": REVISION, "changes": {"price": "100"}}]}
        for extra in [{}, {"accept_rounded_prices": False}, {"accept_rounded_prices": "true"}]:
            with self.subTest(extra=extra):
                status, response = self.request(instance, "POST", "/api/manage", {**request, **extra})
                self.assertEqual(status, 409, response)
                self.assertEqual(manager.writes, [])
        status, response = self.request(instance, "POST", "/api/manage", {**request, "accept_rounded_prices": True})
        self.assertEqual(status, 200, response)
        job = self.wait_management(instance, response["job_id"])
        self.assertEqual(manager.writes[0][2]["price"], "0.99")
        self.assertEqual(manager.writes[0][2]["buyer_target_rub"], "100.00")
        self.assertEqual(job["items"][0]["buyer_price_rub"], "99.98")

    def test_stop_completes_active_update_or_delete_then_cancels_remaining(self):
        for action, active_state, final_state in [("update", "updating", "updated"), ("delete", "deleting", "deleted")]:
            with self.subTest(action=action):
                entered, release = threading.Event(), threading.Event()
                instance, _, manager = self.configured(entered=entered, release=release)
                self.inspect(instance, "123")
                self.inspect(instance, "124")
                rows = [{"id": offer_id, "revision": REVISION, **({"changes": {"active": False}} if action == "update" else {})}
                        for offer_id in ["123", "124"]]
                job_id = self.start(instance, action=action, items=rows)
                try:
                    self.assertTrue(entered.wait(2))
                    status, current = self.request(instance, "GET", "/api/manage_job?id=" + job_id)
                    self.assertEqual(status, 200, current)
                    self.assertEqual(current["items"][0]["state"], active_state)
                    status, result = self.request(instance, "POST", "/api/manage_stop", {"job_id": job_id})
                    self.assertEqual(status, 200, result)
                    self.assertTrue(instance.busy())
                finally:
                    release.set()
                final = self.wait_management(instance, job_id)
                self.assertEqual(final["state"], "stopped")
                self.assertEqual([row["state"] for row in final["items"]], [final_state, "cancelled"])
                self.assertEqual(len(manager.writes), 1)
                status, response = self.request(instance, "POST", "/api/manage", {"action": action, "items": [rows[1]]})
                self.assertEqual(status, 409, response)
                self.assertEqual(len(manager.writes), 1)

    def test_uncertain_and_fatal_errors_stop_queue_without_automatic_retry(self):
        for error in [FunPayError("Save result unconfirmed", uncertain=True),
                      FunPayError("Login required", kind="auth"),
                      FunPayError("Rate limit", kind="rate_limit"), RuntimeError("Unexpected fixture failure")]:
            with self.subTest(error=repr(error)):
                instance, _, manager = self.configured(errors={"123": error})
                self.inspect(instance, "123")
                self.inspect(instance, "124")
                job_id = self.start(instance, items=[{"id": offer_id, "revision": REVISION, "changes": {"active": False}}
                                                     for offer_id in ["123", "124"]])
                job = self.wait_management(instance, job_id)
                expected = "uncertain" if isinstance(error, RuntimeError) or error.uncertain else "failed"
                self.assertEqual([row["state"] for row in job["items"]], [expected, "cancelled"])
                self.assertEqual(job["state"], "stopped")
                self.assertEqual(len(manager.writes), 1)
                status, response = self.request(instance, "POST", "/api/manage", {
                    "action": "update", "items": [{"id": "123", "revision": REVISION, "changes": {"active": False}}]})
                self.assertEqual(status, 409, response)
                self.assertEqual(len(manager.writes), 1)

    def test_known_validation_failure_continues_remaining_queue(self):
        instance, _, manager = self.configured(errors={"123": FunPayError("Unsupported field value", kind="validation")})
        self.inspect(instance, "123")
        self.inspect(instance, "124")
        job_id = self.start(instance, items=[{"id": offer_id, "revision": REVISION, "changes": {"active": False}}
                                             for offer_id in ["123", "124"]])
        job = self.wait_management(instance, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual([row["state"] for row in job["items"]], ["failed", "updated"])
        self.assertEqual(len(manager.writes), 2)

    def test_running_management_blocks_reads_other_writes_quotes_and_connection_changes(self):
        entered, release = threading.Event(), threading.Event()
        instance, client, manager = self.configured(entered=entered, release=release)
        self.inspect(instance)
        job_id = self.start(instance)
        try:
            self.assertTrue(entered.wait(2))
            reads_before = list(manager.reads)
            for method, path, body in [("GET", "/api/offers", None), ("GET", "/api/offer?id=123", None),
                                       ("POST", "/api/manage", {"action": "delete", "items": [{"id": "123", "revision": REVISION}]}),
                                       ("POST", "/api/quote", {"price_mode": "buyer_rub", "prices": ["100"]}),
                                       ("POST", "/api/start", {"items": [listing_item()]}),
                                       ("POST", "/api/connect", {"golden_key": "another-fixture-key", "category": "789"}),
                                       ("POST", "/api/disconnect", {})]:
                with self.subTest(path=path):
                    status, response = self.request(instance, method, path, body)
                    self.assertEqual(status, 409, response)
            self.assertEqual(manager.reads, reads_before)
            self.assertEqual(client.calculations, [])
        finally:
            release.set()
        self.wait_management(instance, job_id)

    def test_management_routes_require_local_host_origin_and_token(self):
        instance, _, manager = self.configured()
        routes = [("GET", "/api/offers", None), ("GET", "/api/offer?id=123", None),
                  ("GET", "/api/manage_job?id=missing", None),
                  ("POST", "/api/manage", {"action": "delete", "items": [{"id": "123", "revision": REVISION}]}),
                  ("POST", "/api/manage_stop", {"job_id": "missing"})]
        for method, path, body in routes:
            for options in [{"token": False}, {"headers": {"Host": "attacker.example"}},
                            {"headers": {"Origin": "https://attacker.example"}}]:
                with self.subTest(path=path, options=options):
                    status, response = self.request(instance, method, path, body, **options)
                    self.assertEqual(status, 403, response)
        self.assertEqual(manager.reads, [])
        self.assertEqual(manager.writes, [])

    def test_running_quote_or_listing_batch_blocks_management_reads_and_writes(self):
        for operation in ["quote", "batch"]:
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()

                class BlockingClient(PriceClient):
                    def publish(self, value, fields, active=True):
                        entered.set()
                        if not release.wait(2):
                            raise AssertionError("test did not release active listing batch")
                        return super().publish(value, fields, active)

                client = PriceClient(entered=entered, release=release) if operation == "quote" else BlockingClient()
                instance, _, manager = self.configured(client=client)
                self.inspect(instance)
                if operation == "quote":
                    status, started = self.request(instance, "POST", "/api/quote", {"price_mode": "buyer_rub", "prices": ["100"]})
                else:
                    quote_id = self.completed_quote(instance)
                    status, started = self.request(instance, "POST", "/api/start", {"items": [listing_item()], "quote_id": quote_id})
                self.assertEqual(status, 200, started)
                try:
                    self.assertTrue(entered.wait(2))
                    reads_before = list(manager.reads)
                    for method, path, body in [("GET", "/api/offers", None), ("GET", "/api/offer?id=123", None),
                                               ("POST", "/api/manage", {"action": "delete", "items": [{"id": "123", "revision": REVISION}]})]:
                        status, response = self.request(instance, method, path, body)
                        self.assertEqual(status, 409, response)
                    self.assertEqual(manager.reads, reads_before)
                    self.assertEqual(manager.writes, [])
                finally:
                    release.set()
                deadline = time.monotonic() + 2
                while instance.busy():
                    if time.monotonic() > deadline:
                        self.fail("offline quote/batch failed to finish")
                    time.sleep(0.005)

    def test_read_failure_releases_busy_guard_without_inspection_or_write(self):
        instance, _, manager = self.configured()

        def fail_read(_offer_id):
            raise FunPayError("Fixture authentication expired", kind="auth", uncertain=False)

        manager.get_offer = fail_read
        status, response = self.request(instance, "GET", "/api/offer?id=123")
        self.assertEqual(status, 400, response)
        self.assertEqual(response["kind"], "auth")
        self.assertFalse(response["uncertain"])
        self.assertFalse(instance.busy())
        self.assertEqual(instance.inspections, {})
        self.assertEqual(manager.writes, [])
        status, response = self.request(instance, "GET", "/api/offers")
        self.assertEqual(status, 200, response)

    def test_read_in_progress_blocks_connection_and_writes(self):
        instance, _, manager = self.configured()
        self.inspect(instance)
        entered, release = threading.Event(), threading.Event()
        original_list = manager.list_offers

        def blocking_list():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release active management read")
            return original_list()

        manager.list_offers = blocking_list
        responses = []
        reader = threading.Thread(target=lambda: responses.append(self.request(instance, "GET", "/api/offers")))
        reader.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(instance.busy())
            for method, path, body in [("GET", "/api/offer?id=123", None),
                                       ("POST", "/api/manage", {"action": "delete", "items": [{"id": "123", "revision": REVISION}]}),
                                       ("POST", "/api/connect", {"golden_key": "other-fixture-key", "category": "789"}),
                                       ("POST", "/api/disconnect", {})]:
                with self.subTest(path=path):
                    status, response = self.request(instance, method, path, body)
                    self.assertEqual(status, 409, response)
        finally:
            release.set()
            reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(responses[0][0], 200, responses)
        self.assertFalse(instance.busy())
        self.assertEqual(manager.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
