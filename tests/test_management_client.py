import html
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funpay import FunPayClient, FunPayError, _parse_form
from management import OfferManager


SECRET_STOCK = "first:pass&secret\nsecond:<pass>\n"
PAYMENT_RU = "Спасибо. Сохраненное сообщение & текст."
PAYMENT_EN = "Thank you. Existing private message."


def header(user_id=777, token="app-token"):
    return '<body data-app-data="' + html.escape(json.dumps({"userId": user_id, "csrf-token": token}), quote=True) + '"><div class="user-link-name">Fixture Seller</div>'


def trade(offers=("111", "222"), user_id=777, *, noise=True):
    rows = []
    for number in offers:
        rows.append(f'''<a class="tc-item {'warning' if number == '222' else ''}" data-offer="{number}" href="/lots/offerEdit?offer={number}">
        <div class="tc-desc"><div class="tc-desc-text">Меч &amp; щит {number}</div></div>
        <div class="tc-price" data-s="90.91"><div>90.91 <span class="unit">₽</span></div></div>
        <div class="tc-amount">2</div></a>''')
    if noise:
        rows.append('<a class="tc-item" data-offer="333" href="/lots/offer?id=333"><div class="tc-desc-text">Public offer</div></a>')
        rows.append('<a class="tc-item" data-offer="444" href="https://evil.example/lots/offerEdit?offer=444">Off-site link</a>')
        rows.append('<a class="tc-item" data-offer="555" href="/lots/offerEdit?offer=555&amp;node=999">Different category</a>')
        rows.append('<a class="tc-item" data-offer="666" href="/lots/offerEdit?offer=777">Mismatched marker</a>')
    return header(user_id) + '<div class="tc table-hover table-clickable">' + ''.join(rows) + '</div></body>'


def editor(*, offer="111", node="123", token="csrf", auto=True, stock=SECRET_STOCK,
           amount=2, title="Меч", user_id=777, owner=777, missing_stock=False,
           currency="₽", type_value="item", purchase_ru=PAYMENT_RU,
           active=True, active_control=True, disabled_active=False,
           amount_control=True, data_values=None):
    values = {"node_id": node, "offer_id": offer, "csrf_token": token,
              "form_created_at": "created-" + token, "fields[summary][ru]": title,
              "fields[summary][en]": "Sword", "price": "90.91", "amount": str(amount),
              "fields[images]": "12,34", "hidden_misc": "preserve-me"}
    data = {"id": offer, "node_id": node, "user_id": owner, "amount": amount}
    data.update(data_values or {})
    if not amount_control:
        values.pop("amount")
    rules = [{"id": "type", "type": 4}, {"id": "level", "type": 4,
              "conditions": [{"id": "type", "list": ["account"]}]}]
    parts = [header(user_id, "app-" + token), '<a href="/lots/123/trade">Game Items</a>',
             '<form class="form-offer-editor" action="/lots/offerSave" data-offer="' + html.escape(json.dumps(data), quote=True) + '">']
    for name, value in values.items():
        kind = "hidden" if name in {"node_id", "offer_id", "csrf_token", "form_created_at", "fields[images]", "hidden_misc"} else "text"
        control = f'<input type="{kind}" name="{name}" value="{html.escape(value, quote=True)}" maxlength="200">'
        if name == "price":
            control = '<div class="form-group">' + control + '<span class="form-control-feedback">' + currency + '</span></div>'
        parts.append(control)
    parts += ['<textarea name="fields[desc][ru]">Старое описание\nВторая строка.</textarea>',
              '<textarea name="fields[desc][en]">Old description\nSecond line.</textarea>',
              '<textarea name="fields[payment_msg][ru]">' + html.escape(purchase_ru) + '</textarea>',
              '<textarea name="fields[payment_msg][en]">' + html.escape(PAYMENT_EN) + '</textarea>',
              '<input type="checkbox" name="auto_delivery"' + (' checked' if auto else '') + '>']
    if active_control:
        parts.append('<input type="checkbox" name="active"' + (' checked' if active else '') + (' disabled' if disabled_active else '') + '>')
    if not missing_stock:
        parts.append('<textarea name="secrets">' + html.escape(stock) + '</textarea>')
    parts += ['<div class="lot-fields" data-fields="' + html.escape(json.dumps(rules), quote=True) + '">',
              '<div class="form-group"><label for="type">Тип</label><select id="type" name="fields[type]">' +
              ''.join(f'<option value="{value}"' + (' selected' if value == type_value else '') + f'>{label}</option>' for value, label in (("item", "Предмет"), ("account", "Аккаунт"))) + '</select></div>',
              '<div class="form-group hidden"><label for="level">Уровень</label><input id="level" name="fields[level]" value="25" disabled></div>',
              '<input name="fields[disabled_plain]" value="do-not-send" disabled>',
              '<input type="checkbox" name="fields[flag]" value="yes">',
              '</div></form></body>']
    return ''.join(parts)


