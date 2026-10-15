import http.client
import io
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funpay import FunPayClient, FunPayError, _parse_form, _SafeRedirects, parse_category
from urllib.request import Request


HOME = '<body data-app-data="{&quot;userId&quot;:777,&quot;csrf-token&quot;:&quot;app-csrf&quot;}"><div class="user-link-name">Fixture Seller</div></body>'


def editor(token="form-csrf", currency="₽"):
    return '''<body data-app-data="{&quot;userId&quot;:777,&quot;csrf-token&quot;:&quot;app-csrf&quot;}">
    <a class="js-back-link" href="/lots/123/trade">Test Items</a>
    <form class="form-offer-editor" action="/lots/offerSave">
    <input name="node_id" type="hidden" value="123">
    <input name="offer_id" type="hidden" value="">
    <input name="csrf_token" type="hidden" value="TOKEN">
    <input name="form_created_at" type="hidden" value="created-TOKEN">
    <input name="fields[summary][ru]" maxlength="100" value="">
    <input name="fields[summary][en]" maxlength="100" value="">
    <textarea name="fields[desc][ru]"></textarea>
    <textarea name="fields[desc][en]"></textarea>
    <div class="form-group"><label for="price">Цена</label><input id="price" name="price" value="0"><span class="form-control-feedback">CURRENCY</span></div>
    <div class="form-group"><label for="type" class="required">Тип</label><select id="type" name="fields[type]" required><option value="">Выберите</option><option value="Sword" selected>Меч &amp; Щит</option></select></div>
    <div class="form-group"><label for="region">Регион</label><input name="fields[region]" id="region" value="EU"></div>
    <div class="form-group hidden"><input name="fields[hidden-default]" value="preserved"></div>
    <div class="form-group"><label for="flag">Флаг</label><input type="checkbox" id="flag" name="fields[flag]" value="yes"></div>
    <div class="form-group"><label for="amount">Количество</label><input type="number" id="amount" name="amount" value=""></div>
    <input type="checkbox" name="active" checked>
    <input type="checkbox" name="auto_delivery" checked>
    <textarea name="secrets">never send this default</textarea>
    <input name="fields[images]" type="hidden" value="">
    </form></body>'''.replace("TOKEN", token).replace("CURRENCY", currency)


ITEM = {"title_ru": "Меч", "title_en": "Sword", "description_ru": "Меч & щит\nВторая строка", "description_en": "Sword & shield\nSecond line", "price": "12,50"}


class FixtureClient(FunPayClient):
    def __init__(self, result=None):
        super().__init__()
        self.calls = []
        self.result = result if result is not None else {"done": True, "url": "/lots/offerEdit?offer=123456"}
        self.forms = 0

    def _request(self, path, payload=None):
        self.calls.append((path, payload))
        if path == "/":
            return HOME
        if path.startswith("/lots/offerEdit"):
            self.forms += 1
            return editor("token-" + str(self.forms))
        if path == "/lots/offerSave":
            if isinstance(self.result, Exception):
                raise self.result
            return self.result if isinstance(self.result, str) else json.dumps(self.result)
        raise AssertionError(path)


class RaisingOpener:
    def __init__(self, exception):
        self.exception = exception
        self.calls = 0

    def open(self, *args, **kwargs):
        self.calls += 1
        raise self.exception


