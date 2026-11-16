const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/tmp/squire-browser/node_modules/playwright');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const path = require('node:path');
const web = 'http://127.0.0.1:58740', control = 'http://127.0.0.1:58741', messaging = 'http://127.0.0.1:58742', fixture = 'http://127.0.0.1:58744';
const output = process.env.PILOT_TEST_ARTIFACTS || '/tmp/squire-pilot-acceptance';
const checks = [], errors = [];
const note = name => {checks.push(name); process.stdout.write('PASS '+name+'\n');};
async function api(base, route, data, token) {
  const response = await fetch(base+route,{signal:AbortSignal.timeout(15000),method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(token?{'Authorization':'Bearer '+token}:{})},...(data===undefined?{}:{body:JSON.stringify(data)})});
  const body = await response.json().catch(()=>({}));
  if(!response.ok) throw Error(`${route}: ${response.status} ${JSON.stringify(body)}`);
  return body;
}
async function poll(label, fn, timeout=90000) {
  const end=Date.now()+timeout;
  while(Date.now()<end){const value=await fn();if(value)return value;await new Promise(r=>setTimeout(r,250));}
  throw Error('Timed out: '+label);
}
const state=()=>api(fixture,'/fixture/status');
const inject=data=>api(fixture,'/fixture/inbound',data);
async function me(page){return page.evaluate(async()=>{const r=await fetch('/api/v1/me',{signal:AbortSignal.timeout(15000)});return r.json();});}
async function serviceFor(tenant,kind){return poll('provisioned '+kind,async()=>Object.values((await state()).services).find(s=>s.variables.SQUIRE_OWNER_TENANT_ID===tenant&&s.variables.SQUIRE_RUNTIME_KIND===kind));}
async function signup(browser,email,phone,name){
  const invite=await api(control,'/v1/operator/invitations',{email},'operator-integration-test-token');
  const context=await browser.newContext({viewport:{width:1280,height:900}});const page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(invite.signup_url);
  await page.fill('#email',email);await page.locator('#login-form button').click();
  const emailMessage=await poll('email verification',async()=>(await state()).emails.find(e=>e.to.includes(email)));
  const emailCode=emailMessage.text.match(/code is (\d{8})/)[1];
  await page.fill('#email-code',emailCode);await page.locator('#code-form button').click();
  await page.locator('#profile-form').waitFor({state:'visible'});await page.fill('#name',name);await page.check('#consent');
  await page.locator('#profile-form button').click();await page.locator('#phone-form').waitFor({state:'visible'});
  await page.fill('#phone',phone);await page.locator('#phone-form button').click();
  const jid=phone.slice(1)+'@s.whatsapp.net';
  const setup=await poll('first-contact WhatsApp code',async()=>(await state()).sends.find(s=>s.recipient===jid&&s.text.includes('setup code')));
  if(email==='first@fixture.invalid')assert.equal((await state()).inbound_count,0,'first outbound must precede inbound customer contact');
  const phoneCode=setup.text.match(/code is (\d{8})/)[1];
  await page.fill('#phone-code',phoneCode);await page.locator('#phone-code-form button').click();
  await page.locator('#dashboard').waitFor({state:'visible'});
  const account=await poll('runtime heartbeat',async()=>{const m=await me(page);return m.runtime_ready&&m;});
  assert.equal(account.trial_state,'ready_unstarted');assert.equal(account.expires_at,null);
  await poll('ready introduction',async()=>(await state()).sends.find(s=>s.recipient===jid&&s.text.includes('assistant is ready')));
  note(name+' browser email→reservation→first outbound code→phone verification→provisioning→ready intro');
  return {page,context,account,jid};
}

