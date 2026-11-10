// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {toSvg, toWav, spectrogram, songStructure, songStructureText} from './media.mjs';
import {parseDocument} from './tl-document.mjs';
import {limits, options, selectEngine, checkRecord, checkInspection, verification} from './policy.mjs';
import {fetchRecord, isRecordLink, resolveLink, portableLink, libraryLink, linkProblem} from './links.mjs';
import {openQr, closeQr} from './qr-view.mjs';
import {createCatalog, catalogJSON, checkCatalogDocument} from './catalog.mjs';
import {songPreview} from './song-preview.mjs';
import {createInstrumentView} from './instrument-view.mjs';
import {createSpatialSampler} from './spatial.mjs';
import {compileScore, listRecipes, recipe, recipeGain} from './authoring.mjs';

const $ = id => document.getElementById(id);
const pageTitle = document.title || 'JotTune Synth';
function playerStatus(message) {
  if ($('status').textContent !== message) $('status').textContent = message;
  const hint = message || 'Choose a song, press Load, then Play.';
  if ($('rail-status').textContent !== hint) $('rail-status').textContent = hint;
  updateRailFocus();
}
// Rail text boxes are Tab stops only while their text is clipped, so the keyboard can
// still scroll them. Layout is read again only when their text, width or visibility changes.
let railFocusKey = '';
function updateRailFocus() {
  const nodes = [$('rail-selection'), $('rail-loaded-song'), $('rail-status')];
  const key = [...nodes.map(node => node.textContent), $('section-nav').hidden, window.innerWidth].join('\n');
  if (key === railFocusKey) return;
  railFocusKey = key;
  for (const node of nodes) {
    if (node.scrollHeight > node.clientHeight + 1) node.setAttribute('tabindex', '0');
    else if (node !== document.activeElement) node.removeAttribute('tabindex');
  }
}
document.fonts?.ready.then(() => { railFocusKey = ''; updateRailFocus(); }, () => {});
let sectionNavigation, autoMoveEpoch = 0, currentSection = 'library-panel', mobileSongActionReady = false;
function currentSectionChanged(id) {
  currentSection = id;
  if (mobileSongActionReady) updateMobileSongAction();
}
// Automatic switch-off is announced, and the rail offers a one-step way back.
function autoMoveNotice(off) {
  $('auto-move-off').hidden = !off;
  $('auto-move-status').textContent = off ? 'Auto move off. Turn it back on in Sections.' : '';
}
function stopAutoMove() {
  const wasOn = $('auto-move').checked;
  $('auto-move').checked = false; autoMoveEpoch++;
  if (wasOn) autoMoveNotice(true);
}
function followSection(id, epoch = autoMoveEpoch) {
  if (!$('auto-move').checked || epoch !== autoMoveEpoch) return;
  const transport = id === 'live-views' && ['play','play-right','rail-play','mobile-play'].some(key => $(key) === document.activeElement);
  sectionNavigation?.jump(id, {focus:!transport});
  if (transport) {
    const compact = typeof matchMedia === 'function' && matchMedia('(max-width: 1099px)').matches;
    $(compact ? 'mobile-play' : 'rail-play').focus({preventScroll:true});
  }
}
$('auto-move').addEventListener('change', () => { autoMoveEpoch++; autoMoveNotice(false); });
$('auto-move-on').onclick = () => { $('auto-move').checked = true; autoMoveEpoch++; autoMoveNotice(false); $('auto-move').focus(); };

// Page navigation is independent of render/playback state. In particular, the
// fragment belongs to portable records, so section jumps never rewrite it.
function prepareSectionNavigation() {
  const nav = $('section-nav'), toggle = $('section-toggle'), close = $('section-close'), dockToggle = $('dock-sections');
  const header = document.querySelector('.app-header');
  // A hosted page can add a sharing panel after the player; it gets a section link of its own.
  // The standalone page has no such panel and no link to one.
  if ($('publication-panel') && $('publication-summary')) {
    const link = document.createElement('a'), number = document.createElement('span');
    link.href = '#publication-panel'; link.dataset.section = 'publication-panel'; link.dataset.sectionFocus = 'publication-summary';
    number.setAttribute('aria-hidden', 'true'); number.textContent = '07';
    link.append(number, ' Sharing');
    nav.querySelector('.auto-move-control').before(link);
  }
  const links = [...nav.querySelectorAll('a[data-section]')];
  const navigationLinks = [...links,...$('mobile-section-nav').querySelectorAll('a[data-section]')];
  const compact = matchMedia('(max-width: 1099px)');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let open = false, queued = false, requested = null, jumpTimer, opener = toggle;
  let manualScroll = null, manualScrollTimer;
  let documentY = window.scrollY;
  const isToggle = node => toggle.contains(node) || dockToggle.contains(node);
  function clearManualScroll(expected = manualScroll) {
    if (manualScroll !== expected) return;
    manualScroll = null; clearTimeout(manualScrollTimer);
  }
  function setOpen(value, returnFocus = false) {
    open = compact.matches && value;
    nav.hidden = compact.matches && !open;
    toggle.hidden = !compact.matches;
    close.hidden = !compact.matches;
    toggle.setAttribute('aria-expanded', String(open));
    dockToggle.setAttribute('aria-expanded', String(open));
    // Short windows let the header scroll away, so focus returns to the dock's
    // Sections button when that is the one that opened the menu.
    if (returnFocus && compact.matches) (opener === dockToggle && dockToggle.getClientRects().length ? dockToggle : toggle).focus({preventScroll:true});
  }
  // A sharing panel can be introduced by the section just before it; count that section as part of it.
  function sectionStart(target) {
    const before = target.previousElementSibling;
    return before?.querySelector(`[aria-controls="${target.id}"]`) ? before : target;
  }
  function markCurrent(id) {
    currentSectionChanged(id);
    for (const link of navigationLinks) {
      if (link.dataset.section === id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
  }
  function sectionAtViewport() {
    const edge = Math.max(0, header.getBoundingClientRect().bottom) + 24;
    let current = links[0].dataset.section;
    for (const link of links) {
      if (sectionStart($(link.dataset.section)).getBoundingClientRect().top <= edge) current = link.dataset.section;
    }
    return current;
  }
  function updateCurrent() {
    queued = false;
    // Keep the chosen item current during smooth movement. At the bottom a
    // short target may never reach the top; the next scroll updates it normally.
    if (requested) return;
    const current = sectionAtViewport();
    if (manualScroll?.moved && current !== manualScroll.from) { stopAutoMove(); clearManualScroll(); }
    markCurrent(current);
  }
  function scheduleCurrent() {
    if (!queued) { queued = true; requestAnimationFrame(updateCurrent); }
  }
  function syncLayout() {
    document.documentElement.style.setProperty('--header-height', `${getComputedStyle(header).position === 'static' ? 0 : header.getBoundingClientRect().height}px`);
    const dockHeight = $('mobile-transport').getBoundingClientRect().height;
    if (dockHeight) document.documentElement.style.setProperty('--mobile-dock-height', `${dockHeight}px`);
    scheduleCurrent();
  }
  function jump(link, {focus:moveFocus = true} = {}) {
    clearManualScroll();
    const target = $(link.dataset.section), focus = $(link.dataset.sectionFocus);
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
    requested = target.id;
    markCurrent(requested);
    setOpen(false);
    if (moveFocus) focus.focus({preventScroll:true});
    sectionStart(target).scrollIntoView({block:'start', behavior:reduced.matches ? 'auto' : 'smooth'});
    clearTimeout(jumpTimer);
    jumpTimer = setTimeout(() => { requested = null; }, 1500);
  }
  for (const link of navigationLinks) link.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    stopAutoMove();
    jump(link);
  });
  document.querySelector('.skip-link').addEventListener('click', event => {
    event.preventDefault();
    stopAutoMove();
    jump(links.find(link => link.dataset.section === 'player-panel'));
  });
  for (const button of [toggle, dockToggle]) button.addEventListener('click', () => { opener = button; setOpen(!open); });
  close.addEventListener('click', () => setOpen(false, true));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && open) { event.preventDefault(); setOpen(false, true); }
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) noteManualScroll(event);
  });
  document.addEventListener('pointerdown', event => {
    if (!nav.contains(event.target) && !isToggle(event.target)) {
      requested = null;
      if (open) setOpen(false);
    }
  });
  document.addEventListener('focusin', event => {
    if (open && !nav.contains(event.target) && !isToggle(event.target)) setOpen(false);
    if (requested && !$(requested).contains(event.target) && !nav.contains(event.target) && !isToggle(event.target)) {
      requested = null; scheduleCurrent();
    }
  });
  function noteManualScroll(event) {
    // A wheel over a fixed button or End on a focused button can scroll the
    // document. Observe that movement rather than guessing from the target.
    // Consumed jog gestures and internal-only scrolling must leave no intent.
    if (event.defaultPrevented) { clearManualScroll(); return; }
    // Passive wheel dispatch can arrive after compositor scrolling; the last
    // marked section still describes where this gesture started.
    const gesture = manualScroll ||= {from:currentSection, moved:false};
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!gesture.moved) clearManualScroll(gesture);
    }));
  }
  for (const type of ['wheel', 'touchmove']) window.addEventListener(type, noteManualScroll, {passive:true});
  window.addEventListener('scroll', event => {
    if (event.target !== document && event.target !== window) return;
    const moved = window.scrollY !== documentY; documentY = window.scrollY;
    if (manualScroll && moved) {
      requested = null; manualScroll.moved = true;
      clearTimeout(manualScrollTimer);
      manualScrollTimer = setTimeout(() => {updateCurrent(); clearManualScroll();},160);
    }
    scheduleCurrent();
  }, {passive:true});
  window.addEventListener('scrollend', event => {
    if (event.target !== document && event.target !== window) return;
    requested = null; clearTimeout(jumpTimer); updateCurrent(); clearManualScroll();
  });
  window.addEventListener('resize', syncLayout, {passive:true});
  $('library-panel').addEventListener('toggle',scheduleCurrent);
  compact.addEventListener('change', () => {
    const focused = document.activeElement;
    const hidingLink = compact.matches && nav.contains(focused);
    const hidingControl = !compact.matches && (focused === toggle || focused === close || focused === dockToggle);
    setOpen(false, hidingLink);
    if (hidingControl) (links.find(link => link.hasAttribute('aria-current')) || links[0]).focus({preventScroll:true});
  });
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(syncLayout);observer.observe(header);observer.observe($('mobile-transport'));
  }
  setOpen(false);
  syncLayout();
  sectionNavigation = {jump: (id, options) => { const link = links.find(item => item.dataset.section === id); if (link) jump(link, options); }};
}
prepareSectionNavigation();
const assetBase = new URL('./', import.meta.url);
const serviceWorkerURL = new URL('sw.js', assetBase);
const serviceWorker = (() => {try {return navigator.serviceWorker;} catch {return null;}})();
let offlineSetupPending = true, offlineSetupSlow = false, offlineNeedsReload = false;
function hasOfflineControl() {
  try {return serviceWorker?.controller?.scriptURL === serviceWorkerURL.href;} catch {return false;}
}
function updateOfflineStatus() {
  $('offline-status').textContent = hasOfflineControl() ? 'Offline app is ready. Used renderers may be cached; save the TL locally.'
    : offlineNeedsReload ? 'Offline app is installed; reload the page to use it offline. Online playback is available now.'
    : offlineSetupPending ? (offlineSetupSlow ? 'Offline setup is still running; online playback is available now.' : 'Preparing offline support…')
    : 'Offline setup is unavailable; online playback remains available.';
}
function prepareOfflineSupport() {
  const service = serviceWorker;
  if (!service) { offlineSetupPending = false; updateOfflineStatus(); return Promise.resolve(); }
  return new Promise(resolve => {
    let settled = false, installing;
    const finish = available => {
      if (settled) return;
      settled = true; offlineSetupPending = false; clearTimeout(timer);
      service.removeEventListener('controllerchange', controlled);
      installing?.removeEventListener('statechange', changed);
      updateOfflineStatus();
      resolve();
    };
    const controlled = () => { if (hasOfflineControl()) finish(true); };
    const changed = () => {
      controlled(); if (settled) return;
      if (installing?.state === 'redundant') { finish(false); return; }
      // An active worker that does not control this page (for example after a
      // hard reload, which bypasses it) will not change state again. Say so,
      // and keep listening in case it claims the page later.
      if (installing?.state === 'activated') { offlineNeedsReload = true; updateOfflineStatus(); }
    };
    // Offline installation is optional. Slow storage/network must not prevent
    // online rendering indefinitely, and Cancel does not wait for this timer.
    // After it the status says setup is still running and keeps listening, so a
    // late installation or failure still updates it.
    const timer = setTimeout(() => { offlineSetupSlow = true; updateOfflineStatus(); resolve(); }, 15000);
    service.addEventListener('controllerchange', controlled);
    Promise.resolve().then(() => service.register(serviceWorkerURL, {scope:assetBase.pathname})).then(registration => {
      if (settled) return;
      controlled(); if (settled) return;
      installing = registration.installing || registration.waiting || registration.active;
      if (!installing) { finish(false); return; }
      installing.addEventListener('statechange', changed); changed();
    }).catch(() => finish(false));
    controlled();
  });
}
void prepareOfflineSupport();
let activeWorker, activeReject, lastResult, audio, source, started = 0, elapsed = 0;
// Compiled native modules are private view state, never part of the public API.
const spatialModules = new WeakMap();
let analysisJob, analysisResult, analysisSequence = 0;
let generation = 0, expectedRecordSha = null, loadedRelease;
let rendering = false, playbackRequest = 0, playbackBuffer, playbackBufferResult;
const mediaSession = (() => {try {return navigator.mediaSession || null;} catch {return null;}})();
let mediaTitle = null;
let spectrumController, spectrumResult, displayView = 'live';
let liveOnscreen = true;
let profileBase = null, appliedProfile = null, loadedIdentity = null, linkedMetadata = null, pendingSongLoad = null;
const instrumentView = createInstrumentView($('live-panel'), {onSeek: seconds => seekTo(seconds)});
instrumentView.setVisible(!document.hidden);
function liveVisible() {return displayView === 'live' && !document.hidden && liveOnscreen;}
let liveAnimation = null, lastLiveFrame = null;
function synchronizeLiveAnimation() {
  if (!source || !liveVisible()) {
    if (liveAnimation !== null) cancelAnimationFrame(liveAnimation);
    liveAnimation = lastLiveFrame = null;
  } else if (liveAnimation === null) liveAnimation = requestAnimationFrame(drawLiveFrame);
}
function drawLiveFrame(timestamp) {
  liveAnimation = null;
  if (!source || !liveVisible()) { lastLiveFrame = null; return; }
  if (lastLiveFrame === null || timestamp - lastLiveFrame >= 1000 / 30) {
    instrumentView.update(position(), true);
    lastLiveFrame = lastLiveFrame === null ? timestamp
      : timestamp - (timestamp - lastLiveFrame) % (1000 / 30);
  }
  liveAnimation = requestAnimationFrame(drawLiveFrame);
}
if (typeof IntersectionObserver === 'function') {
  const observer = new IntersectionObserver(entries => {
    const entry = entries.find(value => value.target === $('live-panel'));if (!entry) return;
    // At the default zero threshold, an edge-adjacent entry can have ratio 0.
    // Waiting for a positive ratio can leave the view asleep with no new event.
    liveOnscreen = entry.isIntersecting;
    instrumentView.setVisible(liveVisible());
    synchronizeLiveAnimation();
    if (liveVisible()) updateTransport();
  });
  observer.observe($('live-panel'));
}
const registryPromise = fetch(new URL('engines.json', assetBase)).then(response => {
  if (!response.ok) throw Error('Build the player with make web first.');
  return response.json();
});
registryPromise.catch(() => {}); // A render reports the failure; opening an idle page must not reject globally.

