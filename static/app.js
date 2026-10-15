"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DRAFT_KEY = "listcraft.funpay.draft.v1";
  const LISTING_LIMIT = 39;
  const LEGACY_DRAFT_ROW_LIMIT = 500;
  const CONNECTION_PENDING_KEY = "listcraft.connection.pending";
  const state = {
    config: null, connected: false, account: "", category: null, currency: "", schema: [],
    fields: {}, rows: [], hasRun: false, running: false, busy: false, jobId: null,
    pollTimer: null, editId: null, lastFocusedTemplate: null, toastTimer: null, pollFailures: 0,
    priceMode: "buyer_rub", quoteId: null, quoteExpiresAt: 0, quoteRunning: false,
    quoteTimer: null, quoteExpiryTimer: null, quoteGeneration: 0, quoteCancelRequested: false, quotePollFailures: 0,
    managementBusy: false, capacity: null, capacityChecking: false,
    creationAvailable: true, connectionMessage: "", connectionJobId: null, connectionStopping: false
  };
  const formIds = ["category", "title-ru", "title-en", "description-ru", "description-en", "items-input", "default-price", "delay"];
  const statusLabels = { queued: "Ready", publishing: "Publishing", published: "Published", failed: "Failed", uncertain: "Check on FunPay", cancelled: "Cancelled" };
  const locked = (row) => ["published", "uncertain", "publishing"].includes(row.state);
  const eligible = () => state.rows.filter((row) => ["queued", "failed", "cancelled"].includes(row.state));
  const toText = (value) => value == null ? "" : String(value);
  const numberText = (value) => Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
  const priceNumber = (value) => Number(toText(value).trim().replace(",", "."));
  const goodPrice = (value) => {
    const price = priceNumber(value);
    return Number.isFinite(price) && price > 0 && price <= 1000000000 && Math.abs(price * 100 - Math.round(price * 100)) < 0.00001;
  };
  const currentLines = () => $("items-input").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const inputCurrency = () => "RUB";
  const money = (value, currency = inputCurrency()) => `${numberText(value)} ${currency}`;
  const priceKey = (value) => priceNumber(value).toFixed(2);
  const editingBlocked = () => state.running || state.quoteRunning || state.busy || state.managementBusy;
  const makeId = () => crypto.randomUUID();

  function invalidateCapacity() {
    state.capacity = null;
  }

  function capacityNote() {
    const note = $("capacity-note");
    if (!note) return;
    if (state.connected && !state.creationAvailable) {
      note.textContent = state.connectionMessage || "This account is connected for managing existing listings. New listings are unavailable in this category. Use My listings to edit, price, pause, or delete existing listings.";
      return;
    }
    if (state.capacityChecking) { note.textContent = "Checking this category's current listing availability…"; return; }
    if (state.capacity) {
      note.textContent = `Last checked: ${state.capacity.used} / ${LISTING_LIMIT} listings used, including paused listings · ${state.capacity.remaining} ${state.capacity.remaining === 1 ? "space" : "spaces"} remaining. Availability is checked again before review.`;
      return;
    }
    note.textContent = `Category limit: ${LISTING_LIMIT} listings in total, including paused listings. Current availability is checked before prices are calculated or a batch is reviewed.`;
  }

  async function checkCapacity() {
    if (editingBlocked() || !state.connected) return false;
    const count = eligible().length;
    if (count > LISTING_LIMIT) { message(`This batch has ${count} remaining listings. The limit is ${LISTING_LIMIT}, including active and paused listings. Remove extra items before checking prices or publishing.`); update(); return false; }
    state.capacityChecking = true; state.busy = true; update();
    try {
      const data = await api("/api/capacity");
      const used = Number(data.used); const remaining = Number(data.remaining);
      if (!Number.isInteger(used) || used < 0 || !Number.isInteger(remaining) || remaining < 0) throw new Error("The current listing count could not be verified. Try reviewing the batch again before publishing.");
      if (toText(data.category?.id) !== toText(state.category?.id) || toText(data.account) !== state.account) throw new Error("The listing count belongs to a different account or category. Reconnect before publishing.");
      state.capacity = { used, remaining: Math.min(remaining, Math.max(0, LISTING_LIMIT - used)) };
      if (count > state.capacity.remaining) {
        message(`This category has ${used} / ${LISTING_LIMIT} listings, including paused listings. Your batch has ${count} remaining ${count === 1 ? "listing" : "listings"}, but only ${state.capacity.remaining} ${state.capacity.remaining === 1 ? "space is" : "spaces are"} available. Remove extra batch items or free listing slots before publishing.`);
        return false;
      }
      return true;
    } catch (error) { invalidateCapacity(); message(error.message); return false; }
    finally { state.capacityChecking = false; state.busy = false; update(); }
  }

  function decimalAmount(value) {
    const text = toText(value).trim().replace(",", ".");
    return /^\d+(?:\.\d+)?$/.test(text) && Number.isFinite(Number(text)) && Number(text) > 0 && Number(text) <= 1000000000 ? text : "";
  }

  function quoteMoney(value, currency = "RUB") {
    const [whole, fraction] = toText(value).split(".");
    return `${numberText(whole)}${fraction ? "." + fraction : ""} ${currency}`;
  }

  function sumAmounts(values) {
    const parts = values.map((value) => toText(value).split("."));
    const digits = Math.max(0, ...parts.map((part) => (part[1] || "").length));
    const scale = 10n ** BigInt(digits);
    const total = parts.reduce((sum, [whole, fraction]) => sum + BigInt(whole || "0") * scale + BigInt((fraction || "").padEnd(digits, "0") || "0"), 0n);
    if (!digits) return toText(total);
    const text = total.toString().padStart(digits + 1, "0");
    return `${text.slice(0, -digits)}.${text.slice(-digits)}`;
  }

  function invalidateQuote() {
    state.quoteId = null; state.quoteExpiresAt = 0;
    clearTimeout(state.quoteExpiryTimer);
    for (const row of state.rows) if (!locked(row)) { row.quote = null; applyTemplates(row); }
  }

  function quoteValid() {
    return !!state.quoteId && Date.now() < state.quoteExpiresAt
      && eligible().every((row) => row.quote && row.quote.target_rub === priceKey(row.price));
  }

  function cleanQuote(value) {
    if (!value || typeof value !== "object") return null;
    if (!goodPrice(value.seller_price) || !decimalAmount(value.buyer_price_rub) || !goodPrice(value.target_rub)) return null;
    return {
      target_rub: priceKey(value.target_rub), seller_price: priceKey(value.seller_price),
      buyer_price_rub: decimalAmount(value.buyer_price_rub), seller_currency: toText(value.seller_currency).slice(0, 30),
      exact: value.exact === true, note: toText(value.note).slice(0, 1000)
    };
  }

  function priceConversion(row) {
    const display = node("div", "price-conversion");
    if ((row.price_mode || state.priceMode) !== "buyer_rub") {
      display.append(node("span", "", "Previous listing · seller amount"));
      if (locked(row) && goodPrice(row.seller_price || row.price)) display.append(node("span", "", money(row.seller_price || row.price, row.seller_currency || state.currency)));
      return display;
    }
    if (!row.quote) { display.append(node("span", "", "Net checked before publishing")); return display; }
    display.append(node("span", "", `You receive: ${money(row.quote.seller_price, row.quote.seller_currency || state.currency)}`));
    display.append(node("span", row.quote.exact ? "" : "rounding-note", `Buyer: ${quoteMoney(row.quote.buyer_price_rub)}`));
    if (!row.quote.exact) display.append(node("span", "rounding-note", `Target: ${money(row.price, "RUB")} · rounded`));
    return display;
  }

  function fieldEnabled(field) {
    const conditions = Array.isArray(field.conditions) ? field.conditions : [];
    return conditions.every((condition) => {
      if (!condition || !condition.name || !Array.isArray(condition.values)) return false;
      const value = toText(state.fields[condition.name]).trim().toLowerCase();
      return condition.values.some((expected) => toText(expected).trim().toLowerCase() === value);
    });
  }

  function fieldLabel(field) {
    if (field.label && field.label !== field.name) return toText(field.label);
    if (field.name === "fields[method]") return "Метод / Способ получения";
    return toText(field.label || field.name);
  }

  function syncCategoryFields() {
    const schema = new Map(state.schema.map((field) => [toText(field.name), field]));
    for (const wrapper of $("category-fields").children) {
      const field = schema.get(wrapper.dataset.fieldName);
      const enabled = !!field && fieldEnabled(field);
      wrapper.hidden = !enabled;
      for (const input of wrapper.querySelectorAll("input,select,textarea")) input.disabled = editingBlocked() || !enabled;
    }
    $("category-fields-section").hidden = ![...$("category-fields").children].some((wrapper) => !wrapper.hidden);
  }

  function categoryPayload() {
    const result = {};
    for (const field of state.schema) {
      if (!fieldEnabled(field)) continue;
      const value = state.fields[field.name] ?? field.value ?? "";
      if (!field.required && value === "" && Array.isArray(field.options) && field.options.length
        && !field.options.some((option) => toText(option.value) === "")) continue;
      result[field.name] = value;
    }
    return result;
  }

  function node(tag, className, content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content != null) el.textContent = content;
    return el;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function message(text, success = false) {
    $("global-message").textContent = text;
    $("global-message").className = `message${success ? " success" : ""}`;
    $("global-message").hidden = !text;
  }

  function toast(text) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = text;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500);
  }

  async function api(path, payload) {
    if (!state.config?.token) throw new Error("The local app is not ready. Refresh this page or restart the app.");
    const options = { cache: "no-store", headers: { "X-Lister-Token": state.config.token } };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    let response;
    try { response = await fetch(path, options); }
    catch { throw new Error("Can't reach the local app. Keep its window open, then try again."); }
    let result;
    try { result = await response.json(); }
    catch { throw new Error(`The local app returned an unreadable response (${response.status}).`); }
    const connectionJob = path.startsWith("/api/connection_job?") && typeof result.state === "string";
    if (!response.ok || result.error && !connectionJob) {
      const error = new Error(result.error || `Request failed (${response.status}).`);
      error.status = response.status; error.kind = result.kind; error.uncertain = result.uncertain === true; throw error;
    }
    return result;
  }

  function draft() {
    return {
      version: 1,
      category: $("category").value,
      title_ru: $("title-ru").value, title_en: $("title-en").value,
      description_ru: $("description-ru").value, description_en: $("description-en").value,
      items_text: $("items-input").value, default_price: $("default-price").value,
      delay: $("delay").value, active: $("active-listings").checked, price_mode: state.priceMode,
      fields: Object.fromEntries(Object.entries(state.fields).filter(([name]) => name !== "payment_msg" && !name.startsWith("fields[payment_msg]"))), currency: state.currency,
      has_run: state.hasRun, job_id: state.jobId,
      rows: state.rows.map((row) => ({
        id: row.id, source: row.source, title_ru: row.title_ru, title_en: row.title_en,
        description_ru: row.description_ru, description_en: row.description_en, price: row.price,
        state: row.state, message: row.message || "", url: row.url || "", custom: !!row.custom,
        price_mode: row.price_mode || state.priceMode, pricing: locked(row) && row.quote ? { ...row.quote } : null,
        seller_price: locked(row) ? row.seller_price || row.quote?.seller_price || "" : "",
        seller_currency: locked(row) ? row.seller_currency || row.quote?.seller_currency || state.currency : ""
      }))
    };
  }

  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft()));
      $("draft-status").textContent = "Draft saved locally";
    } catch { $("draft-status").textContent = "Draft storage unavailable — export to save"; }
  }

  function safeUrl(value) {
    if (!value) return "";
    try {
      const url = new URL(value);
      return url.protocol === "https:" && (url.hostname === "funpay.com" || url.hostname.endsWith(".funpay.com")) ? url.href : "";
    } catch { return ""; }
  }

  function restoreDraft(data, imported = false) {
    if (!data || typeof data !== "object" || Array.isArray(data) || data.version !== 1) throw new Error("Choose a Listcraft draft JSON file (version 1).");
    // Older drafts can retain history beyond today's creation limit; only eligible rows may publish.
    if (!Array.isArray(data.rows) || data.rows.length > LEGACY_DRAFT_ROW_LIMIT) throw new Error("This draft has an invalid or oversized saved history.");
    const restoredMode = data.price_mode === "buyer_rub" ? "buyer_rub" : "seller";
    let resetPrices = restoredMode !== "buyer_rub" && !!toText(data.default_price);
    const seenIds = new Set();
    const validatedRows = data.rows.map((row) => {
      if (!row || typeof row !== "object") throw new Error("The draft contains an invalid listing.");
      const id = toText(row.id) || makeId();
      if (id.length > 100 || seenIds.has(id)) throw new Error("The draft contains invalid or duplicate listing IDs.");
      seenIds.add(id);
      const status = Object.hasOwn(statusLabels, row.state) ? row.state : "queued";
      const historical = ["published", "uncertain", "publishing"].includes(status) || status === "queued" && !!data.job_id;
      const legacyPrice = (row.price_mode || restoredMode) !== "buyer_rub";
      if (legacyPrice && !historical && toText(row.price)) resetPrices = true;
      return {
        id, source: toText(row.source), title_ru: toText(row.title_ru), title_en: toText(row.title_en),
        description_ru: toText(row.description_ru), description_en: toText(row.description_en),
        price: legacyPrice && !historical ? "" : toText(row.price), state: status, message: toText(row.message), url: safeUrl(row.url), custom: !!row.custom,
        price_mode: legacyPrice && historical ? "seller" : "buyer_rub",
        quote: ["published", "uncertain", "publishing"].includes(status) ? cleanQuote(row.pricing) : null,
        seller_price: goodPrice(row.seller_price) ? priceKey(row.seller_price) : "",
        seller_currency: toText(row.seller_currency || data.currency).slice(0, 30)
      };
    });
    const names = { category: "category", title_ru: "title-ru", title_en: "title-en", description_ru: "description-ru", description_en: "description-en", items_text: "items-input", default_price: "default-price", delay: "delay" };
    for (const [name, id] of Object.entries(names)) {
      if (typeof data[name] === "string" || typeof data[name] === "number") $(id).value = toText(data[name]);
    }
    $("active-listings").checked = data.active !== false;
    if (restoredMode !== "buyer_rub") $("default-price").value = "";
    state.rows = validatedRows; state.priceMode = "buyer_rub";
    invalidateQuote();
    state.fields = {};
    if (data.fields && typeof data.fields === "object" && !Array.isArray(data.fields)) {
      for (const [key, value] of Object.entries(data.fields)) {
        if (key === "payment_msg" || key.startsWith("fields[payment_msg]")) continue;
        if (["golden_key", "csrf_token", "__proto__", "constructor", "prototype"].includes(key.toLowerCase())) continue;
        if (["string", "number", "boolean"].includes(typeof value)) state.fields[key] = value;
      }
    }
    state.hasRun = !!data.has_run || state.rows.some((row) => row.state !== "queued");
    state.jobId = imported ? null : (typeof data.job_id === "string" ? data.job_id : null);
    if (imported) {
      for (const row of state.rows) {
        if (row.state === "publishing" || row.state === "queued" && data.job_id) {
          row.state = "uncertain";
          row.message = "Imported from a publishing batch. Check FunPay before creating another listing.";
        }
      }
    }
    state.currency = typeof data.currency === "string" ? data.currency.slice(0, 20) : "";
    if (resetPrices) message("All prices now mean the buyer's price in RUB. Re-enter buyer prices for the older draft; its seller amounts were cleared.");
    renderRows(); update();
    return resetPrices;
  }

  function template(text, item, price) {
    return text.replace(/\{item\}/g, () => item).replace(/\{price\}/g, () => toText(price));
  }

  function applyTemplates(row) {
    if (row.custom || locked(row)) return;
    const actualPrice = row.quote?.target_rub === priceKey(row.price) ? row.quote.buyer_price_rub : row.price;
    row.title_ru = template($("title-ru").value || "{item}", row.source, actualPrice);
    row.title_en = template($("title-en").value || "{item}", row.source, actualPrice);
    row.description_ru = template($("description-ru").value, row.source, actualPrice);
    row.description_en = template($("description-en").value, row.source, actualPrice);
  }

  function renderFields() {
    const container = $("category-fields");
    container.replaceChildren();
    $("category-fields-section").hidden = !state.schema.some((field) => field.type !== "hidden");
    for (const field of state.schema) {
      if (!field || !field.name) continue;
      const fieldName = toText(field.name);
      if (!Object.hasOwn(state.fields, fieldName)) state.fields[fieldName] = field.value ?? (field.type === "checkbox" ? false : "");
      if (field.type === "hidden") continue;
      const wrapper = node("div", "field");
      wrapper.dataset.fieldName = fieldName;
      const label = node("label", "", fieldLabel(field));
      const id = `category-field-${container.children.length}`;
      label.htmlFor = id;
      if (field.required) label.append(node("span", "required", " *"));
      let input;
      if (Array.isArray(field.options) && field.options.length) {
        input = node("select");
        if (!field.options.some((option) => toText(option.value) === "")) {
          const placeholder = node("option", "", "Choose an option");
          placeholder.value = ""; input.append(placeholder);
        }
        for (const option of field.options) {
          const el = node("option", "", toText(option.label ?? option.value));
          el.value = toText(option.value); input.append(el);
        }
        input.value = toText(state.fields[fieldName]);
        if (input.selectedIndex < 0) input.value = "";
        state.fields[fieldName] = input.value;
      } else if (field.type === "textarea") {
        input = node("textarea"); input.rows = 3; input.value = toText(state.fields[fieldName]);
      } else {
        input = node("input");
        input.type = field.type === "checkbox" ? "checkbox" : field.type === "number" ? "number" : "text";
        if (input.type === "checkbox") {
          input.checked = state.fields[fieldName] !== false && state.fields[fieldName] !== "" && state.fields[fieldName] != null;
          state.fields[fieldName] = input.checked;
        }
        else input.value = toText(state.fields[fieldName]);
      }
      for (const attr of ["min", "max", "step", "maxlength"]) {
        if (field[attr] != null && field[attr] !== "") input.setAttribute(attr, toText(field[attr]));
      }
      input.id = id; input.required = !!field.required; input.dataset.fieldName = fieldName;
      const changed = () => {
        state.fields[fieldName] = input.type === "checkbox" ? input.checked : input.value;
        saveDraft(); update();
      };
      input.addEventListener("input", changed);
      input.addEventListener("change", changed);
      if (input.type === "checkbox") {
        const checkbox = node("div", "category-checkbox"); checkbox.append(input, label); wrapper.append(checkbox);
      } else wrapper.append(label, input);
      container.append(wrapper);
    }
    syncCategoryFields();
  }

  function requiredFieldsValid() {
    return state.schema.every((field) => !fieldEnabled(field) || !field.required || (field.type === "checkbox" ? !!state.fields[field.name] : toText(state.fields[field.name]).trim() !== ""))
      && [...$("category-fields").querySelectorAll("input,select,textarea")].every((input) => input.disabled || input.checkValidity());
  }

  function renderRows() {
    const tbody = $("items-table-body"); tbody.replaceChildren();
    $("empty-preview").hidden = state.rows.length > 0;
    $("table-wrapper").hidden = state.rows.length === 0;
    for (const [index, row] of state.rows.entries()) {
      const tr = node("tr"); tr.dataset.rowId = row.id;
      tr.append(node("td", "row-number", String(index + 1).padStart(2, "0")));
      const titleCell = node("td");
      titleCell.append(node("div", "item-title", row.title_ru || row.source || "Untitled item"), node("div", "item-subtitle", row.title_en));
      const priceCell = node("td");
      const price = node("input", "row-price"); price.type = "number"; price.min = "0.01"; price.step = "0.01";
      price.inputMode = "decimal"; price.value = row.price_mode === "seller" ? "" : row.price; price.setAttribute("aria-label", `Buyer price (RUB) for ${row.source || row.title_en}`);
      price.disabled = editingBlocked() || locked(row); price.classList.toggle("invalid", !!row.price && !goodPrice(row.price));
      let conversion = priceConversion(row);
      price.addEventListener("input", () => {
        row.price = price.value; invalidateQuote(); applyTemplates(row); price.classList.toggle("invalid", !goodPrice(row.price));
        const next = priceConversion(row); conversion.replaceWith(next); conversion = next;
        for (const other of state.rows) {
          if (other.id === row.id || locked(other)) continue;
          const otherTr = [...tbody.children].find((item) => item.dataset.rowId === other.id);
          const old = otherTr?.querySelector(".price-conversion"); if (old) old.replaceWith(priceConversion(other));
        }
        titleCell.children[0].textContent = row.title_ru || row.source || "Untitled item";
        titleCell.children[1].textContent = row.title_en; saveDraft(); update();
      });
      priceCell.append(price, conversion);
      const detailsCell = node("td");
      const details = node("button", "details-button"); details.type = "button";
      details.append(icon(locked(row) || editingBlocked() ? "eye" : "edit"), document.createTextNode(locked(row) || editingBlocked() ? "View" : "Edit"));
      details.setAttribute("aria-label", `${locked(row) || editingBlocked() ? "View" : "Edit"} ${row.source || row.title_en}`);
      details.addEventListener("click", () => openEdit(row.id)); detailsCell.append(details);
      const statusCell = node("td");
      statusCell.append(node("span", `row-status status-${row.state}`, statusLabels[row.state] || row.state));
      if (row.message) statusCell.append(node("div", "status-message", row.message));
      if (safeUrl(row.url)) {
        const p = node("div", "status-message"); const a = node("a", "", "Open on FunPay ↗");
        a.href = safeUrl(row.url); a.target = "_blank"; a.rel = "noopener noreferrer"; p.append(a); statusCell.append(p);
      }
      const removeCell = node("td");
      if (!locked(row) && !editingBlocked()) {
        const remove = node("button", "remove-row"); remove.type = "button"; remove.append(icon("close"));
        remove.setAttribute("aria-label", `Remove ${row.source || row.title_en}`);
        remove.addEventListener("click", () => { state.rows = state.rows.filter((item) => item.id !== row.id); invalidateQuote(); saveDraft(); renderRows(); update(); });
        removeCell.append(remove);
      }
      tr.append(titleCell, priceCell, detailsCell, statusCell, removeCell); tbody.append(tr);
    }
  }

  function publishReason() {
    if (!state.config) return "Connecting to the local app…";
    if (state.running) return "Your batch is publishing";
    if (state.quoteRunning) return "Calculating buyer prices…";
    if (state.managementBusy) return "Finish managing your listings";
    if (state.busy) return "Please wait…";
    if (state.connected && !state.creationAvailable) return "New listings unavailable; use My listings";
    if (!state.rows.length) return "Build a batch to get started";
    if (eligible().length > LISTING_LIMIT) return `Remove extra items: at most ${LISTING_LIMIT} remaining listings are allowed`;
    if (!state.connected) return "Load a category to continue";
    if (!requiredFieldsValid()) return "Complete your category options";
    if (!eligible().length) return "No remaining listings to publish";
    if (eligible().some((row) => !goodPrice(row.price))) return "Set a valid price for every item";
    if (eligible().some((row) => !row.title_ru.trim() || !row.title_en.trim())) return "Add a title in both languages";
    if (eligible().some((row) => !row.description_ru.trim() || !row.description_en.trim())) return "Add a description in both languages";
    if (eligible().some((row) => row.title_ru.length > 500 || row.title_en.length > 500)) return "Keep listing titles under 500 characters";
    if (eligible().some((row) => row.description_ru.length > 20000 || row.description_en.length > 20000)) return "Keep descriptions under 20,000 characters";
    if (!Number.isInteger(Number($("delay").value)) || Number($("delay").value) < 3 || Number($("delay").value) > 60) return "Set a pause from 3 to 60 seconds";
    return "";
  }

  function update() {
    syncCategoryFields();
    capacityNote();
    const rowsById = new Map(state.rows.map((row) => [row.id, row]));
    for (const tr of $("items-table-body").children) {
      const row = rowsById.get(tr.dataset.rowId); if (!row) continue;
      const readonly = editingBlocked() || locked(row);
      const price = tr.querySelector(".row-price"); if (price) price.disabled = readonly;
      const details = tr.querySelector(".details-button");
      if (details) {
        details.replaceChildren(icon(readonly ? "eye" : "edit"), document.createTextNode(readonly ? "View" : "Edit"));
        details.setAttribute("aria-label", `${readonly ? "View" : "Edit"} ${row.source || row.title_en}`);
      }
      const remove = tr.querySelector(".remove-row");
      if (remove) { remove.disabled = editingBlocked(); remove.hidden = locked(row); }
    }
    const lineCount = currentLines().length;
    $("line-count").textContent = `${lineCount} / ${LISTING_LIMIT} ${lineCount === 1 ? "line" : "lines"}`;
    $("stat-items").replaceChildren(document.createTextNode(`${state.rows.length} `), node("small", "", state.rows.length === 1 ? "item" : "items"));
    const total = state.rows.reduce((sum, row) => sum + (row.price_mode !== "seller" && goodPrice(row.price) ? priceNumber(row.price) : 0), 0);
    $("stat-value").replaceChildren(document.createTextNode(`${numberText(total)} `), node("small", "", "RUB"));
    const ready = eligible().length;
    $("stat-ready").textContent = state.rows.length ? `${ready} ready · ${state.rows.filter((row) => row.state === "published").length} published` : "Your list starts below";
    $("stat-account").textContent = state.account || "Not connected";
    $("stat-category").textContent = state.connected && state.category ? `${state.category.name} · #${state.category.id}` : "Connect to load your category";
    $("value-caption").textContent = "Your entered buyer prices combined";
    const currency = "RUB";
    const symbol = ({ RUB: "₽", USD: "$", EUR: "€", UAH: "₴" })[currency.toUpperCase()] || (currency.length <= 3 ? currency : "") || "¤";
    $("currency-suffix").textContent = symbol; $("stat-currency-icon").textContent = symbol;
    $("table-currency").textContent = currency;
    $("table-price-label").textContent = "BUYER PRICE";
    $("default-price-label").textContent = "Default buyer price (RUB)";
    $("buyer-price-note").hidden = false;
    $("connection-badge").replaceChildren(node("i"), document.createTextNode(state.connected ? "Connected" : "Not connected"));
    $("connection-badge").classList.toggle("connected", state.connected);
    $("connected-info").hidden = !state.connected;
    $("connected-label").textContent = state.connected ? `${state.account} · ${state.category?.name || "Category loaded"} · ${state.creationAvailable ? "buyer prices in RUB" : "manage existing listings"}` : "";
    const reason = publishReason();
    $("publish-hint").textContent = reason || `${ready} ${ready === 1 ? "listing" : "listings"} ready to go`;
    $("publish-btn").disabled = !!reason;
    $("publish-btn").firstChild.textContent = state.hasRun ? "Check prices & retry " : "Check prices & review ";
    $("publish-btn").hidden = state.running;
    $("stop-btn").hidden = !state.running;
    $("stop-btn").disabled = state.busy;
    $("connect-btn").disabled = editingBlocked() || state.connected || !state.config;
    $("disconnect-btn").disabled = editingBlocked();
    $("golden-key").disabled = editingBlocked() || state.connected;
    $("category").disabled = editingBlocked() || state.connected || state.hasRun;
    $("reveal-key").disabled = editingBlocked();
    $("clear-key").disabled = editingBlocked();
    for (const id of ["title-ru", "title-en", "description-ru", "description-en", "items-input"]) $(id).disabled = editingBlocked() || state.hasRun;
    for (const id of ["default-price", "apply-prices", "delay", "active-listings"]) $(id).disabled = editingBlocked();
    $("build-btn").disabled = editingBlocked() || state.hasRun || !currentLines().length;
    $("build-btn").firstChild.nextSibling.textContent = state.rows.length ? "Update my batch" : "Build my batch";
    $("demo-btn").disabled = editingBlocked() || state.hasRun;
    $("import-btn").disabled = editingBlocked();
    $("clear-draft-btn").disabled = editingBlocked();
    $("new-batch-btn").hidden = !state.hasRun;
    $("new-batch-btn").disabled = editingBlocked();
    $("cancel-quote-btn").disabled = state.quoteCancelRequested || !state.quoteId;
    window.dispatchEvent(new Event("listcraft:session"));
  }

  function applyConnection(data) {
    state.connected = true; state.account = toText(data.account); state.category = data.category;
    state.creationAvailable = data.creation_available !== false && data.management_only !== true;
    state.connectionMessage = !state.creationAvailable ? toText(data.message).trim() || "Your account is connected for managing existing listings. FunPay did not provide a new-listing editor for this category. Use My listings below to manage your existing listings." : "";
    invalidateCapacity(); state.currency = toText(data.currency); state.schema = Array.isArray(data.fields) ? data.fields : [];
    invalidateQuote();
    const allowedNames = new Set(state.schema.map((field) => field.name));
    state.fields = Object.fromEntries(Object.entries(state.fields).filter(([name]) => allowedNames.has(name)));
    $("golden-key").value = ""; $("golden-key").type = "password";
    $("golden-key").placeholder = "Connected · key held in app memory";
    renderFields(); renderRows(); saveDraft();
    if (state.creationAvailable) toast("Category loaded. Your batch is ready to build.");
    else { message(state.connectionMessage); toast("Connected. You can manage existing listings."); }
  }

  function connectionProgress(job) {
    $("connection-progress").hidden = false;
    const running = job.state === "running";
    const phases = { authenticating: "Connecting to your account", checking: "Loading this category's listings", cleaning: "Checking paused and zero-stock listings", loading: "Loading category options" };
    $("connection-progress-title").textContent = running ? phases[job.phase] || "Connecting and cleaning listings" : job.state === "completed" ? "Connection cleanup finished" : job.state === "stopped" ? "Connection cleanup stopped" : "Connection could not finish";
    const completed = Number(job.completed) || 0; const total = Number(job.total) || 0;
    $("connection-progress-count").textContent = `${completed} / ${total} checked`;
    $("connection-progress-bar").style.width = `${total ? Math.min(100, completed / total * 100) : 0}%`;
    $("connection-progress-note").textContent = `${Number(job.deleted) || 0} deleted · ${Number(job.skipped) || 0} kept. ` + (job.error || (running ? "Each listing is checked again before deletion. Keep the app open." : "Only verified paused or zero-stock listings were deleted."));
    $("connection-results").replaceChildren(...(Array.isArray(job.items) ? job.items : []).filter((item) => !["queued", "checking", "skipped"].includes(item.state)).map((item) => node("div", ["failed", "uncertain"].includes(item.state) ? "manager-result-failed" : "", `${toText(item.title) || "Listing"} #${toText(item.id)} · ${toText(item.state)}: ${toText(item.message)}`)));
    $("connection-stop-btn").hidden = !running;
    $("connection-stop-btn").disabled = state.connectionStopping || !state.connectionJobId;
  }

  async function pollConnection() {
    let failures = 0;
    while (state.connectionJobId) {
      let job;
      try {
        job = await api(`/api/connection_job?id=${encodeURIComponent(state.connectionJobId)}`);
        failures = 0;
      } catch (error) {
        failures++;
        $("connection-progress-note").textContent = error.message + " Cleanup may still be running. No connection or deletion will be resubmitted automatically.";
        if (failures >= 3 || error.status === 403 || error.status === 404) throw error;
        await new Promise((resolve) => setTimeout(resolve, 1500)); continue;
      }
      connectionProgress(job);
      if (job.state !== "running") {
        if (!["completed", "stopped", "failed"].includes(job.state)) throw new Error("The cleanup result could not be confirmed. Check your FunPay listings before reconnecting.");
        try { sessionStorage.removeItem(CONNECTION_PENDING_KEY); } catch { /* Storage is optional. */ }
        state.connectionJobId = null;
        if (job.result) applyConnection(job.result);
        else message(job.error || "Connection cleanup could not finish. Check the results before reconnecting.");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  async function stopConnection() {
    if (!state.connectionJobId || state.connectionStopping) return;
    state.connectionStopping = true; $("connection-stop-btn").disabled = true;
    try {
      await api("/api/connect_stop", { job_id: state.connectionJobId });
      $("connection-progress-note").textContent = "Stopping after the current listing check or deletion…";
    } catch (error) { state.connectionStopping = false; message(error.message); $("connection-stop-btn").disabled = false; }
  }

  async function connect(event) {
    event?.preventDefault(); if (editingBlocked()) return;
    const key = $("golden-key").value.trim(); const category = $("category").value.trim();
    if (!key) { message("Paste your Golden Key first."); $("golden-key").focus(); return; }
    if (!category) { message("Enter a category ID or paste its FunPay /lots/ID/ URL."); $("category").focus(); return; }
    state.busy = true; update(); message(""); $("connect-btn").firstChild.textContent = "Loading category ";
    try {
      const data = await api("/api/connect", { golden_key: key, category });
      if (data.connection_job_id) {
        state.connectionJobId = toText(data.connection_job_id); state.connectionStopping = false;
        $("golden-key").value = ""; $("golden-key").type = "password";
        try { sessionStorage.setItem(CONNECTION_PENDING_KEY, "1"); } catch { /* Storage is optional. */ }
        connectionProgress({ state: "running", phase: "authenticating" }); await pollConnection();
      } else applyConnection(data);
    } catch (error) { message(error.message + (state.connectionJobId || !error.status ? " Cleanup may still be running. Check your listings before reconnecting; nothing was retried." : "")); }
    finally { state.connectionJobId = null; state.busy = false; $("connect-btn").firstChild.textContent = "Load category "; update(); }
  }

  async function disconnect() {
    if (editingBlocked()) return;
    state.busy = true; update();
    try {
      await api("/api/disconnect", {});
      state.connected = false; state.account = ""; state.category = null; state.schema = []; state.creationAvailable = true; state.connectionMessage = ""; invalidateQuote(); invalidateCapacity();
      $("golden-key").value = ""; $("golden-key").type = "password"; $("golden-key").placeholder = "Paste your golden_key cookie";
      renderFields(); renderRows(); toast("Disconnected. Golden Key cleared from memory.");
    } catch (error) { message(error.message); }
    finally { state.busy = false; update(); }
  }

  function buildBatch() {
    if (editingBlocked() || state.hasRun) return;
    const lines = currentLines();
    if (!lines.length) { message("Add at least one item, one per line."); return; }
    if (lines.length > LISTING_LIMIT) { message(`This list has ${lines.length} items. A category can hold at most ${LISTING_LIMIT} active or paused listings in total. Remove extra items before building your batch.`); return; }
    const existing = [...state.rows];
    state.rows = lines.map((item) => {
      const oldIndex = existing.findIndex((row) => row.source === item);
      const row = oldIndex >= 0 ? existing.splice(oldIndex, 1)[0] : { id: makeId(), source: item, price: $("default-price").value, state: "queued", custom: false, price_mode: state.priceMode, quote: null };
      applyTemplates(row); return row;
    });
    invalidateQuote(); message(""); renderRows(); saveDraft(); update();
    toast(`${state.rows.length} ${state.rows.length === 1 ? "listing" : "listings"} added to your batch.`);
    $("preview").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function openEdit(id) {
    const row = state.rows.find((item) => item.id === id); if (!row) return;
    state.editId = id;
    $("edit-heading").textContent = row.source || row.title_en || "Listing details";
    const readonly = editingBlocked() || locked(row);
    for (const [key, target] of Object.entries({ title_ru: "edit-title-ru", title_en: "edit-title-en", description_ru: "edit-desc-ru", description_en: "edit-desc-en" })) {
      $(target).value = row[key]; $(target).readOnly = readonly;
    }
    $("save-listing-btn").hidden = readonly;
    $("edit-dialog").showModal();
  }

  async function review() {
    if (publishReason()) { update(); return; }
    if (!quoteValid()) { await startQuote(); return; }
    if (await checkCapacity()) openReview();
  }

  function openReview() {
    if (publishReason() || !quoteValid()) { update(); return; }
    const rows = eligible();
    $("review-account").textContent = state.account;
    $("review-category").textContent = `${state.category?.name || "Category"} · #${state.category?.id || ""}`;
    $("review-count").textContent = `${rows.length} ${rows.length === 1 ? "listing" : "listings"}`;
    $("review-value-label").textContent = "Total buyer price (actual)";
    $("review-value").textContent = quoteMoney(sumAmounts(rows.map((row) => row.quote.buyer_price_rub)));
    $("review-net-summary").hidden = false;
    $("review-net-value").textContent = money(rows.reduce((sum, row) => sum + priceNumber(row.quote.seller_price), 0), state.currency);
    $("review-buyer-heading").textContent = "BUYER / YOU RECEIVE";
    $("review-table-body").replaceChildren(...rows.map((row) => {
      const tr = node("tr"); const td = node("td");
      td.append(node("div", "item-title", row.title_ru), node("div", "item-subtitle", row.title_en));
      const priceCell = node("td");
      priceCell.append(node("div", "review-price-main", `${quoteMoney(row.quote.buyer_price_rub)} buyer`));
      priceCell.append(node("div", "review-price-net", `${money(row.quote.seller_price, row.quote.seller_currency || state.currency)} you receive`));
      if (!row.quote.exact) priceCell.append(node("div", "review-price-target", `Target ${money(row.price, "RUB")} · ${row.quote.note || "closest of prices checked"}`));
      tr.append(td, priceCell); return tr;
    }));
    const rounded = rows.some((row) => !row.quote.exact);
    $("review-pricing-note").hidden = false;
    $("review-pricing-note").classList.toggle("warning", rounded);
    $("review-pricing-note").textContent = rounded ? "Some targets needed rounding. Check the actual buyer prices above before publishing. Other payment methods can cost more." : "Checked with FunPay's live calculator. Other payment methods can cost more.";
    $("accept-rounded-prices").checked = false;
    $("rounding-acceptance").hidden = !rounded;
    $("retry-note").hidden = !state.hasRun;
    $("confirm-publish-btn").disabled = rounded;
    $("review-dialog").showModal();
  }

  async function startQuote() {
    if (editingBlocked() || publishReason()) return;
    if (!await checkCapacity()) return;
    invalidateQuote(); state.quoteRunning = true; state.quoteCancelRequested = false; state.quotePollFailures = 0;
    const generation = ++state.quoteGeneration;
    $("quote-progress").hidden = false; $("quote-progress-title").textContent = "Calculating buyer prices";
    $("quote-progress-count").textContent = "0 / " + new Set(eligible().map((row) => priceKey(row.price))).size;
    $("quote-progress-note").textContent = "Checking FunPay's live price calculator. No listings are being created.";
    message(""); update(); renderRows();
    $("quote-progress").scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      const data = await api("/api/quote", { prices: [...new Set(eligible().map((row) => priceKey(row.price)))], price_mode: "buyer_rub" });
      if (!data.quote_id) throw new Error("The local app did not return a price check ID. Try the price check again.");
      if (generation !== state.quoteGeneration) return;
      state.quoteId = toText(data.quote_id); update(); pollQuote(generation);
    } catch (error) {
      if (generation !== state.quoteGeneration) return;
      state.quoteRunning = false; state.quoteId = null;
      $("quote-progress").hidden = true; message(error.message); renderRows(); update();
    }
  }

  async function pollQuote(generation) {
    clearTimeout(state.quoteTimer);
    if (generation !== state.quoteGeneration || !state.quoteId) return;
    try {
      const data = await api(`/api/quote?id=${encodeURIComponent(state.quoteId)}`);
      if (generation !== state.quoteGeneration) return;
      state.quotePollFailures = 0;
      $("quote-progress-count").textContent = `${Number(data.completed) || 0} / ${Number(data.total) || 0}`;
      if (data.state === "running") { state.quoteTimer = setTimeout(() => pollQuote(generation), 1000); return; }
      if (data.state === "completed" && !state.quoteCancelRequested) {
        const quotes = data.quotes || {};
        const assignments = eligible().map((row) => [row, cleanQuote(quotes[priceKey(row.price)])]);
        if (assignments.some(([, quote]) => !quote || quote.seller_currency !== state.currency)) throw new Error("FunPay returned incomplete price results or a different seller currency. Reconnect and check prices again.");
        for (const [row, quote] of assignments) { row.quote = quote; applyTemplates(row); }
        state.quoteExpiresAt = Date.now() + Math.max(0, Math.min(300, Number(data.expires_in) || 0)) * 1000;
        if (state.quoteExpiresAt <= Date.now()) throw new Error("The price check expired. Check prices again before publishing.");
        state.quoteRunning = false;
        $("quote-progress").hidden = true; renderRows(); saveDraft(); update();
        state.quoteExpiryTimer = setTimeout(() => {
          state.quoteExpiresAt = 0;
          if ($("review-dialog").open) {
            $("confirm-publish-btn").disabled = true;
            $("review-pricing-note").hidden = false; $("review-pricing-note").classList.add("warning");
            $("review-pricing-note").textContent = "This price check has expired. Keep editing, then check prices again before publishing.";
          }
          update();
        }, Math.max(0, state.quoteExpiresAt - Date.now()));
        openReview(); return;
      }
      state.quoteRunning = false; invalidateQuote(); $("quote-progress").hidden = true;
      if (data.state === "cancelled" || state.quoteCancelRequested) toast("Price check cancelled. No listings were created.");
      else message(data.error || "The price check failed. Review your prices and try again.");
      state.quoteCancelRequested = false; renderRows(); update();
    } catch (error) {
      if (generation !== state.quoteGeneration) return;
      state.quotePollFailures++;
      if (!error.status && state.quotePollFailures <= 3) {
        $("quote-progress-note").textContent = error.message + " Reconnecting to this price check…";
        state.quoteTimer = setTimeout(() => pollQuote(generation), 1500); return;
      }
      state.quoteRunning = false; state.quoteCancelRequested = false; invalidateQuote();
      $("quote-progress").hidden = true; message(error.message); renderRows(); update();
    }
  }

  async function cancelQuote() {
    if (!state.quoteRunning || !state.quoteId || state.quoteCancelRequested) return;
    state.quoteCancelRequested = true; update();
    $("quote-progress-title").textContent = "Cancelling price check";
    $("quote-progress-note").textContent = "Waiting for the current calculator request to finish…";
    try { await api("/api/cancel_quote", { quote_id: state.quoteId }); }
    catch (error) { state.quoteCancelRequested = false; message(error.message); update(); }
  }

  async function startJob() {
    if (editingBlocked() || publishReason()) return;
    if (!quoteValid()) {
      $("review-dialog").close(); invalidateQuote(); message("This price check expired. Check prices again before publishing."); update(); return;
    }
    if (eligible().some((row) => !row.quote.exact) && !$("accept-rounded-prices").checked) {
      $("accept-rounded-prices").focus(); return;
    }
    $("confirm-publish-btn").disabled = true;
    state.busy = true; update(); message("");
    const submitted = eligible();
    try {
      const data = await api("/api/start", {
        items: submitted.map((row) => ({ id: row.id, title_ru: row.title_ru, title_en: row.title_en, description_ru: row.description_ru, description_en: row.description_en, price: toText(priceNumber(row.price)) })),
        fields: categoryPayload(), delay: Number($("delay").value), active: $("active-listings").checked,
        price_mode: "buyer_rub", quote_id: state.quoteId,
        accept_rounded_prices: $("accept-rounded-prices").checked
      });
      if (!data.job_id) throw new Error("The local app did not return a batch ID. Check FunPay before trying again.");
      state.jobId = toText(data.job_id); state.running = true; state.hasRun = true; state.pollFailures = 0;
      invalidateCapacity();
      for (const row of submitted) { row.state = "queued"; row.message = ""; }
      $("review-dialog").close(); $("job-progress").hidden = false;
      $("progress-title").textContent = "Publishing your batch";
      $("progress-note").textContent = "Keep this app open while your batch publishes.";
      saveDraft(); renderRows(); pollJob();
    } catch (error) {
      invalidateCapacity();
      if (error.status === 409) invalidateQuote();
      if (!error.status) {
        state.hasRun = true;
        for (const row of submitted) {
          row.state = "uncertain";
          row.message = "The batch request result could not be confirmed. Check your FunPay listings before creating another batch.";
        }
        saveDraft(); renderRows();
      }
      renderRows(); message(error.message);
      $("review-dialog").close();
    } finally { state.busy = false; $("confirm-publish-btn").disabled = false; update(); }
  }

  async function pollJob() {
    clearTimeout(state.pollTimer); if (!state.jobId) return;
    try {
      const job = await api(`/api/job?id=${encodeURIComponent(state.jobId)}`);
      invalidateCapacity();
      state.pollFailures = 0;
      if (!state.account && job.account) state.account = toText(job.account);
      const records = new Map((Array.isArray(job.items) ? job.items : []).map((row) => [toText(row.id), row]));
      for (const row of state.rows) {
        const result = records.get(row.id);
        if (!result) continue;
        if (Object.hasOwn(statusLabels, result.state)) row.state = result.state;
        row.message = toText(result.message); row.url = safeUrl(result.url);
        if (["buyer_rub", "seller"].includes(result.price_mode)) row.price_mode = result.price_mode;
        if (goodPrice(result.seller_price)) {
          row.seller_price = priceKey(result.seller_price);
          row.seller_currency = toText(result.seller_currency || row.seller_currency || state.currency);
        }
        if (row.price_mode === "buyer_rub" && result.seller_price && result.buyer_price_rub) {
          const actual = cleanQuote({
            target_rub: row.price, seller_price: result.seller_price, buyer_price_rub: result.buyer_price_rub,
            seller_currency: result.seller_currency || row.quote?.seller_currency || state.currency,
            exact: priceNumber(result.buyer_price_rub) === priceNumber(row.price), note: row.quote?.note || ""
          });
          if (actual) row.quote = actual;
        }
      }
      state.running = job.state === "running" || job.state === "stopping";
      state.hasRun = true;
      const total = Number(job.total) || records.size;
      const completed = Number(job.completed) || 0;
      $("job-progress").hidden = false;
      $("progress-count").textContent = `${completed} / ${total}`;
      $("progress-bar").style.width = `${total ? Math.min(100, completed / total * 100) : 0}%`;
      $("progress-title").textContent = state.running ? "Publishing your batch" : job.state === "stopped" ? "Batch stopped" : "Batch finished";
      const failed = state.rows.filter((row) => row.state === "failed").length;
      const uncertain = state.rows.filter((row) => row.state === "uncertain").length;
      const published = state.rows.filter((row) => row.state === "published").length;
      $("progress-note").textContent = state.running ? "Keep this app open while your batch publishes." : `${published} published${failed ? ` · ${failed} failed — review the message before retrying` : ""}${uncertain ? ` · ${uncertain} uncertain — check FunPay before making another listing` : ""}.`;
      saveDraft(); renderRows(); update();
      if (state.running) state.pollTimer = setTimeout(pollJob, 1200);
    } catch (error) {
      state.pollFailures++;
      $("job-progress").hidden = false;
      $("progress-title").textContent = "Reconnecting to the local app…";
      $("progress-note").textContent = error.message + " Publishing may still be running. Don't start another batch.";
      if (state.pollFailures >= 3 && error.status === 404) {
        for (const row of state.rows) {
          if (row.state === "publishing" || row.state === "queued" && state.hasRun) {
            row.state = "uncertain"; row.message = "The previous job could not be recovered. Check FunPay before creating another listing.";
          }
        }
        state.jobId = null; state.running = false; saveDraft(); renderRows(); update();
        message("The previous batch is unavailable. Any listing without a confirmed result is locked so it cannot be accidentally duplicated. Check your listings on FunPay.");
        return;
      }
      state.pollTimer = setTimeout(pollJob, Math.min(10000, 1500 * state.pollFailures));
    }
  }

  async function stopJob() {
    if (!state.jobId || state.busy) return;
    state.busy = true; update();
    try {
      await api("/api/stop", { job_id: state.jobId });
      $("progress-note").textContent = "Stopping after the current listing finishes…";
      toast("Stop requested. Waiting for the current listing.");
    } catch (error) { message(error.message); }
    finally { state.busy = false; update(); }
  }

  function resetBatch() {
    if (editingBlocked()) return;
    if (state.rows.length && !confirm("Start a new batch? The current batch will be cleared. Export it first if you want to keep the listing results.")) return;
    clearTimeout(state.pollTimer); state.rows = []; state.hasRun = false; state.jobId = null; state.priceMode = "buyer_rub"; invalidateQuote();
    $("items-input").value = ""; $("default-price").value = ""; $("job-progress").hidden = true;
    message(""); renderRows(); saveDraft(); update(); toast("New batch ready.");
  }

  function bind() {
    for (const id of ["title-ru", "title-en", "edit-title-ru", "edit-title-en"]) $(id).maxLength = 500;
    for (const id of ["description-ru", "description-en", "edit-desc-ru", "edit-desc-en"]) $(id).maxLength = 20000;
    $("connect-form").addEventListener("submit", connect);
    $("disconnect-btn").addEventListener("click", disconnect);
    $("connection-stop-btn").addEventListener("click", stopConnection);
    $("reveal-key").addEventListener("click", () => {
      const reveal = $("golden-key").type === "password";
      $("golden-key").type = reveal ? "text" : "password";
      $("reveal-key").setAttribute("aria-label", reveal ? "Hide Golden Key" : "Show Golden Key");
      $("reveal-key").title = reveal ? "Hide Golden Key" : "Show Golden Key";
    });
    $("clear-key").addEventListener("click", () => {
      if (state.connected) { disconnect(); return; }
      $("golden-key").value = ""; $("golden-key").type = "password"; $("golden-key").focus();
    });
    for (const lang of ["ru", "en"]) $("tab-" + lang).addEventListener("click", () => {
      for (const other of ["ru", "en"]) {
        $("tab-" + other).classList.toggle("active", lang === other);
        $("tab-" + other).setAttribute("aria-selected", String(lang === other));
        $("panel-" + other).hidden = lang !== other;
      }
    });
    for (const id of formIds) {
      $(id).addEventListener("input", () => { saveDraft(); update(); });
      if (/^(title|description)-/.test(id)) $(id).addEventListener("focus", () => { state.lastFocusedTemplate = $(id); });
    }
    $("active-listings").addEventListener("change", () => { saveDraft(); update(); });
    document.querySelectorAll("[data-token]").forEach((button) => button.addEventListener("click", () => {
      const fallback = $("panel-ru").hidden ? $("description-en") : $("description-ru");
      const input = state.lastFocusedTemplate || fallback;
      if (input.disabled || input.closest("[hidden]")) return;
      const start = input.selectionStart ?? input.value.length; const end = input.selectionEnd ?? start;
      input.setRangeText(button.dataset.token, start, end, "end"); input.focus(); saveDraft();
    }));
    $("build-btn").addEventListener("click", buildBatch);
    $("apply-prices").addEventListener("click", () => {
      if (editingBlocked()) return;
      const price = $("default-price").value;
      if (!goodPrice(price)) { message("Enter a default price greater than zero first."); $("default-price").focus(); return; }
      invalidateQuote();
      for (const row of state.rows) if (!locked(row)) { row.price = price; applyTemplates(row); }
      renderRows(); saveDraft(); update(); toast(state.rows.length ? "Price applied to all editable items." : "Default price ready for your batch.");
    });
    $("edit-form").addEventListener("submit", (event) => {
      event.preventDefault(); const row = state.rows.find((item) => item.id === state.editId);
      if (!row || locked(row) || editingBlocked()) return;
      row.title_ru = $("edit-title-ru").value; row.title_en = $("edit-title-en").value;
      row.description_ru = $("edit-desc-ru").value; row.description_en = $("edit-desc-en").value; row.custom = true;
      saveDraft(); renderRows(); update(); $("edit-dialog").close(); toast("Listing details saved.");
    });
    document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
    $("publish-btn").addEventListener("click", review);
    $("confirm-publish-btn").addEventListener("click", startJob);
    $("accept-rounded-prices").addEventListener("change", () => {
      $("confirm-publish-btn").disabled = !quoteValid() || eligible().some((row) => !row.quote.exact) && !$("accept-rounded-prices").checked;
    });
    $("cancel-quote-btn").addEventListener("click", cancelQuote);
    $("stop-btn").addEventListener("click", stopJob);
    $("new-batch-btn").addEventListener("click", resetBatch);
    $("demo-btn").addEventListener("click", () => {
      if (editingBlocked() || state.hasRun) return;
      if (state.rows.length && !confirm("Replace your current draft with an example?")) return;
      if (!state.connected) $("category").value = "1234";
      $("title-ru").value = "{item} · Быстрая доставка";
      $("title-en").value = "{item} · Fast delivery";
      $("description-ru").value = "Товар: {item}\nЦена: {price}\n\nПосле оплаты напишите мне в чат. Перед покупкой уточните наличие.";
      $("description-en").value = "Item: {item}\nPrice: {price}\n\nMessage me after payment. Please check availability before buying.";
      $("items-input").value = "Starter bundle\nAdventure bundle\nCollector bundle";
      $("default-price").value = "10"; state.rows = []; state.priceMode = "buyer_rub"; invalidateQuote(); buildBatch();
      state.rows[1].price = "20"; state.rows[2].price = "35";
      state.rows.forEach(applyTemplates); renderRows(); saveDraft(); update();
      message("Example draft only. Replace the sample category and listings, then connect your own account when you're ready.", true);
    });
    $("export-btn").addEventListener("click", () => {
      const data = draft(); data.job_id = null;
      if (state.running) {
        for (const row of data.rows) if (["queued", "publishing"].includes(row.state)) {
          row.state = "uncertain";
          row.message = "Exported while the batch was running. Check FunPay to confirm this listing's result.";
        }
      }
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob); const a = node("a"); a.href = url;
      a.download = `listcraft-draft-${new Date().toISOString().slice(0, 10)}.json`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); toast("Draft exported. Your Golden Key is not included.");
    });
    $("import-btn").addEventListener("click", () => $("import-file").click());
    $("import-file").addEventListener("change", async () => {
      const file = $("import-file").files[0]; if (!file || editingBlocked()) return;
      try {
        if (file.size > 5000000) throw new Error("This file is too large. Choose a draft smaller than 5 MB.");
        const data = JSON.parse(await file.text());
        if (state.rows.length && !confirm("Replace your current batch with this imported draft?")) return;
        if (state.connected) await disconnect();
        clearTimeout(state.pollTimer); const resetPrices = restoreDraft(data, true); $("job-progress").hidden = true; saveDraft();
        message("Draft imported. Reconnect to its category before publishing." + (resetPrices ? " Re-enter buyer prices in RUB; older seller amounts were cleared." : ""), true); toast("Draft imported.");
      } catch (error) { message(error instanceof SyntaxError ? "This file isn't valid JSON. Choose an exported Listcraft draft." : error.message); }
      finally { $("import-file").value = ""; }
    });
    $("clear-draft-btn").addEventListener("click", () => {
      if (editingBlocked() || !confirm("Clear this saved draft and reset the batch? Your current connection will stay open.")) return;
      clearTimeout(state.pollTimer); state.rows = []; state.hasRun = false; state.jobId = null; state.fields = {}; state.priceMode = "buyer_rub"; invalidateQuote();
      for (const id of formIds) if (id !== "category") $(id).value = id.startsWith("title-") ? "{item}" : id === "delay" ? "3" : "";
      $("active-listings").checked = true; $("job-progress").hidden = true;
      try { localStorage.removeItem(DRAFT_KEY); } catch { /* Storage may be unavailable. */ }
      renderFields(); renderRows(); update(); message(""); $("draft-status").textContent = "Saved draft cleared"; toast("Saved draft cleared.");
    });
    window.addEventListener("beforeunload", (event) => {
      if (state.running || state.quoteRunning || state.managementBusy || state.connectionJobId) { event.preventDefault(); event.returnValue = ""; }
    });
  }

  async function initialize() {
    bind();
    try {
      const response = await fetch("/api/config", { cache: "no-store" });
      if (!response.ok) throw new Error("The local app could not start. Close this tab, then open the Windows launcher again.");
      state.config = await response.json();
      if (!state.config.token) throw new Error("The app returned an invalid session. Restart it and try again.");
    } catch (error) { message(error.message); update(); return; }
    try {
      const saved = localStorage.getItem(DRAFT_KEY);
      if (saved) restoreDraft(JSON.parse(saved));
    } catch { message("Your saved draft couldn't be restored. You can import an exported draft or start a new one."); }
    try {
      if (sessionStorage.getItem(CONNECTION_PENDING_KEY)) message("A connection cleanup was in progress before this page reloaded. It may still be running. Check your FunPay listings before reconnecting. No cleanup was resubmitted.");
    } catch { /* Storage is optional. */ }
    if (state.jobId) {
      state.running = true; update(); renderRows(); await pollJob();
    } else {
      for (const row of state.rows) if (row.state === "publishing") {
        row.state = "uncertain"; row.message = "Check FunPay to confirm whether this listing was created.";
      }
      renderRows(); update();
    }
  }
  window.Listcraft = {
    api,
    getSession: () => ({ connected: state.connected, account: state.account, category: state.category ? { ...state.category } : null, currency: state.currency, creationAvailable: state.creationAvailable, busy: editingBlocked(), batchRunning: state.running, ready: !!state.config }),
    setManagementBusy: (value) => {
      if (value && editingBlocked() && !state.managementBusy) return false;
      if (value && !state.managementBusy) invalidateQuote();
      invalidateCapacity();
      state.managementBusy = !!value; update(); return true;
    }
  };
  initialize();
})();
