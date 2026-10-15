const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js', 'utf8');
function harness() {
  const cells = new Map();
  const document = {getElementById(id) {
    if (!cells.has(id)) cells.set(id, {value:'', textContent:'', hidden:false});
    return cells.get(id);
  }};
  const context = {document, window:{}, crypto:{randomUUID:()=>'generated'}, clearTimeout, setTimeout};
  vm.createContext(context);
  vm.runInContext(source.replace('  initialize();', '  renderRows = () => {}; update = () => {}; window.testing = {restoreDraft, draft, state, eligible, priceConversion};'), context);
  return {...context.window.testing, cells};
}
const row = (extra={}) => ({id:'one', source:'Item', price:'100', state:'queued', ...extra});
const data = (extra={}) => ({version:1, default_price:'100', currency:'USD', rows:[row()], ...extra});

// Seller amounts must never silently become buyer RUB targets on restore.
for (const mode of [undefined, 'seller']) {
  const h = harness();
  assert.equal(h.restoreDraft(data({price_mode:mode})), true);
  assert.equal(h.state.priceMode, 'buyer_rub');
  assert.equal(h.state.rows[0].price, '');
  assert.equal(h.state.rows[0].price_mode, 'buyer_rub');
  assert.equal(h.cells.get('default-price').value, '');
  assert.match(h.cells.get('global-message').textContent, /Re-enter buyer prices/);
}
// Modern buyer targets survive, but editable cached conversions never do.
{
  const h=harness();
  h.restoreDraft(data({price_mode:'buyer_rub', rows:[row({pricing:{seller_price:'90.91',buyer_price_rub:'100',target_rub:'100',exact:true}})]}));
  assert.equal(h.state.rows[0].price, '100');
  assert.equal(h.state.rows[0].quote, null);
  assert.equal(h.state.quoteId, null);
}
// Row provenance takes precedence over the draft's mode in mixed drafts.
{
  const h=harness();
  h.restoreDraft(data({price_mode:'buyer_rub', rows:[row({price_mode:'seller'}),row({id:'two'})]}));
  assert.equal(h.state.rows[0].price, '');
  assert.equal(h.state.rows[1].price, '100');
  h.restoreDraft(data({price_mode:'seller', rows:[row({price_mode:'buyer_rub'})]}));
  assert.equal(h.state.rows[0].price, '100');
}
// Confirmed and ambiguous history retains its original amount and lock after export/import.
for (const status of ['published','uncertain','publishing']) {
  const h=harness(); h.restoreDraft(data({rows:[row({state:status})]}));
  assert.equal(h.state.rows[0].price, '100');
  assert.equal(h.state.rows[0].price_mode, 'seller');
  assert.equal(h.eligible().length,0);
  const exported=h.draft();
  assert.equal(exported.price_mode,'buyer_rub');
  h.restoreDraft(exported,true);
  assert.equal(h.state.rows[0].price_mode,'seller');
  assert.equal(h.state.rows[0].price,'100');
  assert.equal(h.eligible().length,0);
}
// Queued offers from a submitted legacy job retain history, even before polling.
for (const imported of [false,true]) {
  const h=harness(); h.restoreDraft(data({job_id:'old-job'}),imported);
  assert.equal(h.state.rows[0].price,'100');
  assert.equal(h.state.rows[0].price_mode,'seller');
  if (imported) {assert.equal(h.state.rows[0].state,'uncertain');assert.equal(h.eligible().length,0);}
}
assert.doesNotMatch(fs.readFileSync('static/index.html','utf8'), /option value="seller"/);
console.log('Buyer-only draft migration checks passed.');
