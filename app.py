"""Local FunPay listing tool. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

from funpay import FunPayClient, FunPayError, field_enabled
from management import OfferManager
from limits import LISTING_LIMIT

ROOT = Path(__file__).resolve().parent
MAX_ITEMS = LISTING_LIMIT
VERSION = "1.3.1"
CLEANUP_DELAY = 3
QUOTE_TTL = 300


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean_price(value, label="Price"):
    try:
        price = Decimal(str(value).strip().replace(",", "."))
        if not price.is_finite() or not Decimal("0") < price <= Decimal("1000000000"):
            raise InvalidOperation
        if price != price.quantize(Decimal("0.01")):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise RequestError(f"{label} needs a positive price with up to 2 decimal places.")
    return format(price, ".2f")


def clean_items(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS:
        raise RequestError(f"Use between 1 and {MAX_ITEMS} items per batch.")
    result, seen = [], set()
    for index, value in enumerate(raw, 1):
        if not isinstance(value, dict):
            raise RequestError(f"Item {index} is invalid.")
        item_id = value.get("id")
        if not isinstance(item_id, str) or not 1 <= len(item_id) <= 100 or item_id in seen:
            raise RequestError("Every item needs its own unique ID. Rebuild the item table.")
        seen.add(item_id)
        item = {"id": item_id}
        for field, limit in (("title_ru", 500), ("title_en", 500),
                             ("description_ru", 20000), ("description_en", 20000)):
            text = value.get(field, "")
            if not isinstance(text, str) or len(text) > limit or "\x00" in text:
                raise RequestError(f"Item {index}: {field} is invalid or too long.")
            item[field] = text.strip()
        if not item["title_ru"] or not item["title_en"]:
            raise RequestError(f"Item {index} needs a Russian and an English title.")
        item["price"] = clean_price(value.get("price", ""), f"Item {index}")
        result.append(item)
    return result


class Lister:
    def __init__(self, client_factory=FunPayClient, manager_factory=OfferManager):
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.client_factory = client_factory
        self.manager_factory = manager_factory
        self.manager = None
        self.managing = False
        self.manage_job = None
        self.manage_stop_event = threading.Event()
        self.inspections = {}
        self.client = None
        self.connection = None
        self.identity = None
        self.connecting = False
        self.connection_job = None
        self.connection_stop_event = threading.Event()
        self.job = None
        self.stop_event = threading.Event()
        self.price_job = None
        self.quote_stop_event = threading.Event()
        # Memory only, scoped by account credential + category + item ID.
        self.outcomes = {}

    def busy(self):
        return (self.connecting or self.managing or bool(self.job and self.job["state"] == "running")
                or bool(self.price_job and self.price_job["state"] == "running")
                or bool(self.manage_job and self.manage_job["state"] == "running"))

    def _reserve_connection(self, body):
        key = body.get("golden_key", "")
        category = body.get("category", "")
        if not isinstance(key, str) or not key.strip() or len(key) > 4096:
            raise RequestError("Enter your Golden Key.")
        if not isinstance(category, (str, int)):
            raise RequestError("Enter a category ID or a FunPay /lots/ URL.")
        with self.lock:
            if self.busy():
                raise RequestError("Wait for the current operation to finish.", 409)
            self.connecting = True
            self.client = self.connection = self.identity = None
            self.manager = None
            self.inspections = {}
            self.price_job = None
            self.connection_stop_event = threading.Event()
            self.connection_job = {"job_id": secrets.token_urlsafe(16), "state": "running", "phase": "authenticating",
                                   "completed": 0, "total": 0, "deleted": 0, "skipped": 0,
                                   "items": [], "result": None, "error": None}
            return self.connection_job["job_id"]

    def begin_connect(self, body):
        job_id = self._reserve_connection(body)
        threading.Thread(target=self._connection_worker, args=(copy.deepcopy(body),), daemon=True).start()
        return {"connection_job_id": job_id}

    def connect(self, body):
        self._reserve_connection(body)
        return self._connection_worker(body, propagate=True)

    def _connection_worker(self, body, propagate=False):
        try:
            client = self.client_factory()
            key, category = body["golden_key"].strip(), str(body["category"]).strip()
            staged = callable(getattr(client, "authenticate", None)) and callable(getattr(client, "load_category", None))
            if staged:
                auth = client.authenticate(key, category)
                manager = self.manager_factory(client)
                metadata = manager.connection_metadata()
                client.currency = metadata["currency"]
                info = {"account": auth["account"], "user_id": auth.get("user_id"), "category": metadata["category"],
                        "currency": metadata["currency"], "fields": [], "constraints": {}, "default_price": "",
                        "creation_available": False, "management_only": True}
            else:
                # Alternate local clients retain the original connection protocol.
                info = client.connect(key, category)
                manager = self.manager_factory(client)
            identity = hashlib.sha256(key.encode()).hexdigest() + ":" + str(info["category"]["id"])
            with self.lock:
                self.client, self.connection, self.identity = client, info, identity
                self.manager = manager
                self.connection_job["phase"] = "checking"
            snapshot = self._checked_offers(manager)
            with self.lock:
                self.connection_job["total"] = len(snapshot["items"])
                self.connection_job["items"] = [{"id": item["id"], "title": item.get("title_ru") or item.get("title_en") or "Listing #" + item["id"],
                                                 "state": "queued", "message": "Waiting for a fresh status check"} for item in snapshot["items"]]
                self.connection_job["phase"] = "cleaning"
            stopped = False
            for index, item in enumerate(snapshot["items"]):
                if self.connection_stop_event.is_set():
                    stopped = True
                    break
                with self.lock:
                    row = self.connection_job["items"][index]
                    row.update(state="checking", message="Checking current status and quantity…")
                try:
                    if item.get("active") is True and type(item.get("amount")) is int and item["amount"] > 0:
                        # A complete, freshly read seller page already shows stock
                        # and an active offer. Skipping only retains this listing.
                        result = {"state": "skipped", "message": "Kept active listing with stock, verified on the category page."}
                    else:
                        result = manager.cleanup_offer(item["id"])
                    state = result.get("state")
                    if state not in ("deleted", "skipped"):
                        raise FunPayError("Cleanup returned an unconfirmed result. Check your listings before reconnecting.", uncertain=True)
                    detail = result.get("message", "Deleted" if state == "deleted" else "Kept")
                except FunPayError as error:
                    state, detail = ("uncertain" if error.uncertain else "failed"), str(error)
                    stopped = True
                except Exception:
                    state, detail, stopped = "uncertain", "Cleanup could not be confirmed. Check My listings before reconnecting.", True
                with self.lock:
                    row.update(state=state, message=detail)
                    self.connection_job["completed"] += 1
                    if state in ("deleted", "skipped"):
                        self.connection_job[state] += 1
                    else:
                        self.connection_job["error"] = detail
                if stopped:
                    break
                if state == "deleted" and index < len(snapshot["items"]) - 1 and self.connection_stop_event.wait(CLEANUP_DELAY):
                    stopped = True
                    break
            stopped = stopped or self.connection_stop_event.is_set()
            with self.lock:
                for row in self.connection_job["items"]:
                    if row["state"] == "queued":
                        row.update(state="cancelled", message="Not checked or deleted")
            if stopped:
                info = {**info, "creation_available": False, "management_only": True,
                        "message": "Connection cleanup stopped. Check the cleanup results and My listings before reconnecting. No further listings will be deleted by this connection."}
            else:
                with self.lock:
                    self.connection_job["phase"] = "loading"
                after = self._checked_offers(manager)
                if staged:
                    try:
                        info = client.load_category(listing_count=len(after["items"]))
                    except FunPayError as error:
                        info = {**info, "creation_available": False, "management_only": True,
                                "message": "Your account is connected for managing existing listings. " + str(error)}
                elif len(after["items"]) >= LISTING_LIMIT:
                    info = {**info, "creation_available": False, "management_only": True,
                            "message": f"This category has {len(after['items'])} listings. The tool's {LISTING_LIMIT}-listing cap blocks new offers."}
            with self.lock:
                info["cleanup"] = {key: copy.deepcopy(self.connection_job[key]) for key in ("completed", "total", "deleted", "skipped", "items", "error")}
                info["cleanup"]["stopped"] = stopped
                self.connection = info
                self.connection_job.update(state="stopped" if stopped else "completed", result=copy.deepcopy(info))
            return copy.deepcopy(info)
        except Exception as error:
            detail = str(error) if isinstance(error, (RequestError, FunPayError)) else "The connection could not be completed. Check the cleanup results before reconnecting."
            if callable(getattr(locals().get("client"), "_safe", None)):
                detail = client._safe(detail)
            with self.lock:
                self.connection_job.update(state="failed", error=detail)
                self.client = self.connection = self.identity = self.manager = None
            if propagate:
                raise
        finally:
            with self.lock:
                self.connecting = False

    def get_connection_job(self, job_id):
        with self.lock:
            if not self.connection_job or self.connection_job["job_id"] != job_id:
                raise RequestError("Connection cleanup not found. Check FunPay before reconnecting.", 404)
            return copy.deepcopy(self.connection_job)

    def stop_connection(self, job_id):
        with self.lock:
            if not self.connection_job or self.connection_job["job_id"] != job_id:
                raise RequestError("Connection cleanup not found.", 404)
            self.connection_stop_event.set()
        return {"ok": True, "message": "Stopping after the current listing check or deletion."}

    def disconnect(self):
        with self.lock:
            if self.busy():
                raise RequestError("Stop the batch and wait for the current listing first.", 409)
            self.client = self.connection = self.identity = None
            self.manager = None
            self.inspections = {}
            self.price_job = None
        return {"ok": True}

    @staticmethod
    def offer_id(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise RequestError("Choose a valid listing.")
        text = str(value)
        if not re.fullmatch(r"[1-9]\d{0,18}", text) or int(text) > 9223372036854775807:
            raise RequestError("Choose a valid listing.")
        return text

    def _manager_read(self, callback):
        with self.lock:
            if self.busy():
                raise RequestError("Wait for the current operation to finish.", 409)
            if not self.manager or not self.connection:
                raise RequestError("Connect and load a category first.", 409)
            manager = self.manager
            self.managing = True
        try:
            return callback(manager)
        finally:
            with self.lock:
                self.managing = False

    def list_offers(self):
        def load(manager):
            result = manager.list_offers()
            result["account"] = self.connection["account"]
            return result
        return self._manager_read(load)

    def _checked_offers(self, manager):
        if not manager:
            raise RequestError("Reconnect before publishing so the tool can count your existing listings.", 409)
        result = manager.list_offers()
        if (not isinstance(result, dict) or result.get("complete") is not True
                or not isinstance(result.get("items"), list)
                or not isinstance(result.get("category"), dict)
                or str(result["category"].get("id")) != str(self.connection["category"]["id"])):
            raise RequestError("Could not verify all existing listings in this category. Refresh your listings before publishing.", 409)
        seen = set()
        for item in result["items"]:
            try:
                offer_id = self.offer_id(item.get("id")) if isinstance(item, dict) else None
            except RequestError:
                offer_id = None
            if not offer_id or offer_id in seen:
                raise RequestError("Could not count every existing listing. Refresh your listings before publishing.", 409)
            seen.add(offer_id)
        return result

    def _capacity_snapshot(self, manager):
        count = len(self._checked_offers(manager)["items"])
        return {"limit": LISTING_LIMIT, "used": count, "remaining": max(0, LISTING_LIMIT - count),
                "category": copy.deepcopy(self.connection["category"]), "account": self.connection["account"]}

    def capacity(self):
        return self._manager_read(self._capacity_snapshot)

    @staticmethod
    def _require_capacity(capacity, requested):
        if requested > capacity["remaining"]:
            raise RequestError(
                f"This category has {capacity['used']} of {capacity['limit']} listings (including paused listings). "
                f"Only {capacity['remaining']} slots remain; this batch needs {requested}. "
                "Remove listings or reduce the batch before publishing.", 409)

    def get_offer(self, offer_id):
        offer_id = self.offer_id(offer_id)
        def load(manager):
            result = manager.get_offer(offer_id)
            with self.lock:
                self.inspections[offer_id] = {"revision": result["revision"], "offer": copy.deepcopy(result)}
                if len(self.inspections) > 500:
                    self.inspections.pop(next(iter(self.inspections)))
            return result
        return self._manager_read(load)

    def _manage_price(self, changes, body):
        if "price" not in changes:
            return changes
        entered = clean_price(changes["price"], "Listing price")
        quote_job = self.price_job
        if (not quote_job or quote_job["quote_id"] != body.get("quote_id")
                or quote_job["state"] != "completed" or quote_job["identity"] != self.identity
                or quote_job["currency"] != self.connection["currency"]
                or time.monotonic() - quote_job["created_at"] > QUOTE_TTL):
            raise RequestError("Review the buyer prices again before saving these listings.", 409)
        quote = quote_job["quotes"].get(entered)
        if not quote:
            raise RequestError("A listing price changed. Review the buyer prices again.", 409)
        if not quote.get("exact") and body.get("accept_rounded_prices") is not True:
            raise RequestError("Accept the shown buyer prices before saving these listings.", 409)
        changes["price"] = clean_price(quote["seller_price"], "Calculated seller price")
        changes["buyer_target_rub"] = entered
        changes["buyer_price_rub"] = quote["buyer_price_rub"]
        return changes

    def manage(self, body):
        action = body.get("action")
        if action not in ("update", "delete"):
            raise RequestError("Choose an update or delete action.")
        raw = body.get("items")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
            raise RequestError("Choose between 1 and 100 listings at a time.")
        if body.get("price_mode", "buyer_rub") != "buyer_rub":
            raise RequestError("All listing prices are buyer prices in RUB. Refresh the app and review the prices.")
        items, seen = [], set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise RequestError("Invalid listing action.")
            offer_id = self.offer_id(entry.get("id"))
            if offer_id in seen:
                raise RequestError("A listing appears more than once in this action.")
            seen.add(offer_id)
            revision = entry.get("revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise RequestError("Reload this listing before changing it.", 409)
            changes = copy.deepcopy(entry.get("changes", {}))
            if not isinstance(changes, dict) or any(key not in {"title_ru", "title_en", "description_ru", "description_en", "price", "active", "amount", "fields"} for key in changes):
                raise RequestError("This action contains unsupported listing changes.")
            if action == "delete" and changes:
                raise RequestError("Deletion cannot include listing edits.")
            if action == "update" and not changes:
                raise RequestError("Choose what to change in this listing.")
            for key in ("title_ru", "title_en", "description_ru", "description_en"):
                if key in changes and (not isinstance(changes[key], str) or "\x00" in changes[key] or len(changes[key]) > (500 if key.startswith("title") else 20000)):
                    raise RequestError("A title or description is invalid or too long.")
            if "active" in changes and not isinstance(changes["active"], bool):
                raise RequestError("Listing visibility is invalid.")
            if "amount" in changes:
                amount = str(changes["amount"])
                if not re.fullmatch(r"\d{1,12}", amount) or not 1 <= int(amount) <= 1000000000:
                    raise RequestError("Stock must be a positive whole number no greater than 1,000,000,000.")
                changes["amount"] = str(int(amount))
            if "fields" in changes:
                fields = changes["fields"]
                if not isinstance(fields, dict) or len(fields) > 100 or any(not isinstance(key, str) or not isinstance(value, (str, int, bool)) for key, value in fields.items()):
                    raise RequestError("The category options are invalid.")
            items.append({"id": offer_id, "revision": revision, "changes": changes})
        with self.lock:
            if self.busy():
                raise RequestError("Wait for the current operation to finish.", 409)
            if not self.manager or not self.connection:
                raise RequestError("Connect and load a category first.", 409)
            for item in items:
                inspected = self.inspections.get(item["id"])
                if not inspected or inspected["revision"] != item["revision"]:
                    raise RequestError("Reload this listing before changing it.", 409)
                item["title"] = inspected["offer"].get("title_ru") or inspected["offer"].get("title_en") or "Listing #" + item["id"]
                item["changes"] = self._manage_price(item["changes"], body)
            for item in items:
                self.inspections.pop(item["id"], None)
            self.manage_stop_event = threading.Event()
            self.manage_job = {"job_id": secrets.token_urlsafe(16), "state": "running", "completed": 0,
                               "total": len(items), "action": action, "items": [
                                   {"id": item["id"], "title": item["title"], "state": "queued", "message": "Waiting", "url": None,
                                    "seller_price": item["changes"].get("price"), "buyer_price_rub": item["changes"].get("buyer_price_rub")}
                                   for item in items]}
            job_id = self.manage_job["job_id"]
            threading.Thread(target=self._manage_run, args=(action, items), daemon=True).start()
        return {"job_id": job_id}

    def _manage_run(self, action, items):
        fatal = False
        try:
            for index, item in enumerate(items):
                if self.manage_stop_event.is_set():
                    break
                with self.lock:
                    row = self.manage_job["items"][index]
                    row.update(state="deleting" if action == "delete" else "updating", message="Deleting…" if action == "delete" else "Saving…")
                try:
                    if action == "delete":
                        result = self.manager.delete_offer(item["id"], item["revision"])
                    else:
                        result = self.manager.save_offer(item["id"], item["changes"], item["revision"])
                    state, message, url = ("deleted" if action == "delete" else "updated"), result.get("message", "Saved"), result.get("url")
                except FunPayError as error:
                    state, message, url = "uncertain" if error.uncertain else "failed", str(error), None
                    fatal = error.uncertain or error.kind in ("auth", "blocked", "rate_limit", "rate", "cloudflare", "access", "network", "schema", "form", "editor_missing", "currency", "http", "pricing")
                except Exception:
                    state, message, url, fatal = "uncertain", "The result could not be confirmed. Refresh your listings before taking another action.", None, True
                with self.lock:
                    row.update(state=state, message=message, url=url)
                    self.manage_job["completed"] += 1
                if fatal or (index < len(items) - 1 and self.manage_stop_event.wait(3)):
                    break
        finally:
            with self.lock:
                for row in self.manage_job["items"]:
                    if row["state"] == "queued":
                        row.update(state="cancelled", message="Not changed")
                self.manage_job["state"] = "stopped" if fatal or self.manage_stop_event.is_set() else "completed"

    def get_manage_job(self, job_id):
        with self.lock:
            if not self.manage_job or self.manage_job["job_id"] != job_id:
                raise RequestError("Listing action not found. Refresh your listings before trying again.", 404)
            return copy.deepcopy(self.manage_job)

    def stop_manage(self, job_id):
        with self.lock:
            if not self.manage_job or self.manage_job["job_id"] != job_id:
                raise RequestError("Listing action not found.", 404)
            self.manage_stop_event.set()
        return {"ok": True, "message": "Stopping after the current listing finishes."}

    def quote(self, body):
        if body.get("price_mode") != "buyer_rub":
            raise RequestError("Buyer price checks must use RUB.")
        raw = body.get("prices")
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS:
            raise RequestError(f"Check between 1 and {MAX_ITEMS} prices at a time.")
        prices = list(dict.fromkeys(clean_price(value, "Buyer price") for value in raw))
        with self.lock:
            if self.busy():
                raise RequestError("Wait for the current operation to finish.", 409)
            if not self.client or not self.connection:
                raise RequestError("Connect and load a category first.", 409)
            if self.connection.get("currency") not in ("RUB", "USD", "EUR", "UAH"):
                raise RequestError("FunPay did not identify your seller currency. Reconnect before calculating buyer prices.", 409)
            self.quote_stop_event = threading.Event()
            self.price_job = {"quote_id": secrets.token_urlsafe(16), "state": "running", "completed": 0,
                              "total": len(prices), "quotes": {}, "error": None,
                              "currency": self.connection["currency"], "category": self.connection["category"],
                              "identity": self.identity, "created_at": time.monotonic()}
            quote_id = self.price_job["quote_id"]
            threading.Thread(target=self._quote_run, args=(prices,), daemon=True).start()
        return {"quote_id": quote_id}

    def _quote_run(self, prices):
        try:
            for index, price in enumerate(prices):
                if self.quote_stop_event.is_set():
                    break
                result = self.client.quote_buyer_rub(price)
                with self.lock:
                    self.price_job["quotes"][price] = result
                    self.price_job["completed"] += 1
                if index < len(prices) - 1 and self.quote_stop_event.wait(0.3):
                    break
            with self.lock:
                self.price_job["state"] = "cancelled" if self.quote_stop_event.is_set() else "completed"
                self.price_job["created_at"] = time.monotonic()
        except FunPayError as error:
            with self.lock:
                self.price_job["state"] = "failed"
                self.price_job["error"] = str(error)
        except Exception:
            with self.lock:
                self.price_job["state"] = "failed"
                self.price_job["error"] = "Could not calculate these buyer prices. No listings were submitted. Reconnect and try again."

    def get_quote(self, quote_id):
        with self.lock:
            if not self.price_job or self.price_job["quote_id"] != quote_id:
                raise RequestError("Price check not found. Calculate the prices again.", 404)
            result = copy.deepcopy(self.price_job)
            result.pop("identity", None)
            result["expires_in"] = max(0, int(QUOTE_TTL - (time.monotonic() - result.pop("created_at"))))
            return result

    def cancel_quote(self, quote_id):
        with self.lock:
            if not self.price_job or self.price_job["quote_id"] != quote_id:
                raise RequestError("Price check not found.", 404)
            self.quote_stop_event.set()
        return {"ok": True, "message": "Stopping after the current price check."}

    def start(self, body):
        items = clean_items(body.get("items"))
        price_mode = body.get("price_mode", "buyer_rub")
        if price_mode != "buyer_rub":
            raise RequestError("All listing prices are buyer prices in RUB. Refresh the app and review the prices.")
        fields = body.get("fields", {})
        if not isinstance(fields, dict) or len(fields) > 100:
            raise RequestError("Category fields are invalid.")
        if any(not isinstance(k, str) or not isinstance(v, (str, bool, int, float)) for k, v in fields.items()):
            raise RequestError("Category fields must contain simple values.")
        try:
            delay = float(body.get("delay", 3))
            if not 3 <= delay <= 60:
                raise ValueError
        except (TypeError, ValueError):
            raise RequestError("Use a delay between 3 and 60 seconds.")
        if not isinstance(body.get("active", True), bool):
            raise RequestError("Listing visibility is invalid.")
        with self.lock:
            if self.busy():
                raise RequestError("A batch is already running.", 409)
            if not self.client or not self.connection:
                raise RequestError("Connect and load a category first.", 409)
            if self.connection.get("creation_available") is False:
                raise RequestError("FunPay has not provided a new-listing editor for this category. You can manage existing offers in My listings. Open the category on FunPay, then reconnect when creating offers is available.", 409)
            allowed_fields = {field["name"]: field for field in self.connection.get("fields", [])}
            if any(name not in allowed_fields for name in fields):
                raise RequestError("Reload the category before changing its fields.")
            field_values = {name: field.get("value", "") for name, field in allowed_fields.items()}
            field_values.update(fields)
            for name, field in allowed_fields.items():
                if not field_enabled(field, field_values):
                    fields.pop(name, None)
                    continue
                value = fields.get(name, field.get("value", ""))
                if field.get("required") and (value is False or not str(value).strip()):
                    raise RequestError(f"Fill in the category field: {field.get('label', name)}.")
                if field.get("options") and str(value) not in {str(o["value"]) for o in field["options"]}:
                    raise RequestError(f"Choose a valid value for {field.get('label', name)}.")
                fields[name] = value
            for item in items:
                prior = self.outcomes.get((self.identity, item["id"]))
                if prior in ("published", "uncertain"):
                    raise RequestError("This batch includes an already published or unconfirmed item. Check it on FunPay first.", 409)
                quote_job = self.price_job
                if not quote_job or quote_job["quote_id"] != body.get("quote_id") or quote_job["state"] != "completed" or quote_job["identity"] != self.identity or quote_job["currency"] != self.connection["currency"]:
                    raise RequestError("Review the buyer prices before publishing.", 409)
                if time.monotonic() - quote_job["created_at"] > QUOTE_TTL:
                    raise RequestError("Your pricing preview expired. Review the buyer prices again.", 409)
                quote = quote_job["quotes"].get(item["price"])
                if not quote:
                    raise RequestError("An item price changed. Review the buyer prices again.", 409)
                if not quote.get("exact") and body.get("accept_rounded_prices") is not True:
                    raise RequestError("FunPay could not match every buyer price exactly. Review and accept the shown buyer amounts before publishing.", 409)
                item["buyer_target_rub"] = item["price"]
                item["buyer_price_rub"] = quote["buyer_price_rub"]
                item["price"] = clean_price(quote["seller_price"], "Calculated seller price")
                item["seller_currency"] = quote["seller_currency"]
                item["price_mode"] = price_mode
            # Reserve the connection while reading FunPay, without holding the lock over network I/O.
            self.managing = True
            manager = self.manager
        try:
            capacity = self._capacity_snapshot(manager)
            self._require_capacity(capacity, len(items))
            with self.lock:
                self.stop_event = threading.Event()
                self.job = {"job_id": secrets.token_urlsafe(16), "state": "running",
                            "account": self.connection["account"], "total": len(items), "completed": 0,
                            "items": [{"id": item["id"], "state": "queued", "message": "Waiting", "url": None,
                                       "price_mode": price_mode, "seller_price": item["price"], "seller_currency": self.connection.get("currency", "Account currency"),
                                       "buyer_price_rub": item.get("buyer_price_rub"), "buyer_target_rub": item.get("buyer_target_rub")} for item in items]}
                job_id = self.job["job_id"]
                threading.Thread(target=self._run, args=(items, fields, delay, body.get("active", True)), daemon=True).start()
        finally:
            with self.lock:
                self.managing = False
        return {"job_id": job_id}

    def _run(self, items, fields, delay, active):
        fatal = False
        try:
            for index, item in enumerate(items):
                if self.stop_event.is_set():
                    break
                with self.lock:
                    row = self.job["items"][index]
                    row.update(state="publishing", message="Sending to FunPay…")
                publishing_started = False
                try:
                    self._require_capacity(self._capacity_snapshot(self.manager), 1)
                    if self.stop_event.is_set():
                        with self.lock:
                            row.update(state="cancelled", message="Not sent")
                        break
                    publishing_started = True
                    result = self.client.publish(item, fields, active=active)
                    state, message, url = "published", result.get("message", "Published"), result.get("url")
                except RequestError as error:
                    state, message, url, fatal = "failed", str(error), None, True
                except FunPayError as error:
                    state = "uncertain" if error.uncertain else "failed"
                    message, url = str(error), None
                    fatal = not publishing_started or error.uncertain or error.kind in ("auth", "blocked", "rate_limit", "rate", "cloudflare", "access", "network", "schema", "form", "editor_missing", "currency", "http", "pricing", "capacity")
                except Exception:
                    # An unexpected error could occur after FunPay saved the listing.
                    state = "uncertain" if publishing_started else "failed"
                    message = ("Result could not be confirmed. Check your FunPay offers before trying again." if publishing_started
                               else "Could not verify the category's remaining listing slots. Nothing was sent for this item.")
                    url, fatal = None, True
                with self.lock:
                    row.update(state=state, message=message, url=url)
                    self.outcomes[(self.identity, item["id"])] = state
                    self.job["completed"] += 1
                if fatal or (index < len(items) - 1 and self.stop_event.wait(delay)):
                    break
        finally:
            with self.lock:
                for row in self.job["items"]:
                    if row["state"] == "queued":
                        row.update(state="cancelled", message="Not sent")
                self.job["state"] = "stopped" if self.stop_event.is_set() or fatal else "completed"

    def get_job(self, job_id):
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("Batch not found. Keep this tab open while publishing.", 404)
            return copy.deepcopy(self.job)

    def stop(self, job_id):
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("Batch not found.", 404)
            self.stop_event.set()
        return {"ok": True, "message": "Stopping after the current listing finishes."}


class Handler(BaseHTTPRequestHandler):
    server_version = "FunPayLister/1.0"

    def log_message(self, *_):
        pass

    def send_data(self, status, data, content_type="application/json; charset=utf-8"):
        payload = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def check_request(self, require_token=True):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in allowed_hosts:
            raise RequestError("This tool only accepts local connections.", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed_hosts}:
            raise RequestError("Open the tool directly in its local tab.", 403)
        if require_token and not secrets.compare_digest(self.headers.get("X-Lister-Token", ""), self.server.lister.token):
            raise RequestError("Refresh the tool to reconnect to the local app.", 403)

    def do_GET(self):
        try:
            route = urlsplit(self.path)
            self.check_request(require_token=route.path.startswith("/api/") and route.path != "/api/config")
            if route.path == "/api/config":
                self.send_data(200, {"token": self.server.lister.token, "version": VERSION, "max_items": MAX_ITEMS, "listing_limit": LISTING_LIMIT})
            elif route.path == "/api/job":
                self.send_data(200, self.server.lister.get_job(parse_qs(route.query).get("id", [""])[0]))
            elif route.path == "/api/quote":
                self.send_data(200, self.server.lister.get_quote(parse_qs(route.query).get("id", [""])[0]))
            elif route.path == "/api/offers":
                self.send_data(200, self.server.lister.list_offers())
            elif route.path == "/api/capacity":
                self.send_data(200, self.server.lister.capacity())
            elif route.path == "/api/offer":
                self.send_data(200, self.server.lister.get_offer(parse_qs(route.query).get("id", [""])[0]))
            elif route.path == "/api/manage_job":
                self.send_data(200, self.server.lister.get_manage_job(parse_qs(route.query).get("id", [""])[0]))
            elif route.path == "/api/connection_job":
                self.send_data(200, self.server.lister.get_connection_job(parse_qs(route.query).get("id", [""])[0]))
            elif route.path in ("/", "/index.html", "/styles.css", "/app.js", "/manager.js"):
                path = ROOT / "static" / ("index.html" if route.path == "/" else route.path[1:])
                if not path.is_file():
                    raise RequestError("Application file not found.", 404)
                mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}[path.suffix]
                self.send_data(200, path.read_bytes(), mime + "; charset=utf-8")
            else:
                raise RequestError("Not found.", 404)
        except RequestError as error:
            self.send_data(error.status, {"error": str(error)})
        except FunPayError as error:
            self.send_data(400, {"error": str(error), "kind": error.kind, "uncertain": error.uncertain})
        except Exception:
            self.send_data(500, {"error": "Could not load your listings. Reconnect and try again."})

    def do_POST(self):
        try:
            self.check_request()
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise RequestError("Send JSON data.", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    raise ValueError
            except ValueError:
                raise RequestError("Request is empty or too large.", 413)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise RequestError("Invalid request data.")
            if not isinstance(body, dict):
                raise RequestError("Invalid request data.")
            lister, path = self.server.lister, urlsplit(self.path).path
            if path == "/api/connect":
                response = lister.begin_connect(body)
            elif path == "/api/connect_stop":
                response = lister.stop_connection(body.get("job_id"))
            elif path == "/api/disconnect":
                response = lister.disconnect()
            elif path == "/api/start":
                response = lister.start(body)
            elif path == "/api/quote":
                response = lister.quote(body)
            elif path == "/api/cancel_quote":
                response = lister.cancel_quote(body.get("quote_id"))
            elif path == "/api/manage":
                response = lister.manage(body)
            elif path == "/api/manage_stop":
                response = lister.stop_manage(body.get("job_id"))
            elif path == "/api/stop":
                response = lister.stop(body.get("job_id"))
            else:
                raise RequestError("Not found.", 404)
            self.send_data(200, response)
        except RequestError as error:
            self.send_data(error.status, {"error": str(error)})
        except FunPayError as error:
            self.send_data(400, {"error": str(error), "kind": error.kind, "uncertain": error.uncertain})
        except Exception:
            self.send_data(500, {"error": "The local app could not complete this request. Reconnect and try again."})


def main():
    parser = argparse.ArgumentParser(description="FunPay Lister — local Windows tool")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print("That local port is in use. Close the other Lister window, or run app.py --port 8766.")
        return 1
    server.lister = Lister()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"FunPay Lister {VERSION} is ready: {url}")
    print("Keep this window open while using the tool. Press Ctrl+C to stop.")
    print("Golden Keys stay in memory. Connecting automatically deletes paused and zero-stock listings in the chosen category.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        server.lister.stop_event.set()
        server.lister.quote_stop_event.set()
        server.lister.manage_stop_event.set()
        server.lister.connection_stop_event.set()
        print("\nClosing FunPay Lister.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