(async()=>{
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_BIN||'/home/shaur/.cache/ms-playwright/chromium-1234/chrome-linux/chrome',headless:true,args:['--no-sandbox']});
  let first;
  try{
    const blocked=await fetch(web+'/api/v1/operator/invitations',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    assert.equal(blocked.status,404);note('production Caddy allowlist hides operator/service/runtime APIs');
    first=await signup(browser,'first@fixture.invalid','+15550000101','First Customer');
    await inject({chat_jid:first.jid,text:'My first trial request'});
    await poll('first model reply',async()=>(await state()).sends.find(s=>s.recipient===first.jid&&s.text==='Fixture model reply: My first trial request'));
    const active=await poll('trial activation',async()=>{const m=await me(first.page);return m.trial_state==='active'&&m.turns_used===1&&m;});
    assert.ok(active.expires_at);note('first turn activates capped model credentials through actual provisioning lifecycle');
    await inject({replay:true});await new Promise(r=>setTimeout(r,750));
    assert.equal((await state()).sends.filter(s=>s.text==='Fixture model reply: My first trial request').length,1);
    assert.equal((await me(first.page)).turns_used,1);note('provider replay does not duplicate model reply or trial quota');
    const second=await signup(browser,'second@fixture.invalid','+15550000102','Second Customer');
    await inject({chat_jid:second.jid,text:'Only the second customer gets this'});
    await poll('second reply',async()=>(await state()).sends.find(s=>s.recipient===second.jid&&s.text.includes('Fixture model reply: Only the second')));
    const a=await serviceFor(first.account.tenant_id,'dm'),b=await serviceFor(second.account.tenant_id,'dm');
    const cross=await fetch(messaging+'/v1/outbound',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+a.variables.SQUIRE_EGRESS_TOKEN},body:JSON.stringify({conversation_id:b.variables.SQUIRE_CONVERSATION_ID,response_key:'cross-tenant-attempt',text:'must not escape'})});
    assert.equal(cross.status,403);note('two independent browser customers and scoped recipient isolation');
    await first.page.locator('#group-button').click();await first.page.locator('#group-command').waitFor({state:'visible'});
    const command=await first.page.locator('#group-command').textContent();const group='120555001@g.us';
    await inject({chat_jid:group,chat_type:'group',sender_jid:first.jid,text:command});
    await poll('admin request',async()=>(await state()).sends.find(s=>s.recipient===first.jid&&s.text.includes('current group administrator')));
    assert.equal(Object.values((await state()).services).filter(s=>s.variables.SQUIRE_RUNTIME_KIND==='group').length,0);
    await inject({chat_jid:group,chat_type:'group',sender_jid:'999999@lid',text:command.replace(' claim ',' approve ')});
    const groupService=await serviceFor(first.account.tenant_id,'group');
    await poll('group introduction',async()=>(await state()).sends.find(s=>s.recipient===group&&s.text.includes('approved group')));
    assert.notEqual(groupService.variables.SQUIRE_RUNTIME_ID,a.variables.SQUIRE_RUNTIME_ID);
    assert.equal(groupService.variables.SQUIRE_DISABLE_PRIVATE_TOOLS,'true');
    const scopedServices=[a,b,groupService], repositories=scopedServices.map(service=>service.variables.RESTIC_REPOSITORY);
    assert.equal(new Set(repositories).size,3,'private customers and group require separate backup buckets');
    for (const service of scopedServices) {
      const bucket='backup-'+service.variables.SQUIRE_RUNTIME_ID;
      assert.equal(service.variables.RESTIC_REPOSITORY,'s3:${{'+bucket+'.ENDPOINT}}/${{'+bucket+'.BUCKET}}');
      assert.equal(service.variables.AWS_ACCESS_KEY_ID,'${{'+bucket+'.ACCESS_KEY_ID}}');
      assert.equal(service.variables.AWS_SECRET_ACCESS_KEY,'${{'+bucket+'.SECRET_ACCESS_KEY}}');
    }
    assert.equal(Object.keys((await state()).buckets).length,3,'idempotent provisioning creates one bucket per runtime');
    note('each customer and approved group receives a distinct backup bucket capability');
    await inject({chat_jid:group,chat_type:'group',sender_jid:first.jid,text:'An unaddressed group conversation'});
    await inject({chat_jid:group,chat_type:'group',sender_jid:first.jid,text:'!squire Please help the group'});
    await poll('group reply',async()=>(await state()).sends.find(s=>s.recipient===group&&s.text==='Fixture model reply: !squire Please help the group'));
    assert.equal((await state()).sends.filter(s=>s.text.includes('Fixture model reply: An unaddressed')).length,0);
    note('owner plus LID administrator approval provisions isolated group and addressed-only routing');
    const approval=await api(messaging,'/v1/approvals',{request_key:'browser-private-approval',summary:'Find an available time in my private calendar'},groupService.variables.SQUIRE_EGRESS_TOKEN);
    await first.page.reload();await first.page.getByRole('button',{name:'Approve private action'}).waitFor({state:'visible'});
    await first.page.getByRole('button',{name:'Approve private action'}).click();
    await poll('private approval grant',async()=>{const result=await api(messaging,'/v1/approvals',undefined,a.variables.SQUIRE_EGRESS_TOKEN);return result.approvals.find(r=>r.approval_id===approval.approval_id);});
    await api(messaging,`/v1/approvals/${approval.approval_id}/result`,{tenant_id:first.account.tenant_id,decision:'approve',result:'Private details. Selected time is Tuesday at noon.'},a.variables.SQUIRE_EGRESS_TOKEN);
    assert.equal((await state()).sends.filter(s=>s.recipient===group&&s.text.includes('Private details')).length,0);
    await first.page.reload();const selected=first.page.getByLabel('Selected result to share with the group');await selected.waitFor({state:'visible'});await selected.fill('Tuesday at noon works.');
    await first.page.getByRole('button',{name:'Share selected result with group'}).click();
    await poll('explicit selected release',async()=>(await state()).sends.find(s=>s.recipient===group&&s.text==='Tuesday at noon works.'));
    note('browser private approval and separate selected result release keep private details out of group');
    await first.page.screenshot({path:path.join(output,'desktop-dashboard.png'),fullPage:true});
    await first.page.setViewportSize({width:390,height:844});await first.page.screenshot({path:path.join(output,'mobile-dashboard.png'),fullPage:true});
    assert.equal(await first.page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await first.page.locator('#pause-button').click();await poll('pause',async()=>(await me(first.page)).trial_state==='paused');
    await inject({chat_jid:first.jid,text:'Do not answer while paused'});await new Promise(r=>setTimeout(r,2000));
    assert.equal((await state()).sends.filter(s=>s.text==='Fixture model reply: Do not answer while paused').length,0);
    note('website pause denies new trial turns and outbound replies');
    assert.deepEqual(errors,[]);note('desktop/mobile browser run has no JavaScript errors or mobile overflow');
    const final=await state();
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify({passed:true,checks,boundary:'Real control/messaging services and Caddy; fixture Railway/email/provider/model and runtime contract simulator. No real WhatsApp sends or Hermes execution.',counts:{sends:final.sends.length,inbound:final.inbound_count,services:Object.keys(final.services).length},railway_operations:[...new Set(final.railway_operations)],javascript_errors:errors},null,2));
  }catch(error){if(first)await first.page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await fs.writeFile(path.join(output,'report.json'),JSON.stringify({passed:false,checks,error:String(error),javascript_errors:errors},null,2));throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
