"""Listing cap regressions using fresh, complete offline offer snapshots."""

import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from funpay import FunPayError
import test_management_jobs
import test_price_jobs


class CapacityManager:
    def __init__(self, client, count=0):
        self.client = client
        self.items = [{"id": str(i + 1), "active": i % 2 == 0, "amount": 1} for i in range(count)]
        self.reads = 0
        self.script = []
        self.entered = None
        self.release = None

    def list_offers(self):
        self.reads += 1
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(2):
            raise AssertionError("test did not release capacity read")
        if self.script:
            result = self.script.pop(0)
            if isinstance(result, BaseException):
                raise result
            if isinstance(result, dict):
                return copy.deepcopy(result)
        return {"items": copy.deepcopy(self.items), "complete": True,
                "category": {"id": self.client.category_id}, "currency": "USD"}


class CapacityClient(test_price_jobs.PriceClient):
    def __init__(self):
        super().__init__()
        self.manager = None
        self.after_publish = None
        self.publish_error = None

    def publish(self, value, fields, active=True):
        result = super().publish(value, fields, active)
        self.manager.items.append({"id": str(10000 + len(self.published)), "active": active, "amount": 1})
        if self.after_publish:
            self.after_publish()
        if self.publish_error:
            raise self.publish_error
        return result


