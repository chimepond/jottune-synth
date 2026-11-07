// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {QrCode, QrSegment} from './vendor/qrcodegen/qrcodegen.mjs';

// QR Model 2 version 40, one byte-mode segment, error correction level M.
export const QR_MAX_BYTES = 2331;
const QUIET_ZONE = 4;
const tooLong = () => new RangeError('This link is too large for a QR code (maximum 2331 bytes). Use a shorter song link.');

/** Pure offline URL encoder. size/matrix exclude the four-module quiet zone. */
export function qrSvg(input) {
  if (typeof input !== 'string' || !/^https?:\/\//i.test(input) || /[\s\u0000-\u001f\u007f\\]/u.test(input))
    throw new TypeError('QR links must be complete HTTP or HTTPS URLs without whitespace or control characters.');
  // Bound work before URL normalization and encoder allocation; never truncate.
  if (input.length > QR_MAX_BYTES) throw tooLong();
  let address;
  try { address = new URL(input); }
  catch { throw new TypeError('QR link is not a valid HTTP or HTTPS URL.'); }
  if (!['http:', 'https:'].includes(address.protocol) || !address.hostname ||
      address.username || address.password || input.match(/^https?:\/\/([^/?#]*)/i)[1].includes('@'))
    throw new TypeError('QR links must use HTTP or HTTPS without embedded credentials.');
  const url = address.href, bytes = new TextEncoder().encode(url);
  if (bytes.length > QR_MAX_BYTES) throw tooLong();
  const qr = QrCode.encodeSegments([QrSegment.makeBytes(bytes)], QrCode.Ecc.MEDIUM, 1, 40, -1, false);
  const matrix = Array.from({length:qr.size}, (_, y) =>
    Array.from({length:qr.size}, (_, x) => qr.getModule(x, y)));
  const paths = [];
  for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++)
    if (matrix[y][x]) paths.push(`M${x + QUIET_ZONE},${y + QUIET_ZONE}h1v1h-1z`);
  const edge = qr.size + QUIET_ZONE * 2;
  // Fixed markup only: the URL is encoded in modules, never interpolated as XML.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${edge} ${edge}" width="${edge}" height="${edge}" role="img" aria-label="Song link QR code" shape-rendering="crispEdges"><title>Song link QR code</title><rect width="100%" height="100%" fill="#fff"/><path d="${paths.join('')}" fill="#000"/></svg>`;
  return {svg, url, size:qr.size, version:qr.version, quietZone:QUIET_ZONE, matrix};
}
