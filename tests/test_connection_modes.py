"""Offline connection fallback tests: management without a new-offer editor."""

from contextlib import redirect_stderr, redirect_stdout
import html
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from funpay import FunPayClient, FunPayError
from management import OfferManager
import test_funpay_client as creation_fixture
import test_management_client as management_fixture


KEY = "connection-fixture-key-never-real"


def authenticated_home(user_id=777):
    return management_fixture.header(user_id) + "</body>"


def missing_editor(notice="The new-offer editor is unavailable."):
    return management_fixture.header() + '<p class="lead">' + html.escape(notice) + "</p></body>"


def seller_trade(count=3, *, category="123", user_id=777, currency="₽"):
    rows = []
    for index in range(count):
        offer_id = str(111 + index)
        rows.append(f'''<a class="tc-item" data-offer="{offer_id}" data-node="{category}"
            href="/lots/offerEdit?offer={offer_id}&amp;node={category}">
            <div class="tc-desc-text">Fixture Item {offer_id}</div>
            <div class="tc-price" data-s="90.91">90.91 <span class="unit">{currency}</span></div>
            <div class="tc-amount">2</div></a>''')
    return (management_fixture.header(user_id) + f'<a href="/lots/{category}/trade">Game Items</a>'
            + f'<div class="tc table-hover table-clickable" data-node="{category}">'
            + "".join(rows) + "</div></body>")


class ConnectionFixtureClient(FunPayClient):
    def __init__(self, *, count=3, currency="₽", new_editor=None):
        super().__init__()
        self.home = authenticated_home()
        self.new_editor = missing_editor() if new_editor is None else new_editor
        self.trade_source = seller_trade(count, currency=currency)
        self.editor_currency = currency
        self.calls = []
        self.edits = 0

    def _request(self, path, payload=None, *, readonly=False):
        self.calls.append((path, dict(payload) if payload is not None else None, readonly))
        if path == "/":
            return self.home
        if path == "/lots/offerEdit?node=123":
            if isinstance(self.new_editor, BaseException):
                raise self.new_editor
            return self.new_editor
        if path == "/lots/123/trade":
            return self.trade_source
        if path.startswith("/lots/offerEdit?offer="):
            self.edits += 1
            offer_id = path.split("offer=", 1)[1].split("&", 1)[0]
            return management_fixture.editor(offer=offer_id, token=f"edit-{self.edits}", currency=self.editor_currency)
        if path == "/lots/calc":
            return json.dumps({"methods": [{"unit": "₽", "price": payload["price"]}]})
        if path == "/lots/offerSave":
            return json.dumps({"done": True})
        raise AssertionError(f"Unexpected offline request: {path}")

    @property
    def saves(self):
        return [payload for path, payload, _ in self.calls if path == "/lots/offerSave"]


