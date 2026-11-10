#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
""" pure explicit-score validation/lowering; no native calls or publication.

The integrating operation supplies a freshly imported composer and selected
Native metadata. This module uses Piece only as a clock and expands the complete
declarations block once. Native alone validates the resulting instrument.
"""
from __future__ import annotations

from collections import defaultdict, deque
from fractions import Fraction
import decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import tempfile
from types import CodeType

SOURCE_LIMIT = 1 << 20
TL_LIMIT = 1_000_000
EVENT_LIMIT = 8192
ROW_LIMIT = 16384
QUARTER_LIMIT = 65536
LENGTH_LIMIT_NS = 600_000_000_000
RATIONAL_LIMIT = (1 << 53) - 1
I64_MIN, I64_MAX = -(1 << 63), (1 << 63) - 1
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
RATIONAL = r"-?(0|[1-9][0-9]*)/[1-9][0-9]*"
OWNED = frozenset(("timeline", "engine", "fs", "length_ns", "streams", "voice", "hash"))
BUSES = ("chorus", "delay", "reverb", "duck", "flanger")
EVENT_FIELDS = {
    "note": ("voice", "duration", "pitch", "velocity_q15"),
    "on": ("voice", "pitch", "velocity_q15"),
    "off": ("voice",), "pitch": ("voice", "pitch"),
    "ctl": ("voice", "input", "value_q15"),
    "filter": ("voice", "cutoff_millihz", "q_milli"),
    "send": ("voice", "bus", "level_q15"),
    "crush": ("voice", "bits", "rate_div"),
    "pos": ("voice", "x_mm", "y_mm", "z_mm"),
    "move": ("voice", "duration", "x_mm", "y_mm", "z_mm"),
    "yaw": ("yaw_mdeg",), "gen": ("id", "param", "value"),
    "lane": ("id", "enabled"), "mark": ("text",),
}


class ScoreError(Exception):
    """Expected source refusal. I/O and unexpected helper failures propagate."""
    def __init__(self, message, pointer="", **details):
        super().__init__(message)
        self.exit = 2
        self.pointer = pointer
        self.details = {"pointer": pointer, **details}

    def to_dict(self):
        return {"ok": False, "exit": self.exit, "error": str(self), **self.details}


class ValidatedScore:
    """Independent source snapshot and decoded shape; contains no renderer state."""
    def __init__(self, source, encoded, meter, tempo, events, declarations, extent):
        self.source, self.encoded = source, encoded
        self.source_sha256 = hashlib.sha256(encoded).hexdigest()
        self.meter, self.tempo, self.events = meter, tempo, events
        self.declarations, self.extent = declarations, extent
        self.expansion = None  #  metadata exists only for opted-in version2.


