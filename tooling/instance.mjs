import { spawn } from 'node:child_process';
import { createReadStream,createWriteStream } from 'node:fs';
import { mkdir,readFile,writeFile,rename,open,rm,stat } from 'node:fs/promises';
import { createHash,randomBytes,randomUUID } from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { integrationValues,parseIntegrations } from './integrations.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const instances=path.join(root,'.local','instances');
const instancePath=name=>path.join(instances,name);
const secret=()=>randomBytes(32).toString('hex');
function validName(name){if(!/^[a-z][a-z0-9-]{0,31}$/.test(name||''))throw new Error('Instance name must contain 1–32 lowercase letters, digits or hyphens, beginning with a letter.');return name;}
function validPort(port){const n=Number(port);if(!Number.isInteger(n)||n<1024||n>65535)throw new Error('Port must be 1024–65535.');return n;}
function validOrigin(origin){const u=new URL(origin);if(u.origin!==origin||u.username||u.password||!['http:','https:'].includes(u.protocol)||(u.protocol==='http:'&&!['localhost','127.0.0.1','[::1]'].includes(u.hostname)))throw new Error('Origin must be an exact HTTPS origin, or loopback HTTP for development.');return origin;}
async function command(args,{env=process.env,input,output,capture=false}={}){
 const child=spawn('docker',args,{cwd:root,env,windowsHide:true,stdio:[input?'pipe':'ignore',output||capture?'pipe':'ignore','pipe']});
 let stderr='',stdout='';child.stderr.on('data',b=>{stderr=(stderr+b).slice(-8192);});if(capture)child.stdout.on('data',b=>{stdout+=b;if(stdout.length>2**20)child.kill();});
 const completion=new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',code=>code===0?resolve(stdout.trim()):reject(new Error(`Docker command failed (${code}): ${stderr.trim()}`)));});
 try{await Promise.all([completion,...(input?[pipeline(createReadStream(input),child.stdin)]:[]),...(output?[pipeline(child.stdout,createWriteStream(output,{flags:'wx',mode:0o600}))]:[])]);}catch(error){child.kill();throw error;}
 return stdout.trim();
}
function composeEnv(c){return {...process.env,...integrationValues(c.integrations),ANIMEMO_DB_PASSWORD:c.password,ANIMEMO_DATABASE:c.database,ANIMEMO_IMAGE:c.image,PUBLIC_ORIGIN:c.origin,ANIMEMO_INSTANCE_PORT:String(c.port),ANIMEMO_SETUP_TOKEN:c.setupToken,ANIMEMO_SECRET_KEY:c.secretKey};}
const composeArgs=c=>['compose','-p',c.project,'-f','deploy/compose.instance.yaml'];
const compose=(c,args,options={})=>command([...composeArgs(c),...args],{...options,env:composeEnv(c)});
const sql=(c,query,database=c.database)=>compose(c,['exec','-T','db','psql','-U','animemo','-d',database,'-v','ON_ERROR_STOP=1','-Atc',query],{capture:true});
async function load(name){validName(name);const c=JSON.parse(await readFile(path.join(instancePath(name),'config.json'),'utf8'));if(![c.password,c.setupToken,c.secretKey].every(value=>/^[a-f0-9]{64}$/.test(value))||c.name!==name||c.project!==`animemo-instance-${name}`||!/^[a-z][a-z0-9_]{0,62}$/.test(c.database)||!/^sha256:[a-f0-9]{64}$/.test(c.image))throw new Error('Instance configuration is invalid.');validPort(c.port);validOrigin(c.origin);return c;}
async function save(c){const target=path.join(instancePath(c.name),'config.json');const temp=target+'.'+randomUUID()+'.tmp';await writeFile(temp,JSON.stringify(c,null,2)+'\n',{mode:0o600,flag:'wx'});await rename(temp,target);}
async function lock(name,fn){const dir=instancePath(validName(name));await mkdir(dir,{recursive:true,mode:0o700});const target=path.join(dir,'operation.lock');let handle;try{handle=await open(target,'wx',0o600);}catch(error){if(error.code==='EEXIST')throw new Error('An instance operation is already in progress. A stale operation.lock may be removed only after confirming its recorded process has stopped.');throw error;}await handle.writeFile(JSON.stringify({pid:process.pid,started_at:new Date().toISOString()})+'\n');try{return await fn();}finally{await handle.close();await rm(target);}}
async function pinImage(image){if(!image||image.startsWith('-'))throw new Error('Supply a locally built or loaded application image.');const result=await command(['image','inspect',image,'--format','{{.Id}}'],{capture:true});if(!/^sha256:[a-f0-9]{64}$/.test(result))throw new Error('Cannot resolve local image digest.');return result;}
async function sha256(file){const hash=createHash('sha256');for await(const chunk of createReadStream(file))hash.update(chunk);return hash.digest('hex');}
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
  await writeFile(path.join(directory,'manifest.json'),JSON.stringify({schema:hasMedia?'animemo.instance/v2':'animemo.instance/v1',created_at:new Date().toISOString(),image:c.image,postgres_major:17,files},null,2)+'\n',{mode:0o600,flag:'wx'});
  return directory;
 }catch(error){await writeFile(path.join(directory,'INCOMPLETE'),'This backup did not complete. Do not restore it.\n',{mode:0o600});throw error;}
}
async function validateBackup(directory){
 directory=path.resolve(directory);try{await stat(path.join(directory,'INCOMPLETE'));throw new Error('Incomplete backups cannot be restored.');}catch(error){if(error.code!=='ENOENT')throw error;}const manifest=JSON.parse(await readFile(path.join(directory,'manifest.json'),'utf8'));
 const names=manifest.schema==='animemo.instance/v2'?['database.dump','media.zip','secrets.json']:['database.dump','secrets.json'];
 if(!['animemo.instance/v1','animemo.instance/v2'].includes(manifest.schema)||manifest.postgres_major!==17||!/^sha256:[a-f0-9]{64}$/.test(manifest.image)||Object.keys(manifest.files||{}).sort().join(',')!==names.join(','))throw new Error('Unsupported or invalid backup manifest.');
 for(const name of names){const info=await stat(path.join(directory,name));if(!info.isFile()||info.size!==manifest.files[name].bytes||await sha256(path.join(directory,name))!==manifest.files[name].sha256)throw new Error(`Backup checksum or size mismatch: ${name}`);}
 const keys=JSON.parse(await readFile(path.join(directory,'secrets.json'),'utf8'));if(!/^[a-f0-9]{64}$/.test(keys.secretKey)||!/^[a-f0-9]{64}$/.test(keys.setupToken))throw new Error('Backup encryption key or setup token is invalid.');
 integrationValues(keys.integrations);return {directory,manifest,keys};
}
const revokeExternalSQL=`DO $$ BEGIN
 IF to_regclass('email_tokens') IS NOT NULL THEN DELETE FROM email_tokens; UPDATE email_outbox SET state='cancelled',payload=NULL WHERE state IN ('pending','sending'); END IF;
 IF to_regclass('external_connections') IS NOT NULL THEN DELETE FROM external_oauth_states; UPDATE external_connections SET state='disconnected',tokens=NULL,generation=gen_random_uuid(),expires_at=NULL; UPDATE external_sync_jobs SET state='cancelled' WHERE state IN ('fetching','ready','applying'); END IF;
 END $$;`;