class ConnectionModeTests(unittest.TestCase):
    def test_normal_editor_connection_keeps_creation_schema(self):
        client = ConnectionFixtureClient(new_editor=creation_fixture.editor())
        info = client.connect(KEY, "123")
        self.assertTrue(info["creation_available"])
        self.assertFalse(info.get("management_only", False))
        self.assertTrue(info["fields"])
        self.assertTrue(client._authenticated)
        self.assertIsNotNone(client._template)
        self.assertNotIn("/lots/123/trade", [path for path, _, _ in client.calls])
        self.assertEqual(client.saves, [])

    def test_full_category_connects_for_management_with_no_creation_editor(self):
        client = ConnectionFixtureClient(count=39)
        info = client.connect(KEY, "123")
        self.assertFalse(info["creation_available"])
        self.assertTrue(info["management_only"])
        self.assertEqual(info["fields"], [])
        self.assertEqual(info["constraints"], {})
        self.assertEqual(info["category"]["id"], "123")
        self.assertEqual(info["currency"], "RUB")
        self.assertIn("39", info["message"])
        self.assertTrue(client._authenticated)
        self.assertIsNone(client._template)
        result = OfferManager(client).list_offers()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["items"]), 39)
        self.assertEqual(client.saves, [])

    def test_nonfull_missing_editor_does_not_claim_limit_was_reached(self):
        client = ConnectionFixtureClient(count=3)
        info = client.connect(KEY, "123")
        self.assertFalse(info["creation_available"])
        self.assertTrue(info["management_only"])
        self.assertNotIn("39", info["message"])
        self.assertEqual(client.saves, [])

    def test_reconnect_restores_creation_after_new_editor_becomes_available(self):
        client = ConnectionFixtureClient(count=40)
        self.assertFalse(client.connect(KEY, "123")["creation_available"])
        client.new_editor = creation_fixture.editor()
        info = client.connect(KEY, "123")
        self.assertTrue(info["creation_available"])
        self.assertFalse(info.get("management_only", False))
        self.assertIsNotNone(client._template)
        self.assertEqual(client.saves, [])

    def test_localized_save_action_without_old_form_class_is_supported(self):
        editor = creation_fixture.editor().replace('class="form-offer-editor" ', "").replace(
            'action="/lots/offerSave"', 'action="/en/lots/offerSave"')
        client = ConnectionFixtureClient(new_editor=editor)
        self.assertTrue(client.connect(KEY, "123")["creation_available"])
        self.assertNotIn("/lots/123/trade", [path for path, _, _ in client.calls])

    def test_management_only_server_connection_retains_capacity_and_blocks_creation(self):
        for count in [3, 40]:
            with self.subTest(count=count):
                client = ConnectionFixtureClient(count=count)
                instance = app.Lister(client_factory=lambda: client)
                info = instance.connect({"golden_key": KEY, "category": "123"})
                self.assertFalse(info["creation_available"])
                self.assertEqual(instance.capacity()["used"], count)
                with self.assertRaises(app.RequestError) as raised:
                    instance.start({"items": [{"id": "new-one", **creation_fixture.ITEM}]})
                self.assertEqual(raised.exception.status, 409)
                self.assertIsNone(instance.job)
                self.assertEqual(client.saves, [])

    def test_management_only_known_currency_can_quote_buyer_rub(self):
        client = ConnectionFixtureClient()
        client.connect(KEY, "123")
        quote = client.quote_buyer_rub("100.00")
        self.assertEqual(quote["buyer_price_rub"], "100.00")
        self.assertEqual(quote["seller_currency"], "RUB")
        self.assertTrue(quote["exact"])
        self.assertIsNone(client._template)
        self.assertEqual(client.saves, [])

    def test_unknown_currency_blocks_quote_but_allows_nonprice_management(self):
        client = ConnectionFixtureClient(currency="")
        info = client.connect(KEY, "123")
        self.assertNotIn(info["currency"], {"RUB", "USD", "EUR", "UAH"})
        with self.assertRaises(FunPayError) as raised:
            client.quote_buyer_rub("100.00")
        self.assertIn(raised.exception.kind, {"currency", "pricing"})
        manager = OfferManager(client)
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"active": False}, loaded["revision"])
        self.assertEqual(client.saves[0]["price"], "90.91")
        self.assertNotIn("active", client.saves[0])
        self.assertFalse(any(path == "/lots/calc" for path, _, _ in client.calls))

    def test_explicit_logged_out_user_id_rejected_even_with_username_markup(self):
        client = ConnectionFixtureClient()
        client.home = authenticated_home(0)
        with self.assertRaises(FunPayError) as raised:
            client.connect(KEY, "123")
        self.assertEqual(raised.exception.kind, "auth")
        self.assertFalse(client._authenticated)
        self.assertEqual([path for path, _, _ in client.calls], ["/"])
        self.assertEqual(client.saves, [])

    def test_failed_reconnect_clears_prior_authentication_before_validation(self):
        client = ConnectionFixtureClient()
        client.connect(KEY, "123")
        manager = OfferManager(client)
        self.assertTrue(client._authenticated)
        with self.assertRaises(FunPayError):
            client.connect("bad", "123")
        self.assertFalse(client._authenticated)
        with self.assertRaises(FunPayError) as raised:
            manager.list_offers()
        self.assertEqual(raised.exception.kind, "auth")
        self.assertEqual(client.saves, [])

    def test_protected_or_network_editor_errors_never_become_fallback_success(self):
        for kind in ["auth", "cloudflare", "access", "network", "http", "rate"]:
            with self.subTest(kind=kind):
                client = ConnectionFixtureClient(new_editor=FunPayError("Fixture editor request failed", kind=kind))
                with self.assertRaises(FunPayError) as raised:
                    client.connect(KEY, "123")
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("/lots/123/trade", [path for path, _, _ in client.calls])
                self.assertEqual(client.saves, [])

    def test_login_and_access_html_do_not_trigger_fallback(self):
        pages = [management_fixture.header(0) + '<form action="/account/login"></form></body>',
                 authenticated_home().replace("</body>", '<h1 class="page-header">Доступ запрещен</h1></body>')]
        for page in pages:
            with self.subTest(page=page[:70]):
                client = ConnectionFixtureClient(new_editor=page)
                with self.assertRaises(FunPayError) as raised:
                    client.connect(KEY, "123")
                self.assertIn(raised.exception.kind, {"auth", "access", "cloudflare"})
                self.assertNotIn("/lots/123/trade", [path for path, _, _ in client.calls])
                self.assertEqual(client.saves, [])

    def test_rate_limit_notice_never_triggers_management_fallback(self):
        client = ConnectionFixtureClient(new_editor=missing_editor("Too many requests. Please try again later."))
        with self.assertRaises(FunPayError) as raised:
            client.connect(KEY, "123")
        self.assertEqual(raised.exception.kind, "rate")
        self.assertNotIn("/lots/123/trade", [path for path, _, _ in client.calls])
        self.assertFalse(client._authenticated)

    def test_fallback_rejects_other_account_or_category(self):
        for trade in [seller_trade(user_id=888), seller_trade(category="999")]:
            with self.subTest(trade=trade[:100]):
                client = ConnectionFixtureClient()
                client.trade_source = trade
                with self.assertRaises(FunPayError):
                    client.connect(KEY, "123")
                self.assertFalse(client._authenticated)
                self.assertEqual(client.saves, [])

    def test_connection_notice_never_returns_or_logs_golden_key(self):
        client = ConnectionFixtureClient(new_editor=missing_editor("Editor unavailable. golden_key=" + KEY))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            info = client.connect(KEY, "123")
        self.assertNotIn(KEY, json.dumps(info))
        self.assertNotIn(KEY, output.getvalue())
        self.assertEqual(client.saves, [])


