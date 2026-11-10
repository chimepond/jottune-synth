// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Portable document metadata only. Native readers still own musical TL grammar.
export const tlVersion = 1, engineVersion = 1;
export const documentLimits = Object.freeze({bytes:1000000,lineBytes:1022});
const encoder = new TextEncoder(), decoder = new TextDecoder('utf-8',{fatal:true,ignoreBOM:true});
const fields = new Set(['title','collection','author','made_by','genre','tempo','model','utc_day','license','renderer_release','prompt_id']);
const label = /^[A-Za-z0-9._:+/@-]{1,160}$/;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const need = (condition,message) => {if(!condition)throw Error(message);};
const string = (value,max) => typeof value === 'string' && [...value].length >= 1 && [...value].length <= max &&
  value.trim() === value && value.normalize('NFC') === value && !/[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]/u.test(value);
const keys = (value,expected) => plain(value) && Object.keys(value).length === expected.length && expected.every(key => Object.hasOwn(value,key));

export function validateMetadata(value) {
  need(plain(value) && Object.keys(value).every(key=>fields.has(key)),'Unknown or invalid TL metadata fields.');
  for(const [key,item] of Object.entries(value)) {
    if(key==='title'||key==='collection'||key==='author')need(string(item,key==='title'?60:200),'Invalid TL metadata '+key+'.');
    else if(key==='made_by')need(['agent','human','mixed'].includes(item),'Invalid TL metadata made_by.');
    else if(key==='genre')need(item===null || typeof item==='string'&&label.test(item),'Invalid TL metadata genre.');
    else if(key==='tempo')need(item===null || keys(item,['min_bpm','max_bpm']) &&
      ['min_bpm','max_bpm'].every(name=>typeof item[name]==='number'&&Number.isFinite(item[name])&&item[name]>0&&item[name]<=Number.MAX_SAFE_INTEGER)&&
      item.min_bpm<=item.max_bpm,'Invalid TL metadata tempo.');
    else if(key==='model')need(item===null || keys(item,['id','source'])&&typeof item.id==='string'&&label.test(item.id)&&
      ['client-reported','user-stated'].includes(item.source),'Invalid TL metadata model.');
    else if(key==='utc_day')need(typeof item==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(item)&&Number(item.slice(0,4))>=1&&
      Number.isFinite(Date.parse(item+'T00:00:00Z'))&&new Date(item+'T00:00:00Z').toISOString().slice(0,10)===item,'Invalid TL metadata utc_day.');
    else if(key==='renderer_release')need(typeof item==='string'&&/^[A-Za-z0-9._-]{1,128}$/.test(item),'Invalid TL metadata renderer_release.');
    else need(typeof item==='string'&&label.test(item),'Invalid TL metadata '+key+'.');
  }
  return value;
}

function uniqueJSON(text) {
  // JSON.parse validates syntax; this lexical pass refuses keys it would overwrite.
  const tokens = text.match(/"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,]+/g) || [];
  const stack=[];
  for(let i=0;i<tokens.length;i++) {
    const token=tokens[i];
    if(token==='{')stack.push(new Set());
    else if(token==='[')stack.push(null);
    else if(token==='}'||token===']')stack.pop();
    else if(token.startsWith('"')&&tokens[i+1]===':') {
      const key=JSON.parse(token),set=stack.at(-1);
      need(set && !set.has(key),'Duplicate TL metadata JSON key.');set.add(key);
    }
  }
  return validateMetadata(JSON.parse(text));
}
function bytesText(value) {
  if(typeof value==='string') {
    need(!/[\uD800-\uDFFF]/u.test(value),'TL document contains an invalid Unicode surrogate.');
    return value;
  }
  need(ArrayBuffer.isView(value) || Object.prototype.toString.call(value)==='[object ArrayBuffer]','A TL document must be text or UTF-8 bytes.');
  return decoder.decode(value);
}
const reserved = line => /^[\t \r]*#[\t \r]*(?:jottune-tl|end-jottune-tl)/.test(line);
function version(value,name) {
  need(/^[1-9][0-9]*$/.test(String(value))&&Number.isInteger(Number(value))&&Number(value)<=4294967295,'Invalid unsigned 32-bit '+name+' version.');
  const number=Number(value), supported=name==='TL'?tlVersion:engineVersion;
  need(number<=supported,'Unsupported '+name+' version '+number+'; this reader supports up to '+supported+'.');return number;
}

export function parseDocument(value) {
  const text=bytesText(value),bytes=encoder.encode(text);
  need(bytes.length>0 && bytes.length<=documentLimits.bytes,'TL document exceeds the 1 MB input limit or is empty.');
  need(!text.startsWith('\uFEFF')&&!text.includes('\0'),'TL document must be UTF-8 without BOM or NUL.');
  const lines=text.split('\n');
  for(const line of lines)need(encoder.encode(line.replace(/\r$/,'')).length<=documentLimits.lineBytes,'TL document line exceeds 1022 content bytes.');
  let body=text,header='',metadata={},format=1,engine=1;
  const first=lines[0].replace(/\r$/,'');
  if(reserved(first)) {
    const marker=/^# jottune-tl ([1-9][0-9]*) ([1-9][0-9]*)$/.exec(first);
    need(marker,'Malformed TL document header.');format=version(marker[1],'TL');engine=version(marker[2],'engine');
    let end=-1;
    const payload=[];
    for(let i=1;i<lines.length;i++) {
      const line=lines[i].replace(/\r$/,'');
      if(line==='# end-jottune-tl'){end=i;break;}
      need(!reserved(line)&&line.startsWith('# '),'Malformed or repeated TL document header.');payload.push(line.slice(2));
    }
    need(end>=0&&end<lines.length-1,'Unterminated TL document header or missing music body.');
    metadata=uniqueJSON(payload.join('\n'));
    header=lines.slice(0,end+1).join('\n')+'\n';body=lines.slice(end+1).join('\n');
    need(body.length>0,'TL document is missing its music body.');
  }
  need(!body.split('\n').some(reserved),'A TL metadata header must occur once at the beginning.');
  return {body,header,metadata,tlVersion:format,engineVersion:engine};
}

export function composeDocument(body,metadata,format=1,engine=1) {
  const parsed=parseDocument(body);
  need(!parsed.header,'Supply a musical body without a metadata header.');
  version(format,'TL');version(engine,'engine');validateMetadata(metadata);
  const payload=JSON.stringify(metadata,null,2).split('\n').map(line=>'# '+line).join('\n');
  const text=`# jottune-tl ${format} ${engine}\n${payload}\n# end-jottune-tl\n${parsed.body}`;
  parseDocument(text);return text;
}
export function preserveDocument(source,canonical) {
  const original=parseDocument(source),rendered=parseDocument(canonical);
  const text=original.header?original.header+rendered.body:rendered.header?rendered.header+rendered.body:rendered.body;
  parseDocument(text);return text;
}
// Existing private stores may contain accepted legacy bytes outside current
// admission limits. Preserve their old identity; headers still require validation.
export function identityBody(value) {
  const text=bytesText(value);
  return text.split('\n').some(reserved)?parseDocument(text).body:text;
}

export async function bodySha256(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(parseDocument(value).body)))].map(x=>x.toString(16).padStart(2,'0')).join('');
}
export async function documentSha256(value) {
  parseDocument(value);
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(bytesText(value))))].map(x=>x.toString(16).padStart(2,'0')).join('');
}
