#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Bounded channel codes and a rendered-template matcher.

Writes alphabet0 frames through compose and supplies template primitives
used by the audio analysis tools. It is not a complete message transport
or a streaming device receiver.
"""
import argparse
import copy
from dataclasses import dataclass
import hashlib
import itertools
import json
from pathlib import Path
import random
import tempfile
import wave
import zipfile
import zlib


#  fixes the field and code lengths. Root ordering and CRC conventions
# are provisional regression choices; there is no dependency on binary libm.
DIALECT = {
    'field_polynomial': 0x13,       # x^4 + x + 1
    'field_generator': 2,
    'code_length': 15,
    'data_lengths': (11, 7),
    'first_root': 1,
    'crc8_polynomial': 0x07,
    'crc8_initial': 0,
    'crc16_polynomial': 0x1021,
    'crc16_initial': 0xffff,
    'clocks_ms': (12, 25, 50, 100),
    'reuse_ms': 400,
    'guard_ms': 400,
    'template_ms': 1500,
    'velocity': 32767,
    'riff': (0, 15, 5, 10, 3, 12, 6, 9),
    'tick_millihz': 5200000,
    'sender_tag_bits': 16,
    'max_payload_bytes': 2047,     # three header nibbles count payload nibbles
    'voice_limit': 64,
    'pitches_millihz': (329628, 349228, 369994, 391995,
                       415305, 440000, 466164, 493883,
                       523251, 554365, 587330, 622254,
                       659255, 698456, 739989, 783991),
}


def _tables():
    exp, log, x = [], [-1] * 16, 1
    for i in range(15):
        exp.append(x)
        log[x] = i
        x <<= 1
        if x & 16:
            x ^= DIALECT['field_polynomial']
    if x != 1 or len(set(exp)) != 15:
        raise AssertionError('GF16 primitive polynomial/generator')
    mul = tuple(0 if not a or not b else exp[(log[a] + log[b]) % 15]
                for a in range(16) for b in range(16))
    inv = tuple(0 if not a else exp[-log[a] % 15] for a in range(16))
    return tuple(exp), tuple(log), mul, inv


_EXP, _LOG, _MUL, _INV = _tables()


def _mul(a, b):
    return _MUL[(a << 4) | b]


def _valid(values, length, what):
    values = tuple(values)
    if len(values) != length:
        raise ValueError('%s needs %d symbols' % (what, length))
    if any(type(x) is not int or not 0 <= x <= 15 for x in values):
        raise ValueError('%s symbols must be integers 0..15' % what)
    return values


def _check_k(k):
    if type(k) is not int or k not in DIALECT['data_lengths']:
        raise ValueError('RS data length must be 11 or 7')


def _generator(k):
    # High-degree-first polynomial ∏(x + α^j), j=1..15-k.
    p = [1]
    for j in range(DIALECT['first_root'], DIALECT['first_root'] + 15 - k):
        q = [0] * (len(p) + 1)
        for i, coefficient in enumerate(p):
            q[i] ^= coefficient
            q[i + 1] ^= _mul(coefficient, _EXP[j % 15])
        p = q
    return tuple(p)


_GENERATORS = {k: _generator(k) for k in DIALECT['data_lengths']}


def rs_encode(data, k=11):
    """Systematic RS(15,k); exactly k nibbles in, 15 nibbles out."""
    _check_k(k)
    data = _valid(data, k, 'RS data')
    out = list(data) + [0] * (15 - k)
    g = _GENERATORS[k]
    for i in range(k):
        coefficient = out[i]
        if coefficient:
            for j in range(1, len(g)):
                out[i + j] ^= _mul(coefficient, g[j])
    return data + tuple(out[k:])


def _syndromes(word, count):
    out = []
    for power in range(1, count + 1):
        x, value = _EXP[power], 0
        for coefficient in word:
            value = _mul(value, x) ^ coefficient
        out.append(value)
    return out


def _locator(syndromes):
    # Berlekamp–Massey, ascending powers. C(x)=∏(1 + X_i*x);
    # an error at high-degree-first index p has X_i=α^(14-p).
    count = len(syndromes)
    c, b = [1] + [0] * count, [1] + [0] * count
    degree, gap, old_discrepancy = 0, 1, 1
    for n in range(count):
        discrepancy = syndromes[n]
        for i in range(1, degree + 1):
            discrepancy ^= _mul(c[i], syndromes[n - i])
        if not discrepancy:
            gap += 1
            continue
        saved = c[:]
        scale = _mul(discrepancy, _INV[old_discrepancy])
        for i in range(count + 1 - gap):
            c[i + gap] ^= _mul(scale, b[i])
        if 2 * degree <= n:
            degree = n + 1 - degree
            b, old_discrepancy, gap = saved, discrepancy, 1
        else:
            gap += 1
    return c[:degree + 1]


def _magnitudes(positions, syndromes):
    # Solve S_r = Σ e_i * X_i^r, r=1..number_of_errors. This is a
    # small Vandermonde system; root validation precedes elimination.
    size = len(positions)
    rows = [[_EXP[((14 - p) * (r + 1)) % 15] for p in positions]
            + [syndromes[r]] for r in range(size)]
    for col in range(size):
        pivot = next((r for r in range(col, size) if rows[r][col]), None)
        if pivot is None:
            raise ValueError('uncorrectable RS singular error system')
        rows[col], rows[pivot] = rows[pivot], rows[col]
        inverse = _INV[rows[col][col]]
        rows[col] = [_mul(x, inverse) for x in rows[col]]
        for r in range(size):
            if r != col and rows[r][col]:
                coefficient = rows[r][col]
                rows[r] = [a ^ _mul(coefficient, b)
                           for a, b in zip(rows[r], rows[col])]
    return [row[-1] for row in rows]


def rs_decode(word, k=11):
    """Return (data, corrected positions); reject uncorrectable syndromes.

    Within t=(15-k)/2 errors, correction is unique. Above t a valid
    different codeword can be returned; the frame's CRC must detect such
    miscorrections. The caller supplies the block number for its report.
    """
    _check_k(k)
    word = _valid(word, 15, 'RS codeword')
    syndromes = _syndromes(word, 15 - k)
    if not any(syndromes):
        return word[:k], ()
    locator = _locator(syndromes)
    degree = len(locator) - 1
    if not 1 <= degree <= (15 - k) // 2:
        raise ValueError('uncorrectable RS locator degree')
    positions = []
    for p in range(15):
        x, value = _EXP[(p - 14) % 15], 0
        for coefficient in reversed(locator):
            value = _mul(value, x) ^ coefficient
        if value == 0:
            positions.append(p)
    if len(positions) != degree:
        raise ValueError('uncorrectable RS locator roots')
    out = list(word)
    for p, magnitude in zip(positions, _magnitudes(positions, syndromes)):
        if not magnitude:
            raise ValueError('uncorrectable RS zero error magnitude')
        out[p] ^= magnitude
    if any(_syndromes(out, 15 - k)):
        raise ValueError('uncorrectable RS residual syndrome')
    return tuple(out[:k]), tuple(positions)


def _crc(data, width, polynomial, initial):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError('CRC input must be bytes')
    value, top, mask = initial, 1 << (width - 1), (1 << width) - 1
    for byte in data:
        value ^= byte << (width - 8)
        for _ in range(8):
            value = ((value << 1) ^ (polynomial if value & top else 0)) & mask
    return value


def crc8(data):
    """CRC-8/SMBUS: polynomial0x07, init0, no reflection/xorout."""
    return _crc(data, 8, DIALECT['crc8_polynomial'], DIALECT['crc8_initial'])


def crc16(data):
    """CRC-16/CCITT-FALSE: polynomial0x1021, init0xffff, no reflection/xorout."""
    return _crc(data, 16, DIALECT['crc16_polynomial'], DIALECT['crc16_initial'])


def _nibbles(data):
    return tuple(n for byte in data for n in (byte >> 4, byte & 15))


def _pack(nibbles):
    values = tuple(nibbles)
    if len(values) % 2:
        values += (0,)
    return bytes((a << 4) | b for a, b in zip(values[::2], values[1::2]))


def frame_symbols(payload, sender=0):
    """One alphabet0 frame, without tick/riff or render configuration.

    Provisional ordinary header: length(3 nibbles), alphabet0(1),
    sender(4), CRC8(2), reserved0(1). The CRC covers all non-CRC fields,
    with the final reserved nibble padded on its right for byte packing.
    Payload CRC16 is appended before zero-padding and RS15/11 encoding.
    These bytes carry no envelope, extra stream or render metadata.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('payload must be bytes')
    payload = bytes(payload)
    if len(payload) > DIALECT['max_payload_bytes']:
        raise ValueError('payload exceeds the three-nibble length field')
    if type(sender) is not int or not 0 <= sender < 1 << DIALECT['sender_tag_bits']:
        raise ValueError('sender must be an unsigned 16-bit integer')
    data = _nibbles(payload)
    length = len(data)
    prefix = ((length >> 8) & 15, (length >> 4) & 15, length & 15, 0,
              (sender >> 12) & 15, (sender >> 8) & 15, (sender >> 4) & 15, sender & 15)
    check = crc8(_pack(prefix + (0,)))
    header = prefix + (check >> 4, check & 15, 0)
    trailer = crc16(payload)
    protected = data + _nibbles(trailer.to_bytes(2, 'big'))
    protected += (0,) * (-len(protected) % 11)
    words = [rs_encode(header)]
    words += [rs_encode(protected[i:i + 11]) for i in range(0, len(protected), 11)]
    return tuple(symbol for word in words for symbol in word)