function verificationBudgetReason(result = lastResult) {
  if (result?.verdict !== 'valid' || result.info?.render_bytes_lower_bound === undefined) return '';
  try {checkInspection(result.info,'verify');return '';}
  catch (error) {return error.message;}
}
function busy(value) {
  rendering = value;
  const budgetReason = verificationBudgetReason();
  $('render').disabled = value || !!budgetReason;
  $('render').setAttribute('title',budgetReason || 'Verify the current record.');
  $('listen').disabled = value;
  $('cancel').hidden = !value;
  $('verify-saved').disabled = value || !lastResult?.canonical?.length || lastResult.verdict !== 'valid' || lastResult.verified || !!budgetReason;
  $('verify-saved').setAttribute('title',budgetReason || 'Verify the prepared saved record.');
  updateProfileControls();
  updateTransport();
}
function cancel() {
  cancelAnalysis();
  generation++;
  const reject = activeReject; activeReject = null;
  activeWorker?.terminate(); activeWorker = null;
  if (reject) {
    busy(false); playerStatus('Rendering cancelled.');
    reject(Error('Rendering cancelled.'));
  }
}
function invalidate(retainProfile = false, retainRelease = false) {
  pendingSongLoad?.controller.abort(); pendingSongLoad = null;
  updateOfflineStatus();
  closeQr();
  cancel(); stop(); lastResult = null; analysisResult = null;
  $('analysis-status').textContent = 'Analyze the prepared audio without rendering again.';
  $('analysis-output').textContent = ''; $('analysis-download').disabled = true; $('analysis-cancel').hidden = true; expectedRecordSha = null;
  $('song-structure').textContent = 'Load a song to describe its native note commands.';
  if (!retainRelease) loadedRelease = undefined;
  playbackBuffer = null; playbackBufferResult = null;
  instrumentView.clear();
  if (!retainProfile) {
    profileBase = null; appliedProfile = null; loadedIdentity = null; loadedSong = null; linkedSelectionActive = false;
    linkedMetadata?.controller.abort(); clearTimeout(linkedMetadata?.timer); linkedMetadata = null;
  }
  updateProfileControls(true);
  spectrumController?.abort(); spectrumController = null; spectrumResult = null;
  $('spectrum-cancel').hidden = true;
  $('spectrum-status').textContent = 'Render a song to inspect its sound.';
  for (const id of ['play', 'stop', 'wav-download', 'spectrum', 'tl-download', 'svg-download', 'share', 'show-qr', 'png-download', 'verify-saved', 'analyze-audio']) $(id).disabled = true;
  $('verdict').textContent = 'Not ready'; $('verdict').className = 'badge';
  $('timeline').textContent = 'Render the current record to inspect it.'; $('facts').replaceChildren();
  $('raw').textContent = 'No output for the current text.'; $('spectrogram').hidden = true;
  $('time').value = '0:00 / 0:00';
  updateTransport();
  window.dispatchEvent?.(new CustomEvent('intfm-invalidate', {detail: null}));
}
function clear() { invalidate(); $('record').value = ''; playerStatus('Record cleared.'); }
$('record').addEventListener('input', () => { invalidate(false, true); playerStatus('Record changed. Listen or verify.'); });

function rawRender(recordText, config) {
  cancel();
  const requestGeneration = generation;
  return new Promise((resolve, reject) => {
    let worker = null;
    const spatialDeadlineMs = Date.now() + limits.renderSeconds * 1000;
    const timer = setTimeout(() => rejectRequest(Error('Rendering exceeded the 120-second browser limit.')), limits.renderSeconds * 1000);
    const finish = () => {
      clearTimeout(timer); worker?.terminate();
      if (activeReject === rejectRequest) { activeReject = null; activeWorker = null; }
    };
    const rejectRequest = error => { finish(); reject(error); };
    activeReject = rejectRequest;
    registryPromise.then(registry => {
      if (requestGeneration !== generation || activeReject !== rejectRequest) return;
      let engine;
      try { engine = selectEngine(registry, config.release); }
      catch (error) {
        finish(); resolve({verdict: 'mismatch: engine build not available', stderr: error.message, code: 3}); return;
      }
      const controlled = hasOfflineControl();
      updateOfflineStatus();
      worker = new Worker(new URL('worker.mjs', assetBase), {type: 'module'}); activeWorker = worker;
      worker.onmessage = ({data}) => {
        if (requestGeneration !== generation || activeReject !== rejectRequest) return;
        if (data.spatialModule) spatialModules.set(data, data.spatialModule);
        delete data.spatialModule;
        if (controlled && data.verdict === 'valid') updateOfflineStatus();
        finish(); resolve(data);
      };
      worker.onerror = event => rejectRequest(Error(event.message || 'Worker failed.'));
      worker.postMessage({recordText, engine, mode: config.mode, spatialDeadlineMs, ...(config.profile ? {profile: config.profile} : {}),
        ...(config.composition ? {operation:'compose-selection',...config.composition} : {})});
    }).catch(rejectRequest);
  });
}

const guide = async () => {
  const response = await fetch(new URL('browser-guide.txt', assetBase));
  if (!response.ok) throw Error('Browser guide is not available.');
  return response.text();
};
const reference = async (name = 'params') => {
  if (!['params', 'gen-params'].includes(name)) throw Error('Reference must be params or gen-params.');
  const response = await fetch(new URL('reference/' + name + '.txt', assetBase));
  if (!response.ok) throw Error('Native reference is not packaged. Run make web.');
  return response.text();
};
const describe = () => ({name: 'jottune-synth', title: 'JotTune Synth', format: 'JotTune Timeline (.tl)',
  rendering: 'local WebAssembly worker',
  operations: ['describe', 'compose', 'recipes', 'recipe', 'recipeGain', 'analyze', 'cancelAnalysis', 'render', 'listen', 'verify', 'verifySaved', 'cancel', 'clear', 'loadLink', 'prepareLink', 'result', 'metrics', 'toWav', 'toSvg', 'guide', 'reference', 'showQr', 'closeQr'],
  modes: {verify: 'Two renders; checks hash and block independence.', listen: 'One render; block independence is unchecked.'},
  profiles: {halfWidths:[30,45,60,75,90], unit:'degrees each side', mirror:'reverse low/high sides',
    behavior:'Native Keys projection; fully verifies the source and derived ordinary-stereo record. Does not change the input file.'},
  defaultMode: 'verify', limits,
  references: {start: new URL('agent-start.txt', assetBase).href, authoring: new URL('authoring.txt', assetBase).href, client: new URL('agent.mjs', assetBase).href, analysis: new URL('analysis.txt', assetBase).href, native: new URL('native-reference.txt', assetBase).href, parameters: new URL('reference/params.txt', assetBase).href, generators: new URL('reference/gen-params.txt', assetBase).href},
  verdicts: ['valid', 'refused', 'mismatch'], integerResults: 'decimal strings',
  composition: 'compose(score, {release,soloStreams|muteStreams}) compiles musical-time score v1 without rendering. Optional stream selection natively inspects the entire original and selected draft; it preserves controls/effects and is not additive mix stems. Recipes and recipeGain provide native declarations. Direct TL supports the complete instrument. See authoring reference.',
  versioning: 'Explicit releases select trusted registry entries without fallback. Bare TL uses the installed default and must match its preserved hash.'});

