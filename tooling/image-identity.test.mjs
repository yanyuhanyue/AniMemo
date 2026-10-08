import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir,mkdtemp,writeFile,rm } from 'node:fs/promises';
import path from 'node:path';
import { archiveImageIdentity } from './image-identity.mjs';

const digest=data=>`sha256:${createHash('sha256').update(data).digest('hex')}`;
function tar(files){
 const chunks=[];
 for(const [name,data] of files){const body=Buffer.from(data),h=Buffer.alloc(512);h.write(name);h.write('0000644\0',100);h.write('0000000\0',108);h.write('0000000\0',116);h.write(body.length.toString(8).padStart(11,'0')+'\0',124);h.fill(32,148,156);h[156]=48;h.write('ustar\0',257);h.write(h.reduce((n,b)=>n+b,0).toString(8).padStart(6,'0')+'\0 ',148);chunks.push(h,body,Buffer.alloc((512-body.length%512)%512));}
 return Buffer.concat([...chunks,Buffer.alloc(1024)]);
}
test('Docker config and OCI manifest identities bind to the same verified archive bytes',async()=>{
 await mkdir('.local/tmp',{recursive:true});const dir=await mkdtemp(path.resolve('.local/tmp/image-identity-test-'));
 try{
  const config=JSON.stringify({os:'linux',architecture:'amd64',config:{Labels:{version:'test'}}}),configID=digest(config);
  const manifest=JSON.stringify({schemaVersion:2,config:{digest:configID},layers:[]}),manifestID=digest(manifest);
  const files=[['index.json',JSON.stringify({schemaVersion:2,manifests:[{digest:manifestID}]})],[`blobs/sha256/${manifestID.slice(7)}`,manifest],[`blobs/sha256/${configID.slice(7)}`,config]];
  const archive=path.join(dir,'image.tar');await writeFile(archive,tar(files));
  for(const id of [configID,manifestID]){const r=await archiveImageIdentity(archive,id);assert.equal(r.config_digest,configID);assert.deepEqual(r.identities,[manifestID,configID]);}
  await assert.rejects(()=>archiveImageIdentity(archive,`sha256:${'a'.repeat(64)}`),/matching/);
  await writeFile(archive,tar([...files.slice(0,-1),[files.at(-1)[0],config.replace('test','evil')]]));
  await assert.rejects(()=>archiveImageIdentity(archive,manifestID),/digest mismatch/);
  await writeFile(archive,tar([['manifest.json',JSON.stringify([{Config:`${configID.slice(7)}.json`}])],[`${configID.slice(7)}.json`,config]]));
  assert.equal((await archiveImageIdentity(archive,configID)).config_digest,configID);
  await writeFile(archive,tar([...files,files.at(-1)]));
  await assert.rejects(()=>archiveImageIdentity(archive,manifestID),/Duplicate/);
 }finally{await rm(dir,{recursive:true,force:true});}
});