class FixtureClient(FunPayClient):
    def __init__(self, **editor_values):
        super().__init__()
        self.category_id = "123"
        self.user_id = 777
        self.account = "Fixture Seller"
        self.currency = "RUB"
        self._golden_key = "fake-not-a-real-key"
        self._authenticated = True
        self._template = {"category_name": "Game Items"}
        self.calls = []
        self.edits = 0
        self.editor_values = editor_values
        self.members = ("111", "222")
        self.trade_user = 777
        self.trade_noise = True
        self.trade_transform = lambda source: source
        self.editor_transform = lambda source: source
        self.response = {"done": True}
        self.write_exception = None
        self.calc_price = "100.00"
        self.calc_calls = []

    def _request(self, path, payload=None, **kwargs):
        self.calls.append((path, dict(payload) if payload is not None else None))
        if path == "/lots/123/trade":
            return self.trade_transform(trade(self.members, self.trade_user, noise=self.trade_noise))
        if path.startswith("/lots/offerEdit?offer="):
            self.edits += 1
            offer = path.split("offer=", 1)[1].split("&", 1)[0]
            options = {"offer": offer, "token": "token-" + str(self.edits)}
            options.update(self.editor_values)
            return self.editor_transform(editor(**options))
        if path == "/lots/offerSave":
            if self.write_exception:
                raise self.write_exception
            return self.response if isinstance(self.response, str) else json.dumps(self.response)
        raise AssertionError(path)

    def _calc(self, price):
        self.calc_calls.append(price)
        return {"minPrice": self.calc_price + " RUB"}

    @property
    def posts(self):
        return [payload for _, payload in self.calls if payload is not None]