async function compose(score, value = {}) {
  const config = options(value);
  if (value.soloStreams !== undefined || value.muteStreams !== undefined) {
    if (value.soloStreams !== undefined && value.muteStreams !== undefined)
      throw Error('Choose either soloStreams or muteStreams.');
    if (config.profile) throw Error('Score stream selection cannot also apply a Keys profile.');
    const selection = value.soloStreams !== undefined ? {soloStreams:value.soloStreams} : {muteStreams:value.muteStreams};
    // Freeze the source and register cancellation before any registry/offline
    // wait. The selected worker compiles with its admitted renderer identity.
    const serialized = JSON.stringify({score,selection});
    if (new TextEncoder().encode(serialized).length > limits.recordBytes) throw Error('Score selection source exceeds the 1 MB browser limit.');
    const composition = JSON.parse(serialized);
    const result = await rawRender('',{...config,composition});
    if (result.verdict !== 'valid' || result.type !== 'composition') throw Error(result.stderr || 'Score stream selection failed.');
    return result;
  }
  const engine = selectEngine(await registryPromise, config.release);
  const authored = compileScore(score, {engine:engine.identity});
  return {...authored, rendererRelease:engine.release_id || engine.id,
    engineId:engine.id, engineIdentity:engine.identity};
}
function cancelAnalysis() {
  analysisSequence++;
  analysisJob?.(Error('Analysis cancelled.')); analysisJob = null;
}
async function analyze(result = lastResult, value = {}) {
  if (result?.verdict !== 'valid' || !(result.pcm instanceof Int16Array) || !result.pcm.length)
    throw Error('Prepare valid audio before analysis.');
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).some(key => !['windowSeconds','sections','dryVoices','dryMaxBytes','dryTimeoutMs','timeoutMs'].includes(key)))
    throw Error('Unknown audio analysis option.');
  const timeout = value.timeoutMs ?? 120000;
  if (!Number.isSafeInteger(timeout) || timeout < 100 || timeout > 300000) throw Error('Analysis timeout must be 100–300000 ms.');
  cancelAnalysis();
  const currentGeneration = generation, sequence = analysisSequence;
  const engine = selectEngine(await registryPromise, result.rendererRelease || result.engineId);
  if (currentGeneration !== generation || sequence !== analysisSequence) throw Error('Analysis cancelled.');
  return new Promise((resolve, reject) => {
    let worker, timer, dryTimer, dryPartial, finished = false;
    const finish = (error, report) => {
      if (finished) return; finished = true;
      clearTimeout(timer); clearTimeout(dryTimer); worker?.terminate();
      if (analysisJob === finish) analysisJob = null;
      if (error) return reject(error);
      const bound = {...report, recordSha256:result.recordSha256, pcmHash:result.pcmHash,
        rendererRelease:result.rendererRelease || result.engineId};
      if (lastResult === result && currentGeneration === generation) presentAnalysis(bound);
      resolve(bound);
    };
    analysisJob = finish;
    try {
      worker = new Worker(new URL('analysis-worker.mjs', assetBase), {type:'module'});
      const optionalFailure = reason => dryPartial ? finish(null, {...dryPartial,dry:{status:'unavailable',scope:'voice_out_pre_fx',reason}}) : finish(Error(reason));
      worker.onerror = () => optionalFailure('Audio analysis worker failed; the prepared audio remains usable.');
      worker.onmessage = ({data}) => {
        if (currentGeneration !== generation) return finish(Error('Analysis cancelled.'));
        if (data.phase === 'dry') {
          const budget = value.dryTimeoutMs ?? 10000;
          if (dryTimer || !value.dryVoices || data.timeoutMs !== budget || !Number.isSafeInteger(budget)
              || budget < 1 || budget > 300000 || !data.partial?.pcm || !data.partial?.events)
            return finish(Error('Invalid dry analysis phase.'));
          dryPartial = data.partial;
          dryTimer = setTimeout(() => finish(null, {...data.partial,dry:{status:'unavailable',
            scope:'voice_out_pre_fx', reason:'Dry analysis timed out; final PCM measurements are retained.'}}), budget);
          return;
        }
        if (data.error) return optionalFailure(data.error);
        if (!data.result?.pcm || !data.result?.events) return finish(Error('Invalid audio analysis result.'));
        finish(null, data.result);
      };
      timer = setTimeout(() => optionalFailure('Audio analysis timed out; the prepared audio remains usable.'), timeout);
      // Structured clone preserves the player's original PCM and canonical bytes.
      worker.postMessage({result:{verdict:result.verdict, pcm:result.pcm, canonical:result.canonical,
        sampleRate:result.sampleRate, channels:result.channels, info:result.info, events:result.events,
        eventsError:result.eventsError, pcmHash:result.pcmHash}, options:value, engine});
    } catch (error) {finish(error);}
  });
}
function presentAnalysis(report) {
    analysisResult = report; const p = report.pcm, level = value => value === null || value === undefined ? 'unavailable' : value.toFixed(2);
    const lines = [`Sample peak: ${level(p.sample_peak_dbfs)} dBFS`,
      `Estimated true peak: ${level(p.loudness?.true_peak_dbtp)} dBTP (4× FFT estimate)`,
      `RMS: ${level(p.rms_dbfs)} dBFS`, `Integrated: ${level(p.loudness?.integrated_lufs)} LUFS`,
      `Short-term maximum: ${level(p.loudness?.short_term_max_lufs)} LUFS`,
      `Trailing digital silence: ${level(p.trailing_digital_silence_seconds)} seconds`];
    if (p.stereo) lines.push(`Stereo correlation: ${level(p.stereo.correlation)}`,
      `Mid / side RMS: ${level(p.stereo.mid_rms_dbfs)} / ${level(p.stereo.side_rms_dbfs)} dBFS`);
    for (const section of p.sections || []) lines.push(`${section.name} (${section.start_seconds}–${section.end_seconds} s): ${level(section.loudness?.integrated_lufs)} LUFS, ${level(section.rms_dbfs)} dBFS RMS, ${level(section.true_peak_dbtp)} dBTP estimated true peak`);
    if (report.dry) lines.push(report.dry.status === 'available'
      ? `Dry sources: ${report.dry.voices?.length || 0} voices measured before effects; see JSON for individual levels.`
      : `Dry sources unavailable: ${report.dry.reason}`);
    lines.push(`${p.windows?.length || 0} measured windows. JSON includes channels and gate advisories${p.sections?.length ? ', plus the named section measurements' : ''}.`);
    $('analysis-output').textContent = lines.join('\n');
    $('analysis-status').textContent = 'Analysis complete. Measurements do not change verification or judge musical quality.';
    $('analysis-download').disabled = false;
}
$('analyze-audio').onclick = async () => {
  const current = lastResult;
  $('analysis-status').textContent = 'Measuring cached audio…'; $('analysis-cancel').hidden = false; $('analyze-audio').disabled = true;
  try {
    const value = {windowSeconds:Number($('analysis-window').value || 4)};
    if ($('analysis-dry').checked) value.dryVoices = true;
    if ($('analysis-section').checked) value.sections = [{name:$('analysis-section-name').value || 'Section',
      startSeconds:Number($('analysis-section-start').value || 0),
      endSeconds:$('analysis-section-end').value.trim() ? Number($('analysis-section-end').value) : current.pcm.length / current.channels / current.sampleRate}];
    const report = await analyze(current, value);
    if (lastResult !== current) return;
    presentAnalysis(report);
  } catch (error) {if (lastResult === current) $('analysis-status').textContent = error.message;}
  finally {if (lastResult === current) { $('analysis-cancel').hidden = true; $('analyze-audio').disabled = false; }}
};
$('analysis-cancel').onclick = cancelAnalysis;
$('analysis-download').onclick = () => {if (analysisResult) download(new Blob([JSON.stringify(analysisResult,null,2)+'\n'],{type:'application/json'}),'audio-analysis.json');};

async function render(recordText, value = {}, sourceContext = null) {
  const requested = options(value);
  const sameText = recordText === $('record').value;
  const expected = sourceContext ? sourceContext.expected : sameText ? expectedRecordSha : null;
  const contextualRelease = sourceContext ? sourceContext.release : sameText ? loadedRelease : undefined;
  const query = new URL(location.href).searchParams;
  const config = options({...requested, release: requested.release ?? contextualRelease ?? (query.has('release') ? query.get('release') : undefined)});
  // A projection attempt replaces the editor with its source. Until success,
  // no previous projected label may describe that source after a refusal/cancel.
  if (sourceContext) appliedProfile = null;
  invalidate(!!sourceContext || sameText); expectedRecordSha = expected; loadedRelease = config.release;
  $('record').value = recordText;
  try { checkRecord(recordText); }
  catch (error) { playerStatus(error.message); throw error; }
  playerStatus(config.profile ? 'Computing and verifying the listening profile…'
    : config.mode === 'listen' ? 'Rendering once for listening…' : 'Rendering twice to verify…');
  const pending = rawRender(recordText, config), requestGeneration = generation;
  busy(true);
  try {
    const result = await pending;
    if (requestGeneration !== generation) throw Error('Rendering cancelled.');
    result.mode = config.profile ? (result.mode || 'verify') : config.mode;
    if (expected && result.verdict === 'valid' && (config.profile ? result.sourceRecordSha256 : result.recordSha256) !== expected) {
      result.verdict = 'mismatch: record identity differs from link';
      result.stderr = 'This song does not match the saved version the link names, so it was not played. The link may be out of date.';
      result.canonical = new Uint8Array();
    }
    Object.assign(result, verification(result));
    if (result.verdict === 'valid' && result.canonical?.length) {
      profileBase ||= sourceContext || {text: recordText, release: config.release, expected};
      if (config.profile) {
        appliedProfile = config.profile;
        $('record').value = new TextDecoder().decode(result.canonical);
        expectedRecordSha = result.recordSha256;
      } else if (sourceContext) appliedProfile = null;
    }
    show(result);
    window.dispatchEvent?.(new CustomEvent('intfm-result', {detail: result}));
    return result;
  } catch (error) {
    if (requestGeneration === generation) playerStatus(error.message);
    throw error;
  } finally {
    if (requestGeneration === generation) busy(false);
  }
}
const listen = (text, value = {}) => render(text, {...value, mode: 'listen'});
const verify = (text, value = {}) => render(text, {...value, mode: 'verify'});
async function verifySaved() {
  const result = lastResult;
  if (result?.verdict !== 'valid' || !result.canonical?.length)
    throw Error('Render a valid draft before verifying its saved canonical output.');
  // This explicit action replays accepted output; a failed hash assertion never
  // supplies canonical bytes and cannot be silently replaced by a new hash.
  const budgetReason = verificationBudgetReason(result);
  if (budgetReason) throw Error(budgetReason);
  const text = new TextDecoder('utf-8', {fatal:true}).decode(result.canonical);
  return verify(text, {release:result.rendererRelease || result.engineId});
}
$('verify-saved').onclick = () => verifySaved().catch(error => { playerStatus(error.message); });
$('draft-download').onclick = () => download(new Blob([$('record').value], {type:'text/plain;charset=utf-8'}), 'draft-unverified.tl');
function profileDescription(profile) {
  return profile ? `Keys ${profile.mirror ? 'mirror ' : ''}±${profile.halfWidth}° (${profile.halfWidth * 2}° total spread)` : 'Loaded version';
}
function updateProfileControls(resetSelection = false) {
  const available = !!profileBase && !rendering;
  if (resetSelection) {
    $('listening-profile').value = appliedProfile ? String(appliedProfile.halfWidth) : 'original';
    $('profile-mirror').checked = !!appliedProfile?.mirror;
    $('profile-quiet').checked = appliedProfile?.levelPolicy === 'carrier-bound';
  }
  $('listening-profile').disabled = !available;
  $('profile-mirror').disabled = !available || $('listening-profile').value === 'original';
  $('profile-quiet').disabled = !available || $('listening-profile').value === 'original';
  const budgetReason = verificationBudgetReason();
  $('profile-apply').disabled = !available || !!budgetReason;
  $('profile-apply').setAttribute('title',budgetReason || 'Prepare the selected listening profile.');
  const selected = selectedProfile();
  const pending = (selected?.halfWidth ?? null) !== (appliedProfile?.halfWidth ?? null)
    || !!selected?.mirror !== !!appliedProfile?.mirror
    || (selected?.levelPolicy || 'strict') !== (appliedProfile?.levelPolicy || 'strict');
  $('profile-detail').textContent = `${profileDescription(appliedProfile)}${lastResult?.verdict === 'valid' ? ' prepared' : ''}. `
    + (pending ? `${profileDescription(selected)} selected; Prepare profile resets playback and prepares the change. Press Play afterward.`
      : appliedProfile ? 'Computed on this device from the loaded source. Save song (.tl) keeps this version.'
        : 'Keeps the saved version named above, including any saved Keys placement.');
  if (budgetReason) $('profile-detail').textContent += ' Browser verification and computed profiles are unavailable for this record; its prepared listening audio is unchanged.';
  if (lastResult?.profile?.targetMet === false)
    $('profile-detail').textContent += ' Below the normalization target at the carrier gain limit; relative levels are preserved.';
}
function selectedProfile() {
  const halfWidth = Number($('listening-profile').value);
  return [30,45,60,75,90].includes(halfWidth) ? {halfWidth, mirror: $('profile-mirror').checked,
    ...($('profile-quiet').checked ? {levelPolicy:'carrier-bound'} : {})} : null;
}
async function applyProfile() {
  if (!profileBase || rendering) return;
  const base = profileBase, profile = selectedProfile();
  const pending = render(base.text, {mode:'verify', release:base.release, ...(profile ? {profile} : {})}, base);
  const requestGeneration = generation;
  try {
    const result = await pending;
    if (requestGeneration !== generation) return;
    if (result.verdict === 'valid') {
      updateProfileControls(true);
      playerStatus(`${profileDescription(profile)} ready. Press Play to listen.`);
    } else {
      // Keep the requested choice available for an explicit recovery policy,
      // while the applied label correctly remains the source's loaded-version state.
      $('listening-profile').value = profile ? String(profile.halfWidth) : 'original';
      $('profile-mirror').checked = !!profile?.mirror;
      $('profile-quiet').checked = profile?.levelPolicy === 'carrier-bound';
      updateProfileControls();
    }
  } catch (error) { if (requestGeneration === generation) playerStatus(error.message); }
}
$('listening-profile').onchange = () => updateProfileControls();
$('profile-mirror').onchange = () => updateProfileControls();
$('profile-quiet').onchange = () => updateProfileControls();
$('profile-apply').onclick = applyProfile;
async function preparePlayback(text) {
  // UI loading prepares PCM without opening an audio device or starting playback.
  const pending = listen(text), requestGeneration = generation;
  try {
    const result = await pending;
    if (requestGeneration === generation && canAudition(result)) playerStatus('Ready. Press Play to listen.');
    return result;
  } catch (error) {
    if (requestGeneration === generation) playerStatus(error.message);
    throw error;
  }
}
window.IntFM = Object.freeze({describe, compose, recipes:listRecipes, recipe, recipeGain, analyze, cancelAnalysis, render: (text, value) => render(text, value), listen, verify, verifySaved, cancel, clear, loadLink, prepareLink: loadLinkForPlayback, showQr: openQr, closeQr, result: () => lastResult,
  metrics: async (text, value) => (await render(text, value)).metrics, toWav, toSvg, guide, reference});
