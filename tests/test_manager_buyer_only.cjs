const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sourcePath = path.join(__dirname, "..", "static", "manager.js");
const source = fs.readFileSync(sourcePath, "utf8");
const bindings = source.indexOf('  $("manager-refresh").addEventListener');
assert.ok(bindings > 0);
const testSource = source.slice(0, bindings) + "  globalThis.testManager = {model,openEditor,reviewEdit,openPrice,reviewPrice,confirmChanges,hasPriceChange,quoteValid,updateControls};\n})();";

class Node {
  constructor() {
    this.value = ""; this.checked = false; this.hidden = false; this.disabled = false;
    this.children = []; this.dataset = {}; this.firstChild = { textContent: "" };
    this.classList = { toggle() {} }; this.selectors = new Map(); this.open = false;
  }
  append(...values) { this.children.push(...values); }
  replaceChildren(...values) { this.children = values; }
  querySelectorAll() { return []; }
  querySelector(selector) { if (!this.selectors.has(selector)) this.selectors.set(selector, new Node()); return this.selectors.get(selector); }
  setAttribute() {}
  addEventListener() {}
  reportValidity() { return true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function harness() {
  const nodes = new Map(); const calls = [];
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const offer = { id: "1", revision: "fresh-revision", title_ru: "Старый заголовок", title_en: "Old title", description_ru: "Описание", description_en: "Description", seller_price: "90.91", currency: "RUB", active: true, amount: "2", stock_editable: true, fields: [] };
  let quotedTarget;
  const bridge = {
    getSession: () => ({ connected: true, currency: "RUB", account: "Test account", category: { id: "3503", name: "Test category" }, busy: false }),
    setManagementBusy: () => true,
    async api(url, body) {
      calls.push({ url, body: body ? JSON.parse(JSON.stringify(body)) : undefined });
      if (url === "/api/quote") { quotedTarget = body.prices[0]; return { quote_id: "trusted-quote" }; }
      if (url.startsWith("/api/quote?")) return { state: "completed", quotes: { [quotedTarget]: { target_rub: quotedTarget, seller_price: "90.91", buyer_price_rub: "100.001", seller_currency: "RUB", exact: false, note: "Closest of prices checked" } }, expires_in: 300 };
      if (url === "/api/manage") return { job_id: "job-1" };
      if (url.startsWith("/api/manage_job?")) return { state: "completed", total: 1, completed: 1, items: [{ id: "1", state: "updated", message: "Saved", seller_price: "90.91", buyer_price_rub: "100.001" }] };
      throw new Error(`Unexpected test request: ${url}`);
    }
  };
  const context = {
    window: { Listcraft: bridge }, document: { getElementById: get, createElement: () => new Node(), querySelectorAll: () => [] },
    clearTimeout() {}, setTimeout() { return 1; }, URL
  };
  vm.createContext(context); vm.runInContext(testSource, context);
  const app = context.testManager;
  app.model.prepared = [offer]; app.model.kind = "edit"; app.model.holdsLock = true; app.model.items = [{ ...offer }];
  app.openEditor(offer);
  return { app, get, calls, offer };
}

const event = { preventDefault() {} };
(async () => {
  {
    const { app, get, calls } = harness();
    assert.equal(get("manager-edit-price").value, "", "Seller net must never prefill an editable price");
    assert.match(get("manager-current-net").textContent, /you receive: 90\.91 RUB/);
    get("manager-title-en").value = "New title";
    get("manager-desc-ru").value = "Новое описание";
    get("manager-edit-price").value = "100";
    await app.reviewEdit(event);
    assert.equal(calls[0].url, "/api/quote");
    assert.deepEqual(calls[0].body, { prices: ["100.00"], price_mode: "buyer_rub" });
    assert.equal(app.model.pending.items[0].changes.price, "100.00");
    assert.equal(app.model.pending.items[0].changes.title_en, "New title");
    assert.equal(app.model.pending.items[0].changes.description_ru, "Новое описание");
    assert.equal(app.model.quote.buyer_price_rub, "100.001", "Actual buyer precision must be visible");
    assert.equal(get("manager-confirm").disabled, true, "Inexact buyer price needs explicit acceptance");
    await app.confirmChanges();
    assert.equal(calls.some((call) => call.url === "/api/manage"), false);
    get("manager-accept-rounding").checked = true;
    await app.confirmChanges();
    const sent = calls.find((call) => call.url === "/api/manage").body;
    assert.equal(sent.price_mode, "buyer_rub"); assert.equal(sent.quote_id, "trusted-quote"); assert.equal(sent.accept_rounded_prices, true);
    assert.equal(sent.items[0].changes.price, "100.00", "Send the buyer target, never the net");
    assert.equal(sent.items[0].changes.title_en, "New title");
  }
  {
    const { app, get, calls } = harness();
    get("manager-title-en").value = "Only a title change";
    await app.reviewEdit(event);
    assert.equal(calls.length, 0, "Blank optional price must not call the calculator");
    assert.equal(Object.hasOwn(app.model.pending.items[0].changes, "price"), false);
    assert.equal(get("manager-confirm").disabled, false);
    await app.confirmChanges();
    const sent = calls.find((call) => call.url === "/api/manage").body;
    assert.equal(Object.hasOwn(sent, "quote_id"), false); assert.equal(Object.hasOwn(sent.items[0].changes, "price"), false);
  }
  {
    const { app, get, calls } = harness();
    get("manager-edit-price").value = "90.91";
    await app.reviewEdit(event);
    assert.deepEqual(calls[0].body.prices, ["90.91"], "Entering the old net number still means a new buyer RUB target");
  }
  {
    const { app, get, calls } = harness();
    app.model.kind = "price"; app.openPrice();
    assert.equal(get("manager-price-value").value, "");
    get("manager-price-value").value = "100";
    await app.reviewPrice(event);
    assert.equal(calls[0].body.price_mode, "buyer_rub"); assert.equal(app.model.pending.price_mode, "buyer_rub");
    assert.equal(app.model.pending.items[0].changes.price, "100.00");
  }
  for (const action of ["delete", "activate", "pause"]) {
    const { app, get, calls, offer } = harness();
    app.model.kind = action; app.model.pending = { action: action === "delete" ? "delete" : "update", price_mode: "buyer_rub", items: [{ id: offer.id, revision: offer.revision, changes: action === "delete" ? {} : { active: action === "activate" } }] };
    app.updateControls(); assert.equal(get("manager-confirm").disabled, false);
    await app.confirmChanges(); assert.equal(calls.some((call) => call.url === "/api/quote"), false);
  }
  const html = fs.readFileSync(path.join(path.dirname(sourcePath), "index.html"), "utf8");
  const missing = [...source.matchAll(/\$\("([\w-]+)"\)/g)].map((match) => match[1]).filter((id) => !html.includes(`id="${id}"`));
  assert.deepEqual([...new Set(missing)], []);
  const dialogs = html.slice(html.indexOf('<dialog id="manager-edit-dialog"'), html.indexOf('<dialog id="edit-dialog"'));
  assert.equal(dialogs.includes('value="seller"'), false);
  assert.match(dialogs, /id="manager-edit-stock"[^>]+min="1"/);
  assert.match(dialogs, /All entered prices are buyer prices in RUB/);
  console.log("Manager buyer-only UI checks passed: optional blank price preserves net, combined edits retain content through live quotes, entered net is still a RUB buyer target, inexact acceptance, buyer-only dedicated editor, and no-price actions skip calculation.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
