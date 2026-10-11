#!/usr/bin/env node
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Copyright 2026 Qiuyu Chen (canghaimeng).
// Portable release check: real offline demo and browser, no accounts or private projects.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir,writeFile,mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {spawn} from 'node:child_process';
const require=createRequire(path.resolve(process.env.EULER_BROWSER_TOOLS || '/tmp/euler-dashboard-browser','package.json'));
const {chromium}=require('playwright');
const output=process.env.EULER_BROWSER_ARTIFACTS || '/tmp/euler-release-ui';
await mkdir(output,{recursive:true});
let server,browser;
const checks=[],errors=[];
let logs='';
try {
 let base=process.env.BASE_URL;
 if(!base){
  const port=await new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
  server=spawn(process.env.EULER_BROWSER_PYTHON || 'python3',['-m','euler.observability.demo','--port',String(port)],{
   cwd:await mkdtemp(path.join(tmpdir(),'euler-wheel-ui-')),env:{...process.env,EULER_DASHBOARD_TOKEN:'',PYTHONPATH:''},stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
  base=`http://127.0.0.1:${port}`;
  let ready=false;
  for(let i=0;i<900;i++){
   if(server.exitCode!==null)throw new Error(`Demo server failed: ${logs}`);
   try{const r=await fetch(base+'/api/projects');if(r.ok){ready=true;break;}}catch{}
   await new Promise(r=>setTimeout(r,200));
  }
  assert.ok(ready,`Demo did not become ready: ${logs}`);
 }
 browser=await chromium.launch({headless:true,...(process.env.EULER_BROWSER_EXECUTABLE?{executablePath:process.env.EULER_BROWSER_EXECUTABLE}:{}),args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base,{waitUntil:'networkidle'});
 await page.waitForFunction(()=>document.querySelectorAll('#project-selector option').length===2);
 assert.equal(await page.locator('html').getAttribute('lang'),'en-US');
 assert.ok((await page.locator('#project-name').innerText()).includes('demo'));
 console.log('PASS checkpoint');checks.push('English-first two-project no-account demo');
 const data=await (await fetch(base+'/api/graph?project=completed')).json();
 assert.ok(data.nodes.length>0);
 await page.screenshot({path:path.join(output,'overview.png'),fullPage:false});
 for(const view of ['reader','explore','map','ops','team']){
  await page.click(`[data-view="${view}"]`);
  await page.locator(`[data-panel="${view}"]`).waitFor({state:'visible'});
 }
 console.log('PASS checkpoint');checks.push('all six workbench views open');
 await page.click('[data-view="map"]');
 await page.waitForFunction(()=>typeof state!=='undefined' && state.cy?.nodes().length>0);
 await page.evaluate(()=>{state.cy.nodes().first().emit('tap');});
 await page.screenshot({path:path.join(output,'map.png'),fullPage:true});
 console.log('PASS checkpoint');checks.push('dependency graph renders and node selection works');
 await page.click('[data-lang="zh"]');
 assert.equal(await page.locator('html').getAttribute('lang'),'zh-CN');
 await page.reload({waitUntil:'networkidle'});
 assert.equal(await page.locator('html').getAttribute('lang'),'zh-CN');
 await page.click('[data-lang="en"]');
 console.log('PASS checkpoint');checks.push('language toggle persists across reload');
 await page.selectOption('#project-selector','exploration');
 await page.waitForFunction(()=>document.querySelector('#project-name')?.textContent.includes('New exploration'));
 await page.click('[data-view="explore"]');
 assert.equal((await (await fetch(base+'/api/graph?project=exploration')).json()).nodes.length,0);
 await page.reload({waitUntil:'networkidle'});
 assert.equal(await page.locator('#project-selector').inputValue(),'exploration');
 console.log('PASS checkpoint');checks.push('new research with no Facts, paper or Lean; project deep link survives reload');
 await page.setViewportSize({width:390,height:844});
 await page.click('[data-view="overview"]');
 await page.waitForTimeout(250);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
 console.log('PASS checkpoint');checks.push('mobile layout has no horizontal overflow');
 assert.equal((await fetch(base+'/static/help.html')).status,200);
 assert.equal((await fetch(base+'/static/i18n.js')).status,200);
 assert.equal((await fetch(base+'/api/workspace?project=../../private')).status,404);
 console.log('PASS checkpoint');checks.push('packaged help assets and unknown-project isolation');
 assert.deepEqual(errors,[]);
 await writeFile(path.join(output,'acceptance.json'),JSON.stringify({checks,errors,mock_models:true},null,2));
 console.log(JSON.stringify({checks,errors},null,2));
} finally {
 server?.kill('SIGTERM');await browser?.close();
 await writeFile(path.join(output,'server.log'),logs);
}
