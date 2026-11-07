// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Focus/lifecycle contracts with the real encoder; device acceptance is separate.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createQrView} from '../web/qr-view.mjs';
import {QR_MAX_BYTES} from '../web/qr.mjs';

const LINK='https://example.invalid/preview/r/'+'a'.repeat(64);
function fixture(clipboard) {
  const ids=new Map();let document;
  class Element {
    constructor(tag) {
      this.tagName=tag;this.children=[];this.listeners=new Map();this.textContent='';this.innerHTML='';
      this.hidden=false;this.disabled=false;this.value='';this.open=false;
      Object.defineProperty(this,'id',{set:value=>{this._id=value;ids.set(value,this);},get:()=>this._id});
    }
    get isConnected(){return this===document.body || Boolean(this.parent?.isConnected);}
    append(...children){for(const child of children){this.children.push(child);child.parent=this;}}
    replaceChildren(...children){for(const child of this.children)child.parent=null;this.children=[];this.innerHTML='';this.append(...children);}
    setAttribute(name,value){this[name]=value;}
    addEventListener(name,callback){this.listeners.set(name,callback);}
    focus(){if(this.isConnected && !this.disabled)document.activeElement=this;}
    select(){this.selected=true;}
    click(){if(!this.disabled)return this.onclick?.();}
    showModal(){this.open=true;}
    close(){this.open=false;queueMicrotask(()=>this.listeners.get('close')?.());}
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;}
  }
  document={createElement:tag=>new Element(tag),activeElement:null};document.body=new Element('body');document.activeElement=document.body;
  const trigger=new Element('button');document.body.append(trigger);trigger.focus();
  const view=createQrView({document,clipboard});
  const get=id=>{assert.ok(ids.has(id),'Missing '+id);return ids.get(id);};
  return {view,get,trigger,document};
}

test('closing clears QR and URL, restores the trigger and ignores a queued old close',async()=>{
  const page=fixture();assert.equal(page.view.open(LINK,{label:'A song',trigger:page.trigger}),true);
  assert.equal(page.get('qr-dialog').open,true);assert.equal(page.document.activeElement,page.get('qr-close'));
  assert.ok(page.get('qr-image').innerHTML.startsWith('<svg'));
  page.view.close();
  assert.equal(page.get('qr-url').value,'');assert.equal(page.get('qr-image').innerHTML,'');
  assert.equal(page.get('qr-download').disabled,true);assert.equal(page.get('qr-copy').disabled,true);
  assert.equal(page.document.activeElement,page.trigger);
  const next=LINK+'?release=2026-10-26-abcdef0';page.view.open(next);
  await Promise.resolve();
  assert.equal(page.get('qr-dialog').open,true);assert.equal(page.get('qr-url').value,next);
  page.view.close();await Promise.resolve();
});

test('Escape closes through the same cleanup and avoids focusing a disabled trigger',async()=>{
  const page=fixture();page.view.open(LINK);let prevented=false;
  page.get('qr-dialog').listeners.get('cancel')({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(page.get('qr-dialog').open,false);assert.equal(page.get('qr-url').value,'');
  assert.equal(page.document.activeElement,page.trigger);await Promise.resolve();
  page.view.open(LINK);page.trigger.disabled=true;page.view.close();
  assert.notEqual(page.document.activeElement,page.trigger);await Promise.resolve();
});

test('clipboard refusal selects the visible same link for manual recovery',async()=>{
  const page=fixture({async writeText(){throw Error('Permission denied');}});page.view.open(LINK);
  await page.get('qr-copy').click();
  assert.equal(page.get('qr-url').value,LINK);assert.equal(page.get('qr-url').selected,true);
  assert.equal(page.document.activeElement,page.get('qr-url'));
  assert.match(page.get('qr-status').textContent,/copy it manually/);
  page.view.close();await Promise.resolve();
});

test('late clipboard results cannot change a closed or replaced QR view',async()=>{
  let reject;
  const page=fixture({writeText:()=>new Promise((_,no)=>{reject=no;})});page.view.open(LINK);
  const pending=page.get('qr-copy').click();page.view.close();
  const next=LINK+'?next=1';page.view.open(next);const status=page.get('qr-status').textContent;
  reject(Error('Late refusal'));await pending;
  assert.equal(page.get('qr-status').textContent,status);assert.equal(page.get('qr-url').value,next);
  assert.equal(page.document.activeElement,page.get('qr-close'));assert.notEqual(page.get('qr-url').selected,true);
  page.view.close();await Promise.resolve();
});

test('oversized safe links keep copying available without a stale SVG',async()=>{
  const copied=[],page=fixture({async writeText(value){copied.push(value);}});
  page.view.open(LINK);const tooLong='https://example.invalid/#'+ 'x'.repeat(QR_MAX_BYTES+1);
  assert.equal(page.view.open(tooLong),false);
  assert.equal(page.get('qr-image').innerHTML,'');assert.equal(page.get('qr-image').hidden,true);
  assert.equal(page.get('qr-download').disabled,true);assert.equal(page.get('qr-copy').disabled,false);
  assert.match(page.get('qr-status').textContent,/too large/);
  assert.doesNotMatch(page.get('qr-help').textContent,/Scan/);
  await page.get('qr-copy').click();assert.deepEqual(copied,[tooLong]);
  page.view.close();await Promise.resolve();
});

test('invalid URLs and withdrawal credentials are never retained or offered for copy',async()=>{
  const page=fixture();
  for(const value of ['javascript:alert(1)','https://user:password@example.invalid/song','not a URL',
    {withdrawal_secret:'private'},'https://example.invalid/api/records/hash/withdraw',
    'https://example.invalid/?withdrawal_secret=private','https://example.invalid/?secret=private']) {
    page.view.open(LINK);assert.equal(page.view.open(value),false);
    assert.equal(page.get('qr-url').value,'');assert.equal(page.get('qr-image').innerHTML,'');
    assert.equal(page.get('qr-copy').disabled,true);assert.equal(page.get('qr-download').disabled,true);
    assert.ok(!page.get('qr-status').textContent.includes('password'));
  }
  page.view.close();await Promise.resolve();
});

test('untrusted display labels remain text outside the constant encoder markup',async()=>{
  const page=fixture(),label='<img src=x onerror=alert(1)>';
  assert.equal(page.view.open(LINK,{label}),true);
  assert.equal(page.get('qr-heading').textContent,'QR code · '+label);
  assert.equal(page.get('qr-heading').innerHTML,'');
  assert.ok(!page.get('qr-image').innerHTML.includes(label));
  assert.ok(!page.get('qr-image').innerHTML.includes(LINK));
  page.view.close();await Promise.resolve();
});


test('a failed initial copy opens with the same URL selected and a manual recovery message',async()=>{
  const page=fixture();page.view.open(LINK,{selectLink:true});
  assert.equal(page.get('qr-url').selected,true);
  assert.equal(page.document.activeElement,page.get('qr-url'));
  assert.match(page.get('qr-status').textContent,/Clipboard access.*copy it manually/);
  page.view.close();await Promise.resolve();
});
