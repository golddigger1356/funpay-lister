"""Manage only the connected seller's offers in the currently loaded category."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from pricing import PricingError, buyer_rub_amount

from funpay import (
    BASE_URL, CORE_NAMES, PROTECTED_NAMES, FunPayError, _Document, _app_data,
    _currency, _parse_form, _price, _purchase_message, _space, field_enabled,
    parse_category,
)


_VOLATILE = {"csrf_token", "csrf-token", "form_created_at", "form_updated_at",
             "created_at", "updated_at", "timestamp", "nonce", "_token"}
_TEXT_KEYS = {"title_ru": "fields[summary][ru]", "title_en": "fields[summary][en]",
              "description_ru": "fields[desc][ru]", "description_en": "fields[desc][en]"}
_EDIT_PATHS = {"/lots/offerEdit", "/en/lots/offerEdit", "/uk/lots/offerEdit"}
_PUBLIC_PATHS = {"/lots/offer", "/en/lots/offer", "/uk/lots/offer"}
_KNOWN_CURRENCIES = {"RUB", "USD", "EUR", "UAH"}


def _offer_id(value) -> str:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 9223372036854775807:
        raise FunPayError("Choose a valid listing from your loaded offers.", kind="validation")
    return str(int(text))


def _sensitive(name: str) -> bool:
    lowered = name.lower()
    return (_purchase_message(name) or name in PROTECTED_NAMES
            or any(word in lowered for word in ("secret", "goods", "password", "token")))


def _integer(value) -> int | None:
    text = re.sub(r"\s", "", str(value if value is not None else ""))
    return int(text) if text.isascii() and text.isdigit() else None


def _boolean(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in ("0", "1", "false", "true"):
        return value.strip().casefold() in ("1", "true")
    return None


class OfferManager:
    def __init__(self, client):
        self.client = client
        self.category_id = parse_category(client.category_id)
        self.account_id = str(client.user_id or "")
        self._members = {}
        self._last_trade_document = None
        self._trade_category_name = ""

    def _context(self):
        if getattr(self.client, "_authenticated", False) is not True or not self.client._golden_key:
            raise FunPayError("Connect your FunPay account and category first.", kind="auth")
        if (str(self.client.category_id) != self.category_id
                or str(self.client.user_id or "") != self.account_id):
            raise FunPayError("The connected account or category changed. Reload your offers.", kind="conflict")

    def _authenticated(self, document):
        app = _app_data(document)
        raw_id = app.get("userId")
        current_id = _integer(raw_id)
        username = next((_space(node.text()) for node in document.root.walk()
                         if node.has_class("user-link-name")), "")
        if raw_id is not None:
            if not current_id:
                raise FunPayError("Your FunPay session has expired. Reconnect before managing offers.", kind="auth")
            if self.account_id and str(current_id) != self.account_id:
                raise FunPayError("FunPay returned a different account. Reconnect before managing offers.", kind="auth")
        elif not username or username != self.client.account:
            raise FunPayError("FunPay did not return your authenticated seller page. Reconnect before managing offers.", kind="auth")
        token = app.get("csrf-token") or app.get("csrfToken")
        if token:
            self.client._app_csrf = str(token)

    def _category(self):
        template = self.client._template or {}
        return {"id": self.category_id, "name": template.get("category_name") or self._trade_category_name or "Category " + self.category_id}

    def connection_metadata(self) -> dict:
        """Read-only account/category connection when creating offers is gated.

        Existing-offer forms may supply currency metadata, but are never stored
        as a new-offer template or exposed with their tokens, goods, or messages.
        """
        snapshot = self.list_offers()
        if snapshot.get("complete") is not True or self._last_trade_document is None:
            raise FunPayError(snapshot.get("count_error") or "The complete seller listing page could not be verified.", kind="form")
        nodes = list(self._last_trade_document.root.walk())
        proof = False
        category_name = ""
        explicit_rows = set()
        currencies = set()
        for node in nodes:
            if node.tag == "input" and (node.attrs.get("type") or "").lower() == "hidden":
                if node.attrs.get("name") in ("node_id", "node") and str(node.attrs.get("value") or "") == self.category_id:
                    proof = True
            if node.tag != "a" or not node.attrs.get("href"):
                continue
            target = urlsplit(urljoin(BASE_URL, node.attrs["href"]))
            if target.scheme != "https" or target.netloc.lower() != "funpay.com" or target.fragment:
                continue
            params = parse_qs(target.query)
            if re.fullmatch(r"/(?:en/|uk/)?lots/" + self.category_id + r"/(?:trade/?)?", target.path):
                if not target.query:
                    proof = True
                    text = _space(node.text())
                    if text and (not category_name or not target.path.rstrip("/").endswith("trade")):
                        category_name = text[:160]
            if target.path in _EDIT_PATHS and len(params.get("node", [])) == 1 and params["node"][0] == self.category_id:
                # A same-category create link is also an explicit category ID.
                offer = (params.get("offer") or [""])[0]
                if offer in ("", "0"):
                    proof = True
            if not node.has_class("tc-item"):
                continue
            offer = (params.get("offer") or [""])[0]
            if offer not in self._members:
                continue
            explicit_node = (params.get("node") or [node.attrs.get("data-node") or node.attrs.get("data-node-id") or ""])[0]
            if str(explicit_node) == self.category_id:
                explicit_rows.add(offer)
            price = next((child for child in node.walk() if child.has_class("tc-price")), None)
            if price:
                for child in price.walk():
                    if child.has_class("unit"):
                        currency = _currency(_space(child.text()))
                        if currency in _KNOWN_CURRENCIES:
                            currencies.add(currency)
        proof = proof or bool(self._members) and set(self._members).issubset(explicit_rows)
        if not proof:
            raise FunPayError("FunPay's seller page could not be matched to this category. Open the category on FunPay and reconnect.", kind="form")
        if len(currencies) > 1:
            raise FunPayError("FunPay returned inconsistent seller currencies. Reconnect before managing prices.", kind="currency")
        currency = next(iter(currencies), "")
        if not currency and snapshot["items"]:
            # Read only an owned existing editor, with exact offer/node checks.
            _, existing = self._load(snapshot["items"][0]["id"])
            if existing["currency"] in _KNOWN_CURRENCIES:
                currency = existing["currency"]
        if not currency and self.client.currency in _KNOWN_CURRENCIES:
            currency = self.client.currency
        self._trade_category_name = category_name or self._trade_category_name
        return {"currency": currency or "Account currency", "category": self._category(), "count": len(snapshot["items"])}

    def _trade_completeness(self, source, document, accepted_rows, row_issues, observed_count):
        """A partial/changed seller page must never be treated as spare capacity."""
        nodes = list(document.root.walk())
        tables = [node for node in nodes if node.has_class("tc")]
        if not tables:
            return False, "FunPay did not return the expected seller offer table."
        if not re.search(r"</(?:body|html)\s*>", source, re.IGNORECASE):
            return False, "FunPay returned an incomplete seller page."
        for table in tables:
            for name in ("data-offer-count", "data-total-offers", "data-total-count"):
                if name in table.attrs and _integer(table.attrs[name]) != observed_count:
                    return False, "The seller table's total does not match the offers returned."
        if row_issues:
            return False, "Some seller offer rows did not match the connected category or their offer IDs."
        for node in nodes:
            if node.has_class("tc-item") and id(node) not in accepted_rows:
                return False, "Some seller offer rows could not be counted safely."
            if node.tag in ("h1", "p") and (node.has_class("lead") or node.has_class("page-header")):
                text = _space(node.text()).casefold()
                if any(marker in text for marker in ("доступ запрещ", "access denied", "доступ заборон", "page not found", "страница не найдена", "сторінку не знайдено")):
                    return False, "FunPay did not return an available seller offer page."
        current_path = "/lots/" + self.category_id + "/trade"
        trade_paths = {current_path, "/en" + current_path, "/uk" + current_path}
        for node in nodes:
            if node.tag == "a" and node.attrs.get("href"):
                target = urlsplit(urljoin(BASE_URL + current_path, node.attrs["href"]))
                if target.netloc.lower() == "funpay.com" and target.path.rstrip("/") in trade_paths:
                    params = parse_qs(target.query)
                    rel = (node.attrs.get("rel") or "").casefold().split()
                    if "next" in rel or any(name in params for name in ("page", "cursor", "start_from", "offset")):
                        return False, "FunPay returned pagination; the complete listing count is unavailable."
            if any(node.attrs.get(name) not in (None, "", "0", "false", "False")
                   for name in ("data-next-page", "data-next-cursor", "data-has-more")):
                return False, "FunPay indicated that additional seller offers were not included."
            if node.attrs.get("data-offer") is not None and not node.has_class("tc-item"):
                current = node.parent
                while current is not None:
                    if any(current is table for table in tables):
                        return False, "FunPay returned an unrecognized seller offer row."
                    current = current.parent
        return True, ""

    def list_offers(self) -> dict:
        self._context()
        source = self.client._request("/lots/" + self.category_id + "/trade")
        document = _Document(source)
        self._authenticated(document)
        self._last_trade_document = document
        items = []
        members = {}
        accepted_rows = set()
        row_issues = False
        for node in document.root.walk():
            if node.tag != "a" or not node.has_class("tc-item"):
                continue
            target = urlsplit(urljoin(BASE_URL, node.attrs.get("href") or ""))
            if target.scheme != "https" or target.netloc.lower() != "funpay.com" or target.path not in _EDIT_PATHS:
                continue
            params = parse_qs(target.query)
            offer = (params.get("offer") or [""])[0]
            if not offer.isascii() or not offer.isdigit() or int(offer) <= 0:
                continue
            offer = str(int(offer))
            marker = str(node.attrs.get("data-offer") or "")
            if marker and marker != offer:
                continue
            node_id = (params.get("node") or [self.category_id])[0]
            if str(node_id) != self.category_id:
                continue
            if (not marker or target.fragment or len(params.get("offer", [])) != 1
                    or len(params.get("node", [])) > 1
                    or any(str(node.attrs[name]) != self.category_id
                           for name in ("data-node", "data-node-id") if name in node.attrs)):
                row_issues = True
            else:
                accepted_rows.add(id(node))
            if offer in members:
                continue
            title = next((_space(child.text()) for child in node.walk() if child.has_class("tc-desc-text")), "")
            price_node = next((child for child in node.walk() if child.has_class("tc-price")), None)
            price = str(price_node.attrs.get("data-s") or "") if price_node else ""
            unit = next((_space(child.text()) for child in price_node.walk() if child.has_class("unit")), "") if price_node else ""
            currency = _currency(unit) if unit else self.client.currency
            amount_text = next((_space(child.text()) for child in node.walk() if child.has_class("tc-amount")), "")
            item = {"id": offer, "title_ru": title, "title_en": "", "description": title,
                    "seller_price": price, "currency": currency,
                    "active": not node.has_class("warning"), "amount": _integer(amount_text),
                    "url": BASE_URL + "/lots/offer?id=" + offer}
            items.append(item)
            members[offer] = item
        self._members = members
        complete, count_error = self._trade_completeness(source, document, accepted_rows, row_issues, len(items))
        return {"items": items, "category": self._category(), "currency": self.client.currency,
                "complete": complete, "count_error": count_error}

    def _load(self, offer) -> tuple[str, dict]:
        offer = _offer_id(offer)
        self.list_offers()  # Recheck current ownership/category before every edit.
        if offer not in self._members:
            raise FunPayError("This listing is no longer in your connected account and category. Refresh your offers.", kind="unavailable")
        source = self.client._request("/lots/offerEdit?offer=" + offer + "&node=" + self.category_id)
        document = _Document(source)
        self._authenticated(document)
        template = _parse_form(source, self.category_id, expected_offer_id=offer,
                               preserve_purchase_messages=True)
        if self.client.currency in _KNOWN_CURRENCIES and template["currency"] != self.client.currency:
            raise FunPayError("Your FunPay price currency changed. Reconnect and review prices before editing.", kind="currency")
        if self.client.currency not in _KNOWN_CURRENCIES and template["currency"] in _KNOWN_CURRENCIES:
            self.client.currency = template["currency"]
        form = next((node for node in template["document"].root.walk()
                     if node.tag == "form" and node.has_class("form-offer-editor")), None)
        if form is None:
            for node in template["document"].root.walk():
                action = urlsplit(urljoin(BASE_URL, node.attrs.get("action") or ""))
                if (node.tag == "form" and action.scheme == "https" and action.netloc.lower() == "funpay.com"
                        and action.path in ("/lots/offerSave", "/en/lots/offerSave", "/uk/lots/offerSave")):
                    form = node
                    break
        data = {}
        if form and form.attrs.get("data-offer"):
            try:
                data = json.loads(form.attrs["data-offer"])
            except (ValueError, TypeError):
                raise FunPayError("FunPay returned unreadable offer data. Refresh before editing.", kind="form") from None
            if not isinstance(data, dict):
                raise FunPayError("FunPay returned unexpected offer data. Refresh before editing.", kind="form")
        for key in ("id", "offer_id", "offerId"):
            if key in data and str(data[key]) != offer:
                raise FunPayError("The returned offer metadata does not match this listing. Refresh before editing.", kind="form")
        for key in ("node_id", "nodeId"):
            if key in data and str(data[key]) != self.category_id:
                raise FunPayError("The returned offer metadata belongs to a different category.", kind="form")
        for key in ("user_id", "userId", "seller_id", "sellerId"):
            if key in data and self.account_id and str(data[key]) != self.account_id:
                raise FunPayError("The offer editor does not belong to your connected account.", kind="unavailable")
        template["offer_data"] = data
        template["offer_form"] = form
        template["public_fields"] = [dict(control) for control in template["fields"]
                                     if not _sensitive(control["name"])]
        if self._automatic(template):
            template["public_fields"] = [control for control in template["public_fields"]
                                         if control["name"] != "amount"]
        return offer, template

    @staticmethod
    def _automatic(template) -> bool:
        return bool(template["values"].get("auto_delivery"))

    def _revision(self, offer: str, template: dict) -> str:
        stable = {name: value for name, value in template["values"].items() if name not in _VOLATILE}
        snapshot = {"offer": offer, "node": self.category_id, "currency": template["currency"],
                    "values": stable, "stock_amount": template["offer_data"].get("amount")}
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _metadata(self, offer: str, template: dict) -> dict:
        values = template["values"]
        amount = _integer(template["offer_data"].get("amount"))
        if amount is None:
            amount = _integer(values.get("amount"))
        if amount is None and self._automatic(template) and "secrets" in values:
            amount = len([line for line in values["secrets"].splitlines() if line.strip()])
        active = bool(values.get("active")) and amount != 0
        cleanup_reason = self._cleanup_reason(template)
        return {"id": offer, **{key: values.get(name, "") for key, name in _TEXT_KEYS.items()},
                "seller_price": values.get("price", ""), "currency": template["currency"],
                "active": active, "amount": amount, "auto_delivery": self._automatic(template),
                "stock_editable": not self._automatic(template) and any(control["name"] == "amount" for control in template["public_fields"]),
                "cleanup_eligible": bool(cleanup_reason), "cleanup_reason": cleanup_reason,
                "fields": [control for control in template["public_fields"] if control["name"] != "amount"],
                "constraints": template["constraints"],
                "revision": self._revision(offer, template),
                "url": BASE_URL + "/lots/offer?id=" + offer}

    def get_offer(self, offer) -> dict:
        offer, template = self._load(offer)
        return self._metadata(offer, template)

    @staticmethod
    def _cleanup_reason(template: dict) -> str:
        """Require explicit editor evidence; missing controls never mean paused."""
        form = template.get("offer_form")
        nodes = list(form.walk()) if form is not None else []

        def enabled(node):
            current = node
            while current is not None and current is not form:
                if "disabled" in current.attrs:
                    return False
                current = current.parent
            return True

        statuses = []
        active_controls = [node for node in nodes if node.attrs.get("name") == "active"]
        if (len(active_controls) == 1 and active_controls[0].tag == "input"
                and (active_controls[0].attrs.get("type") or "text").lower() == "checkbox"
                and enabled(active_controls[0])):
            statuses.append("checked" in active_controls[0].attrs)
        data = template["offer_data"]
        if "active" in data:
            data_active = _boolean(data["active"])
            if data_active is not None:
                statuses.append(data_active)
        # Contradictory status evidence cannot authorize automatic deletion.
        paused = len(active_controls) <= 1 and bool(statuses) and all(status is False for status in statuses)

        amounts = []
        if "amount" in data:
            amount = _integer(data["amount"])
            if amount is not None:
                amounts.append(amount)
        amount_controls = [node for node in nodes if node.attrs.get("name") == "amount"]
        if (len(amount_controls) == 1 and amount_controls[0].tag == "input"
                and (amount_controls[0].attrs.get("type") or "text").lower() in ("text", "number", "hidden")
                and enabled(amount_controls[0])):
            amount = _integer(amount_controls[0].attrs.get("value"))
            if amount is not None:
                amounts.append(amount)
        # No stock inference from an omitted/empty secrets textarea: FunPay may
        # withhold automatic-delivery goods even for a stocked listing.
        empty = len(amount_controls) <= 1 and bool(amounts) and all(amount == 0 for amount in amounts)
        return "paused and zero quantity" if paused and empty else "paused" if paused else "zero quantity" if empty else ""

    def cleanup_offer(self, offer) -> dict:
        """Delete an owned paused/empty offer only after rechecking its editor."""
        offer, template = self._load(offer)
        reason = self._cleanup_reason(template)
        if not reason:
            return {"state": "skipped", "id": offer,
                    "message": "Kept listing: its paused or zero-quantity state could not be verified."}
        result = self._delete_loaded(offer, template)
        return {**result, "state": "deleted", "cleanup_reason": reason}

    @staticmethod
    def _expected(expected_revision):
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise FunPayError("Reload this listing before saving or deleting it.", kind="conflict")

    def _check_revision(self, offer, template, expected_revision):
        if not hmac.compare_digest(self._revision(offer, template), expected_revision):
            raise FunPayError("This listing changed on FunPay after you loaded it. Reload it to keep the latest changes.", kind="conflict")

    def _preserve_stock(self, template):
        if not self._automatic(template):
            return
        values = template["values"]
        amount = _integer(template["offer_data"].get("amount"))
        lines = [line for line in values.get("secrets", "").splitlines() if line.strip()]
        if "secrets" not in values or amount is None or len(lines) != amount:
            raise FunPayError("FunPay did not expose enough automatic-delivery stock data to preserve it safely. Edit this listing on FunPay.", kind="stock")

    def _post_result(self, raw, offer: str, *, deleting: bool = False) -> dict:
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise FunPayError("FunPay did not confirm the change. Refresh your offers before trying it again.", kind="response", uncertain=True) from None
        error = self.client._response_error(result)
        if error:
            lowered = error.lower()
            kind = "validation"
            for marker, terms in (("rate", ("слишком часто", "too often", "too many", "подождите", "зачекайте")),
                                  ("auth", ("авторизац", "unauthorized", "authentication", "войдите")),
                                  ("access", ("доступ запрещ", "access denied", "доступ заборон")),
                                  ("form", ("обновите страницу", "refresh the page", "reload the page", "оновіть сторінку"))):
                if any(term in lowered for term in terms):
                    kind = marker
                    break
            raise FunPayError(error, kind=kind)
        expected_url = False
        if isinstance(result, dict) and isinstance(result.get("url"), str):
            parsed = urlsplit(urljoin(BASE_URL, result["url"]))
            if parsed.scheme == "https" and parsed.netloc.lower() == "funpay.com":
                params = parse_qs(parsed.query)
                returned_id = (params.get("offer") or params.get("id") or [""])[0]
                expected_url = (not deleting and parsed.path in _EDIT_PATHS | _PUBLIC_PATHS and returned_id == offer)
                if deleting and not parsed.query:
                    expected_url = bool(re.fullmatch(r"/(?:en/|uk/)?lots/" + self.category_id + r"/trade/?", parsed.path))
        confirmed = isinstance(result, dict) and (
            result.get("done") is True or result.get("done") == 1
            or ("done" not in result and expected_url)
        )
        if not confirmed:
            raise FunPayError("FunPay returned an unrecognized result. Refresh your offers before trying the change again.", kind="response", uncertain=True)
        return {"id": offer, "message": "Listing deleted from FunPay." if deleting else "Listing updated on FunPay.",
                "url": BASE_URL + "/lots/" + self.category_id + "/trade" if deleting else BASE_URL + "/lots/offer?id=" + offer}

    def save_offer(self, offer, changes: dict, expected_revision: str) -> dict:
        self._expected(expected_revision)
        if not isinstance(changes, dict) or not changes:
            raise FunPayError("Choose a listing change before saving.")
        permitted = set(_TEXT_KEYS) | {"seller_price", "price", "active", "amount", "fields", "buyer_target_rub", "buyer_price_rub"}
        if set(changes) - permitted:
            raise FunPayError("The requested listing update contains unsupported fields.")
        offer, template = self._load(offer)
        self._check_revision(offer, template, expected_revision)
        if ("seller_price" in changes or "price" in changes or changes.get("buyer_target_rub") is not None) and template["currency"] not in _KNOWN_CURRENCIES:
            raise FunPayError("FunPay's seller currency could not be identified. Reconnect before changing prices.", kind="currency")
        self._preserve_stock(template)
        payload = dict(template["values"])
        for key, name in _TEXT_KEYS.items():
            if key not in changes:
                continue
            if not isinstance(changes[key], str):
                raise FunPayError("Listing titles and descriptions must be text.")
            value = changes[key]
            if key.startswith("title_") and not value.strip():
                raise FunPayError("A listing title cannot be empty.")
            limit = template["constraints"].get(name, {}).get("maxlength")
            if limit and len(value) > int(limit):
                raise FunPayError("The listing " + key.replace("_", " ") + " is too long.")
            payload[name] = value
        if "seller_price" in changes and "price" in changes and _price(changes["seller_price"]) != _price(changes["price"]):
            raise FunPayError("The listing update includes conflicting prices.")
        if "seller_price" in changes or "price" in changes:
            payload["price"] = _price(changes.get("seller_price", changes.get("price")))
        if "active" in changes:
            if not isinstance(changes["active"], bool):
                raise FunPayError("The listing status must be active or inactive.")
            if changes["active"]:
                payload["active"] = "on"
            else:
                payload.pop("active", None)
        overrides = changes.get("fields", {})
        if not isinstance(overrides, dict):
            raise FunPayError("Category field changes must be a field-value mapping.")
        overrides = dict(overrides)
        if "amount" in changes:
            if self._automatic(template):
                raise FunPayError("Automatic-delivery stock cannot be edited in this tool.", kind="stock")
            quantity = _integer(changes["amount"])
            if quantity is None or not 1 <= quantity <= 1000000000:
                raise FunPayError("The listing quantity must be a positive whole number.")
            overrides["amount"] = str(quantity)
        schema = {control["name"]: control for control in template["public_fields"]}
        for name, value in overrides.items():
            if name not in schema or name in CORE_NAMES | PROTECTED_NAMES or _sensitive(name):
                raise FunPayError("Only the loaded listing's editable category fields can be changed.", kind="form")
            control = schema[name]
            if control["type"] == "checkbox":
                if value in (True, "on", "1", 1, control.get("checked_value") or "on"):
                    payload[name] = control.get("checked_value") or "on"
                else:
                    payload.pop(name, None)
                continue
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise FunPayError("Category field values must be text or numbers.")
            value = str(value)
            if control["type"] == "select" and value not in {option["value"] for option in control["options"]}:
                raise FunPayError("Choose a valid value for " + control["label"] + ".")
            if control.get("maxlength") and len(value) > int(control["maxlength"]):
                raise FunPayError(control["label"] + " is too long.")
            if name == "amount" and (_integer(value) is None or not 1 <= _integer(value) <= 1000000000):
                raise FunPayError("The listing quantity must be a positive whole number.")
            payload[name] = value
        for name, control in schema.items():
            if not field_enabled(control, payload):
                payload.pop(name, None)
            elif overrides and control["required"] and not str(payload.get(name, "")).strip():
                raise FunPayError("Complete the required category field: " + control["label"] + ".")
        payload["offer_id"] = offer
        payload["node_id"] = self.category_id
        payload.pop("deleted", None)
        if changes.get("buyer_target_rub") is not None:
            try:
                approved = Decimal(str(changes["buyer_price_rub"]))
                if not approved.is_finite() or approved <= 0:
                    raise InvalidOperation()
                current_buyer = buyer_rub_amount(self.client._calc(payload["price"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                raise FunPayError("The reviewed buyer price is missing or invalid. Preview the listing price again.", kind="pricing") from None
            except PricingError as error:
                raise FunPayError(str(error), kind="pricing") from None
            if current_buyer != approved:
                self.client._buyer_quote_cache.clear()
                raise FunPayError("FunPay's buyer price changed since your preview. Review the buyer price again before saving.", kind="pricing")
        raw = self.client._request("/lots/offerSave", payload)
        return self._post_result(raw, offer)

    def delete_offer(self, offer, expected_revision: str) -> dict:
        self._expected(expected_revision)
        offer, template = self._load(offer)
        self._check_revision(offer, template, expected_revision)
        return self._delete_loaded(offer, template)

    def _delete_loaded(self, offer: str, template: dict) -> dict:
        # FunPayAPI's delete_lot uses deleted="1" at the normal save endpoint.
        # Send only the identity/fresh tokens and the explicit deletion flag.
        payload = {name: template["values"][name] for name in ("csrf_token", "form_created_at")
                   if name in template["values"]}
        payload.update({"offer_id": offer, "node_id": self.category_id, "deleted": "1"})
        result = self._post_result(self.client._request("/lots/offerSave", payload), offer, deleting=True)
        self._members.pop(offer, None)
        return result