class FinalURLResponse:
    def __init__(self, url, body=None):
        self.url = url
        self.body = authenticated_home() if body is None else body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body.encode()


class FinalURLOpener:
    def __init__(self, url, body=None):
        self.url = url
        self.body = body

    def open(self, *args, **kwargs):
        return FinalURLResponse(self.url, self.body)


class TradeRedirectTests(unittest.TestCase):
    def test_success_status_challenge_is_classified_by_real_transport(self):
        client = FunPayClient()
        client._opener = FinalURLOpener("https://funpay.com/lots/offerEdit?node=123",
            '<html><head><title>Just a moment</title></head><body class="challenge-platform"></body></html>')
        with self.assertRaises(FunPayError) as raised:
            client._request("/lots/offerEdit?node=123")
        self.assertEqual(raised.exception.kind, "cloudflare")
        self.assertFalse(raised.exception.uncertain)

    def test_seller_trade_redirect_must_preserve_category_and_complete_page(self):
        for target in ["https://funpay.com/lots/999/trade", "https://funpay.com/",
                       "https://evil.example/lots/123/trade", "https://funpay.com/account/login",
                       "https://funpay.com/lots/123/trade?page=2"]:
            with self.subTest(target=target):
                client = FunPayClient()
                client._opener = FinalURLOpener(target)
                with self.assertRaises(FunPayError):
                    client._request("/lots/123/trade")

    def test_localized_same_category_trade_redirect_is_allowed(self):
        for target in ["https://funpay.com/lots/123/trade", "https://funpay.com/en/lots/123/trade",
                       "https://funpay.com/uk/lots/123/trade/"]:
            with self.subTest(target=target):
                client = FunPayClient()
                client._opener = FinalURLOpener(target)
                self.assertIn("Fixture Seller", client._request("/lots/123/trade"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