window.addEventListener('message', async event => {
  if (event.origin !== location.origin || !event.source || event.data?.protocol !== 'intfm-v1') return;
  const {id, operation, args = []} = event.data;
  if (!Object.hasOwn(window.IntFM, operation) || !Array.isArray(args)) return;
  try { const value = await window.IntFM[operation](...args); event.source.postMessage({protocol: 'intfm-v1', id, value}, event.origin); }
  catch (error) { event.source.postMessage({protocol: 'intfm-v1', id, error: error.message}, event.origin); }
});
const modelContext = document.modelContext || navigator.modelContext;
if (modelContext?.registerTool) {
  try {
    Promise.resolve(modelContext.registerTool({name: 'intfm_render', description: 'Render a JotTune Timeline locally; full verification is the default.',
      inputSchema: {type: 'object', properties: {recordText: {type: 'string'}, mode: {type: 'string', enum: ['listen', 'verify']}, release: {type: 'string'}}, required: ['recordText']},
      execute: async ({recordText, mode, release}) => {
        const result = await render(recordText, {mode, release});
        return {content: [{type: 'text', text: JSON.stringify({verdict: result.verdict, verified: result.verified,
          mode: result.mode, verification: result.verification, recordSha256: result.recordSha256,
          pcmHash: result.pcmHash, rendererRelease: result.rendererRelease, metrics: result.metrics, stderr: result.stderr})}]};
      }})).catch(() => {});
  } catch { /* Optional browser integration; the window API remains available. */ }
}

