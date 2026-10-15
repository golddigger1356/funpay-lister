const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js','utf8');
const info = {account:'Fixture Seller',category:{id:'123'},currency:'RUB',fields:[],creation_available:true};
function harness(jobs) {
  const nodes = new Map(), calls = [], storage = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id,{value:'',type:'password',hidden:false,textContent:'',style:{},children:[],firstChild:{textContent:''},replaceChildren(...items){this.children=items;},append(...items){this.children.push(...items);},querySelectorAll:()=>[],focus(){}});
    return nodes.get(id);
  };
  const document = {getElementById:node,createElement:()=>node('element-'+nodes.size)};
  let app;
  const context = {document,window:{},crypto:{randomUUID:()=> 'id'},clearTimeout(){},setTimeout(fn){queueMicrotask(fn);return 1;},
    localStorage:{setItem(){}},sessionStorage:{setItem(k,v){storage.set(k,v);},removeItem(k){storage.delete(k);}},
    async fetch(url,options) {
      calls.push({url,body:options.body && JSON.parse(options.body)});
      if (url==='/api/connect') return {ok:true,json:async()=>({connection_job_id:'job'})};
      if (url==='/api/connect_stop') return {ok:true,json:async()=>({ok:true})};
      assert.equal(url,'/api/connection_job?id=job');
      assert.equal(app.state.busy,true,'Other work must remain blocked while cleanup runs');
      assert.equal(node('golden-key').value,'','Credential must clear when the job is accepted');
      const job=jobs.shift();
      if (typeof job==='function') return await job({app,node,calls});
      return {ok:true,json:async()=>job};
    }};
  vm.createContext(context);
  vm.runInContext(source.replace('  initialize();','  renderFields=()=>{};renderRows=()=>{};update=()=>{};window.testing={state,connect,stopConnection,draft};'),context);
  app=context.window.testing; app.state.config={token:'token'};
  node('golden-key').value='LOCALTEST999'; node('category').value='123';
  return {app,node,calls,storage};
}
(async()=>{
  const running={state:'running',phase:'cleaning',completed:1,total:3,deleted:1,skipped:0,items:[{id:'1',title:'Paused',state:'deleted',message:'Deleted'}]};
  {
    const h=harness([running,{...running,state:'completed',completed:3,deleted:2,skipped:1,result:info}]);
    await h.app.connect();
    assert.equal(h.app.state.connected,true,h.node('global-message').textContent); assert.equal(h.app.state.busy,false);
    assert.equal(h.calls.filter(c=>c.url==='/api/connect').length,1);
    assert.match(h.node('connection-progress-note').textContent,/2 deleted · 1 kept/);
    assert.equal(h.storage.size,0);
    assert.equal(Object.hasOwn(h.app.draft(),'connectionJobId'),false);
  }
  {
    const h=harness([async({app})=>{await app.stopConnection();return {ok:true,json:async()=>({...running,state:'stopped',error:'Stopped',result:{...info,creation_available:false,management_only:true,message:'Cleanup stopped'}})};}]);
    await h.app.connect();
    assert.equal(h.app.state.connected,true);assert.equal(h.app.state.creationAvailable,false);
    assert.equal(h.calls.filter(c=>c.url==='/api/connect_stop').length,1);
    assert.match(h.node('connection-progress-title').textContent,/stopped/);
  }
  {
    const h=harness([()=>{throw new Error('Lost connection')},()=>{throw new Error('Lost connection')},()=>{throw new Error('Lost connection')}]);
    await h.app.connect();
    assert.equal(h.app.state.connected,false);assert.equal(h.calls.filter(c=>c.url==='/api/connect').length,1);
    assert.match(h.node('global-message').textContent,/nothing was retried/);
    assert.equal(h.storage.size,1,'An interrupted cleanup needs a reload warning');
  }
  console.log('Async connection UI checks passed: progress, busy lock, stop, credential clearing, and no automatic deletion retry.');
})().catch(error=>{console.error(error);process.exitCode=1;});
