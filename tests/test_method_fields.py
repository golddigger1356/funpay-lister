"""Regression fixtures for conditional FunPay category fields. No live requests."""

from __future__ import annotations

import html
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import funpay


HOME = '<body data-app-data="{&quot;userId&quot;:777,&quot;csrf-token&quot;:&quot;app-csrf&quot;}"><div class="user-link-name">Fixture Seller</div></body>'
METADATA = [
    {"id": "type", "type": 4, "conditions": []},
    {"id": "method", "type": 5, "conditions": [{"id": "type", "list": ["предметы"]}]},
]
ITEM = {"id": "method-item", "title_ru": "Предмет", "title_en": "Item",
        "description_ru": "Описание", "description_en": "Description", "price": "12.50"}


def editor(*, pseudo_type=False, token="form-csrf"):
    type_control = '''<select id="f-type" name="fields[type]" required>
      <option value="">Выберите</option><option value="Предметы">Предметы</option>
      <option value="Услуги">Услуги</option></select>'''
    if pseudo_type:
        type_control = '''<div class="lot-field-radio-box">
          <input id="f-type" type="text" class="lot-field-input radio-box-value hidden" name="fields[type]" value="">
          <button type="button" value="">Выберите</button>
          <button type="button" value="Валюта">Валюта</button>
          <button type="button" value="Аккаунты">Аккаунты</button>
          <button type="button" value="Предметы">Предметы</button>
          <button type="button" value="Услуги">Услуги</button>
          <button type="button" value="VIP-сервер">VIP-сервер</button>
          <button type="button" value="Gamepass">Gamepass</button>
        </div>'''
    metadata = html.escape(json.dumps(METADATA, ensure_ascii=False), quote=True)
    return f'''<body data-app-data="{{&quot;userId&quot;:777,&quot;csrf-token&quot;:&quot;app-csrf&quot;}}">
      <a class="js-back-link" href="/lots/123/trade">Items &amp; Services</a>
      <form class="form-offer-editor" action="/lots/offerSave">
        <input name="node_id" type="hidden" value="123">
        <input name="offer_id" type="hidden" value="">
        <input name="csrf_token" type="hidden" value="{token}">
        <input name="form_created_at" type="hidden" value="created-{token}">
        <input name="fields[summary][ru]" maxlength="100" value="">
        <input name="fields[summary][en]" maxlength="100" value="">
        <textarea name="fields[desc][ru]"></textarea>
        <textarea name="fields[desc][en]"></textarea>
        <div class="form-group"><label for="price">Цена</label>
          <input id="price" name="price" value="0"><span class="form-control-feedback">₽</span>
        </div>
        <div class="lot-fields" data-fields="{metadata}">
          <div class="form-group lot-field" data-id="type"><label for="f-type" class="required">Тип</label>{type_control}</div>
          <div class="form-group lot-field hidden" data-id="method" style="display:none">
            <label for="f-method" class="required">Способ передачи</label>
            <select id="f-method" name="fields[method]" disabled required>
              <option value="">Выберите</option><option value="Почта">Почта</option>
              <option value="Трейд">Трейд</option><option value="Прочее">Прочее</option>
            </select>
          </div>
        </div>
        <div class="form-group hidden"><input name="fields[hidden-default]" value="server-default"></div>
        <input type="checkbox" name="active" checked>
      </form></body>'''


class MethodClient(funpay.FunPayClient):
    def __init__(self, pseudo_type=False):
        super().__init__()
        self.calls = []
        self.forms = 0
        self.pseudo_type = pseudo_type

    def _request(self, path, payload=None, *, readonly=False):
        self.calls.append((path, payload.copy() if payload is not None else None))
        if path == "/":
            return HOME
        if path.startswith("/lots/offerEdit"):
            self.forms += 1
            return editor(pseudo_type=self.pseudo_type, token=f"form-{self.forms}")
        if path == "/lots/calc":
            return json.dumps({"methods": [{"unit": "₽", "price": payload["price"]}]})
        if path == "/lots/offerSave":
            return json.dumps({"done": True, "url": "/lots/offer?id=99123"})
        raise AssertionError(f"Unexpected fixture request: {path}")

    @property
    def posts(self):
        return [payload for path, payload in self.calls if path == "/lots/offerSave"]


def connected(pseudo_type=False):
    client = MethodClient(pseudo_type)
    info = client.connect("method-fixture-key-never-real-123456789", "123")
    return client, info


class EmptyCategoryManager:
    def __init__(self, client):
        self.client = client

    def list_offers(self):
        return {"items": [], "complete": True, "category": {"id": self.client.category_id}}

    def connection_metadata(self):
        return {"currency": "RUB", "category": self.list_offers()["category"], "count": 0}