function downloadName(extension) {
  let title = (loadedIdentity?.title || 'song').normalize('NFKC').replace(/[\x00-\x1f\x7f<>:"/\\|?*]/g, '-')
    .replace(/\s+/g, ' ').replace(/^[. ]+|[. ]+$/g, '').slice(0, 80) || 'song';
  while (new TextEncoder().encode(title).length > 160) title = [...title].slice(0,-1).join('');
  const safeTitle = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(title) ? 'song-' + title : title;
  const version = appliedProfile ? `keys-${appliedProfile.halfWidth}${appliedProfile.mirror ? '-mirror' : ''}`
    : loadedSong?.variant.kind || 'saved';
  return `${safeTitle}-${version}.${extension}`;
}
function download(blob, name) {
  const url = URL.createObjectURL(blob), anchor = document.createElement('a');
  anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function releaseSource() {
  if (source) { const previous = source; source = null; previous.onended = null; previous.stop(); previous.disconnect(); }
}
function stop() { playbackRequest++; releaseSource(); elapsed = 0; updateTransport(); }
function time(value) { return Math.floor(value / 60) + ':' + String(Math.floor(value % 60)).padStart(2, '0'); }
function durationOf(result = lastResult) { return result?.pcm ? result.pcm.length / result.channels / result.sampleRate : 0; }
function position() {
  const duration = durationOf(), now = source ? audio.currentTime - started : elapsed;
  return Math.max(0, source?.loop && duration ? now % duration : Math.min(now, duration));
}
function updateTransport(redraw = true) {
  const duration = durationOf(), current = position();
  $('time').value = time(current) + ' / ' + time(duration);
  $('seek').disabled = !canAudition(lastResult);
  $('seek').max = String(duration); $('seek').value = String(current);
  $('seek').setAttribute('aria-valuetext', `${time(current)} of ${time(duration)}`);
  $('play').textContent = rendering ? 'Rendering…' : source ? 'Pause' : elapsed > 0 && elapsed < duration ? 'Resume' : 'Play';
  $('play-right').textContent = $('play').textContent;
  $('play-right').disabled = $('play').disabled;
  $('song-load-right').disabled = $('song-load').disabled;
  const title = loadedIdentity?.title || (lastResult ? 'Local song' : 'No song loaded');
  const version = appliedProfile ? `${profileDescription(appliedProfile)} · computed from ${loadedIdentity?.version || 'loaded source'}`
    : loadedIdentity?.version || (lastResult ? 'Local version' : 'Load a song, then press Play.');
  $('loaded-song').textContent = title;
  document.title = lastResult?.verdict === 'valid' && loadedIdentity?.title ? `${title} · ${pageTitle}` : pageTitle;
  $('loaded-version').textContent = version;
  const prepared = lastResult?.verdict === 'valid' && !!lastResult.pcm?.length;
  const selected = selectedSong(), variant = selectedVariant();
  const selectionReady = !!variant && !$('song-load').disabled;
  $('mobile-play').textContent = rendering ? 'Rendering…' : pendingSongLoad ? 'Loading…' : $('play').textContent;
  $('mobile-play').disabled = rendering || !!pendingSongLoad || !prepared || !canAudition(lastResult);
  updateMobileSongAction();
  if (rendering) {
    $('mobile-song').textContent = loadedIdentity?.title || 'Preparing song';
    $('mobile-version').textContent = `Preparing · ${version}`;
  } else if (pendingSongLoad) {
    $('mobile-song').textContent = pendingSongLoad.song.title;
    $('mobile-version').textContent = `Selected · ${variantLabels[pendingSongLoad.variant.kind] || pendingSongLoad.variant.kind}`;
  } else if (prepared) {
    $('mobile-song').textContent = title;
    $('mobile-version').textContent = `Loaded · ${version}`;
  } else {
    $('mobile-song').textContent = selectionReady ? selected.title : 'Choose a song';
    $('mobile-version').textContent = selectionReady ? `Selected · ${variantLabels[variant.kind] || variant.kind}` : 'No song selected';
  }
  $('mobile-time').value = $('time').value;
  // The selection belongs to Load; Play always belongs to the prepared audio.
  // Mirror the existing transport rather than introduce another playback state.
  $('rail-selected-song').textContent = selected?.title || 'No song selected';
  $('rail-selected-version').textContent = variant ? variantLabels[variant.kind] || variant.kind : '';
  $('rail-load').disabled = rendering || !!pendingSongLoad || !selectionReady;
  $('rail-load').textContent = rendering ? 'Preparing…' : pendingSongLoad ? 'Loading…' : 'Load song';
  // Each accessible name starts with the visible text; the song name is added only while the button acts on it.
  const busy = rendering || !!pendingSongLoad;
  $('rail-load').setAttribute('aria-label', selected && !busy ? `Load song: ${selected.title}` : $('rail-load').textContent);
  $('rail-play').disabled = $('mobile-play').disabled;
  $('rail-play').textContent = $('mobile-play').textContent;
  $('rail-play').setAttribute('aria-label', prepared && !busy ? `${$('play').textContent} ${title}` : $('rail-play').textContent);
  $('rail-loaded-song').textContent = rendering ? `Preparing: ${loadedIdentity?.title || pendingSongLoad?.song.title || 'song'}`
    : pendingSongLoad ? `Loading: ${pendingSongLoad.song.title}`
    : prepared ? `${source ? 'Playing' : 'Loaded'}: ${title} · ${version}` : 'No song ready to play';
  $('rail-time').value = $('time').value;
  playerStatus($('status').textContent);
  updateMediaSession(title, prepared && canAudition(lastResult));
  if (redraw && liveVisible()) instrumentView.update(current, !!source);
  synchronizeLiveAnimation();
}
function pause() {
  playbackRequest++; elapsed = position(); releaseSource(); updateTransport();
  playerStatus('Paused.');
}
setInterval(() => updateTransport(false), 200);
// Where a browser offers Media Session for this page, it shows the loaded song and its
// play, pause and stop actions use the same transport. Playback stays on Web Audio.
function updateMediaSession(title, playable) {
  if (!mediaSession) return;
  try {
    const name = playable ? title : null;
    if (name !== mediaTitle && typeof MediaMetadata === 'function') {
      mediaSession.metadata = name ? new MediaMetadata({title: name, album: pageTitle}) : null;
      mediaTitle = name;
    }
    mediaSession.playbackState = source ? 'playing' : playable ? 'paused' : 'none';
  } catch { /* Optional platform integration must not affect playback. */ }
}

function prepareAudio() {
  // Called synchronously inside the click handler, before any render/fetch await.
  const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Context) throw Error('Web Audio is unavailable in this browser.');
  try {
    const session = navigator.audioSession;
    if (session) session.type = 'playback';
  } catch { /* Optional platform audio routing must not prevent playback. */ }
  audio ||= new Context();
  return audio.resume();
}
function canAudition(result) {
  return result?.verdict === 'valid' && result.pcm && [1, 2].includes(result.channels)
    && !['1', '2'].includes(result.info?.route);
}
function audition(result, offset = 0, followEpoch = null, followStart = false) {
  if (lastResult !== result || !canAudition(result)) return;
  releaseSource();
  if (playbackBufferResult !== result) {
    playbackBuffer = audio.createBuffer(result.channels, result.pcm.length / result.channels, result.sampleRate);
    for (let channel = 0; channel < result.channels; channel++) {
      const output = playbackBuffer.getChannelData(channel);
      for (let index = 0; index < output.length; index++) output[index] = result.pcm[index * result.channels + channel] / 32768;
    }
    playbackBufferResult = result;
  }
  const buffer = playbackBuffer;
  elapsed = offset >= buffer.duration ? 0 : Math.max(0, offset);
  const next = audio.createBufferSource();
  next.buffer = buffer; next.loop = $('loop').checked; next.connect(audio.destination);
  source = next; started = audio.currentTime - elapsed;
  next.onended = () => { if (source === next) {
    source = null; elapsed = buffer.duration; updateTransport(); playerStatus('Finished.');
    if (!next.loop && followEpoch !== null) followSection('library-panel');
  } };
  next.followEpoch = followEpoch;
  next.start(0, elapsed); updateTransport(); playerStatus('Playing.');
  if (followStart && followEpoch !== null) followSection('live-views', followEpoch);
}
$('play').onclick = async () => {
  const result = lastResult;
  if (!canAudition(result)) return;
  if (source) { pause(); return; }
  const request = ++playbackRequest, offset = elapsed, followEpoch = autoMoveEpoch;
  try {
    const resumed = prepareAudio();
    playerStatus(audio.state === 'suspended' ? 'Waiting for audio. Press Play again if it stays silent.' : 'Starting audio…');
    await resumed;
    if (lastResult !== result || request !== playbackRequest) return;
    if (audio.state && audio.state !== 'running') throw Error('Audio is suspended. Press Play to retry.');
    audition(result, offset, followEpoch, true);
  } catch (error) { if (lastResult === result && request === playbackRequest) playerStatus(error.message); }
};
$('play-right').onclick = () => { if (!$('play').disabled) return $('play').click(); };
$('listen').onclick = async () => {
  let requestGeneration = generation, request = playbackRequest; const followEpoch = autoMoveEpoch;
  try {
    const resumed = Promise.resolve(prepareAudio());
    const audioReady = resumed.then(() => null, error => error);
    const pending = listen($('record').value); requestGeneration = generation; request = playbackRequest;
    const result = await pending;
    if (requestGeneration === generation && request === playbackRequest && canAudition(result) && audio.state !== 'running')
      playerStatus('Waiting for audio. Press Play again if it stays silent.');
    const error = await audioReady;
    if (error) throw error;
    if (audio.state && audio.state !== 'running') throw Error('Audio is suspended. Press Play to retry.');
    if (requestGeneration === generation && request === playbackRequest) audition(result, 0, followEpoch, true);
  } catch (error) { if (requestGeneration === generation && request === playbackRequest) playerStatus(error.message); }
};
$('stop').onclick = () => { stop(); if (canAudition(lastResult)) playerStatus('Stopped.'); };
if (mediaSession) for (const [action, handler] of [
  ['play', () => { if (!source) $('play').click(); }],
  ['pause', () => { if (source) $('play').click(); }],
  ['stop', () => $('stop').click()],
]) {
  try { mediaSession.setActionHandler(action, handler); } catch { /* The browser does not offer this action. */ }
}
function seekTo(target) {
  if (!canAudition(lastResult)) return;
  const playing = !!source;
  const followEpoch = source?.followEpoch ?? null;
  if (!Number.isFinite(target)) return;
  playbackRequest++; releaseSource(); elapsed = Math.max(0, Math.min(durationOf(), target));
  if (playing && (elapsed < durationOf() || $('loop').checked)) audition(lastResult, elapsed, followEpoch);
  else { updateTransport(); playerStatus('Position ' + time(elapsed) + '. Press Play to listen.'); }
}
$('seek').oninput = () => seekTo(Number($('seek').value));
$('loop').onchange = () => {
  const current = position();
  if (source) { source.loop = $('loop').checked; started = audio.currentTime - current; }
  updateTransport();
};

function label(result) {
  if (result.verdict !== 'valid') return result.verdict;
  if (result.verified) return 'Verified';
  if (result.play?.block_indep === 'unchecked')
    return result.play.match === 'yes' ? 'Checksum matched · block independence unchecked' : 'Rendered · hash and block independence unchecked';
  return 'Rendered · expected hash not provided';
}
function show(result) {
  releaseSource(); elapsed = 0; lastResult = result;
  result.structure = result.verdict === 'valid' ? songStructure(result)
    : {version:1,status:'unavailable',scope:'native_gate_commands',reason:'A valid loaded or inspected song is required.'};
  $('song-structure').textContent = songStructureText(result.structure);
  $('verdict').textContent = result.verdict === 'valid'
    ? result.verified ? `Verified · ready to ${canAudition(result) ? 'play' : 'export'}` : result.verification === 'checksum-matched' ? 'Ready · checksum matched' : 'Ready · unverified'
    : label(result);
  $('verdict').setAttribute('title', label(result));
  $('verdict').className = 'badge ' + (result.verdict === 'valid' ? result.verified ? 'valid' : 'unverified' : result.verdict.split(':')[0]);
  $('raw').textContent = [result.stdout, result.stderr, result.eventsError].filter(Boolean).join('\n');
  const rendered = !!result.pcm && result.verdict === 'valid';
  for (const id of ['play', 'stop']) $(id).disabled = !canAudition(result);
  for (const id of ['wav-download', 'spectrum']) $(id).disabled = !rendered;
  for (const id of ['tl-download', 'svg-download', 'share', 'show-qr']) $(id).disabled = !(result.verdict === 'valid' && result.recordSha256);
  $('png-download').disabled = true; $('spectrogram').hidden = true;
  $('analyze-audio').disabled = result.verdict !== 'valid' || !result.pcm?.length;
  $('verify-saved').disabled = !(result.verdict === 'valid' && result.canonical?.length && !result.verified);
  playerStatus(result.verdict === 'mismatch'
    ? `Saved PCM hash does not match: expected ${result.play?.expected || '—'}, rendered ${result.pcmHash || result.play?.hash || '—'}. Playback, Save song and Save audio are blocked. Reopen a known-good saved TL or investigate the source and renderer before removing any hash.`
    : result.verdict !== 'valid'
    ? (result.stderr?.split('\n').find(line => line.trim()) || label(result))
    : result.info && ['1', '2'].includes(result.info.route)
      ? 'Raw field coefficients: export and inspect; speaker playback is unavailable.' : 'Ready. Press Play to listen.');
  $('facts').replaceChildren();
  if (result.info) {
    const values = {...(result.verdict !== 'valid' ? {'Render result': 'Refused. Measurements below are diagnostic; no canonical export was accepted.'} : {}),
      ...(Number.isInteger(result.tlVersion) ? {'TL version':String(result.tlVersion) + (result.documentHeader === 'legacy-headerless' ? ' (legacy, inferred)' : ''),
        'Engine compatibility version':String(result.engineCompatibilityVersion),
        'Document header':result.documentHeader === 'present' ? 'Portable metadata' : 'Legacy headerless'} : {}),
      ...(result.metadata?.title?{'Song title':result.metadata.title}:{}),
      ...(result.metadata?.collection?{'Collection (declared)':result.metadata.collection}:{}),
      ...(result.metadata?.author?{'Author (declared)':result.metadata.author}:{}),
      ...(result.metadata?.made_by?{'Author kind (declared)':result.metadata.made_by}:{}),
      ...(result.metadata?.model?{'Model (declared)':result.metadata.model.id+' · '+result.metadata.model.source}:{}),
      ...(result.metadata?.genre?{'Genre (declared)':result.metadata.genre}:{}),
      ...(result.metadata?.tempo?{'Tempo (declared)':result.metadata.tempo.min_bpm+'–'+result.metadata.tempo.max_bpm+' BPM'}:{}),
      ...(result.metadata?.license?{'License (declared)':result.metadata.license}:{}),
      ...(result.metadata?.utc_day?{'UTC day (declared)':result.metadata.utc_day}:{}),
      ...(result.metadata?.prompt_id?{'Prompt ID (declared)':result.metadata.prompt_id}:{}),
      ...(result.metadata?.renderer_release?{'Renderer release (declared)':result.metadata.renderer_release}:{}),
      'Technical verification': label(result), 'Record SHA-256': result.recordSha256 || '—', ...(result.documentSha256 && result.documentSha256 !== result.recordSha256 ? {'Document SHA-256':result.documentSha256}:{}), 'PCM hash': result.pcmHash || '—',
      'Engine': result.engineIdentity || '—', 'Release': result.rendererRelease || 'Local development build',
      'Build': result.engineId || '—', 'Mode': result.mode,
      'Declared duration': (Number(result.info.length_ns) / 1e9).toFixed(6) + ' s',
      'PCM duration': `${result.pcm?.length ? (result.pcm.length / result.channels / result.sampleRate).toFixed(6) : '—'} s / ${result.info.frames ?? '—'} whole frames`,
      'Size': result.verdict === 'valid'
        ? `${result.canonical?.length || 0} bytes TL / ${Number(result.info.pcm_bytes) + 44} bytes PCM WAV`
        : 'No accepted TL or WAV export.',
      'Sample peak': `${result.play?.peak_dbfs ?? '—'} dBFS`,
      'RMS': `${result.play?.rms_dbfs ?? '—'} dBFS (not loudness)`,
      'Native saturation count': result.play?.clipped ?? '—',
      'Crest ratio': `${result.metrics?.crest ?? '—'} (linear peak/RMS, not dB)`,
      'Block independence': result.play?.block_indep || '—'};
    for (const [key, value] of Object.entries(values)) {
      const term = document.createElement('dt'), detail = document.createElement('dd');
      term.textContent = key; detail.textContent = value; $('facts').append(term, detail);
    }
    try { $('timeline').innerHTML = toSvg(result); }
    catch (error) { $('timeline').textContent = error.message; }
  } else $('timeline').textContent = 'No rendered record. See the diagnostic below.';
  let spatialSampler = null, spatialWarning = result.spatialError;
  if (result.verdict === 'valid' && result.spatial && spatialModules.has(result)) {
    try { spatialSampler = createSpatialSampler(result.spatial, spatialModules.get(result)); }
    catch (error) { spatialWarning = error.message; }
  }
  instrumentView.setResult(result, {spatialSampler, spatialWarning});
  reconcileLinkedIdentity();
  updateProfileControls(true);
  updateTransport();
  if (displayView === 'spectrum') void showSpectrum();
}
$('render').onclick = () => verify($('record').value).catch(() => {});
$('cancel').onclick = cancel;

let songLibrary = [], librarySelection = [], selectedSongId = '', songChoices = new Map(), libraryView = 'featured', catalogIndex, catalogResult;
let libraryRequest, librarySequence = 0, libraryHistory = [], libraryCursor = null, loadedSong = null, linkedSelectionActive = false;
let previewWorker, previewAbort, previewSequence = 0, previewTimer;
const catalog = createCatalog(assetBase, validDiscovery);
const variantLabels = {standard: 'Original · saved', keys: 'Keys · saved ±30°', mirror: 'Keys mirror · saved ±30°'};
const songLabel = (song, variant) => `${song.title} · ${time(variant.duration_seconds)}`;
function libraryOption(value, label) {
  const option = document.createElement('option'); option.value = value; option.textContent = label; return option;
}
function selectedSong() { return librarySelection.find(entry => entry.song.id === selectedSongId)?.song; }
function selectedVariant() { return selectedSong()?.variants.find(variant => variant.kind === $('song-variant').value); }
function songChoice(song, label) {
  const row = document.createElement('label'), radio = document.createElement('input'), text = document.createElement('span');
  row.className = 'song-choice';text.className = 'song-choice-label';text.textContent = label;
  radio.type = 'radio';radio.name = 'song-choice';radio.value = song.id;radio.checked = song.id === selectedSongId;
  songChoices.set(song.id,radio);
  radio.addEventListener('change',() => {
    if (!radio.checked || $('song-select').disabled || songChoices.get(song.id) !== radio) return;
    linkedSelectionActive = false; selectedSongId = song.id;
    for (const [id,choice] of songChoices) choice.checked = id === selectedSongId;
    libraryVariants();
  });
  row.append(radio,text);return row;
}
function emptySongChoices(message) {
  songChoices.clear();selectedSongId = '';
  const text = document.createElement('p');text.className = 'song-empty';text.textContent = message;
  $('song-select').replaceChildren(text);$('song-select').disabled = true;$('song-select').setAttribute('aria-busy','false');
}
const metadataOf = variant => variant.discovery?.metadata || {};
const modelOf = variant => variant.discovery?.provenance?.model || '';
function validDiscovery(variant) {
  const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const keys = (value, allowed) => object(value) && Object.keys(value).every(key => allowed.includes(key));
  const text = value => typeof value === 'string' && !!value.trim() && !/[\x00-\x1f]/.test(value);
  const token = value => typeof value === 'string' && /^[A-Za-z0-9._:+/@-]{1,160}$/.test(value);
  if (variant.record_sha256 !== undefined && !hash(variant.record_sha256)) return false;
  if (variant.document_sha256 !== undefined && !hash(variant.document_sha256)) return false;
  if (variant.technical !== undefined) {
    const info = variant.technical;
    if (!keys(info, ['source','channels','route','allocated_voices','events']) || info.source !== 'native-inspect'
        || !Number.isSafeInteger(info.channels) || info.channels < 1 || info.channels > 16
        || !Number.isSafeInteger(info.route) || info.route < 0 || info.route > 4
        || !Number.isSafeInteger(info.allocated_voices) || info.allocated_voices < 0
        || !Number.isSafeInteger(info.events) || info.events < 0) return false;
  }
  const item = variant.discovery;
  if (item === undefined) return true;
  if (!keys(item, ['record_sha256','document_sha256','source','metadata','provenance','featured','showcase'])
      || !hash(item.record_sha256) || item.record_sha256 !== variant.record_sha256
      || (item.document_sha256 !== undefined && (!hash(item.document_sha256)
        || variant.document_sha256 !== undefined && item.document_sha256 !== variant.document_sha256))
      || !text(item.source) || item.source.startsWith('/') || item.source.includes('\\')
      || item.source.split('/').some(part => !part || part === '.' || part === '..')) return false;
  if (item.metadata !== undefined && !keys(item.metadata, ['genre','tempo'])) return false;
  const meta = item.metadata || {};
  if (meta.genre !== undefined && (!keys(meta.genre, ['value','source']) || !token(meta.genre.value)
      || !['retained-score','creator-declared'].includes(meta.genre.source))) return false;
  if (meta.tempo !== undefined && (!keys(meta.tempo, ['min_bpm','max_bpm','source'])
      || !['retained-score','creator-declared'].includes(meta.tempo.source)
      || !Number.isFinite(meta.tempo.min_bpm) || !Number.isFinite(meta.tempo.max_bpm)
      || meta.tempo.min_bpm <= 0 || meta.tempo.min_bpm > meta.tempo.max_bpm
      || meta.tempo.max_bpm > Number.MAX_SAFE_INTEGER)) return false;
  for (const key of ['featured', 'showcase']) if (item[key] !== undefined && (!keys(item[key], ['order','note'])
    || !Number.isSafeInteger(item[key].order) || item[key].order < 1 || !text(item[key].note))) return false;
  // Contributed songs declare made_by, with the model pair when one was declared (tunes/tools/validate.py).
  const provenance = item.provenance;
  return provenance === undefined || (keys(provenance, ['made_by','model','model_source']) && Object.keys(provenance).length > 0
    && (provenance.made_by === undefined || ['agent','human','mixed'].includes(provenance.made_by))
    && ('model' in provenance) === ('model_source' in provenance)
    && (provenance.model === undefined || (token(provenance.model)
      && ['client-reported', 'user-stated', 'unknown'].includes(provenance.model_source))));
}

function matchingVariants(song) {
  const query = $('song-search').value.trim().normalize('NFKC').toLocaleLowerCase();
  if ($('song-collection').value && song.collection !== $('song-collection').value) return [];
  if (![song.title, song.collection, song.revision || ''].join(' ').normalize('NFKC').toLocaleLowerCase().includes(query)) return [];
  const genre = $('song-genre').value, tempo = $('song-tempo').value, model = $('song-model').value;
  return song.variants.filter(variant => {
    const meta = metadataOf(variant), tempoKind = !meta.tempo ? 'unknown'
      : meta.tempo.min_bpm === meta.tempo.max_bpm ? 'constant' : 'range';
    return (!genre || (genre === 'unknown' ? !meta.genre : meta.genre?.value === genre.slice(6)))
      && (!tempo || tempo === tempoKind)
      && (!model || (model === 'unknown' ? !modelOf(variant) : modelOf(variant) === model.slice(6)));
  });
}
// Catalog genres are slugs; the page shows them as words and keeps the slug as the value.
const genreLabel = value => (value => value.charAt(0).toLocaleUpperCase() + value.slice(1))(value.replace(/[-_]+/g, ' ').replace(/\s*\/\s*/g, ' / '));
const plural = (count, one, many = one + 's') => `${count} ${count === 1 ? one : many}`;
// With genres (a Set, or null for every genre) only the genre choices are refilled.
function libraryFilters(genres) {
  const categories = catalogIndex.categories, current = $('song-genre').value.replace(/^value:/, '');
  const fill = (id, label, prefix, trim = '') => {
    $(id).replaceChildren(libraryOption('', label), ...categories.filter(item => item.id.startsWith(prefix)).flatMap(item => {
      const value = item.title.replace(trim, '');
      if (id === 'song-genre' && genres && !genres.has(value) && value !== current) return [];
      return [libraryOption(id === 'song-collection' ? value : 'value:' + value, id === 'song-genre' ? genreLabel(value) : value)];
    }), ...(id === 'song-collection' ? [] : [libraryOption('unknown', 'Unknown')]));
    $(id).value = ''; $(id).disabled = false;
  };
  if (genres !== undefined) { const genre = $('song-genre').value; fill('song-genre', 'All genres', 'genre-', 'Genre: '); $('song-genre').value = genre; return; }
  fill('song-collection', 'All collections', 'collection-');
  fill('song-genre', 'All genres', 'genre-', 'Genre: ');
  fill('song-model', 'All models', 'model-', 'Model: ');
  // Unknown is useful even when the entire current catalog lacks a declaration.
  for (const field of ['genre','tempo','model']) $('song-' + field + '-field').hidden = false;
  $('song-tempo').value = ''; $('song-tempo').disabled = false;
}
function cancelPreview(message = 'Inspect the selected song’s notes without rendering audio.') {
  previewSequence++; previewAbort?.abort(); previewAbort = null;
  previewWorker?.terminate(); previewWorker = null; clearTimeout(previewTimer);
  $('song-preview-cancel').hidden = true; $('song-preview').disabled = !selectedVariant();
  $('song-preview-roll').hidden = true; $('song-preview-roll').replaceChildren();
  $('song-preview').textContent = 'Inspect notes'; $('song-preview').setAttribute('aria-expanded','false');
  $('song-preview-status').textContent = message;
}
function libraryDetails() {
  cancelPreview();
  const song = selectedSong(), variant = selectedVariant();
  if (pendingSongLoad && (pendingSongLoad.song !== song || pendingSongLoad.variant !== variant)) {
    pendingSongLoad.controller.abort(); pendingSongLoad = null; generation++;
    playerStatus('Selection changed. Load the selected song when ready.');
  }
  $('song-load').disabled = !variant; $('song-preview').disabled = !variant;
  updateTransport(false);
  updateJogControls();
  $('selected-song-title').textContent = song?.title || (libraryView === 'featured' ? 'No matching favorites on these pages' : 'No matching song on these pages');
  if (!variant) {
    if (linkedSelectionActive) {
      $('selected-song-title').textContent = 'Song from link';
      $('song-detail').textContent = 'The linked song is in Player. Press Play, or choose Favorites or Browse all to select a replacement.';
      $('song-discovery').textContent = ''; $('song-note').textContent = ''; return;
    }
    $('song-detail').textContent = (libraryView === 'featured' ? 'Browse all searches beyond Favorites with the same filters. ' : '')
      + (catalogResult?.next ? 'Next results continues the search. You can also clear filters or open a .tl file.' : 'Clear filters or open your own .tl file below.');
    $('song-discovery').textContent = ''; $('song-note').textContent = ''; return;
  }
  const larger = variant.bytes > limits.recordBytes || variant.duration_seconds > limits.durationSeconds;
  $('song-preview').disabled = larger;
  $('song-detail').textContent = `${song.collection} · ${time(variant.duration_seconds)} · ${(variant.bytes / 1000).toFixed(1)} kB. `
    + (larger ? 'Exceeds the browser profile; use the local CLI or app.' : 'Load song prepares it silently. Then press Play.');
  const meta = metadataOf(variant), sourceLabel = value => value === 'retained-score' ? 'retained score' : value === 'creator-declared' ? 'creator declaration' : value;
  const tempo = meta.tempo ? (meta.tempo.min_bpm === meta.tempo.max_bpm ? `${meta.tempo.min_bpm} BPM`
    : `${meta.tempo.min_bpm}–${meta.tempo.max_bpm} BPM range`) + ` (${sourceLabel(meta.tempo.source)})` : 'Unknown';
  const genre = meta.genre ? `${genreLabel(meta.genre.value)} (${sourceLabel(meta.genre.source)})` : 'Unknown';
  const model = modelOf(variant) ? `${modelOf(variant)} (${variant.discovery.provenance.model_source})` : 'Unknown';
  const note = variant.discovery?.[libraryView]?.note || (variant.kind === song.defaultKind ? song.note : '');
  $('song-note').textContent = note || '';
  const technical = variant.technical;
  const route = technical ? ({1:'Raw first-order field',2:'Raw third-order field',3:'Binaural',4:'Pan-law stereo'}[technical.route]
    || (technical.channels === 1 ? 'Mono' : technical.channels === 2 ? 'Stereo' : `${technical.channels} channels`)) : '';
  $('song-discovery').textContent = [note, route, technical ? `${technical.allocated_voices} allocated voices · ${technical.events} authored events.` : '',
    song.revision ? `Source revision: ${song.revision}.` : '', `Genre: ${genre}. Tempo: ${tempo}. Model: ${model}.`].filter(Boolean).join(' ');
}
function libraryVariants(preferred) {
  const song = selectedSong(), select = $('song-variant');
  const variants = librarySelection.find(entry => entry.song === song)?.variants || [];
  preferred = variants.some(variant => variant.kind === preferred) ? preferred : preferredLibraryVariant(variants)?.kind;
  select.replaceChildren(...variants.map(variant => libraryOption(variant.kind, variantLabels[variant.kind] || variant.kind)));
  select.disabled = !song; select.value = preferred || ''; libraryDetails();
}
function preferredLibraryVariant(variants) {
  return variants.find(variant => variant.kind === 'keys')
    || variants.find(variant => variant.kind === 'standard') || variants[0];
}
function categoryForFilters() {
  const categories = catalogIndex.categories;
  let category = categories.find(item => item.id === libraryView);
  if (libraryView === 'all') {
    const collection = $('song-collection').value, genre = $('song-genre').value, model = $('song-model').value;
    category = categories.find(item => collection ? item.id.startsWith('collection-') && item.title === collection
      : genre.startsWith('value:') ? item.id.startsWith('genre-') && item.title === 'Genre: ' + genre.slice(6)
      : model.startsWith('value:') ? item.id.startsWith('model-') && item.title === 'Model: ' + model.slice(6) : false) || category;
  }
  return category || categories.find(item => item.id === 'all');
}
async function librarySongs({cursor = null, reset = true, preserveLinked = false} = {}) {
  if (!preserveLinked) linkedSelectionActive = false;
  if (!catalogIndex) return;
  libraryRequest?.abort(); libraryRequest = new AbortController();
  const request = ++librarySequence, signal = libraryRequest.signal;
  cancelPreview();
  if (reset) {libraryHistory = []; libraryCursor = null;}
  const previous = selectedSongId, kind = $('song-variant').value, category = categoryForFilters();
  for (const view of ['featured','all']) $('library-' + view).setAttribute('aria-pressed', String(view === libraryView));
  $('library-view-detail').textContent = libraryView === 'featured' ? 'A curated listening selection. Browse all includes the available source songs.'
    : 'Browse the available source songs and saved versions.';
  const crumbs = ['Source library', {featured:'Favorites',all:'Browse all'}[libraryView]];
  for (const [field,label] of [['collection','Collection'],['genre','Genre'],['tempo','Tempo'],['model','Model']]) {
    const value = $('song-' + field).value.replace(/^value:/,''); if (value) crumbs.push(label + ': ' + (field === 'genre' && value !== 'unknown' ? genreLabel(value) : value));
  }
  if ($('song-search').value.trim()) crumbs.push('Search: ' + $('song-search').value.trim());
  $('library-path').textContent = crumbs.join(' › ');
  $('library-status').textContent = 'Searching catalog pages…';
  $('song-select').disabled = true;$('song-select').setAttribute('aria-busy','true');
  $('song-load').disabled = $('song-preview').disabled = true;
  updateTransport(false);
  $('library-previous').disabled = $('library-next').disabled = true;
  updateJogControls();
  try {
    let result = await catalog.browse(category, matchingVariants, cursor, signal);
    const start = result.start, items = [...result.items];
    // Continue search chunks until a full result page or the end of the source
    // selection. Each chunk reads at most four pages, a step at most twelve chunks,
    // and a changed query aborts it.
    for (let chunks = 1; items.length < 8 && result.next && chunks < 12; chunks++) {
      if (request !== librarySequence || signal.aborted) return;
      $('library-status').textContent = `Searching… ${result.examined} of ${plural(result.total, 'song')} checked.`;
      result = await catalog.browse(category, matchingVariants, result.next, signal, 8 - items.length);
      items.push(...result.items);
    }
    result = {...result,start,items};
    if (request !== librarySequence) return;
    catalogResult = result; libraryCursor = result.start; librarySelection = result.items;
    songLibrary = [...result.items.map(item => item.song)];
    if (loadedSong && !songLibrary.includes(loadedSong.song)) songLibrary.push(loadedSong.song);
    if (linkedSelectionActive) { surfaceLinkedSelection(); reconcileLinkedIdentity(); return; }
    const select = $('song-select');
    selectedSongId = result.items.some(item => item.song.id === previous) ? previous : result.items[0]?.song.id || '';
    songChoices.clear();
    select.replaceChildren(...result.items.map(({song, variants}) => {
      const variant = preferredLibraryVariant(variants);
      const sameTitle = result.items.filter(other => other.song.id !== song.id && other.song.title === song.title);
      const disambiguation = sameTitle.length ? ` · ${song.collection}`
        + (sameTitle.some(other => other.song.collection === song.collection) ? ` [${song.id}]` : '') : '';
      return songChoice(song, songLabel(song, variant) + disambiguation);
    }));
    select.disabled = !result.items.length;
    select.setAttribute('aria-busy','false');
    if (!result.items.length) emptySongChoices(libraryView === 'featured' ? 'No matching favorites on searched pages' : 'No matches on searched pages');
    const filtered = ['search','collection','genre','tempo','model'].some(field => $('song-' + field).value.trim());
    const range = result.items.length ? `${result.matched - result.items.length + 1}–${result.matched}` : '0 on this step';
    $('library-status').textContent = filtered
      ? `Results ${range}. Searched ${result.examined} of ${plural(result.total, 'song')}. ${result.next ? 'More songs may match; choose Next results.' : `${plural(result.matched, 'match', 'matches')} in this source selection.`}`
      : `${result.total ? range : 0} of ${plural(result.total, 'song')}`;
    $('library-previous').disabled = !libraryHistory.length;
    $('library-next').disabled = !result.next;
    libraryVariants(selectedSongId === previous ? kind : undefined);
    reconcileLinkedIdentity();
  } catch (error) {
    if (request !== librarySequence || signal.aborted) return;
    librarySelection = [];emptySongChoices('Catalog page unavailable');
    $('library-status').textContent = 'Song library unavailable: ' + error.message + ' Clear filters to retry, or open a .tl file below.';
    libraryDetails();
  }
}
async function loadLibrary() {
  try {
    catalogIndex = await catalog.open(); libraryFilters();
    if (!catalogIndex.categories.find(item => item.id === 'featured')?.count) libraryView = 'all';
    $('song-search').disabled = false; await librarySongs({preserveLinked:true});
  } catch {
    emptySongChoices('Catalog unavailable');
    $('library-status').textContent = 'Song library unavailable. Open or drop a .tl file below.';
  }
}
$('song-search').addEventListener('input', () => {if ($('song-search').value.trim()) libraryView = 'all'; void librarySongs();});
for (const view of ['featured','all']) $('library-' + view).onclick = () => {libraryView = view; return librarySongs();};
// Genre choices follow the chosen collection when its songs fit one bounded
// read of at most four pages; a larger collection keeps every genre choice.
let genreRequest = 0;
async function collectionGenres() {
  const request = ++genreRequest, collection = $('song-collection').value;
  const category = collection && catalogIndex?.categories.find(item => item.id.startsWith('collection-') && item.title === collection);
  let genres = null;
  if (category && category.count <= 96) try {
    const result = await catalog.browse(category, song => song.variants, null, undefined, category.count);
    if (!result.next) genres = new Set(result.items.flatMap(item => item.variants.map(variant => metadataOf(variant).genre?.value).filter(Boolean)));
  } catch { genres = null; }
  if (request === genreRequest && catalogIndex) libraryFilters(genres);
}
for (const field of ['collection','genre','tempo','model']) $('song-' + field).onchange = () => {
  if ($('song-' + field).value) libraryView = 'all';
  return field === 'collection' ? librarySongs().then(collectionGenres) : librarySongs();
};
$('library-clear').onclick = () => {for (const field of ['search','collection','genre','tempo','model']) $('song-' + field).value = ''; return librarySongs().then(collectionGenres);};
$('library-next').onclick = () => {if (!catalogResult?.next) return; libraryHistory.push(libraryCursor); if (libraryHistory.length > 128) libraryHistory.shift(); return librarySongs({cursor:catalogResult.next,reset:false});};
$('library-previous').onclick = () => {if (!libraryHistory.length) return; return librarySongs({cursor:libraryHistory.pop(),reset:false});};
function updateJogControls() {
  for (const side of ['left','right']) {
    $('song-step-up-' + side).disabled = $('library-previous').disabled;
    $('song-step-down-' + side).disabled = $('library-next').disabled;
  }
}
async function jogPage(direction) {
  // Use the same bounded request, stale-response guard and page history as the
  // labelled pagination controls; browsing never loads the highlighted song.
  const button = $(direction < 0 ? 'library-previous' : 'library-next');
  if (!button.disabled) return button.onclick();
}
let jogWheelCooling = false;
for (const side of ['left','right']) {
  for (const [name,direction] of [['up',-1],['down',1]]) $('song-step-' + name + '-' + side).onclick = () => jogPage(direction);
  $('song-jog-' + side).addEventListener('wheel', event => {
    if (!event.deltaY || event.ctrlKey) return;
    event.preventDefault();
    const direction = event.deltaY > 0 ? 1 : -1;
    if (jogWheelCooling || $('song-step-' + (direction > 0 ? 'down' : 'up') + '-' + side).disabled) return;
    jogWheelCooling = true;
    void jogPage(direction).finally(() => setTimeout(() => {jogWheelCooling = false;},120));
  }, {passive:false});
}
$('song-variant').onchange = () => {linkedSelectionActive = false; libraryDetails();};
function showPlayer() { followSection('player-panel'); }
function updateMobileSongAction() {
  const load = currentSection === 'library-panel' && $('library-panel').open && !!selectedVariant();
  $('choose-song').dataset.action = load ? 'load' : 'choose';
  $('choose-song').textContent = load ? rendering ? 'Rendering…' : pendingSongLoad ? 'Loading…' : 'Load song' : 'Choose song';
  $('choose-song').disabled = load && (rendering || !!pendingSongLoad || $('song-load').disabled);
}
$('choose-song').onclick = () => {
  if ($('choose-song').dataset.action === 'load') {
    if (!$('choose-song').disabled && !$('song-load').disabled) return $('song-load').click();
    return;
  }
  stopAutoMove();sectionNavigation?.jump('library-panel');
};
$('library-panel').addEventListener('toggle',updateMobileSongAction);
mobileSongActionReady = true;updateMobileSongAction();
$('mobile-play').onclick = () => {
  if (rendering || pendingSongLoad) return;
  if (lastResult?.verdict === 'valid' && lastResult.pcm?.length) return $('play').click();
};
$('song-load').onclick = async () => {
  const song = selectedSong(), variant = selectedVariant(); if (!song || !variant) return;
  showPlayer(); invalidate(); const requestGeneration = generation;
  playerStatus('Loading ' + song.title + '…');
  const pending = {song,variant,controller:new AbortController()}; pendingSongLoad = pending; updateTransport(false);
  try {
    const text = await checkCatalogDocument(await fetchRecord(new URL(variant.path, assetBase), undefined,
      {signal:pending.controller.signal}),variant);
    if (requestGeneration !== generation || selectedSong() !== song || selectedVariant() !== variant) return;
    checkRecord(text); $('record').value = text; expectedRecordSha = variant.record_sha256 || null;
    loadedSong = {song,variant}; loadedIdentity = {title:song.title,version:variantLabels[variant.kind] || variant.kind};
    return await preparePlayback(text);
  } catch (error) {if (requestGeneration === generation) playerStatus(error.message);}
  finally {if (pendingSongLoad === pending) {pendingSongLoad = null; updateTransport(false);}}
};
$('song-load-right').onclick = () => { if (!$('song-load').disabled) return $('song-load').click(); };
$('rail-load').onclick = () => {
  if (!$('rail-load').disabled && !rendering && !pendingSongLoad && !$('song-load').disabled)
    return $('song-load').click();
};
$('rail-play').onclick = () => {
  if (!$('rail-play').disabled && !rendering && !pendingSongLoad && canAudition(lastResult))
    return $('play').click();
};
$('song-preview-cancel').onclick = () => cancelPreview('Note inspection cancelled. Playback is unchanged.');
$('song-preview').onclick = async () => {
  if (!$('song-preview-roll').hidden) { cancelPreview('Note preview hidden. Inspect notes to show it again.'); return; }
  const song = selectedSong(), variant = selectedVariant(); if (!song || !variant) return;
  cancelPreview(); const request = previewSequence;
  previewAbort = new AbortController(); const signal = previewAbort.signal;
  $('song-preview').disabled = true; $('song-preview-cancel').hidden = false;
  $('song-preview-status').textContent = 'Reading the selected song’s native note events…';
  const failed = error => {if (request === previewSequence) cancelPreview('Note preview unavailable: ' + error.message + ' Playback is unchanged.');};
  previewTimer = setTimeout(() => failed(Error('Inspection exceeded 30 seconds.')), 30000);
  try {
    const [recordText,registry] = await Promise.all([fetchRecord(new URL(variant.path, assetBase), undefined, {signal}),registryPromise]);
    if (request !== previewSequence) return;
    checkRecord(recordText);
    await checkCatalogDocument(recordText,variant);
    if (request !== previewSequence) return;
    const engine = selectEngine(registry, new URL(location.href).searchParams.get('release') || undefined);
    const worker = new Worker(new URL('worker.mjs',assetBase),{type:'module'}); previewWorker = worker;
    worker.onmessage = ({data}) => {
      if (request !== previewSequence) return;
      try {
        if (data.type !== 'inspection' || data.verdict !== 'valid') throw Error(data.stderr || 'Native inspection failed.');
        if (variant.record_sha256 && data.sourceRecordSha256 !== variant.record_sha256) throw Error('Selected song identity does not match the catalog.');
        const preview = songPreview(data); worker.terminate(); previewWorker = null; clearTimeout(previewTimer);
        $('song-preview-roll').innerHTML = preview.svg + (preview.legend || ''); $('song-preview-roll').hidden = false;
        $('song-preview-status').textContent = `${song.title} · ${variantLabels[variant.kind] || variant.kind}. ${preview.text}`;
        $('song-preview').disabled = false; $('song-preview-cancel').hidden = true;
        $('song-preview').textContent = 'Hide notes'; $('song-preview').setAttribute('aria-expanded','true');
      } catch (error) {failed(error);}
    };
    worker.onerror = event => failed(Error(event.message || 'Inspection worker failed.'));
    worker.onmessageerror = () => failed(Error('Inspection response could not be read.'));
    worker.postMessage({operation:'inspect',engine,recordText});
  } catch (error) {if (!signal.aborted) failed(error);}
};
$('library-research').addEventListener('toggle', async () => {
  if (!$('library-research').open || !catalogIndex?.summary || $('library-statistics').dataset.loaded) return;
  try {
    const summary = await catalogJSON(assetBase, catalogIndex.summary);
    if (summary?.version !== 1 || summary.scope !== 'source-library' || summary.selection !== 'standard'
      || !/^[a-f0-9]{64}$/.test(summary.as_of?.sha256) || summary.compositions !== catalogIndex.total_songs) throw Error('Collection summary is unavailable.');
    const durations = summary.duration_seconds, declarations = summary.declarations;
    const duration = (value, divisor, unit) => Number.isFinite(value) ? (value / divisor).toFixed(1) + ' ' + unit : 'unavailable';
    $('library-statistics').textContent = `${summary.compositions} source compositions · ${summary.canonical_versions} saved versions. `
      + `One Original per composition: duration known for ${durations.known}/${durations.denominator}; total ${duration(durations.total,60,'minutes')}, mean ${duration(durations.mean,1,'s')}, median ${duration(durations.median,1,'s')}. `
      + ['genre','tempo','model'].map(key => `${key} declared for ${declarations[key].known}/${declarations[key].denominator}, unknown ${declarations[key].unknown}`).join('; ') + '. '
      + 'These statistics describe this catalog selection, not all music made with the instrument.';
    $('library-duration-distribution').textContent = Array.isArray(durations.distribution)
      ? 'Duration distribution: ' + durations.distribution.map(bin => `${bin.min_inclusive}–${bin.max_exclusive === null ? 'up' : '<' + bin.max_exclusive} s: ${bin.count}`).join('; ') + '. Snapshot: the source catalog packaged with this player.' : '';
    $('library-declaration-counts').textContent = ['genre','tempo','model'].map(key => {
      const group = declarations[key], counts = group.breakdown;
      if (!counts?.items?.length) return `${key}: no known declarations (${group.unknown} unknown).`;
      const labels = counts.items.slice(0,32).map(item => {
        const label = key === 'tempo' ? (item.min_bpm === item.max_bpm ? `${item.min_bpm} BPM` : `${item.min_bpm}–${item.max_bpm} BPM`) : item.value;
        return `${label}: ${item.count}`;
      });
      return `${key} (${group.known}/${group.denominator} known): ${labels.join('; ')}.`
        + (counts.omitted_labels ? ` ${counts.omitted_labels} further labels cover ${counts.omitted_count} songs.` : '');
    }).join(' ') + ' Counts describe declarations, not musical quality; tempo uses quarter-note BPM.';
    $('library-statistics').dataset.loaded = 'true';
  } catch (error) {$('library-statistics').textContent = error.message;}
});
loadLibrary();

async function loadFile(file) {
  if (!file) return;
  showPlayer();
  invalidate(); const requestGeneration = generation;
  playerStatus('Loading ' + file.name + '…');
  try {
    if (file.size > limits.recordBytes) throw Error('Record exceeds the 1 MB input limit.');
    const text = new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await file.arrayBuffer());
    if (requestGeneration !== generation) return;
    checkRecord(text); $('record').value = text; loadedIdentity = {title:parseDocument(text).metadata.title || file.name.replace(/\.tl$/i,''),version:'Local file · as saved'};
    const result = await preparePlayback(text);
    if (lastResult === result && result?.verdict === 'valid' && result.recordSha256) {
      linkedMetadata = {path:null,sha:result.recordSha256,recordText:text,match:catalog.cached(null,result.recordSha256),controller:new AbortController(),timer:null};
      reconcileLinkedIdentity();
    }
    return result;
  } catch (error) { if (requestGeneration === generation) playerStatus(error.message); }
}
function surfaceLinkedSelection(match = null) {
  libraryHistory = []; libraryCursor = null; catalogResult = null;
  librarySelection = match ? [{song:match.song,variants:match.song.variants}] : [];
  selectedSongId = match?.song.id || ''; songChoices.clear();
  $('song-select').replaceChildren(...(match ? [songChoice(match.song,songLabel(match.song,match.variant))] : []));
  $('song-select').disabled = !match; $('song-select').setAttribute('aria-busy','false');
  $('library-previous').disabled = $('library-next').disabled = true;
  for (const view of ['featured','all']) $('library-' + view).setAttribute('aria-pressed','false');
  $('library-view-detail').textContent = 'The incoming song is selected here. Choose Favorites or Browse all to resume browsing.';
  $('library-path').textContent = 'Source library › Song from link';
  $('library-status').textContent = match ? 'Linked saved version selected. Browse the library to choose another song.'
    : 'The linked song is in Player; no unrelated library song is selected.';
  libraryVariants(match?.variant.kind);
}
// A link never supplies its display title. Only an exact path/hash match, or a
// hash naming one song (legacy path), in a validated packaged catalog can label
// a successfully prepared linked record.
function reconcileLinkedIdentity() {
  const context = linkedMetadata; if (!context || context.catalogMismatch) return;
  context.match ||= catalog.cached(context.path, context.sha);
  if (!context.match || lastResult?.verdict !== 'valid'
      || ![lastResult.recordSha256,lastResult.sourceRecordSha256].includes(context.sha)) return;
  const checksum=context.match.variant.document_sha256 ?? context.match.variant.record_sha256;
  if (checksum && context.checkedDocumentSha !== checksum) {
    if (!context.documentCheck) context.documentCheck=checkCatalogDocument(context.recordText,context.match.variant).then(()=>{
      context.checkedDocumentSha=checksum;
      if(context===linkedMetadata)reconcileLinkedIdentity();
    }).catch(()=>{context.catalogMismatch=true;context.match=null;});
    return;
  }
  loadedSong = context.match;
  loadedIdentity = {title:loadedSong.song.title,version:variantLabels[loadedSong.variant.kind] || loadedSong.variant.kind};
  songLibrary = librarySelection.map(item => item.song);
  if (!songLibrary.some(song => song.id === loadedSong.song.id)) songLibrary.push(loadedSong.song);
  if (linkedSelectionActive) surfaceLinkedSelection(loadedSong);
  updateTransport(false);
}
async function hydrateLinkedIdentity(context) {
  context.timer = setTimeout(() => context.controller.abort(), 10000);
  try {
    await catalog.open();
    if (context !== linkedMetadata || context.controller.signal.aborted) return;
    context.match = context.hint
      ? await catalog.lookup(context.hint,context.path,context.sha,context.controller.signal)
      : catalog.cached(context.path,context.sha);
    if (context === linkedMetadata) reconcileLinkedIdentity();
  } catch { // Optional display metadata must never refuse or delay valid audio.
    if (context === linkedMetadata) reconcileLinkedIdentity();
  } finally {clearTimeout(context.timer);}
}
// The link is resolved and checked before anything is replaced, so a failed
// link keeps the current song and selection and reports the problem plainly.
let linkRequest = 0;
const linkGenerations = new WeakMap();
async function loadLink(value) {
  const request = ++linkRequest, requestGeneration = generation, current = () => request === linkRequest && requestGeneration === generation;
  playerStatus('Loading record…');
  try {
    let loaded;
    try { loaded = await resolveLink(value, location.href); }
    catch (error) { if (current()) error.linkMessage = linkProblem(error); throw error; }
    if (!current()) throw Error('Loading cancelled.');
    checkRecord(loaded.recordText);
    const cachedMatch=loaded.libraryPath && loaded.recordSha256 ? catalog.cached(loaded.libraryPath,loaded.recordSha256) : null;
    if(cachedMatch)await checkCatalogDocument(loaded.recordText,cachedMatch.variant);
    if (!current()) throw Error('Loading cancelled.');
    invalidate(); linkedSelectionActive = true; surfaceLinkedSelection(); linkGenerations.set(loaded, generation);
    $('record').value = loaded.recordText; expectedRecordSha = loaded.recordSha256 || null; loadedRelease = loaded.release;
    loadedIdentity = {title:'Linked song',version:'Linked saved version'};
    if (loaded.libraryPath && loaded.recordSha256) {
      linkedMetadata = {path:loaded.libraryPath,sha:loaded.recordSha256,hint:loaded.catalogPage,
        controller:new AbortController(),timer:null,recordText:loaded.recordText,match:cachedMatch,
        checkedDocumentSha:cachedMatch?.variant.document_sha256 ?? cachedMatch?.variant.record_sha256};
      void hydrateLinkedIdentity(linkedMetadata);
    }
    playerStatus('Record loaded. Listen or verify.');
    return loaded;
  } catch (error) {
    if (current()) playerStatus((error.linkMessage || error.message)
      + ($('record').value.trim() ? ' The current song is unchanged.' : ''));
    throw error;
  }
}
async function loadLinkForPlayback(value) {
  const loaded = await loadLink(value);
  if (linkGenerations.get(loaded) !== generation) throw Error('Loading cancelled.');
  return preparePlayback(loaded.recordText);
}
$('file').onchange = () => {
  const file = $('file').files[0];
  // A changed local file may keep the same name. Permit selecting it again.
  $('file').value = '';
  return loadFile(file).catch(error => { playerStatus(error.message); });
};
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, event => { event.preventDefault(); $('dropzone').classList.add('drag'); });
$('dropzone').ondragleave = () => $('dropzone').classList.remove('drag');
$('dropzone').ondrop = event => {
  event.preventDefault(); $('dropzone').classList.remove('drag');
  loadFile(event.dataTransfer.files[0]).catch(error => { playerStatus(error.message); });
};
$('url-form').onsubmit = event => {
  event.preventDefault(); showPlayer();
  return loadLinkForPlayback($('url').value).catch(() => {});
};
$('example').onclick = async () => {
  showPlayer();
  invalidate(); const requestGeneration = generation;
  playerStatus('Loading example…');
  try {
    const text = await fetchRecord(new URL('example.tl', assetBase));
    if (requestGeneration === generation) { $('record').value = text; loadedIdentity = {title:'Example',version:'Original example'}; return await preparePlayback(text); }
  } catch (error) { if (requestGeneration === generation) playerStatus(error.message); }
};
$('tl-download').onclick = () => download(new Blob([lastResult.canonical], {type: 'text/plain'}), downloadName('tl'));
$('wav-download').onclick = () => {
  try { download(toWav(lastResult, Object.fromEntries(new FormData($('provenance')))), downloadName('wav')); }
  catch (error) { playerStatus(error.message); }
};
$('svg-download').onclick = () => {
  try { download(new Blob([toSvg(lastResult)], {type: 'image/svg+xml'}), 'timeline.svg'); }
  catch (error) { playerStatus(error.message); }
};
function display(view) {
  displayView = view;
  $('display-surface').setAttribute('data-view', view);
  $('live-panel').hidden = view !== 'live';
  instrumentView.setVisible(liveVisible());
  synchronizeLiveAnimation();
  if (view !== 'spectrum') pauseSpectrum();
  $('timeline').hidden = view !== 'notes';
  $('spectrum-panel').hidden = view !== 'spectrum';
  $('display-off').hidden = view !== 'off';
  $('view-off').setAttribute('aria-pressed', String(view === 'off'));
  $('view-notes').setAttribute('aria-pressed', String(view === 'notes'));
  $('view-live').setAttribute('aria-pressed', String(view === 'live'));
  $('view-spectrum').setAttribute('aria-pressed', String(view === 'spectrum'));
}
function pauseSpectrum() {
  const controller = spectrumController;spectrumController = null;
  if (controller) {
    controller.abort();$('spectrum-cancel').hidden = true;
    $('spectrum-status').textContent = 'Analysis paused. Select Spectrogram to continue.';
  }
}
async function showSpectrum() {
  display('spectrum');
  if (document.hidden) return;
  const result = lastResult;
  if (!result?.pcm || result.verdict !== 'valid') return;
  if (spectrumResult === result) { $('spectrogram').hidden = false; return; }
  if (spectrumController) return;
  const controller = new AbortController(); spectrumController = controller;
  $('spectrum-cancel').hidden = false; $('png-download').disabled = true;
  $('spectrum-status').textContent = 'Analysing audio… Playback remains available.';
  try {
    await spectrogram(result, $('spectrogram'), {signal: controller.signal, onProgress: fraction => {
      if (spectrumController === controller && lastResult === result)
        $('spectrum-status').textContent = `Analysing audio… ${Math.round(fraction * 100)}%. Playback remains available.`;
    }});
    if (spectrumController !== controller || lastResult !== result) return;
    spectrumResult = result; $('spectrogram').hidden = false; $('png-download').disabled = false;
    $('spectrum-status').textContent = 'Channel 1 · bass and full-range views · seconds / Hz · display only.';
  } catch (error) {
    if (spectrumController === controller && lastResult === result)
      $('spectrum-status').textContent = error.name === 'AbortError' ? 'Analysis cancelled. Select Spectrogram to retry.' : error.message;
  } finally {
    if (spectrumController === controller) { spectrumController = null; $('spectrum-cancel').hidden = true; }
  }
}
$('view-live').onclick = () => { display('live'); updateTransport(); };
$('view-off').onclick = () => display('off');
$('view-notes').onclick = () => display('notes');
$('view-spectrum').onclick = showSpectrum;
$('spectrum').onclick = showSpectrum;
$('spectrum-cancel').onclick = () => spectrumController?.abort();
document.addEventListener?.('visibilitychange', () => {
  instrumentView.setVisible(liveVisible());
  synchronizeLiveAnimation();
  if (document.hidden) pauseSpectrum();
  else {
    updateTransport();
    if (displayView === 'spectrum') void showSpectrum();
  }
});
$('png-download').onclick = () => $('spectrogram').toBlob(blob => download(blob, 'spectrogram-channel-1.png'));
$('copy-guide').onclick = async () => {
  let text;
  try {text = await guide();}
  catch (error) {playerStatus(error.message);$('guide-manual').hidden = false;return;}
  try {await navigator.clipboard.writeText(text);$('guide-manual').hidden = true;playerStatus('Browser guide copied.');}
  catch {$('guide-manual').hidden = false;playerStatus('Clipboard access is unavailable. Open Browser guide to read or copy it manually.');}
};

