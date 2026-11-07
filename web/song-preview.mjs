// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {nativeTimeline, pianoRollGeometry, songStructure, songStructureText} from './media.mjs';
const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[char]));
function pitchName(frequency) {
  const pitch = 69 + 12 * Math.log2(frequency / 440), nearest = Math.round(pitch);
  const name = ['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][((nearest % 12) + 12) % 12] + (Math.floor(nearest / 12) - 1);
  const cents = Math.round((pitch - nearest) * 100);
  return name + (cents ? ` (${cents > 0 ? '+' : ''}${cents}¢)` : '');
}
export function songPreview(result) {
  const model = nativeTimeline(result);
  if (model.eventError) throw Error(model.eventError.replace('PCM remains available.', 'Load the song to listen.'));
  if (!(model.duration > 0)) throw Error('Native duration is unavailable.');
  const geometry = pianoRollGeometry(model);
  let svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="222" role="img" aria-label="Selected song note gates"><title>Selected song note gates</title><desc>Time runs left to right; pitch runs upwards. Note names use equal temperament with A4 at 440 Hz; detuning is shown in cents. Native gates include generated events and are not release tails or an audio measurement.</desc><g fill="#a9c0cc" font-family="system-ui,sans-serif" font-size="11">';
  for (const tick of geometry.noteTicks) svg += `<text x="8%" y="${12 + tick.y * .8}" text-anchor="end">${tick.label}</text>`;
  svg += '</g><svg x="10%" y="8" width="88%" height="170" viewBox="0 0 1000 210" preserveAspectRatio="none">';
  for (const tick of geometry.noteTicks) svg += `<path d="M0 ${tick.y}H1000" stroke="#304650" stroke-width=".6"/>`;
  for (const note of geometry.bars) svg += `<rect x="${note.x}" y="${note.y}" width="${note.width}" height="${note.height}" fill="${note.color}"><title>${escape(note.stream)} · ${pitchName(note.frequency)} · ${note.frequency.toFixed(1)} Hz · ${note.start.toFixed(2)}–${note.end.toFixed(2)} s</title></rect>`;
  svg += '</svg><g fill="#a9c0cc" font-family="system-ui,sans-serif" font-size="11">';
  for (let i = 0; i <= 4; i++) svg += `<text x="${10 + i * 22}%" y="197" text-anchor="${i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}">${(model.duration * i / 4).toFixed(1)} s</text>`;
  svg += '</g></svg>';
  const pitches = [...new Set([...model.notes].sort((a,b) => a.frequency - b.frequency).map(note => pitchName(note.frequency)))];
  const visibleStreams = model.streams.slice(0,32), remainingStreams = model.streams.length - visibleStreams.length;
  const legend = '<div class="song-preview-legend" aria-label="Note stream colors">' + visibleStreams.map(stream => {
    const color = geometry.bars.find(note => note.stream === stream).color;
    return `<span style="border-left-color:${color}">${escape(stream)}</span>`;
  }).join('') + (remainingStreams ? `<span>${remainingStreams} more streams</span>` : '') + '</div>';
  const structure = songStructure(result);
  const description = '<section class="song-structure"><h4>Selected song structure</h4><p class="small">' + escape(songStructureText(structure))
    + '</p><p class="small">Gates describe commands, not release tails, audible pitch or musical quality.</p></section>';
  return {svg, legend:legend + description, structure, text:`${model.notes.length} visible gate segment${model.notes.length === 1 ? '' : 's'} across ${model.streams.length} streams: ${visibleStreams.join(', ') || 'none'}${remainingStreams ? `; ${remainingStreams} more` : ''}. ${Number(result.info.voices)} allocated voices. Native note events only; no audio was rendered or verified. Pitch names (A4 = 440 Hz): ${pitches.slice(0,24).join(', ') || 'none'}${pitches.length > 24 ? `; ${pitches.length - 24} more` : ''}. Detuning is shown in cents.`};
}