class ClientTests(unittest.TestCase):
    def connected(self, result=None):
        client = FixtureClient(result)
        info = client.connect("fixture-key-never-real", "123")
        self.assertEqual(info["account"], "Fixture Seller")
        self.assertEqual(info["currency"], "RUB")
        return client

    def test_form_schema_labels_options_and_defaults(self):
        form = _parse_form(editor(), "123")
        fields = {field["name"]: field for field in form["fields"]}
        self.assertEqual(fields["fields[type]"]["label"], "Тип")
        self.assertEqual(fields["fields[type]"]["options"][1]["label"], "Меч & Щит")
        self.assertTrue(fields["fields[type]"]["required"])
        self.assertNotIn("csrf_token", fields)
        self.assertNotIn("fields[hidden-default]", fields)
        self.assertEqual(form["values"]["fields[hidden-default]"], "preserved")
        self.assertNotIn("fields[flag]", form["values"])

    def test_category_only_funpay_lots_allowed(self):
        for value in ("123", 123, "https://funpay.com/lots/123/", "https://funpay.com/en/lots/123/trade"):
            self.assertEqual(parse_category(value), "123")
        for value in ("0", "-1", "https://evil.com/lots/123/", "https://funpay.com@evil.com/lots/123/", "https://funpay.com/chips/123/", "https://funpay.com/lots/offer?id=123", "http://funpay.com/lots/123/"):
            with self.assertRaises(FunPayError):
                parse_category(value)

    def test_fresh_tokens_new_offer_utf8_and_single_save(self):
        client = self.connected()
        result = client.publish(ITEM, {"fields[type]": "Sword", "fields[flag]": True})
        posts = [(path, payload) for path, payload in client.calls if payload is not None]
        self.assertEqual(len(posts), 1)
        payload = posts[0][1]
        self.assertEqual(payload["csrf_token"], "token-2")
        self.assertEqual(payload["form_created_at"], "created-token-2")
        self.assertEqual(payload["offer_id"], "0")
        self.assertEqual(payload["node_id"], "123")
        self.assertEqual(payload["price"], "12.50")
        self.assertEqual(payload["fields[desc][ru]"], ITEM["description_ru"])
        self.assertEqual(payload["fields[hidden-default]"], "preserved")
        self.assertEqual(payload["fields[flag]"], "yes")
        self.assertEqual(payload["amount"], "1")
        self.assertNotIn("auto_delivery", payload)
        self.assertEqual(payload["secrets"], "")
        self.assertEqual(result["url"], "https://funpay.com/lots/offer?id=123456")

    def test_protected_overrides_rejected_before_save(self):
        for name in ("csrf_token", "node_id", "offer_id", "deleted", "active", "fields[hidden-default]", "unknown"):
            client = self.connected()
            with self.assertRaises(FunPayError):
                client.publish(ITEM, {name: "bad"})
            self.assertFalse(any(payload for _, payload in client.calls))

    def test_invalid_select_and_required_blank_are_rejected(self):
        for value in ("Unknown", ""):
            client = self.connected()
            with self.assertRaises(FunPayError):
                client.publish(ITEM, {"fields[type]": value})
            self.assertFalse(any(payload for _, payload in client.calls))

    def test_done_true_without_offer_url_is_confirmed(self):
        client = self.connected({"done": True})
        self.assertEqual(client.publish(ITEM)["url"], "https://funpay.com/lots/123/trade")

    def test_offer_url_without_done_is_confirmed(self):
        client = self.connected({"url": "/en/lots/offer?id=6789"})
        self.assertEqual(client.publish(ITEM)["url"], "https://funpay.com/lots/offer?id=6789")

    def test_malformed_or_arbitrary_success_responses_are_uncertain(self):
        for result in ("<html>saved perhaps</html>", {}, {"url": "https://evil.com/lots/offer?id=777"}, {"url": "/account/login"}, {"url": "/lots/123/trade"}, {"done": False, "url": "/lots/offer?id=1234"}, [1, 2]):
            client = self.connected(result)
            with self.assertRaises(FunPayError) as raised:
                client.publish(ITEM)
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(len([c for c in client.calls if c[1] is not None]), 1)

    def test_server_validation_error_is_known_failure(self):
        client = self.connected({"errors": [["price", "Minimum price is 20"]]})
        with self.assertRaises(FunPayError) as raised:
            client.publish(ITEM)
        self.assertFalse(raised.exception.uncertain)
        self.assertIn("Minimum price", str(raised.exception))

    def test_rate_rejection_classified_for_queue_stop(self):
        client = self.connected({"error": "Нельзя слишком часто добавлять предложения."})
        with self.assertRaises(FunPayError) as raised:
            client.publish(ITEM)
        self.assertEqual(raised.exception.kind, "rate")

    def test_no_network_write_for_invalid_price_or_titles(self):
        for edit in ({"price": "NaN"}, {"price": "0"}, {"price": "1e3"}, {"title_en": ""}, {"title_ru": "x" * 101}):
            client = self.connected()
            with self.assertRaises(FunPayError):
                client.publish({**ITEM, **edit})
            self.assertFalse(any(payload for _, payload in client.calls))

    def test_currency_change_stops_before_write(self):
        client = self.connected()
        client.currency = "USD"
        with self.assertRaises(FunPayError) as raised:
            client.publish(ITEM)
        self.assertEqual(raised.exception.kind, "currency")
        self.assertFalse(any(payload for _, payload in client.calls))

    def test_bad_key_and_zero_user_id_rejected(self):
        client = FixtureClient()
        for key in ("abc", "a\r\nInjected: header", "key; golden_key=other"):
            with self.assertRaises(FunPayError):
                client.connect(key, "123")
            self.assertFalse(client.calls)
        global HOME
        previous = HOME
        HOME = '<body data-app-data="{&quot;userId&quot;:&quot;0&quot;,&quot;csrf-token&quot;:&quot;x&quot;}"></body>'
        try:
            with self.assertRaises(FunPayError) as raised:
                client.connect("fixture-key-never-real", "123")
            self.assertEqual(raised.exception.kind, "auth")
        finally:
            HOME = previous

    def test_transport_error_after_save_is_uncertain_no_retry(self):
        for exception in (TimeoutError(), http.client.IncompleteRead(b"partial")):
            client = FunPayClient()
            opener = RaisingOpener(exception)
            client._opener = opener
            with self.assertRaises(FunPayError) as raised:
                client._request("/lots/offerSave", {"price": "12"})
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(opener.calls, 1)

    def test_get_transport_error_is_known_not_a_write(self):
        client = FunPayClient()
        client._opener = RaisingOpener(TimeoutError())
        with self.assertRaises(FunPayError) as raised:
            client._request("/")
        self.assertFalse(raised.exception.uncertain)

    def test_http_rate_and_cloudflare_classification(self):
        for code, body, kind in ((429, b"too many", "rate"), (403, b"Cloudflare challenge-platform", "cloudflare"), (403, b"Access denied", "access"), (500, b"Server error", "http")):
            client = FunPayClient()
            client._opener = RaisingOpener(HTTPError("https://funpay.com/lots/offerSave", code, "failure", {}, io.BytesIO(body)))
            with self.assertRaises(FunPayError) as raised:
                client._request("/lots/offerSave", {"price": "12"})
            self.assertEqual(raised.exception.kind, kind)
            self.assertEqual(raised.exception.uncertain, code == 500)

    def test_redirect_never_resends_post_or_sends_cookie_off_site(self):
        redirects = _SafeRedirects()
        for req, target in ((Request("https://funpay.com/lots/offerSave", data=b"x"), "https://funpay.com/lots/offer?id=1"), (Request("https://funpay.com/"), "https://evil.com/")):
            with self.assertRaises(HTTPError) as raised:
                redirects.redirect_request(req, None, 302, "Found", {}, target)
            raised.exception.close()

    def test_server_message_sanitizes_golden_key(self):
        client = self.connected({"error": "Invalid golden_key=fixture-key-never-real"})
        with self.assertRaises(FunPayError) as raised:
            client.publish(ITEM)
        self.assertNotIn("fixture-key-never-real", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
