<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# QR Code generator library

Vendored Project Nayuki QR Code generator v1.8.0, MIT licensed.

- [Upstream project](https://www.nayuki.io/page/qr-code-generator-library)
- [Release](https://github.com/nayuki/QR-Code-generator/releases/tag/v1.8.0)
- [Released ES6 JavaScript](https://github.com/nayuki/QR-Code-generator/releases/download/v1.8.0/qrcodegen-v1.8.0-es6.js)
- [Source commit](https://github.com/nayuki/QR-Code-generator/tree/720f62bddb7226106071d4728c292cb1df519ceb)
- [License](LICENSE)

`qrcodegen.mjs` contains the released JavaScript with trailing spaces removed
and an appended ES-module export of `QrCode` and `QrSegment`. No build tool or runtime network
request is needed. `../../qr.mjs` handles URL validation and SVG presentation;
the vendored library owns QR encoding, error correction, sizing and masking.