async function loadedSongLink(result) {
  for (const song of songLibrary) {
    const variant = song.variants.find(item => item.record_sha256 === result.recordSha256);
    if (variant) return {url:libraryLink(variant.path, variant.record_sha256, assetBase, result.rendererRelease, song.catalogPage),
      label:song.title + ' · ' + (variantLabels[variant.kind] || variant.kind), portable:false};
  }
  return {url:await portableLink(result.canonical, assetBase, result.rendererRelease), label:'Portable song link', portable:true};
}
// Touch devices hand the link to the native share sheet when it can carry it; the clipboard stays the fallback.
function shareSheet(data) {
  try {
    return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches
      && typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && navigator.canShare(data);
  } catch { return false; }
}
if (shareSheet({url: location.href})) $('share').textContent = 'Share song link';
$('share').onclick = async () => {
  const result = lastResult;
  try {
    if (!result || result.verdict !== 'valid') return;
    const {url, label:linkLabel, portable} = await loadedSongLink(result);
    if (lastResult !== result) return;
    const data = {title: linkLabel, url};
    if (shareSheet(data)) {
      try {
        await navigator.share(data);
        if (lastResult === result) playerStatus(portable ? 'Portable record link shared. Nothing was published.' : 'Song link shared for this exact saved version. Nothing was published.');
        return;
      } catch (error) {
        if (error?.name === 'AbortError') { if (lastResult === result) playerStatus('Sharing cancelled. Nothing was published.'); return; }
        if (lastResult !== result) return;
      }
    }
    try { await navigator.clipboard.writeText(url); }
    catch { if (lastResult === result) { openQr(url, {label:linkLabel, trigger:$('share'), selectLink:true}); playerStatus('Clipboard access is unavailable. The song link is selected in the dialog; copy it manually. Nothing was published.'); } return; }
    if (lastResult !== result) return;
    playerStatus(portable ? 'Portable record link copied. Nothing was published.' : 'Song link copied for this exact saved version. Nothing was published.');
  } catch (error) { if (lastResult === result) playerStatus(error.message); }
};
$('show-qr').onclick = async () => {
  const result = lastResult;
  try {
    if (!result || result.verdict !== 'valid') return;
    const {url, label:linkLabel} = await loadedSongLink(result);
    if (lastResult !== result) return;
    openQr(url, {label:linkLabel, trigger:$('show-qr')});
  } catch (error) { if (lastResult === result) playerStatus(error.message); }
};
function loadLocationLink() {
  if (isRecordLink(new URL(location.href))) loadLinkForPlayback(location.href).catch(() => {});
}
loadLocationLink();
window.addEventListener('hashchange', loadLocationLink);
if ('launchQueue' in window) window.launchQueue.setConsumer(async launch => {
  if (!launch.files?.length) return;
  try { await loadFile(await launch.files[0].getFile()); }
  catch (error) { playerStatus(error.message); }
});