async function restoreDatabase(c,backup,database,{clone=false}={}){
 // Every restore target is either a brand-new instance or a fresh candidate database.
 await compose(c,['exec','-T','db','pg_restore','-U','animemo','-d',database,'--no-owner','--no-acl','--exit-on-error','--single-transaction'],{input:path.join(backup,'database.dump')});
 await sql(c,'DELETE FROM sessions; UPDATE users SET otp_pending=NULL,otp_pending_expires=NULL;',database);
 if(clone){
  await sql(c,revokeExternalSQL,database);
  const source=await validateBackup(backup);
  if(source.manifest.schema==='animemo.instance/v2')await compose({...c,database},['run','--rm','--no-deps','-T','--volume',`${path.join(source.directory,'media.zip').replaceAll('\\','/')}:/run/animemo-media.zip:ro`,'app','media-restore','/run/animemo-media.zip']);
 }
}
export async function installInstance({name,image,port=18082,origin,backup}={}){
 validName(name);port=validPort(port);origin=validOrigin(origin||`http://127.0.0.1:${port}`);
 return lock(name,async()=>{
  try{await stat(path.join(instancePath(name),'config.json'));throw new Error('Instance already exists; install never overwrites an existing instance.');}catch(error){if(error.code!=='ENOENT')throw error;}
  const source=backup?await validateBackup(backup):null;const pinned=await pinImage(image||source?.manifest.image||'animemo-next-app');
  if(source&&pinned!==source.manifest.image)throw new Error('Restore using the backup image first, then run update.');
  const c={schema:'animemo.instance-config/v1',name,project:`animemo-instance-${name}`,port,origin,image:pinned,database:'animemo',password:secret(),setupToken:source?.keys.setupToken||secret(),secretKey:source?.keys.secretKey||secret(),rollback:null};
  await save(c);await compose(c,['up','-d','--wait','db']);if(source)await restoreDatabase(c,source.directory,c.database,{clone:true});await compose(c,['up','-d','--wait','app']);return {name,origin,restored:!!source};
 });
}
export async function backupInstance(name){return lock(name,async()=>{const c=await load(name);const running=(await compose(c,['ps','--status','running','--services'],{capture:true})).split(/\r?\n/).includes('app');await compose(c,['stop','-t','15','app']);try{return await backupUnlocked(c);}finally{if(running)await compose(c,['up','-d','--wait','app']);}});}
export async function updateInstance(name,image){return lock(name,async()=>{
 const current=await load(name),pinned=await pinImage(image);await compose(current,['stop','-t','15','app']);let backup;
 try{backup=await backupUnlocked(current);}catch(error){await compose(current,['up','-d','--wait','app']);throw error;}
 const database=`animemo_${randomBytes(8).toString('hex')}`;
 const next={...current,image:pinned,database,rollback:{image:current.image,database:current.database,backup,updated_at:new Date().toISOString()}};
 try{
  await compose(current,['exec','-T','db','createdb','-U','animemo','-T','template0','-E','UTF8',database]);await restoreDatabase(current,backup,database);await compose(next,['run','--rm','--no-deps','-T','app','plugins-check']);await save(next);await compose(next,['up','-d','--wait','--no-deps','--force-recreate','app']);return {name,image:pinned,backup,database};
 }catch(error){await save(current);try{await compose(current,['up','-d','--wait','--no-deps','--force-recreate','app']);}catch{throw new Error('Update failed and automatic application recovery failed. Original database and backup are preserved; inspect this instance with Docker Compose.');}throw new Error('Update failed; original image and database are running again. '+error.message);}
});}
export async function rollbackInstance(name,confirmed=false){if(!confirmed)throw new Error('Rollback switches to the database captured before the update. Review the backup and add --confirm-discard-new-writes; newer writes will first be backed up.');return lock(name,async()=>{
 const current=await load(name);if(!current.rollback)throw new Error('No successful update is available to roll back.');await pinImage(current.rollback.image);await compose(current,['stop','-t','15','app']);let rescue;
 try{rescue=await backupUnlocked(current);}catch(error){await compose(current,['up','-d','--wait','app']);throw error;}
 const next={...current,image:current.rollback.image,database:`animemo_${randomBytes(8).toString('hex')}`,rollback:null};
 try{await validateBackup(current.rollback.backup);await compose(current,['exec','-T','db','createdb','-U','animemo','-T','template0','-E','UTF8',next.database]);await restoreDatabase(next,current.rollback.backup,next.database,{clone:true});await save(next);await compose(next,['up','-d','--wait','--no-deps','--force-recreate','app']);return {name,rescue};}catch(error){await save(current);await compose(current,['up','-d','--wait','--no-deps','--force-recreate','app']);throw error;}
});}
export async function configureExternal(name,file){return lock(name,async()=>{
 const current=await load(name),next={...current,integrations:parseIntegrations(await readFile(path.resolve(file),'utf8'))};
 await compose(current,['stop','-t','15','app']);
 try{await save(next);await compose(next,['up','-d','--wait','--no-deps','--force-recreate','app']);return {name,configured:true};}
 catch(error){await save(current);await compose(current,['up','-d','--wait','--no-deps','--force-recreate','app']);throw new Error('External configuration failed; previous configuration restored. '+error.message);}
});}
export async function instanceStatus(name){const c=await load(name);const services=await compose(c,['ps','--format','json'],{capture:true});return {name:c.name,origin:c.origin,image:c.image,database:c.database,rollback_available:!!c.rollback,services:services.split('\n').filter(Boolean).map(line=>{const s=JSON.parse(line);return {service:s.Service,state:s.State,health:s.Health};})};}
export async function stopInstance(name){return lock(name,async()=>compose(await load(name),['stop']));}
export async function startInstance(name){return lock(name,async()=>compose(await load(name),['up','-d','--wait']));}
// Used only by isolated validation, requiring the random test-name namespace.
export async function removeTestInstance(name){if(!/^probe-[a-f0-9]{12}$/.test(name))throw new Error('Only isolated probe instances can be removed by the test helper.');return lock(name,async()=>{const c=await load(name);await compose(c,['down','--volumes','--remove-orphans']);});}
async function main(){
 const [action,...args]=process.argv.slice(2);const options={};for(let n=0;n<args.length;n++){const key=args[n];if(key==='--confirm-discard-new-writes')options.confirmed=true;else if(['--name','--image','--port','--origin','--backup','--integrations-file'].includes(key)&&args[n+1]&&!args[n+1].startsWith('--'))options[key.slice(2)]=args[++n];else throw new Error(`Unknown or incomplete option: ${key}`);}
 let result;
 if(action==='configure-external'){if(!options['integrations-file'])throw new Error('Supply --integrations-file FILE.');console.log(JSON.stringify(await configureExternal(options.name,options['integrations-file']),null,2));return;}
 switch(action){case 'install':result=await installInstance(options);break;case 'restore':if(!options.backup)throw new Error('Supply --backup DIR and a new --name.');result=await installInstance(options);break;case 'backup':result={backup:await backupInstance(options.name)};break;case 'update':result=await updateInstance(options.name,options.image);break;case 'rollback':result=await rollbackInstance(options.name,options.confirmed);break;case 'status':result=await instanceStatus(options.name);break;case 'stop':await stopInstance(options.name);result={stopped:options.name};break;case 'start':await startInstance(options.name);result={started:options.name};break;case 'setup-code':if(!process.stdout.isTTY)throw new Error('Setup code is only shown in an interactive local terminal. It is also stored in the private instance config.');console.log((await load(options.name)).setupToken);return;default:throw new Error('Use install, restore, backup, update, rollback, status, start, stop, or setup-code. See docs/instance-operations.md.');}
 console.log(JSON.stringify(result,null,2));
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(error=>{console.error(error.message);process.exitCode=1;});
