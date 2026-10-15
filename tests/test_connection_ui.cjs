const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync("static/app.js", "utf8");

function harness(reply) {
  const nodes = new Map(); const calls = [];
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, { value: "", checked: true, textContent: "", hidden: false, type: "password", firstChild: { textContent: "" }, querySelectorAll: () => [], focus() {} });
    return nodes.get(id);
  };
  const context = {
    document: { getElementById: node }, window: {}, crypto: { randomUUID: () => "id" },
    clearTimeout() {}, setTimeout() { return 1; }, localStorage: { setItem() {} },
    async fetch(url, options) {
      calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
      if (url === "/api/connect") return { ok: true, status: 200, json: async () => reply };
      if (url === "/api/disconnect") return { ok: true, status: 200, json: async () => ({ ok: true }) };
      if (url === "/api/offers") return { ok: true, status: 200, json: async () => ({ items: [{ id: "1" }] }) };
      throw new Error(`Unexpected batch request: ${url}`);
    }
  };
  vm.createContext(context);
  vm.runInContext(source.replace("  initialize();", "  renderFields = () => {}; renderRows = () => {}; update = () => {}; window.testConnection = {state,connect,disconnect,publishReason,startQuote,review,startJob,capacityNote,draft};"), context);
  const app = context.window.testConnection;
  app.state.config = { token: "token", max_items: 39 };
  node("golden-key").value = "test-only-key"; node("category").value = "3503";
  return { app, node, calls, bridge: context.window.Listcraft };
}

const reply = { account: "Account", category: { id: "3503", name: "Steal a Brainrot" }, currency: "RUB", fields: [] };

(async () => {
  for (const unavailable of [
    { creation_available: false, management_only: true, message: "Account connected. This category has 39 listings. Manage existing listings below." },
    { creation_available: false, message: "FunPay did not provide a new-listing editor for this category." },
    { management_only: true }
  ]) {
    const { app, node, calls, bridge } = harness({ ...reply, ...unavailable });
    await app.connect({ preventDefault() {} });
    assert.equal(app.state.connected, true); assert.equal(app.state.creationAvailable, false);
    assert.equal(bridge.getSession().connected, true); assert.equal(bridge.getSession().busy, false);
    assert.match(app.publishReason(), /New listings unavailable/);
    assert.equal(node("golden-key").value, "");
    assert.match(node("toast").textContent, /manage existing listings/);
    assert.doesNotMatch(node("toast").textContent, /batch.*ready/i);
    assert.ok(node("global-message").textContent.length > 0);
    app.capacityNote(); assert.equal(node("capacity-note").textContent, app.state.connectionMessage);
    await app.startQuote(); await app.review(); await app.startJob();
    assert.deepEqual(calls.map((call) => call.url), ["/api/connect"], "All batch entry points must block before capacity, quote or publication");
    const managed = await bridge.api("/api/offers"); assert.equal(managed.items[0].id, "1", "Management stays available through the shared API bridge");
    assert.equal(Object.hasOwn(app.draft(), "creation_available"), false, "Connection capabilities must not persist in drafts");
    await app.disconnect(); assert.equal(app.state.connected, false); assert.equal(app.state.creationAvailable, true); assert.equal(app.state.connectionMessage, "");
  }
  for (const normal of [{}, { creation_available: true }]) {
    const { app, node } = harness({ ...reply, ...normal }); await app.connect({ preventDefault() {} });
    assert.equal(app.state.creationAvailable, true); assert.equal(app.publishReason(), "Build a batch to get started");
    assert.match(node("toast").textContent, /batch is ready/);
  }
  console.log("Connection UI checks passed: category-specific management-only sessions stay connected, batch quote/review/start block, management remains available, credentials clear, normal and legacy connection responses remain compatible.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
