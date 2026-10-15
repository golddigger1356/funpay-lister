"""Small, dependency-free client for FunPay's seller offer form.

Only talks to https://funpay.com. Credentials and session cookies stay in memory.
FunPay does not publish a stable listing API, so forms are fetched afresh before
every save. In particular, creating an offer is never retried automatically.
"""

from __future__ import annotations

import html
import http.client
import http.cookiejar
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from pricing import PricingError, buyer_rub_amount, quote_buyer_rub
from limits import LISTING_LIMIT


BASE_URL = "https://funpay.com"
MAX_RESPONSE = 3 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CORE_NAMES = {
    "fields[summary][ru]", "fields[summary][en]",
    "fields[desc][ru]", "fields[desc][en]", "price", "active",
}
PROTECTED_NAMES = {
    "csrf_token", "form_created_at", "node_id", "offer_id", "deleted",
    "auto_delivery", "secrets", "fields[images]",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class FunPayError(Exception):
    """A safe, user-facing failure; uncertain means a save may have succeeded."""

    def __init__(self, message: str, *, kind: str = "validation", uncertain: bool = False):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.uncertain = uncertain


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    parent: "_Node | None" = None
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self) -> str:
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children)

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()


class _Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _space(value: str) -> str:
    return " ".join(value.split())


def parse_category(category) -> str:
    """Accept a node ID or an ordinary FunPay /lots/123/ category URL."""
    value = str(category or "").strip()
    if value.isascii() and value.isdigit():
        number = int(value)
    else:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc.lower() != "funpay.com":
            raise FunPayError("Enter a category ID or a https://funpay.com/lots/123/ category link.")
        match = re.fullmatch(r"/(?:en/|uk/)?lots/(\d+)/(?:trade/?)?", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise FunPayError("Use the ID from a /lots/123/ category, not an individual offer or currency page.")
        number = int(match.group(1))
    if not 1 <= number <= 2147483647:
        raise FunPayError("The category ID must be a positive number.")
    return str(number)


def _price(value) -> str:
    text = str(value or "").strip().replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,8})?", text):
        raise FunPayError("Enter a positive numeric price, for example 125 or 125.50.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise FunPayError("The price is invalid.") from None
    if not number.is_finite() or number <= 0 or number > Decimal("1000000000"):
        raise FunPayError("The price must be greater than zero and no more than 1,000,000,000.")
    return format(number, "f")


class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(urljoin(req.full_url, newurl))
        if req.get_method() != "GET" or target.scheme != "https" or target.netloc.lower() != "funpay.com":
            raise HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _app_data(document: _Document) -> dict:
    for node in document.root.walk():
        if "data-app-data" in node.attrs:
            try:
                value = json.loads(node.attrs["data-app-data"] or "{}")
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
    return {}


def _group(node: _Node) -> _Node:
    current = node.parent
    while current is not None and current.tag != "form":
        if current.has_class("form-group"):
            return current
        current = current.parent
    return node.parent or node


def _hidden(node: _Node, form: _Node) -> bool:
    current = node
    while current is not None and current is not form:
        style = (current.attrs.get("style") or "").replace(" ", "").lower()
        if (current.has_class("hidden") or "hidden" in current.attrs
                or "display:none" in style or "visibility:hidden" in style):
            return True
        current = current.parent
    return False


def _label(node: _Node, labels: dict, name: str) -> str:
    explicit = labels.get(node.attrs.get("id"))
    group = _group(node)
    label = explicit or next((n for n in group.walk() if n.tag == "label"), None)
    value = _space(label.text()).strip(" *:") if label else ""
    if not value:
        value = node.attrs.get("placeholder") or node.attrs.get("aria-label") or {
            "fields[method]": "Способ получения", "fields[type]": "Тип товара"
        }.get(name, name)
    if "[payment_msg]" in name:
        value += " (English)" if name.endswith("[en]") else " (Russian)"
    return value[:200]


def field_enabled(control: dict, values: dict) -> bool:
    """FunPay's lot fields can depend on another category choice."""
    return all(str(values.get(condition["name"], "")).strip().casefold() in
               {str(value).strip().casefold() for value in condition["values"]}
               for condition in control.get("conditions", []))


def _purchase_message(name: str) -> bool:
    return name == "payment_msg" or name.startswith("fields[payment_msg]")


def _field_rules(form: _Node) -> dict:
    rules = {}
    for container in form.walk():
        source = container.attrs.get("data-fields")
        if not source:
            continue
        try:
            definitions = json.loads(source)
        except (ValueError, TypeError):
            continue
        if not isinstance(definitions, list):
            continue
        for definition in definitions:
            if not isinstance(definition, dict) or not isinstance(definition.get("id"), str):
                continue
            conditions = []
            for condition in definition.get("conditions", []):
                if isinstance(condition, dict) and isinstance(condition.get("id"), str) and isinstance(condition.get("list"), list):
                    conditions.append({"name": "fields[" + condition["id"] + "]", "values": condition["list"]})
            rules["fields[" + definition["id"] + "]"] = {"conditions": conditions, "type": definition.get("type")}
    return rules


def _parse_form(source: str, category_id: str, expected_offer_id: str | None = None,
                preserve_purchase_messages: bool = False) -> dict:
    document = _Document(source)
    forms = [n for n in document.root.walk() if n.tag == "form"]
    editors = [n for n in forms if n.has_class("form-offer-editor")]
    if not editors:
        for candidate in forms:
            action = urlsplit(urljoin(BASE_URL, candidate.attrs.get("action") or ""))
            if (action.scheme == "https" and action.netloc.lower() == "funpay.com"
                    and action.path in ("/lots/offerSave", "/en/lots/offerSave", "/uk/lots/offerSave")):
                editors.append(candidate)
    if len(editors) > 1:
        raise FunPayError("FunPay returned more than one listing editor. Reload the category before making changes.", kind="form")
    form = editors[0] if editors else None
    if form is None:
        text = _space(document.root.text())
        if any(s in text.lower() for s in ("доступ запрещ", "access denied", "доступ заборон")):
            raise FunPayError("FunPay denied access to this category. Open it on FunPay and complete any seller requirements.", kind="access")
        raise FunPayError("FunPay did not provide a new-listing editor for this category.", kind="editor_missing")
    labels = {n.attrs.get("for"): n for n in form.walk() if n.tag == "label" and n.attrs.get("for")}
    values, controls = {}, {}
    rules = _field_rules(form)
    for node in form.walk():
        name = node.attrs.get("name")
        if node.tag not in ("input", "select", "textarea") or not name:
            continue
        if _purchase_message(name) and not preserve_purchase_messages:
            values[name] = ""
            continue
        kind = (node.attrs.get("type") or "text").lower() if node.tag == "input" else node.tag
        conditional_category = name in rules and name not in CORE_NAMES | PROTECTED_NAMES
        editable_hidden_select = kind == "select" and name.startswith("fields[") and name not in CORE_NAMES | PROTECTED_NAMES
        if "disabled" in node.attrs and not conditional_category and not editable_hidden_select:
            continue
        if expected_offer_id is not None and "disabled" in node.attrs and not conditional_category:
            continue
        if kind in ("submit", "button", "reset", "file", "image"):
            continue
        options = []
        radio_box = node.parent if node.parent and node.parent.has_class("lot-field-radio-box") else None
        buttons = [button for button in radio_box.walk() if button.tag == "button" and "value" in button.attrs] if radio_box else []
        if kind == "textarea":
            value = node.text()
        elif kind == "select":
            if "multiple" in node.attrs:
                raise FunPayError("This category uses a multiple-choice editor that this version cannot post safely.", kind="form")
            option_nodes = [n for n in node.walk() if n.tag == "option"]
            for option in option_nodes:
                options.append({"value": option.attrs.get("value") if "value" in option.attrs else _space(option.text()), "label": _space(option.text())})
            selected = next((o for o in option_nodes if "selected" in o.attrs), option_nodes[0] if option_nodes else None)
            value = (selected.attrs.get("value") if "value" in selected.attrs else _space(selected.text())) if selected else ""
        else:
            value = node.attrs.get("value") or ("on" if kind in ("checkbox", "radio") else "")
        if buttons and name.startswith("fields[") and name not in CORE_NAMES | PROTECTED_NAMES:
            kind = "select"
            options = [{"value": button.attrs["value"], "label": _space(button.text())} for button in buttons]
        checked = "checked" in node.attrs
        if kind not in ("checkbox", "radio") or checked:
            values[name] = value
        if kind == "hidden" or name in CORE_NAMES | PROTECTED_NAMES or _purchase_message(name):
            continue
        if _hidden(node, form) and not (conditional_category or editable_hidden_select or buttons):
            continue
        required = ("required" in node.attrs or node.has_class("required")
                    or _group(node).has_class("required")
                    or any(n.has_class("required") for n in _group(node).walk() if n.tag == "label"))
        # Category selectors in FunPay's lot schema are required when enabled.
        # The HTML often omits `required`; FunPay validates these server-side.
        required = required or name == "fields[method]" or (conditional_category and rules[name]["type"] in (4, 5))
        if kind == "radio":
            control = controls.setdefault(name, {"name": name, "label": _label(node, labels, name), "type": "select", "required": required, "value": "", "options": []})
            control["options"].append({"value": value, "label": _label(node, labels, name)})
            if checked:
                control["value"] = value
            continue
        controls[name] = {
            "name": name, "label": _label(node, labels, name), "type": kind,
            "required": required, "value": (value if checked else "") if kind == "checkbox" else value,
            "options": options,
        }
        if conditional_category:
            controls[name]["conditions"] = rules[name]["conditions"]
        if kind == "checkbox":
            controls[name]["checked_value"] = value
        for attr in ("maxlength", "min", "max", "step"):
            if node.attrs.get(attr):
                controls[name][attr] = node.attrs[attr]
    if not values.get("csrf_token") or str(values.get("node_id")) != category_id:
        raise FunPayError("The FunPay editor returned missing tokens or a different category. Reconnect before publishing.", kind="form")
    for name in CORE_NAMES - {"active"}:
        if name not in values:
            raise FunPayError("This category does not use the expected bilingual item editor. Open it on FunPay to check the offer format.", kind="form")
    if expected_offer_id is None:
        if values.get("offer_id") not in (None, "", "0"):
            raise FunPayError("FunPay returned an existing offer instead of a new-offer form. Nothing was changed.", kind="form")
    elif str(values.get("offer_id") or "") != str(expected_offer_id):
        raise FunPayError("FunPay returned a different offer. Refresh your listings before editing.", kind="form")
    if expected_offer_id is not None:
        for name, control in controls.items():
            if not field_enabled(control, values):
                values.pop(name, None)
    price_node = next((n for n in form.walk() if n.attrs.get("name") == "price"), None)
    feedback = []
    if price_node:
        feedback = [n for n in _group(price_node).walk() if n.has_class("form-control-feedback") or n.has_class("unit")]
    currency_text = " ".join(_space(n.text()) for n in feedback)
    currency = _currency(currency_text)
    category_name = "Category " + category_id
    for node in document.root.walk():
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        target = urlsplit(urljoin(BASE_URL, node.attrs["href"]))
        if re.fullmatch(r"/(?:en/|uk/)?lots/" + category_id + r"/(?:trade/?)?", target.path):
            candidate = _space(node.text())
            if candidate:
                category_name = candidate[:160]
                break
    constraints = {}
    for node in form.walk():
        if node.attrs.get("name") in CORE_NAMES and node.attrs.get("maxlength"):
            constraints[node.attrs["name"]] = {"maxlength": node.attrs["maxlength"]}
    return {"document": document, "values": values, "fields": list(controls.values()),
            "currency": currency, "category_name": category_name, "constraints": constraints}


def _currency(value: str) -> str:
    upper = value.upper()
    for code, markers in (("RUB", ("RUB", "₽", "РУБ", "РУБ.")), ("USD", ("USD", "$")), ("EUR", ("EUR", "€")), ("UAH", ("UAH", "₴", "ГРН"))):
        if any(marker in upper for marker in markers):
            return code
    return _space(value)[:30] or "Account currency"


class FunPayClient:
    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.category_id = ""
        self.account = ""
        self.user_id = None
        self.currency = ""
        self._golden_key = ""
        self._authenticated = False
        self._app_csrf = ""
        self._template = None
        self._buyer_quote_cache = {}
        self._jar = http.cookiejar.CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar), _SafeRedirects(), HTTPSHandler(context=ssl.create_default_context()))

    def _safe(self, message) -> str:
        result = _space(html.unescape(str(message)))
        if self._golden_key:
            result = result.replace(self._golden_key, "[hidden]")
        result = re.sub(r"(?i)(golden_key\s*[=:]\s*)[^\s;,]+", r"\1[hidden]", result)
        return result[:700]

    def _request(self, path: str, payload: dict | None = None, *, readonly: bool = False) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise FunPayError("Invalid FunPay request.", kind="internal")
        posting = payload is not None
        writing = posting and not readonly
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json", "Accept-Encoding": "identity"}
        data = None
        if posting:
            data = urlencode(payload).encode("utf-8")
            headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL, "Referer": BASE_URL + "/lots/offerEdit?node=" + self.category_id})
            if self._app_csrf:
                headers["X-CSRF-Token"] = self._app_csrf
        request = Request(BASE_URL + path, data=data, headers=headers, method="POST" if posting else "GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" or final.netloc.lower() != "funpay.com":
                    raise FunPayError("FunPay redirected outside its site. The request was stopped.", kind="access", uncertain=writing)
                if "/account/login" in final.path:
                    raise FunPayError("Your Golden Key has expired or is invalid. Sign in to FunPay and copy it again.", kind="auth")
                trade = re.fullmatch(r"/(?:en/|uk/)?lots/(\d+)/trade/?", urlsplit(path).path)
                if trade and not re.fullmatch(r"/(?:en/|uk/)?lots/" + trade.group(1) + r"/trade/?", final.path):
                    raise FunPayError("FunPay redirected away from this category's seller page. Open the category on FunPay to check its availability.", kind="access")
                if trade and (final.fragment or any(name in parse_qs(final.query) for name in ("page", "cursor", "start_from", "offset"))):
                    raise FunPayError("FunPay returned only part of the seller listings. The complete category count could not be verified.", kind="capacity")
                content = response.read(MAX_RESPONSE + 1)
                if len(content) > MAX_RESPONSE:
                    raise FunPayError("FunPay returned an unexpectedly large response. Check your offers before retrying.", kind="response", uncertain=writing)
                body = content.decode("utf-8", errors="replace")
        except HTTPError as exc:
            try:
                detail = exc.read(16384).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            finally:
                exc.close()
            location = exc.headers.get("Location", "") if exc.headers else ""
            if exc.code in (401,) or "/account/login" in location:
                raise FunPayError("Your Golden Key has expired or is invalid. Sign in to FunPay and copy it again.", kind="auth") from None
            if exc.code == 429:
                raise FunPayError("FunPay is limiting requests. The queue has stopped; wait before continuing.", kind="rate") from None
            if exc.code == 403:
                cloudflare = any(token in detail.lower() for token in ("cloudflare", "cf-chl-", "just a moment", "challenge-platform"))
                message = "FunPay's browser protection blocked the request. Open FunPay in your browser, then reconnect." if cloudflare else "FunPay refused access. Check your Golden Key and whether this category is available to your account."
                raise FunPayError(message, kind="cloudflare" if cloudflare else "access") from None
            if writing and exc.code in (400, 422):
                try:
                    error = self._response_error(json.loads(detail))
                except ValueError:
                    error = None
                if error:
                    raise FunPayError(error, kind="validation") from None
            raise FunPayError(f"FunPay returned HTTP {exc.code}." + (" Check your offers before trying this item again." if writing else " Try reconnecting later."), kind="http", uncertain=writing and exc.code not in (404, 405)) from None
        except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            message = "The connection ended before FunPay confirmed the save. Check your offers before trying this item again." if writing else "Could not reach FunPay. Check your connection and try again."
            raise FunPayError(message, kind="network", uncertain=writing) from None
        if any(token in body.lower() for token in ("cf-chl-", "challenge-platform", "<title>just a moment")):
            raise FunPayError("FunPay's browser protection requires attention. Open FunPay in your browser, then reconnect.", kind="cloudflare", uncertain=writing)
        return body

    def _calc(self, seller_price: str) -> dict:
        raw = self._request("/lots/calc", {"nodeId": self.category_id, "price": seller_price}, readonly=True)
        try:
            result = json.loads(raw)
        except ValueError:
            raise FunPayError("FunPay returned an unreadable price calculation. No listings were submitted.", kind="pricing") from None
        error = self._response_error(result)
        if error:
            raise FunPayError(error, kind="pricing")
        if not isinstance(result, dict):
            raise FunPayError("FunPay did not return price information. No listings were submitted.", kind="pricing")
        return result

    def quote_buyer_rub(self, target_rub: str) -> dict:
        if not self._authenticated or not self._golden_key or not self.category_id:
            raise FunPayError("Connect and load a category before checking buyer prices.", kind="auth")
        if self.currency not in ("RUB", "USD", "EUR", "UAH"):
            raise FunPayError("FunPay did not identify your seller currency. Reconnect before changing buyer prices.", kind="pricing")
        key = (_price(target_rub), self.currency)
        cached = self._buyer_quote_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return dict(cached[1])
        try:
            result = quote_buyer_rub(self._calc, target_rub, self.currency)
        except PricingError as error:
            raise FunPayError(str(error), kind="pricing") from None
        self._buyer_quote_cache[key] = (time.monotonic(), dict(result))
        if len(self._buyer_quote_cache) > 500:
            self._buyer_quote_cache.pop(next(iter(self._buyer_quote_cache)))
        return result

    def _refresh_form(self) -> dict:
        source = self._request("/lots/offerEdit?node=" + self.category_id)
        document = _Document(source)
        page_user = _app_data(document).get("userId")
        if page_user is not None and str(page_user) != str(self.user_id):
            raise FunPayError("FunPay did not return your signed-in editor. Sign in on FunPay and reconnect.", kind="auth")
        try:
            template = _parse_form(source, self.category_id)
        except FunPayError as error:
            if error.kind != "editor_missing":
                raise
            notice = next((_space(node.text()) for node in document.root.walk()
                           if (node.tag == "p" and node.has_class("lead")
                               or node.has_class("alert-danger") or node.has_class("alert-warning"))
                           and _space(node.text())), "")
            detail = " FunPay says: " + self._safe(notice)[:350] if notice else ""
            if any(term in notice.casefold() for term in ("слишком часто", "слишком много запрос", "too many requests", "rate limit", "забагато запит")):
                raise FunPayError("FunPay is limiting requests. Wait before reconnecting." + detail, kind="rate") from None
            raise FunPayError(str(error) + detail, kind="editor_missing") from None
        app = _app_data(template["document"])
        self._app_csrf = str(app.get("csrf-token") or app.get("csrfToken") or self._app_csrf)
        self._template = template
        return template

    def _clear_session(self):
        self._authenticated = False
        self._golden_key = ""
        self._template = None
        self.account, self.user_id, self.currency, self.category_id = "", None, "", ""
        self._app_csrf = ""
        self._buyer_quote_cache.clear()
        self._jar.clear()

    def connect(self, golden_key: str, category) -> dict:
        self.authenticate(golden_key, category)
        try:
            return self.load_category()
        except Exception:
            self._clear_session()
            raise

    def authenticate(self, golden_key: str, category) -> dict:
        self._clear_session()
        try:
            self._authenticate(golden_key, category)
            return {"account": self.account, "user_id": self.user_id, "category": {"id": self.category_id}}
        except Exception:
            self._clear_session()
            raise

    def _authenticate(self, golden_key: str, category):
        category_id = parse_category(category)
        key = str(golden_key or "").strip()
        if key.startswith("golden_key="):
            key = key[len("golden_key="):]
        if not re.fullmatch(r"[A-Za-z0-9._~%+=/\-]{8,1024}", key):
            raise FunPayError("Paste only the Golden Key cookie value from your FunPay browser session.", kind="auth")
        self._golden_key = key
        self.category_id = category_id
        self._jar.clear()
        self._template = None
        self._app_csrf = ""
        self._buyer_quote_cache.clear()
        self._jar.set_cookie(http.cookiejar.Cookie(0, "golden_key", key, None, False, "funpay.com", False, False, "/", True, True, None, True, None, None, {}, False))
        self._jar.set_cookie(http.cookiejar.Cookie(0, "cookie_prefs", "1", None, False, "funpay.com", False, False, "/", True, True, None, True, None, None, {}, False))
        homepage = _Document(self._request("/"))
        app = _app_data(homepage)
        user_id = app.get("userId")
        if "userId" in app and (not str(user_id or "").isdigit() or int(user_id or 0) <= 0):
            raise FunPayError("FunPay did not recognize this Golden Key. Sign in and copy the current cookie value.", kind="auth")
        if not str(user_id or "").isdigit() or int(user_id or 0) <= 0:
            user_id = None
        username_node = next((n for n in homepage.root.walk() if n.has_class("user-link-name")), None)
        username = _space(username_node.text()) if username_node else ""
        if not user_id:
            profile = next((n for n in homepage.root.walk() if n.tag == "a" and n.has_class("user-link") and re.search(r"/users/\d+/", n.attrs.get("href") or "")), None)
            if profile:
                user_id = re.search(r"/users/(\d+)/", profile.attrs["href"]).group(1)
                username = username or _space(profile.text())
        if not user_id:
            raise FunPayError("FunPay did not recognize this Golden Key. Sign in and copy the current cookie value.", kind="auth")
        self.user_id = user_id
        self.account = username or ("FunPay account #" + str(user_id) if user_id else "Connected FunPay account")
        self._authenticated = True
        self._app_csrf = str(app.get("csrf-token") or app.get("csrfToken") or "")

    def _management_info(self, metadata, message):
        self.currency = metadata["currency"]
        return {"account": self.account, "user_id": self.user_id, "category": metadata["category"],
                "currency": self.currency, "fields": [], "default_price": "", "constraints": {},
                "creation_available": False, "management_only": True, "message": message}

    def load_category(self, listing_count=None) -> dict:
        if not self._authenticated or not self._golden_key:
            raise FunPayError("Connect your FunPay account first.", kind="auth")
        if listing_count is not None and listing_count >= LISTING_LIMIT:
            from management import OfferManager
            metadata = OfferManager(self).connection_metadata()
            return self._management_info(metadata,
                f"This category has {metadata['count']} listings. The tool's {LISTING_LIMIT}-listing cap blocks new offers. "
                "Your account is connected. Use My listings to manage existing offers.")
        try:
            template = self._refresh_form()
        except FunPayError as error:
            if error.kind != "editor_missing":
                raise
            # A category can withhold the new-offer editor while existing offers remain editable.
            from management import OfferManager
            try:
                metadata = OfferManager(self).connection_metadata()
            except FunPayError as fallback:
                if fallback.kind in ("auth", "access", "cloudflare", "rate", "network", "http"):
                    raise
                raise FunPayError(self._safe(error) + " " + self._safe(fallback), kind=fallback.kind) from None
            count = metadata["count"]
            reason = (f"This category has {count} listings, so the tool's {LISTING_LIMIT}-listing cap blocks new offers. "
                      if count >= LISTING_LIMIT else "FunPay did not provide a new-listing editor for this category. ")
            return self._management_info(metadata,
                reason + "Your account is connected. Use My listings to manage existing offers. " + self._safe(error))
        self.currency = template["currency"]
        return {"account": self.account, "user_id": self.user_id,
                "category": {"id": self.category_id, "name": template["category_name"]},
                "currency": self.currency, "fields": template["fields"],
                "default_price": template["values"].get("price", ""), "constraints": template["constraints"],
                "creation_available": True, "management_only": False}

    def _response_error(self, response) -> str | None:
        if not isinstance(response, dict):
            return None
        errors = response.get("errors")
        parts = []
        if isinstance(errors, dict):
            parts = [f"{key}: {value}" for key, value in errors.items() if value]
        elif isinstance(errors, list):
            for error in errors:
                if isinstance(error, (list, tuple)) and len(error) >= 2:
                    parts.append(f"{error[0]}: {error[1]}")
                elif error:
                    parts.append(str(error))
        if response.get("error"):
            parts.insert(0, str(response.get("msg") or response["error"]))
        elif not response.get("done") and not response.get("url") and response.get("msg"):
            parts.insert(0, str(response["msg"]))
        return self._safe("; ".join(parts)) if parts else None

    def publish(self, item: dict, fields: dict | None = None, active: bool = True) -> dict:
        if not self._template:
            raise FunPayError("Connect your FunPay account and load a category first.", kind="auth")
        if not isinstance(item, dict) or not isinstance(fields or {}, dict):
            raise FunPayError("Invalid listing data.")
        price = _price(item.get("price"))
        template = self._refresh_form()
        if template["currency"] != self.currency:
            raise FunPayError("Your FunPay price currency changed. Reconnect and review prices before publishing.", kind="currency")
        if item.get("buyer_target_rub") is not None:
            try:
                current_buyer = buyer_rub_amount(self._calc(price))
            except PricingError as error:
                raise FunPayError(str(error), kind="pricing") from None
            if current_buyer != Decimal(item["buyer_price_rub"]):
                self._buyer_quote_cache.clear()
                raise FunPayError("FunPay's buyer price changed since your preview. Review the buyer prices again before publishing.", kind="pricing")
        payload = dict(template["values"])
        for name in payload:
            if _purchase_message(name):
                payload[name] = ""
        schema = {field["name"]: field for field in template["fields"]}
        field_values = {name: control.get("value", "") for name, control in schema.items()}
        field_values.update(fields or {})
        for name, value in (fields or {}).items():
            if name not in schema or name in CORE_NAMES | PROTECTED_NAMES:
                raise FunPayError("The category fields changed or include a protected field. Reconnect and review the category.", kind="form")
            control = schema[name]
            if not field_enabled(control, field_values):
                payload.pop(name, None)
                continue
            if control["type"] == "checkbox":
                if value in (True, "on", "1", 1):
                    payload[name] = control.get("checked_value") or "on"
                else:
                    payload.pop(name, None)
                continue
            value = str(value if value is not None else "")
            if control["type"] == "select" and value not in {option["value"] for option in control["options"]}:
                raise FunPayError("Choose a valid value for " + control["label"] + ".")
            if control.get("maxlength") and len(value) > int(control["maxlength"]):
                raise FunPayError(control["label"] + " is too long.")
            payload[name] = value
        for name, control in schema.items():
            if not field_enabled(control, field_values):
                payload.pop(name, None)
                continue
            if control["required"] and not str(payload.get(name, "")).strip():
                raise FunPayError("Complete the required category field: " + control["label"] + ".")
        for key, target in (("title_ru", "fields[summary][ru]"), ("title_en", "fields[summary][en]"), ("description_ru", "fields[desc][ru]"), ("description_en", "fields[desc][en]")):
            text = str(item.get(key) or "").strip()
            if not text:
                raise FunPayError("Complete both Russian and English titles and descriptions.")
            limit = template["constraints"].get(target, {}).get("maxlength")
            if limit and len(text) > int(limit):
                raise FunPayError(key.replace("_", " ") + " exceeds FunPay's " + limit + " character limit.")
            payload[target] = text
        payload["price"] = price
        payload["offer_id"] = "0"
        payload["node_id"] = self.category_id
        payload.pop("deleted", None)
        payload.pop("auto_delivery", None)
        if "secrets" in payload:
            payload["secrets"] = ""
        if "amount" in payload and not str(payload["amount"]).strip():
            payload["amount"] = "1"
        if active:
            payload["active"] = "on"
        else:
            payload.pop("active", None)
        raw = self._request("/lots/offerSave", payload)
        try:
            result = json.loads(raw)
        except ValueError:
            raise FunPayError("FunPay did not confirm whether this item was saved. Check your offers before trying it again.", kind="response", uncertain=True) from None
        error = self._response_error(result)
        if error:
            lower = error.lower()
            kind = "rate" if any(s in lower for s in ("слишком часто", "too often", "too many", "подождите", "зачекайте")) else "validation"
            if any(s in lower for s in ("авторизац", "unauthorized", "authentication", "войдите")):
                kind = "auth"
            elif any(s in lower for s in ("доступ запрещ", "access denied", "доступ заборон")):
                kind = "access"
            elif any(s in lower for s in ("обновите страницу", "refresh the page", "reload the page", "оновіть сторінку")):
                kind = "form"
            raise FunPayError(error, kind=kind)
        result_url = str(result.get("url") or "") if isinstance(result, dict) else ""
        offer_url = None
        if result_url:
            parsed = urlsplit(urljoin(BASE_URL, result_url))
            if parsed.scheme == "https" and parsed.netloc.lower() == "funpay.com":
                params = parse_qs(parsed.query)
                offer_id = (params.get("offer") or params.get("id") or [""])[0]
                if offer_id.isdigit() and int(offer_id) > 0 and parsed.path in ("/lots/offer", "/lots/offerEdit", "/en/lots/offer", "/en/lots/offerEdit", "/uk/lots/offer", "/uk/lots/offerEdit"):
                    offer_url = BASE_URL + "/lots/offer?id=" + offer_id
        confirmed = isinstance(result, dict) and (
            result.get("done") is True or result.get("done") == 1
            or ("done" not in result and offer_url is not None)
        )
        if not confirmed:
            raise FunPayError("FunPay returned an unrecognized save result. Check your offers before trying this item again.", kind="response", uncertain=True)
        url = offer_url or BASE_URL + "/lots/" + self.category_id + "/trade"
        return {"message": "Published on FunPay." if active else "Saved as an inactive FunPay offer.", "url": url}