class ManagementTests(unittest.TestCase):
    def setup_manager(self, **editor_values):
        client = FixtureClient(**editor_values)
        return client, OfferManager(client)

    def test_trade_only_current_sellers_editable_anchors(self):
        client, manager = self.setup_manager()
        result = manager.list_offers()
        self.assertEqual([item["id"] for item in result["items"]], ["111", "222"])
        self.assertTrue(result["items"][0]["active"])
        self.assertFalse(result["items"][1]["active"])
        self.assertEqual(result["items"][0]["seller_price"], "90.91")
        self.assertEqual(result["items"][0]["currency"], "RUB")
        self.assertEqual(result["category"]["id"], "123")
        self.assertFalse(client.posts)

    def test_editor_bilingual_safe_metadata_no_secrets_or_messages(self):
        client, manager = self.setup_manager()
        result = manager.get_offer("111")
        self.assertEqual(result["title_ru"], "Меч")
        self.assertEqual(result["title_en"], "Sword")
        self.assertEqual(result["description_en"], "Old description\nSecond line.")
        self.assertEqual(result["amount"], 2)
        self.assertTrue(result["auto_delivery"])
        self.assertFalse(result["stock_editable"])
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ("first:pass", "second:<pass>", PAYMENT_RU, PAYMENT_EN, "csrf_token", "form_created_at", "fields[images]", "fake-not-a-real-key"):
            self.assertNotIn(private, serialized)
        self.assertEqual(len(result["revision"]), 64)
        self.assertNotIn("amount", {field["name"] for field in result["fields"]})

    def test_manual_amount_editable_without_duplicate_schema(self):
        _, manager = self.setup_manager(auto=False, stock="")
        result = manager.get_offer("111")
        self.assertTrue(result["stock_editable"])
        self.assertNotIn("amount", {field["name"] for field in result["fields"]})

    def test_price_only_preserves_all_unrelated_values_and_fresh_tokens(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"price": "95.00"}, loaded["revision"])
        self.assertEqual(len(client.posts), 1)
        payload = client.posts[0]
        self.assertEqual(payload["price"], "95.00")
        self.assertEqual(payload["secrets"], SECRET_STOCK)
        self.assertEqual(payload["auto_delivery"], "on")
        self.assertEqual(payload["fields[images]"], "12,34")
        self.assertEqual(payload["fields[payment_msg][ru]"], PAYMENT_RU)
        self.assertEqual(payload["fields[payment_msg][en]"], PAYMENT_EN)
        self.assertEqual(payload["hidden_misc"], "preserve-me")
        self.assertEqual(payload["csrf_token"], "token-2")
        self.assertEqual(payload["form_created_at"], "created-token-2")
        self.assertEqual(payload["offer_id"], "111")
        self.assertEqual(payload["node_id"], "123")
        self.assertNotIn("fields[level]", payload)
        self.assertNotIn("fields[disabled_plain]", payload)
        self.assertNotIn("fields[flag]", payload)
        self.assertNotIn("deleted", payload)

    def test_active_only_does_not_clear_secret_stock_or_purchase_messages(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"active": False}, loaded["revision"])
        self.assertNotIn("active", client.posts[0])
        self.assertEqual(client.posts[0]["secrets"], SECRET_STOCK)
        self.assertEqual(client.posts[0]["fields[payment_msg][ru]"], PAYMENT_RU)
        self.assertEqual(client.posts[0]["fields[summary][ru]"], "Меч")

    def test_bilingual_descriptions_titles_fields_and_quantity_update(self):
        client, manager = self.setup_manager(auto=False, stock="")
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"title_ru": "Новый меч", "title_en": "New sword",
                                  "description_ru": "Описание\nС переносом", "description_en": "Details\nWith newline",
                                  "amount": 4, "fields": {"fields[type]": "account", "fields[level]": "30"}}, loaded["revision"])
        payload = client.posts[0]
        self.assertEqual(payload["fields[summary][ru]"], "Новый меч")
        self.assertEqual(payload["fields[summary][en]"], "New sword")
        self.assertEqual(payload["fields[desc][ru]"], "Описание\nС переносом")
        self.assertEqual(payload["fields[desc][en]"], "Details\nWith newline")
        self.assertEqual(payload["fields[level]"], "30")
        self.assertEqual(payload["amount"], "4")

    def test_revision_ignores_rotated_csrf_but_detects_actual_edit(self):
        client, manager = self.setup_manager()
        first = manager.get_offer("111")
        second = manager.get_offer("111")
        self.assertEqual(first["revision"], second["revision"])
        client.editor_values["title"] = "Other editor changed title"
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"active": False}, first["revision"])
        self.assertEqual(raised.exception.kind, "conflict")
        self.assertFalse(raised.exception.uncertain)
        self.assertFalse(client.posts)

    def test_revision_detects_private_message_and_stock_changes(self):
        for changed in ({"purchase_ru": "New private message"}, {"stock": "a:b\nc:d\n"}, {"amount": 1}):
            client, manager = self.setup_manager()
            first = manager.get_offer("111")
            client.editor_values.update(changed)
            with self.assertRaises(FunPayError) as raised:
                manager.save_offer("111", {"active": False}, first["revision"])
            self.assertEqual(raised.exception.kind, "conflict")
            self.assertFalse(client.posts)

    def test_missing_expected_revision_refuses_without_network(self):
        for revision in (None, "", "bad", "A" * 64):
            client, manager = self.setup_manager()
            with self.assertRaises(FunPayError):
                manager.save_offer("111", {"price": "100"}, revision)
            with self.assertRaises(FunPayError):
                manager.delete_offer("111", revision)
            self.assertFalse(client.calls)

    def test_removed_or_unowned_offer_cannot_be_fetched_or_saved(self):
        client, manager = self.setup_manager()
        with self.assertRaises(FunPayError):
            manager.get_offer("333")
        self.assertFalse(any(path.startswith("/lots/offerEdit") for path, _ in client.calls))
        loaded = manager.get_offer("111")
        client.members = ("222",)
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"active": False}, loaded["revision"])
        self.assertEqual(raised.exception.kind, "unavailable")
        self.assertFalse(client.posts)

    def test_exact_offer_node_and_owner_checks(self):
        for mismatch in ({"offer": "222"}, {"node": "999"}, {"owner": 888}):
            client, manager = self.setup_manager(**mismatch)
            with self.assertRaises(FunPayError):
                manager.get_offer("111")
            self.assertFalse(client.posts)

    def test_expired_or_changed_account_is_rejected(self):
        for user_id in (0, 888):
            client, manager = self.setup_manager()
            client.trade_user = user_id
            with self.assertRaises(FunPayError) as raised:
                manager.list_offers()
            self.assertEqual(raised.exception.kind, "auth")
            self.assertFalse(client.posts)

    def test_changed_connected_category_is_rejected(self):
        client, manager = self.setup_manager()
        client.category_id = "999"
        with self.assertRaises(FunPayError):
            manager.list_offers()
        self.assertFalse(client.calls)

    def test_delete_uses_verified_flag_and_only_fresh_tokens_identity(self):
        client, manager = self.setup_manager(missing_stock=True)
        loaded = manager.get_offer("111")
        result = manager.delete_offer("111", loaded["revision"])
        self.assertEqual(client.posts, [{"csrf_token": "token-2", "form_created_at": "created-token-2", "offer_id": "111", "node_id": "123", "deleted": "1"}])
        self.assertEqual(result["id"], "111")
        self.assertEqual(result["url"], "https://funpay.com/lots/123/trade")

    def test_incomplete_automatic_stock_rejects_saving(self):
        for stock_options in ({"missing_stock": True}, {"stock": ""}, {"stock": "only-one:product"}):
            client, manager = self.setup_manager(**stock_options)
            loaded = manager.get_offer("111")
            with self.assertRaises(FunPayError) as raised:
                manager.save_offer("111", {"active": False}, loaded["revision"])
            self.assertEqual(raised.exception.kind, "stock")
            self.assertFalse(client.posts)

    def test_protected_overrides_and_invalid_fields_rejected(self):
        invalid = [{"fields": {name: "bad"}} for name in ("csrf_token", "offer_id", "secrets", "fields[payment_msg][ru]", "fields[images]", "hidden_misc")]
        invalid += [{"fields": {"fields[type]": "nonexistent"}}, {"active": "false"}, {"price": "0"}, {"amount": 5}, {"auto_delivery": False}]
        for changes in invalid:
            client, manager = self.setup_manager()
            loaded = manager.get_offer("111")
            with self.assertRaises(FunPayError):
                manager.save_offer("111", changes, loaded["revision"])
            self.assertFalse(client.posts)

    def test_unreadable_or_unrelated_save_result_is_uncertain_once(self):
        for response in ("<html>unknown</html>", {}, {"url": "/account/login"}, {"url": "/lots/offer?id=999"}, {"done": False, "url": "/lots/offer?id=111"}):
            client, manager = self.setup_manager()
            loaded = manager.get_offer("111")
            client.response = response
            with self.assertRaises(FunPayError) as raised:
                manager.save_offer("111", {"price": "100"}, loaded["revision"])
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(len(client.posts), 1)

    def test_validation_rejection_known_failure(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        client.response = {"errors": [["price", "Price invalid"]]}
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"price": "100"}, loaded["revision"])
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(len(client.posts), 1)

    def test_network_uncertainty_propagates_no_retry(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        marker = FunPayError("May have saved", kind="network", uncertain=True)
        client.write_exception = marker
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"price": "100"}, loaded["revision"])
        self.assertIs(raised.exception, marker)
        self.assertEqual(len(client.posts), 1)

    def test_buyer_price_revalidation_matches_before_save(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"price": "90.91", "buyer_target_rub": "100", "buyer_price_rub": "100.00"}, loaded["revision"])
        self.assertEqual(client.calc_calls, ["90.91"])
        self.assertEqual(len(client.posts), 1)

    def test_changed_buyer_price_refuses_and_clears_quote_cache(self):
        client, manager = self.setup_manager()
        loaded = manager.get_offer("111")
        client.calc_price = "101.00"
        client._buyer_quote_cache["old"] = "quote"
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"price": "90.91", "buyer_target_rub": "100", "buyer_price_rub": "100.00"}, loaded["revision"])
        self.assertEqual(raised.exception.kind, "pricing")
        self.assertFalse(client._buyer_quote_cache)
        self.assertFalse(client.posts)

    def test_parser_new_listing_defaults_still_clear_purchase_messages(self):
        source = editor(offer="0")
        parsed = _parse_form(source, "123")
        self.assertEqual(parsed["values"]["fields[payment_msg][ru]"], "")
        self.assertEqual(parsed["values"]["fields[payment_msg][en]"], "")
        with self.assertRaises(FunPayError):
            _parse_form(editor(offer="111"), "123")

    def test_capacity_valid_authenticated_empty_seller_table_is_complete_zero(self):
        client, manager = self.setup_manager()
        client.members = ()
        client.trade_noise = False
        result = manager.list_offers()
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count_error"], "")

    def test_capacity_counts_all_forty_including_inactive(self):
        client, manager = self.setup_manager()
        client.members = tuple(str(number) for number in range(200, 240))
        client.trade_noise = False
        result = manager.list_offers()
        self.assertTrue(result["complete"], result)
        self.assertEqual(len(result["items"]), 40)
        self.assertTrue(any(not item["active"] for item in result["items"]))

    def test_capacity_deduplicates_matching_offer_ids(self):
        client, manager = self.setup_manager()
        client.members = ("111", "111")
        client.trade_noise = False
        result = manager.list_offers()
        self.assertTrue(result["complete"], result)
        self.assertEqual(len(result["items"]), 1)

    def test_capacity_no_trade_table_or_truncated_document_is_incomplete(self):
        for transform in (lambda source: source.replace('class="tc table-hover table-clickable"', 'class="unexpected"'),
                          lambda source: source.replace("</body>", ""),
                          lambda source: header() + "<p>Authenticated generic page</p></body>"):
            client, manager = self.setup_manager()
            client.trade_noise = False
            client.trade_transform = transform
            result = manager.list_offers()
            self.assertFalse(result["complete"], result)
            self.assertTrue(result["count_error"])

    def test_capacity_unaccounted_malformed_or_foreign_rows_are_incomplete(self):
        client, manager = self.setup_manager()
        # Existing display behavior can show recognized offers, but must not
        # authorize creation capacity when even one suspicious row was skipped.
        result = manager.list_offers()
        self.assertEqual(len(result["items"]), 2)
        self.assertFalse(result["complete"])
        for transform in (lambda source: source.replace('data-offer="111"', 'data-offer="broken"'),
                          lambda source: source.replace('data-offer="111"', ''),
                          lambda source: source.replace('offer=111"', 'offer=111#fragment"'),
                          lambda source: source.replace('data-offer="111"', 'data-offer="111" data-node="999"'),
                          lambda source: source.replace('offer=111"', 'offer=111&amp;offer=222"'),
                          lambda source: source.replace('class="tc-item " data-offer="111"', 'class="new-row-type" data-offer="111"')):
            client, manager = self.setup_manager()
            client.trade_noise = False
            client.trade_transform = transform
            result = manager.list_offers()
            self.assertFalse(result["complete"], result)
            self.assertFalse(client.posts)

    def test_capacity_pagination_and_lazy_loading_are_incomplete(self):
        additions = ('<a rel="next" href="?page=2">Next</a>',
                     '<ul class="pagination"><li><a href="/lots/123/trade?cursor=next">2</a></li></ul>',
                     '<div data-has-more="true">Loading additional offers</div>')
        for addition in additions:
            client, manager = self.setup_manager()
            client.trade_noise = False
            client.trade_transform = lambda source, addition=addition: source.replace("</body>", addition + "</body>")
            result = manager.list_offers()
            self.assertFalse(result["complete"], result)

    def test_capacity_declared_total_must_match_unique_rows(self):
        client, manager = self.setup_manager()
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace('class="tc table-hover table-clickable"', 'class="tc table-hover table-clickable" data-total-offers="40"')
        result = manager.list_offers()
        self.assertFalse(result["complete"], result)
        self.assertEqual(len(result["items"]), 2)

    def test_capacity_authentication_is_still_required_for_empty_table(self):
        client, manager = self.setup_manager()
        client.members = ()
        client.trade_noise = False
        client.trade_user = 0
        with self.assertRaises(FunPayError) as raised:
            manager.list_offers()
        self.assertEqual(raised.exception.kind, "auth")

    def test_management_connection_without_new_editor_at_capacity(self):
        client, manager = self.setup_manager()
        client._template = None
        client.currency = ""
        client.members = tuple(str(number) for number in range(200, 240))
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace("</body>", '<a href="/lots/123/">Game Items</a></body>')
        result = manager.connection_metadata()
        self.assertEqual(result, {"currency": "RUB", "category": {"id": "123", "name": "Game Items"}, "count": 40})
        self.assertIsNone(client._template)
        self.assertEqual(client.edits, 0)
        self.assertFalse(client.posts)

    def test_management_empty_category_without_known_currency(self):
        client, manager = self.setup_manager()
        client._template = None
        client.currency = ""
        client.members = ()
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace("</body>", '<input type="hidden" name="node_id" value="123"></body>')
        result = manager.connection_metadata()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["currency"], "Account currency")
        self.assertEqual(result["category"]["id"], "123")
        self.assertEqual(client.edits, 0)
        self.assertIsNone(client._template)

    def test_management_category_proof_accepts_explicit_owned_rows(self):
        client, manager = self.setup_manager()
        client._template = None
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace('offer=111"', 'offer=111&amp;node=123"').replace('offer=222"', 'offer=222&amp;node=123"')
        self.assertEqual(manager.connection_metadata()["count"], 2)
        self.assertIsNone(client._template)
        self.assertEqual(client.edits, 0)

    def test_management_category_proof_rejects_unmatched_page(self):
        for addition in ("", '<a href="/lots/999/">Another category</a>', '<a href="https://evil.example/lots/123/">Spoof</a>', '<a href="/lots/123/#fragment">Fragment</a>'):
            client, manager = self.setup_manager()
            client._template = None
            client.trade_noise = False
            client.trade_transform = lambda source, addition=addition: source.replace("</body>", addition + "</body>")
            with self.assertRaises(FunPayError) as raised:
                manager.connection_metadata()
            self.assertEqual(raised.exception.kind, "form")
            self.assertFalse(client.posts)

    def test_management_connection_requires_complete_seller_table(self):
        client, manager = self.setup_manager()
        client._template = None
        client.trade_transform = lambda source: source.replace("</body>", '<a href="/lots/123/">Game Items</a></body>')
        with self.assertRaises(FunPayError) as raised:
            manager.connection_metadata()
        self.assertEqual(raised.exception.kind, "form")
        self.assertFalse(client.posts)

    def test_management_existing_editor_only_supplies_missing_currency(self):
        client, manager = self.setup_manager()
        client._template = None
        client.currency = ""
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace('<span class="unit">₽</span>', '').replace("</body>", '<a href="/lots/123/">Game Items</a></body>')
        result = manager.connection_metadata()
        self.assertEqual(result["currency"], "RUB")
        self.assertEqual(client.currency, "RUB")
        self.assertEqual(client.edits, 1)
        self.assertIsNone(client._template)
        self.assertEqual(set(result), {"currency", "category", "count"})
        serialized = json.dumps(result)
        for private in ("first:pass", "csrf", "secrets", "fields", "token-1", "fake-not-a-real-key"):
            self.assertNotIn(private, serialized)
        self.assertFalse(client.posts)

    def test_management_connection_rejects_inconsistent_trade_units(self):
        client, manager = self.setup_manager()
        client._template = None
        client.trade_noise = False
        client.trade_transform = lambda source: source.replace('<span class="unit">₽</span>', '<span class="unit">USD</span>', 1).replace("</body>", '<a href="/lots/123/">Game Items</a></body>')
        with self.assertRaises(FunPayError) as raised:
            manager.connection_metadata()
        self.assertEqual(raised.exception.kind, "currency")
        self.assertFalse(client.posts)

    def test_management_auth_flag_required_before_read(self):
        for authenticated in (False, None, 1):
            client, manager = self.setup_manager()
            client._template = None
            client._authenticated = authenticated
            with self.assertRaises(FunPayError) as raised:
                manager.connection_metadata()
            self.assertEqual(raised.exception.kind, "auth")
            self.assertFalse(client.calls)

    def test_management_edits_and_deletion_work_without_new_editor(self):
        client, manager = self.setup_manager()
        client._template = None
        loaded = manager.get_offer("111")
        manager.save_offer("111", {"active": False}, loaded["revision"])
        self.assertEqual(client.posts[-1]["secrets"], SECRET_STOCK)
        self.assertNotIn("active", client.posts[-1])
        loaded = manager.get_offer("111")
        manager.delete_offer("111", loaded["revision"])
        self.assertEqual(client.posts[-1]["deleted"], "1")
        self.assertIsNone(client._template)

    def test_management_unknown_currency_safe_read_status_but_no_price(self):
        client, manager = self.setup_manager(currency="Unknown unit")
        client._template = None
        client.currency = "Account currency"
        loaded = manager.get_offer("111")
        self.assertEqual(loaded["currency"], "Unknown unit")
        manager.save_offer("111", {"active": False}, loaded["revision"])
        before = len(client.posts)
        with self.assertRaises(FunPayError) as raised:
            manager.save_offer("111", {"price": "100"}, loaded["revision"])
        self.assertEqual(raised.exception.kind, "currency")
        self.assertEqual(len(client.posts), before)
        manager.delete_offer("111", loaded["revision"])
        self.assertEqual(client.posts[-1]["deleted"], "1")

    def test_cleanup_paused_listing_uses_one_delete_with_fresh_tokens(self):
        client, manager = self.setup_manager(active=False, missing_stock=True)
        client._template = None
        result = manager.cleanup_offer("111")
        self.assertEqual(result["state"], "deleted")
        self.assertEqual(result["cleanup_reason"], "paused")
        self.assertEqual(client.posts, [{"csrf_token": "token-1", "form_created_at": "created-token-1", "offer_id": "111", "node_id": "123", "deleted": "1"}])
        self.assertIsNone(client._template)
        self.assertEqual(client.edits, 1)

    def test_cleanup_explicit_zero_quantity_manual_and_automatic(self):
        for automatic in (False, True):
            client, manager = self.setup_manager(amount=0, stock="", active=True, auto=automatic)
            result = manager.cleanup_offer("111")
            self.assertEqual(result["state"], "deleted")
            self.assertEqual(result["cleanup_reason"], "zero quantity")
            self.assertEqual(len(client.posts), 1)

    def test_cleanup_active_stocked_or_unverified_state_is_kept(self):
        for options in ({}, {"active_control": False}, {"active": False, "disabled_active": True},
                        {"amount": None}, {"amount": "unknown", "active_control": False},
                        {"active_control": False, "missing_stock": True}, {"stock": ""}):
            client, manager = self.setup_manager(**options)
            result = manager.cleanup_offer("111")
            self.assertEqual(result["state"], "skipped", options)
            self.assertFalse(client.posts, options)

    def test_cleanup_trusted_metadata_paused_only_when_explicit_valid(self):
        for flag in (False, 0, "0", "false"):
            client, manager = self.setup_manager(active_control=False, data_values={"active": flag})
            self.assertEqual(manager.cleanup_offer("111")["state"], "deleted", flag)
            self.assertEqual(len(client.posts), 1)
        for flag in (True, 1, "true", "paused", "", None, [], {}):
            client, manager = self.setup_manager(active_control=False, data_values={"active": flag})
            self.assertEqual(manager.cleanup_offer("111")["state"], "skipped", flag)
            self.assertFalse(client.posts)

    def test_cleanup_contradictory_editor_evidence_is_kept(self):
        options_list = ({"active": True, "data_values": {"active": False}},
                        {"active": False, "data_values": {"active": True}},
                        {"amount": 2, "data_values": {"amount": 0}},
                        {"amount": 0, "data_values": {"amount": 2}})
        for options in options_list:
            client, manager = self.setup_manager(**options)
            self.assertEqual(manager.cleanup_offer("111")["state"], "skipped", options)
            self.assertFalse(client.posts)

    def test_cleanup_known_metadata_zero_without_amount_control(self):
        client, manager = self.setup_manager(amount=0, amount_control=False, active_control=False, missing_stock=True)
        result = manager.cleanup_offer("111")
        self.assertEqual(result["state"], "deleted")
        self.assertEqual(result["cleanup_reason"], "zero quantity")
        self.assertEqual(len(client.posts), 1)

    def test_cleanup_trade_paused_marker_cannot_delete_reactivated_offer(self):
        client, manager = self.setup_manager(active=True)
        summary = manager.list_offers()
        self.assertFalse(next(row for row in summary["items"] if row["id"] == "222")["active"])
        result = manager.cleanup_offer("222")
        self.assertEqual(result["state"], "skipped")
        self.assertFalse(client.posts)

    def test_cleanup_fresh_owner_category_and_membership_must_match(self):
        for options in ({"owner": 888}, {"node": "999"}, {"offer": "222"}):
            client, manager = self.setup_manager(active=False, **options)
            with self.assertRaises(FunPayError):
                manager.cleanup_offer("111")
            self.assertFalse(client.posts)
        client, manager = self.setup_manager(active=False)
        client.members = ("222",)
        with self.assertRaises(FunPayError) as raised:
            manager.cleanup_offer("111")
        self.assertEqual(raised.exception.kind, "unavailable")
        self.assertFalse(client.posts)

    def test_cleanup_uncertain_and_rejected_deletion_never_retry(self):
        client, manager = self.setup_manager(active=False)
        marker = FunPayError("Deletion may have succeeded", kind="network", uncertain=True)
        client.write_exception = marker
        with self.assertRaises(FunPayError) as raised:
            manager.cleanup_offer("111")
        self.assertIs(raised.exception, marker)
        self.assertEqual(len(client.posts), 1)
        client, manager = self.setup_manager(active=False)
        client.response = {"errors": [["deleted", "Cannot delete now"]]}
        with self.assertRaises(FunPayError) as raised:
            manager.cleanup_offer("111")
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(len(client.posts), 1)

    def test_cleanup_metadata_is_explicit_and_does_not_leak_private_values(self):
        for options, expected in (({"active": False}, "paused"), ({"amount": 0}, "zero quantity"),
                                  ({"active": False, "amount": 0}, "paused and zero quantity"),
                                  ({"active_control": False}, "")):
            client, manager = self.setup_manager(**options)
            result = manager.get_offer("111")
            self.assertEqual(result["cleanup_eligible"], bool(expected))
            self.assertEqual(result["cleanup_reason"], expected)
            serialized = json.dumps(result)
            for private in ("first:pass", "csrf_token", "fake-not-a-real-key", "token-1"):
                self.assertNotIn(private, serialized)
            self.assertFalse(client.posts)

    def test_cleanup_duplicate_status_or_quantity_controls_do_not_authorize(self):
        for options, extra in (({"active": False, "data_values": {"active": False}}, '<input type="checkbox" name="active" checked>'),
                               ({"amount": 0}, '<input name="amount" value="3">')):
            client, manager = self.setup_manager(**options)
            client.editor_transform = lambda source, extra=extra: source.replace("</form>", extra + "</form>")
            self.assertEqual(manager.cleanup_offer("111")["state"], "skipped")
            self.assertFalse(client.posts)

    def test_cleanup_action_matched_form_retains_verified_ownership_and_flags(self):
        client, manager = self.setup_manager(active=False)
        client.editor_transform = lambda source: source.replace('class="form-offer-editor"', 'class="new-editor-class"')
        self.assertEqual(manager.cleanup_offer("111")["state"], "deleted")
        self.assertEqual(len(client.posts), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