def frame_header(symbols):
    """Validate exactly 15 header symbols and return the frame's dimensions.

    This recovers the existing ordinary alphabet0 header, including its
    sender and RS correction positions. It consumes no payload symbols
    and infers no timing, rendering configuration or acoustic parameters.
    """
    try:
        header, corrected = rs_decode(symbols)
    except ValueError as error:
        raise ValueError('header block uncorrectable: ' + str(error)) from error
    length = (header[0] << 8) | (header[1] << 4) | header[2]
    if header[3] != 0 or header[10] != 0:
        raise ValueError('unsupported alphabet or reserved header field')
    if length % 2 or length > 2 * DIALECT['max_payload_bytes']:
        raise ValueError('header payload length is not a bounded byte count')
    if crc8(_pack(header[:8] + header[10:])) != (header[8] << 4) | header[9]:
        raise ValueError('header CRC8 mismatch')
    blocks = (length + 4 + 10) // 11
    return {'payload_bytes': length // 2,
            'sender': (header[4] << 12) | (header[5] << 8) | (header[6] << 4) | header[7],
            'payload_blocks': blocks, 'frame_symbols': 15 * (1 + blocks),
            'corrected_positions': corrected}


def unframe_symbols(symbols):
    """Decode a known frame's symbol vector; no WAV/timing inference.

    Return payload and block correction positions, or a named refusal.
    The acoustic decoder/verifier remains a separate, incomplete stage.
    """
    symbols = tuple(symbols)
    if len(symbols) < 30 or len(symbols) % 15:
        raise ValueError('frame needs a header and whole payload RS blocks')
    header = frame_header(symbols[:15])
    length, blocks = 2 * header['payload_bytes'], header['payload_blocks']
    if len(symbols) != header['frame_symbols']:
        raise ValueError('frame length differs from header')
    decoded = []
    report = {'sender': header['sender'],
              'header_corrected': header['corrected_positions'], 'payload_blocks': []}
    for block in range(blocks):
        start = 15 * (block + 1)
        try:
            values, corrected = rs_decode(symbols[start:start + 15])
        except ValueError as error:
            raise ValueError('payload block %d uncorrectable: %s' % (block, error)) from error
        decoded.extend(values)
        report['payload_blocks'].append({'block': block, 'corrected': corrected})
    payload = _pack(decoded[:length])
    trailer = int.from_bytes(_pack(decoded[length:length + 4]), 'big')
    if any(decoded[length + 4:]):
        raise ValueError('nonzero payload padding')
    if crc16(payload) != trailer:
        raise ValueError('payload CRC16 mismatch')
    return payload, report


