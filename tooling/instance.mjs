import { createReadStream } from 'node:fs';
import { mkdir,readFile,writeFile,open,rm,stat,statfs,access,constants } from 'node:fs/promises';
import { createHash,randomBytes,randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { integrationValues,parseIntegrations } from './integrations.mjs';
import { readOperation,advanceOperation,operationReceipt,writeOperation } from './operation-journal.mjs';
import { validName,validPort,validOrigin,validBind,configurationPlan,checkPort,publicConfig } from './instance-config.mjs';
import { runProcess } from './process.mjs';
import { exportBackup,openBackup } from './backup-transfer.mjs';
import { loadVerifiedRelease } from './release.mjs';
import { imageConfigDigest } from './image-identity.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const instances=path.join(root,'.local','instances');
const instancePath=name=>path.join(instances,name);
const secret=()=>randomBytes(32).toString('hex');
const command=(args,options={})=>runProcess('docker',args,{cwd:root,...options});
function composeEnv(c){return {...process.env,...integrationValues(c.integrations),ANIMEMO_DB_PASSWORD:c.password,ANIMEMO_DATABASE:c.database,ANIMEMO_IMAGE:c.image,PUBLIC_ORIGIN:c.origin,ANIMEMO_INSTANCE_BIND:c.bind||'127.0.0.1',ANIMEMO_INSTANCE_PORT:String(c.port),ANIMEMO_SETUP_TOKEN:c.setupToken,ANIMEMO_SECRET_KEY:c.secretKey};}
const composeArgs=c=>['compose','-p',c.project,'-f','deploy/compose.instance.yaml'];
const workerImages=new Map();
async function hasWorker(c){if(!workerImages.has(c.image))workerImages.set(c.image,await command(['image','inspect',c.image,'--format','{{ index .Config.Labels "org.animemo.worker-mode" }}'],{capture:true})==='separate-v1');return workerImages.get(c.image);}
const compose=async(c,args,options={})=>{const worker=await hasWorker(c);return command([...composeArgs(c),'--profile','runtime-worker',...(worker?args:args.filter(a=>a!=='worker'))],{...options,env:composeEnv(c)});};
const operationPath=name=>path.join(instancePath(name),'operation.json');

const sql=(c,query,database=c.database)=>compose(c,['exec','-T','db','psql','-U','animemo','-d',database,'-v','ON_ERROR_STOP=1','-Atc',query],{capture:true});
async function load(name){validName(name);const c=JSON.parse(await readFile(path.join(instancePath(name),'config.json'),'utf8'));if(![c.password,c.setupToken,c.secretKey].every(value=>/^[a-f0-9]{64}$/.test(value))||c.name!==name||c.project!==`animemo-instance-${name}`||!/^[a-z][a-z0-9_]{0,62}$/.test(c.database)||!/^sha256:[a-f0-9]{64}$/.test(c.image))throw new Error('Instance configuration is invalid.');validPort(c.port);validOrigin(c.origin);validBind(c.bind);integrationValues(c.integrations);return c;}
async function save(c){await writeOperation(path.join(instancePath(c.name),'config.json'),c);}
const terminal=operation=>!operation||['completed','recovered','failed_safe'].includes(operation.phase);
async function lock(name,fn,{recovery=false}={}){
 const dir=instancePath(validName(name));await mkdir(dir,{recursive:true,mode:0o700});const target=path.join(dir,'operation.lock');let handle;
 try{handle=await open(target,'wx',0o600);}catch(error){if(error.code==='EEXIST')throw new Error('An instance operation is already in progress; use recover after its process has stopped.');throw error;}
 try{await handle.writeFile(JSON.stringify({pid:process.pid,started_at:new Date().toISOString()})+'\n');if(!recovery&&!terminal(await readOperation(operationPath(name))))throw new Error('An interrupted operation exists; run instance recover first.');return await fn();}
 finally{await handle.close();await rm(target);}
}
async function runtimeServices(c){return (await compose(c,['ps','--status','running','--services'],{capture:true})).split(/\r?\n/).filter(s=>['app','worker'].includes(s));}
async function resume(c,services=['app','worker']){if(services.length)await compose(c,['up','-d','--wait','--wait-timeout','90','--no-deps','--force-recreate',...services]);}
async function begin(c,kind,extra={}){return advanceOperation(operationPath(c.name),{schema:'animemo.operation/v1',id:randomUUID(),kind,started_at:new Date().toISOString(),prior:c,...extra},'prepared');}
async function pinImage(image){if(!image||image.startsWith('-'))throw new Error('Supply a locally built or loaded application image.');const result=await command(['image','inspect',image,'--format','{{.Id}}'],{capture:true});if(!/^sha256:[a-f0-9]{64}$/.test(result))throw new Error('Cannot resolve local image digest.');return result;}
async function sha256(file){const hash=createHash('sha256');for await(const chunk of createReadStream(file))hash.update(chunk);return hash.digest('hex');}
async function imageMetadata(image){
 const info=JSON.parse(await command(['image','inspect',image,'--format','{{json .}}'],{capture:true}));const labels=info.Config.Labels||{};
 return {version:labels['org.opencontainers.image.version']||'unknown',source_revision:labels['org.opencontainers.image.revision']||'unknown',source:labels['org.opencontainers.image.source']||null,platform:`${info.Os}/${info.Architecture}`};
}
async function snapshotInventory(c){
 const migrations=JSON.parse(await sql(c,"SELECT COALESCE(json_agg(x ORDER BY name),'[]') FROM (SELECT name,checksum FROM schema_migrations) x"));
 const plugins=(await sql(c,"SELECT to_regclass('plugin_releases') IS NOT NULL"))==='t'?JSON.parse(await sql(c,"SELECT COALESCE(json_agg(x ORDER BY slug,version),'[]') FROM (SELECT r.slug,r.version,r.digest,COALESCE(d.enabled AND d.active_version=r.version,false) AS enabled FROM plugin_releases r LEFT JOIN plugin_deployments d USING(slug)) x")):[];
 return {migrations,plugins};
}
async function backupUnlocked(c){
 const directory=path.join(root,'.local','output','backups',`${c.name}-${new Date().toISOString().replaceAll(/[:.]/g,'-')}-${randomBytes(3).toString('hex')}`);
 await mkdir(directory,{recursive:true,mode:0o700});
 try{
  await compose(c,['exec','-T','db','pg_dump','-U','animemo','-d',c.database,'--format=custom','--no-owner','--no-acl'],{output:path.join(directory,'database.dump')});
  await writeFile(path.join(directory,'secrets.json'),JSON.stringify({secretKey:c.secretKey,setupToken:c.setupToken,integrations:integrationValues(c.integrations)})+'\n',{mode:0o600,flag:'wx'});
  const hasMedia=(await sql(c,"SELECT to_regclass('media_settings') IS NOT NULL"))==='t';
  const names=['database.dump','secrets.json'];
  if(hasMedia){await compose(c,['run','--rm','--no-deps','-T','app','media-export'],{output:path.join(directory,'media.zip')});names.push('media.zip');}
  const files={};for(const name of names)files[name]={sha256:await sha256(path.join(directory,name)),bytes:(await stat(path.join(directory,name))).size};
  await writeFile(path.join(directory,'manifest.json'),JSON.stringify({schema:'animemo.instance/v3',created_at:new Date().toISOString(),image:c.image,image_config:await imageConfigDigest(c.image),application:await imageMetadata(c.image),postgres_major:17,...await snapshotInventory(c),media:{mode:hasMedia?'portable-archive':'database',archive:hasMedia?'media.zip':null},files},null,2)+'\n',{mode:0o600,flag:'wx'});
  return directory;
 }catch(error){await writeFile(path.join(directory,'INCOMPLETE'),'This backup did not complete. Do not restore it.\n',{mode:0o600});throw error;}
}
export async function validateBackup(directory){
 directory=path.resolve(directory);try{await stat(path.join(directory,'INCOMPLETE'));throw new Error('Incomplete backups cannot be restored.');}catch(error){if(error.code!=='ENOENT')throw error;}const manifest=JSON.parse(await readFile(path.join(directory,'manifest.json'),'utf8'));
 const names=manifest.schema==='animemo.instance/v2'||manifest.media?.mode==='portable-archive'?['database.dump','media.zip','secrets.json']:['database.dump','secrets.json'];
 if(!['animemo.instance/v1','animemo.instance/v2','animemo.instance/v3'].includes(manifest.schema)||manifest.postgres_major!==17||!/^sha256:[a-f0-9]{64}$/.test(manifest.image)||Object.keys(manifest.files||{}).sort().join(',')!==names.join(','))throw new Error('Unsupported or invalid backup manifest.');
 if(manifest.image_config!==undefined&&!/^sha256:[a-f0-9]{64}$/.test(manifest.image_config))throw new Error('Invalid backup image configuration digest.');
 if(manifest.schema==='animemo.instance/v3'&&(!manifest.application||!Array.isArray(manifest.migrations)||!manifest.migrations.length||manifest.migrations.some(m=>!/^migrations\/[a-z0-9_]+\.sql$/.test(m.name)||!/^[a-f0-9]{64}$/.test(m.checksum))||!Array.isArray(manifest.plugins)||!['portable-archive','database'].includes(manifest.media?.mode)))throw new Error('Invalid backup inventory.');
 for(const name of names){const info=await stat(path.join(directory,name));if(!info.isFile()||info.size!==manifest.files[name].bytes||await sha256(path.join(directory,name))!==manifest.files[name].sha256)throw new Error(`Backup checksum or size mismatch: ${name}`);}
 const keys=JSON.parse(await readFile(path.join(directory,'secrets.json'),'utf8'));if(!/^[a-f0-9]{64}$/.test(keys.secretKey)||!/^[a-f0-9]{64}$/.test(keys.setupToken))throw new Error('Backup encryption key or setup token is invalid.');
 integrationValues(keys.integrations);return {directory,manifest,keys};
}
export async function restorePlan({backup,name,image}={}){
 validName(name);const {manifest}=await validateBackup(backup);const issues=[],warnings=[];
 try{await stat(path.join(instancePath(name),'config.json'));issues.push('Target instance already exists; choose a new name.');}catch(error){if(error.code!=='ENOENT')throw error;}
 let pinned,matching=false;try{pinned=await pinImage(image||manifest.image);matching=pinned===manifest.image||await imageConfigDigest(pinned)===(manifest.image_config||manifest.image);if(!matching)issues.push('Restore with the original image; update separately afterwards.');}catch{issues.push('The original image is not loaded locally; load its archive and supply --image with the returned local_image.');}
 if(matching&&manifest.schema==='animemo.instance/v3'){
  const application=await imageMetadata(pinned);
  if(JSON.stringify(application)!==JSON.stringify(manifest.application))issues.push('Image provenance does not match the backup.');
  if(application.version!=='unknown'){
   const version=JSON.parse(await command(['run','--rm','--network','none',pinned,'version'],{capture:true}));
   if(JSON.stringify(version.migrations)!==JSON.stringify(manifest.migrations))issues.push('Backup schema does not match its original image.');
  }else warnings.push('Development image has no version metadata; compatibility is restricted to the identical image ID.');
 }else if(manifest.schema!=='animemo.instance/v3')warnings.push('Older project snapshot has no migration/plugin inventory.');
 return {compatible:issues.length===0,issues,warnings,target:name,image:manifest.image,application:manifest.application||null,postgres_major:manifest.postgres_major,migrations:manifest.migrations||null,plugins:manifest.plugins||null,media:manifest.media||null,changes:['Create a new instance and database; never overwrite an existing instance.','Restore accounts, encrypted TOTP, journal, media and extension package bytes.','Revoke sessions, pending email and external authorization; disable extensions for review.','External service credentials are preserved in backup but not activated.'],requires:['Original local application image','PostgreSQL 17','Free listen address/port','Enough space for database and media'],writes_resources:false};
}
const revokeExternalSQL=`DO $$ BEGIN
 IF to_regclass('memory_share_tokens') IS NOT NULL THEN DELETE FROM memory_share_tokens; END IF;
 IF to_regclass('achievement_backfills') IS NOT NULL THEN UPDATE achievement_backfills SET state='paused' WHERE state='running'; END IF;
 IF to_regclass('user_theme_selections') IS NOT NULL THEN DELETE FROM user_theme_selections; END IF;
 IF to_regclass('user_note_themes') IS NOT NULL THEN DELETE FROM user_note_themes; END IF;
 IF to_regclass('plugin_deployments') IS NOT NULL THEN UPDATE plugin_deployments SET enabled=false,revision=revision+1; END IF;
 IF EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='plugin_deployments' AND column_name='health') THEN UPDATE plugin_deployments SET health='review_required',health_reason='恢复后需要重新审阅并启用。'; END IF;
 IF to_regclass('email_tokens') IS NOT NULL THEN DELETE FROM email_tokens; UPDATE email_outbox SET state='cancelled',payload=NULL WHERE state IN ('pending','sending'); END IF;
 IF to_regclass('external_connections') IS NOT NULL THEN DELETE FROM external_oauth_states; UPDATE external_connections SET state='disconnected',tokens=NULL,generation=gen_random_uuid(),expires_at=NULL; UPDATE external_sync_jobs SET state='cancelled' WHERE state IN ('fetching','ready','applying'); END IF;
 END $$;`;
async function restoreDatabase(c,backup,database,{clone=false}={}){
 // Every restore target is either a brand-new instance or a fresh candidate database.
 await compose(c,['exec','-T','db','pg_restore','-U','animemo','-d',database,'--no-owner','--no-acl','--exit-on-error','--single-transaction'],{input:path.join(backup,'database.dump')});
 const source=await validateBackup(backup);
 if(source.manifest.schema==='animemo.instance/v3'){
  const inventory=await snapshotInventory({...c,database});
  if(JSON.stringify(inventory.migrations)!==JSON.stringify(source.manifest.migrations)||JSON.stringify(inventory.plugins)!==JSON.stringify(source.manifest.plugins))throw new Error('Restored database inventory does not match the backup manifest.');
 }
 await sql(c,'DELETE FROM sessions; UPDATE users SET otp_pending=NULL,otp_pending_expires=NULL;',database);
 if(clone){
  await sql(c,revokeExternalSQL,database);
  // Match the private host archive's owner instead of relaxing its 0600 mode.
  // Only this one-shot command uses that UID; web and worker keep the image UID.
  if(source.manifest.files['media.zip']){
   const archive=path.join(source.directory,'media.zip'),owner=await stat(archive);
   await compose({...c,database},['run','--rm','--no-deps','-T','--user',`${owner.uid}:${owner.gid}`,'--volume',`${archive.replaceAll('\\','/')}:/run/animemo-media.zip:ro`,'app','media-restore','/run/animemo-media.zip']);
  }
 }
}
export async function installInstance({name,image,port=18082,origin,bind='127.0.0.1',backup}={}){
 validName(name);port=validPort(port);bind=validBind(bind);origin=validOrigin(origin||`http://127.0.0.1:${port}`);
 return lock(name,async()=>{
  try{await stat(path.join(instancePath(name),'config.json'));throw new Error('Instance already exists; install never overwrites an existing instance.');}catch(error){if(error.code!=='ENOENT')throw error;}
  if(backup){const plan=await restorePlan({backup,name,image});if(!plan.compatible)throw new Error(plan.issues.join(' '));}
  const source=backup?await validateBackup(backup):null;const pinned=await pinImage(image||source?.manifest.image||'animemo-next-app');
  if(source&&pinned!==source.manifest.image&&await imageConfigDigest(pinned)!==(source.manifest.image_config||source.manifest.image))throw new Error('Restore using the backup image first, then run update.');
  await checkPort(bind,port);
  const c={schema:'animemo.instance-config/v1',name,project:`animemo-instance-${name}`,port,origin,bind,image:pinned,database:'animemo',password:secret(),setupToken:source?.keys.setupToken||secret(),secretKey:source?.keys.secretKey||secret(),rollback:null};
  const operation=await begin(c,source?'restore':'install',{backup:source?.directory});
  await finishInstall(operation);return {name,origin,restored:!!source};
 });
}
async function finishInstall(operation){
 let c=operation.prior;const file=operationPath(c.name);
 await save(c);await compose(c,['up','-d','--wait','--wait-timeout','90','db']);
 if(operation.kind==='restore'&&operation.phase!=='starting'){
  await validateBackup(operation.backup);
  await compose(c,['stop','-t','15','app','worker']);
  c={...c,database:`animemo_${randomBytes(8).toString('hex')}`};
  operation=await advanceOperation(file,operation,'restoring',{prior:c});
  await compose(c,['exec','-T','db','createdb','-U','animemo','-T','template0','-E','UTF8',c.database]);
  await restoreDatabase(c,operation.backup,c.database,{clone:true});await save(c);
 }
 operation=await advanceOperation(file,operation,'starting');
 await compose(c,['up','-d','--wait','--wait-timeout','90','app','worker']);
 return advanceOperation(file,operation,'completed');
}
export async function backupInstance(name){return lock(name,async()=>{
 const c=await load(name),services=await runtimeServices(c),file=operationPath(name);let operation=await begin(c,'backup',{services});
 await compose(c,['stop','-t','15','app','worker']);
 try{const backup=await backupUnlocked(c);operation=await advanceOperation(file,operation,'backed_up',{backup});await resume(c,services);await advanceOperation(file,operation,'completed');return backup;}
 catch(error){await resume(c,services);await advanceOperation(file,operation,'failed_safe');throw error;}
});}
export async function updateInstance(name,image){return lock(name,async()=>{
 const file=operationPath(name),previous=await readOperation(file);
 if(previous&&!['completed','recovered','failed_safe'].includes(previous.phase))throw new Error('An interrupted operation exists; run instance recover first.');
 const current=await load(name),pinned=await pinImage(image);
 let operation=await advanceOperation(file,{schema:'animemo.operation/v1',id:randomUUID(),kind:'update',started_at:new Date().toISOString(),prior:current},'prepared');
 await compose(current,['stop','-t','15','app','worker']);
 operation=await advanceOperation(file,operation,'stopped');
 let backup;
 try{backup=await backupUnlocked(current);}catch(error){await compose(current,['up','-d','--wait','app','worker']);await advanceOperation(file,operation,'failed_safe');throw error;}
 const database=`animemo_${randomBytes(8).toString('hex')}`;
 const next={...current,image:pinned,database,rollback:{image:current.image,database:current.database,backup,updated_at:new Date().toISOString()}};
 operation=await advanceOperation(file,operation,'staging',{backup,next});
 try{
  await compose(current,['exec','-T','db','createdb','-U','animemo','-T','template0','-E','UTF8',database]);
  await restoreDatabase(current,backup,database);
  await compose(next,['run','--rm','--no-deps','-T','app','plugins-check']);
  operation=await advanceOperation(file,operation,'switching');
  await save(next);
  await compose(next,['up','-d','--wait','--no-deps','--force-recreate','app','worker']);
  operation=await advanceOperation(file,operation,'completed');
  return {name,image:pinned,backup,database,operation:operationReceipt(operation)};
 }catch(error){
  await compose(next,['stop','-t','15','app','worker']);
  if(operation.phase==='switching'){
   try{const rescue=await backupUnlocked(next);operation=await advanceOperation(file,operation,'recovering',{rescue});}
   catch{throw new Error('Candidate stopped, but preserving new writes failed. Both databases remain intact; run instance recover.');}
  }
  await save(current);
  try{await compose(current,['up','-d','--wait','--no-deps','--force-recreate','app','worker']);}
  catch{throw new Error('Update interrupted; original database and backup are preserved. Run instance recover.');}
  await advanceOperation(file,operation,'recovered');
  throw new Error('Update failed; original image and database are running again. '+error.message);
 }
});}
export async function recoverInstance(name){
 validName(name);
 // Recovery may clear only a lock whose recorded local process is gone.
 const lockFile=path.join(instancePath(name),'operation.lock');
 try{const owner=JSON.parse(await readFile(lockFile,'utf8'));if(!Number.isInteger(owner.pid)||owner.pid<1)throw new Error('Invalid operation lock.');let alive=true;try{process.kill(owner.pid,0);}catch(error){if(error.code==='ESRCH')alive=false;else throw error;}if(alive)throw new Error('The operation process is still running.');await rm(lockFile);}
 catch(error){if(error.code!=='ENOENT')throw error;}
 return lock(name,async()=>{
  const file=operationPath(name);let operation=await readOperation(file);
  if(!operation)throw new Error('No operation journal exists.');
  if(['completed','recovered','failed_safe'].includes(operation.phase))return {name,operation:operationReceipt(operation)};
  if(['install','restore'].includes(operation.kind))return {name,operation:operationReceipt(await finishInstall(operation))};
  const prior=operation.prior;await pinImage(prior.image);
  await compose(operation.next||prior,['stop','-t','15','app','worker']);
  if(['update','rollback'].includes(operation.kind)&&operation.phase==='switching'&&operation.next){const rescue=await backupUnlocked(operation.next);operation=await advanceOperation(file,operation,'recovering',{rescue});}
  await save(prior);await resume(prior,operation.services);
  operation=await advanceOperation(file,operation,'recovered');return {name,operation:operationReceipt(operation)};
 },{recovery:true});
}
export async function rollbackInstance(name,confirmed=false){if(!confirmed)throw new Error('Rollback switches to the database captured before the update. Review the backup and add --confirm-discard-new-writes; newer writes will first be backed up.');return lock(name,async()=>{
 const current=await load(name);if(!current.rollback)throw new Error('No successful update is available to roll back.');await pinImage(current.rollback.image);await validateBackup(current.rollback.backup);
 const file=operationPath(name);let operation=await begin(current,'rollback');await compose(current,['stop','-t','15','app','worker']);let rescue;
 try{rescue=await backupUnlocked(current);}catch(error){await resume(current);await advanceOperation(file,operation,'failed_safe');throw error;}
 const next={...current,image:current.rollback.image,database:`animemo_${randomBytes(8).toString('hex')}`,rollback:null};
 operation=await advanceOperation(file,operation,'staging',{rescue,next,backup:current.rollback.backup});
 try{
  await compose(current,['exec','-T','db','createdb','-U','animemo','-T','template0','-E','UTF8',next.database]);await restoreDatabase(next,current.rollback.backup,next.database,{clone:true});
  operation=await advanceOperation(file,operation,'switching');await save(next);await resume(next);await advanceOperation(file,operation,'completed');return {name,rescue};
 }catch(error){
  await compose(next,['stop','-t','15','app','worker']);
  if(operation.phase==='switching'){const rollbackRescue=await backupUnlocked(next);operation=await advanceOperation(file,operation,'recovering',{rescue:rollbackRescue,pre_rollback_rescue:rescue});}
  await save(current);await resume(current);await advanceOperation(file,operation,'recovered');throw error;
 }
});}
async function applyConfiguration(current,next){
 const file=operationPath(current.name),services=await runtimeServices(current);
 let operation=await begin(current,'configure',{next,services});
 await compose(current,['stop','-t','15','app','worker']);
 try{operation=await advanceOperation(file,operation,'switching');await save(next);await resume(next,services);await advanceOperation(file,operation,'completed');}
 catch(error){await compose(next,['stop','-t','15','app','worker']);await save(current);await resume(current,services);await advanceOperation(file,operation,'recovered');throw new Error('Configuration failed; previous configuration restored. '+error.message);}
 return {name:current.name,configured:true,health:services.length?'checked':'deferred_until_start'};
}
export async function configureInstance(name,changes={},apply=false){
 if(!apply){const {preview}=configurationPlan(await load(name),changes);return {name,applied:false,...preview};}
 return lock(name,async()=>{const current=await load(name),{next,preview}=configurationPlan(current,changes);if(!preview.changed)return {name,applied:false,...preview};if(next.bind!==(current.bind||'127.0.0.1')||next.port!==current.port)await checkPort(next.bind,next.port);return {...await applyConfiguration(current,next),applied:true,...preview};});
}
export async function configureExternal(name,file){return lock(name,async()=>{
 const current=await load(name),next={...current,integrations:parseIntegrations(await readFile(path.resolve(file),'utf8'))};return applyConfiguration(current,next);
});}
export async function doctorInstance({name,port=18082,bind='127.0.0.1'}={}){
 const checks=[];
 const check=async(id,fn)=>{try{const detail=await fn();checks.push({id,status:'PASS',detail});return detail;}catch{checks.push({id,status:'FAIL',detail:`${id} unavailable or invalid; inspect local configuration/environment. No credentials are included.`});return null;}};
 await check('node',()=>{const [major,minor]=process.versions.node.split('.').map(Number);if(major<24||(major===24&&minor<12))throw Error();return process.version;});
 await check('docker',()=>command(['info','--format','{{.ServerVersion}}'],{capture:true,timeout:10000}));
 await check('compose',async()=>{const v=await command(['compose','version','--short'],{capture:true,timeout:10000});const [major,minor,patch]=v.replace(/^v/,'').split('.').map(Number);if(!(major>2||(major===2&&(minor>24||(minor===24&&patch>=4)))))throw Error();return v;});
 await check('directory',async()=>{await access(root,constants.R_OK|constants.W_OK);return 'Project directory readable and writable';});
 await check('space',async()=>{const s=await statfs(root);return {available_bytes:s.bavail*s.bsize,low_space:s.bavail*s.bsize<1024**3};});
 if(name){
  const c=await check('configuration',()=>load(name).then(c=>({name:c.name,...publicConfig(c)})));
  if(c){
   const config=await load(name);
   await check('private_permissions',async()=>{if(process.platform==='win32')return 'Check current-user ACL on config and instance directory';for(const [file,mask] of [[instancePath(name),0o077],[path.join(instancePath(name),'config.json'),0o077]])if((await stat(file)).mode&mask)throw Error();return 'Private local files';});
   await check('operation',async()=>{const op=await readOperation(operationPath(name));if(!terminal(op))throw Error();return op?operationReceipt(op):'No pending operation';});
   const services=await check('services',async()=>{const s=await instanceStatus(name);const expected=['db','app',...(await hasWorker(config)?['worker']:[])];if(expected.some(n=>!s.services.some(v=>v.service===n&&v.state==='running'&&v.health==='healthy')))throw Error();return s.services;});
   await check('database',async()=>{if(await sql(config,'SELECT 1')!=='1')throw Error();return 'PostgreSQL responds';});
   await check('port',async()=>{if(services){await compose(config,['exec','-T','app','/app/animemo','healthcheck']);return {...publicConfig(config),listener:'owned by healthy app; public DNS/TLS not tested'};}await checkPort(config.bind,config.port);return 'Address/port available; service stopped';});
  }
 }else await check('port',async()=>{await checkPort(bind,port);return 'Address/port available';});
 return {status:checks.some(c=>c.status==='FAIL')?'FAIL':'PASS',name:name||null,checks};
}
export async function instanceStatus(name){const c=await load(name);const services=await compose(c,['ps','--format','json'],{capture:true});return {name:c.name,...publicConfig(c),image:c.image,application:await imageMetadata(c.image),database:c.database,rollback_available:!!c.rollback,services:services.split('\n').filter(Boolean).map(line=>{const s=JSON.parse(line);return {service:s.Service,state:s.State,health:s.Health};})};}
export async function stopInstance(name){return lock(name,async()=>compose(await load(name),['stop']));}
export async function startInstance(name){return lock(name,async()=>compose(await load(name),['up','-d','--wait','app','worker']));}
// Used only by isolated validation, requiring the random test-name namespace.
export async function removeTestInstance(name){if(!/^probe-[a-f0-9]{12}$/.test(name))throw new Error('Only isolated probe instances can be removed by the test helper.');return lock(name,async()=>{const c=await load(name);await compose(c,['down','--volumes','--remove-orphans']);},{recovery:true});}
async function main(){
 const [action,...args]=process.argv.slice(2);const options={};for(let n=0;n<args.length;n++){const key=args[n];if(key==='--confirm-discard-new-writes')options.confirmed=true;else if(key==='--apply')options.apply=true;else if(['--name','--image','--port','--origin','--bind','--backup','--archive','--output','--identity-file','--recipients-file','--integrations-file','--release'].includes(key)&&args[n+1]&&!args[n+1].startsWith('--'))options[key.slice(2)]=args[++n];else throw new Error(`Unknown or incomplete option: ${key}`);}
 let result;
 if(options.release){if(!['install','update','restore'].includes(action)||options.image)throw new Error('--release is for install/update/restore and cannot be combined with --image.');const release=await loadVerifiedRelease(options.release,{trustedRoot:process.env.ANIMEMO_TRUSTED_ROOT});options.image=release.local_image;}
 if(action==='doctor'){result=await doctorInstance(options);console.log(JSON.stringify(result,null,2));if(result.status==='FAIL')process.exitCode=1;return;}
 if(action==='configure'){console.log(JSON.stringify(await configureInstance(options.name,options,options.apply),null,2));return;}
 if(action==='restore-plan'){const plan=await restorePlan(options);console.log(JSON.stringify(plan,null,2));if(!plan.compatible)process.exitCode=1;return;}
 if(action==='backup-export'){console.log(JSON.stringify(await exportBackup({...options,recipientsFile:options['recipients-file']},validateBackup),null,2));return;}
 if(action==='backup-open'){console.log(JSON.stringify(await openBackup({...options,identityFile:options['identity-file']},validateBackup),null,2));return;}
 if(action==='configure-external'){if(!options['integrations-file'])throw new Error('Supply --integrations-file FILE.');console.log(JSON.stringify(await configureExternal(options.name,options['integrations-file']),null,2));return;}
 switch(action){case 'install':result=await installInstance(options);break;case 'restore':if(!options.backup)throw new Error('Supply --backup DIR and a new --name.');result=await installInstance(options);break;case 'backup':result={backup:await backupInstance(options.name)};break;case 'update':result=await updateInstance(options.name,options.image);break;case 'rollback':result=await rollbackInstance(options.name,options.confirmed);break;case 'recover':result=await recoverInstance(options.name);break;case 'status':result=await instanceStatus(options.name);break;case 'stop':await stopInstance(options.name);result={stopped:options.name};break;case 'start':await startInstance(options.name);result={started:options.name};break;case 'setup-code':if(!process.stdout.isTTY)throw new Error('Setup code is only shown in an interactive local terminal. It is also stored in the private instance config.');console.log((await load(options.name)).setupToken);return;default:throw new Error('Use doctor, configure, configure-external, install, restore-plan, restore, backup, backup-export, backup-open, update, rollback, recover, status, start, stop, or setup-code. See docs/instance-operations.md.');}
 console.log(JSON.stringify(result,null,2));
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(error=>{console.error(error.message);process.exitCode=1;});