class CapacityLimitTests(unittest.TestCase):
    def request(self, *args, **kwargs):
        return test_management_jobs.ManagementJobsTests.request(self, *args, **kwargs)

    def configured(self, count=0):
        client = CapacityClient()
        manager = CapacityManager(client, 0)
        client.manager = manager
        instance = app.Lister(client_factory=lambda: client, manager_factory=lambda _client: manager)
        instance.connect({"golden_key": "fixture-capacity-key-never-real", "category": "456"})
        manager.items = [{"id": str(i + 1), "active": i % 2 == 0, "amount": 1} for i in range(count)]
        manager.reads = 0
        return instance, client, manager

    def quote(self, instance):
        response = instance.quote({"price_mode": "buyer_rub", "prices": ["100"]})
        deadline = time.monotonic() + 2
        while instance.get_quote(response["quote_id"])["state"] == "running":
            if time.monotonic() > deadline:
                self.fail("fixture price check did not finish")
            time.sleep(0.005)
        self.assertEqual(instance.get_quote(response["quote_id"])["state"], "completed")
        return response["quote_id"]

    def start(self, instance, quote_id, count=1, first_id=1):
        return self.request(instance, "POST", "/api/start", {
            "items": [test_price_jobs.item(item_id=f"new-{i}") for i in range(first_id, first_id + count)],
            "quote_id": quote_id})

    def wait_batch(self, instance, job_id):
        deadline = time.monotonic() + 6
        while instance.get_job(job_id)["state"] == "running":
            if time.monotonic() > deadline:
                self.fail("fixture batch did not finish")
            time.sleep(0.005)
        return instance.get_job(job_id)

    def test_capacity_counts_active_and_paused_listings(self):
        instance, _, manager = self.configured(39)
        status, result = self.request(instance, "GET", "/api/capacity")
        self.assertEqual(status, 200, result)
        self.assertEqual((result["limit"], result["used"], result["remaining"]), (39, 39, 0))
        self.assertEqual(result["category"]["id"], "456")
        self.assertEqual(result["account"], "Fixture Seller")
        self.assertEqual(sum(item["active"] for item in manager.items), 20)

    def test_full_category_rejects_before_job_or_write(self):
        instance, client, manager = self.configured(39)
        quote_id = self.quote(instance)
        with patch.object(app.threading, "Thread") as worker:
            status, result = self.start(instance, quote_id)
            self.assertEqual(status, 409, result)
            worker.assert_not_called()
        self.assertIsNone(instance.job)
        self.assertEqual(client.published, [])
        self.assertEqual(manager.reads, 1)

    def test_38_listings_allow_one_but_reject_two_as_whole_batch(self):
        instance, client, manager = self.configured(38)
        quote_id = self.quote(instance)
        with patch.object(app.threading, "Thread") as worker:
            status, result = self.start(instance, quote_id, count=2)
            self.assertEqual(status, 409, result)
            worker.assert_not_called()
        self.assertIsNone(instance.job)
        self.assertEqual(client.published, [])
        status, started = self.start(instance, quote_id)
        self.assertEqual(status, 200, started)
        job = self.wait_batch(instance, started["job_id"])
        self.assertEqual(job["items"][0]["state"], "published")
        self.assertEqual(len(client.published), 1)
        self.assertEqual(len(manager.items), 39)
        self.assertGreaterEqual(manager.reads, 3)

    def test_empty_category_admits_exactly_39_and_rejects_40_batch_items(self):
        instance, client, manager = self.configured()
        quote_id = self.quote(instance)
        self.assertEqual(app.MAX_ITEMS, 39)
        self.assertEqual(app.LISTING_LIMIT, 39)
        status, result = self.start(instance, quote_id, count=40)
        self.assertEqual(status, 400, result)
        self.assertEqual(manager.reads, 0)
        with patch.object(app.threading, "Thread") as worker:
            status, started = self.start(instance, quote_id, count=39)
            self.assertEqual(status, 200, started)
            worker.assert_called_once()
        self.assertEqual(instance.get_job(started["job_id"])["total"], 39)
        self.assertEqual(client.published, [])

    def test_external_fill_after_admission_blocks_first_publish(self):
        instance, client, manager = self.configured(39)
        manager.script = [{"items": copy.deepcopy(manager.items[:38]), "complete": True, "category": {"id": "456"}}]
        quote_id = self.quote(instance)
        status, started = self.start(instance, quote_id)
        self.assertEqual(status, 200, started)
        job = self.wait_batch(instance, started["job_id"])
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(job["items"][0]["state"], "failed")
        self.assertEqual(client.published, [])
        self.assertEqual(manager.reads, 2)

    def test_external_fill_between_publishes_stops_remaining_queue(self):
        instance, client, manager = self.configured(36)

        def external_fill():
            while len(manager.items) < 39:
                manager.items.append({"id": str(20000 + len(manager.items)), "active": False, "amount": 1})

        client.after_publish = external_fill
        quote_id = self.quote(instance)
        status, started = self.start(instance, quote_id, count=3)
        self.assertEqual(status, 200, started)
        job = self.wait_batch(instance, started["job_id"])
        self.assertEqual([row["state"] for row in job["items"]], ["published", "failed", "cancelled"])
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(len(client.published), 1)
        self.assertEqual(manager.reads, 3)

    def test_removed_listing_frees_capacity_only_after_fresh_count(self):
        instance, client, manager = self.configured(39)
        status, full = self.request(instance, "GET", "/api/capacity")
        self.assertEqual(status, 200, full)
        self.assertEqual(full["remaining"], 0)
        manager.items.pop()
        status, available = self.request(instance, "GET", "/api/capacity")
        self.assertEqual(status, 200, available)
        self.assertEqual((available["used"], available["remaining"]), (38, 1))
        status, started = self.start(instance, self.quote(instance))
        self.assertEqual(status, 200, started)
        self.assertEqual(self.wait_batch(instance, started["job_id"])["items"][0]["state"], "published")
        self.assertEqual(len(client.published), 1)

    def test_uncertain_save_does_not_leave_stale_capacity_allowance(self):
        instance, client, manager = self.configured(38)
        client.publish_error = FunPayError("Save result unconfirmed", uncertain=True)
        quote_id = self.quote(instance)
        status, started = self.start(instance, quote_id)
        self.assertEqual(status, 200, started)
        self.assertEqual(self.wait_batch(instance, started["job_id"])["items"][0]["state"], "uncertain")
        self.assertEqual(len(manager.items), 39)
        status, result = self.start(instance, quote_id, first_id=2)
        self.assertEqual(status, 409, result)
        self.assertEqual(len(client.published), 1)

    def test_partial_missing_wrong_category_or_malformed_snapshots_fail_closed(self):
        snapshots = [{"items": [], "category": {"id": "456"}},
                     {"items": [], "complete": False, "category": {"id": "456"}},
                     {"items": [], "complete": True, "category": {"id": "789"}},
                     {"items": "unreadable", "complete": True, "category": {"id": "456"}},
                     {"items": [{"id": "1"}, {"id": "1"}], "complete": True, "category": {"id": "456"}}]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                instance, client, manager = self.configured()
                quote_id = self.quote(instance)
                manager.script = [snapshot]
                with patch.object(app.threading, "Thread") as worker:
                    status, result = self.start(instance, quote_id)
                    self.assertIn(status, (400, 409), result)
                    worker.assert_not_called()
                self.assertIsNone(instance.job)
                self.assertEqual(client.published, [])
                self.assertFalse(instance.busy())

    def test_worker_capacity_read_failure_never_sends_listing(self):
        for failure in [FunPayError("Could not read current offers", kind="network", uncertain=False),
                        RuntimeError("Fixture page could not be parsed"),
                        {"items": [], "complete": False, "category": {"id": "456"}}]:
            with self.subTest(failure=repr(failure)):
                instance, client, manager = self.configured()
                manager.script = [{"items": [], "complete": True, "category": {"id": "456"}}, failure]
                status, started = self.start(instance, self.quote(instance), count=2)
                self.assertEqual(status, 200, started)
                job = self.wait_batch(instance, started["job_id"])
                self.assertEqual([row["state"] for row in job["items"]], ["failed", "cancelled"])
                self.assertEqual(job["state"], "stopped")
                self.assertEqual(client.published, [])

    def test_preflight_network_failure_creates_no_job_and_releases_busy_guard(self):
        instance, client, manager = self.configured()
        quote_id = self.quote(instance)
        manager.script = [FunPayError("Could not read current offers", kind="network", uncertain=False)]
        with patch.object(app.threading, "Thread") as worker:
            status, result = self.start(instance, quote_id)
            self.assertEqual(status, 400, result)
            worker.assert_not_called()
        self.assertIsNone(instance.job)
        self.assertEqual(client.published, [])
        self.assertFalse(instance.busy())

    def test_capacity_route_requires_session_token_local_host_and_origin(self):
        instance, _, manager = self.configured()
        for options in [{"token": False}, {"headers": {"Host": "attacker.example"}},
                        {"headers": {"Origin": "https://attacker.example"}}]:
            with self.subTest(options=options):
                status, result = self.request(instance, "GET", "/api/capacity", **options)
                self.assertEqual(status, 403, result)
        self.assertEqual(manager.reads, 0)

    def test_capacity_preflight_reserves_busy_without_holding_operation_lock(self):
        instance, _, manager = self.configured()
        quote_id = self.quote(instance)
        entered, release = threading.Event(), threading.Event()
        manager.entered, manager.release = entered, release
        responses = []
        starter = threading.Thread(target=lambda: responses.append(self.start(instance, quote_id)))
        starter.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(instance.busy())
            for method, path, body in [("GET", "/api/capacity", None),
                                       ("POST", "/api/quote", {"price_mode": "buyer_rub", "prices": ["100"]}),
                                       ("POST", "/api/connect", {"golden_key": "other-fixture-key", "category": "789"})]:
                with self.subTest(path=path):
                    status, result = self.request(instance, method, path, body)
                    self.assertEqual(status, 409, result)
        finally:
            release.set()
            starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(responses[0][0], 200, responses)
        self.wait_batch(instance, responses[0][1]["job_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
