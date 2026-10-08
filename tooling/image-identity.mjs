import { open, mkdir, mkdtemp, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runProcess } from './process.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const digest=bytes=>`sha256:${createHash('sha256').update(bytes).digest('hex')}`;
const validDigest=value=>/^sha256:[a-f0-9]{64}$/.test(value);
const docker=(args,options={})=>runProcess('docker',args,{cwd:root,...options});

// Read bounded JSON metadata from a Docker save TAR without extracting paths or
// buffering layers. Docker/OCI names fit ordinary ustar headers; reject formats
// that would require interpreting extended filenames or numeric overflow.
async function archiveMetadata(file){
 const handle=await open(file,'r');
 try {
  const {size:total}=await handle.stat(),entries=new Map();let offset=0;
  while(offset+512<=total){
   const header=Buffer.alloc(512);if((await handle.read(header,0,512,offset)).bytesRead!==512)throw Error('Truncated image archive.');
   if(header.every(byte=>byte===0))break;
   const field=(start,end)=>header.subarray(start,end).toString('utf8').split('\0')[0].trim();
   const sizeText=field(124,136),checksumText=field(148,156);
   const checksum=header.reduce((sum,byte,index)=>sum+(index>=148&&index<156?32:byte),0);
   if(!/^[0-7]+$/.test(sizeText)||!/^[0-7]+$/.test(checksumText)||parseInt(checksumText,8)!==checksum)throw Error('Unsupported or damaged image TAR header.');
   const size=parseInt(sizeText,8),name=[field(345,500),field(0,100)].filter(Boolean).join('/');
   if(!Number.isSafeInteger(size)||offset+512+size>total||entries.size>=20000)throw Error('Invalid image archive size.');
   if(header[156]===0||header[156]===48){if(entries.has(name))throw Error('Duplicate image archive member.');entries.set(name,{offset:offset+512,size});}
   offset+=512+Math.ceil(size/512)*512;
  }
  const read=async name=>{
   const entry=entries.get(name);if(!entry||entry.size<1||entry.size>2**21)throw Error('Missing or oversized image metadata.');
   const bytes=Buffer.alloc(entry.size);if((await handle.read(bytes,0,bytes.length,entry.offset)).bytesRead!==bytes.length)throw Error('Truncated image metadata.');return bytes;
  };
  const blob=async id=>{if(!validDigest(id))throw Error('Invalid OCI digest.');const bytes=await read(`blobs/sha256/${id.slice(7)}`);if(digest(bytes)!==id)throw Error('Image metadata digest mismatch.');return JSON.parse(bytes);};
  const candidates=[];
  if(entries.has('index.json')){
   let visited=0;
   const visit=async(descriptor,ancestors=[])=>{
    if(++visited>64||ancestors.length>=5||candidates.length>=32||ancestors.includes(descriptor.digest))throw Error('Invalid OCI image graph.');
    const value=await blob(descriptor.digest),ids=[...ancestors,descriptor.digest];
    if(Array.isArray(value.manifests)){for(const child of value.manifests)await visit(child,ids);return;}
    if(!validDigest(value.config?.digest))throw Error('Invalid OCI image configuration.');
    const config=await blob(value.config.digest);
    if(config.os==='linux'&&config.architecture==='amd64')candidates.push({config,config_digest:value.config.digest,identities:[...ids,value.config.digest]});
   };
   const index=JSON.parse(await read('index.json'));if(!Array.isArray(index.manifests)||index.manifests.length>32)throw Error('Invalid OCI index.');
   for(const descriptor of index.manifests)await visit(descriptor);
  }else{
   const manifest=JSON.parse(await read('manifest.json'));if(!Array.isArray(manifest)||manifest.length>32)throw Error('Invalid Docker image manifest.');
   for(const item of manifest){
    const match=/^(?:blobs\/sha256\/)?([a-f0-9]{64})(?:\.json)?$/.exec(item.Config||'');if(!match)throw Error('Invalid image configuration path.');
    const bytes=await read(item.Config),id=`sha256:${match[1]}`;if(digest(bytes)!==id)throw Error('Image metadata digest mismatch.');
    const config=JSON.parse(bytes);if(config.os==='linux'&&config.architecture==='amd64')candidates.push({config,config_digest:id,identities:[id]});
   }
  }
  return candidates;
 }finally{await handle.close();}
}

export async function archiveImageIdentity(file,declared){
 const candidates=(await archiveMetadata(file)).filter(image=>!declared||image.identities.includes(declared));
 if(candidates.length!==1)throw Error('Archive does not identify one matching Linux amd64 image.');
 return candidates[0];
}

export async function resolveArchivedImage(file,declared){
 const identity=await archiveImageIdentity(file,declared);
 for(const id of [declared,...identity.identities].filter((value,index,array)=>value&&array.indexOf(value)===index)){
  let actual;try{actual=JSON.parse(await docker(['image','inspect',id,'--format','{{json .}}'],{capture:true}));}catch{continue;}
  if(!identity.identities.includes(actual.Id)||actual.Os!=='linux'||actual.Architecture!=='amd64')throw Error('Loaded image content identity differs from the archive.');
  return {actual,config_digest:identity.config_digest};
 }
 throw Error('The image from this archive is not loaded in the local Docker engine.');
}

const configDigests=new Map();
export async function imageConfigDigest(image){
 const info=JSON.parse(await docker(['image','inspect',image,'--format','{{json .}}'],{capture:true}));
 if(!validDigest(info.Id))throw Error('Invalid local image identity.');
 // Classic storage exposes the config digest directly. Containerd exposes the
 // manifest/index instead; Docker save supplies its linked configuration bytes.
 if(!info.Descriptor)return info.Id;
 if(configDigests.has(info.Id))return configDigests.get(info.Id);
 const parent=path.join(root,'.local/tmp');await mkdir(parent,{recursive:true});const dir=await mkdtemp(path.join(parent,'image-identity-'));
 try{
  const file=path.join(dir,'image.tar');await docker(['image','save',info.Id],{output:file,timeout:600000});
  const identity=await archiveImageIdentity(file);configDigests.set(info.Id,identity.config_digest);return identity.config_digest;
 }finally{await rm(dir,{recursive:true,force:true});}
}
