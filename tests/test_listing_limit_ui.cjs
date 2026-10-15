const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync("static/app.js", "utf8");

function harness(capacity = { limit: 39, used: 37, remaining: 2, category: { id: "3503" }, account: "Account" }) {
  const nodes = new Map(); const calls = [];
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, { value: "", checked: true, textContent: "", hidden: false, querySelectorAll: () => [], scrollIntoView() {} });
    return nodes.get(id);
  };
  let sequence = 0;
  const context = {
    document: { getElementById: node }, window: {}, crypto: { randomUUID: () => `generated-${++sequence}` },
    clearTimeout() {}, setTimeout() { return 1; }, localStorage: { setItem() {} },
    async fetch(url, options) {
      calls.push({ url, options });
      if (url === "/api/capacity") return { ok: true, status: 200, json: async () => capacity };
      if (url === "/api/quote") return { ok: true, status: 200, json: async () => ({ quote_id: "quote" }) };
      throw new Error(`Unexpected request: ${url}`);
    }
  };
  vm.createContext(context);
  vm.runInContext(source.replace("  initialize();", "  renderRows = () => {}; update = () => {}; pollQuote = () => {}; window.testLimit = {state,restoreDraft,publishReason,checkCapacity,buildBatch,startQuote,capacityNote,invalidateCapacity};"), context);
  const app = context.window.testLimit;
  app.state.config = { token: "token", max_items: 500 };
  app.state.connected = true; app.state.account = "Account"; app.state.category = { id: "3503" }; app.state.currency = "RUB";
  node("delay").value = "3";
  node("title-ru").value = "{item}"; node("title-en").value = "{item}";
  node("description-ru").value = "Описание {item}"; node("description-en").value = "Description {item}";
  node("default-price").value = "100";
  return { app, node, calls };
}

const rows = (count, state = "queued") => Array.from({ length: count }, (_, index) => ({ id: `row-${index}`, source: `Item ${index}`, title_ru: "Товар", title_en: "Item", description_ru: "Описание", description_en: "Description", price: "100", price_mode: "buyer_rub", state }));

(async () => {
  // Neither stale/oversized server config nor missing config can raise the local hard limit.
  for (const configured of [null, { token: "token", max_items: 500 }]) {
    const { app, node } = harness(); app.state.config = configured;
    node("items-input").value = rows(40).map((row) => row.source).join("\n"); app.buildBatch();
    assert.equal(app.state.rows.length, 0); assert.match(node("global-message").textContent, /at most 39/);
    node("items-input").value = rows(39).map((row) => row.source).join("\n"); app.buildBatch();
    assert.equal(app.state.rows.length, 39);
  }
  // Imported historical data survives while oversized eligible subsets never reach the network.
  {
    const { app, calls } = harness(); app.state.config.max_items = 39;
    app.restoreDraft({ version: 1, price_mode: "buyer_rub", rows: rows(41), currency: "RUB" });
    assert.equal(app.state.rows.length, 41); assert.match(app.publishReason(), /at most 39/);
    await app.startQuote(); assert.equal(calls.length, 0);
    app.restoreDraft({ version: 1, price_mode: "buyer_rub", rows: [...rows(40, "published"), ...rows(39).map((row) => ({ ...row, id: `queued-${row.id}` }))], currency: "RUB" });
    assert.equal(app.state.rows.length, 79); assert.equal(app.publishReason(), "");
  }
  // Active+paused existing listings consume category capacity before any quote request.
  {
    const { app, calls, node } = harness(); app.state.rows = rows(3);
    await app.startQuote(); assert.deepEqual(calls.map((call) => call.url), ["/api/capacity"]);
    assert.match(node("global-message").textContent, /37 \/ 39 listings, including paused/);
    assert.match(node("global-message").textContent, /only 2 spaces/);
    assert.equal(app.state.busy, false); assert.equal(app.state.quoteRunning, false);
    app.capacityNote(); assert.match(node("capacity-note").textContent, /2 spaces remaining/);
    // Cached fullness is informational, so a fresh authoritative read is allowed on the next attempt.
    app.state.rows = rows(2); assert.equal(app.publishReason(), "");
    await app.startQuote(); assert.deepEqual(calls.map((call) => call.url), ["/api/capacity", "/api/capacity", "/api/quote"]);
    assert.equal(calls[0].options.headers["X-Lister-Token"], "token");
    app.invalidateCapacity(); app.capacityNote(); assert.match(node("capacity-note").textContent, /checked before/);
  }
  {
    const { app, calls } = harness({ limit: 39, used: 39, remaining: 0, category: { id: "3503" }, account: "Account" });
    app.state.rows = rows(1); await app.startQuote(); assert.equal(calls.length, 1);
  }
  console.log("Listing-limit UI checks passed: hard 39 independent of config, legacy history preserved, oversized eligible rows blocked before network, active+paused capacity checked before quoting, and no stale capacity blocker.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
