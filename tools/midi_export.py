#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Pure, explicitly lossy native-canonical TL to SMF projection.

The caller validates and renders authored input with fm.Native first, then
decides whether to publish these bytes and their complete loss report. This
module neither reads authored TL nor performs I/O or device transmission.
"""
from collections import Counter
from decimal import Context, Decimal, localcontext, ROUND_HALF_UP
from fractions import Fraction
import struct

import fm
from export_schema import SCHEMA


NOTE_EVENTS = {"event_on", "event_pitch", "event_off"}
INTERPRETED_FIELDS = {
    "header_length_ns": {"NS"}, "header_voice": {"INDEX", "PATCH"},
    "event_on": {"T_NS", "STREAM", "VOICE", "FREQ_MILLIHZ", "VELOCITY_Q15"},
    "event_pitch": {"T_NS", "STREAM", "VOICE", "FREQ_MILLIHZ"},
    "event_off": {"T_NS", "STREAM", "VOICE"},
}
VLQ_MAX = 0x0fffffff


def _integer(value, low, high, label, status=2):
    fm.require(type(value) is int and low <= value <= high,
               f"{label} must be an integer in {low}..{high}", status)
    return value


def _vlq(value):
    _integer(value, 0, VLQ_MAX, "MIDI delta ticks", 1)
    result = [value & 127]
    while value > 127:
        value >>= 7
        result.insert(0, 128 | (value & 127))
    return bytes(result)


def _meta(kind, payload):
    return bytes((255, kind)) + _vlq(len(payload)) + payload


def _track(events):
    body, previous = bytearray(), 0
    for tick, data in events:
        body.extend(_vlq(tick - previous))
        body.extend(data)
        previous = tick
    fm.require(len(body) <= 0xffffffff, "MIDI track exceeds its 32-bit chunk size")
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def _position(frequency):
    _integer(frequency, 1, 0xffffffff, "pitch millihertz", 1)
    return Decimal(69) + Decimal(12) * (Decimal(frequency) / 440000).ln() / Decimal(2).ln()


def _pitch(frequency, bend_range, key=None):
    position = _position(frequency)
    if key is None:
        key = int(position.to_integral_value(rounding=ROUND_HALF_UP))
    fm.require(0 <= key <= 127, f"pitch {frequency} mHz has no nearest MIDI key in 0..127")
    offset = (position - key) * 8192 / bend_range
    fm.require(Decimal(-8192) <= offset <= Decimal(8191),
               f"pitch {frequency} mHz exceeds bend range {bend_range} from MIDI key {key}")
    signed = int(offset.to_integral_value(rounding=ROUND_HALF_UP))
    fm.require(-8192 <= signed <= 8191, "rounded MIDI pitch bend is outside its 14-bit range")
    effective = Decimal(key) + Decimal(signed) * bend_range / 8192
    error = (effective - position) * 100
    return key, signed + 8192, dict(source_millihz=frequency, base_key=key,
        bend=signed + 8192, signed_bend=signed, source_midi_note=float(position),
        rendered_midi_note=float(effective), error_cents=float(error))


def encode(accepted, *, channels, ppqn=960, tempo_us=500000,
           midi_format=1, bend_range=2):
    """Return (SMF bytes, JSON-compatible loss report), without mutation or I/O.

    channels maps native voice indices to explicitly selected MIDI channels1..16.
    Each voice owns one channel, including throughout its release. The SMF
    retains performance timing approximately; it never contains the TL synth.
    """
    _integer(ppqn, 1, 32767, "ppqn")
    _integer(tempo_us, 1, 0xffffff, "tempo_us")
    _integer(midi_format, 0, 1, "midi_format")
    _integer(bend_range, 1, 127, "bend_range")
    fm.require(isinstance(accepted, dict) and accepted.get("ok") is True
               and type(accepted.get("exit")) is int and accepted["exit"] == 0,
               "MIDI export requires a successful native canonical render")
    rows = accepted.get("_rows")
    fm.require(isinstance(rows, list) and rows, "MIDI export requires native canonical rows")
    for row in rows:
        fm.require(isinstance(row, dict) and isinstance(row.get("raw"), dict),
                   "unknown canonical structural schema")
        section, identity = row.get("section"), row.get("id")
        fm.require(section != "generator" and identity not in ("event_gen", "event_lane"),
                   "MIDI export refuses generators: note manifests omit generator modulation")
        fm.require(section in SCHEMA and identity in SCHEMA[section],
                   f"unsupported canonical structural schema: {section}/{identity}")
        if identity in INTERPRETED_FIELDS:
            fm.require(set(row["raw"]) == INTERPRETED_FIELDS[identity],
                       f"unsupported canonical fields for {identity}")
    description = fm.describe_rows(accepted)
    voices = {row["index"]: row["patch"] for row in description["voices"]}
    fm.require(len(voices) == len(description["voices"]) and voices,
               "canonical voice declarations are missing or duplicated")
    fm.require(isinstance(channels, dict), "channels must map voice integers to channel integers", 2)
    for voice, channel in channels.items():
        _integer(voice, 0, 63, "mapped voice")
        _integer(channel, 1, 16, "MIDI channel")
        fm.require(voice in voices, f"channel map names unknown voice {voice}", 2)
    fm.require(len(set(channels.values())) == len(channels),
               "each mapped voice requires a distinct MIDI channel", 2)
    length_rows = [r for r in rows if r["id"] == "header_length_ns"]
    fm.require(len(length_rows) == 1, "canonical record needs one length_ns")
    length_ns = _integer(length_rows[0]["raw"].get("NS"), 1, 0xffffffffffffffff,
                         "canonical length_ns", 1)
    source_events = description["events"]
    active = set()
    for event in source_events:
        raw = event["raw"]
        voice = raw.get("VOICE")
        _integer(voice, 0, 63, "canonical event voice", 1)
        fm.require(voice in voices, "canonical event names unknown voice")
        _integer(raw.get("T_NS"), 0, length_ns - 1, "canonical event time", 1)
        active.add(voice)
    fm.require(active <= set(channels),
               "channel map omits active event voices: " + ",".join(map(str, sorted(active - set(channels)))), 2)
    fm.require(all(a["raw"]["T_NS"] <= b["raw"]["T_NS"]
                   for a, b in zip(source_events, source_events[1:])),
               "canonical events are not in native time order")

    # Indexes within each MIDI track retain the canonical source order. Format0
    # has one global event queue, avoiding cross-voice resorting at equal ticks.
    ordered_voices = sorted(channels)
    track_of = {voice: i + 1 if midi_format else 0 for i, voice in enumerate(ordered_voices)}
    tracks = [[] for _ in range(1 + len(ordered_voices) if midi_format else 1)]
    tracks[0].append((0, _meta(0x51, tempo_us.to_bytes(3, "big"))))
    trace, timing, pitches, velocities = [], [], [], []
    lost_events = Counter()
    state = {voice: dict(key=None, held=False) for voice in ordered_voices}
    counts = dict(notes=0, explicit_note_offs=0, retrigger_note_offs=0,
                  end_note_offs=0, pitch_events=0, ignored_pitch_before_note=0,
                  ignored_off_without_held_note=0)

    def tick_of(time_ns, source_index=None, kind="source"):
        numerator, denominator = time_ns * ppqn, tempo_us * 1000
        tick = (2 * numerator + denominator) // (2 * denominator)
        error = Fraction(tick * denominator, ppqn) - time_ns
        timing.append(dict(source_index=source_index, kind=kind, source_ns=time_ns,
            tick=tick, error_ns=float(error), error_ns_numerator=error.numerator,
            error_ns_denominator=error.denominator))
        return tick

    def emit(voice, tick, kind, data, source_index=None, reason=None):
        track = track_of[voice]
        tracks[track].append((tick, bytes(data)))
        trace.append(dict(track=track, tick=tick, voice=voice, channel=channels[voice],
                          kind=kind, bytes=list(data), source_index=source_index, reason=reason))

    for voice in ordered_voices:
        channel = channels[voice] - 1
        if midi_format:
            name = f"Voice {voice}: {voices[voice]}".encode("utf-8")
            tracks[track_of[voice]].append((0, _meta(3, name)))
        for controller, value in ((101, 0), (100, 0), (6, bend_range),
                                  (38, 0), (101, 127), (100, 127)):
            emit(voice, 0, "control_change", (0xb0 | channel, controller, value), reason="pitch_bend_range")
        emit(voice, 0, "pitch_bend", (0xe0 | channel, 0, 64), reason="initial_center")

    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        for index, event in enumerate(source_events):
            identity, raw = event["id"], event["raw"]
            voice, current = raw["VOICE"], state[raw["VOICE"]]
            tick = tick_of(raw["T_NS"], index)
            channel = channels[voice] - 1
            if identity not in NOTE_EVENTS:
                lost_events[identity] += 1
                continue
            if identity == "event_off":
                if current["held"]:
                    emit(voice, tick, "note_off", (0x80 | channel, current["key"], 0), index, "explicit_off")
                    counts["explicit_note_offs"] += 1
                    current["held"] = False
                else:
                    counts["ignored_off_without_held_note"] += 1
                continue
            frequency = raw.get("FREQ_MILLIHZ")
            if identity == "event_pitch" and current["key"] is None:
                _pitch(frequency, bend_range)  # invalid pitches still refuse
                counts["ignored_pitch_before_note"] += 1
                continue
            key, bend, projected = _pitch(frequency, bend_range,
                current["key"] if identity == "event_pitch" else None)
            pitches.append(dict(projected, voice=voice, source_index=index, kind=identity))
            if identity == "event_on":
                raw_velocity = _integer(raw.get("VELOCITY_Q15"), 0, 65535, "native velocity", 1)
                fm.require(raw_velocity != 0, "MIDI export refuses TL note-on velocity 0; MIDI interprets it as note-off")
                effective_velocity = min(raw_velocity, 32767)
                midi_velocity = max(1, min(127, (2 * effective_velocity * 127 + 32767) // (2 * 32767)))
                reconstructed = midi_velocity * 32767 // 127
                velocities.append(dict(source_index=index, voice=voice, source_q15=raw_velocity,
                    effective_q15=effective_velocity, native_clamped=raw_velocity != effective_velocity,
                    midi_velocity=midi_velocity, imported_q15=reconstructed,
                    imported_error_q15=reconstructed - effective_velocity))
                if current["held"]:
                    emit(voice, tick, "note_off", (0x80 | channel, current["key"], 0), index, "held_retrigger")
                    counts["retrigger_note_offs"] += 1
                emit(voice, tick, "pitch_bend", (0xe0 | channel, bend & 127, bend >> 7), index, "note_pitch")
                emit(voice, tick, "note_on", (0x90 | channel, key, midi_velocity), index)
                current.update(key=key, held=True)
                counts["notes"] += 1
            else:
                emit(voice, tick, "pitch_bend", (0xe0 | channel, bend & 127, bend >> 7), index, "pitch_event")
                counts["pitch_events"] += 1

    end_tick = tick_of(length_ns, kind="record_end")
    for voice in ordered_voices:
        if state[voice]["held"]:
            emit(voice, end_tick, "note_off", (0x80 | (channels[voice] - 1), state[voice]["key"], 0),
                 reason="record_end")
            counts["end_note_offs"] += 1
    for track in tracks:
        track.append((end_tick, _meta(0x2f, b"")))
    data = struct.pack(">4sIHHH", b"MThd", 6, midi_format, len(tracks), ppqn)
    data += b"".join(_track(track) for track in tracks)

    omissions = Counter((row["section"], row["id"]) for row in rows
                        if row["section"] in ("patch", "asset", "fx", "space"))
    losses = [dict(id="synth_sound_not_transferred", message="SMF contains performance events, not TL patch synthesis, sound, assets or effects."),
              dict(id="receiver_articulation", message="Receiver envelopes, release tails, oscillator phases, velocity response and note allocation differ; playback audio is not equivalent."),
              dict(id="tempo_provenance_absent", message="The selected constant tempo is an export timing grid; TL does not retain original tempo, meter or program changes.")]
    losses.extend(dict(id=identity, section=section, count=count, message="Native statement omitted from MIDI.")
                  for (section, identity), count in sorted(omissions.items()))
    losses.extend(dict(id=identity, section="event", count=count, message="Native automation/event omitted from MIDI.")
                  for identity, count in sorted(lost_events.items()))
    for key, text in (("retrigger_note_offs", "Held native retrigger projected to MIDI note-off then note-on."),
                      ("end_note_offs", "Still-held notes receive note-off at the record endpoint."),
                      ("ignored_pitch_before_note", "Pitch before any note is omitted."),
                      ("ignored_off_without_held_note", "Off without a MIDI-held note is omitted; repeated native release behavior is not modeled.")):
        if counts[key]:
            losses.append(dict(id=key, count=counts[key], message=text))
    for name, count, text in (
            ("timing_quantization", sum(row["error_ns_numerator"] != 0 for row in timing), "Nanosecond events rounded half up to absolute MIDI ticks."),
            ("pitch_quantization", sum(row["error_cents"] != 0 for row in pitches), "Pitch quantized to a base note and 14-bit pitch bend."),
            ("velocity_quantization", sum(row["imported_error_q15"] != 0 for row in velocities), "Effective Q15 velocity quantized to MIDI1..127."),
            ("native_velocity_clamp", sum(row["native_clamped"] for row in velocities), "Native values above32767 use the engine's effective clamped velocity.")):
        if count:
            losses.append(dict(id=name, count=count, message=text))
    report = dict(version=1, format="smf", midi_format=midi_format, tracks=len(tracks),
        ppqn=ppqn, tempo_us=tempo_us, bend_range_semitones=bend_range,
        pitch_bend_denominator=8192, pitch_bend_signed_bounds=[-8192, 8191],
        length_ns=length_ns, end_tick=end_tick, counts=counts,
        channel_map=[dict(voice=v, patch=voices[v], channel=channels[v], track=track_of[v]) for v in ordered_voices],
        timing=dict(rounding="absolute_half_up", max_abs_error_ns=max(abs(r["error_ns"]) for r in timing), rows=timing),
        pitch=dict(rounding="Decimal50_half_up", max_abs_error_cents=max((abs(r["error_cents"]) for r in pitches), default=0), rows=pitches),
        velocity=dict(max_abs_imported_error_q15=max((abs(r["imported_error_q15"]) for r in velocities), default=0), rows=velocities),
        losses=losses, requires_accept_loss=True, audio_equivalent=False,
        event_trace=trace)
    return data, report