class ConditionalMethodTests(unittest.TestCase):
    def test_hidden_disabled_method_is_in_schema_with_conditions_and_real_options(self):
        form = funpay._parse_form(editor(), "123")
        fields = {field["name"]: field for field in form["fields"]}
        self.assertIn("fields[method]", fields)
        method = fields["fields[method]"]
        self.assertEqual(method["label"], "Способ передачи")
        self.assertEqual(method["type"], "select")
        self.assertTrue(method["required"])
        self.assertEqual([option["value"] for option in method["options"]], ["", "Почта", "Трейд", "Прочее"])
        self.assertEqual(method["conditions"], [{"name": "fields[type]", "values": ["предметы"]}])

    def test_active_method_requires_nonblank_before_post(self):
        for submitted in [{"fields[type]": "Предметы"}, {"fields[type]": "Предметы", "fields[method]": ""}]:
            with self.subTest(submitted=submitted):
                client, _ = connected()
                with self.assertRaises(funpay.FunPayError) as raised:
                    client.publish(ITEM, submitted)
                self.assertFalse(raised.exception.uncertain)
                self.assertEqual(client.posts, [])

    def test_actual_method_value_is_posted_exactly_once(self):
        client, _ = connected()
        result = client.publish(ITEM, {"fields[type]": "Предметы", "fields[method]": "Трейд"})
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0]["fields[type]"], "Предметы")
        self.assertEqual(client.posts[0]["fields[method]"], "Трейд")
        self.assertEqual(result["url"], "https://funpay.com/lots/offer?id=99123")

    def test_active_method_rejects_values_not_in_real_options_before_post(self):
        client, _ = connected()
        with self.assertRaises(funpay.FunPayError):
            client.publish(ITEM, {"fields[type]": "Предметы", "fields[method]": "Trade"})
        self.assertEqual(client.posts, [])

    def test_inactive_method_is_omitted_even_with_stale_submitted_value(self):
        for method in ["", "Трейд", "obsolete-not-an-option"]:
            with self.subTest(method=method):
                client, _ = connected()
                client.publish(ITEM, {"fields[type]": "Услуги", "fields[method]": method})
                self.assertEqual(len(client.posts), 1)
                self.assertEqual(client.posts[0]["fields[type]"], "Услуги")
                self.assertNotIn("fields[method]", client.posts[0])

    def test_hidden_tokens_and_unrecognized_hidden_defaults_are_uneditable(self):
        form = funpay._parse_form(editor(), "123")
        names = {field["name"] for field in form["fields"]}
        for protected in ["csrf_token", "form_created_at", "node_id", "offer_id", "fields[hidden-default]"]:
            with self.subTest(protected=protected):
                self.assertNotIn(protected, names)
                client, _ = connected()
                with self.assertRaises(funpay.FunPayError):
                    client.publish(ITEM, {"fields[type]": "Предметы", "fields[method]": "Трейд", protected: "override"})
                self.assertEqual(client.posts, [])
        client, _ = connected()
        client.publish(ITEM, {"fields[type]": "Предметы", "fields[method]": "Трейд"})
        self.assertEqual(client.posts[0]["csrf_token"], "form-2")
        self.assertEqual(client.posts[0]["form_created_at"], "created-form-2")
        self.assertEqual(client.posts[0]["fields[hidden-default]"], "server-default")

    def test_pseudo_radio_hidden_input_and_buttons_map_to_select(self):
        form = funpay._parse_form(editor(pseudo_type=True), "123")
        fields = {field["name"]: field for field in form["fields"]}
        self.assertIn("fields[type]", fields)
        control = fields["fields[type]"]
        self.assertEqual(control["type"], "select")
        self.assertTrue(control["required"])
        self.assertEqual([option["value"] for option in control["options"]],
                         ["", "Валюта", "Аккаунты", "Предметы", "Услуги", "VIP-сервер", "Gamepass"])
        client, _ = connected(pseudo_type=True)
        client.publish(ITEM, {"fields[type]": "Предметы", "fields[method]": "Трейд"})
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0]["fields[type]"], "Предметы")
        self.assertEqual(client.posts[0]["fields[method]"], "Трейд")

    def test_missing_method_is_rejected_by_queue_before_worker_starts(self):
        for submitted in [{"fields[type]": "Предметы"}, {"fields[type]": "Предметы", "fields[method]": ""}]:
            with self.subTest(submitted=submitted):
                instance = app.Lister(client_factory=MethodClient, manager_factory=EmptyCategoryManager)
                instance.connect({"golden_key": "method-fixture-key-never-real-123456789", "category": "123"})
                with patch.object(app.threading, "Thread") as worker:
                    with self.assertRaises(app.RequestError):
                        instance.start({"items": [ITEM], "fields": submitted})
                    worker.assert_not_called()
                self.assertEqual(instance.client.posts, [])

    def test_inactive_method_does_not_block_queue(self):
        for method in ["", "obsolete-not-an-option"]:
            with self.subTest(method=method):
                instance = app.Lister(client_factory=MethodClient, manager_factory=EmptyCategoryManager)
                instance.connect({"golden_key": "method-fixture-key-never-real-123456789", "category": "123"})
                quoted = instance.quote({"price_mode": "buyer_rub", "prices": [ITEM["price"]]})
                deadline = time.monotonic() + 2
                while instance.get_quote(quoted["quote_id"])["state"] == "running":
                    if time.monotonic() > deadline:
                        self.fail("Queue fixture did not finish its buyer price calculation")
                    time.sleep(0.005)
                self.assertEqual(instance.get_quote(quoted["quote_id"])["state"], "completed")
                result = instance.start({"items": [ITEM], "quote_id": quoted["quote_id"],
                                         "fields": {"fields[type]": "Услуги", "fields[method]": method}})
                deadline = time.monotonic() + 2
                while instance.get_job(result["job_id"])["state"] == "running":
                    if time.monotonic() > deadline:
                        self.fail("Queue did not finish its offline fixture request")
                    time.sleep(0.005)
                job = instance.get_job(result["job_id"])
                self.assertEqual(job["items"][0]["state"], "published")
                self.assertEqual(len(instance.client.posts), 1)
                self.assertNotIn("fields[method]", instance.client.posts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
