"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.Listcraft;
  if (!bridge) return;
  const model = {
    items: [], selected: new Set(), loaded: false, identity: "", holdsLock: false,
    preparing: false, prepared: [], kind: "", editor: null, fields: {}, pending: null,
    jobId: null, jobRunning: false, jobStopRequested: false, jobTimer: null, jobFailures: 0,
    quoteId: null, quote: null, quoteRunning: false, quoteCancel: false, quoteTimer: null,
    quoteExpiresAt: 0, quoteExpiryTimer: null, quoteFailures: 0, uncertain: new Set(), results: new Map()
  };
  const text = (value) => value == null ? "" : String(value);
  const session = () => bridge.getSession();
  const blocked = () => model.preparing || model.jobRunning || model.quoteRunning;
  const idOf = (value) => text(value.id);
  const titleOf = (value) => text(value.title_ru || value.title_en || `Listing #${value.id}`);
  const priceNum = (value) => Number(text(value).trim().replace(",", "."));
  const validPrice = (value) => Number.isFinite(priceNum(value)) && priceNum(value) > 0 && priceNum(value) <= 1000000000 && Math.abs(priceNum(value) * 100 - Math.round(priceNum(value) * 100)) < 0.00001;
  const priceKey = (value) => priceNum(value).toFixed(2);
  const currency = () => session().currency || "account currency";
  const money = (value, unit = currency()) => `${text(value)} ${unit}`;
  const hasPriceChange = (pending = model.pending) => !!pending?.items?.some((item) => Object.hasOwn(item.changes || {}, "price"));
  const priceInput = () => $(model.kind === "edit" ? "manager-edit-price" : "manager-price-value");
  const quoteMessageId = () => model.kind === "edit" ? "manager-edit-message" : "manager-price-message";
  const quoteLabelId = () => model.kind === "edit" ? "manager-edit-quote-label" : "manager-quote-label";
  const boolean = (value) => value === true || value === 1 || value === "1" || value === "on";
  const element = (tag, className, content) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content != null) el.textContent = content;
    return el;
  };
  const safeUrl = (value) => {
    try { const url = new URL(value); return url.protocol === "https:" && (url.hostname === "funpay.com" || url.hostname.endsWith(".funpay.com")) ? url.href : ""; } catch { return ""; }
  };

  function showMessage(value, target = "manager-message", success = false) {
    $(target).textContent = value;
    $(target).className = `message${success ? " success" : ""}`;
    $(target).hidden = !value;
  }

  function acquire() {
    if (!session().connected) { showMessage("Connect your FunPay account and category above first."); return false; }
    if (model.holdsLock) return true;
    if (session().busy) { showMessage("Wait for the current batch or price check to finish first."); return false; }
    model.holdsLock = true;
    if (!bridge.setManagementBusy(true)) { model.holdsLock = false; showMessage("Another operation is already running. Try again when it finishes."); return false; }
    return true;
  }

  function release() {
    if (!model.holdsLock) return;
    model.holdsLock = false;
    bridge.setManagementBusy(false);
  }

  function clearQuote() {
    clearTimeout(model.quoteExpiryTimer);
    model.quote = null; model.quoteId = null; model.quoteExpiresAt = 0;
  }

  function filtered() {
    const query = $("manager-search").value.trim().toLowerCase();
    return model.items.filter((item) => !query || [item.title_ru, item.title_en, item.id].some((value) => text(value).toLowerCase().includes(query)));
  }

  function updateControls() {
    const current = session();
    const externalBusy = current.busy && !model.holdsLock;
    $("manager-refresh").disabled = !current.connected || externalBusy || model.holdsLock || blocked();
    $("manager-refresh").textContent = model.preparing ? "Loading…" : model.loaded ? "Refresh listings" : "Load my listings";
    $("manager-category-label").textContent = current.connected ? `${current.category?.name || "Connected category"} · ${current.account}` : "Manage the listings in your connected category.";
    $("manager-toolbar").hidden = !model.loaded;
    $("manager-bulk").hidden = !model.selected.size;
    $("manager-selected-count").textContent = `${model.selected.size} selected`;
    const visible = filtered();
    const listingsLabel = model.items.length === 1 ? "listing" : "listings";
    $("manager-count").textContent = visible.length === model.items.length ? `${model.items.length} ${listingsLabel}` : `${visible.length} / ${model.items.length} ${listingsLabel}`;
    $("manager-select-all").checked = !!visible.length && visible.every((item) => model.selected.has(idOf(item)));
    $("manager-select-all").indeterminate = visible.some((item) => model.selected.has(idOf(item))) && !$("manager-select-all").checked;
    $("manager-select-all").disabled = model.holdsLock || externalBusy || !visible.length;
    for (const button of $("manager-bulk").querySelectorAll("button")) button.disabled = model.holdsLock || externalBusy || !current.connected;
    for (const input of $("manager-table-body").querySelectorAll("input,button")) {
      const row = input.closest("tr");
      input.disabled = model.holdsLock || externalBusy || !current.connected || model.uncertain.has(row?.dataset.offerId);
    }
    $("manager-empty").hidden = model.loaded && visible.length > 0;
    $("manager-table-wrap").hidden = !model.loaded || !visible.length;
    if (model.loaded) {
      $("manager-empty").querySelector("h3").textContent = model.items.length ? "No listings match your search" : "No listings in this category yet";
      $("manager-empty").querySelector("p").textContent = model.items.length ? "Try another title or listing ID." : "Create a batch below, or refresh after adding listings on FunPay.";
    }
    $("manager-quote-progress").hidden = !model.quoteRunning || model.kind !== "price";
    $("manager-edit-quote-progress").hidden = !model.quoteRunning || model.kind !== "edit";
    for (const input of $("manager-price-form").querySelectorAll("input,select")) input.disabled = model.quoteRunning;
    for (const input of $("manager-edit-form").querySelectorAll("input,select,textarea")) input.disabled = model.quoteRunning;
    $("manager-edit-stock").disabled = model.quoteRunning || model.editor?.stock_editable !== true;
    syncFields();
    $("manager-review-edit").disabled = model.quoteRunning;
    $("manager-review-price").disabled = model.quoteRunning;
    $("manager-cancel-quote").disabled = !model.quoteId || model.quoteCancel;
    $("manager-edit-cancel-quote").disabled = !model.quoteId || model.quoteCancel;
    $("manager-operation-progress").hidden = !model.jobRunning;
    $("manager-operation-stop").disabled = model.jobStopRequested;
    $("manager-review-back").disabled = model.jobRunning;
    $("manager-confirm").disabled = blocked() || !model.pending || (hasPriceChange() && (!quoteValid() || model.quote?.exact !== true && !$("manager-accept-rounding").checked));
    for (const button of document.querySelectorAll("[data-manager-close]")) button.disabled = blocked();
    if (!model.loaded) {
      $("manager-empty").querySelector("h3").textContent = "Your live listings, in one place";
      $("manager-empty").querySelector("p").textContent = "Connect your account above, then load your listings to edit, price, pause, or delete them.";
    }
  }

  function render() {
    const body = $("manager-table-body"); body.replaceChildren();
    for (const item of filtered()) {
      const id = idOf(item); const tr = element("tr"); tr.dataset.offerId = id;
      const pick = element("td"); const checkbox = element("input"); checkbox.type = "checkbox";
      checkbox.checked = model.selected.has(id); checkbox.setAttribute("aria-label", `Select ${titleOf(item)}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && model.selected.size >= 100) { checkbox.checked = false; showMessage("Select up to 100 listings for one operation."); return; }
        if (checkbox.checked) model.selected.add(id); else model.selected.delete(id);
        updateControls();
      }); pick.append(checkbox);
      const titleCell = element("td");
      const heading = element("div", "item-title", titleOf(item));
      if (safeUrl(item.url)) { const a = element("a", "", titleOf(item)); a.href = safeUrl(item.url); a.target = "_blank"; a.rel = "noopener noreferrer"; heading.replaceChildren(a); }
      titleCell.append(heading, element("div", "item-subtitle", `${text(item.title_en)}${item.title_en ? " · " : ""}#${id}`));
      const priceCell = element("td", "", item.seller_price == null ? "—" : money(item.seller_price, item.currency || currency()));
      const statusCell = element("td");
      statusCell.append(element("span", `manager-active${boolean(item.active) ? "" : " paused"}`, boolean(item.active) ? "Active" : "Paused"));
      if (item.amount != null && text(item.amount) !== "") statusCell.append(element("div", "manager-stock", `Stock: ${text(item.amount)}`));
      const result = model.results.get(id);
      if (result) statusCell.append(element("div", "status-message", `${result.state}: ${result.message || ""}`));
      if (model.uncertain.has(id)) statusCell.append(element("div", "status-message", "Refresh listings before another action."));
      const actionsCell = element("td"); const actions = element("div", "manager-row-actions");
      for (const [label, kind, danger] of [["Edit", "edit"], ["Price", "price"], [boolean(item.active) ? "Pause" : "Activate", boolean(item.active) ? "pause" : "activate"], ["Delete", "delete", true]]) {
        const button = element("button", `button ${danger ? "button-danger" : "button-secondary"}`, label); button.type = "button";
        button.setAttribute("aria-label", `${label} ${titleOf(item)}`); button.addEventListener("click", () => prepare([id], kind)); actions.append(button);
      }
      actionsCell.append(actions); tr.append(pick, titleCell, priceCell, statusCell, actionsCell); body.append(tr);
    }
    updateControls();
  }

  async function refresh() {
    if (blocked() || model.holdsLock || !acquire()) return;
    model.preparing = true; updateControls(); showMessage("Loading your listings from FunPay…");
    try {
      const data = await bridge.api("/api/offers");
      if (!Array.isArray(data.items)) throw new Error("FunPay returned an unreadable listing list. Refresh again.");
      model.items = data.items.filter((item) => item && item.id != null).map((item) => ({ ...item, id: idOf(item) }));
      model.loaded = true; model.selected.clear(); model.results.clear(); model.uncertain.clear();
      showMessage(`${model.items.length} ${model.items.length === 1 ? "listing" : "listings"} loaded.`, "manager-message", true);
    } catch (error) { showMessage(error.message); }
    finally { model.preparing = false; release(); render(); }
  }

  async function prepare(ids, kind) {
    if (blocked() || model.holdsLock) return;
    ids = [...new Set(ids.map(text))];
    if (!ids.length || ids.length > 100) { showMessage("Select between 1 and 100 listings for one operation."); return; }
    if (ids.some((id) => model.uncertain.has(id))) { showMessage("Refresh your listings to check the previous result before changing these listings again."); return; }
    if (!acquire()) return;
    model.preparing = true; model.prepared = []; model.kind = kind; model.pending = null; clearQuote(); updateControls();
    try {
      for (const [index, id] of ids.entries()) {
        showMessage(`Preparing ${index + 1} / ${ids.length}: checking the latest listing details…`);
        const offer = await bridge.api(`/api/offer?id=${encodeURIComponent(id)}`);
        if (!offer || idOf(offer) !== id || !offer.revision) throw new Error("This listing could not be verified. Refresh your listings and try again.");
        model.prepared.push(offer);
      }
      showMessage(""); model.preparing = false;
      if (kind === "edit") openEditor(model.prepared[0]);
      else if (kind === "price") openPrice();
      else {
        model.pending = { action: kind === "delete" ? "delete" : "update", price_mode: "buyer_rub", items: model.prepared.map((offer) => ({ id: idOf(offer), revision: offer.revision, changes: kind === "delete" ? {} : { active: kind === "activate" } })) };
        openReview();
      }
    } catch (error) { model.preparing = false; release(); showMessage(error.message); }
    updateControls();
  }

  function enabled(field) {
    return (Array.isArray(field.conditions) ? field.conditions : []).every((condition) => Array.isArray(condition.values) && condition.values.some((value) => text(value).trim().toLowerCase() === text(model.fields[condition.name]).trim().toLowerCase()));
  }

  function syncFields() {
    const fields = new Map((model.editor?.fields || []).map((field) => [field.name, field]));
    for (const wrapper of $("manager-edit-fields").children) {
      const field = fields.get(wrapper.dataset.fieldName); const active = !!field && enabled(field);
      wrapper.hidden = !active;
      for (const input of wrapper.querySelectorAll("input,select,textarea")) input.disabled = !active || model.quoteRunning;
    }
  }

  function renderFields() {
    const container = $("manager-edit-fields"); container.replaceChildren(); model.fields = {};
    for (const field of model.editor?.fields || []) {
      if (!field?.name || field.name === "payment_msg" || field.name.startsWith("fields[payment_msg]")) continue;
      model.fields[field.name] = field.type === "checkbox" ? field.value !== false && field.value !== "" && field.value != null : field.value ?? "";
      if (field.type === "hidden") continue;
      const wrapper = element("div", "field"); wrapper.dataset.fieldName = field.name;
      const id = `manager-field-${container.children.length}`;
      const label = element("label", "", text(field.label || (field.name === "fields[method]" ? "Метод / Способ получения" : field.name))); label.htmlFor = id;
      if (field.required) label.append(element("span", "required", " *"));
      let input;
      if (Array.isArray(field.options) && field.options.length) {
        input = element("select");
        if (!field.options.some((option) => text(option.value) === "")) { const blank = element("option", "", "Choose an option"); blank.value = ""; input.append(blank); }
        for (const option of field.options) { const el = element("option", "", text(option.label ?? option.value)); el.value = text(option.value); input.append(el); }
        input.value = text(model.fields[field.name]); if (input.selectedIndex < 0) input.value = "";
        model.fields[field.name] = input.value;
      } else if (field.type === "textarea") { input = element("textarea"); input.rows = 3; input.value = text(model.fields[field.name]); }
      else { input = element("input"); input.type = field.type === "checkbox" ? "checkbox" : field.type === "number" ? "number" : "text"; if (input.type === "checkbox") input.checked = model.fields[field.name]; else input.value = text(model.fields[field.name]); }
      input.id = id; input.required = !!field.required;
      for (const attr of ["maxlength", "min", "max", "step"]) if (field[attr] != null && field[attr] !== "") input.setAttribute(attr, text(field[attr]));
      input.addEventListener("input", () => { model.fields[field.name] = input.type === "checkbox" ? input.checked : input.value; syncFields(); });
      input.addEventListener("change", () => { model.fields[field.name] = input.type === "checkbox" ? input.checked : input.value; syncFields(); });
      if (input.type === "checkbox") { const checkbox = element("div", "category-checkbox"); checkbox.append(input, label); wrapper.append(checkbox); } else wrapper.append(label, input);
      container.append(wrapper);
    }
    syncFields();
  }

  function fieldPayload() {
    const result = {};
    for (const field of model.editor?.fields || []) {
      if (!enabled(field) || field.name === "payment_msg" || field.name.startsWith("fields[payment_msg]")) continue;
      const value = model.fields[field.name] ?? field.value ?? "";
      if (!field.required && value === "" && field.options?.length && !field.options.some((option) => text(option.value) === "")) continue;
      result[field.name] = value;
    }
    return result;
  }

  function openEditor(offer) {
    model.editor = offer; showMessage("", "manager-edit-message");
    $("manager-edit-heading").textContent = `Edit listing #${offer.id}`;
    for (const [name, id] of Object.entries({ title_ru: "manager-title-ru", title_en: "manager-title-en", description_ru: "manager-desc-ru", description_en: "manager-desc-en" })) $(id).value = text(offer[name]);
    $("manager-edit-price").value = "";
    $("manager-edit-price-label").textContent = "New buyer price (RUB, optional)";
    $("manager-current-net").textContent = `Currently you receive: ${money(offer.seller_price, offer.currency || currency())}.`;
    $("manager-edit-active").checked = boolean(offer.active);
    $("manager-stock-field").hidden = offer.stock_editable !== true;
    $("manager-edit-stock").disabled = offer.stock_editable !== true;
    $("manager-edit-stock").value = Number(offer.amount) >= 1 ? text(offer.amount) : "";
    renderFields(); $("manager-edit-dialog").showModal();
  }

  async function reviewEdit(event) {
    event.preventDefault();
    if (blocked() || !model.editor || !$("manager-edit-form").reportValidity()) return;
    const enteredPrice = $("manager-edit-price").value.trim();
    if (enteredPrice && !validPrice(enteredPrice)) { showMessage("Enter a positive buyer price in RUB with up to two decimal places, or leave it blank to keep the current price.", "manager-edit-message"); return; }
    const changes = {};
    for (const [name, id] of Object.entries({ title_ru: "manager-title-ru", title_en: "manager-title-en", description_ru: "manager-desc-ru", description_en: "manager-desc-en" })) if ($(id).value !== text(model.editor[name])) changes[name] = $(id).value;
    if (enteredPrice) changes.price = priceKey(enteredPrice);
    if ($("manager-edit-active").checked !== boolean(model.editor.active)) changes.active = $("manager-edit-active").checked;
    if (model.editor.stock_editable === true && $("manager-edit-stock").value.trim() && $("manager-edit-stock").value !== text(model.editor.amount)) changes.amount = $("manager-edit-stock").value;
    const fields = fieldPayload();
    if (Object.keys(fields).some((name) => text(fields[name]) !== text((model.editor.fields || []).find((field) => field.name === name)?.value))) changes.fields = fields;
    if (!Object.keys(changes).length) { showMessage("Make a change before reviewing.", "manager-edit-message"); return; }
    model.pending = { action: "update", price_mode: "buyer_rub", items: [{ id: idOf(model.editor), revision: model.editor.revision, changes }] };
    if (enteredPrice) {
      if (quoteValid()) finishPriceReview(); else await startQuote();
    } else { $("manager-edit-dialog").close(); openReview(); }
  }

  function openPrice() {
    $("manager-price-items").textContent = model.prepared.length === 1 ? titleOf(model.prepared[0]) : `One new price for ${model.prepared.length} selected listings.`;
    $("manager-price-value").value = "";
    showMessage("", "manager-price-message"); $("manager-price-dialog").showModal();
  }

  function quoteValid() { return !!model.quoteId && !!model.quote && model.quoteExpiresAt > Date.now() && validPrice(priceInput().value) && model.quote.target_rub === priceKey(priceInput().value); }

  function finishPriceReview() {
    if (!quoteValid()) return;
    if (model.kind === "price") model.pending = { action: "update", price_mode: "buyer_rub", items: model.prepared.map((offer) => ({ id: idOf(offer), revision: offer.revision, changes: { price: priceKey(priceInput().value) } })) };
    if (!model.pending || !hasPriceChange()) return;
    model.pending.price_mode = "buyer_rub"; model.pending.quote_id = model.quoteId;
    $(model.kind === "edit" ? "manager-edit-dialog" : "manager-price-dialog").close(); openReview();
  }

  async function reviewPrice(event) {
    event.preventDefault(); if (blocked() || !$("manager-price-form").reportValidity()) return;
    if (!validPrice($("manager-price-value").value)) { showMessage("Enter a positive buyer price in RUB with up to two decimal places.", "manager-price-message"); return; }
    if (quoteValid()) { finishPriceReview(); return; }
    await startQuote();
  }

  async function startQuote() {
    clearQuote(); model.quoteRunning = true; model.quoteCancel = false; model.quoteFailures = 0;
    showMessage("", quoteMessageId()); $(quoteLabelId()).textContent = "Calculating buyer price…"; updateControls();
    try {
      const data = await bridge.api("/api/quote", { prices: [priceKey(priceInput().value)], price_mode: "buyer_rub" });
      if (!data.quote_id) throw new Error("The price check could not be started. Try again.");
      model.quoteId = text(data.quote_id); updateControls(); await pollQuote();
    } catch (error) { model.quoteRunning = false; clearQuote(); showMessage(error.message, quoteMessageId()); updateControls(); }
  }

  async function pollQuote() {
    clearTimeout(model.quoteTimer); if (!model.quoteId) return;
    try {
      const result = await bridge.api(`/api/quote?id=${encodeURIComponent(model.quoteId)}`); model.quoteFailures = 0;
      if (result.state === "running") { model.quoteTimer = setTimeout(pollQuote, 1000); return; }
      model.quoteRunning = false;
      if (result.state === "completed" && !model.quoteCancel) {
        const quote = result.quotes?.[priceKey(priceInput().value)];
        if (!quote || !validPrice(quote.seller_price) || !/^\d+(?:\.\d+)?$/.test(text(quote.buyer_price_rub)) || Number(quote.buyer_price_rub) <= 0 || quote.seller_currency !== currency()) throw new Error("FunPay returned incomplete pricing. Reconnect and check the price again.");
        model.quote = { ...quote, target_rub: priceKey(priceInput().value) };
        model.quoteExpiresAt = Date.now() + Math.min(300, Math.max(0, Number(result.expires_in) || 0)) * 1000;
        if (!quoteValid()) throw new Error("This price check expired. Check it again before saving.");
        model.quoteExpiryTimer = setTimeout(() => {
          model.quoteExpiresAt = 0;
          if ($("manager-review-dialog").open) showMessage("This price check expired. Go back and check the price again before saving.", "manager-review-message");
          updateControls();
        }, model.quoteExpiresAt - Date.now());
        finishPriceReview(); updateControls(); return;
      }
      clearQuote(); showMessage(model.quoteCancel || result.state === "cancelled" ? "Price check cancelled. No listings were changed." : result.error || "This price check failed. Try again when ready.", quoteMessageId());
      model.quoteCancel = false; updateControls();
    } catch (error) {
      if (!error.status && ++model.quoteFailures <= 3) { $(quoteLabelId()).textContent = "Reconnecting to the price check…"; model.quoteTimer = setTimeout(pollQuote, 1500); return; }
      model.quoteRunning = false; clearQuote(); showMessage(error.message, quoteMessageId()); updateControls();
    }
  }

  async function cancelQuote() {
    if (!model.quoteRunning || !model.quoteId || model.quoteCancel) return;
    model.quoteCancel = true; $(quoteLabelId()).textContent = "Cancelling after the current price request…"; updateControls();
    try { await bridge.api("/api/cancel_quote", { quote_id: model.quoteId }); }
    catch (error) { model.quoteCancel = false; showMessage(error.message, quoteMessageId()); updateControls(); }
  }

  function changeBlock(label, value, warning = false) {
    const block = element("div", warning ? "manager-change-warning" : "");
    block.append(element("span", "manager-change-label", label), element("div", "manager-review-description", value));
    return block;
  }

  function openReview() {
    if (!model.pending) return;
    const deleting = model.pending.action === "delete";
    $("manager-review-heading").textContent = deleting ? `Delete ${model.prepared.length === 1 ? "this listing" : model.prepared.length + " listings"}?` : model.kind === "price" ? "Review new price" : model.kind === "pause" ? "Pause these listings?" : model.kind === "activate" ? "Activate these listings?" : "Review listing changes";
    $("manager-review-intro").textContent = deleting ? "These listings will be deleted from your FunPay account." : "These changes will be applied to your existing FunPay listings.";
    $("manager-review-summary").replaceChildren(...[["Account", session().account], ["Category", session().category?.name || ""], ["Listings", text(model.prepared.length)], ["Action", deleting ? "Delete" : model.kind === "price" ? "Change price" : model.kind === "activate" ? "Activate" : model.kind === "pause" ? "Pause" : "Update"]].map(([label, value]) => { const div = element("div"); div.append(element("span", "", label), element("strong", "", value)); return div; }));
    $("manager-review-list").replaceChildren(...model.prepared.map((offer) => { const div = element("div", "", titleOf(offer)); div.append(element("span", "", `#${offer.id}`)); return div; }));
    const changes = $("manager-review-change"); changes.replaceChildren();
    const priceChanged = hasPriceChange();
    const rounded = priceChanged && model.quote?.exact !== true;
    if (priceChanged) {
      changes.append(changeBlock("Buyer price (actual)", money(model.quote.buyer_price_rub, "RUB")));
      changes.append(changeBlock("You receive per listing", money(model.quote.seller_price, model.quote.seller_currency)));
      if (rounded) changes.append(changeBlock("Requested buyer price", `${money(model.quote.target_rub, "RUB")} · ${text(model.quote.note) || "Closest of prices checked."}`, true));
    }
    if (model.kind === "edit") {
      const patch = model.pending.items[0].changes;
      for (const [name, label] of [["title_ru", "Russian title"], ["title_en", "English title"], ["description_ru", "Russian description"], ["description_en", "English description"], ["amount", "Stock quantity"]]) if (Object.hasOwn(patch, name)) changes.append(changeBlock(label, patch[name]));
      if (!priceChanged) changes.append(changeBlock("Price", "Current price is unchanged."));
      if (Object.hasOwn(patch, "active")) changes.append(changeBlock("Visibility", patch.active ? "Active" : "Paused"));
      if (patch.fields) for (const field of model.editor.fields || []) if (Object.hasOwn(patch.fields, field.name)) {
        const value = patch.fields[field.name]; const option = field.options?.find((item) => text(item.value) === text(value));
        changes.append(changeBlock(text(field.label || field.name), option ? text(option.label) : typeof value === "boolean" ? value ? "Yes" : "No" : text(value)));
      }
    }
    $("manager-rounding-label").hidden = !rounded; $("manager-accept-rounding").checked = false;
    $("manager-review-note").textContent = deleting ? "Deletion removes these listings from FunPay. Check the titles and count above before continuing." : priceChanged ? "All entered prices are buyer prices in RUB. FunPay's live calculator checks the shown buyer price. Other payment methods can cost more. Only the reviewed changes will be sent." : "Only the reviewed changes will be sent. The current price and other listing details are preserved.";
    $("manager-confirm").classList.toggle("button-danger", deleting); $("manager-confirm").classList.toggle("button-primary", !deleting);
    $("manager-confirm").firstChild.textContent = deleting ? `Delete ${model.prepared.length} ${model.prepared.length === 1 ? "listing" : "listings"} ` : "Save changes ";
    $("manager-review-back").textContent = "Go back";
    showMessage("", "manager-review-message"); $("manager-operation-progress").hidden = true;
    $("manager-review-dialog").showModal(); updateControls();
  }

  function closeManager(dialogId) {
    if (blocked()) return;
    $(dialogId).close(); model.pending = null; model.editor = null; model.prepared = []; clearQuote(); release(); updateControls();
  }

  function goBack() {
    if (model.jobRunning) return;
    $("manager-review-dialog").close();
    if (model.kind === "edit") $("manager-edit-dialog").showModal();
    else if (model.kind === "price") { showMessage("", "manager-price-message"); $("manager-price-dialog").showModal(); }
    else { model.pending = null; model.prepared = []; clearQuote(); release(); }
    updateControls();
  }

  async function confirmChanges() {
    if (!model.pending || blocked()) return;
    if (hasPriceChange() && (!quoteValid() || model.quote.exact !== true && !$("manager-accept-rounding").checked)) { updateControls(); return; }
    model.preparing = true; updateControls(); $("manager-confirm").disabled = true;
    const body = { ...model.pending, price_mode: "buyer_rub", accept_rounded_prices: hasPriceChange() && $("manager-accept-rounding").checked };
    showMessage("Sending your reviewed changes…", "manager-review-message");
    try {
      const data = await bridge.api("/api/manage", body);
      if (!data.job_id) throw new Error("The update result could not be confirmed. Refresh FunPay before trying again.");
      model.jobId = text(data.job_id); model.jobRunning = true; model.jobStopRequested = false; model.jobFailures = 0;
      model.preparing = false; showMessage("", "manager-review-message");
      $("manager-operation-label").textContent = `Applying changes · 0 / ${model.prepared.length}`; updateControls(); pollJob();
    } catch (error) {
      model.preparing = false;
      for (const offer of model.prepared) model.uncertain.add(idOf(offer));
      model.pending = null; $("manager-review-back").textContent = "Close results";
      showMessage(`${error.message}\nRefresh your listings before reviewing another action for these items.`, "manager-review-message"); render(); updateControls();
    }
  }

  async function pollJob() {
    clearTimeout(model.jobTimer); if (!model.jobId) return;
    try {
      const job = await bridge.api(`/api/manage_job?id=${encodeURIComponent(model.jobId)}`); model.jobFailures = 0;
      const records = Array.isArray(job.items) ? job.items : [];
      for (const result of records) {
        const id = idOf(result); model.results.set(id, result);
        if (["failed", "uncertain"].includes(result.state)) model.uncertain.add(id);
        const index = model.items.findIndex((item) => idOf(item) === id);
        if (result.state === "deleted" && index >= 0) { model.items.splice(index, 1); model.selected.delete(id); }
        if (result.state === "updated" && index >= 0) {
          const patch = model.pending?.items.find((item) => item.id === id)?.changes || {};
          for (const name of ["title_ru", "title_en", "active", "amount"]) if (Object.hasOwn(patch, name)) model.items[index][name] = patch[name];
          if (result.seller_price != null) model.items[index].seller_price = result.seller_price;
          else if (patch.price && model.quote) model.items[index].seller_price = model.quote.seller_price;
        }
      }
      model.jobRunning = job.state === "running";
      $("manager-operation-label").textContent = `Applying changes · ${Number(job.completed) || 0} / ${Number(job.total) || model.prepared.length}`;
      render();
      if (model.jobRunning) { model.jobTimer = setTimeout(pollJob, 1100); return; }
      finishJob(job, records);
    } catch (error) {
      model.jobFailures++;
      $("manager-operation-label").textContent = "Reconnecting — the current operation may still be running…";
      if (error.status === 404 && model.jobFailures >= 3) {
        for (const offer of model.prepared) model.uncertain.add(idOf(offer));
        model.jobRunning = false;
        showMessage("The operation result could not be recovered. Check FunPay and refresh your listings before trying any affected listing again.", "manager-review-message");
        model.pending = null; $("manager-review-back").textContent = "Close results"; release(); render(); return;
      }
      model.jobTimer = setTimeout(pollJob, Math.min(10000, 1500 * model.jobFailures));
    }
  }

  function finishJob(job, records) {
    const successes = records.filter((item) => ["updated", "deleted"].includes(item.state)).length;
    const failed = records.filter((item) => ["failed", "uncertain"].includes(item.state)).length;
    $("manager-review-heading").textContent = job.state === "stopped" ? "Operation stopped" : "Changes finished";
    showMessage(`${successes} ${model.kind === "delete" ? "deleted" : "updated"}${failed ? ` · ${failed} need a refresh before another action` : ""}.`, "manager-review-message", !failed);
    const resultList = element("div", "manager-result-list");
    for (const result of records) {
      const offer = model.prepared.find((item) => idOf(item) === idOf(result));
      const div = element("div", ["failed", "uncertain"].includes(result.state) ? "manager-result-failed" : "", `${offer ? titleOf(offer) : "#" + result.id} · ${result.state}${result.message ? ": " + result.message : ""}`);
      resultList.append(div);
    }
    $("manager-review-change").replaceChildren(resultList);
    $("manager-rounding-label").hidden = true; $("manager-review-back").textContent = "Close results";
    model.pending = null; clearQuote(); release(); render();
  }

  async function stopJob() {
    if (!model.jobRunning || !model.jobId || model.jobStopRequested) return;
    model.jobStopRequested = true; updateControls();
    try { await bridge.api("/api/manage_stop", { job_id: model.jobId }); $("manager-operation-label").textContent = "Stopping after the current listing finishes…"; }
    catch (error) { model.jobStopRequested = false; showMessage(error.message, "manager-review-message"); updateControls(); }
  }

  function sessionChanged() {
    const current = session(); const identity = current.connected ? `${current.account}:${current.category?.id}` : "";
    if (identity !== model.identity) {
      model.identity = identity; model.items = []; model.loaded = false; model.selected.clear(); model.results.clear(); model.uncertain.clear();
      $("manager-search").value = ""; showMessage(""); render();
    } else updateControls();
  }

  $("manager-refresh").addEventListener("click", refresh);
  $("manager-search").addEventListener("input", render);
  $("manager-select-all").addEventListener("change", () => {
    const chosen = $("manager-select-all").checked;
    for (const item of filtered()) {
      if (model.uncertain.has(idOf(item))) continue;
      if (chosen && model.selected.size < 100) model.selected.add(idOf(item)); else if (!chosen) model.selected.delete(idOf(item));
    }
    if (chosen && filtered().length > 100) showMessage("Selected up to 100 visible listings. Run another operation for the rest."); render();
  });
  for (const kind of ["price", "activate", "pause", "delete"]) $("manager-bulk-" + kind).addEventListener("click", () => prepare([...model.selected], kind));
  $("manager-edit-form").addEventListener("submit", reviewEdit);
  $("manager-price-form").addEventListener("submit", reviewPrice);
  $("manager-price-value").addEventListener("input", clearQuote);
  $("manager-edit-price").addEventListener("input", clearQuote);
  $("manager-cancel-quote").addEventListener("click", cancelQuote);
  $("manager-edit-cancel-quote").addEventListener("click", cancelQuote);
  $("manager-accept-rounding").addEventListener("change", updateControls);
  $("manager-confirm").addEventListener("click", confirmChanges);
  $("manager-review-back").addEventListener("click", () => { if (!model.pending) closeManager("manager-review-dialog"); else goBack(); });
  $("manager-operation-stop").addEventListener("click", stopJob);
  for (const button of document.querySelectorAll("[data-manager-close]")) button.addEventListener("click", () => closeManager(button.dataset.managerClose));
  for (const id of ["manager-edit-dialog", "manager-price-dialog", "manager-review-dialog"]) $(id).addEventListener("cancel", (event) => { event.preventDefault(); closeManager(id); });
  window.addEventListener("listcraft:session", sessionChanged);
  window.addEventListener("beforeunload", (event) => { if (model.jobRunning || model.quoteRunning) { event.preventDefault(); event.returnValue = ""; } });
  sessionChanged();
})();
