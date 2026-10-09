// Use only a synthetic acceptance instance; the supplied admin can install/disable extensions.
import { chromium } from '../.local/tools/browser/node_modules/playwright/index.mjs';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
const access=JSON.parse(await readFile(process.env.REVIEW_ADMIN_ACCESS || '.local/output/theme-review-access.json','utf8'));
const origin=process.env.REVIEW_ORIGIN || access.origin;
const output=process.env.REVIEW_OUTPUT || '.local/output/browser/themes';
await mkdir(output,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.ANIMEMO_BROWSER || '/usr/bin/chromium',args:['--no-sandbox']});
const admin=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'});
const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'});
for(const p of [admin,page])p.setDefaultTimeout(15000);
const report={checks:[],axe:[],errors:[]};
for(const p of [admin,page])p.on('pageerror',error=>report.errors.push(error.message));
const call=async(p,method,path,data)=>{const r=await p.request.fetch(origin+path,{method,headers:{Origin:origin},data});assert.ok(r.ok(),`${method} ${path}: ${r.status()}`);return r.status()===204?null:r.json();};
const click=async(p,locator)=>{await p.locator('body').ariaSnapshot();await locator.click();};
const mark=value=>{report.checks.push(value);console.log(value);};
const scan=async(name)=>{
 await page.evaluate(await readFile('.local/tools/browser/node_modules/axe-core/axe.min.js','utf8'));
 const violations=await page.evaluate(async()=>(await window.axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)})));
 report.axe.push({name,violations});assert.deepEqual(violations,[]);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'page fits viewport');
 for(const el of await page.locator('.dialog,.notes-appearance-preview,.note-sheet-entry').all())assert.ok(await el.evaluate(n=>n.scrollWidth<=n.clientWidth+1),'notes surfaces fit viewport');
};
const open=()=>click(page,page.getByRole('button',{name:'札记外观',exact:true}));
const choose=()=>click(page,page.getByRole('button',{name:/樱色信笺/}));
const paper=()=>page.locator('.note-sheet').first().evaluate(n=>getComputedStyle(n).backgroundColor);
let original;
try{
 await call(admin,'POST','/api/v1/auth/login',{email:access.email,password:access.password});
 const installed=(await call(admin,'GET','/api/v1/admin/plugins')).items;
 original=installed.find(p=>p.manifest.slug==='notes-hanami'&&p.active);
 await admin.goto(origin+'/admin#plugins');
 await admin.getByRole('navigation',{name:'管理导航'}).getByRole('button',{name:'插件',exact:true}).click();
 if(!installed.some(p=>p.manifest.slug==='notes-hanami')){
  await admin.getByLabel('选择插件包').setInputFiles('.local/output/notes-hanami.animemo-plugin');
  await admin.getByRole('dialog').waitFor();
  await click(admin,admin.getByRole('button',{name:'已审核，安装此版本',exact:true}));
  await admin.getByRole('dialog').waitFor({state:'detached'});
 }
 const card=admin.locator('.admin-plugin-card').filter({hasText:'樱色信笺'}).first();
 await card.waitFor();
 if(await card.getByRole('button',{name:'启用此版本',exact:true}).count())await click(admin,card.getByRole('button',{name:'启用此版本',exact:true}));
 await card.getByRole('button',{name:'停用',exact:true}).waitFor();
 await admin.screenshot({path:output+'/theme-admin.png'});
 const user=await call(page,'POST','/api/v1/auth/register',{email:`theme-user-${randomUUID()}@example.test`,password:`test-${randomUUID()}`,display_name:'主题验收'});
 const note=await call(page,'POST','/api/v1/memory/notes',{title:'给未来的自己留一封信',body:'故事结束以后，仍有一些画面留在心里。\n\n换一种纸色，再读一读当时的心情。',tags:['温柔的故事'],highlight:true});
 await page.goto(origin+'/memory#notes');await page.locator('.note-sheet-entry').waitFor();
 assert.equal(await paper(),'rgb(255, 252, 244)');
 await open();await choose();
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'','preview must not save');
 assert.equal(await paper(),'rgb(255, 252, 244)','preview must not restyle the page behind the dialog');
 await scan('desktop preview');await page.screenshot({path:output+'/theme-preview.png'});
 for(const width of [390,320]){await page.setViewportSize({width,height:844});await scan(`preview ${width}`);await page.screenshot({path:output+`/theme-preview-${width}.png`});}
 await click(page,page.getByRole('button',{name:'取消',exact:true}));
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'');
 await open();await choose();await click(page,page.getByRole('button',{name:'应用外观',exact:true}));await page.getByRole('dialog').waitFor({state:'detached'});
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'notes-hanami');
 assert.equal(await paper(),'rgb(255, 250, 251)');
 await scan('applied mobile');await page.screenshot({path:output+'/theme-applied-mobile.png'});
 await page.reload();await page.locator('.note-sheet-entry').waitFor();
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('.note-sheet')).backgroundColor==='rgb(255, 250, 251)');
 await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:output+'/theme-applied.png'});
 await click(page,page.getByRole('button',{name:`阅读 ${note.title}`,exact:true}));
 assert.equal(await page.locator('.dialog.notes-surface').evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(255, 250, 251)');
 await scan('themed reader');
 await click(page,page.getByRole('button',{name:'编辑记忆',exact:true}));
 assert.equal(await page.locator('.dialog.notes-surface').evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(255, 250, 251)');
 await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'detached'});
 mark('Admin installation/activation, per-user preview/cancel/apply, reload, reader and composer work against the real API.');
 await click(admin,card.getByRole('button',{name:'停用',exact:true}));await card.getByRole('button',{name:'启用此版本',exact:true}).waitFor();
 await page.reload();await page.locator('.note-sheet-entry').waitFor();assert.equal(await paper(),'rgb(255, 252, 244)');
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'');
 await click(admin,card.getByRole('button',{name:'启用此版本',exact:true}));await card.getByRole('button',{name:'停用',exact:true}).waitFor();
 await page.reload();await page.locator('.note-sheet-entry').waitFor();await open();
 await click(page,page.getByRole('button',{name:/纸页原色/}));await click(page,page.getByRole('button',{name:'恢复默认外观',exact:true}));await page.getByRole('dialog').waitFor({state:'detached'});
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'');assert.equal(await paper(),'rgb(255, 252, 244)');
 await open();await choose();await click(page,page.getByRole('button',{name:'应用外观',exact:true}));await page.getByRole('dialog').waitFor({state:'detached'});
 await page.goto(origin+'/admin');await page.getByRole('heading',{name:'需要管理员权限',exact:true}).waitFor();
 assert.equal(await page.locator('[style*="--notes-paper"]').count(),0);
 await call(page,'POST','/api/v1/auth/logout');await page.goto(origin+'/memory');await page.getByRole('button',{name:'已有账号',exact:true}).waitFor();
 assert.equal(await page.locator('[style*="--notes-paper"]').count(),0);
 await page.getByLabel('怎么称呼你').fill('另一个账号');
 await page.getByLabel('邮箱地址').fill(`theme-other-${randomUUID()}@example.test`);
 await page.getByLabel(/^密码/).fill(`test-${randomUUID()}`);
 await click(page,page.getByRole('button',{name:'创建我的手账',exact:true}));await page.getByRole('heading',{name:'札记',exact:true}).waitFor();
 assert.equal((await call(page,'GET','/api/v1/themes')).selected_slug,'');
 mark('Disable falls back to Core; restore default persists; admin/login and another account never inherit the selected theme.');
 assert.deepEqual(report.errors,[]);
}catch(error){await page.screenshot({path:output+'/failure.png'});await writeFile(output+'/failure.txt',await page.locator('body').ariaSnapshot());throw error;}
finally{
 const current=(await call(admin,'GET','/api/v1/admin/plugins')).items.find(p=>p.manifest.slug==='notes-hanami'&&p.active);
 if(current)await call(admin,'POST','/api/v1/admin/plugins/notes-hanami',{action:original?.enabled?'activate':'disable',version:original?.manifest.version||current.manifest.version,revision:current.revision});
 await writeFile(output+'/report.json',JSON.stringify(report,null,2));await browser.close();
}
