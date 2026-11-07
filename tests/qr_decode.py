#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Independently decode the real browser encoder and optional browser screenshots.

Requires an installed Node, zxing-cpp and Pillow. No installs, network requests,
physical-camera assertions or retained images are part of this check.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlsplit
import zlib

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get('NODE', 'node')
PREFIX = 'https://example.invalid/2026-10-26-' + 'a' * 32 + '/'
RELEASE = '2026-10-25-abcdef0'


def dependencies():
    try:
        import zxingcpp
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError('Independent QR acceptance requires zxing-cpp and Pillow in this Python environment.') from error
    return zxingcpp, Image, ImageDraw


def decode(image, expected):
    zxingcpp, _, _ = dependencies()
    found = zxingcpp.read_barcodes(image)
    if len(found) != 1 or found[0].format != zxingcpp.BarcodeFormat.QRCode:
        raise AssertionError(f'Expected one independently decoded QR symbol; got {len(found)}.')
    if found[0].text != expected:
        raise AssertionError('The independently decoded URL differs from the intended URL.')


def encode(url=None, portable=False):
    # Exercise the shipped module and shipped deflate-link creator; the decoder
    # below is a separate library, not the encoder's matrix assertions.
    script = r'''
      import fs from 'node:fs/promises';
      import {qrSvg,QR_MAX_BYTES} from './web/qr.mjs';
      import {portableLink} from './web/links.mjs';
      let input=''; for await(const chunk of process.stdin) input+=chunk;
      const request=JSON.parse(input);
      const url=request.portable ? await portableLink(await fs.readFile('tests/golden/48000/sine.tl','utf8'),request.base,request.release) : request.url;
      try { process.stdout.write(JSON.stringify({...qrSvg(url),maxBytes:QR_MAX_BYTES})); }
      catch(error) { process.stdout.write(JSON.stringify({error:error.name,message:error.message})); }
    '''
    result = subprocess.run([NODE, '--input-type=module', '-e', script], cwd=ROOT,
                            input=json.dumps({'url': url, 'portable': portable, 'base': PREFIX, 'release': RELEASE}),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def raster(value, scale=6):
    _, Image, ImageDraw = dependencies()
    matrix, size, quiet = value['matrix'], value['size'], value['quietZone']
    if quiet != 4 or size != len(matrix) or any(len(row) != size for row in matrix):
        raise AssertionError('The encoder returned an invalid QR matrix or quiet zone.')
    if any(type(module) is not bool for row in matrix for module in row):
        raise AssertionError('QR matrix entries must be booleans.')
    image = Image.new('L', ((size + 2 * quiet) * scale,) * 2, color=255)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                left, top = (x + quiet) * scale, (y + quiet) * scale
                draw.rectangle((left, top, left + scale - 1, top + scale - 1), fill=0)
    return image


class IndependentDecode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dependencies()

    def check(self, url=None, portable=False):
        value = encode(url, portable)
        self.assertNotIn('error', value)
        self.assertEqual(value['maxBytes'], 2331)
        decode(raster(value), value['url'])
        return value

    def test_canonical_record_link_preserves_hidden_prefix_and_identity(self):
        url = PREFIX + 'r/' + '0123456789abcdef' * 4
        value = self.check(url)
        self.assertEqual(value['url'], url)

    def test_portable_qr_recovers_the_complete_original_record(self):
        value = self.check(portable=True)
        parsed = urlsplit(value['url'])
        self.assertEqual(parsed.path, urlsplit(PREFIX).path)
        self.assertEqual(parse_qs(parsed.query), {'release': [RELEASE]})
        self.assertTrue(parsed.fragment.startswith('v1.'))
        compressed = parsed.fragment[3:]
        decoded = zlib.decompress(base64.urlsafe_b64decode(compressed + '=' * (-len(compressed) % 4)))
        self.assertEqual(decoded, (ROOT / 'tests/golden/48000/sine.tl').read_bytes())

    def test_long_byte_mode_versions_decode_through_standard_maximum(self):
        prefix = 'https://example.invalid/q?x='
        for capacity in (700, 1500, 2331):
            with self.subTest(bytes=capacity):
                pattern = '0123456789abcdefghijklmnopqrstuvwxyz-._~'
                url = prefix + (pattern * capacity)[:capacity - len(prefix)]
                value = self.check(url)
                self.assertEqual(value['url'], url)
                if capacity == 2331:
                    self.assertEqual(value['version'], 40)
                    self.assertEqual(value['size'], 177)

    def test_normalized_unicode_url_decodes_without_text_loss(self):
        value = self.check('https://example.invalid/tune/caf\u00e9?title=\u6708')
        self.assertEqual(value['url'], 'https://example.invalid/tune/caf%C3%A9?title=%E6%9C%88')

    def test_over_capacity_and_unsafe_urls_produce_no_partial_symbol(self):
        prefix = 'https://example.invalid/q?x='
        value = encode(prefix + 'x' * (2332 - len(prefix)))
        self.assertEqual(value.get('error'), 'RangeError')
        self.assertNotIn('matrix', value)
        for url in ('javascript:alert(1)', 'ftp://example.invalid/song', 'https://user:password@example.invalid/'):
            with self.subTest(url=url):
                self.assertEqual(encode(url).get('error'), 'TypeError')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, help='Actual browser PNG screenshot to decode without rewriting it.')
    parser.add_argument('--expected', help='Exact URL expected in that screenshot.')
    args, remaining = parser.parse_known_args()
    if args.image:
        if args.expected is None or remaining:
            parser.error('--image requires --expected and no test selectors.')
        _, Image, _ = dependencies()
        with Image.open(args.image) as source:
            decode(source.convert('L'), args.expected)
        print('QR screenshot independently decoded to the exact intended URL.')
    elif args.expected is not None:
        parser.error('--expected requires --image.')
    else:
        unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == '__main__':
    main()