@dataclass(frozen=True)
class EncodedRecord:
    """Unpinned record files and host metadata; nothing extra is transmitted."""
    files: tuple
    symbols: tuple
    data_voices: tuple
    tick_voice: int
    length_ns: int
    clock_ms: int
    fs: int


def encode(payload_bytes, clock_ms, fx=('reverb', 500, 500),
           extra_streams=None, pos=None, *, fs=48000, sender=0, out_dir=None):
    """Write the ordinary alphabet0 frame through compose.Piece.

    extra_streams is either None or a compose.Piece containing the free
    streams. The Piece is copied; existing streams, events, patches and
    voice indices remain unchanged. Channel voices are appended. No raw
    record text is parsed. Free events must fit inside the frame/guard.

    fx accepts None (clean), one typed effect tuple (a delay/reverb sends
    at32768), or a sequence of effect/send tuples as templates accepts.
    Fixed pos is an (x,y,z) integer tuple; it selects ordinary stereo.
    out_dir defaults to a fresh directory under out/channel.
    Returns EncodedRecord; callers may render its files with compose.play.
    The function does not claim an acoustic decode or exact-WAV verifier.
    """
    import compose
    if type(clock_ms) is not int or clock_ms not in DIALECT['clocks_ms']:
        raise ValueError('clock must be one of 12, 25, 50, 100 ms')
    if type(fs) is not int or fs not in (44100, 48000):
        raise ValueError('sample rate must be 44100 or 48000')
    coded = frame_symbols(payload_bytes, sender)
    if fx is None:
        statements = ()
    elif isinstance(fx, (tuple, list)) and fx and isinstance(fx[0], str):
        statements = [tuple(fx)]
        if fx[0] in ('reverb', 'delay'):
            statements.append(('send', fx[0], 32768))
        statements = _statements(statements)
    else:
        statements = _statements(fx)
    if pos is not None:
        if (not isinstance(pos, (tuple, list)) or len(pos) != 3
                or any(type(v) is not int or abs(v) > 1 << 24 for v in pos)):
            raise ValueError('pos needs three signed integer millimetre coordinates')
        pos = tuple(pos)
    if extra_streams is not None and not isinstance(extra_streams, compose.Piece):
        raise ValueError('extra_streams must be a compose.Piece, not record text')
    if extra_streams is None:
        piece = compose.Piece('channel-frame', 120, 4, ['regression unpinned alphabet0 frame.'],
                              fs=fs, space='stereo' if pos is not None else None)
    else:
        piece = copy.deepcopy(extra_streams)
    if (piece.fs != fs or piece.space not in (None, 'stereo')
            or (piece.space == 'stereo' and pos is None)
            or getattr(piece, 'fieldh', ()) or piece.block != 64):
        raise ValueError('extra streams have an incompatible sample rate or space route')
    if pos is not None:
        piece.space = 'stereo'
    reserved = ('channel_data', 'channel_marimba', 'channel_tick')
    if (reserved[0] in piece.streams
            or any(name in piece.patches for name in reserved[1:])):
        raise ValueError('extra streams collide with reserved channel names')
    period_ns = clock_ms * 1000000
    symbols = DIALECT['riff'] + coded
    length_ns = (len(symbols) + 1) * period_ns + DIALECT['guard_ms'] * 1000000
    if piece.length_ns is not None and (
            type(piece.length_ns) is not int or not 0 <= piece.length_ns <= length_ns):
        raise ValueError('extra stream length extends beyond the channel guard')
    old_voices = len(piece.voices)
    voices = max(4, (DIALECT['reuse_ms'] + clock_ms - 1) // clock_ms)
    if old_voices + voices + 1 > DIALECT['voice_limit']:
        raise ValueError('channel and extra streams exceed the 64-voice record limit')
    if any(type(values[0]) is not int or not 0 <= values[0] < old_voices
           for values in piece.gen_lanes.values()):
        raise ValueError('extra generator lane addresses an undefined voice')
    for stream, events in piece.ev.items():
        for event in events:
            t, _, _, voice, text, _ = event
            if type(t) is not int or not 0 <= t < length_ns:
                raise ValueError('extra event lies outside the frame: ' + stream)
            if voice is not None and (type(voice) is not int or not 0 <= voice < old_voices):
                if text.startswith('move '):
                    raise ValueError('move on a data voice is refused: %s' % voice)
                raise ValueError('extra event addresses an undefined voice: %s' % voice)
    effects = {row[0]: tuple(row[1:]) for row in statements if row[0] != 'send'}
    for name, values in piece.fxh.items():
        if name not in effects or tuple(values) != effects[name]:
            raise ValueError('extra effect is outside or conflicts with the channel: ' + name)
    for name, values in effects.items():
        piece.fx(**{name: values[0] if name == 'dcblock' else values})
    piece.stream('channel_data', 'tick, riff and one coded alphabet0 frame')
    piece.patch('channel_marimba', *compose.recipe('marimba'))
    piece.patch('channel_tick', *compose.recipe('tick'))
    tick_voice = piece.add_voices('channel_tick', 1, 'channel timing mark')[0]
    data_voices = tuple(piece.add_voices('channel_marimba', voices, '400 ms reuse ring'))
    for voice in data_voices:
        if pos is not None:
            piece.pos('channel_data', 0, voice, *pos)
        for statement in statements:
            if statement[0] == 'send':
                piece.send('channel_data', 0, voice, statement[1], statement[2])
    piece.on_f('channel_data', 0, tick_voice, DIALECT['tick_millihz'], DIALECT['velocity'], '')
    piece.off('channel_data', period_ns // 2, tick_voice)
    for index, symbol in enumerate(symbols):
        voice = data_voices[index % voices]
        onset = (index + 1) * period_ns
        piece.on_f('channel_data', onset, voice, DIALECT['pitches_millihz'][symbol],
                   DIALECT['velocity'], '')
        piece.off('channel_data', onset + period_ns // 2, voice)
    piece.length_ns = length_ns
    if out_dir is None:
        parent = Path(compose.OUT).parent / 'channel'
        parent.mkdir(parents=True, exist_ok=True)
        out_dir = tempfile.mkdtemp(prefix='record-', dir=parent)
    piece.dir = str(out_dir)
    if extra_streams is not None and Path(piece.dir).resolve() == Path(extra_streams.dir).resolve():
        raise ValueError('channel output must not overwrite the extra Piece')
    piece.write()
    return EncodedRecord(tuple(piece.files()), symbols, data_voices, tick_voice,
                         length_ns, clock_ms, fs)


def _statements(statements):
    """Typed effect descriptions, not record text or a second record parser."""
    allowed = {'dcblock': ((1, 100000),),
               'delay': ((50000, 1000000), (0, 29491), (20, 20000)),
               'reverb': ((0, 1000), (0, 1000))}
    effects, sends = {}, {}
    if not isinstance(statements, (tuple, list)):
        raise ValueError('statements must be a sequence of typed tuples')
    for statement in statements:
        if not isinstance(statement, (tuple, list)) or not statement:
            raise ValueError('each statement must be a nonempty tuple')
        name, *values = statement
        if name == 'send':
            if (len(values) != 2 or values[0] not in ('delay', 'reverb')
                    or type(values[1]) is not int or not 0 <= values[1] <= 32768):
                raise ValueError('send takes delay or reverb and an integer 0..32768')
            bus, level = values
            if bus in sends:
                raise ValueError('duplicate send: ' + bus)
            sends[bus] = level
        elif isinstance(name, str) and name in allowed:
            if name in effects:
                raise ValueError('duplicate effect: ' + name)
            bounds = allowed[name]
            if len(values) != len(bounds) or any(
                    type(value) is not int or not lo <= value <= hi
                    for value, (lo, hi) in zip(values, bounds)):
                raise ValueError('effect values outside docs/INSTRUMENT.md section 7: ' + name)
            effects[name] = tuple(values)
        else:
            raise ValueError('effect outside the channel dialect')
    if any(bus not in effects for bus in sends):
        raise ValueError('send needs its declared bus')
    return tuple((name, *effects[name]) for name in sorted(effects)) + tuple(
        ('send', bus, sends[bus]) for bus in sorted(sends))


def read_wav(path):
    """Return (sample rate, float64 mono or stereo mid), in PCM16 units."""
    import numpy as np
    with wave.open(str(path), 'rb') as source:
        fs, channels = source.getframerate(), source.getnchannels()
        if (fs not in (44100, 48000) or channels not in (1, 2)
                or source.getsampwidth() != 2 or source.getcomptype() != 'NONE'):
            raise ValueError('WAV needs PCM16, mono/stereo, and 44100 or 48000 Hz')
        frames = source.getnframes()
        raw = source.readframes(frames)
        if len(raw) != frames * channels * 2:
            raise ValueError('truncated WAV samples')
    samples = np.frombuffer(raw, dtype='<i2').astype(np.float64)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    return fs, samples


@dataclass(frozen=True)
class TemplateBank:
    """Read-only PCM templates and the complete cache identity."""
    samples: object
    identity: dict
    key: str


def _configuration(line, fs, engine):
    if not isinstance(line, str) or not line.startswith('CONFIG '):
        raise ValueError('config_line must be an actual harness CONFIG line')
    try:
        fields = dict(token.split('=', 1) for token in line.split()[1:])
        declaration = dict(token.split('=', 1) for token in engine.split()[1:])
    except ValueError as error:
        raise ValueError('malformed CONFIG or engine declaration') from error
    required = {'fs', 'sine_bits', 'exp_bits', 'phase64', 'env64', 'fb_avg',
                'int128', 'table_bytes', 'table_hash', 'voice_bytes'}
    dimensions = {'patch_bytes', 'fx_bytes', 'stereo_bytes', 'field_bytes'}
    if (fields.keys() not in (required, required | dimensions)
            or len(line.split()) != 1 + len(fields)
            or fields['fs'] != str(fs)):
        raise ValueError('CONFIG fields or sample rate do not match')
    for key in ('sine_bits', 'exp_bits', 'phase64', 'env64', 'fb_avg'):
        if fields[key] != declaration[key]:
            raise ValueError('CONFIG does not match compose.ENGINE: ' + key)
    if fields['table_hash'] != declaration['tables']:
        raise ValueError('CONFIG table hash does not match compose.ENGINE')
    if fields['int128'] not in ('0', '1') or any(
            not fields[key].isdigit() or int(fields[key]) <= 0
            for key in {'table_bytes', 'voice_bytes'} | (dimensions & fields.keys())):
        raise ValueError('invalid CONFIG dimensions/backend')
    return 'CONFIG ' + ' '.join(key + '=' + fields[key] for key in sorted(fields))


def templates(statements, clock_ms, *, config_line, fs=48000,
              recipe_name='marimba', pitches_millihz=None, offset_ns=0,
              cache_dir=None, hold_ns=None, minimum_frames=None, carrier_level_db10=None):
    """Render a 1.5-second template for each pitch using compose's writer.

    ``statements`` is a tuple such as (('reverb',500,500),
    ('send','reverb',32768)); only reverb, delay and DC blocker are allowed.
    ``config_line`` comes from the exact build's recorded harness output.
    The actual harness binary SHA256 also participates in the cache key.
    Call compose.build first: this function neither rebuilds nor probes
    an unrelated case to obtain CONFIG.

    offset_ns is the note's absolute onset in the candidate record. It
    determines the half-slot's exact frame count and the 64-frame send
    ramp phase. A later note is rendered after one silent settled block,
    preserving its phase modulo 64. Its silent prefix is then discarded.
    This avoids accumulating floored slot lengths at 44100 Hz. It does
    not recreate the tails of OTHER notes; a decoder must use SIC.

    The recipe/pitch arguments expose the same matcher to regression. This
    function does not implement framing, timing recovery or a transcriber.
    An explicit hold_ns renders that positive duration plus a 1.5-second
    tail. Omission preserves the original half-slot hold and cache key.
    minimum_frames can extend the native render for a fixed observation
    interval. carrier_level_db10 uses the writer's actual carrier attenuation
    and no-clamp validation. Both options enter identity only when supplied.
    """
    import numpy as np
    import compose
    if type(clock_ms) is not int or clock_ms not in DIALECT['clocks_ms']:
        raise ValueError('clock must be one of 12, 25, 50, 100 ms')
    if type(fs) is not int or fs not in (44100, 48000):
        raise ValueError('sample rate must be 44100 or 48000')
    if type(offset_ns) is not int or offset_ns < 0:
        raise ValueError('offset_ns must be a nonnegative integer')
    if hold_ns is not None and (type(hold_ns) is not int or hold_ns <= 0):
        raise ValueError('hold_ns must be a positive integer or None')
    if minimum_frames is not None and (type(minimum_frames) is not int or minimum_frames <= 0):
        raise ValueError('minimum_frames must be a positive integer or None')
    if carrier_level_db10 is not None and type(carrier_level_db10) is not int:
        raise ValueError('carrier_level_db10 must be an integer or None')
    if not isinstance(recipe_name, str) or not recipe_name:
        raise ValueError('recipe_name must name a compose recipe')
    statements = _statements(statements)
    pitches = DIALECT['pitches_millihz'] if pitches_millihz is None else tuple(pitches_millihz)
    if not pitches or any(type(p) is not int or not 1 <= p <= 0xffffffff for p in pitches):
        raise ValueError('pitches must be nonzero uint32 millihertz integers')
    if len(set(pitches)) != len(pitches):
        raise ValueError('duplicate template pitch')
    engine = compose.ENGINE
    config = _configuration(config_line, fs, engine)
    binary = Path(compose.H)
    if not binary.is_file():
        raise ValueError('build the approved harness with compose.build() first')
    car, mod, comment, extra = compose.recipe(recipe_name)
    patch = compose.patch_headers('channel_template', car, mod, comment, extra)
    if carrier_level_db10 is not None:
        proof = compose.Piece('channel-template-proof', 120, 4, [], fs=fs)
        proof.patch('channel_template', car, mod, comment, extra)
        compose.carrier_attenuation_levels('channel_template', proof.patches['channel_template'],
            proof.routes.get('channel_template', (None, None)), carrier_level_db10, 0,
            vector='channel_template' in proof.vectors, purpose='template')
    period_ns = clock_ms * 1000000
    absolute_frame = offset_ns * fs // 1000000000
    onset_frame = min(absolute_frame, 64 + absolute_frame % 64)
    hold_frames = ((offset_ns + (period_ns // 2 if hold_ns is None else hold_ns)) * fs // 1000000000
                   - absolute_frame)
    if hold_frames < 1:
        raise ValueError('hold_ns must cover at least one sample frame')
    slot_frames = ((offset_ns + period_ns) * fs // 1000000000
                   - absolute_frame)
    frames = DIALECT['template_ms'] * fs // 1000
    if hold_ns is not None:
        frames += hold_frames
    if minimum_frames is not None:
        frames = max(frames, minimum_frames)
    identity = {'version': 1, 'config': config, 'engine': engine,
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                'fs': fs, 'clock_ms': clock_ms, 'statements': statements,
                'recipe': recipe_name, 'patch_headers': patch,
                'pitches_millihz': pitches, 'velocity': DIALECT['velocity'],
                'onset_frame': onset_frame, 'hold_frames': hold_frames,
                'slot_frames': slot_frames, 'template_frames': frames}
    if hold_ns is not None:
        identity['hold_ns'] = hold_ns
    if minimum_frames is not None:
        identity['minimum_frames'] = minimum_frames
    if carrier_level_db10 is not None:
        identity['carrier_level_db10'] = carrier_level_db10
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'))
    # JSON normalization also makes fresh and cached identities identical.
    identity = json.loads(encoded)
    key = hashlib.sha256(encoded.encode()).hexdigest()
    directory = Path(cache_dir) if cache_dir is not None else Path(compose.OUT).parent / 'channel'
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / (key + '.npz')
    if cache.exists():
        try:
            with np.load(cache, allow_pickle=False) as saved:
                metadata = json.loads(str(saved['metadata']))
                if not isinstance(metadata, dict):
                    raise ValueError('template metadata must be an object')
                pcm = saved['pcm']
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile, zlib.error) as error:
            raise ValueError('invalid template cache: ' + key) from error
        if (metadata.get('identity') != identity or pcm.shape != (len(pitches), frames)
                or pcm.dtype != np.dtype('<i2')
                or hashlib.sha256(pcm.tobytes()).hexdigest() != metadata.get('pcm_sha256')):
            raise ValueError('template cache identity or samples do not match: ' + key)
        pcm.setflags(write=False)
        return TemplateBank(pcm, identity, key)
    pcm = np.empty((len(pitches), frames), dtype='<i2')
    hashes = []
    to_ns = lambda n: (n * 1000000000 + fs - 1) // fs
    with tempfile.TemporaryDirectory(prefix='template-', dir=directory) as scratch:
        for index, pitch in enumerate(pitches):
            piece = compose.Piece('channel-template', 120, 4,
                                  ['regression isolated symbol template.'], fs=fs)
            piece.dir = scratch
            piece.length_ns = to_ns(onset_frame + frames)
            piece.stream('data', 'one template symbol')
            piece.patch('channel_template', car, mod, comment, extra)
            piece.add_voices('channel_template', 1, '')
            for statement in statements:
                if statement[0] != 'send':
                    value = statement[1] if statement[0] == 'dcblock' else tuple(statement[1:])
                    piece.fx(**{statement[0]: value})
            for statement in statements:
                if statement[0] == 'send':
                    piece.send('data', 0, 0, statement[1], statement[2])
            piece.on_f('data', to_ns(onset_frame), 0, pitch, DIALECT['velocity'], '')
            piece.off('data', to_ns(onset_frame + hold_frames), 0)
            piece.write(levels_db10=None if carrier_level_db10 is None else
                        {'channel_template': carrier_level_db10})
            wav = Path(scratch) / 'template.wav'
            rc, values, line = compose.play(piece.files(), str(wav))
            if rc or values.get('clipped') != '0' or values.get('block_indep') != 'yes':
                raise ValueError('template render failed: ' + line)
            got_fs, samples = read_wav(wav)
            if got_fs != fs or len(samples) != onset_frame + frames:
                raise ValueError('template render frame count changed')
            pcm[index] = samples[onset_frame:]
            hashes.append(values['hash'])
        metadata = {'identity': identity, 'render_hashes': hashes,
                    'pcm_sha256': hashlib.sha256(pcm.tobytes()).hexdigest()}
        temporary = Path(scratch) / 'cache.npz'
        np.savez_compressed(temporary, pcm=pcm,
                            metadata=json.dumps(metadata, sort_keys=True, separators=(',', ':')))
        temporary.replace(cache)
    pcm.setflags(write=False)
    return TemplateBank(pcm, identity, key)


def match(segment, bank):
    """Return index, fitted level, normalized score and residual RMS.

    Match a finite, nonempty mono segment against each template's prefix.
    The score is its signed correlation divided by template energy's
    square root, as in the study. The selected least-squares level is
    reported separately. No sample timing or framing is inferred here.
    """
    import numpy as np
    if not isinstance(bank, TemplateBank):
        raise ValueError('bank must come from templates()')
    x = np.asarray(segment, dtype=np.float64)
    if (x.ndim != 1 or not len(x) or len(x) > bank.samples.shape[1]
            or not np.isfinite(x).all() or not np.any(x)):
        raise ValueError('segment must be finite, non-silent and fit the templates')
    candidates = bank.samples[:, :len(x)].astype(np.float64)
    energy = np.einsum('ij,ij->i', candidates, candidates)
    if np.any(energy == 0):
        raise ValueError('segment is too short for a template attack')
    correlation = candidates @ x
    scores = correlation / np.sqrt(energy)
    index = int(np.argmax(scores))
    level = float(correlation[index] / energy[index])
    residual = x - level * candidates[index]
    return {'index': index, 'level': level, 'score': float(scores[index]),
            'residual_rms': float(np.sqrt(np.mean(residual * residual)))}


def selftest_codes():
    """All 23,850 nonzero t≤2 errors; seeded t≤4 and t+1 observations."""
    checks = 0
    if crc8(b'123456789') != 0xf4 or crc16(b'123456789') != 0x29b1:
        raise AssertionError('CRC check vectors')
    checks += 2
    rng = random.Random(20261020)
    for k in (11, 7):
        for _ in range(32):
            data = tuple(rng.randrange(16) for _ in range(k))
            codeword = rs_encode(data, k)
            if codeword[:k] != data or any(_syndromes(codeword, 15 - k)):
                raise AssertionError('RS encoder is not systematic or divisible')
            for size in range((15 - k) // 2 + 1):
                positions = sorted(rng.sample(range(15), size))
                received = list(codeword)
                for p in positions:
                    received[p] ^= rng.randrange(1, 16)
                got, corrected = rs_decode(received, k)
                if got != data or corrected != tuple(positions):
                    raise AssertionError('RS seeded correction')
                checks += 1
    data = tuple(rng.randrange(16) for _ in range(11))
    codeword = rs_encode(data)
    exhaustive = 0
    for size in (1, 2):
        for positions in itertools.combinations(range(15), size):
            for errors in itertools.product(range(1, 16), repeat=size):
                received = list(codeword)
                for p, e in zip(positions, errors):
                    received[p] ^= e
                got, corrected = rs_decode(received)
                if got != data or corrected != positions:
                    raise AssertionError('RS15/11 exhaustive correction')
                exhaustive += 1
    if exhaustive != 23850:
        raise AssertionError('RS15/11 enumeration count')
    checks += exhaustive
    for k in (11, 7):
        detected = miscorrected = 0
        for _ in range(1000):
            data = tuple(rng.randrange(16) for _ in range(k))
            received = list(rs_encode(data, k))
            for p in rng.sample(range(15), (15 - k) // 2 + 1):
                received[p] ^= rng.randrange(1, 16)
            try:
                got, _ = rs_decode(received, k)
            except ValueError:
                detected += 1
            else:
                if got == data:
                    raise AssertionError('t+1 unexpectedly corrected within t')
                miscorrected += 1
        print('RS15/%d t+1 sample=1000 detected=%d miscorrected=%d'
              % (k, detected, miscorrected))
        checks += detected + miscorrected
    print('CHANNEL_CODES ok checks=%d exhaustive15_11=%d' % (checks, exhaustive))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test-codes', action='store_true')
    commands = parser.add_subparsers(dest='command')
    encoder = commands.add_parser('encode', help='write one unpinned alphabet0 record')
    encoder.add_argument('--hex', required=True, help='payload bytes as hexadecimal')
    encoder.add_argument('--clock', type=int, choices=DIALECT['clocks_ms'], default=25)
    encoder.add_argument('--fs', type=int, choices=(48000, 44100), default=48000)
    encoder.add_argument('--sender', type=int, default=0)
    encoder.add_argument('--clean', action='store_true')
    encoder.add_argument('--send', type=int, default=32768, help='reverb500/500 send, 0..32768')
    encoder.add_argument('--pos', type=int, nargs=3, metavar=('X', 'Y', 'Z'))
    encoder.add_argument('--out', type=Path, required=True, help='new record output directory')
    args = parser.parse_args()
    if args.command:
        if args.self_test_codes:
            parser.error('choose either --self-test-codes or encode')
        if not 0 <= args.send <= 32768:
            parser.error('--send must be an integer from 0 to 32768')
        try:
            payload = bytes.fromhex(args.hex)
            fx = None if args.clean else (('reverb', 500, 500), ('send', 'reverb', args.send))
            record = encode(payload, args.clock, fx, pos=args.pos, fs=args.fs,
                            sender=args.sender, out_dir=args.out)
        except ValueError as error:
            parser.error(str(error))
        print('CHANNEL_RECORD alphabet=0 payload_bytes=%d payload_nibbles=%d slots=%d fs=%d clock_ms=%d'
              % (len(payload), 2 * len(payload), len(record.symbols) + 1, record.fs, record.clock_ms))
        print('files=' + json.dumps(record.files))
    elif args.self_test_codes:
        selftest_codes()
    else:
        parser.error('bounded preparation: choose --self-test-codes or encode')


if __name__ == '__main__':
    main()