def _pointer(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _require(condition, message, pointer="", **details):
    if not condition:
        raise ScoreError(message, pointer, **details)


def source_bytes(score):
    """Strict canonical JSON, identical encoding to receipts.canonical_bytes.

    This transport preflight needs neither native metadata nor composer imports.
    All text is strict UTF8/NUL-free, including keys that shape validation rejects.
    """
    pending = [(score, "", frozenset())]
    while pending:
        value, pointer, ancestors = pending.pop()
        if type(value) in (dict, list):
            _require(id(value) not in ancestors, "score JSON must not contain cycles", pointer)
            parents = ancestors | {id(value)}
            if type(value) is dict:
                for key, item in value.items():
                    _require(type(key) is str, "score JSON keys must be strings", pointer)
                    _text(key, pointer)
                    pending.append((item, _pointer(pointer, key), parents))
            else:
                pending.extend((item, _pointer(pointer, i), parents) for i, item in enumerate(value))
        elif type(value) is str:
            _text(value, pointer)
        else:
            _require(value is None or type(value) in (bool, int, float),
                     "score values must be JSON values", pointer)
            _require(type(value) is not float or math.isfinite(value),
                     "score JSON numbers must be finite", pointer)
    # Match the shared receipt serializer exactly, while bounding the assembled
    # document before retaining it. No normalization of rational spellings occurs.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    chunks, size = [], 0
    try:
        for chunk in encoder.iterencode(score):
            data = chunk.encode("utf-8")
            size += len(data)
            _require(size <= SOURCE_LIMIT, "canonical score JSON exceeds 1MiB", "", source_bytes=size)
            chunks.append(data)
    except (ValueError, RecursionError) as error:
        raise ScoreError("score JSON cannot be encoded: " + str(error)) from error
    return b"".join(chunks)


def _text(value, pointer, *, single=False, nonempty=False):
    _require(type(value) is str, "must be a string", pointer)
    _require("\x00" not in value, "NUL is not allowed in score text", pointer)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ScoreError("score text must be valid UTF8", pointer) from error
    _require(len(encoded) <= SOURCE_LIMIT, "score text exceeds 1MiB", pointer)
    _require(not single or ("\n" not in value and "\r" not in value),
             "must be a single-line string", pointer)
    _require(not nonempty or bool(value), "must be nonempty", pointer)
    return value


def _closed(value, required, pointer, optional=()):
    _require(type(value) is dict, "must be an object", pointer)
    for key in value:
        _require(key in required or key in optional, "unknown score field", _pointer(pointer, key))
    for key in required:
        _require(key in value, "required score field is missing", _pointer(pointer, key))


def _integer(value, pointer, low=I64_MIN, high=I64_MAX):
    _require(type(value) is int, "must be an integer (not boolean)", pointer)
    _require(low <= value <= high, "integer is outside the allowed range", pointer,
             minimum=low, maximum=high)
    return value


def rational(value, pointer):
    """The one bounded decoder used for meter, tempo, onset and duration."""
    if type(value) is int:
        numerator, denominator = value, 1
    else:
        _require(type(value) is str and len(value) <= 22 and re.fullmatch(RATIONAL, value),
                 "must be an integer or a bounded numerator/denominator rational", pointer)
        numerator, denominator = (int(part) for part in value.split("/"))
    _require(abs(numerator) <= RATIONAL_LIMIT and 1 <= denominator <= 4096,
             "rational exceeds numerator or denominator bounds", pointer)
    return Fraction(numerator, denominator)


def _identifier(value, pointer):
    _text(value, pointer)
    _require(re.fullmatch(IDENTIFIER, value), "must be an identifier", pointer)
    return value


def _list(value, pointer, low, high):
    _require(type(value) is list, "must be an array", pointer)
    _require(low <= len(value) <= high, "array length is outside the allowed range", pointer,
             minimum=low, maximum=high)


def _pitch(value, pointer):
    _require(type(value) is dict and len(value) == 1, "pitch must contain exactly one route", pointer)
    key = next(iter(value))
    _require(key in ("note", "midi", "millihz"), "unknown pitch route", _pointer(pointer, key))
    if key == "note":
        _text(value[key], _pointer(pointer, key), single=True)
    else:
        _integer(value[key], _pointer(pointer, key), *( (0, 127) if key == "midi" else (I64_MIN, I64_MAX)))


def _ordinary_event(event, pointer, source, extent):
    """One validator for direct and phrase-body events; preserve version1 order."""
    _require(type(event) is dict, "event must be an object", pointer)
    kind = event.get("kind")
    _require(type(kind) is str and kind in EVENT_FIELDS, "unknown event kind", pointer + "/kind")
    _closed(event, ("kind", "at", "stream") + EVENT_FIELDS[kind], pointer)
    _identifier(event["stream"], pointer + "/stream")
    _require(event["stream"] in source["streams"], "stream is not declared", pointer + "/stream")
    decoded = dict(event)
    decoded["at"] = rational(event["at"], pointer + "/at")
    _require(0 <= decoded["at"] <= extent, "event time is outside the musical extent", pointer + "/at")
    for field in EVENT_FIELDS[kind]:
        value, address = event[field], pointer + "/" + field
        if field == "voice":
            _integer(value, address, 0, len(source["voices"]) - 1)
        elif field == "duration":
            decoded[field] = rational(value, address)
            _require(decoded[field] > 0, "duration must be positive", address)
            _require(decoded["at"] + decoded[field] <= extent, "span endpoint is outside the musical extent", address)
        elif field == "pitch":
            _pitch(value, address)
        elif field == "param":
            _identifier(value, address)
        elif field == "bus":
            _require(type(value) is str and value in BUSES, "unknown send bus", address)
        elif field == "enabled":
            _integer(value, address, 0, 1)
        elif field == "bits":
            _integer(value, address, 1, 24)
        elif field == "rate_div":
            _integer(value, address, 1, 64)
        elif field == "text":
            _text(value, address, single=True)
        else:
            _integer(value, address)
    return decoded


def document_module():
    spec = importlib.util.spec_from_file_location("score_tl_document", Path(__file__).with_name("tl_document.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_score(score):
    """Validate closed source shape and cheap bounds without any native/helper work."""
    encoded = source_bytes(score)
    score = json.loads(encoded)  # Snapshot preserves original tokens, independent of caller mutation.
    version3 = type(score) is dict and type(score.get("version")) is int and score["version"] == 3
    phrase_version = type(score) is dict and type(score.get("version")) is int and score["version"] in (2, 3)
    required = ("version", "fs", "clock", "declarations", "streams", "voices", "events")
    _closed(score, required + (("phrases",) if phrase_version else ()), "", ("name", "about", "regions", "metadata") if version3 else ("name", "about", "metadata"))
    version = 3 if version3 else 2 if phrase_version else 1
    _integer(score["version"], "/version", version, version)
    _integer(score["fs"], "/fs")
    _require(score["fs"] in (44100, 48000), "fs must be 44100 or 48000", "/fs")
    if "name" in score:
        _text(score["name"], "/name", single=True, nonempty=True)
    if "about" in score:
        _require(type(score["about"]) is list, "must be an array", "/about")
        for i, text in enumerate(score["about"]):
            _text(text, "/about/" + str(i), single=True)
    document = document_module()
    if "metadata" in score:
        try:
            document.validate_metadata(score["metadata"])
        except ValueError as error:
            raise ScoreError(str(error), "/metadata") from error
    declarations = _text(score["declarations"], "/declarations").replace("\r\n", "\n")
    _require(not any(document._recognized(line.encode("utf-8")) for line in declarations.split("\n")),
             "document headers belong in score metadata, not declarations", "/declarations")
    _require("\r" not in declarations, "declarations contain a lone CR", "/declarations")
    for name, low, high in (("streams", 1, 16), ("voices", 1, 64)):
        _list(score[name], "/" + name, low, high)
        for i, value in enumerate(score[name]):
            _identifier(value, "/" + name + "/" + str(i))
    _require(len(set(score["streams"])) == len(score["streams"]),
             "streams must be distinct", "/streams")
    clock = score["clock"]
    _closed(clock, ("bars", "meter", "tempo", "ramp", "tail_ns"), "/clock")
    bars = _integer(clock["bars"], "/clock/bars", 1, QUARTER_LIMIT)
    _integer(clock["tail_ns"], "/clock/tail_ns", 0, LENGTH_LIMIT_NS)
    _require(type(clock["ramp"]) is str and clock["ramp"] in ("step", "linear"),
             "ramp must be step or linear", "/clock/ramp")
    maps = []
    for name, field, maximum in (("meter", "quarters", bars), ("tempo", "bpm", bars + 1)):
        rows, previous = [], 0
        _list(clock[name], "/clock/" + name, 1, 128)
        for i, row in enumerate(clock[name]):
            pointer = "/clock/" + name + "/" + str(i)
            _closed(row, ("bar", field), pointer)
            bar = _integer(row["bar"], pointer + "/bar", 1, maximum)
            _require(bar > previous and (i != 0 or bar == 1),
                     "map anchors must begin at bar 1 and strictly increase", pointer + "/bar")
            value = rational(row[field], pointer + "/" + field)
            _require(value > 0, "must be a positive rational", pointer + "/" + field)
            rows.append((bar, value))
            previous = bar
        maps.append(rows)
    meter, tempo = maps
    # Meter extent is a sum of bar counts, computed before Piece allocates its
    # quarter grid. Piece's own exact extent is checked against this bound later.
    extent = sum((end - start) * quarters for (start, quarters), (end, _) in
                 zip(meter, meter[1:] + [(bars + 1, Fraction(0))]))
    _require(0 < extent <= QUARTER_LIMIT, "quarter extent must be positive and at most 65536", "/clock/meter")
    _list(score["events"], "/events", 0, EVENT_LIMIT)
    if phrase_version:
        events, expansion = (_version3_events if version3 else _phrase_events)(score, encoded, extent)
        result = ValidatedScore(score, encoded, meter, tempo, events, declarations, extent)
        result.expansion = expansion
        return result
    events, rows = [], 0
    for i, event in enumerate(score["events"]):
        pointer = "/events/" + str(i)
        decoded = _ordinary_event(event, pointer, score, extent)
        events.append(decoded)
        rows += 2 if event["kind"] == "note" else int(event["kind"] != "mark")
    _require(rows <= ROW_LIMIT, "expanded authored event rows exceed 16384", "/events")
    return ValidatedScore(score, encoded, meter, tempo, events, declarations, extent)


def _phrase_events(source, encoded, extent):
    """Bound literal expansion before native work, without another musical clock."""
    _list(source["phrases"], "/phrases", 0, EVENT_LIMIT)
    definitions, nodes = {}, len(source["events"])
    for i, definition in enumerate(source["phrases"]):
        pointer = "/phrases/" + str(i)
        _closed(definition, ("id", "events"), pointer)
        name = _identifier(definition["id"], pointer + "/id")
        _require(name not in definitions, "duplicate phrase id", pointer + "/id")
        _list(definition["events"], pointer + "/events", 1, EVENT_LIMIT)
        nodes += len(definition["events"])
        _require(nodes <= EVENT_LIMIT, "literal source nodes exceed 8192", pointer + "/events")
        bodies = [_ordinary_event(event, pointer + "/events/" + str(j), source, extent)
                  for j, event in enumerate(definition["events"])]
        definitions[name] = (i, definition["events"], bodies)
    plans, event_count, row_count, instances = [], 0, 0, 0
    for i, item in enumerate(source["events"]):
        pointer = "/events/" + str(i)
        _require(type(item) is dict, "event must be an object", pointer)
        if item.get("kind") != "phrase":
            decoded = _ordinary_event(item, pointer, source, extent)
            plan = (pointer, None, [item], [decoded], Fraction(0), 1, Fraction(0))
        else:
            _closed(item, ("kind", "phrase", "at"), pointer, ("repeat",))
            name = _identifier(item["phrase"], pointer + "/phrase")
            _require(name in definitions, "unknown phrase id", pointer + "/phrase")
            at = rational(item["at"], pointer + "/at")
            _require(0 <= at <= extent, "phrase time is outside the musical extent", pointer + "/at")
            count, every = 1, Fraction(0)
            if "repeat" in item:
                repeat = item["repeat"]
                _closed(repeat, ("count", "every"), pointer + "/repeat")
                count = _integer(repeat["count"], pointer + "/repeat/count", 2, EVENT_LIMIT)
                every = rational(repeat["every"], pointer + "/repeat/every")
                _require(every > 0, "repeat interval must be positive", pointer + "/repeat/every")
            index, body, decoded_body = definitions[name]
            plan = (pointer, index, body, decoded_body, at, count, every)
            instances += count
        body, count = plan[2], plan[5]
        event_count += len(body) * count
        row_count += sum(2 if event["kind"] == "note" else int(event["kind"] != "mark")
                         for event in body) * count
        _require(event_count <= EVENT_LIMIT, "expanded ordinary events exceed 8192", pointer)
        _require(row_count <= ROW_LIMIT, "expanded authored event rows exceed 16384", pointer)
        plans.append(plan)
    expanded_source = {key: value for key, value in source.items() if key != "phrases"}
    expanded_source.update(version=1, events=[])
    size = len(source_bytes(expanded_source))
    events, origins, origin_bytes = [], [], 2  # Canonical array brackets.
    for pointer, index, body, decoded_body, at, count, every in plans:
        for repetition in range(count):
            for j, (event, decoded) in enumerate(zip(body, decoded_body)):
                event, decoded = dict(event), dict(decoded)
                origin = dict(pointer=pointer, expanded_event_index=len(events))
                if index is not None:
                    origin.update(pointer="/phrases/" + str(index) + "/events/" + str(j),
                                  invocation_pointer=pointer, repeat_index=repetition,
                                  phrase_id=source["phrases"][index]["id"])
                    onset = at + repetition * every + decoded["at"]
                    context = {key: value for key, value in origin.items() if key != "pointer"}
                    _require(abs(onset.numerator) <= RATIONAL_LIMIT and onset.denominator <= 4096,
                             "derived onset exceeds existing rational bounds", origin["pointer"] + "/at", **context)
                    _require(0 <= onset <= extent, "event time is outside the musical extent",
                             origin["pointer"] + "/at", **context)
                    if "duration" in decoded:
                        _require(onset + decoded["duration"] <= extent,
                                 "span endpoint is outside the musical extent", origin["pointer"] + "/duration", **context)
                    decoded["at"] = onset
                    event["at"] = onset.numerator if onset.denominator == 1 else str(onset)
                size += len(source_bytes(event)) + int(bool(events))
                _require(size <= SOURCE_LIMIT, "expanded literal JSON exceeds 1MiB", origin["pointer"],
                         **{key: value for key, value in origin.items() if key != "pointer"})
                # : bound provenance separately; long identifiers disappear
                # from expanded source but recur in every retained origin/map row.
                origin_bytes += len(source_bytes(origin)) + int(bool(origins))
                _require(origin_bytes <= SOURCE_LIMIT, "event origins JSON exceeds 1MiB", origin["pointer"],
                         **{key: value for key, value in origin.items() if key != "pointer"})
                expanded_source["events"].append(event)
                events.append(decoded)
                origins.append(origin)
    expanded_bytes = source_bytes(expanded_source)
    if size != len(expanded_bytes):
        raise RuntimeError("incremental expanded source byte count differs")
    return events, dict(expanded_source=expanded_source,
                        expanded_source_sha256=hashlib.sha256(expanded_bytes).hexdigest(),
                        expanded_source_bytes=size, event_origins=origins,
                        expansion_counts=dict(phrase_definitions=len(definitions), source_nodes=nodes,
                            top_level_items=len(source["events"]), phrase_instances=instances,
                            expanded_events=event_count, authored_event_rows=row_count,
                            source_bytes=len(encoded), expanded_source_bytes=size))


def _rational_token(value, pointer):
    """Emit an exact derived token inside the existing transport domain."""
    _require(abs(value.numerator) <= RATIONAL_LIMIT and value.denominator <= 4096,
             "derived value exceeds existing rational bounds", pointer)
    return value.numerator if value.denominator == 1 else str(value)


def _v3_transform(value, pointer):
    _closed(value, (), pointer, ("time_scale", "pitch"))
    _require(bool(value), "transform must select time_scale or pitch", pointer)
    scale = rational(value.get("time_scale", 1), pointer + "/time_scale")
    _require(scale > 0, "time_scale must be positive", pointer + "/time_scale")
    pitch = value.get("pitch")
    if "pitch" in value:
        address = pointer + "/pitch"
        _require(type(pitch) is dict, "pitch transform must be an object", address)
        domain = pitch.get("domain")
        _require(domain in ("midi", "millihz"), "unknown pitch transform domain", address + "/domain")
        if domain == "midi":
            _closed(pitch, ("domain", "semitones"), address, ("invert_about",))
            _integer(pitch["semitones"], address + "/semitones", -127, 127)
            if "invert_about" in pitch:
                _integer(pitch["invert_about"], address + "/invert_about", 0, 127)
        else:
            _closed(pitch, ("domain", "ratio", "rounding"), address)
            ratio = rational(pitch["ratio"], address + "/ratio")
            _require(ratio > 0, "frequency ratio must be positive", address + "/ratio")
            _require(pitch["rounding"] == "nearest_even", "rounding must be nearest_even", address + "/rounding")
            pitch = dict(pitch, ratio=ratio)
    return scale, pitch


def _v3_item(item, pointer, source, extent):
    """Plan one body item without allocating its expanded events."""
    _require(type(item) is dict, "event must be an object", pointer)
    kind = item.get("kind")
    if kind not in ("chord", "automation"):
        decoded = _ordinary_event(item, pointer, source, extent)
        return dict(item=item, decoded=decoded, pointer=pointer, count=1,
                    rows=2 if kind == "note" else int(kind != "mark"), nodes=1)
    if kind == "chord":
        _closed(item, ("kind", "at", "stream", "duration", "notes"), pointer)
        _list(item["notes"], pointer + "/notes", 1, 64)
        voices = set()
        for j, member in enumerate(item["notes"]):
            address = pointer + "/notes/" + str(j)
            _closed(member, ("voice", "pitch", "velocity_q15"), address)
            _integer(member["voice"], address + "/voice", 0, len(source["voices"]) - 1)
            _require(member["voice"] not in voices, "chord members must use distinct voices", address + "/voice")
            voices.add(member["voice"])
            _pitch(member["pitch"], address + "/pitch")
            _integer(member["velocity_q15"], address + "/velocity_q15")
        # The common time/stream fields use the ordinary note validator, including
        # unused phrase bodies and their pre-transform whole-score extent check.
        literal = dict(kind="note", at=item["at"], stream=item["stream"], duration=item["duration"], **item["notes"][0])
        decoded = _ordinary_event(literal, pointer, source, extent)
        return dict(item=item, decoded=decoded, pointer=pointer, count=len(voices), rows=2 * len(voices),
                    nodes=1 + len(voices))
    _closed(item, ("kind", "at", "stream", "voice", "input", "points", "every", "interpolation", "rounding", "end"), pointer)
    _integer(item["input"], pointer + "/input", 0, 7)
    _list(item["points"], pointer + "/points", 2, EVENT_LIMIT)
    points, previous = [], None
    for j, point in enumerate(item["points"]):
        address = pointer + "/points/" + str(j)
        _closed(point, ("offset", "value_q15"), address)
        offset = rational(point["offset"], address + "/offset")
        _require(offset == 0 if previous is None else offset > previous,
                 "automation points must start at zero and strictly increase", address + "/offset")
        value = _integer(point["value_q15"], address + "/value_q15", -32768, 32768)
        points.append((offset, value))
        previous = offset
    every = rational(item["every"], pointer + "/every")
    _require(every > 0, "automation sample interval must be positive", pointer + "/every")
    _require(item["interpolation"] in ("hold", "linear"), "interpolation must be hold or linear", pointer + "/interpolation")
    _require(item["rounding"] == "nearest_even", "rounding must be nearest_even", pointer + "/rounding")
    _require(item["end"] in ("include", "exclude"), "end must be include or exclude", pointer + "/end")
    literal = dict(kind="ctl", at=item["at"], stream=item["stream"], voice=item["voice"], input=item["input"], value_q15=points[0][1])
    decoded = _ordinary_event(literal, pointer, source, extent)
    _require(decoded["at"] + points[-1][0] <= extent,
             "automation endpoint is outside the musical extent", pointer + "/points/" + str(len(points) - 1) + "/offset")
    count = math.ceil(points[-1][0] / every) + int(item["end"] == "include")
    return dict(item=item, decoded=decoded, pointer=pointer, points=points, every=every,
                count=count, rows=count, nodes=1 + len(points))


def _v3_literals(plan):
    """Yield only after caller has bounded all products and sample counts."""
    item, pointer = plan["item"], plan["pointer"]
    if item["kind"] == "chord":
        for j, member in enumerate(item["notes"]):
            yield (dict(kind="note", at=item["at"], stream=item["stream"], duration=item["duration"], **member),
                   dict(pointer=pointer + "/notes/" + str(j), item_pointer=pointer, member_index=j))
    elif item["kind"] == "automation":
        points, segment = plan["points"], 0
        for j in range(plan["count"]):
            offset = min(j * plan["every"], points[-1][0])
            while segment + 1 < len(points) and points[segment + 1][0] <= offset:
                segment += 1
            value = points[segment][1]
            indices = [segment]
            if segment + 1 < len(points) and item["interpolation"] == "linear" and offset != points[segment][0]:
                left, right = points[segment], points[segment + 1]
                value = round(left[1] + (right[1] - left[1]) * (offset - left[0]) / (right[0] - left[0]))
                indices.append(segment + 1)
            yield (dict(kind="ctl", at=plan["decoded"]["at"] + offset, stream=item["stream"],
                        voice=item["voice"], input=item["input"], value_q15=value),
                   dict(pointer=pointer, sample_index=j, point_indices=indices))
    else:
        yield dict(item), dict(pointer=pointer)


def _version3_events(source, encoded, extent):
    """: bounded literal transforms, chords, sampled controls and regions."""
    regions = source.get("regions", [])
    _list(regions, "/regions", 0, 128)
    region_ids = set()
    for i, region in enumerate(regions):
        pointer = "/regions/" + str(i)
        _closed(region, ("id", "start", "end"), pointer)
        name = _identifier(region["id"], pointer + "/id")
        _require(name not in region_ids, "duplicate region id", pointer + "/id")
        region_ids.add(name)
        lo, hi = rational(region["start"], pointer + "/start"), rational(region["end"], pointer + "/end")
        _require(0 <= lo < hi <= extent, "region must be a nonempty interval within the musical extent", pointer)
    _list(source["phrases"], "/phrases", 0, EVENT_LIMIT)
    definitions, nodes = {}, len(regions)
    chord_members = automation_points = 0

    def item_plan(item, pointer):
        nonlocal nodes, chord_members, automation_points
        plan = _v3_item(item, pointer, source, extent)
        nodes += plan["nodes"]
        chord_members += len(item.get("notes", [])) if item["kind"] == "chord" else 0
        automation_points += len(item.get("points", [])) if item["kind"] == "automation" else 0
        _require(nodes <= EVENT_LIMIT, "literal source nodes exceed 8192", pointer)
        return plan

    for i, definition in enumerate(source["phrases"]):
        pointer = "/phrases/" + str(i)
        _closed(definition, ("id", "events"), pointer)
        name = _identifier(definition["id"], pointer + "/id")
        _require(name not in definitions, "duplicate phrase id", pointer + "/id")
        _list(definition["events"], pointer + "/events", 1, EVENT_LIMIT)
        body = [item_plan(item, pointer + "/events/" + str(j)) for j, item in enumerate(definition["events"])]
        definitions[name] = (i, body)
    plans, event_count, row_count, instances = [], 0, 0, 0
    for i, item in enumerate(source["events"]):
        pointer = "/events/" + str(i)
        _require(type(item) is dict, "event must be an object", pointer)
        index, at, count, every, scale, pitch = None, Fraction(0), 1, Fraction(0), Fraction(1), None
        if item.get("kind") == "phrase":
            nodes += 1
            _require(nodes <= EVENT_LIMIT, "literal source nodes exceed 8192", pointer)
            _closed(item, ("kind", "phrase", "at"), pointer, ("repeat", "transform"))
            name = _identifier(item["phrase"], pointer + "/phrase")
            _require(name in definitions, "unknown phrase id", pointer + "/phrase")
            index, body = definitions[name]
            at = rational(item["at"], pointer + "/at")
            _require(0 <= at <= extent, "phrase time is outside the musical extent", pointer + "/at")
            if "repeat" in item:
                _closed(item["repeat"], ("count", "every"), pointer + "/repeat")
                count = _integer(item["repeat"]["count"], pointer + "/repeat/count", 2, EVENT_LIMIT)
                every = rational(item["repeat"]["every"], pointer + "/repeat/every")
                _require(every > 0, "repeat interval must be positive", pointer + "/repeat/every")
            if "transform" in item:
                scale, pitch = _v3_transform(item["transform"], pointer + "/transform")
            instances += count
        else:
            body = [item_plan(item, pointer)]
        event_count += sum(part["count"] for part in body) * count
        row_count += sum(part["rows"] for part in body) * count
        _require(event_count <= EVENT_LIMIT, "expanded ordinary events exceed 8192", pointer)
        _require(row_count <= ROW_LIMIT, "expanded authored event rows exceed 16384", pointer)
        plans.append((i, index, body, at, count, every, scale, pitch))
    expanded = {key: value for key, value in source.items() if key not in ("phrases", "regions")}
    expanded.update(version=1, events=[])
    size = len(source_bytes(expanded))
    events, origins, occurrences = [], [], []
    provenance_size = len(source_bytes(dict(event_origins=[], phrase_occurrences=[])))
    samples = transformed = 0

    def provenance_append(target, value, pointer, context):
        nonlocal provenance_size
        provenance_size += len(source_bytes(value)) + int(bool(target))
        _require(provenance_size <= SOURCE_LIMIT, "joint event origins and phrase occurrences JSON exceeds 1MiB", pointer, **context)
        target.append(value)

    for invocation, index, body, at, count, every, scale, pitch in plans:
        invocation_pointer = "/events/" + str(invocation)
        transform_pointer = invocation_pointer + "/transform" if "transform" in source["events"][invocation] else None
        for repetition in range(count):
            begin, finish = None, None
            for plan in body:
                for event, origin in _v3_literals(plan):
                    origin["expanded_event_index"] = len(events)
                    if index is not None:
                        origin.update(invocation_pointer=invocation_pointer, repeat_index=repetition,
                                      phrase_id=source["phrases"][index]["id"], phrase_occurrence_index=len(occurrences))
                        if transform_pointer is not None:
                            origin["transform_pointer"] = transform_pointer
                    context = {key: val for key, val in origin.items() if key != "pointer"}
                    try:
                        local = event["at"] if type(event["at"]) is Fraction else rational(event["at"], origin["pointer"] + "/at")
                        onset = at + repetition * every + scale * local
                        event["at"] = _rational_token(onset, plan["pointer"] + "/at")
                        if "duration" in event:
                            duration = scale * plan["decoded"]["duration"]
                            event["duration"] = _rational_token(duration, plan["pointer"] + "/duration")
                        if pitch is not None and "pitch" in event:
                            domain = pitch["domain"]
                            address = origin["pointer"] + "/pitch"
                            _require(domain in event["pitch"], "pitch route does not match transform domain", address)
                            value = event["pitch"][domain]
                            if domain == "midi":
                                value = (2 * pitch["invert_about"] - value if "invert_about" in pitch else value) + pitch["semitones"]
                                _integer(value, address + "/midi", 0, 127)
                            else:
                                value = round(value * pitch["ratio"])
                                _integer(value, address + "/millihz")
                            event["pitch"] = {domain: value}
                            transformed += 1
                        decoded = _ordinary_event(event, origin["pointer"], source, extent)
                    except ScoreError as error:
                        # Chord members own pitch/voice/velocity, while the
                        # shorthand owns shared placement/span fields.
                        if "item_pointer" in origin:
                            for field in ("at", "duration", "stream"):
                                if error.pointer == origin["pointer"] + "/" + field:
                                    error.pointer = origin["item_pointer"] + "/" + field
                                    error.details["pointer"] = error.pointer
                        error.details.update(context)
                        raise
                    endpoint = onset + decoded.get("duration", 0)
                    begin = onset if begin is None else min(begin, onset)
                    finish = endpoint if finish is None else max(finish, endpoint)
                    size += len(source_bytes(event)) + int(bool(events))
                    _require(size <= SOURCE_LIMIT, "expanded literal JSON exceeds 1MiB", origin["pointer"], **context)
                    provenance_append(origins, origin, origin["pointer"], context)
                    expanded["events"].append(event)
                    events.append(decoded)
                    samples += int(plan["item"]["kind"] == "automation")
            if index is not None:
                occurrence = dict(definition_index=index, invocation_index=invocation, repeat_index=repetition,
                                  start_quarter=str(begin), end_quarter=str(finish))
                if transform_pointer is not None:
                    occurrence["transform_pointer"] = transform_pointer
                provenance_append(occurrences, occurrence, invocation_pointer,
                    dict(invocation_pointer=invocation_pointer, repeat_index=repetition,
                         phrase_occurrence_index=len(occurrences), definition_pointer="/phrases/" + str(index)))
    expanded_bytes = source_bytes(expanded)
    if size != len(expanded_bytes) or provenance_size != len(source_bytes(dict(event_origins=origins, phrase_occurrences=occurrences))):
        raise RuntimeError("incremental version3 byte count differs")
    return events, dict(expanded_source=expanded, expanded_source_sha256=hashlib.sha256(expanded_bytes).hexdigest(),
        expanded_source_bytes=size, event_origins=origins, phrase_occurrences=occurrences,
        expansion_counts=dict(phrase_definitions=len(definitions), source_nodes=nodes, top_level_items=len(source["events"]),
            phrase_instances=instances, expanded_events=event_count, authored_event_rows=row_count,
            source_bytes=len(encoded), expanded_source_bytes=size, provenance_bytes=provenance_size,
            chord_members=chord_members, automation_points=automation_points, automation_samples=samples,
            transformed_pitch_events=transformed, regions=len(regions)))


def _pitch_millihz(pitch, pointer, composer):
    if "millihz" in pitch:
        return pitch["millihz"]
    if "midi" in pitch:
        number = pitch["midi"]
    else:
        try:
            number = composer.midi(pitch["note"])
        except AttributeError as error:
            # compose.midi's sole known malformed-spelling path is None.group.
            # An unrelated AttributeError remains a host/dependency failure.
            if getattr(error, "name", None) != "group" or getattr(error, "obj", object()) is not None:
                raise
            raise ScoreError("malformed note spelling", pointer + "/note") from error
        _integer(number, pointer + "/note", 0, 127)
    return composer.mhz_m(number)


def _recipe_arithmetic_line(error, composer, before, after, declarations):
    """Recognize only observed authored overflows in the supplied recipe helper.

    The old helper's try/except does not classify these numeric failures. Use
    its actual frame objects and already-parsed arguments, not another recipe
    parser, source line numbers, or a blanket ArithmeticError conversion.
    """
    frames, trace = [], error.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame)
        trace = trace.tb_next
    expand_code = composer.expand_timeline.__code__
    recipe = getattr(composer, "recipe", None)
    if recipe is None:
        return None
    recipe_code = recipe.__code__
    for index, frame in enumerate(frames[:-1]):
        if frame.f_code is not expand_code or frames[index + 1].f_code is not recipe_code:
            continue
        local, call = frame.f_locals, frames[index + 1].f_locals
        n, kw, name, words = (local.get(key) for key in ("n", "kw", "name", "words"))
        lines = declarations.split("\n")
        if not (local.get("src") == str(before) and local.get("dst") == str(after) and
                type(n) is int and 1 <= n <= len(lines) and
                type(local.get("line")) is str and local["line"].removesuffix("\n") == lines[n - 1] and
                type(words) is list and words[:3] == ["recipe", local.get("pname"), name] and
                type(name) is str and name in composer.RECIPES and call.get("name") == name and
                type(kw) is dict and type(call.get("tweaks")) is dict):
            continue
        bound_names = recipe_code.co_varnames[:recipe_code.co_argcount + recipe_code.co_kwonlyargcount]
        if call["tweaks"] != {key: value for key, value in kw.items() if key not in bound_names}:
            continue
        if any(call.get(key) != value for key, value in kw.items() if key in bound_names):
            continue
        tail = frames[index + 1:]
        # On Python versions before comprehension inlining, this exact format
        # operation has its own code object nested directly in recipe.
        formats = {code for code in recipe_code.co_consts if isinstance(code, CodeType) and
                   code.co_name == "<listcomp>" and "%s %+g" in code.co_consts}
        format_path = len(tail) == 1 or (len(tail) == 2 and tail[-1].f_code in formats)
        if type(error) is OverflowError and str(error) == "int too large to convert to float" and format_path:
            for value in (call.get("time"), *call["tweaks"].values()):
                if type(value) is int and any(value is parsed for parsed in kw.values()):
                    try:
                        float(value)
                    except OverflowError:
                        return n
        pitch = getattr(composer, "pitch_scale", None)
        if pitch is None or len(tail) != 2 or tail[-1].f_code is not pitch.__code__:
            continue
        pitch_local, side = tail[-1].f_locals, call.get("side")
        if type(side) is not str:
            continue
        parameter = side + "_cents"
        if not (parameter in kw and parameter in call["tweaks"] and
                pitch_local.get("divisions") == 1200 and
                pitch_local.get("steps") is kw[parameter] and
                call["tweaks"][parameter] is kw[parameter] and
                isinstance(pitch_local.get("exponent"), decimal.Decimal)):
            continue
        exponent = pitch_local["exponent"]
        if type(error) is decimal.Overflow and error.args == ([decimal.Overflow],) and exponent.is_finite():
            return n
        if (type(error) is OverflowError and str(error) == "cannot convert Infinity to integer" and
                exponent.is_infinite()):
            return n
    return None


def score_clock(validated, composer):
    """The existing exact musical clock, shared by lowering and source views."""
    source, clock = validated.source, validated.source["clock"]
    piece = composer.Piece("score-clock", 120, validated.meter[0][1], [], fs=source["fs"])
    piece.meter(validated.meter)
    if piece._bar_beat(clock["bars"] + 1) != validated.extent:
        raise RuntimeError("composer meter extent differs from the validated service bound")
    piece.tempo_map(validated.tempo, ramp=clock["ramp"], bars=clock["bars"])
    length = piece.t(clock["bars"] + 1) + clock["tail_ns"]
    _require(0 < length <= LENGTH_LIMIT_NS, "length must be positive and at most 600e9 ns", "/clock", length_ns=length)
    return piece, length


def lower_score(validated, *, composer, engine, statements):
    """Return expanded authored TL and exact row origins; perform no native work.

    ``statements`` is Native.metadata.statements, not a user-authored schema.
    Unexpected clock/helper failures deliberately propagate to the host wrapper.
    """
    if not isinstance(validated, ValidatedScore):
        raise TypeError("lower_score requires validate_score's ValidatedScore")
    source, clock = validated.source, validated.source["clock"]
    allowed = {row["name"] for row in statements.values()
               if row["row"] == "STATEMENT" and row["section"] != "event"} - OWNED
    allowed.add("recipe")
    for i, line in enumerate(validated.declarations.split("\n"), 1):
        words = line.partition("#")[0].split()
        if words:
            _require(words[0] in allowed, "declaration keyword is unknown or adapter-owned", "/declarations",
                     declaration_line=i, keyword=words[0])
    piece, length = score_clock(validated, composer)
    with tempfile.TemporaryDirectory(prefix="fm-score-") as directory:
        before, after = Path(directory) / "declarations.in", Path(directory) / "declarations.tl"
        before.write_text(validated.declarations, encoding="utf-8", newline="\n")
        try:
            recipe_count = composer.expand_timeline(str(before), str(after))
        except SystemExit as error:
            message = str(error.code)
            prefix = "compose.py: " + composer.shown(str(before)) + ":"
            if not message.startswith(prefix):
                raise  # Do not classify arbitrary helper exits as authored input.
            diagnostic = message[len(prefix):]
            match = re.match(r"([0-9]+): (.*)", diagnostic, re.DOTALL)
            if match is None:
                raise
            raise ScoreError("recipe expansion: " + match.group(2), "/declarations",
                             declaration_line=int(match.group(1)), helper_diagnostic="compose.py: declarations.in:" + diagnostic) from error
        except ValueError as error:
            # The existing expander converts KEY=INTEGER before its own input
            # error wrapper. Python's integer-token limit can fail there. Match
            # that specific conversion context without another recipe parser or
            # a global digit-limit change. Every unrelated ValueError propagates.
            terminal = error.__traceback__
            while terminal.tb_next is not None:
                terminal = terminal.tb_next
            local = terminal.tb_frame.f_locals
            token, key, line_number = local.get("text"), local.get("key"), local.get("n")
            known = (terminal.tb_frame.f_code is composer.expand_timeline.__code__ and
                     re.fullmatch(r"Exceeds the limit \([0-9]+ digits\) for integer string conversion: "
                                  r"value has [0-9]+ digits; use sys\.set_int_max_str_digits\(\) to increase the limit", str(error)) and
                     type(line_number) is int and 1 <= line_number <= len(validated.declarations.split("\n")) and
                     type(token) is str and "." not in token and type(key) is str and
                     local.get("eq") == "=" and local.get("word") == key + "=" + token and
                     composer.TWEAK_VALUE.fullmatch(token))
            if not known:
                raise
            raise ScoreError("recipe expansion: " + str(error), "/declarations",
                             declaration_line=line_number, helper_diagnostic=str(error),
                             helper_exception="ValueError") from error
        except (OverflowError, decimal.Overflow) as error:
            line_number = _recipe_arithmetic_line(error, composer, before, after, validated.declarations)
            if line_number is None:
                raise
            raise ScoreError("recipe expansion: " + str(error), "/declarations",
                             declaration_line=line_number, helper_diagnostic=str(error),
                             helper_exception=type(error).__name__) from error
        expanded = after.read_text(encoding="utf-8")
    lines, line_map, byte_count = [], [], 0

    def add(text, pointer, role, **context):
        nonlocal byte_count
        line = text + "\n"
        byte_count += len(line.encode("utf-8"))
        _require(byte_count <= TL_LIMIT, "authored TL exceeds 1000000 bytes", pointer, authored_tl_bytes=byte_count,
                 **(context if validated.expansion is not None else {}))
        lines.append(line)
        line_map.append({"line": len(lines), "pointer": pointer, "role": role, **context})

    add("timeline draft2", "/version", "owned")
    add("engine " + engine, "", "owned_engine")
    add("fs " + str(source["fs"]), "/fs", "owned")
    add("length_ns " + str(length), "/clock", "owned")
    add("streams " + " ".join(source["streams"]), "/streams", "owned")
    if "name" in source:
        add("# " + source["name"], "/name", "comment")
    for i, text in enumerate(source.get("about", [])):
        add("# " + text, "/about/" + str(i), "comment")
    expanded_lines = expanded.split("\n")
    if expanded_lines[-1] == "":
        expanded_lines.pop()
    for i, text in enumerate(expanded_lines, 1):
        add(text, "/declarations", "declaration", expanded_line=i)
    for i, patch in enumerate(source["voices"]):
        add("voice %d %s" % (i, patch), "/voices/" + str(i), "owned")
    event_rows = 0
    for i, event in enumerate(validated.events):
        origin = validated.expansion["event_origins"][i] if validated.expansion is not None else None
        pointer, kind = origin["pointer"] if origin is not None else "/events/" + str(i), event["kind"]
        context = {key: value for key, value in origin.items() if key != "pointer"} if origin is not None else {}
        at, stream = piece.at_beat(event["at"]), event["stream"]
        if kind == "mark":
            add("# %d %s %s" % (at, stream, event["text"]), pointer, "mark", **context)
            continue
        prefix = "%d %s " % (at, stream)
        if kind == "gen":
            command = "gen %d %s %d" % (event["id"], event["param"], event["value"])
        elif kind == "lane":
            command = "lane %d enable %d" % (event["id"], event["enabled"])
        elif kind == "yaw":
            command = "0 yaw %d" % event["yaw_mdeg"]
        else:
            native_kind = "on" if kind == "note" else kind
            args = []
            if kind in ("note", "on", "pitch"):
                try:
                    args.append(_pitch_millihz(event["pitch"], pointer + "/pitch", composer))
                except ScoreError as error:
                    error.details.update(context)
                    raise
                if kind != "pitch":
                    args.append(event["velocity_q15"])
            elif kind in ("pos", "move"):
                args = [event[axis + "_mm"] for axis in ("x", "y", "z")]
                if kind == "move":
                    args.append(piece.at_beat(event["at"] + event["duration"]) - at)
            else:
                args = [event[field] for field in EVENT_FIELDS[kind] if field != "voice"]
            command = "%d %s" % (event["voice"], native_kind)
            if args:
                command += " " + " ".join(map(str, args))
        add(prefix + command, pointer, "on" if kind == "note" else kind, **context)
        event_rows += 1
        if kind == "note":
            end = piece.at_beat(event["at"] + event["duration"])
            add("%d %s %d off" % (end, stream, event["voice"]), pointer, "off", **context)
            event_rows += 1
    text = "".join(lines)
    document = document_module()
    try:
        encoded = (document.compose_document(text.encode("utf-8"), source["metadata"])
                   if "metadata" in source else text.encode("utf-8"))
        parsed_document = document.parse_document(encoded)
    except ValueError as error:
        raise ScoreError(str(error), "/metadata" if "metadata" in source else "") from error
    header_lines = parsed_document["header"].count(b"\n")
    line_map = ([dict(line=i + 1, pointer="/metadata", role="metadata") for i in range(header_lines)] +
                [dict(row, line=row["line"] + header_lines) for row in line_map])
    text, byte_count = encoded.decode("utf-8"), len(encoded)
    result = {"authored_tl": text, "authored_tl_bytes": byte_count,
            "authored_tl_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "source_sha256": validated.source_sha256, "source_bytes": len(validated.encoded),
            "line_map": line_map, "fs": source["fs"], "length_ns": length,
            "quarter_extent": str(validated.extent),
            "counts": {"input_events": len(validated.events), "authored_event_rows": event_rows,
                       "authored_lines": len(lines) + header_lines, "expanded_declaration_lines": len(expanded_lines),
                       "recipes": recipe_count, "streams": len(source["streams"]), "voices": len(source["voices"])}}
    if validated.expansion is not None:
        result.update(validated.expansion)
    return result


def _bar_position(validated, quarter):
    """Locate a rational quarter without constructing a second tempo clock."""
    if quarter == validated.extent:
        return {"bar": validated.source["clock"]["bars"] + 1, "quarter_in_bar": "0"}
    total = Fraction(0)
    rows = validated.meter + [(validated.source["clock"]["bars"] + 1, Fraction(0))]
    for (bar, width), (end, _) in zip(rows, rows[1:]):
        extent = (end - bar) * width
        if quarter < total + extent:
            offset = quarter - total
            count = offset // width
            return {"bar": bar + count, "quarter_in_bar": str(offset - count * width)}
        total += extent
    raise RuntimeError("quarter has no validated meter position")


def _source_rows(validated, piece, composer):
    """Original tokens/origins once; scheduled rows refer to integer indices."""
    source = validated.source
    expanded = validated.expansion["expanded_source"] if validated.expansion is not None else source
    origins = (validated.expansion["event_origins"] if validated.expansion is not None else
               [{"pointer": "/events/" + str(i), "expanded_event_index": i}
                for i in range(len(validated.events))])
    ranks = {name: i for i, name in enumerate(source["streams"])}
    rows, schedule = [], []
    for i, (event, value) in enumerate(zip(validated.events, expanded["events"])):
        kind, at = event["kind"], piece.at_beat(event["at"])
        end = piece.at_beat(event["at"] + event["duration"]) if "duration" in event else None
        row = dict(event_index=i, event=value, at_ns=at, end_ns=end,
                   bar_position=_bar_position(validated, event["at"]),
                   stream_index=ranks[event["stream"]])
        if "pitch" in event:
            try:
                row["pitch_millihz"] = _pitch_millihz(event["pitch"], origins[i]["pointer"] + "/pitch", composer)
            except ScoreError as error:
                error.details.update({key: val for key, val in origins[i].items() if key != "pointer"})
                raise
        rows.append(row)
        if kind == "mark":
            continue
        payload = {key: val for key, val in value.items() if key not in ("at", "stream", "duration")}
        if "pitch" in payload:
            payload["pitch"] = {"millihz": row["pitch_millihz"]}
        payload["kind"] = "on" if kind == "note" else kind
        if kind == "move":
            payload["duration_ns"] = end - at
        schedule.append(dict(event_index=i, at_ns=at, stream_index=row["stream_index"],
                             source_order=len(schedule), payload=payload, generated_off=False))
        if kind == "note":
            schedule.append(dict(event_index=i, at_ns=end, stream_index=row["stream_index"],
                                 source_order=len(schedule), payload={"kind": "off", "voice": event["voice"]},
                                 generated_off=True))
    schedule.sort(key=lambda row: (row["at_ns"], row["stream_index"], row["source_order"]))
    return rows, origins, schedule


def _gate_advisories(schedule, voices, musical_end_ns):
    """Source gate facts only; explicit offs never acquire an invented owner."""
    active, last_on = [None] * voices, [None] * voices
    result = []

    def warn(kind, row, **context):
        result.append(dict(kind=kind, event_index=row["event_index"], at_ns=row["at_ns"],
                           stream_index=row["stream_index"], voice=row["payload"]["voice"],
                           generated_off=row["generated_off"], **context))
        if len(result) > 2 * ROW_LIMIT:
            raise RuntimeError("gate advisory bound differs from authored row bound")

    for row in schedule:
        kind = row["payload"]["kind"]
        if kind not in ("on", "off"):
            continue
        voice = row["payload"]["voice"]
        previous = active[voice]
        if kind == "on":
            if previous is not None:
                warn("retrigger", row, previous_event_index=previous["event_index"])
            if last_on[voice] is not None and last_on[voice]["at_ns"] == row["at_ns"]:
                warn("simultaneous_on", row, previous_event_index=last_on[voice]["event_index"])
            active[voice] = last_on[voice] = row
        else:
            if previous is None:
                warn("note_off_without_active_gate" if row["generated_off"] else "explicit_off_without_active_gate", row)
            elif row["generated_off"] and previous["event_index"] != row["event_index"]:
                warn("earlier_note_off_closes_retriggered_gate", row,
                     active_event_index=previous["event_index"])
            active[voice] = None
    for row in active:
        if row is not None:
            warn("held_gate_at_authored_end", dict(row, at_ns=musical_end_ns),
                 at_selection_end=True)
    return result


def _region_member(event, lo, hi, spans=False):
    if spans:
        return event["kind"] in ("note", "move") and event["at"] < hi and event["at"] + event["duration"] > lo
    return lo <= event["at"] < hi


def _region_summaries(validated, piece, rows):
    """Bounded annotation summaries; no duplicated events or inferred gates."""
    summaries = []
    for index, region in enumerate(validated.source.get("regions", [])):
        lo, hi = rational(region["start"], ""), rational(region["end"], "")
        voices = [dict(voice=i, note_onsets=0, intersecting_note_spans=0,
                       note_pitch_millihz_min=None, note_pitch_millihz_max=None,
                       first_on_ns=None, last_on_ns=None, last_note_span_end_ns=None)
                  for i in range(len(validated.source["voices"]))]
        counts = dict(onset_events=0, note_onsets=0, intersecting_note_spans=0,
                      intersecting_move_spans=0, intersecting_phrase_occurrences=0,
                      pos_onsets=0, move_onsets=0, yaw_onsets=0)
        kinds = {}
        for event, row in zip(validated.events, rows):
            onset, span = _region_member(event, lo, hi), _region_member(event, lo, hi, True)
            kind = event["kind"]
            if onset:
                counts["onset_events"] += 1
                kinds[kind] = kinds.get(kind, 0) + 1
                if kind in ("pos", "move", "yaw"):
                    counts[kind + "_onsets"] += 1
            if span:
                counts["intersecting_" + kind + "_spans"] += 1
            if "voice" not in event:
                continue
            voice = voices[event["voice"]]
            if span and kind == "note":
                voice["intersecting_note_spans"] += 1
                old = voice["last_note_span_end_ns"]
                voice["last_note_span_end_ns"] = row["end_ns"] if old is None else max(old, row["end_ns"])
            if onset and kind in ("note", "on"):
                counts["note_onsets"] += 1
                voice["note_onsets"] += 1
                for key, value, choose in (("note_pitch_millihz_min", row["pitch_millihz"], min),
                                            ("note_pitch_millihz_max", row["pitch_millihz"], max),
                                            ("first_on_ns", row["at_ns"], min), ("last_on_ns", row["at_ns"], max)):
                    voice[key] = value if voice[key] is None else choose(value, voice[key])
        for occurrence in validated.expansion["phrase_occurrences"]:
            start, end = Fraction(occurrence["start_quarter"]), Fraction(occurrence["end_quarter"])
            counts["intersecting_phrase_occurrences"] += int(lo <= start < hi if start == end else start < hi and end > lo)
        summaries.append(dict(region_index=index, pointer="/regions/" + str(index),
            start_quarter=str(lo), end_quarter=str(hi), start_ns=piece.at_beat(lo), end_ns=piece.at_beat(hi),
            start_bar=_bar_position(validated, lo), end_bar=_bar_position(validated, hi),
            counts=counts, onset_event_kinds=kinds, per_voice=voices))
    return summaries


def _inspect_validated(validated, composer, start=0, end=None):
    lo = rational(start, "/selection/start")
    hi = validated.extent if end is None else rational(end, "/selection/end")
    _require(0 <= lo <= hi <= validated.extent, "selection must be within the musical extent", "/selection")
    piece, length = score_clock(validated, composer)
    rows, origins, schedule = _source_rows(validated, piece, composer)
    start_ns, end_ns = piece.at_beat(lo), piece.at_beat(hi)
    source = validated.source
    by_voice = [dict(voice=i, onset_events=0, note_onsets=0, intersecting_note_spans=0,
                     event_kinds={}, note_pitch_millihz_min=None, note_pitch_millihz_max=None)
                for i in range(len(source["voices"]))]
    by_stream = [dict(stream_index=i, onset_events=0, note_onsets=0, intersecting_note_spans=0, event_kinds={})
                 for i in range(len(source["streams"]))]
    onsets, spans = [], []
    for i, (event, row) in enumerate(zip(validated.events, rows)):
        onset = lo <= event["at"] < hi
        span = event["kind"] == "note" and lo < hi and event["at"] < hi and event["at"] + event["duration"] > lo
        row.update(onset_in_selection=onset, note_span_intersects_selection=span)
        if onset:
            onsets.append(i)
        if span:
            spans.append(i)
        groups = [by_stream[row["stream_index"]]]
        if "voice" in event:
            groups.append(by_voice[event["voice"]])
        for group in groups:
            group["intersecting_note_spans"] += int(span)
            if onset:
                group["onset_events"] += 1
                group["event_kinds"][event["kind"]] = group["event_kinds"].get(event["kind"], 0) + 1
                if event["kind"] in ("note", "on"):
                    group["note_onsets"] += 1
                    if "voice" in group:
                        pitch = row["pitch_millihz"]
                        for key, choose in (("note_pitch_millihz_min", min), ("note_pitch_millihz_max", max)):
                            group[key] = pitch if group[key] is None else choose(group[key], pitch)
    advisories = _gate_advisories(schedule, len(source["voices"]), piece.at_beat(validated.extent))
    selected_advisories = [row for row in advisories if start_ns <= row["at_ns"] < end_ns or
                          lo < hi and hi == validated.extent and row.get("at_selection_end")]
    view = dict(source_sha256=validated.source_sha256, source_bytes=len(validated.encoded),
                source_version=source["version"], fs=source["fs"], render_length_ns=length,
                selection=dict(start_quarter=str(lo), end_quarter=str(hi), start_ns=start_ns, end_ns=end_ns,
                               start_bar=_bar_position(validated, lo), end_bar=_bar_position(validated, hi),
                               interval="half_open_quarters", advisory_interval="half_open_rounded_ns_plus_selected_authored_end"),
                counts=dict(source_events=len(source["events"]), expanded_events=len(rows), authored_rows=len(schedule),
                            selected_onset_events=len(onsets), intersecting_note_spans=len(spans),
                            all_advisories=len(advisories), selected_advisories=len(selected_advisories)),
                streams=source["streams"], voices=source["voices"], event_rows=rows, event_origins=origins,
                per_stream=by_stream, per_voice=by_voice, advisories=selected_advisories,
                declarations_sha256=hashlib.sha256(source["declarations"].encode("utf-8")).hexdigest(),
                coverage=dict(scope="authored_source_only", voice_patch_assignments="authored",
                              event_counts="authored_not_generated", native_declarations="not_validated",
                              native_bindings="unknown", defaults="unknown", generator_activity="unknown",
                              acoustic_contribution="unknown", gate_advice="not_envelope_activity_or_audibility"))
    if validated.expansion is not None:
        view.update(expanded_source_sha256=validated.expansion["expanded_source_sha256"],
                    expanded_source_bytes=validated.expansion["expanded_source_bytes"],
                    expansion_counts=validated.expansion["expansion_counts"])
    if source["version"] == 3:
        view.update(region_definitions=[dict(id=region["id"], pointer="/regions/" + str(i))
                                       for i, region in enumerate(source.get("regions", []))],
                    region_summaries=_region_summaries(validated, piece, rows),
                    phrase_definitions=[dict(id=phrase["id"], pointer="/phrases/" + str(i))
                                        for i, phrase in enumerate(source["phrases"])],
                    phrase_occurrences=validated.expansion["phrase_occurrences"])
    return view, schedule


def inspect_source(score, *, composer, start=0, end=None):
    """Inspect immutable explicit intent; perform no native work or publication."""
    return _inspect_validated(validate_score(score), composer, start, end)[0]


def _difference_validated(before, after, composer):
    left, left_schedule = _inspect_validated(before, composer)
    right, right_schedule = _inspect_validated(after, composer)
    left_keys = [source_bytes(row["event"]) for row in left["event_rows"]]
    right_keys = [source_bytes(row["event"]) for row in right["event_rows"]]
    available = defaultdict(deque)
    for i, key in enumerate(left_keys):
        available[key].append(i)
    added, pairs = [], []
    for i, key in enumerate(right_keys):
        if available[key]:
            pairs.append((available[key].popleft(), i))
        else:
            added.append(i)
    removed = sorted(i for indices in available.values() for i in indices)
    timing = []
    for i, j in pairs:
        old, new = left["event_rows"][i], right["event_rows"][j]
        fields = [key for key in ("at_ns", "end_ns", "bar_position", "stream_index", "pitch_millihz")
                  if old.get(key) != new.get(key)]
        if fields:
            timing.append(dict(before_event_index=i, after_event_index=j, changed_fields=fields))
    context = []
    absent = object()
    for key in sorted((before.source.keys() | after.source.keys()) - {"events"}):
        old, new = before.source.get(key, absent), after.source.get(key, absent)
        old_bytes = None if old is absent else source_bytes(old)
        new_bytes = None if new is absent else source_bytes(new)
        if old_bytes != new_bytes:
            context.append(dict(pointer="/" + key, before_present=old is not absent, after_present=new is not absent,
                                before_sha256=None if old_bytes is None else hashlib.sha256(old_bytes).hexdigest(),
                                after_sha256=None if new_bytes is None else hashlib.sha256(new_bytes).hexdigest()))

    def effective(schedule):
        # Stream rank has already ordered the schedule. It is not an audible
        # event payload: changing the rank at distinct times changes no order.
        return [(row["at_ns"], source_bytes(row["payload"])) for row in schedule]

    old_effective, new_effective = effective(left_schedule), effective(right_schedule)
    # Changed sequence plus the same multiset proves an ordering change. If
    # values differ too, report sequence inequality without inventing pairing.
    same_event_multiset = not added and not removed
    old_sorted, new_sorted = sorted(old_effective), sorted(new_effective)
    result = dict(before_source_sha256=before.source_sha256, after_source_sha256=after.source_sha256,
                before_source_bytes=len(before.encoded), after_source_bytes=len(after.encoded),
                same_source=before.encoded == after.encoded,
                counts=dict(before_events=len(left_keys), after_events=len(right_keys),
                            equal_value_occurrences=len(pairs), added_events=len(added), removed_events=len(removed)),
                matching="exact_event_value_fifo_occurrences_not_note_identity", context_changes=context,
                added_event_indices=added, removed_event_indices=removed, timing_changes=timing,
                same_event_multiset=same_event_multiset, source_sequence_changed=left_keys != right_keys,
                source_order_changed=same_event_multiset and left_keys != right_keys,
                effective_schedule_changed=old_effective != new_effective,
                effective_order_changed=old_sorted == new_sorted and old_effective != new_effective,
                timing_context_changed=before.source["clock"] != after.source["clock"],
                before=dict(event_rows=left["event_rows"], event_origins=left["event_origins"],
                            streams=left["streams"], voices=left["voices"], render_length_ns=left["render_length_ns"]),
                after=dict(event_rows=right["event_rows"], event_origins=right["event_origins"],
                           streams=right["streams"], voices=right["voices"], render_length_ns=right["render_length_ns"]),
                audio_equivalence="not_evaluated", native_validation="not_run")
    if before.source["version"] == 3 or after.source["version"] == 3:
        result["region_differences"] = _region_differences(before, after, left, right)
        for side, view in (("before", left), ("after", right)):
            if view["source_version"] == 3:
                result[side].update(phrase_definitions=view["phrase_definitions"],
                                    phrase_occurrences=view["phrase_occurrences"])
    return result


def _region_differences(before, after, left, right):
    """Compare each region's exact-value multisets, separately for two memberships."""
    old = {region["id"]: i for i, region in enumerate(before.source.get("regions", []))}
    new = {region["id"]: i for i, region in enumerate(after.source.get("regions", []))}

    def selected(validated, index, spans):
        if index is None:
            return {}
        region = validated.source["regions"][index]
        lo, hi = Fraction(region["start"]), Fraction(region["end"])
        events = validated.expansion["expanded_source"]["events"]
        values = defaultdict(list)
        for event, decoded in zip(events, validated.events):
            if _region_member(decoded, lo, hi, spans):
                values[source_bytes(event)].append(event)
        return values

    def delta(one, other):
        kinds, voices, total = {}, {}, 0
        for key, events in one.items():
            count = max(0, len(events) - len(other.get(key, [])))
            if not count:
                continue
            event = events[0]
            total += count
            kinds[event["kind"]] = kinds.get(event["kind"], 0) + count
            if "voice" in event:
                voice = voices.setdefault(event["voice"], dict(voice=event["voice"], count=0, event_kinds={}))
                voice["count"] += count
                voice["event_kinds"][event["kind"]] = voice["event_kinds"].get(event["kind"], 0) + count
        return dict(count=total, event_kinds=kinds, per_voice=[voices[i] for i in sorted(voices)])

    rows = []
    for name in list(old) + [name for name in new if name not in old]:
        i, j = old.get(name), new.get(name)
        row = dict(id=name, before=None if i is None else left["region_summaries"][i],
                   after=None if j is None else right["region_summaries"][j])
        for key, spans in (("onsets", False), ("note_move_span_intersections", True)):
            a, b = selected(before, i, spans), selected(after, j, spans)
            row[key] = dict(added=delta(b, a), removed=delta(a, b))
        rows.append(row)
    return rows


def source_difference(before, after, *, composer):
    """Compare source values and declared schedules, never guessed note identity."""
    return _difference_validated(validate_score(before), validate_score(after), composer)


def _edit_path(source, pointer):
    _text(pointer, "/edit/pointer")
    _require(pointer == "" or pointer.startswith("/"), "pointer must be an RFC6901 path or the empty root", "/edit/pointer")
    _require(re.search(r"~(?:[^01]|$)", pointer) is None, "pointer contains an invalid escape", "/edit/pointer")
    if pointer == "":
        return None, None, source
    target, parent, key = source, None, None
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        parent = target
        if type(parent) is dict:
            _require(token in parent, "pointer field does not exist", "/edit/pointer")
            key = token
        elif type(parent) is list:
            _require(re.fullmatch(r"0|[1-9][0-9]*", token) is not None,
                     "pointer array index must be a nonnegative canonical integer", "/edit/pointer")
            _require(len(token) <= len(str(len(parent))), "pointer array index is out of range", "/edit/pointer")
            key = int(token)
            _require(key < len(parent), "pointer array index is out of range", "/edit/pointer")
        else:
            raise ScoreError("pointer cannot descend through a scalar", "/edit/pointer")
        target = parent[key]
    return parent, key, target


def _edit_scope(pointer, before, after):
    parts = pointer.split("/")
    kind, affected = "score_structure", ""
    if len(parts) >= 3 and parts[1] in ("events", "phrases"):
        affected = "/" + parts[1] + "/" + parts[2]
        if parts[1] == "phrases":
            kind = "phrase_definition_all_uses"
            if len(parts) >= 5 and parts[3] == "events":
                affected += "/events/" + parts[4]
        else:
            index = int(parts[2])
            phrase = any(index < len(item.source["events"]) and item.source["events"][index].get("kind") == "phrase"
                         for item in (before, after))
            kind = "phrase_invocation_all_repeats" if phrase else "direct_event"
    elif (pointer.startswith("/name") or pointer.startswith("/about") or
          before.source["version"] == 3 and (pointer == "/regions" or pointer.startswith("/regions/"))):
        kind, affected = "annotation", None
    elif pointer == "/declarations":
        kind = "declarations"

    def counts(validated):
        origins = (validated.expansion["event_origins"] if validated.expansion is not None else
                   [{"pointer": "/events/" + str(i)} for i in range(len(validated.events))])
        def belongs(origin):
            return affected is not None and any(path == affected or path.startswith(affected + "/")
                for path in (origin["pointer"], origin.get("invocation_pointer", "!")))
        selected = [origin for origin in origins if belongs(origin)]
        instances = {(origin["invocation_pointer"], origin["repeat_index"]) for origin in selected
                     if "invocation_pointer" in origin}
        return dict(expanded_events=len(selected), phrase_instances=len(instances))

    return dict(kind=kind, pointer=pointer, before=counts(before), after=counts(after),
                counts_meaning="events_in_selected_source_scope_not_proven_changed_events",
                occurrence_detachment="explicit_source_replacement_only")


def revise_source(score, *, expected_source_sha256, pointer, expected, value, composer):
    """Replace one guarded existing source path, with immutable preview evidence."""
    encoded = source_bytes(score)
    _require(type(expected_source_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256),
             "expected source identity must be a lowercase SHA256", "/expected_source_sha256")
    _require(hashlib.sha256(encoded).hexdigest() == expected_source_sha256,
             "source identity differs from expected_source_sha256", "/expected_source_sha256")
    before = validate_score(score)
    revised = json.loads(encoded)
    parent, key, old = _edit_path(revised, pointer)
    old_bytes, expected_bytes, new_bytes = source_bytes(old), source_bytes(expected), source_bytes(value)
    _require(old_bytes == expected_bytes, "source value differs from expected old value", pointer)
    replacement = json.loads(new_bytes)
    if parent is None:
        revised = replacement
    else:
        parent[key] = replacement
    after = validate_score(revised)
    return dict(source=after.source, source_sha256=after.source_sha256, source_bytes=len(after.encoded),
                base_source_sha256=before.source_sha256, base_source_bytes=len(before.encoded),
                edit=dict(pointer=pointer, expected=json.loads(expected_bytes), value=replacement),
                scope=_edit_scope(pointer, before, after),
                inspection=_inspect_validated(after, composer)[0],
                difference=_difference_validated(before, after, composer))


def score_schema():
    """Closed discovery schema; semantic coupling remains validate_score/native.

    Event required fields are shared with the validator. JSON Schema describes
    individual bounds; source-byte, rational-value and clock/voice relationships
    are additionally enforced by the same operation before lowering.
    """
    def obj(properties, required=None):
        return {"type": "object", "properties": properties,
                "required": list(properties) if required is None else list(required), "additionalProperties": False}
    integer = {"type": "integer", "minimum": I64_MIN, "maximum": I64_MAX}
    # JSON Schema pattern uses a search, and '$' alone can match before a final
    # newline. The final negative lookahead makes the token boundary exact.
    end = r"$(?![\s\S])"
    identifier = {"type": "string", "pattern": "^" + IDENTIFIER + end}
    single = {"type": "string", "pattern": r"^[^\r\n\u0000]*" + end}
    rat = {"oneOf": [{"type": "integer", "minimum": -RATIONAL_LIMIT, "maximum": RATIONAL_LIMIT},
                      {"type": "string", "pattern": "^" + RATIONAL + end, "maxLength": 22}],
           "description": "Exact integer or numerator/denominator. abs(numerator)<=2^53-1; denominator<=4096."}
    positive = {"oneOf": [{"type": "integer", "minimum": 1, "maximum": RATIONAL_LIMIT},
                           {"type": "string", "pattern": r"^[1-9][0-9]*/[1-9][0-9]*" + end, "maxLength": 22}],
                "description": rat["description"] + " Must be positive."}
    onset = {"oneOf": [{"type": "integer", "minimum": 0, "maximum": RATIONAL_LIMIT},
                        {"type": "string", "pattern": r"^(0|[1-9][0-9]*|-0)/[1-9][0-9]*" + end, "maxLength": 22}],
             "description": rat["description"] + " Nonnegative quarters within the musical extent; tail is render-only."}
    pitch = {"oneOf": [obj({"note": single}), obj({"midi": {"type": "integer", "minimum": 0, "maximum": 127}}),
                         obj({"millihz": integer})]}
    fields = {"voice": {"type": "integer", "minimum": 0, "maximum": 63}, "duration": positive, "pitch": pitch,
              "param": identifier, "bus": {"type": "string", "enum": list(BUSES)},
              "enabled": {"type": "integer", "enum": [0, 1]}, "text": single,
              "bits": {"type": "integer", "minimum": 1, "maximum": 24},
              "rate_div": {"type": "integer", "minimum": 1, "maximum": 64}}
    events = []
    for kind, names in EVENT_FIELDS.items():
        properties = {"kind": {"const": kind}, "at": onset, "stream": identifier}
        properties.update((name, fields.get(name, integer)) for name in names)
        events.append(obj(properties))
    map_schema = lambda field: {"type": "array", "minItems": 1, "maxItems": 128,
                               "items": obj({"bar": {"type": "integer", "minimum": 1,
                                                       "maximum": 65536 if field == "quarters" else 65537}, field: positive})}
    legacy = obj({"version": {"type": "integer", "const": 1}, "fs": {"type": "integer", "enum": [44100, 48000]},
                "name": {**single, "minLength": 1}, "about": {"type": "array", "items": single},
                "metadata": document_module().metadata_schema(),
                "clock": obj({"bars": {"type": "integer", "minimum": 1, "maximum": QUARTER_LIMIT},
                              "meter": map_schema("quarters"), "tempo": map_schema("bpm"),
                              "ramp": {"type": "string", "enum": ["step", "linear"]},
                              "tail_ns": {"type": "integer", "minimum": 0, "maximum": LENGTH_LIMIT_NS}}),
                "declarations": {"type": "string", "description": "Native declaration block or recipe rows; no owned headers/timed events. CRLF normalized to LF; lone CR/NUL refused."},
                "streams": {"type": "array", "items": identifier, "minItems": 1, "maxItems": 16, "uniqueItems": True},
                "voices": {"type": "array", "items": identifier, "minItems": 1, "maxItems": 64},
                "events": {"type": "array", "items": {"oneOf": events}, "maxItems": EVENT_LIMIT}},
               ("version", "fs", "clock", "declarations", "streams", "voices", "events"))

    placement = obj({"kind": {"const": "phrase"}, "phrase": identifier, "at": onset,
                     "repeat": obj({"count": {"type": "integer", "minimum": 2, "maximum": EVENT_LIMIT},
                                    "every": positive})}, ("kind", "phrase", "at"))
    properties = dict(legacy["properties"], version={"type": "integer", "const": 2},
        phrases={"type": "array", "maxItems": EVENT_LIMIT,
                 "items": obj({"id": identifier, "events": {"type": "array", "minItems": 1,
                     "maxItems": EVENT_LIMIT, "items": {"oneOf": events}}})},
        events={"type": "array", "maxItems": EVENT_LIMIT, "items": {"oneOf": [*events, placement]}})
    phrase = obj(properties, [*legacy["required"], "phrases"])
    chord = obj({"kind": {"const": "chord"}, "at": onset, "stream": identifier, "duration": positive,
                 "notes": {"type": "array", "minItems": 1, "maxItems": 64,
                           "items": obj({"voice": fields["voice"], "pitch": pitch, "velocity_q15": integer})}})
    automation = obj({"kind": {"const": "automation"}, "at": onset, "stream": identifier,
        "voice": fields["voice"], "input": {"type": "integer", "minimum": 0, "maximum": 7},
        "points": {"type": "array", "minItems": 2, "maxItems": EVENT_LIMIT,
                   "items": obj({"offset": onset, "value_q15": {"type": "integer", "minimum": -32768, "maximum": 32768}})},
        "every": positive, "interpolation": {"enum": ["hold", "linear"]},
        "rounding": {"const": "nearest_even"}, "end": {"enum": ["include", "exclude"]}})
    pitch_transform = {"oneOf": [
        obj({"domain": {"const": "midi"}, "semitones": {"type": "integer", "minimum": -127, "maximum": 127},
             "invert_about": {"type": "integer", "minimum": 0, "maximum": 127}}, ("domain", "semitones")),
        obj({"domain": {"const": "millihz"}, "ratio": positive, "rounding": {"const": "nearest_even"}})]}
    transform = obj({"time_scale": positive, "pitch": pitch_transform}, ())
    transform["minProperties"] = 1
    placement3 = obj(dict(placement["properties"], transform=transform), placement["required"])
    body3 = [*events, chord, automation]
    properties3 = dict(properties, version={"type": "integer", "const": 3},
        phrases={"type": "array", "maxItems": EVENT_LIMIT,
                 "items": obj({"id": identifier, "events": {"type": "array", "minItems": 1,
                     "maxItems": EVENT_LIMIT, "items": {"oneOf": body3}}})},
        events={"type": "array", "maxItems": EVENT_LIMIT, "items": {"oneOf": [*body3, placement3]}},
        regions={"type": "array", "maxItems": 128,
                 "items": obj({"id": identifier, "start": onset, "end": onset})})
    return {"oneOf": [legacy, phrase, obj(properties3, phrase["required"])]}
