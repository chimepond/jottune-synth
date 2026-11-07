// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {qrSvg} from './qr.mjs';

// The factory keeps browser state local and permits focused lifecycle checks.
export function createQrView({document=globalThis.document,clipboard=globalThis.navigator?.clipboard,encode=qrSvg}={}) {
  const node=(tag,id,text)=>{
    const element=document.createElement(tag);if(id)element.id=id;if(text!==undefined)element.textContent=text;return element;
  };
  const dialog=node('dialog','qr-dialog');dialog.className='qr-dialog';
  dialog.setAttribute('aria-labelledby','qr-heading');dialog.setAttribute('aria-describedby','qr-help');
  const header=node('div');header.className='qr-dialog-header';
  const heading=node('h2','qr-heading','Song link QR');
  const dismiss=node('button','qr-close','Close');dismiss.type='button';dismiss.setAttribute('aria-label','Close QR code');
  header.append(heading,dismiss);
  const help=node('p','qr-help','Scan with your phone’s camera to open this song link.');
  const graphic=node('div','qr-image');graphic.className='qr-image';graphic.setAttribute('role','img');graphic.setAttribute('aria-label','Song link QR code');
  const label=node('label',null,'Song link');label.htmlFor='qr-url';
  const urlText=node('textarea','qr-url');urlText.readOnly=true;urlText.rows=3;urlText.spellcheck=false;urlText.autocomplete='off';
  const actions=node('div');actions.className='qr-actions';
  const copy=node('button','qr-copy','Copy link'),download=node('button','qr-download','Download QR SVG');
  copy.type=download.type='button';copy.disabled=download.disabled=true;actions.append(copy,download);
  const status=node('p','qr-status');status.className='qr-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  dialog.append(header,help,graphic,label,urlText,actions,status);document.body.append(dialog);
  let target=null,currentUrl='',svg='',generation=0;

  function clear() {
    generation++;currentUrl='';svg='';urlText.value='';graphic.replaceChildren();graphic.hidden=true;
    copy.disabled=download.disabled=true;status.textContent='';heading.textContent='Song link QR';
  }
  function finishClose() {
    const restore=target;target=null;clear();
    if(restore?.isConnected && !restore.disabled && !restore.hidden)restore.focus();
  }
  function close() {
    if(dialog.open)dialog.close();
    finishClose();
  }
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  dialog.addEventListener('close',()=>{if(!dialog.open)finishClose();});
  dialog.addEventListener('click',event=>{
    if(event.target!==dialog)return;
    const rect=dialog.getBoundingClientRect();
    if(event.clientX<rect.left || event.clientX>rect.right || event.clientY<rect.top || event.clientY>rect.bottom)close();
  });
  dismiss.onclick=close;
  copy.onclick=async()=>{
    if(!currentUrl || !dialog.open)return;
    const token=generation,value=currentUrl;
    try {
      if(!clipboard?.writeText)throw Error('Clipboard unavailable');
      await clipboard.writeText(value);
      if(token===generation && dialog.open)status.textContent='Song link copied.';
    } catch {
      if(token!==generation || !dialog.open)return;
      status.textContent='Clipboard access is unavailable. The link is selected; copy it manually.';
      urlText.focus();urlText.select();
    }
  };
  download.onclick=()=>{
    if(!svg || !dialog.open)return;
    const objectUrl=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml'}));
    const anchor=node('a');anchor.href=objectUrl;anchor.download='song-link-qr.svg';
    document.body.append(anchor);anchor.click();anchor.remove();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
    status.textContent='QR SVG download requested.';
  };
  function open(value,{label='Song link',trigger=document.activeElement,selectLink=false}={}) {
    if(!dialog.open)target=trigger;
    clear();heading.textContent='QR code · '+String(label);
    let success=false;
    try {
      if(typeof value!=='string')throw TypeError('Use an HTTP or HTTPS song link without credentials.');
      const url=new URL(value);
      if(!['http:','https:'].includes(url.protocol) || url.username || url.password)
        throw TypeError('Use an HTTP or HTTPS song link without credentials.');
      if(url.searchParams.has('secret') || url.searchParams.has('withdrawal_secret') || /\/withdraw\/?$/.test(url.pathname))
        throw TypeError('Withdrawal links and credentials cannot be shared as song QR codes.');
      currentUrl=url.href;urlText.value=currentUrl;copy.disabled=false;
      const result=encode(currentUrl);svg=result.svg;
      // Only the local encoder generates this constant-format SVG. Neither the
      // input URL nor the untrusted display label is inserted as markup.
      graphic.innerHTML=svg;graphic.hidden=false;download.disabled=false;
      status.textContent='QR ready. The same link is available below.';success=true;
    } catch(error) {
      status.textContent=currentUrl
        ? error instanceof RangeError ? 'This link is too large for a QR code. Copy the link or use the player’s TL download.'
          : 'The QR code could not be generated. You can still copy the link.'
        : error instanceof TypeError ? error.message : 'Use a valid HTTP or HTTPS song link without credentials.';
    }
    if(!dialog.open)dialog.showModal();
    help.textContent = success ? 'Scan with your phone’s camera to open this song link.'
      : currentUrl ? 'Copy the song link below or save the TL file from the player.' : 'Use a valid song link to make a QR code.';
    if (selectLink && currentUrl) {
      status.textContent = 'Clipboard access is unavailable. The link is selected; copy it manually.' + (success ? '' : ' No QR code is available.');
      urlText.focus(); urlText.select();
    } else dismiss.focus();
    return success;
  }
  return {open,close};
}

let view;
export function openQr(url,options) { return (view ||= createQrView()).open(url,options); }
export function closeQr() { view?.close(); }
