#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Stateless native synth operations. Only native canonical output is described."""
from __future__ import annotations

import argparse
import base64
import contextlib
import codecs
import csv
import datetime as dt
import fcntl
import functools
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import wave

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HARNESS = ROOT / "build/s12e10p1v1/harness"
# Version of this module's Python interface as jottune-mcp uses it. The MCP doctor reports
# whether it supports this version. Raise it when the interface changes incompatibly.
API_VERSION = 1
WORKSPACE_ROOT = Path(os.environ["JOTTUNE_MCP_WORKSPACE"]).resolve() if os.environ.get("JOTTUNE_MCP_WORKSPACE") else None
NATIVE_SECONDS = 120
MAX_RENDER_BYTES = 192 * 1024 * 1024
MAX_MEMORY_BYTES = 256 * 1024 * 1024


def workspace_path(path):
    """MCP sets an explicit root; ordinary command-line paths stay explicit."""
    resolved = Path(path).resolve()
    require(WORKSPACE_ROOT is None or resolved.is_relative_to(WORKSPACE_ROOT),
            "tool path leaves the configured workspace", 2)
    return resolved

NUMERIC = re.compile(r"[ui](8|16|32|64)\Z")
EDGE = re.compile(r"([^>]+)>([^:]+):([^:]+)(?::([^:]+))?\Z")
PARTS = {"FROM": 0, "TO": 1, "WEIGHT": 2, "KIND": 3}
LOG_COLUMNS = ("version", "take", "hash", "parent", "record_sha256",
               "parent_record_sha256", "wav_sha256", "fs", "utc", "note",
               "peak_dbfs", "rms_dbfs", "clipped", "changed", "space")


class ToolError(Exception):
    def __init__(self, message, exit=1, **details):
        super().__init__(message)
        self.exit, self.details = exit, details


class RenderReply(dict):
    """Native reply carrying private bytes and parsed rows until publication."""


class OperationReply(dict):
    """Public projection with optional complete evidence awaiting publication."""
    full_result_bytes = None


def require(condition, message, exit=1):
    if not condition:
        raise ToolError(message, exit)


def boolean(value, name):
    require(type(value) is bool, name + " must be a boolean", 2)
    return value


def integer(value, name):
    require(type(value) is int, name + " must be an integer", 2)
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clean(value):
    """JSON has no NaN or Infinity; undefined measurements remain null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()
                if not (isinstance(value, RenderReply) and k.startswith("_"))}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def tl_document_module():
    """Load the same reusable helper when fm itself was imported by path."""
    name = "fm_tl_document"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tools/tl_document.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def body_sha256(data):
    document = tl_document_module()
    try:
        return document.body_sha256(data)
    except document.DocumentError as error:
        raise ToolError("invalid retained TL artifact: " + str(error), 5) from error


def canonical_body(accepted):
    return tl_document_module().parse_document(accepted["canonical"].encode("utf-8"))["body"].decode("utf-8")


def receipts_module():
    name = "fm_operation_receipts"
    state_name = "_fm_operation_receipts_import_state"
    state = sys.modules.get(state_name)
    if state is None:
        candidate = ModuleType(state_name)
        candidate.lock = threading.RLock()
        state = sys.modules.setdefault(state_name, candidate)
    # Separate spec-loaded fm objects share the cache, so they must also share
    # its initialization lock. Keep the lock after failure for waiting retries.
    with state.lock:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, ROOT / "tools/receipts.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                if sys.modules.get(name) is module:
                    del sys.modules[name]
                raise
        return sys.modules[name]


def receipt_arguments(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: receipt_arguments(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [receipt_arguments(item) for item in value]
    return value


def operation_provenance(kwargs, operation=None):
    sources = ("tools/fm.py", "tools/tl_document.py", "tools/catalog.py", "tools/receipts.py", "tools/session.py",
               "tools/music/analyze.py", "tools/music/compose.py", "data/recipes.txt",
               "tools/music/midi2tl.py", "data/tables.txt", "src/gen/params.def")
    if operation in ("compose", "score_inspect", "score_revise"):
        sources += ("tools/score.py",)
    if operation in ("measure_section", "measure_sections", "measure_voice_sections"):
        sources += ("tools/measure.py",)
    if operation == "gen_events":
        sources += ("tools/generator.py",)
    source_hashes = {name: sha((ROOT / name).read_bytes()) for name in sources}
    path = Path(kwargs.get("harness") or DEFAULT_HARNESS).resolve()
    try:
        native = dict(path=str(path), sha256=sha(path.read_bytes()))
    except OSError as error:
        native = dict(path=str(path), sha256=None, unavailable=type(error).__name__)
    return dict(sources=source_hashes, native=native)


def public(fn=None, *, preflight=None, outcome=None, receipt_projection=None):
    if fn is None:
        return lambda operation: public(operation, preflight=preflight, outcome=outcome,
                                        receipt_projection=receipt_projection)
    @functools.wraps(fn)
    def call(*args, **kwargs):
        ticket, journal = None, None
        original_arguments = dict(kwargs) if preflight is not None else None
        directory = kwargs.pop("receipt_dir", None)
        parent = kwargs.pop("receipt_parent", None)
        purpose = kwargs.pop("receipt_purpose", "")
        def error_reply(error):
            if isinstance(error, ToolError) or journal is not None and isinstance(error, journal.ReceiptError):
                return clean(dict(version=1, ok=False, exit=error.exit,
                                  message=str(error), **error.details))
            status = 5 if isinstance(error, (OSError, subprocess.SubprocessError, ImportError)) else 1
            return dict(version=1, ok=False, exit=status, message=str(error))
        try:
            if preflight is not None:
                preflight(args, original_arguments)
            require(directory is not None or parent is None and purpose == "",
                    "receipt_parent and receipt_purpose require receipt_dir", 2)
            if directory is not None:
                journal = receipts_module()
                ticket = journal.begin(directory, fn.__name__,
                    dict(args=receipt_arguments(args), kwargs=receipt_arguments(kwargs)),
                    provenance=operation_provenance(kwargs, fn.__name__), parent=parent, purpose=purpose)
        except Exception as error:
            # Receipt setup is host storage/argument work; no operation has run.
            if not isinstance(error, (ToolError, OSError, ValueError, TypeError, ImportError)) and not (
                    journal is not None and isinstance(error, journal.ReceiptError)):
                raise
            return error_reply(error)
        full = None
        try:
            raw = fn(*args, **kwargs)
            full = getattr(raw, "full_result_bytes", None)
            result = clean(raw)
        except (ToolError, wave.Error, EOFError, UnicodeError, ValueError, TypeError, KeyError,
                OSError, subprocess.SubprocessError, ImportError) as error:
            result = error_reply(error)
        if outcome is not None:
            raw = outcome(result, kwargs, fn.__name__)
            full = getattr(raw, "full_result_bytes", None)
            result = clean(raw)
        if ticket is not None:
            try:
                metadata = journal.complete(ticket, result, full_result_bytes=full)
                if receipt_projection is not None:
                    metadata = receipt_projection(metadata, result)
                require(reply_bytes(metadata) <= 900, "receipt metadata exceeds response budget", 5)
                result["receipt"] = metadata
            except (OSError, ValueError, TypeError, ToolError, journal.ReceiptError) as error:
                failure = dict(version=1, ok=False, exit=5, message="operation completed but receipt publication failed: " + str(error),
                               operation_result=result, receipt=dict(journal.metadata(ticket), state="unknown"))
                return clean(outcome(failure, kwargs, fn.__name__)) if outcome is not None else failure
        return result
    return call


def listing(text):
    rows = []
    for line in text.splitlines():
        words = line.split()
        require(words and all("=" in w for w in words[1:]), "malformed native parameter listing")
        row = dict(w.split("=", 1) for w in words[1:])
        require(len(row) == len(words) - 1, "duplicate native listing key")
        rows.append(dict(row, row=words[0]))
    return rows


class Metadata:
    """Zip native-written tokens to native metadata; never validate authored TL."""
    def __init__(self, rows, generators):
        require(rows and rows[0].get("version") == "1", "unsupported native parameter version")
        self.rows, self.generators = rows, generators
        self.statements = {r["id"]: r for r in rows if r["row"] in ("STATEMENT", "EVENT")}
        self.params = [r for r in rows if r["row"] == "PARAM"]
        self.forms = {(r["statement"], r["id"]): r for r in rows if r["row"] == "FORM"}
        require(len(self.params) == int(rows[-1]["count"]), "native PARAM count differs")
        require(len(self.statements) == int(rows[-1]["statements"]), "native statement count differs")
        self.kinds = {r["name"]: int(r["id"]) for r in generators if r["row"] == "GEN_KIND"}
        self.gen_params = [r for r in generators if r["row"] == "GEN_PARAM"]
        self.by_statement = {s: [p for p in self.params if p["statement"] == s] for s in self.statements}
        self.engine = " ".join(k + "=" + v if k != "engine" else v
                               for k, v in rows[0].items() if k not in ("row", "version"))

    def positions(self, p, tokens):
        position = p["position"]
        if position == "last":
            return [(len(tokens) - 1, None)]
        if position == "6+slot":
            kind = self.kinds[tokens[2]]
            return [(6 + int(r["slot"][1:]), None) for r in self.gen_params
                    if re.fullmatch(r"p[0-3]", r["slot"]) and int(r["kinds"]) & (1 << (kind - 1))]
        match = re.fullmatch(r"(\d+)(\+)?(?:\.(\w+))?", position)
        require(match, "unsupported native position " + position)
        first, repeat, part = int(match[1]), match[2], match[3]
        return [(i, part) for i in (range(first, len(tokens)) if repeat else [first]) if i < len(tokens)]

    @staticmethod
    def value(tokens, slot):
        index, part = slot
        if part in PARTS:
            match = EDGE.fullmatch(tokens[index])
            require(match, "native packed token and metadata disagree")
            return match.groups()[PARTS[part]]
        if part:
            key, value = tokens[index].split("=", 1)
            require(key == part, "native key token and metadata disagree")
            return value
        return tokens[index]

    def selected(self, p, tokens, slot):
        if p["form"] == "all":
            return True
        form = self.forms[p["statement"], p["form"]]
        choices = form["values"].split("|")
        if form["match"] == "count":
            return str(len(tokens)) in choices
        selectors = self.positions(form, tokens)
        if "+." in form["position"]:
            selectors = [s for s in selectors if s[0] == slot[0]]
        require(len(selectors) == 1, "ambiguous native form selector")
        value = self.value(tokens, selectors[0])
        if form["match"] == "prefix":
            return value is not None and any(value.startswith(c) for c in choices)
        require(form["match"] in ("exact", "default_or_exact"), "unknown native form selector")
        return value in choices or (value is None and form["match"] == "default_or_exact")

    def external(self, p, tokens, slot, source_kinds):
        if p["type"] != "external" or p["unit"] == "token":
            return None
        reference = p["depends"].split(";", 1)[0].removeprefix("generator.")
        kind = self.kinds[tokens[2]] if p["statement"] == "gen" else None
        if p["id"] == "gen_fields":
            found = [r for r in self.gen_params if r["slot"] == "p" + str(slot[0] - 6)
                     and int(r["kinds"]) & (1 << (kind - 1))]
        elif p["id"] == "event_gen_value":
            kind = source_kinds[int(tokens[3])]
            found = [r for r in self.gen_params if r["name"] == tokens[4]
                     and int(r["kinds"]) & (1 << (kind - 1))]
        else:
            found = [r for r in self.gen_params if r["name"] == reference and
                     (not int(r["kinds"]) or kind is None or int(r["kinds"]) & (1 << (kind - 1)))]
        require(len(found) <= 1, "ambiguous external generator field")
        return found[0] if found else None

    def canonical(self, data):
        result = []
        source_kinds = {}
        for line_index, line in enumerate(data.decode("utf-8").splitlines()):
            tokens = line.split()
            if not tokens:
                continue
            event = tokens[0][0].isdigit()
            candidates = [s for s in self.statements.values()
                          if (s["row"] == "EVENT") == event and int(s["word_position"]) < len(tokens)
                          and tokens[int(s["word_position"])] == s["name"]]
            require(len(candidates) == 1, "canonical statement missing or ambiguous in native listing")
            statement = candidates[0]
            if statement["id"] == "gen":
                source_kinds[int(tokens[1])] = self.kinds[tokens[2]]
            fields, owners = [], set()
            raw, human = {}, {}
            for p in self.by_statement[statement["id"]]:
                for slot in self.positions(p, tokens):
                    if not self.selected(p, tokens, slot):
                        continue
                    value = self.value(tokens, slot)
                    if value is None:
                        continue
                    require(slot not in owners, "duplicate canonical token owner: " + p["id"])
                    owners.add(slot)
                    external = self.external(p, tokens, slot, source_kinds)
                    type_name = external["type"] if external else p["type"]
                    numeric = bool(NUMERIC.fullmatch(type_name) or type_name == "bool") and value != "-"
                    value = int(value) if numeric else value
                    hvalue = None if external and numeric else value / int(p["scale"]) if numeric else value
                    name = external["name"] if external and p["name"] in ("FIELDS", "VALUE") else p["name"]
                    human_name = external["name"] if external else p["human"]
                    fields.append(dict(id=p["id"], name=name, token=slot[0], component=slot[1],
                                       raw=value, human=hvalue, human_name=human_name,
                                       unit=external["unit"] if external else p["unit"],
                                       external=external, human_reason="external listing has no human scale" if external and numeric else None))
                    for target, key, item in ((raw, name, value), (human, human_name, hvalue)):
                        if key in target:
                            if not isinstance(target[key], list):
                                target[key] = [target[key]]
                            target[key].append(item)
                        else:
                            target[key] = item
            expected = set()
            for i, token in enumerate(tokens):
                if i == int(statement["word_position"]):
                    continue
                components = {part for index, part in owners if index == i and part}
                if components & PARTS.keys():
                    packed = EDGE.fullmatch(token)
                    require(packed, "canonical packed token differs")
                    expected.update((i, part) for part, n in PARTS.items() if packed.groups()[n] is not None)
                elif "=" in token:
                    expected.add((i, token.split("=", 1)[0]))
                else:
                    expected.add((i, None))
            require(owners == expected, "incomplete canonical token inventory: " + statement["id"])
            result.append(dict(line_index=line_index, statement=statement["name"], id=statement["id"], section=statement["section"],
                               once=statement["once"], raw=raw, human=human, fields=fields))
        return result


class Native:
    def __init__(self, harness=None, budget_hook=None, call_hook=None):
        self.budget_hook = budget_hook
        self.call_hook = call_hook
        self.path = Path(harness or DEFAULT_HARNESS).resolve()
        require(self.path.is_file(), "native harness missing; build it or pass --harness", 5)
        self.identity = dict(path=str(self.path), sha256=sha(self.path.read_bytes()))
        result = self.run(["--params"])
        require(result.returncode == 0, "native --params failed; regression build required", result.returncode,
                )
        gen = self.run(["--gen-params"])
        require(gen.returncode == 0, "native --gen-params failed", gen.returncode)
        self.metadata = Metadata(listing(result.stdout), listing(gen.stdout))
        self.listings = {"params": result.stdout, "gen-params": gen.stdout}
        self.identity["engine"] = self.metadata.engine

    def run(self, args):
        if args and str(args[0]) == "--play" and self.budget_hook is not None:
            self.budget_hook()
        require(sha(self.path.read_bytes()) == self.identity["sha256"], "native executable changed during operation", 5)
        command = [str(self.path), *map(str, args)]
        try:
            if WORKSPACE_ROOT is not None and args and str(args[0]) == "--play" and "--" in args:
                files = args[list(args).index("--") + 1:]
                for path in files:
                    workspace_path(path)
                if self.call_hook is not None:
                    self.call_hook(["--inspect", "--", *map(str, files)])
                admit_render(subprocess.run([str(self.path), "--inspect", "--", *map(str, files)],
                    capture_output=True, text=True, timeout=NATIVE_SECONDS))
            if self.call_hook is not None:
                self.call_hook(args)
            result = subprocess.run(command, capture_output=True, text=True, timeout=NATIVE_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ToolError("native operation exceeded the 120-second execution limit", 5) from error
        require(sha(self.path.read_bytes()) == self.identity["sha256"], "native executable changed during operation", 5)
        return result


def admit_render(inspected):
    """Refuse a render outside the MCP profile; a failed inspection leaves the refusal to the render."""
    if inspected.returncode == 0:
        rows = [line for line in inspected.stdout.splitlines() if line.startswith("INSPECT ")]
        require(len(rows) == 1, "native inspection returned no unique cost estimate", 5)
        costs = dict(item.split("=", 1) for item in rows[0].split()[1:] if "=" in item)
        lower, pcm = int(costs["render_bytes_lower_bound"]), int(costs["pcm_bytes"])
        require(lower <= MAX_RENDER_BYTES and lower + pcm * 3 <= MAX_MEMORY_BYTES,
                "render exceeds the supported 192MiB render/256MiB memory profile", 2)


def refusal(text):
    message = text.strip().splitlines()[0] if text.strip() else "native operation failed"
    match = re.fullmatch(r"(.*):(\d+): (.*)", message)
    out = dict(file=match[1] if match else None, line=int(match[2]) if match else None, message=message)
    body = match[3] if match else message
    bound = re.fullmatch(r"(\S+) (\S+)(?: (\S+))?: (\S+) '([^']*)' is not (-?\d+) to (\d+)", body)
    if bound:
        got = int(bound[5]) if re.fullmatch(r"-?\d+", bound[5]) else bound[5]
        low, high = int(bound[6]), int(bound[7])
        out.update(statement=bound[1], name=bound[2], field=bound[4], got=got, lo=low, hi=high,
                   nearest=max(low, min(high, got)) if isinstance(got, int) else None,
                   nearest_validated=False)
        if bound[3] is not None:
            out["op"] = int(bound[3]) if bound[3].isdigit() else bound[3]
    return out


def play_fields(stdout):
    lines = [line for line in stdout.splitlines() if line.startswith("PLAY ")]
    require(len(lines) == 1, "native output has no unique PLAY row")
    out = {}
    for word in lines[0].split()[1:]:
        if "=" not in word:
            continue
        key, value = word.split("=", 1)
        if key in ("hash", "expected"):
            out[key] = None if value == "-" else value
        elif value in ("yes", "NO", "no", "-"):
            out[key] = None if value == "-" else value == "yes"
        elif re.fullmatch(r"-?\d+", value):
            out[key] = int(value)
        else:
            try:
                out[key] = float(value)
            except ValueError:
                out[key] = value
    out.setdefault("channels", 1)
    metrics = [line for line in stdout.splitlines() if line.startswith("METRICS ")]
    if metrics:
        out["metrics"] = dict(word.split("=", 1) for word in metrics[0].split()[1:] if "=" in word)
        def metric(value):
            if "," in value:
                return [metric(v) for v in value.split(",")]
            if value == "-":
                return None
            return int(value) if re.fullmatch(r"\d+", value) else float(value)
        for key, value in list(out["metrics"].items()):
            out["metrics"][key] = metric(value)
            out[key] = out["metrics"][key]
    return out


def targets(items):
    out = {}
    for item in items or []:
        try:
            key, value = item.split("=", 1)
            if ".." in value:
                low, high = value.split("..", 1)
                low, high = float(low) if low else None, float(high) if high else None
            else:
                low = high = float(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ToolError("target must be KEY=VALUE or KEY=LO..HI: " + str(item), 2) from error
        require(bool(key), "target key must not be empty", 2)
        require(key not in out, "duplicate target key: " + key, 2)
        require((low is None or math.isfinite(low)) and (high is None or math.isfinite(high)), "target must be finite", 2)
        require(low is None or high is None or low <= high, "target lower bound exceeds upper", 2)
        out[key] = (low, high)
    return out


def checks(reply, requested=None):
    if isinstance(requested, (list, tuple)):
        requested = targets(requested)
    result = []
    for key, expected in (("match", True), ("block_indep", True)):
        value = reply.get(key)
        result.append(dict(name="expected_hash" if key == "match" else "block_independence",
                           status="unchecked" if value is None else "pass" if value == expected else "fail",
                           actual=value, expected=expected))
    for key, (low, high) in (requested or {}).items():
        value = reply
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        measured = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        passed = measured and (low is None or value >= low) and (high is None or value <= high)
        result.append(dict(name=key, status="unchecked" if not measured else "pass" if passed else "fail",
                           actual=value, expected=[low, high]))
    verdict = False if any(c["status"] == "fail" for c in result) else None if any(c["status"] == "unchecked" for c in result) else True
    return result, verdict


@contextlib.contextmanager
def input_files(files=None, text=None):
    """Submit text to the same native reader, retaining its logical filename."""
    require(files is None or isinstance(files, (list, tuple)), "files must be a list of paths", 2)
    require(text is None or isinstance(text, str), "text must be a string", 2)
    supplied_text = text is not None and bool(text.strip())
    require(bool(files) != supplied_text, "provide exactly one nonempty input: files or text", 2)
    if not supplied_text:
        for path in files:
            workspace_path(path)
        yield files
        return
    try:
        data = text.encode("utf-8")
    except UnicodeError as error:
        raise ToolError("text must be valid UTF-8", 2) from error
    with tempfile.TemporaryDirectory(prefix="fm-input-") as temporary:
        path = Path(temporary) / "input.tl"
        path.write_bytes(data)
        try:
            yield [path]
        except ToolError as error:
            def logical(value):
                if isinstance(value, str):
                    return value.replace(str(path), "input.tl")
                if isinstance(value, dict):
                    return {key: logical(item) for key, item in value.items()}
                if isinstance(value, (list, tuple)):
                    return [logical(item) for item in value]
                return value
            raise ToolError(logical(str(error)), error.exit, **logical(error.details)) from error


def document_header(files):
    """Validate bounded descriptive inputs before any native operation."""
    document = tl_document_module()
    try:
        headers = [document.parse_document(document.read_bounded(path))["header"] for path in files]
    except document.DocumentError as error:
        raise ToolError(str(error), 2) from error
    require(not any(headers[1:]), "TL document header must occur in the first input file", 2)
    return headers[0] if headers else b""


def read_native_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "native TL output is not a regular file", 5)
        data = stream.read(tl_document_module().MAX_BYTES + 1)
    require(len(data) <= tl_document_module().MAX_BYTES, "native TL output exceeds document limit", 5)
    return data


def verify_native_document(data, body, expected_header):
    document = tl_document_module()
    try:
        parsed, parsed_body = document.parse_document(data), document.parse_document(body)
    except document.DocumentError as error:
        raise ToolError("invalid native TL output: " + str(error), 5) from error
    require(not parsed_body["header"] and parsed["body"] == body,
            "native canonical document/body outputs disagree", 5)
    require(parsed["header"] == expected_header, "native output did not preserve the TL header", 5)
    return parsed


def native_render(native, files, want_wav=False):
    require(files, "render needs at least one input", 2)
    document = tl_document_module()
    expected_header = document_header(files)
    with tempfile.TemporaryDirectory(prefix="fm-") as temporary:
        directory = Path(temporary)
        record, body_path, wav = directory / "canonical.tl", directory / "body.tl", directory / "render.wav"
        args = ["--play", "--metrics", "--merged", record, "--merged-body", body_path]
        if want_wav:
            args += ["--wav", wav]
        result = native.run([*args, "--", *files])
        if result.returncode not in (0, 3):
            raise ToolError("native render failed", result.returncode, refusals=[refusal(result.stderr)],
                            native=native.identity, stdout=result.stdout, stderr=result.stderr)
        data, body = read_native_document(record), read_native_document(body_path)
        parsed_document = verify_native_document(data, body, expected_header)
        rows = native.metadata.canonical(body)
        parsed = play_fields(result.stdout)
        reply = RenderReply(version=1, ok=True, exit=result.returncode, native=native.identity,
                            **parsed, record=None, wav=None,
                            record_sha256=sha(body), document_sha256=sha(data), metadata=parsed_document["metadata"],
                            tl_version=parsed_document["tl_version"], engine_version=parsed_document["engine_version"],
                            wav_sha256=None, canonical=data.decode("utf-8"))
        reply["_play"] = parsed
        reply["_record_bytes"], reply["_body_bytes"], reply["_rows"] = data, body, rows
        if want_wav:
            reply["_wav_bytes"] = wav.read_bytes()
            reply["wav_sha256"] = sha(reply["_wav_bytes"])
        reply["checks"], reply["checks_pass"] = checks(reply)
        return reply


@contextlib.contextmanager
def locked(directory, create=True):
    directory = workspace_path(directory)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    with workspace_path(directory / ".lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield directory


def atomic(path, data):
    path = workspace_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def artifact(path, data):
    if path.exists():
        require(path.read_bytes() == data, "existing artifact is corrupt: " + str(path), 5)
    else:
        atomic(path, data)


def source_artifact_bytes(directory, digest, filename):
    """Project one frozen source from an exact, recognized receipt document."""
    require(isinstance(filename, str) and re.fullmatch(r"[0-9a-f]{64}\.json", filename),
            "score filename must be its parent receipt SHA256 plus .json", 2)
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "score digest must be a lowercase SHA256", 2)
    journal = receipts_module()
    try:
        workspace_path(Path(directory) / "receipts" / filename)
        document = journal._document(Path(directory), filename[:-5])
    except journal.ReceiptError as error:
        raise ToolError(str(error), error.exit, **error.details) from error
    require(type(document) is dict and type(document.get("version")) is int
            and document["version"] == 1, "unsupported score parent document", 5)
    if document.get("kind") == "operation_request":
        require(document.get("operation") == "compose", "score parent request must be compose", 5)
        request = document.get("request")
        require(type(request) is dict and set(request) == {"args", "kwargs"},
                "malformed score parent request", 5)
        args, kwargs = request["args"], request["kwargs"]
        require(type(args) is list and type(kwargs) is dict and len(args) <= 1
                and (len(args) == 1) != ("score" in kwargs) and kwargs.get("source_ref") is None,
                "score parent request needs exactly one inline source", 5)
        source = args[0] if args else kwargs["score"]
    else:
        require(document.get("kind") is None and document.get("operation")
                in ("compose", "score_inspect", "score_revise")
                and "source" in document and document.get("source_sha256") == digest,
                "unsupported score parent result or source identity", 5)
        source = document["source"]
    module = score_module()
    try:
        data = module.source_bytes(source)
    except module.ScoreError as error:
        raise ToolError("retained source cannot be encoded: " + str(error), 5) from error
    require(sha(data) == digest, "retained score SHA256 differs", 5)
    return data


TEXT_ARTIFACT_KINDS = ("authored_tl", "generator_manifest")
TEXT_PARENT_LIMIT = 64 * 1048576
TEXT_SELECTED_LIMIT = 16 * 1048576


def text_artifact_selection(document, kind):
    """Recognize retained producer fields; never parse TL or generated events."""
    def identity(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    def count(value):
        return type(value) is int and value >= 0
    require(type(document) is dict and type(document.get("version")) is int
            and document["version"] == 1 and "kind" not in document and "receipt" not in document,
            "unsupported retained text parent", 5)
    if kind == "authored_tl":
        require(document.get("operation") == "compose" and type(document.get("ok")) is bool
                and type(document.get("exit")) is int and isinstance(document.get("authored_tl"), str)
                and identity(document.get("authored_tl_sha256")) and count(document.get("authored_tl_bytes")),
                "parent lacks a complete authored TL identity", 5)
        data = document["authored_tl"].encode("utf-8")
        require(len(data) <= TEXT_SELECTED_LIMIT, "selected text exceeds 16MiB", 2)
        require(len(data) == document["authored_tl_bytes"] and sha(data) == document["authored_tl_sha256"],
                "retained authored TL identity differs", 5)
        return data
    require("operation" not in document and document.get("ok") is True
            and type(document.get("exit")) is int and document["exit"] == 0
            and "failure_stage" in document and document["failure_stage"] is None
            and document.get("manifest_complete") is True
            and type(document.get("manifest_version")) is int and document["manifest_version"] == 1
            and identity(document.get("manifest_sha256")) and count(document.get("manifest_bytes"))
            and count(document.get("event_count")) and document["event_count"] <= 2**64 - 1
            and all(type(document.get(key)) is int and 1 <= document[key] <= 2**32 - 1 for key in ("fs", "ctl")),
            "parent lacks a complete generator manifest identity", 5)
    processes = document.get("processes")
    require(type(processes) is list and all(type(row) is dict and isinstance(row.get("operation"), str)
                                          for row in processes), "malformed retained process list", 5)
    selected = [row for row in processes if row["operation"] == "--gen-events"]
    require(len(selected) == 1, "parent needs exactly one generator manifest process", 5)
    process = selected[0]
    require(type(process.get("native_returncode")) is int and process["native_returncode"] == 0
            and "cancellation" in process and process["cancellation"] is None,
            "retained generator process is not complete success", 5)
    for name in ("stdout", "stderr"):
        capture = process.get(name)
        require(type(capture) is dict and capture.get("encoding") == "utf8"
                and isinstance(capture.get("text"), str) and "base64" in capture and capture["base64"] is None
                and capture.get("complete") is True and identity(capture.get("sha256"))
                and identity(capture.get("captured_sha256")) and count(capture.get("bytes")),
                "retained generator capture is incomplete or malformed", 5)
    captures = {}
    for name in ("stdout", "stderr"):
        capture = process[name]
        data = capture["text"].encode("utf-8")
        require(len(data) <= (TEXT_SELECTED_LIMIT if name == "stdout" else 65536),
                "retained generator capture exceeds byte cap", 2 if name == "stdout" else 5)
        require(len(data) == capture["bytes"] and sha(data) == capture["sha256"] == capture["captured_sha256"],
                "retained generator capture identity differs", 5)
        captures[name] = data
    data = captures["stdout"]
    require(len(data) == document["manifest_bytes"] and sha(data) == document["manifest_sha256"],
            "retained generator manifest identity differs", 5)
    return data


def text_artifact_page(directory, kind, digest, offset, length, filename, encoding):
    """: capped same-open parent verification, exact selected byte page."""
    known = dict(kind=kind)
    try:
        require(encoding in ("base64", "utf8"), "artifact encoding must be base64 or utf8", 2)
        require(isinstance(directory, (str, Path)), "directory must be a nonempty UTF8 path", 2)
        directory = str(directory)
        require(directory and "\0" not in directory, "directory must be a nonempty UTF8 path", 2)
        try:
            directory.encode("utf-8")
        except UnicodeError as error:
            raise ToolError("directory must be valid UTF8", 2) from error
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                "digest must be a lowercase SHA256", 2)
        require(isinstance(filename, str) and re.fullmatch(r"[0-9a-f]{64}\.json", filename),
                "text view filename must be its parent receipt SHA256 plus .json", 2)
        require(type(offset) is int and offset >= 0, "offset must be a nonnegative integer", 2)
        require(type(length) is int and 1 <= length <= 8192, "text view length must be from1 through8192", 2)
        fd = os.open(workspace_path(Path(directory) / "receipts" / filename), os.O_RDONLY | os.O_NONBLOCK)
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), "text parent must be a regular file", 5)
            require(info.st_size <= TEXT_PARENT_LIMIT, "text parent exceeds 64MiB", 2)
            encoded = bytearray()
            while len(encoded) <= TEXT_PARENT_LIMIT:
                block = stream.read(min(65536, TEXT_PARENT_LIMIT + 1 - len(encoded)))
                if not block:
                    break
                encoded.extend(block)
            require(len(encoded) <= TEXT_PARENT_LIMIT, "text parent exceeds 64MiB", 2)
        require(sha(encoded) == filename[:-5], "text parent receipt SHA256 differs", 5)
        known.update(parent_sha256=filename[:-5], parent_bytes=len(encoded))
        journal = receipts_module()
        try:
            document = journal._json(bytes(encoded))
        except (journal.ReceiptError, ValueError, UnicodeError, TypeError, RecursionError) as error:
            raise ToolError("text parent is not strict canonical receipt JSON", 5) from error
        data = text_artifact_selection(document, kind)
        known.update(sha256=sha(data), size=len(data))
        require(known["sha256"] == digest, "selected text SHA256 differs", 5)
        require(offset <= len(data), "offset exceeds artifact size", 2)
        reply = dict(version=1, ok=True, exit=0, **known, mime_type="text/plain", offset=offset,
                     retrieval_work="retained_parent_only")
        if encoding == "utf8":
            reply = bounded_text_page(data, dict(reply, encoding="utf8", _receipt_reserve=" " * 1024),
                                      length, "artifact")
            reply.pop("_receipt_reserve")
            return reply
        chunk = data[offset:offset + length]
        return dict(reply, length=len(chunk), eof=offset + len(chunk) == len(data),
                    data_base64=base64.b64encode(chunk).decode("ascii"))
    except (ToolError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, ImportError) as error:
        message = str(error).encode("utf-8", errors="backslashreplace")[:1024].decode("utf-8", errors="ignore")
        return dict(version=1, ok=False, exit=error.exit if isinstance(error, ToolError) else 5,
                    message=message, **known)


def text_artifact_outcome(result, arguments, operation):
    """Only new typed views change their failure envelope; old kinds are exact."""
    inner = result.get("operation_result")
    if (isinstance(inner, dict) and inner.get("kind") in TEXT_ARTIFACT_KINDS
            and result.get("receipt", {}).get("state") == "unknown"):
        result = dict(result, operation_result=dict(inner))
        try:
            result.get("message", "").encode("utf-8")
        except UnicodeError:
            result["message"] = None
        if reply_bytes(result) > 15360:
            result["message"] = None
        if reply_bytes(result) > 15360:
            inner = result["operation_result"]
            removed = [key for key in ("text", "data_base64") if key in inner]
            for key in removed:
                inner.pop(key)
            inner["omitted"] = sorted(set(inner.get("omitted", [])) | set(removed))
        return result
    return result


def text_receipt_metadata(metadata, result):
    """Optional text readers use known producer facts after receipt completion."""
    def identity(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    def count(value):
        return type(value) is int and value >= 0
    if result.get("operation") == "compose":
        metadata = source_receipt_metadata(metadata, result)
        kind, digest, size = "authored_tl", result.get("authored_tl_sha256"), result.get("authored_tl_bytes")
        complete = identity(digest) and count(size)
    else:
        kind, digest, size = "generator_manifest", result.get("manifest_sha256"), result.get("manifest_bytes")
        rows = result.get("captures", [])
        selected = [row for row in rows if isinstance(row, dict) and row.get("operation") == "--gen-events"]
        complete = (result.get("manifest_complete") is True and identity(digest) and count(size)
                    and len(selected) == 1)
        if complete:
            row = selected[0]
            complete = (type(row.get("native_returncode")) is int and row["native_returncode"] == 0
                        and row.get("cancellation") is None
                        and row.get("stdout_complete") is True and row.get("stderr_complete") is True
                        and row.get("stdout_sha256") == row.get("captured_stdout_sha256") == digest
                        and row.get("stdout_bytes") == size
                        and identity(row.get("stderr_sha256"))
                        and row["stderr_sha256"] == row.get("captured_stderr_sha256")
                        and count(row.get("stderr_bytes")) and row["stderr_bytes"] <= 65536)
    parent, parent_size = metadata.get("full_result_sha256"), result.get("full_result_bytes")
    if (metadata.get("state") != "complete" or not complete or not identity(parent)
            or result.get("full_result_sha256") != parent or not count(parent_size)):
        return metadata
    omitted = dict(metadata, artifact_views_omitted=[kind])
    if reply_bytes(omitted) > 900:
        return metadata
    if size > TEXT_SELECTED_LIMIT or parent_size > TEXT_PARENT_LIMIT:
        return omitted
    candidate = dict(metadata, text_artifact=dict(kind=kind, digest=digest, filename=parent + ".json", size=size))
    # Reserve space for the explicit omission marker before deciding whether
    # this optional convenience fits. Never make publication fail for it.
    return candidate if reply_bytes(dict(candidate, artifact_views_omitted=[kind])) <= 900 else omitted


@public(outcome=text_artifact_outcome)
def artifact_read(directory, kind, digest, offset=0, length=8192, *, filename=None, encoding="base64"):
    """Return a bounded chunk only after verifying the complete retained file."""
    if kind in TEXT_ARTIFACT_KINDS:
        return text_artifact_page(directory, kind, digest, offset, length, filename, encoding)
    require(kind in ("record", "wav", "png", "midi", "sfz", "zip", "receipt", "take_log", "session_manifest", "score"),
            "kind must be record, wav, png, midi, sfz, zip, receipt, take_log, session_manifest or score", 2)
    require(encoding in ("base64", "utf8"), "artifact encoding must be base64 or utf8", 2)
    require(encoding != "utf8" or kind in ("record", "sfz", "receipt", "take_log", "session_manifest", "score"),
            "utf8 encoding requires record, sfz, receipt, take_log, session_manifest or score", 2)
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "digest must be a lowercase SHA256", 2)
    integer(offset, "offset")
    integer(length, "length")
    require(offset >= 0, "offset must be nonnegative", 2)
    require(1 <= length <= 1048576, "length must be from 1 through 1048576", 2)
    require(encoding != "utf8" or length <= 8192, "utf8 length must be from 1 through8192", 2)
    if kind == "score":
        data = source_artifact_bytes(directory, digest, filename)
        require(offset <= len(data), "offset exceeds artifact size", 2)
        reply = dict(version=1, ok=True, exit=0, kind=kind, sha256=digest,
                     mime_type="application/json", size=len(data), offset=offset)
        if encoding == "utf8":
            return bounded_text_page(data, dict(reply, encoding="utf8"), length, "artifact")
        chunk = data[offset:offset + length]
        return dict(reply, length=len(chunk), eof=offset + len(chunk) == len(data),
                    data_base64=base64.b64encode(chunk).decode("ascii"))
    if kind in ("png", "midi", "sfz", "zip") or filename is not None and kind in ("record", "wav"):
        suffix, mime = {"png": (".png", "image/png"), "record": (".tl", "text/plain"),
                        "wav": (".wav", "audio/wav"), "midi": (".mid", "audio/midi"),
                        "sfz": (".sfz", "text/plain"), "zip": (".zip", "application/zip")}[kind]
        require(isinstance(filename, str) and filename and not any(c in filename for c in ("/", "\\", "\x00"))
                and not Path(filename).is_absolute() and Path(filename).name == filename
                and Path(filename).suffix.lower() == suffix,
                kind + " filename must be one " + suffix + " basename without separators or traversal", 2)
        path = Path(directory) / filename
    else:
        require(filename is None, "filename applies only to PNG, record, WAV, MIDI, SFZ or ZIP artifacts", 2)
        folder, suffix, mime = (("records", ".tl", "text/plain") if kind == "record"
                                else ("receipts", ".json", "application/json") if kind == "receipt"
                                else ("sounds", ".wav", "audio/wav"))
        path = Path(directory) / "log.tsv" if kind == "take_log" else Path(directory) / folder / (digest + suffix)
        if kind == "take_log":
            mime = "text/tab-separated-values"
        elif kind == "session_manifest":
            path, mime = Path(directory) / "manifest.json", "application/json"
    whole = hashlib.sha256()
    record_chunks = [] if kind == "record" else None
    size, chunks, signature = 0, [], b""
    decoder = codecs.getincrementaldecoder("utf-8")("strict") if encoding == "utf8" else None
    invalid_utf8 = False
    # A page boundary needs only lookahead; all other bytes are hashed and
    # validated incrementally from this same stream, never reopened or cached.
    retain_end = offset + length + (4 if decoder is not None else 0)
    with workspace_path(path).open("rb") as stream:
        while True:
            block = stream.read(65536)
            if not block:
                break
            if size == 0:
                signature = block[:8]
            whole.update(block)
            if record_chunks is not None:
                require(size + len(block) <= tl_document_module().MAX_BYTES, "retained TL exceeds document limit", 5)
                record_chunks.append(block)
            if decoder is not None and not invalid_utf8:
                try:
                    decoder.decode(block, final=False)
                except UnicodeError:
                    invalid_utf8 = True
            begin, end = max(offset, size), min(retain_end, size + len(block))
            if begin < end:
                chunks.append(block[begin - size:end - size])
            size += len(block)
    require(offset <= size, "offset exceeds artifact size", 2)
    require(whole.hexdigest() == digest, "retained artifact SHA256 differs", 5)
    if record_chunks is not None:
        body_sha256(b"".join(record_chunks))  # Validate framing/metadata; transport identity covers the complete file.
    require(kind != "png" or signature == b"\x89PNG\r\n\x1a\n", "retained artifact is not a PNG", 5)
    data = b"".join(chunks)
    if decoder is not None:
        if not invalid_utf8:
            try:
                decoder.decode(b"", final=True)
            except UnicodeError:
                invalid_utf8 = True
        require(not invalid_utf8, "retained text artifact is not valid UTF8", 5)
        return bounded_text_page(data, dict(version=1, ok=True, exit=0, kind=kind,
                                 sha256=digest, mime_type=mime, size=size, offset=offset,
                                 encoding="utf8"), length, "artifact", data_offset=offset,
                                 total_size=size)
    return dict(version=1, ok=True, exit=0, kind=kind, sha256=digest, mime_type=mime,
                size=size, offset=offset, length=len(data), eof=offset + len(data) == size,
                data_base64=base64.b64encode(data).decode("ascii"))


def read_log(directory, verify=True):
    path = workspace_path(directory / "log.tsv")
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        csv.field_size_limit(sys.maxsize)
        reader = csv.DictReader(stream, delimiter="\t")
        require(tuple(reader.fieldnames or []) == LOG_COLUMNS, "unsupported or corrupt take log", 5)
        rows = list(reader)
    for index, row in enumerate(rows, 1):
        require(row.get("version") == "1" and row.get("take") == str(index) and None not in row,
                "corrupt take log row", 5)
        require(all(isinstance(row.get(k), str) for k in LOG_COLUMNS), "short take log row", 5)
        require(re.fullmatch(r"[0-9a-f]{16}", row["hash"]) and row["fs"].isdigit() and int(row["fs"]) > 0,
                "invalid logged audio identity", 5)
        if row["parent_record_sha256"] == "-":
            require(row["parent"] == "-", "parent hash without parent record", 5)
        else:
            parents = [r for r in rows[:index - 1] if r["record_sha256"] == row["parent_record_sha256"]]
            require(parents and all(r["hash"] == row["parent"] for r in parents), "invalid parent lineage", 5)
        for field, folder, suffix in (("record_sha256", "records", ".tl"), ("wav_sha256", "sounds", ".wav")):
            digest = row[field]
            if field == "wav_sha256" and digest == "-":
                continue
            require(re.fullmatch(r"[0-9a-f]{64}", digest), "invalid take artifact identity", 5)
            if verify:
                path = workspace_path(directory / folder / (digest + suffix))
                try:
                    data = tl_document_module().read_bounded(path) if field == "record_sha256" else path.read_bytes()
                except tl_document_module().DocumentError as error:
                    raise ToolError("invalid retained TL artifact: " + str(error), 5) from error
                actual = body_sha256(data) if field == "record_sha256" else sha(data)
                require(actual == digest, "take artifact digest mismatch: " + digest, 5)
    return rows


def row_key(row):
    raw = row["raw"]
    return (row["id"], *(json.dumps(raw[k], sort_keys=True) for k in
            ("PATCH", "BANK", "OP", "SLOT", "INDEX", "ID", "SYMBOL", "T_NS", "STREAM", "VOICE") if k in raw))


def record_diff(a, b):
    def keyed(rows):
        result, counts = {}, {}
        for row in rows:
            if row["id"] == "header_hash":
                continue
            key = row_key(row)
            ordinal = counts.get(key, 0)
            counts[key] = ordinal + 1
            result[key + (ordinal,)] = row
        return result
    left, right = keyed(a), keyed(b)
    changed = []
    for key in sorted(left.keys() & right.keys(), key=repr):
        x, y = left[key], right[key]
        for field in sorted(x["raw"].keys() | y["raw"].keys()):
            av, bv = x["raw"].get(field), y["raw"].get(field)
            if av != bv:
                changed.append(dict(statement=x["statement"], key=list(key), field=field, a=av, b=bv))
    return dict(added=[right[k] for k in sorted(right.keys() - left.keys(), key=repr)],
                removed=[left[k] for k in sorted(left.keys() - right.keys(), key=repr)], changed=changed)


def retain(reply, native, out=None, log=None, parent_record=None, note_text=""):
    require(out is not None or log is not None, "render requires --out DIR or --log DIR", 2)
    require(parent_record is None or log is not None, "--parent-record requires --log", 2)
    destinations = list(dict.fromkeys(Path(p).resolve() for p in (out, log) if p is not None))
    for destination in destinations:
        with locked(destination):
            rows = read_log(destination) if log is not None and destination == Path(log).resolve() else []
            parent = None
            difference = None
            if parent_record is not None and log is not None and destination == Path(log).resolve():
                found = [r for r in rows if r["record_sha256"] == parent_record]
                require(found, "parent record is not a take in this log", 2)
                parent = found[-1]
                # Native canonicalize the exact parent too; stored bytes are never
                # treated as authored-input syntax by the Python mapper.
                previous = native_render(native, [destination / "records" / (parent_record + ".tl")])
                require(previous["record_sha256"] == parent_record, "parent canonical identity drift", 5)
                require(previous["hash"] == parent["hash"] and str(previous["fs"]) == parent["fs"],
                        "parent log identity differs from native output", 5)
                difference = record_diff(previous["_rows"], reply["_rows"])
            record = destination / "records" / (reply["record_sha256"] + ".tl")
            artifact(record, reply["_record_bytes"])
            wav = None
            if "_wav_bytes" in reply:
                wav = destination / "sounds" / (reply["wav_sha256"] + ".wav")
                artifact(wav, reply["_wav_bytes"])
            reply["record"], reply["wav"] = str(record), str(wav) if wav else None
            if log is None or destination != Path(log).resolve():
                continue
            space = next((r["raw"].get("MODE") for r in reply["_rows"] if r["id"] == "header_space"), "off")
            item = dict(version="1", take=str(len(rows) + 1), hash=reply["hash"], parent=parent["hash"] if parent else "-",
                        record_sha256=reply["record_sha256"], parent_record_sha256=parent_record or "-",
                        wav_sha256=reply["wav_sha256"] or "-", fs=str(reply["fs"]),
                        utc=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"), note=note_text,
                        peak_dbfs=str(reply["peak_dbfs"]), rms_dbfs=str(reply["rms_dbfs"]), clipped=str(reply["clipped"]),
                        changed=json.dumps(difference, separators=(",", ":")) if difference is not None else "-", space=space)
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, fieldnames=LOG_COLUMNS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows([*rows, item])
            atomic(destination / "log.tsv", text.getvalue().encode("utf-8"))
            reply.update(take=int(item["take"]), parent=item["parent"] if parent else None,
                         parent_record_sha256=parent_record, log=str(destination / "log.tsv"))
    return reply


@public
def params(*, harness=None):
    native = Native(harness)
    return dict(version=1, ok=True, exit=0, native=native.identity, grammar=1,
                statements=list(native.metadata.statements.values()), parameters=native.metadata.params,
                forms=list(native.metadata.forms.values()), generators=native.metadata.generators)


def generator_module():
    name = "fm_native_generator"
    if name not in sys.modules:
        try:
            spec = importlib.util.spec_from_file_location(name, ROOT / "tools/generator.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as error:
            raise ToolError("generator adapter could not be loaded: " + str(error), 5) from error
        sys.modules[name] = module
    return sys.modules[name]


def gen_options(positional, arguments):
    return generator_module().preflight(SimpleNamespace(**globals()), positional, arguments)


def gen_outcome(full, arguments, operation):
    return generator_module().outcome(SimpleNamespace(**globals()), full, arguments, operation)


@public(preflight=gen_options, outcome=gen_outcome, receipt_projection=text_receipt_metadata)
def gen_events(files=None, *, text=None, harness=None, offset=0, length=4096,
               expected_manifest_sha256=None, detail="page", start_ns=None, end_ns=None):
    """Inspect exact native generated events or summarize emitted lanes without PCM."""
    return generator_module().execute(SimpleNamespace(**globals()), files, text=text, harness=harness)


REFERENCE_SPECS = {
    "instrument": dict(
        default_section="Start through MCP", fixed_sections=["index", "all"],
        named_selectors=["heading_title", "heading_slug", "heading_line"],
        item_operation=dict(tool="reference", argument="section"),
        examples=[dict(tool="reference", arguments=dict(source="instrument", section="index")),
                  dict(tool="reference", arguments=dict(source="instrument", section="Complete MCP text example"))],
        normal_work=dict(native_children=0, python_children=0),
        notes="Select an exact heading title, generated slug or complete heading line. "
              "Unknown or ambiguous selections refuse; use index to discover current headings."),
    "params": dict(
        default_section="index", fixed_sections=["index", "all"],
        named_selectors=["prefixed_statement_id", "statement_name", "statement_id"],
        item_operation=dict(tool="reference", argument="section"),
        examples=[dict(tool="reference", arguments=dict(source="params", section=section))
                  for section in ("index", "send", "event_send", "id:event_send")],
        normal_work=dict(native_children=2, python_children=0),
        notes="Exact matching: id: selects a statement ID directly. Otherwise public names "
              "take precedence over bare statement IDs; one public name may select multiple forms. "
              "Statement IDs are not numeric parameter IDs. Unknown selections refuse."),
    "gen-params": dict(
        default_section="index", fixed_sections=["index", "all"],
        named_selectors=["generator_name", "generator_id"],
        item_operation=dict(tool="reference", argument="section"),
        examples=[dict(tool="reference", arguments=dict(source="gen-params", section=section))
                  for section in ("index", "euclid", "7")],
        normal_work=dict(native_children=2, python_children=0),
        notes="Select an exact generator name or emitted decimal-ID string: 7 selects euclid, "
              "but 07 is not an alias. Unknown or ambiguous selections refuse."),
    "recipes": dict(
        default_section="index", fixed_sections=["index", "all"], named_selectors=[],
        item_operation=dict(tool="load_patch", argument="name"),
        examples=[dict(tool="reference", arguments=dict(source="recipes", section="index")),
                  dict(tool="load_patch", arguments=dict(name="bell", detail="page", length=4096))],
        normal_work=dict(native_children=0, python_children=0),
        notes="Only index/all are reference sections; recipe names refuse here. Use load_patch "
              "for an individual recipe's expanded text. Loading establishes no current patch."),
    "midi-options": dict(
        default_section="all", fixed_sections=["all"], named_selectors=[], item_operation=None,
        examples=[dict(tool="reference", arguments=dict(source="midi-options", section="all"))],
        normal_work=dict(native_children=0, python_children=1),
        notes="Only all is supported. One Python help process runs; no MIDI file is converted."),
    "transcribe-options": dict(
        default_section="all", fixed_sections=["all"], named_selectors=[], item_operation=None,
        examples=[dict(tool="reference", arguments=dict(source="transcribe-options", section="all"))],
        normal_work=dict(native_children=0, python_children=1),
        notes="Only all is supported. One Python help process runs and requires NumPy; "
              "no WAV is decoded or transcribed."),
    "reference-options": dict(
        default_section="all", fixed_sections=["all"], named_selectors=[], item_operation=None,
        examples=[dict(tool="reference", arguments=dict(source="reference-options", section="all"))],
        normal_work=dict(native_children=0, python_children=0),
        notes="Only all is supported. This generated host metadata needs no native, compose "
              "or analysis lookup, including when the harness or NumPy is unavailable."),
}


def reference_options_document():
    """Describe the existing selectors without loading their reference sources."""
    sources = [dict(source=source, **{**spec, "normal_work": dict(
        **spec["normal_work"], synthesizes_pcm=False, creates_wav=False,
        writes_artifacts=False, recomputes_each_page=True)})
        for source, spec in REFERENCE_SPECS.items()]
    document = dict(
        version=1, kind="reference_options",
        paging=dict(offset_unit="utf8_bytes", length_default=4096, length_min=1, length_max=8192,
                    continuation_field="next_offset", identity_fields=["source_sha256", "selected_sha256"]),
        cost_scope="Declared direct child counts for normal successful reference-body execution, "
                   "not observed counters, recursive process totals, duration estimates or a "
                   "whole-process I/O bound. Failed prefixes may dispatch fewer children. "
                   "Each page rereads or rebuilds the complete source before selection, including "
                   "UTF8 and hash work; it is not a retained snapshot. Interpreter/import/cache I/O, "
                   "optional generic Python receipt publication and follow-up operations are excluded. "
                   "No zero-file-write claim is made. Paging identities are observations to compare "
                   "across pages, not expected-hash input guards.",
        sources=sources)
    return (json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def reference_selection(source, section, harness):
    """Project existing documentation bytes, never parse authored timelines."""
    if source == "reference-options":
        require(section in (None, "all"), "reference-options supports section=all only", 2)
        original = reference_options_document()
        return original, "all", original
    if source == "midi-options":
        require(section in (None, "all"), "midi-options supports section=all only", 2)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/music/midi2tl.py"),
                                 "--agent-options"], capture_output=True)
        require(result.returncode == 0, "MIDI option discovery failed", 5)
        result.stdout.decode("utf-8")
        return result.stdout, "all", result.stdout
    if source == "transcribe-options":
        require(section in (None, "all"), "transcribe-options supports section=all only", 2)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/fm.py"),
                                 "--transcribe-options"], capture_output=True)
        require(result.returncode == 0, "transcription option discovery failed", 5)
        result.stdout.decode("utf-8")
        return result.stdout, "all", result.stdout
    if source in ("instrument", "recipes"):
        filename = "INSTRUMENT.md" if source == "instrument" else "recipes.txt"
        original = (ROOT / ('docs' if source == 'instrument' else 'data') / filename).read_bytes()
        text = original.decode("utf-8")
        if section == "all":
            return original, "all", original
        if source == "recipes":
            require(section in (None, "index"), "recipes supports index or all; use load_patch for a recipe", 2)
            index = "".join(name + "\n" for name in compose_module().RECIPES)
            return original, "index", index.encode("utf-8")
        headings, position, fence = [], 0, None
        for line in text.splitlines(keepends=True):
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
            elif fence is None:
                match = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*\n?$", line)
                if match:
                    title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2])
                    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    headings.append(dict(title=title, slug=slug, level=len(match[1]),
                                         start=position, heading=line.rstrip("\r\n")))
            position += len(line)
        if section == "index":
            index = "".join(row["slug"] + "\t" + row["title"] + "\n" for row in headings)
            return original, "index", index.encode("utf-8")
        selected = section if section is not None else "Start through MCP"
        matches = [i for i, row in enumerate(headings)
                   if selected in (row["title"], row["slug"], row["heading"])]
        require(len(matches) == 1, "unknown or ambiguous instrument section; request section=index", 2)
        i = matches[0]
        row = headings[i]
        end = next((following["start"] for following in headings[i + 1:]
                    if following["level"] <= row["level"]), len(text))
        return original, row["slug"], text[row["start"]:end].encode("utf-8")

    native = Native(harness)
    require(sha(native.path.read_bytes()) == native.identity["sha256"],
            "native executable changed during operation", 5)
    text = native.listings[source]
    original = text.encode("utf-8")
    if section == "all":
        return original, "all", original
    lines = text.splitlines(keepends=True)
    rows = listing(text)
    if source == "params":
        statements = [row for row in rows if row["row"] in ("STATEMENT", "EVENT")]
        if section in (None, "index"):
            index = lines[0] + "".join("id:" + row["id"] + "\t" + row["name"] + "\n"
                                      for row in statements)
            return original, "index", index.encode("utf-8")
        if section.startswith("id:"):
            selected = [row for row in statements if row["id"] == section[3:]]
        else:
            selected = [row for row in statements if row["name"] == section]
            if not selected:
                selected = [row for row in statements if row["id"] == section]
        require(bool(selected), "unknown parameter section; request section=index", 2)
        ids = {row["id"] for row in selected}
        resolved = "id:" + selected[0]["id"] if len(selected) == 1 else selected[0]["name"]
        dependencies = [dependency for row in rows if row["row"] == "PARAM" and row.get("statement") in ids
                        for dependency in row.get("depends", "").split(";")]
        families = {row["id"] for row in rows if row["row"] == "FAMILY"
                    and any(dependency.startswith(row["id"] + ".") for dependency in dependencies)}
        def included(row):
            return ((row["row"] in ("STATEMENT", "EVENT") and row.get("id") in ids)
                    or (row["row"] in ("PARAM", "FORM") and row.get("statement") in ids)
                    or (row["row"] == "FAMILY" and row.get("id") in families))
    else:
        kinds = [row for row in rows if row["row"] == "GEN_KIND"]
        if section in (None, "index"):
            index = lines[0] + "".join(row["name"] + "\t" + row["id"] + "\n" for row in kinds)
            return original, "index", index.encode("utf-8")
        selected = [row for row in kinds if section in (row["name"], row["id"])]
        require(len(selected) == 1, "unknown or ambiguous generator section; request section=index", 2)
        kind = selected[0]
        mask, resolved = 1 << (int(kind["id"]) - 1), kind["name"]
        def included(row):
            return ((row["row"] == "GEN_KIND" and row["id"] == kind["id"])
                    or (row["row"] == "GEN_PARAM"
                        and (int(row["kinds"]) == 0 or int(row["kinds"]) & mask)))
    excerpt = lines[0] + "".join(line for line, row in zip(lines[1:], rows[1:]) if included(row))
    return original, resolved, excerpt.encode("utf-8")


@public
def reference(source="instrument", section=None, offset=0, length=4096, *, harness=None):
    require(isinstance(source, str) and source in REFERENCE_SPECS,
            "source must be " + ", ".join(tuple(REFERENCE_SPECS)[:-1]) + " or " + tuple(REFERENCE_SPECS)[-1], 2)
    require(section is None or isinstance(section, str), "section must be a string or null", 2)
    integer(offset, "offset")
    integer(length, "length")
    require(offset >= 0 and 1 <= length <= 8192, "offset must be nonnegative and length must be 1..8192", 2)
    original, resolved, selected = reference_selection(source, section, harness)
    base = dict(version=1, ok=True, exit=0, source=source, section=resolved,
                source_sha256=sha(original), selected_sha256=sha(selected),
                total_bytes=len(selected), offset=offset)
    return bounded_text_page(selected, base, length, "selected reference")


def reply_bytes(reply):
    """Conservative JSON-text budget, including escaping and display indentation."""
    return len(json.dumps(reply, ensure_ascii=True, indent=2, allow_nan=False).encode("utf-8"))


def bounded_text_page(data, base, length, name="result", *, data_offset=0, total_size=None):
    """Shared UTF8 policy; a verified slice includes requested bytes plus lookahead."""
    offset = base["offset"]
    size = len(data) if total_size is None else total_size
    relative = offset - data_offset
    require(offset <= size, "offset exceeds " + name + " length", 2)
    require(offset == size or data[relative] & 0xc0 != 0x80,
            "offset must be a UTF-8 codepoint boundary", 2)
    end = min(relative + length, len(data))
    while end < len(data) and data[end] & 0xc0 == 0x80:
        end -= 1
    require(end > relative or offset == size, "length cannot hold the next UTF-8 codepoint", 2)
    text = data[relative:end].decode("utf-8")
    def reply(chars):
        page = text[:chars]
        count = len(page.encode("utf-8"))
        eof = offset + count == size
        return dict(base, length=count, next_offset=None if eof else offset + count, eof=eof, text=page)
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if reply_bytes(reply(mid)) <= 15360:
            low = mid
        else:
            high = mid - 1
    result = reply(low)
    require((low > 0 or offset == size) and reply_bytes(result) <= 15360,
            "page metadata exceeds response budget", 2)
    return result


def native_detail_options(positional, arguments):
    require(arguments.get("detail", "full") in ("full", "compact"),
            "detail must be full or compact", 2)
    if arguments.get("detail", "full") != "compact":
        return
    def utf8(value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as error:
                raise ToolError("compact arguments must contain valid UTF8 strings", 2) from error
        elif isinstance(value, dict):
            for key, child in value.items():
                utf8(key)
                utf8(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                utf8(child)
    utf8(positional)
    utf8(arguments)
    # On filesystems permitting undecodable names, an ASCII symlink can resolve
    # to a non-UTF8 publication path. Refuse that stable path before publication.
    for value in (arguments.get("out"), arguments.get("log"), arguments.get("receipt_dir"),
                  arguments.get("harness") or DEFAULT_HARNESS):
        if isinstance(value, (str, Path)):
            try:
                resolved = Path(value).resolve()
            except (OSError, ValueError):
                continue  # Preserve ordinary host/input error handling below.
            utf8(resolved)


def native_outcome_minimal(reply):
    """Keep bounded native identities/counts and name every omitted group."""
    fixed = ("version", "ok", "exit", "detail", "canonical", "canonical_bytes",
             "full_result_sha256", "full_result_bytes", "full_result_retention", "published",
             "check_counts", "checks_pass", "hash", "expected", "match", "block_indep",
             "record_sha256", "wav_sha256", "fs", "samples", "events", "voices", "streams",
             "channels", "take", "parent", "parent_record_sha256", "clipped", "peak",
             "peak_dbfs", "rms", "rms_dbfs", "crest")
    minimal = {key: reply[key] for key in fixed if key in reply}
    omitted = list(reply.get("omitted", []))
    if isinstance(reply.get("native"), dict):
        minimal["native"] = {key: reply["native"][key] for key in ("sha256",) if key in reply["native"]}
        omitted.extend("native." + key for key in reply["native"] if key != "sha256")
    minimal["omitted"] = sorted(set(omitted) | (set(reply) - set(minimal) - {"omitted"}))
    return minimal


def native_receipt_failure(full):
    """Preserve a completed outcome when its receipt publication is unknown."""
    operation = dict(full["operation_result"], full_result_retention="unknown")
    reply = dict(full, detail="compact", published=operation.get("published"),
                 full_result_retention="unknown", operation_result=operation, omitted=[])
    if reply_bytes(reply) <= 15360:
        return reply
    reply["message"] = None
    reply["omitted"].append("message")
    if reply_bytes(reply) <= 15360:
        return reply
    if operation.get("checks") is not None:
        operation["checks"] = None
        operation["omitted"] = [*operation.get("omitted", []), "checks"]
    if reply_bytes(reply) <= 15360:
        return reply
    reply["operation_result"] = native_outcome_minimal(operation)
    return reply


def record_artifact(reply):
    """Keep body-keyed storage names while artifact digests identify full bytes."""
    path = Path(reply["record"])
    digest = reply.get("document_sha256", reply["record_sha256"])
    locator = dict(directory=str(path.parent.parent), kind="record", digest=digest, encoding="utf8")
    if digest != reply["record_sha256"]:
        locator.update(directory=str(path.parent), filename=path.name)
    return locator


def native_outcome(full, arguments, operation):
    """Bound explicit native outcomes after the existing public error mapping.

    Full mode preserves the original response. Compact mode keeps its exact
    bytes in the optional receipt store; it never reruns native work or adds
    storage when receipts were not requested.
    """
    if arguments.get("detail", "full") == "full":
        return full
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        return native_receipt_failure(full)
    document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    reply = dict(full)
    canonical = full.get("canonical")
    published = bool(full.get("record")) if full.get("ok") else None
    if operation == "check_patch":
        published = False
    counts = None
    if isinstance(full.get("checks"), list):
        counts = {status: sum(check.get("status") == status for check in full["checks"])
                  for status in ("pass", "fail", "unchecked")}
        counts["total"] = len(full["checks"])
    omitted = ["canonical"] if "canonical" in full else []
    artifact = None
    if published:
        artifact = record_artifact(full)
    reply.update(detail="compact", canonical=None,
                 canonical_bytes=len(canonical.encode("utf-8")) if isinstance(canonical, str) else None,
                 full_result_sha256=sha(document), full_result_bytes=len(document),
                 published=published, artifact=artifact, check_counts=counts, omitted=omitted)

    def finish(value):
        result = OperationReply(value)
        result.full_result_bytes = document
        return result

    def omit(key):
        if key in reply and reply[key] is not None:
            reply[key] = None
            omitted.append(key)

    if reply_bytes(reply) <= 15360:
        return finish(reply)
    # Diagnostics and requested target names can be arbitrarily long. Preserve
    # complete verdict counts before omitting a whole check or diagnostic list.
    for group in (("stdout", "stderr", "refusals", "message"), ("checks",),
                  ("record", "wav", "log", "artifact"),
                  ("metrics", "peak_ch", "rms_ch", "crest_ch")):
        for key in group:
            omit(key)
        if reply_bytes(reply) <= 15360:
            return finish(reply)
    if isinstance(reply.get("native"), dict) and "path" in reply["native"]:
        reply["native"] = dict(reply["native"], path=None)
        omitted.append("native.path")
    if reply_bytes(reply) <= 15360:
        return finish(reply)
    # A successful publication must remain successful even when caller metadata
    # defeats the ordinary projection. These identity/count fields have native
    # bounds; caller-controlled strings and collections remain in the full bytes.
    return finish(native_outcome_minimal(reply))


@public(preflight=native_detail_options, outcome=native_outcome)
def render(files=None, *, text=None, harness=None, out=None, wav=False, log=None, parent_record=None, note_text="", target=None, detail="full"):
    if isinstance(target, (list, tuple)):
        target = targets(target)
    require(out is not None or log is not None, "render requires --out DIR or --log DIR", 2)
    boolean(wav, "wav")
    with input_files(files, text) as inputs:
        native = Native(harness)
        reply = native_render(native, inputs, wav)
    reply["checks"], reply["checks_pass"] = checks(reply, target)
    return retain(reply, native, out, log, parent_record, note_text)


def score_module():
    spec = importlib.util.spec_from_file_location("fm_score", ROOT / "tools/score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_reference(value):
    require(type(value) is dict and set(value) == {"directory", "kind", "digest", "filename"}
            and value["kind"] == "score", "source_ref requires directory, kind=score, digest and filename", 2)
    require(isinstance(value["directory"], str) and value["directory"] and "\0" not in value["directory"],
            "source_ref directory must be a nonempty path", 2)
    require(isinstance(value["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", value["digest"]),
            "source_ref digest must be a lowercase SHA256", 2)
    require(isinstance(value["filename"], str) and re.fullmatch(r"[0-9a-f]{64}\.json", value["filename"]),
            "source_ref filename must be its parent receipt SHA256 plus .json", 2)
    return value


def source_input(score, source_ref):
    require((score is not None) != (source_ref is not None), "provide exactly one of score or source_ref", 2)
    module = score_module()
    try:
        if source_ref is not None:
            ref = source_reference(source_ref)
            encoded = source_artifact_bytes(ref["directory"], ref["digest"], ref["filename"])
        else:
            encoded = module.source_bytes(score)
        return module, json.loads(encoded), encoded
    except module.ScoreError as error:
        raise ToolError(str(error), 2, **error.details) from error


def source_receipt_metadata(metadata, result):
    """Attach a relative reader only after publication completed successfully."""
    identity, parent = result.get("source_sha256"), metadata.get("full_result_sha256")
    if (metadata.get("state") == "complete" and result.get("operation")
            in ("compose", "score_inspect", "score_revise")
            and isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
            and isinstance(parent, str) and re.fullmatch(r"[0-9a-f]{64}", parent)):
        return dict(metadata, source_artifact=dict(kind="score", digest=identity,
                    filename=parent + ".json"))
    return metadata


def compose_options(positional, arguments):
    """Only transport/required/encoding limits precede the request receipt."""
    require(arguments.get("detail", "compact") in ("compact", "full"),
            "detail must be compact or full", 2)
    require(arguments.get("log") is not None and arguments.get("receipt_dir") is not None,
            "compose requires explicit log and receipt_dir directories", 2)
    # Check UTF8 paths even in full mode; this new operation has one input policy.
    native_detail_options((), dict(arguments, detail="compact", score=None))
    source = positional[0] if positional else arguments.get("score")
    reference = arguments.get("source_ref")
    require((source is not None) != (reference is not None), "provide exactly one of score or source_ref", 2)
    if reference is not None:
        source_reference(reference)
        return
    try:
        module = score_module()
    except (Exception, SystemExit) as error:
        raise ToolError("score helper could not be imported: " + str(error), 5) from error
    try:
        module.source_bytes(source)
    except module.ScoreError as error:
        raise ToolError(str(error), 2, **error.details) from error


def compose_minimal(reply):
    result = native_outcome_minimal(reply)
    # Full-mode publication/serialization failures also need a bounded inner
    # outcome. Keep canonical identity/size, never the complete TL payload.
    result["detail"] = "compact"
    if result.get("canonical") is not None:
        result["canonical"] = None
        result["omitted"].append("canonical")
    for key in ("source_sha256", "source_bytes", "authored_tl_sha256", "authored_tl_bytes",
                "counts", "native_calls", "publication_state", "native_outcome", "length_ns",
                "quarter_extent", "geometry_counts", "dependency_state", "pointer",
                "expanded_source_sha256", "expanded_source_bytes", "expansion_counts", "operation"):
        if key in reply:
            result[key] = reply[key]
    result["omitted"] = sorted(set(result["omitted"]) -
                               {key for key, value in result.items() if value is not None})
    if reply_bytes(result) > 15360 and "pointer" in result:
        result.pop("pointer")
        result["omitted"].append("pointer")
    return result


def compose_outcome(full, arguments, operation):
    """Retain the complete lowering on either verdict and either detail mode."""
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        inner = compose_minimal(full["operation_result"])
        inner["full_result_retention"] = full["operation_result"].get("full_result_retention", "unknown")
        reply = dict(full, detail="compact", operation_result=inner, full_result_retention="unknown")
        try:
            reply.get("message", "").encode("utf-8")
        except UnicodeError:
            reply["message"] = None
        if reply_bytes(reply) > 15360:
            reply["message"] = None
        return reply
    try:
        document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError):
        inner = compose_minimal(full)
        inner.update(full_result_sha256=None, full_result_bytes=None, full_result_retention="unavailable")
        return dict(version=1, ok=False, exit=5, detail="compact",
                    message="compose outcome cannot be encoded as exact canonical UTF8 JSON",
                    operation_result=inner, full_result_sha256=None, full_result_bytes=None,
                    full_result_retention="unavailable")
    reply = dict(full, detail=arguments.get("detail", "compact"),
                 full_result_sha256=sha(document), full_result_bytes=len(document))
    if reply["detail"] == "compact":
        reply["omitted"] = []
        for key in ("authored_tl", "line_map", "dependencies", "canonical", "space", "source",
                    "expanded_source", "event_origins", "phrase_occurrences"):
            if key in reply:
                reply[key] = None
                reply["omitted"].append(key)
        reply["check_counts"] = ({status: sum(row.get("status") == status for row in full["checks"])
                                  for status in ("pass", "fail", "unchecked")}
                                 if isinstance(full.get("checks"), list) else None)
        if reply["check_counts"] is not None:
            reply["check_counts"]["total"] = len(full["checks"])
        if reply_bytes(reply) > 15360:
            for key in ("stdout", "stderr", "refusals", "message", "mapped_refusals", "checks"):
                if key in reply:
                    reply[key] = None
                    reply["omitted"].append(key)
        if reply_bytes(reply) > 15360:
            reply = compose_minimal(reply)
    result = OperationReply(reply)
    result.full_result_bytes = document
    return result


@public(preflight=compose_options, outcome=compose_outcome, receipt_projection=text_receipt_metadata)
def compose(score=None, *, source_ref=None, log, harness=None, parent_record=None, note_text="", target=None, detail="compact"):
    """Lower an explicit score, native-validate once, and retain a WAV-free take."""
    calls = dict(discovery=0, candidate=0, parent=0, total=0)
    result = dict(version=1, ok=False, exit=5, operation="compose", publication_state="not_attempted", published=False,
                  native_calls=calls, dependency_state="not_checked")
    native, candidate, module, input_path = None, None, None, None
    phase = "candidate"
    def attempted(arguments):
        role = phase if arguments and str(arguments[0]) == "--play" else "discovery"
        calls[role] += 1
        calls["total"] += 1
    try:
        module, score, encoded = source_input(score, source_ref)
        result.update(source=score, source_sha256=sha(encoded), source_bytes=len(encoded))
        validated = module.validate_score(score)
        if validated.expansion is not None:
            result.update(validated.expansion)
        if isinstance(target, (list, tuple)):
            target = targets(target)
        # These are on-disk snapshots, not attestation of already loaded code.
        before = operation_provenance(dict(harness=harness), "compose")
        result["dependencies"] = dict(kind="on_disk_before_after", before=before, after=None,
                                      loaded_modules_attested=False)
        composer = compose_module()
        native = Native(harness, call_hook=attempted)
        result["native"] = native.identity
        lowered = module.lower_score(validated, composer=composer, engine=native.metadata.engine,
                                     statements=native.metadata.statements)
        result.update(lowered)
        # Keep exact native stderr. input_files intentionally replaces temporary
        # paths for legacy text calls; this operation maps them alongside it.
        with tempfile.TemporaryDirectory(prefix="fm-compose-") as temporary:
            authored = Path(temporary) / "authored.tl"
            authored.write_bytes(lowered["authored_tl"].encode("utf-8"))
            input_path = str(authored)
            candidate = native_render(native, [authored])
        result.update(clean(candidate))
        result["native_outcome"] = dict(ok=candidate["ok"], exit=candidate["exit"])
        candidate["checks"], candidate["checks_pass"] = checks(candidate, target)
        result.update(checks=candidate["checks"], checks_pass=candidate["checks_pass"])
        result["canonical_bytes"] = len(candidate["_record_bytes"])
        result["space"] = describe_rows(candidate)["space"]
        result["geometry_counts"] = {name: sum(row["id"] == "event_" + name for row in candidate["_rows"])
                                     for name in ("pos", "move", "yaw")}
        after = operation_provenance(dict(harness=harness), "compose")
        result["dependencies"]["after"] = after
        result["dependency_state"] = "equal" if before == after else "changed"
        require(before == after, "authoring dependencies changed during compose", 5)
        phase = "parent"
        result.update(publication_state="unknown", published=None)
        retained = retain(candidate, native, log=log, parent_record=parent_record, note_text=note_text)
        result.update(clean(retained))
        result.update(publication_state="complete", published=True,
                      artifact=record_artifact(retained))
    except (Exception, SystemExit) as error:
        if module is not None and isinstance(error, module.ScoreError):
            result.update(ok=False, exit=2, message=str(error), **error.details)
        elif isinstance(error, ToolError):
            result.update(ok=False, exit=error.exit, message=str(error), **error.details)
        else:
            result.update(ok=False, exit=5, message=str(error), host_exception=type(error).__name__)
        if native is not None:
            result["native"] = native.identity
        if candidate is not None:
            # retain can publish a record before failing its index update.
            # Keep any known paths, but never infer a transaction rollback.
            for key in ("record", "wav", "log", "take", "parent", "parent_record_sha256"):
                if key in candidate:
                    result[key] = candidate[key]
        if input_path is not None and isinstance(result.get("line_map"), list):
            by_line = {row["line"]: row for row in result["line_map"]}
            result["mapped_refusals"] = [dict(diagnostic=diagnostic, source=by_line[diagnostic["line"]])
                for diagnostic in result.get("refusals", [])
                if diagnostic.get("file") == input_path and diagnostic.get("line") in by_line]
        if result.get("dependencies", {}).get("after") is None and "dependencies" in result:
            try:
                after = operation_provenance(dict(harness=harness), "compose")
                result["dependencies"]["after"] = after
                result["dependency_state"] = "equal" if before == after else "changed"
                if before != after:
                    result["operation_error"] = {key: result.get(key) for key in ("ok", "exit", "message")}
                    result.update(ok=False, exit=5, message="authoring dependencies changed during compose")
            except Exception as snapshot_error:
                result["operation_error"] = {key: result.get(key) for key in ("ok", "exit", "message")}
                result.update(ok=False, exit=5, message="authoring dependency snapshot failed: " + str(snapshot_error),
                              dependency_state="unavailable")
    return result


def workflow_outcome(full, arguments, operation):
    """Project host workflow evidence without repeating its execution."""
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        inner = full["operation_result"]
        return dict(version=1, ok=False, exit=5, operation=operation,
                    message="operation completed but receipt publication failed",
                    operation_result={key: inner[key] for key in
                        ("ok", "exit", "source_sha256", "record_sha256", "full_result_sha256", "full_result_bytes")
                        if key in inner}, receipt=full["receipt"], full_result_retention="unknown")
    try:
        document = receipts_module().canonical_bytes(full)
    except (UnicodeError, ValueError, TypeError, RecursionError, receipts_module().ReceiptError):
        return dict(version=1, ok=False, exit=5, operation=operation,
                    message="workflow outcome cannot be encoded as exact canonical UTF8 JSON",
                    operation_result={key: full[key] for key in ("ok", "exit", "source_sha256", "record_sha256")
                                      if key in full}, full_result_sha256=None, full_result_bytes=None,
                    full_result_retention="unavailable")
    reply = dict(full, detail=arguments.get("detail", "compact"),
                 full_result_sha256=sha(document), full_result_bytes=len(document))
    if reply["detail"] == "compact":
        omitted = []
        def omit(owner, key, prefix=""):
            if key in owner:
                owner[key] = None
                omitted.append(prefix + key)
        for key in ("source", "canonical", "event_rows", "event_origins", "advisories", "streams", "voices",
                    "region_definitions", "region_summaries", "phrase_definitions", "phrase_occurrences"):
            if key in ("region_definitions", "region_summaries", "phrase_definitions", "phrase_occurrences") and isinstance(reply.get(key), list):
                reply[key + "_count"] = len(reply[key])
            omit(reply, key)
        if isinstance(reply.get("edit"), dict):
            reply["edit"] = dict(reply["edit"])
            for key in ("expected", "value"):
                omit(reply["edit"], key, "edit.")
        for name in ("inspection", "difference"):
            if isinstance(reply.get(name), dict):
                nested = dict(reply[name])
                reply[name] = nested
                for key in ("event_rows", "event_origins", "advisories", "streams", "voices",
                            "before_event_rows", "after_event_rows", "before_event_origins", "after_event_origins",
                            "added_event_indices", "removed_event_indices", "timing_changes", "before", "after",
                            "region_definitions", "region_summaries", "phrase_definitions", "phrase_occurrences", "region_differences"):
                    if isinstance(nested.get(key), list):
                        nested[key + "_count"] = len(nested[key])
                    omit(nested, key, name + ".")
        reply["omitted"] = omitted
        if reply_bytes(reply) > 15360:
            for key in ("per_stream", "per_voice", "inspection", "difference", "scope", "edit",
                        "stdout", "stderr", "refusals", "message", "checks", "native"):
                omit(reply, key)
        if reply_bytes(reply) > 15360:
            kept = {key: reply[key] for key in ("version", "ok", "exit", "operation", "detail",
                    "source_sha256", "source_bytes", "base_source_sha256", "base_source_bytes",
                    "record_sha256", "full_result_sha256", "full_result_bytes") if key in reply}
            kept["omitted"] = sorted(set(full) - set(kept))
            reply = kept
    result = OperationReply(reply)
    result.full_result_bytes = document
    return result


def score_view_options(positional, arguments):
    require(arguments.get("receipt_dir") is not None, "score operations require explicit receipt_dir", 2)
    require(arguments.get("detail", "compact") in ("compact", "full"), "detail must be compact or full", 2)
    source = positional[0] if positional else arguments.get("score")
    reference = arguments.get("source_ref")
    require(len(positional) <= 1 and not (positional and "score" in arguments),
            "score must be supplied once", 2)
    require((source is not None) != (reference is not None), "provide exactly one of score or source_ref", 2)
    native_detail_options((), dict(arguments, detail="compact"))
    if reference is not None:
        source_reference(reference)
    else:
        module = score_module()
        try:
            module.source_bytes(source)
        except module.ScoreError as error:
            raise ToolError(str(error), 2, **error.details) from error


@public(preflight=score_view_options, outcome=workflow_outcome, receipt_projection=source_receipt_metadata)
def score_inspect(score=None, *, source_ref=None, start=0, end=None, detail="compact"):
    """Inspect authored intent and advisory gates; execute no native child."""
    module, source, _ = source_input(score, source_ref)
    try:
        result = module.inspect_source(source, composer=compose_module(), start=start, end=end)
    except module.ScoreError as error:
        raise ToolError(str(error), 2, **error.details) from error
    return dict(version=1, ok=True, exit=0, operation="score_inspect", source=source, native_calls=0, **result)


@public(preflight=score_view_options, outcome=workflow_outcome, receipt_projection=source_receipt_metadata)
def score_revise(score=None, *, source_ref=None, expected_source_sha256, pointer, expected, value, detail="compact"):
    """Preview one exact guarded replacement; compose validates and publishes audio."""
    module, source, _ = source_input(score, source_ref)
    try:
        result = module.revise_source(source, expected_source_sha256=expected_source_sha256,
                    pointer=pointer, expected=expected, value=value, composer=compose_module())
    except module.ScoreError as error:
        raise ToolError(str(error), 2, **error.details) from error
    return dict(version=1, ok=True, exit=0, operation="score_revise", native_calls=0,
                native_validation="not_run", published_take=False, **result)


def measure_module():
    spec = importlib.util.spec_from_file_location("fm_measure", ROOT / "tools/measure.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_options(positional, arguments):
    measure_module().preflight(SimpleNamespace(**globals()), positional, arguments)


@public(preflight=measure_options, outcome=workflow_outcome)
def measure_section(files=None, *, text=None, harness=None, start_ns=0, end_ns=None):
    """Measure final output frames after rendering the complete session history."""
    return measure_module().execute(SimpleNamespace(**globals()), files, text=text, harness=harness,
                                    start_ns=start_ns, end_ns=end_ns)


def measure_sections_options(positional, arguments):
    measure_module().preflight_sections(SimpleNamespace(**globals()), positional, arguments)


def measure_sections_outcome(full, arguments, operation):
    return measure_module().outcome_sections(SimpleNamespace(**globals()), full, arguments, operation)


@public(preflight=measure_sections_options, outcome=measure_sections_outcome)
def measure_sections(files=None, *, text=None, harness=None, sections, detail="compact"):
    """Measure ordered whole-mix windows through a bounded pipe, without an audio file."""
    return measure_module().execute_sections(SimpleNamespace(**globals()), files, text=text,
                                            harness=harness, sections=sections)


def measure_voice_sections_options(positional, arguments):
    measure_module().preflight_voice_sections(SimpleNamespace(**globals()), positional, arguments)


def measure_voice_sections_outcome(full, arguments, operation):
    return measure_module().outcome_voice_sections(SimpleNamespace(**globals()), full, arguments, operation)


@public(preflight=measure_voice_sections_options, outcome=measure_voice_sections_outcome)
def measure_voice_sections(files=None, *, text=None, harness=None, voices, sections, detail="compact"):
    """Measure exact dry Q30 voice windows through a pipe after both full-mix checks."""
    return measure_module().execute_voice_sections(SimpleNamespace(**globals()), files, text=text,
        harness=harness, voices=voices, sections=sections)


def score_file(path):
    """Read JSON source; duplicated keys cannot silently change author intent."""
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate score JSON key: " + key, 2)
            result[key] = value
        return result
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeError as error:
        raise ToolError("score file must contain valid UTF8", 2) from error
    except OSError as error:
        raise ToolError(str(error), 5) from error
    try:
        return json.loads(text, object_pairs_hook=object_pairs)
    except (ValueError, RecursionError) as error:
        raise ToolError("invalid score JSON: " + str(error), 2) from error


def geometry(raw):
    x, y, z = (raw[k] for k in ("X", "Y", "Z"))
    distance = math.sqrt(x * x + y * y + z * z)
    return dict(x=x, y=y, z=z, distance_mm=distance,
                azimuth_deg=math.degrees(math.atan2(x, y)) if x or y else None,
                elevation_deg=math.degrees(math.atan2(z, math.hypot(x, y))) if distance else None)


def describe_rows(reply):
    rows = reply["_rows"]
    patches, events, voices, fx, gen = {}, [], [], [], []
    for row in rows:
        if row["section"] == "patch":
            patches.setdefault(row["raw"]["PATCH"], []).append(row)
        elif row["section"] == "event":
            events.append(row)
        elif row["id"] == "header_voice":
            voices.append(dict(index=row["raw"]["INDEX"], patch=row["raw"]["PATCH"]))
        elif row["section"] == "fx":
            fx.append(row)
        elif row["section"] == "generator":
            gen.append(row)
    spatial = [r for r in rows if r["section"] == "space"]
    space = None
    if spatial and any(r["id"] == "header_space" and r["raw"].get("MODE") != "off" for r in spatial):
        places = {str(v["index"]): [] for v in voices}
        for row in events:
            if row["id"] in ("event_pos", "event_move"):
                places.setdefault(str(row["raw"]["VOICE"]), []).append(row)
        per_voice = {}
        for voice, moves in places.items():
            points = [geometry(r["raw"]) for r in moves]
            angles = [p["azimuth_deg"] for p in points if p["azimuth_deg"] is not None]
            span, reason = None, None
            if len(angles) != len(points):
                reason = "a declared waypoint has undefined azimuth"
            elif not angles:
                reason = "no declared places"
            else:
                unwrapped = [angles[0]]
                for previous, current in zip(angles, angles[1:]):
                    unwrapped.append(unwrapped[-1] + (current - previous + 180) % 360 - 180)
                span = max(unwrapped) - min(unwrapped)
            per_voice[voice] = dict(first_pos=next((geometry(r["raw"]) for r in moves if r["id"] == "event_pos"), None),
                                    pos_count=sum(r["id"] == "event_pos" for r in moves),
                                    move_count=sum(r["id"] == "event_move" for r in moves),
                                    extent_deg=span, extent_method="declared_place_unwrapped_span",
                                    extent_reason=reason, places=points)
        space = dict(header=spatial, voices=per_voice, image=None, per_voice_image=None,
                     per_voice_image_reason=": per-voice output is not available")
    return dict(rows=rows, patches=patches, voices=voices, events=events,
                fx=fx or None, gen=gen or None, space=space, descriptors=None,
                descriptors_reason="measurement not requested")


def analysis_module():
    spec = importlib.util.spec_from_file_location("fm_analysis", ROOT / "tools/music/analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_module():
    spec = importlib.util.spec_from_file_location("fm_compose", ROOT / "tools/music/compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recipe_record(native, name, note=None):
    """Use the existing recipe writer, then accept it through the native reader."""
    analysis, compose = analysis_module(), compose_module()
    try:
        car, mod, _, *extra = compose.recipe(name)
        tags, pitches = analysis.recipe_metadata(compose)
        mhz = note if note is not None else pitches.get(name, analysis.DESC_NOTE)
        headers = compose.patch_headers(name, car, mod, "", *extra)
    except (SystemExit, KeyError, ValueError) as error:
        raise ToolError("recipe could not be written: " + str(error), 2) from error
    hold = round(analysis.DESC_HOLD * 1e9)
    text = ["timeline draft1", "engine " + native.metadata.engine, f"fs {analysis.DESC_FS}",
            f"length_ns {hold + round(analysis.DESC_TAIL * 1e9)}", "streams d", *headers,
            f"voice 0 {name}", f"0 d 0 on {mhz} {analysis.DESC_VEL}", f"{hold} d 0 off"]
    with tempfile.TemporaryDirectory(prefix="fm-recipe-") as temporary:
        path = Path(temporary) / "recipe.tl"
        path.write_text("\n".join(text) + "\n")
        reply = native_render(native, [path])
    reply["recipe"], reply["tags"] = name, tags.get(name, [])
    return reply


def resolve_source(source, native, wav=False):
    """Resolve explicit files, a piece directory, or a recipe through C."""
    if isinstance(source, (list, tuple)):
        return native_render(native, source, wav)
    path = workspace_path(source)
    if path.is_dir():
        header = path / "session.tl"
        require(header.is_file(), "piece directory has no session.tl", 2)
        declared = native_render(native, [header])
        streams = next(r["raw"]["NAME"] for r in declared["_rows"] if r["id"] == "header_streams")
        streams = streams if isinstance(streams, list) else [streams]
        files, absent = [header], []
        for name in streams:
            sibling = path / (name + ".tl")
            if sibling.is_file():
                files.append(sibling)
            else:
                absent.append(name)
        reply = native_render(native, files, wav)
        reply.update(source_files=list(map(str, files)), absent_sibling_streams=absent)
        return reply
    if path.is_file() or isinstance(source, Path) or path.suffix == ".tl" or Path(source).parent != Path("."):
        return native_render(native, [path], wav)
    reply = recipe_record(native, str(source))
    if wav:
        with tempfile.TemporaryDirectory(prefix="fm-recipe-") as temporary:
            path = Path(temporary) / "accepted.tl"
            path.write_bytes(reply["_record_bytes"])
            measured = native_render(native, [path], True)
            measured["recipe"], measured["tags"] = reply["recipe"], reply["tags"]
            reply = measured
    return reply


def edit_canonical(accepted, changes):
    """Edit returned physical field references; native validation remains next."""
    document = tl_document_module().parse_document(accepted["canonical"].encode("utf-8"))
    lines = [line.split() for line in document["body"].decode("utf-8").splitlines()]
    rows = {r["line_index"]: r for r in accepted.get("_rows", accepted.get("rows", []))}
    require(rows, "canonical editing needs the returned row inventory", 2)
    for change in changes:
        index, token, component = change["row"], change["token"], change.get("component")
        require(index in rows and any(f["token"] == token and f["component"] == component for f in rows[index]["fields"]),
                "edit does not name a returned canonical field", 2)
        value = str(change["value"])
        require(value and not any(c.isspace() or c == "#" for c in value), "edit value must be one token", 2)
        if component in PARTS:
            values = list(EDGE.fullmatch(lines[index][token]).groups())
            values[PARTS[component]] = value
            lines[index][token] = values[0] + ">" + values[1] + ":" + values[2] + (":" + values[3] if values[3] is not None else "")
        elif component:
            lines[index][token] = component + "=" + value
        else:
            lines[index][token] = value
    body = ("\n".join(" ".join(line) for i, line in enumerate(lines)
                       if rows.get(i, {}).get("id") != "header_hash") + "\n").encode("utf-8")
    result = document["header"] + body
    tl_document_module().parse_document(result)
    return result


def isolated_patch(native, accepted, patch, note=None, fs=None):
    """'s existing two-render note protocol, from accepted canonical rows.

    This derives authored test inputs from native output; it never reads an
    authored source. Assets and generators remain native-owned dependencies.
    """
    import numpy as np
    analysis = analysis_module()
    rows = accepted["_rows"]
    voices = {r["raw"]["INDEX"]: r["raw"]["PATCH"] for r in rows if r["id"] == "header_voice"}
    first = next((r["raw"]["FREQ_MILLIHZ"] for r in rows if r["id"] == "event_on"
                  and voices.get(r["raw"]["VOICE"]) == patch), None)
    mhz = note if note is not None else first if first is not None else analysis.DESC_NOTE
    fs = accepted["fs"] if fs is None else fs
    profile = dict(kind="isolated_patch_note", fs=fs, note_millihz=mhz,
                   note_source="explicit" if note is not None else "first_on" if first is not None else "A4_fallback",
                   velocity=analysis.DESC_VEL, hold_s=analysis.DESC_HOLD, tail_s=analysis.DESC_TAIL,
                   level_db10=analysis.DESC_LEVEL, level_policy="D83_carrier_sum",
                   native_sha256=native.identity["sha256"])
    if not isinstance(mhz, int) or not analysis.BAND[0] * 1000 <= mhz < fs * 500:
        return dict(measured=False, profile=profile, values=None,
                    reason="note is outside the descriptor measurement band")
    pairs = list(zip(rows, canonical_body(accepted).splitlines()))
    selected = [(r, line.split()) for r, line in pairs
                if (r["section"] == "patch" and r["raw"].get("PATCH") == patch)
                or r["section"] == "asset" or (r["section"] == "generator" and r["id"] != "gen_lane")]
    operators = [r for r, _ in selected if r["id"] == "patch"]
    require(operators, "accepted record has no requested patch " + patch, 2)
    route = next((r for r, _ in selected if r["id"] == "route"), None)
    mask = route["raw"]["CARRIERS"] if route else 1
    carriers = [r for r in operators if mask & (1 << r["raw"]["OP"])]
    levels = [r["raw"]["LEVEL_DB10"] for r in carriers]
    # Subtract the quietest numerical level before exponentiation so even
    # accepted uint16 attenuations cannot underflow the normalization sum.
    minimum = min(levels)
    attenuation = minimum - 200 * math.log10(sum(10 ** (-(level - minimum) / 200) for level in levels))
    shift = math.ceil(round(analysis.DESC_LEVEL - attenuation, 9)) if len(levels) > 1 else analysis.DESC_LEVEL - levels[0]
    hold_ns = round(analysis.DESC_HOLD * 1e9)
    header = ["timeline draft1", "engine " + native.metadata.engine, f"fs {fs}",
              f"length_ns {hold_ns + round(analysis.DESC_TAIL * 1e9)}", "streams d"]
    tail = [f"voice 0 {patch}", f"0 d 0 on {mhz} {analysis.DESC_VEL}", f"{hold_ns} d 0 off"]
    renders = []
    try:
        with tempfile.TemporaryDirectory(prefix="fm-note-") as temporary:
            path = Path(temporary) / "note.tl"
            for normalized in (False, True):
                body = []
                for row, tokens in selected:
                    tokens = tokens[:]
                    if normalized and row["id"] == "patch" and mask & (1 << row["raw"]["OP"]):
                        field = next(f for f in row["fields"] if f["id"] == "patch_level_db10")
                        tokens[field["token"]] = str(row["raw"]["LEVEL_DB10"] + shift)
                    body.append(" ".join(tokens))
                path.write_text("\n".join(header + body + tail) + "\n")
                renders.append(native_render(native, [path], normalized))
        with wave.open(io.BytesIO(renders[1]["_wav_bytes"]), "rb") as audio:
            values = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float64) / 32768
        measured = analysis.measure_note(values, fs, mhz / 1000, hold_ns * fs // 10 ** 9)
        measured["peak_dbfs"] = renders[0]["peak_dbfs"]
        return clean(dict(measured=True, profile=profile, values=measured,
                          written_hash=renders[0]["hash"], measured_hash=renders[1]["hash"],
                          written_record_sha256=renders[0]["record_sha256"],
                          measured_record_sha256=renders[1]["record_sha256"]))
    except SystemExit as error:
        return dict(measured=False, profile=profile, values=None, reason=str(error))
measure_patch = isolated_patch


def measurement_describe(native, reply, note=None):
    reply.update(describe_rows(reply))
    reply["descriptors"] = {patch: isolated_patch(native, reply, patch, note) for patch in reply["patches"]}
    reply.pop("descriptors_reason", None)
    whole = wav_measure(reply["_wav_bytes"], True, note)
    reply["whole_mix"] = whole
    if reply["space"] is not None:
        reply["space"]["image"] = whole["image"]
    return reply


def wav_measure(data, measure=False, note=None):
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as audio:
        channels, fs, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
        require(audio.getsampwidth() == 2 and channels in (1, 2), "only mono/stereo 16-bit PCM is measured", 2)
        pcm = audio.readframes(frames)
    require(frames and len(pcm) == frames * channels * 2, "empty or truncated WAV", 1)
    values = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels).astype(np.float64) / 32768
    peak = float(np.abs(values).max())
    rms = float(np.sqrt(np.mean(values ** 2)))
    fnv = 14695981039346656037
    for byte in pcm:
        fnv = ((fnv ^ byte) * 1099511628211) & ((1 << 64) - 1)
    reply = dict(wav_sha256=sha(data), fs=fs, channels=channels, samples=frames,
                 hash=f"{fnv:016x}", hash_source="PCM_FNV1a64", peak_dbfs=20 * math.log10(peak) if peak else None,
                 rms_dbfs=20 * math.log10(rms) if rms else None, descriptors=None, image=None)
    if not measure:
        return reply
    analysis = analysis_module()
    try:
        reply["descriptors"] = analysis.meter(tuple(values[:, i] for i in range(channels)), fs)
        reply["measured"] = True
    except (SystemExit, ValueError, IndexError) as error:
        reply["measured"], reply["descriptors_reason"] = False, str(error)
    if note is not None:
        require(isinstance(note, int) and note > 0, "--note must be positive integer mHz", 2)
        reply["note_profile"] = dict(kind="external_wav_note", note_millihz=note, fs=fs,
                                     onset_frame=0, note_off_frame=frames - 1, level_policy="original",
                                     release_reason="no observed note-off/release; EOF is censored")
        try:
            if not analysis.BAND[0] * 1000 <= note < fs * 500:
                raise ValueError("note is outside the descriptor measurement band")
            reply["note_descriptors"] = analysis.measure_note(values.mean(axis=1), fs, note / 1000, frames - 1)
            reply["note_descriptors"]["release_ms"] = None
            reply["note_measured"] = True
        except (SystemExit, ValueError, IndexError) as error:
            reply["note_descriptors"] = None
            reply["note_measured"], reply["note_reason"] = False, str(error)
    if channels == 2:
        windows, size = [], max(1, round(fs / 10))
        for start in range(0, frames, size):
            block = values[start:start + size]
            left, right = block[:, 0], block[:, 1]
            mid, side = (left + right) / 2, (left - right) / 2
            pm, ps = float(np.mean(mid ** 2)), float(np.mean(side ** 2))
            correlation = float(np.corrcoef(left, right)[0, 1]) if np.ptp(left) and np.ptp(right) else None
            itd, ild = None, [None, None, None]
            if len(block) >= 96 and np.any(left) and np.any(right):
                nfft = max(96, 2 * ((len(block) + 1) // 2))
                with np.errstate(divide="ignore", invalid="ignore"):
                    delay, levels = analysis.itd_ild(np.fft.rfft(left, nfft), np.fft.rfft(right, nfft), np.fft.rfftfreq(nfft, 1 / fs), fs)
                itd, ild = float(delay), [float(v) for v in levels]
            windows.append(dict(start_frame=start, frames=len(block), itd_us=itd,
                                ild_db=ild, side_mid_db=10 * math.log10(ps / pm) if ps and pm else None,
                                correlation=correlation))
        reply["image"] = dict(fs=fs, window_frames=size, windows=windows,
                              bands_hz=[[200, 1500], [1500, 6000], [6000, 16000]])
    return clean(reply)


def description_detail(reply, detail):
    """Project descriptions only after validation and measurements are complete."""
    reply["detail"] = detail
    if detail == "summary" and "rows" in reply:
        reply["description_counts"] = {key: len(reply.get(key) or [])
                                       for key in ("rows", "events", "patches", "voices", "fx", "gen")}
        def compact(row):
            return {key: row[key] for key in ("id", "statement", "line_index", "raw", "human")}
        reply["patches"] = {name: [compact(row) for row in rows]
                            for name, rows in reply["patches"].items()}
        for key in ("fx", "gen"):
            if reply[key] is not None:
                reply[key] = [compact(row) for row in reply[key]]
        if reply["space"] is not None:
            reply["space"]["header"] = [compact(row) for row in reply["space"]["header"]]
        reply["rows"] = reply["events"] = None
        reply["omitted"] = ["rows", "events", "field_metadata"]
    return reply


def detail_options(detail, offset, length, expected_result_sha256, out=None):
    require(detail in ("compact", "summary", "full", "page", "windows"),
            "detail must be compact, summary, full, page or windows", 2)
    integer(offset, "offset")
    integer(length, "length")
    require(offset >= 0 and 1 <= length <= 8192,
            "offset must be nonnegative and length must be 1..8192", 2)
    require(expected_result_sha256 is None or isinstance(expected_result_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected_result_sha256),
            "expected_result_sha256 must be a lowercase SHA256", 2)
    require(detail == "page" or (offset == 0 and length == 4096
            and (expected_result_sha256 is None or detail == "windows")),
            "paging arguments require detail=page", 2)
    require(out is None or detail in ("summary", "full"),
            "describe out requires explicit summary or full detail; use render to retain artifacts", 2)


def window_options(detail, measure, start_ns, end_ns, window_offset, window_limit):
    """Validate selection intent before resolving sources or running native code."""
    for name, value in (("start_ns", start_ns), ("window_offset", window_offset),
                        ("window_limit", window_limit)):
        integer(value, name)
    if end_ns is not None:
        integer(end_ns, "end_ns")
    require(start_ns >= 0 and (end_ns is None or end_ns > start_ns),
            "start_ns must be nonnegative and end_ns must exceed start_ns", 2)
    require(window_offset >= 0 and 1 <= window_limit <= 32,
            "window_offset must be nonnegative and window_limit must be 1..32", 2)
    require(detail == "windows" or (start_ns == 0 and end_ns is None
            and window_offset == 0 and window_limit == 8),
            "window selection arguments require detail=windows", 2)
    require(detail != "windows" or measure, "windows detail requires measure=true", 2)


def window_result(full, identity, size, start_ns, end_ns, window_offset, window_limit):
    """Select complete existing windows; checks still concern the complete result."""
    comparison = "a" in full and "b" in full and "same_record" in full
    selected = {}

    def source_view(source, name):
        image = (source.get("whole_mix") or {}).get("image")
        windows = image["windows"] if image is not None else []
        chosen = {i: window for i, window in enumerate(windows)
                  if (end_ns is None or window["start_frame"] * 10 ** 9 < end_ns * image["fs"])
                  and (window["start_frame"] + window["frames"]) * 10 ** 9 > start_ns * image["fs"]}
        selected[name] = chosen
        fixed = ("hash", "record_sha256", "wav_sha256", "fs", "channels", "samples",
                 "exit", "match", "block_indep", "checks_pass")
        view = {key: source.get(key) for key in fixed}
        view["image"] = dict(available=image is not None,
                             reason=None if image is not None else "mono_has_no_image"
                             if source.get("channels") == 1 else "image_unavailable",
                             fs=image["fs"] if image is not None else source.get("fs"),
                             window_frames=image["window_frames"] if image is not None else None,
                             bands_hz=image["bands_hz"] if image is not None else None,
                             total_windows=len(windows), selected_windows=len(chosen))
        return view

    reply = dict(version=full["version"], ok=full["ok"], exit=full["exit"], detail="windows",
                 native=dict(sha256=full["native"]["sha256"]),
                 full_result=dict(sha256=identity, size=size, detail="full"),
                 interval=dict(start_ns=start_ns, end_ns=end_ns, policy="whole_overlapping_windows"),
                 checks_scope="full_result", checks_pass=full.get("checks_pass"),
                 check_counts={status: sum(c["status"] == status for c in full.get("checks", []))
                               for status in ("pass", "fail", "unchecked")},
                 window_offset=window_offset, window_limit=window_limit, omitted=["full_result"])
    if comparison:
        for side in ("a", "b"):
            reply[side] = source_view(full[side], side)
        reply.update(same_hash=full["same_hash"], same_record=full["same_record"])
        indexes = sorted(selected["a"].keys() | selected["b"].keys())
    else:
        reply["source"] = source_view(full, "source")
        indexes = sorted(selected["source"])
    require(window_offset <= len(indexes), "window_offset exceeds selected row count", 2)
    reply["selected_rows"] = len(indexes)
    rows = []
    for index in indexes[window_offset:window_offset + window_limit]:
        if not comparison:
            rows.append(dict(window_index=index, window=selected["source"][index]))
            continue
        left, right = selected["a"].get(index), selected["b"].get(index)
        delta, reason = None, "missing_or_unselected"
        if left is not None and right is not None:
            same_geometry = (reply["a"]["image"]["fs"] == reply["b"]["image"]["fs"]
                             and all(left[key] == right[key] for key in ("start_frame", "frames")))
            if same_geometry:
                delta, reason = full["whole_mix"]["image"]["windows"][index], None
            else:
                reason = "unequal_geometry"
        rows.append(dict(window_index=index, a=left, b=right, delta=delta, delta_reason=reason))

    def page(count):
        stop = window_offset + count
        return dict(reply, rows=rows[:count], returned_count=count,
                    next_window_offset=None if stop == len(indexes) else stop,
                    eof=stop == len(indexes))

    count = len(rows)
    try:
        while count and reply_bytes(page(count)) > 15360:
            count -= 1
        result = page(count)
        fits = reply_bytes(result) <= 15360
    except (ValueError, OverflowError) as error:
        raise ToolError("window metadata exceeds response budget", 2) from error
    require((count > 0 or window_offset == len(indexes)) and fits,
            "window metadata exceeds response budget", 2)
    return result


def result_detail(reply, detail, offset, length, expected_result_sha256, *, start_ns=0,
                  end_ns=None, window_offset=0, window_limit=8):
    """Project only complete results; explicit legacy modes never change shape."""
    full = clean(reply)
    document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    def finish(value):
        result = OperationReply(value)
        result.full_result_bytes = document
        return result
    if detail == "full":
        return finish(full)
    if detail == "summary":
        projected = clean(full)
        if "a" in projected and "b" in projected and "same_record" in projected:
            for side in ("a", "b"):
                description_detail(projected[side], "summary")
        else:
            description_detail(projected, "summary")
        return finish(projected)
    identity = sha(document)
    if detail in ("page", "windows"):
        require(expected_result_sha256 is None or expected_result_sha256 == identity,
                "result identity changed; restart paging from the new result", 2)
    if detail == "windows":
        return finish(window_result(full, identity, len(document), start_ns, end_ns, window_offset, window_limit))
    if detail == "page":
        return finish(bounded_text_page(document,
            dict(version=full["version"], ok=full["ok"], exit=full["exit"], detail="page",
                 result_sha256=identity, total_bytes=len(document), offset=offset), length))
    return finish(compact_result(full, identity, len(document)))


def compact_result(full, identity, size):
    """Bound a presentation without changing measurements, checks or full bytes."""
    # clean makes an independent tree, including duplicate spatial image paths.
    reply = clean(full)
    comparison = "a" in reply and "b" in reply and "same_record" in reply
    objects = [(reply, "")]
    if comparison:
        objects += [(reply[side], side + ".") for side in ("a", "b")]
    omitted = []

    def omit(obj, key, prefix):
        if key in obj and obj[key] is not None:
            obj[key] = None
            name = prefix + key
            if name not in omitted:
                omitted.append(name)

    def image_detail(value, prefix):
        # A mono/stereo delta embeds the present image inside [a,b,null].
        if isinstance(value, dict):
            if isinstance(value.get("windows"), list):
                value["window_count"] = len(value["windows"])
                omit(value, "windows", prefix)
            for key, child in value.items():
                image_detail(child, prefix + key + ".")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                image_detail(child, prefix + str(index) + ".")

    for obj, prefix in objects:
        if "detail" in obj:
            obj["detail"] = "compact"
        if "checks" in obj:
            obj["check_counts"] = {status: sum(c["status"] == status for c in obj["checks"])
                                   for status in ("pass", "fail", "unchecked")}
        if "rows" in obj:
            obj["description_counts"] = {key: len(obj.get(key) or [])
                                         for key in ("rows", "events", "patches", "voices", "fx", "gen")}
        if isinstance(obj.get("canonical"), str):
            obj["canonical_bytes"] = len(obj["canonical"].encode("utf-8"))
        for key in ("canonical", "rows", "events", "patches", "fx", "gen"):
            # Native PLAY event/voice counts are scalar, not a description table.
            if key != "events" or isinstance(obj.get(key), list):
                omit(obj, key, prefix)
        spatial = obj.get("space")
        if isinstance(spatial, dict):
            for key in ("header", "voices"):
                if isinstance(spatial.get(key), (dict, list)):
                    spatial[key + "_count"] = len(spatial[key])
                omit(spatial, key, prefix + "space.")
        images = [(obj.get("image"), prefix + "image."),
                  ((spatial or {}).get("image"), prefix + "space.image."),
                  ((obj.get("whole_mix") or {}).get("image"), prefix + "whole_mix.image.")]
        for image, image_prefix in images:
            image_detail(image, image_prefix)
    if comparison:
        reply["record_counts"] = ({key: len(reply["record"][key]) for key in ("added", "removed", "changed")}
                                  if reply["record"] is not None else None)
    reply.update(detail="compact", full_result=dict(sha256=identity, size=size, detail="full"), omitted=omitted)

    def fits():
        return reply_bytes(reply) <= 15360

    if fits():
        return reply
    # Drop whole groups, never present an incomplete diff/check array as whole.
    for obj, prefix in objects:
        if "description_counts" in obj or obj is reply and comparison and reply["record_counts"] is not None:
            omit(obj, "descriptors", prefix)
    if fits():
        return reply
    for obj, prefix in objects:
        omit(obj, "space", prefix)
        omit(obj, "image", prefix)
    if fits():
        return reply
    if comparison:
        omit(reply, "record", "")
    if fits():
        return reply
    for obj, prefix in objects:
        omit(obj, "checks", prefix)
    if fits():
        return reply
    for obj, prefix in objects:
        for key in ("whole_mix", "play", "metrics", "descriptors", "note_descriptors",
                    "note_profile", "descriptor_profile", "peak_ch", "rms_ch", "crest_ch"):
            omit(obj, key, prefix)
    if fits():
        return reply
    # Caller-controlled paths, source lists or diagnostics cannot defeat the cap.
    fixed = ("hash", "expected", "match", "block_indep", "record_sha256", "wav_sha256",
             "fs", "channels", "samples", "canonical_bytes", "description_counts", "check_counts", "checks_pass")
    minimal = {key: reply[key] for key in ("version", "ok", "exit", "detail", "full_result")}
    minimal.update({key: reply[key] for key in fixed if key in reply})
    minimal["native"] = {"sha256": full["native"]["sha256"]}
    minimal["omitted"] = ["full_result"]
    if comparison:
        minimal.update({key: reply[key] for key in ("same_hash", "same_record", "record_counts")})
        for side in ("a", "b"):
            minimal[side] = {key: reply[side][key] for key in fixed if key in reply[side]}
    require(reply_bytes(minimal) <= 15360, "compact result identity exceeds response budget", 2)
    return minimal


@public
def describe(source, *, harness=None, measure=False, note=None, out=None, detail="compact",
             offset=0, length=4096, expected_result_sha256=None, start_ns=0, end_ns=None,
             window_offset=0, window_limit=8):
    boolean(measure, "measure")
    if note is not None:
        integer(note, "note")
        require(note > 0, "note must be positive integer millihertz", 2)
    detail_options(detail, offset, length, expected_result_sha256, out)
    window_options(detail, measure, start_ns, end_ns, window_offset, window_limit)
    native = Native(harness)
    reply = resolve_source(source, native, measure)
    if out is not None:
        retain(reply, native, out=out)
    reply.update(describe_rows(reply))
    if measure:
        measurement_describe(native, reply, note)
    else:
        reply["whole_mix"] = None
    description_detail(reply, "full")
    return result_detail(reply, detail, offset, length, expected_result_sha256,
                         start_ns=start_ns, end_ns=end_ns, window_offset=window_offset, window_limit=window_limit)


def numeric_deltas(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: numeric_deltas(a.get(k), b.get(k)) for k in sorted(a.keys() | b.keys())}
    if isinstance(a, list) and isinstance(b, list):
        return [numeric_deltas(a[i] if i < len(a) else None, b[i] if i < len(b) else None)
                for i in range(max(len(a), len(b)))]
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return [a, b, b - a]
    if a is None or b is None:
        return [a, b, None]
    return [a, b, 0 if a == b else None]


def numeric_fields(fields):
    """Select the actual native numeric fields, including per-channel arrays."""
    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return {key: value for key, value in fields.items()
            if numeric(value) or isinstance(value, list) and all(numeric(v) for v in value)}


@public
def compare(a, b, *, harness=None, measure=False, note=None, target=None, detail="compact",
            offset=0, length=4096, expected_result_sha256=None, start_ns=0, end_ns=None,
            window_offset=0, window_limit=8):
    if isinstance(target, (list, tuple)):
        target = targets(target)
    boolean(measure, "measure")
    if note is not None:
        integer(note, "note")
        require(note > 0, "note must be positive integer millihertz", 2)
    detail_options(detail, offset, length, expected_result_sha256)
    window_options(detail, measure, start_ns, end_ns, window_offset, window_limit)
    native = Native(harness)
    sides = []
    for source in (a, b):
        path = Path(source)
        with path.open("rb") as stream:
            is_wav = stream.read(4) == b"RIFF"
        if is_wav:
            side = RenderReply(wav_measure(path.read_bytes(), measure, note))
            side["_play"] = numeric_fields(side)
            if measure:
                side["whole_mix"] = clean(side)
            side.update(record=None, record_sha256=None, wav=str(path.resolve()), _rows=None)
        else:
            side = native_render(native, [source], measure)
            if measure:
                measurement_describe(native, side, note)
            else:
                side.update(describe_rows(side))
        sides.append(side)
    left, right = sides
    reply = dict(version=1, ok=True, exit=max(s.get("exit", 0) for s in sides), native=native.identity,
                 a=left, b=right, same_hash=left.get("hash") == right.get("hash") if left.get("hash") and right.get("hash") else None,
                 same_record=left["record_sha256"] == right["record_sha256"] if left["record_sha256"] and right["record_sha256"] else None,
                 play=numeric_deltas(numeric_fields(left["_play"]), numeric_fields(right["_play"])),
                 record=record_diff(left["_rows"], right["_rows"]) if left["_rows"] is not None and right["_rows"] is not None else None,
                 descriptors=numeric_deltas({k: v["values"] for k, v in (left.get("descriptors") or {}).items()},
                                           {k: v["values"] for k, v in (right.get("descriptors") or {}).items()})
                             if measure and left["_rows"] is not None and right["_rows"] is not None else None,
                 space=numeric_deltas({k: v for k, v in (left.get("space") or {}).items() if k in ("voices", "image")},
                                      {k: v for k, v in (right.get("space") or {}).items() if k in ("voices", "image")}),
                 whole_mix=numeric_deltas(left.get("whole_mix"), right.get("whole_mix")) if measure else None)
    if measure and (left["_rows"] is None or right["_rows"] is None):
        descriptor_key = "note_descriptors" if note is not None else "descriptors"
        reply["descriptors"] = numeric_deltas((left.get("whole_mix") or {}).get(descriptor_key),
                                              (right.get("whole_mix") or {}).get(descriptor_key))
        reply["descriptor_profile"] = "whole_mix_original_level"
    requested, _ = checks(reply, target)
    reply["checks"] = [dict(c, name=side_name + "." + c["name"]) for side_name, side in zip(("a", "b"), sides)
                       for c in checks(side)[0]] + requested[2:]
    statuses = [c["status"] for c in reply["checks"]]
    reply["checks_pass"] = False if "fail" in statuses else None if "unchecked" in statuses else True
    for side in sides:
        description_detail(side, "full")
    return result_detail(reply, detail, offset, length, expected_result_sha256,
                         start_ns=start_ns, end_ns=end_ns, window_offset=window_offset, window_limit=window_limit)


def source_log_entry(entry, request, response):
    if response is not None:
        result = response["response"]
        metadata = source_receipt_metadata(dict(state="complete", full_result_sha256=response["full_result_sha256"]), result)
        if "source_artifact" in metadata:
            return dict(entry, source_sha256=result["source_sha256"], source_artifact=metadata["source_artifact"])
        # Before original sources were also copied into full outcomes, compose
        # kept their exact JSON in the request receipt.
        inline = request.get("request", {})
        args, kwargs = inline.get("args", []), inline.get("kwargs", {})
        if (request.get("operation") == "compose" and type(args) is list and type(kwargs) is dict
                and len(args) <= 1 and (len(args) == 1) != ("score" in kwargs)
                and kwargs.get("source_ref") is None and isinstance(result.get("source_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", result["source_sha256"])):
            return dict(entry, source_sha256=result["source_sha256"], source_artifact=dict(kind="score",
                digest=result["source_sha256"], filename=entry["request_sha256"] + ".json"))
    return entry


@public
def log_show(directory, *, kind="takes", offset=0, limit=20, expected_index_sha256=None, detail="compact"):
    require(kind in ("takes", "operations"), "kind must be takes or operations", 2)
    require(detail in ("compact", "full"), "log detail must be compact or full", 2)
    integer(offset, "offset")
    integer(limit, "limit")
    require(offset >= 0 and 1 <= limit <= 100, "log offset must be nonnegative and limit must be 1..100", 2)
    require(expected_index_sha256 is None or isinstance(expected_index_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected_index_sha256),
            "expected_index_sha256 must be a lowercase SHA256", 2)
    if kind == "operations":
        require(detail == "compact", "operations support compact detail only", 2)
        journal = receipts_module()
        try:
            return journal.show(directory, offset=offset, limit=limit,
                                expected_index_sha256=expected_index_sha256, project_entry=source_log_entry)
        except journal.ReceiptError as error:
            raise ToolError(str(error), error.exit, **error.details) from error
    require(detail != "full" or offset == 0 and limit == 20 and expected_index_sha256 is None,
            "full take log does not support paging arguments", 2)
    path = existing_log(directory)
    with locked(path, create=False) as path:
        rows = read_log(path)
        if detail == "full":
            text = workspace_path(path / "log.tsv").read_text() if rows else "\t".join(LOG_COLUMNS) + "\n"
            return dict(version=1, ok=True, exit=0, count=len(rows), rows=rows, text=text)
        data = workspace_path(path / "log.tsv").read_bytes()
    identity = sha(data)
    require(expected_index_sha256 is None or expected_index_sha256 == identity,
            "take index SHA256 differs; restart discovery", 2)
    omitted = ["note", "changed", "space"]
    selected = [dict(row, **{key: None for key in omitted}, omitted=omitted)
                for row in rows[offset:offset + limit]]
    reply = dict(version=1, ok=True, exit=0, kind="takes", detail="compact", count=len(rows),
                 index_sha256=identity, rows=selected, text=None, omitted=["text"],
                 artifact=dict(kind="take_log", digest=identity))
    while True:
        count = len(selected)
        reply["page"] = dict(offset=offset, limit=limit, total=len(rows), returned=count,
                            next_offset=offset + count if offset + count < len(rows) else None)
        require(count > 0 or offset >= len(rows), "take log row exceeds response budget", 2)
        try:
            fits = reply_bytes(reply) <= 15360
        except (ValueError, OverflowError) as error:
            raise ToolError("take log metadata exceeds response budget", 2) from error
        require(count > 0 or fits, "take log metadata exceeds response budget", 2)
        if fits:
            return reply
        selected.pop()


def existing_log(directory):
    path = workspace_path(directory)
    require((path / "log.tsv").is_file(), "take log does not exist", 5)
    return path


def replay_options(positional, arguments):
    native_detail_options(positional, arguments)
    if arguments.get("detail", "full") == "compact":
        directory = positional[0] if positional else arguments.get("directory")
        if isinstance(directory, (str, Path)):
            try:
                resolved = Path(directory).resolve()
            except (OSError, ValueError):
                return  # Preserve the operation's ordinary path error mapping.
            native_detail_options((), dict(detail="compact", directory=str(resolved)))


def replay_counts(full):
    rows = full.get("takes")
    if not isinstance(rows, list):
        return None  # An existing abort can discard a completed prefix.
    def verdicts(key):
        values = [row.get(key) for row in rows]
        return dict(true=sum(value is True for value in values),
                    false=sum(value is False for value in values),
                    unknown=sum(value is not True and value is not False for value in values))
    identity = dict(equal=0, different=0, unknown=0)
    for row in rows:
        left, right = row.get("record_sha256"), row.get("rendered_record_sha256")
        key = "unknown" if left is None or right is None else "equal" if left == right else "different"
        identity[key] += 1
    return dict(returned=len(rows), exit_0=sum(row.get("exit") == 0 for row in rows),
                exit_3=sum(row.get("exit") == 3 for row in rows), match=verdicts("match"),
                block_indep=verdicts("block_indep"), canonical_identity=identity)


def replay_minimal(full):
    """Only fixed known fields survive exceptional diagnostics/encoding failures."""
    native = full.get("native")
    identity = native.get("sha256") if isinstance(native, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        identity = None
    count = full.get("count")
    reply = dict(version=1, ok=full.get("ok") if type(full.get("ok")) is bool else None,
                exit=full.get("exit") if type(full.get("exit")) is int else None,
                detail="compact", native=dict(sha256=identity),
                count=count if type(count) is int else None, takes=None,
                replay_counts=full.get("replay_counts") if "replay_counts" in full else replay_counts(full),
                full_result_sha256=full.get("full_result_sha256"),
                full_result_bytes=full.get("full_result_bytes"),
                omitted=["takes", "diagnostics", "native.path", "native.engine", "other_fields"])
    if full.get("full_result_retention") in ("unknown", "unavailable"):
        reply["full_result_retention"] = full["full_result_retention"]
    if isinstance(full.get("operation_result"), dict):
        reply["operation_result"] = replay_minimal(full["operation_result"])
    return reply


def replay_outcome(full, arguments, operation):
    if arguments.get("detail", "full") == "full":
        return full
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        inner = replay_minimal(full["operation_result"])
        inner["full_result_retention"] = full["operation_result"].get("full_result_retention", "unknown")
        reply = dict(full, detail="compact", operation_result=inner, full_result_retention="unknown", omitted=[])
        try:
            reply.get("message", "").encode("utf-8")
        except UnicodeError:
            reply.update(message=None, omitted=["message"])
        if reply_bytes(reply) > 15360:
            reply.update(message=None, omitted=["message"])
        return reply
    try:
        document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError):
        # Preserve the known execution outcome without inventing exact bytes or
        # letting a terminal presentation exception escape the public operation.
        inner = replay_minimal(full)
        inner.update(full_result_sha256=None, full_result_bytes=None, full_result_retention="unavailable")
        return dict(version=1, ok=False, exit=5, detail="compact",
                    message="replay outcome cannot be encoded as exact canonical UTF8 JSON",
                    operation_result=inner, full_result_sha256=None, full_result_bytes=None,
                    full_result_retention="unavailable", omitted=["unencodable_full_result"])
    reply = dict(full, detail="compact", takes=None, count=full.get("count"),
                 replay_counts=replay_counts(full), full_result_sha256=sha(document),
                 full_result_bytes=len(document), omitted=["takes"] if "takes" in full else [])
    if reply_bytes(reply) > 15360:
        for key in ("message", "stdout", "stderr", "refusals"):
            if key in reply:
                reply[key] = None
                reply["omitted"].append(key)
    if reply_bytes(reply) > 15360:
        reply = replay_minimal(reply)
    result = OperationReply(reply)
    result.full_result_bytes = document
    return result


@public(preflight=replay_options, outcome=replay_outcome)
def log_replay(directory, *, harness=None, detail="full"):
    path = existing_log(directory)
    with locked(path, create=False) as path:
        rows = read_log(path)
        if not rows:
            raise ToolError("cannot replay an empty take log", 2, count=0, takes=[])
        native = Native(harness)
        results = []
        for row in rows:
            result = native_render(native, [path / "records" / (row["record_sha256"] + ".tl")])
            require(result["expected"] == row["hash"] and str(result["fs"]) == row["fs"],
                    "logged expected identity differs from the retained record", 5)
            if result["exit"] == 0:
                require(result["record_sha256"] == row["record_sha256"], "replayed canonical identity differs", 1)
            results.append(dict(take=int(row["take"]), record_sha256=row["record_sha256"],
                                rendered_record_sha256=result["record_sha256"], exit=result["exit"],
                                hash=result["hash"], expected=result["expected"], match=result["match"],
                                block_indep=result["block_indep"]))
    return dict(version=1, ok=all(r["exit"] in (0, 3) for r in results), exit=max((r["exit"] for r in results), default=0),
                native=native.identity, count=len(results), takes=results)


@public(preflight=native_detail_options, outcome=native_outcome)
def check_patch(files=None, *, text=None, harness=None, patch_name=None, fs=48000, detail="full"):
    """Check a complete input pool or a named patch fragment through C."""
    integer(fs, "fs")
    if patch_name is not None:
        require(isinstance(patch_name, str) and patch_name and not any(c.isspace() for c in patch_name),
                "patch_name must be one token", 2)
    with input_files(files, text) as inputs:
        native = Native(harness)
        if patch_name is None:
            return native_render(native, inputs)
        duration = (10 ** 9 + fs - 1) // fs if fs > 0 else 1
        with tempfile.TemporaryDirectory(prefix="fm-check-") as temporary:
            header = Path(temporary) / "context.tl"
            header.write_text("\n".join(("timeline draft1", "engine " + native.metadata.engine,
                                         f"fs {fs}", f"length_ns {duration}", "streams check",
                                         f"voice 0 {patch_name}")) + "\n", encoding="utf-8")
            return native_render(native, [header, *inputs])


def existing_command(args):
    """Preserve an existing host program's UTF-8 stdout, stderr and exit."""
    result = subprocess.run([sys.executable, "-B", *map(str, args)], capture_output=True, text=True,
                            encoding="utf-8", cwd=ROOT)
    return dict(version=1, ok=result.returncode == 0, exit=result.returncode,
                stdout=result.stdout, stderr=result.stderr)


@public
def analyze(wav, *, mode="overview", out=None, detail="compact"):
    """Run existing audio analysis; spectrogram output requires a PNG path."""
    require(mode in ("overview", "loudness", "spectro"), "analyze mode must be overview, loudness or spectro", 2)
    require(detail in ("compact", "full"), "analyze detail must be compact or full", 2)
    require(mode == "spectro" or out is None, "out applies only to spectro", 2)
    require(mode != "spectro" or out is not None, "spectro requires out", 2)
    args = [ROOT / "tools/music/analyze.py", mode, Path(wav).resolve()]
    if out is not None:
        require(Path(out).suffix.lower() == ".png", "spectro out must have a .png extension", 2)
        raw_destination = Path(out).absolute()
        require(not os.path.lexists(raw_destination), "spectro output already exists; choose a new path", 2)
        destination = workspace_path(raw_destination)
        require(destination != workspace_path(wav), "spectro output must differ from its input", 2)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".spectro-pending-", dir=destination.parent) as staging:
            staged = Path(staging) / "output.png"
            result = existing_command([*args, staged])
            for field in ("stdout", "stderr"):
                if isinstance(result.get(field), str):
                    result[field] = result[field].replace(str(staged), str(destination))
            if not result["ok"]:
                return result
            data = staged.read_bytes()
            require(data.startswith(b"\x89PNG\r\n\x1a\n"), "analyzer output is not a PNG", 5)
            # Exclusive hard-link publication is atomic and cannot replace a
            # pre-existing file or follow a destination symlink after validation.
            os.link(staged, destination)
    else:
        result = existing_command(args)
    if result["ok"] and out is not None:
        data = Path(out).read_bytes()
        require(data.startswith(b"\x89PNG\r\n\x1a\n"), "analyzer output is not a PNG", 5)
        import base64
        result.update(png=str(Path(out).resolve()), png_sha256=sha(data),
                      png_base64=base64.b64encode(data).decode("ascii") if detail == "full" else None,
                      mime_type="image/png")
        if detail == "compact":
            result.update(detail=detail, png_bytes=len(data), omitted=["png_base64"],
                          artifact=dict(directory=str(Path(out).parent.resolve()), kind="png",
                                        digest=result["png_sha256"], filename=Path(out).name))
    return result


def _find_describe_worker(harness, query):
    """Run only in an isolated child: retain the analyzer's original output."""
    try:
        native = Native(harness)
        analysis, compose = analysis_module(), compose_module()
        compose.ENGINE = native.metadata.engine
        compose.build = lambda: None  # Native already verified the selected executable.

        def play(files, wav=None, merged=None):
            expected_header = document_header(files)
            with tempfile.TemporaryDirectory(prefix="fm-find-body-") if merged else contextlib.nullcontext() as temporary:
                args = ["--play"]
                if wav:
                    args += ["--wav", wav]
                if merged:
                    body_path = Path(temporary) / "body.tl"
                    args += ["--merged", merged, "--merged-body", body_path]
                result = native.run([*args, "--", *files])
                if merged and result.returncode in (0, 3):
                    verify_native_document(read_native_document(merged), read_native_document(body_path), expected_header)
            lines = [line for line in result.stdout.splitlines() if line.startswith("PLAY ")]
            if result.returncode == 5:
                raise ToolError(result.stderr.strip() or "native render failed", 5)
            if not lines:
                sys.exit("harness failed: %s %s" % (result.stdout, result.stderr))
            return result.returncode, dict(word.split("=", 1) for word in lines[0].split()[1:]), lines[0]

        compose.play = play
        analysis.music_compose = lambda: compose
        with tempfile.TemporaryDirectory(prefix="fm-find-") as temporary:
            analysis.DESC_DIR = str(Path(temporary) / "describe")
            return analysis.cmd_describe(["--json", "--", *query])
    except ToolError as error:
        print(str(error), file=sys.stderr)
        return error.exit
    except (OSError, subprocess.SubprocessError, ImportError) as error:
        print(str(error), file=sys.stderr)
        return 5


@public
def find_patch(query, *, tags=None, tsv=None, mode="search", harness=None):
    """Return existing search or recipe-description output unchanged."""
    require(mode in ("search", "describe"), "find_patch mode must be search or describe", 2)
    if mode == "describe":
        require(len(query) == 1 and not tags and not tsv, "describe needs one recipe and no tag/tsv filters", 2)
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);import fm;"
                "sys.exit(fm._find_describe_worker(sys.argv[2],json.loads(sys.argv[3])))")
        return existing_command(["-c", code, ROOT / "tools", Path(harness or DEFAULT_HARNESS).resolve(),
                                 json.dumps(query)])
    args = [ROOT / "tools/music/analyze.py", "search"]
    for tag in tags or []:
        args += ["--tag", tag]
    for path in tsv or []:
        args += ["--tsv", Path(path).resolve()]
    return existing_command([*args, "--", *query])


def _load_patch_full(name, patch):
    """Keep the legacy loader's expansion, successful object and error mapping."""
    compose = compose_module()
    require(name in compose.RECIPES, "unknown recipe " + name, 2)
    require(isinstance(patch, str) and not any(c.isspace() for c in patch), "patch name must be one token", 2)
    with tempfile.TemporaryDirectory(prefix="fm-load-") as temporary:
        source, output = Path(temporary) / "source.tl", Path(temporary) / "expanded.tl"
        source.write_text(f"recipe {patch} {name}\n", encoding="utf-8")
        try:
            compose.expand_timeline(str(source), str(output))
        except SystemExit as error:
            raise ToolError(str(error), 1) from error
        data = output.read_bytes()
    return dict(version=1, ok=True, exit=0, name=name, patch=patch,
                text=data.decode("utf-8"), sha256=sha(data))


def _load_patch_minimal(reply):
    """Keep bounded loader identities/status; omit arbitrary diagnostic groups."""
    fixed = ("version", "ok", "exit", "detail", "sha256", "total_bytes",
             "offset", "length", "next_offset", "eof")
    result = {key: reply[key] for key in fixed if key in reply}
    if isinstance(reply.get("operation_result"), dict):
        result["operation_result"] = _load_patch_minimal(reply["operation_result"])
    if isinstance(reply.get("receipt"), dict):
        result["receipt"] = reply["receipt"]
    omitted = sorted(set(reply.get("omitted", [])) | (set(reply) - set(result) - {"omitted"}))
    result.update(message="load_patch page outcome omitted oversized fields", omitted=omitted)
    if reply_bytes(result) > 15360:
        # Many or unusually long diagnostic field names are one omitted group.
        result.update(omitted=["additional_fields"], omitted_field_count=len(omitted))
    return result


def load_patch_outcome(reply, arguments, operation):
    """Bound only the new loader mode, including optional receipt uncertainty."""
    if arguments.get("detail", "full") == "full":
        return reply
    detail = "page" if arguments.get("detail") == "page" else None
    result = dict(reply, detail=detail)
    try:
        # ensure_ascii=True alone would hide invalid runtime Unicode.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError):
        result = dict(version=1, ok=False, exit=5, detail=detail,
                      message="load_patch outcome cannot be encoded as strict UTF8 JSON",
                      omitted=["unencodable_outcome"])
        # Receipt completion can fail with an unencodable outer diagnostic
        # after a valid, bounded page has already been computed.
        for key in ("operation_result", "receipt", "sha256", "total_bytes"):
            if key not in reply:
                continue
            try:
                value = reply[key]
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if key == "operation_result" and isinstance(value, dict) and reply_bytes({key: value}) > 15360:
                    value = _load_patch_minimal(value)
                if reply_bytes({key: value}) <= 15360:
                    result[key] = value
            except (UnicodeError, ValueError, TypeError):
                pass
    if reply_bytes(result) <= 15360:
        return result
    omitted = list(result.get("omitted", []))
    for key in ("message", "name", "patch"):
        if key in result and result[key] is not None:
            result[key] = None
            omitted.append(key)
    result["omitted"] = omitted
    if reply_bytes(result) <= 15360:
        return result
    if isinstance(result.get("operation_result"), dict):
        inner = dict(result["operation_result"])
        if inner.get("text") is not None:
            inner["text"] = None
            inner["omitted"] = [*inner.get("omitted", []), "text"]
        result["operation_result"] = inner
    if reply_bytes(result) <= 15360:
        return result
    # ToolError may carry diagnostic groups unknown to this operation. A closed
    # identity/status projection prevents those groups bypassing the budget.
    return _load_patch_minimal(result)


@public(outcome=load_patch_outcome)
def load_patch(name, *, patch="patch", detail="full", offset=0, length=4096,
               expected_sha256=None):
    """Expand a recipe once; optionally return a guarded, stateless UTF8 page."""
    require(isinstance(detail, str) and detail in ("full", "page"),
            "detail must be full or page", 2)
    if detail == "full":
        require(type(offset) is int and offset == 0 and type(length) is int and length == 4096
                and expected_sha256 is None, "paging options require detail=page", 2)
        return _load_patch_full(name, patch)
    require(isinstance(name, str) and isinstance(patch, str),
            "name and patch must be strings", 2)
    try:
        name.encode("utf-8")
        patch.encode("utf-8")
    except UnicodeError as error:
        raise ToolError("name and patch must contain valid UTF8", 2) from error
    integer(offset, "offset")
    integer(length, "length")
    require(offset >= 0 and 1 <= length <= 8192,
            "offset must be nonnegative and length must be 1..8192", 2)
    require(expected_sha256 is None or isinstance(expected_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
            "expected_sha256 must be a lowercase SHA256 or null", 2)
    require(offset == 0 or expected_sha256 is not None,
            "continuation offset requires expected_sha256", 2)
    try:
        full = _load_patch_full(name, patch)
        data = full["text"].encode("utf-8")
    except ToolError:
        raise
    except (Exception, SystemExit) as error:
        raise ToolError(str(error), 5) from error
    identity = sha(data)
    if expected_sha256 is not None and expected_sha256 != identity:
        raise ToolError("expanded patch SHA256 differs; restart loading", 2,
                        sha256=identity, total_bytes=len(data))
    base = dict(version=1, ok=True, exit=0, detail="page", name=name, patch=patch,
                sha256=identity, total_bytes=len(data), offset=offset)
    try:
        return bounded_text_page(data, base, length, "expanded patch")
    except ToolError as error:
        raise ToolError(str(error), error.exit,
                        **dict(error.details, sha256=identity, total_bytes=len(data))) from error
    except (Exception, SystemExit) as error:
        raise ToolError(str(error), 5, sha256=identity, total_bytes=len(data)) from error


def catalog_module():
    spec = importlib.util.spec_from_file_location("fm_catalog", ROOT / "tools/catalog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def session_module():
    spec = importlib.util.spec_from_file_location("fm_session", ROOT / "tools/session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@public
def session_export(sources, out):
    return session_module().session_export(sources, out, api=sys.modules[__name__])


@public
def session_verify(directory, *, expected_manifest_sha256=None):
    return session_module().session_verify(directory, expected_manifest_sha256=expected_manifest_sha256,
                                         api=sys.modules[__name__])


def midi_projection(full):
    """Retain exact child evidence; bound delivery without changing its outcome."""
    document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    keys = ("version", "ok", "exit", "operation", "dry_run", "name", "fs", "end_ns", "length_ns",
            "source", "native", "counts", "render_attempts", "output", "record", "wav", "hash",
            "message", "native_returncode")
    reply = {key: full[key] for key in keys if key in full}
    reply.update(detail="compact", published=full.get("output") is not None,
                 full_result_sha256=sha(document), full_result_bytes=len(document),
                 omitted=sorted(set(full) - set(keys)))
    reply["channel_summary"] = [dict(channel=row["channel"], mapped=row["mapped"],
        notes=row["statistics"].get("notes", 0), skipped=row["statistics"].get("skipped", 0))
        for row in full.get("channels", [])]
    for key, kind in (("record", "record"), ("wav", "wav")):
        item = reply.get(key)
        if item is not None and full.get("output") is not None:
            relative = Path(item["path"])
            reply[key] = dict(item, artifact=dict(directory=str(Path(full["output"]) / relative.parent),
                kind=kind, digest=item["sha256"], filename=relative.name))
    # Keep ordinary paths for convenient use. Extreme metadata can be read from
    # the opted-in full receipt, without misreporting a completed publication.
    if reply_bytes(reply) > 15360:
        for key in ("source", "native", "record", "wav"):
            value = reply.get(key)
            if isinstance(value, dict) and "path" in value:
                reply[key] = {field: item for field, item in value.items() if field not in ("path", "artifact")}
                reply["omitted"].append(key + ".path")
                if "artifact" in value:
                    reply["omitted"].append(key + ".artifact")
        for key in ("name", "output", "message"):
            if key in reply:
                reply[key] = None
                reply["omitted"].append(key)
    if reply_bytes(reply) > 15360:
        # Counter keys are normally fixed by the converter. Retain the outcome
        # even if a future converter adds unusually verbose statistics.
        reply["counts"] = None
        reply["omitted"].append("counts")
    require(reply_bytes(reply) <= 15360, "MIDI result metadata exceeds response budget", 5)
    answer = OperationReply(reply)
    answer.full_result_bytes = document
    return answer


def strict_json(text):
    """Reject ambiguous keys and every nonfinite JSON spelling, including overflow."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("nonfinite JSON value")
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def child_result(command, request, operation):
    """One strict process/result contract for isolated host adapters."""
    try:
        result = subprocess.run(command, input=json.dumps(request, allow_nan=False), capture_output=True, text=True)
    except UnicodeError as error:
        raise ToolError("adapter child returned invalid text", 5, publication_state="unknown") from error
    try:
        full = strict_json(result.stdout)
    except (ValueError, TypeError):
        full = None
    valid = (isinstance(full, dict) and type(full.get("version")) is int and full["version"] == 1
             and full.get("operation") == operation and type(full.get("exit")) is int
             and full["exit"] in range(6) and type(full.get("ok")) is bool
             and full["ok"] == (full["exit"] == 0) and result.returncode == full["exit"])
    if valid and operation == "transcribe":
        complete, reason, output = full.get("complete"), full.get("stop_reason"), full.get("output")
        valid = type(complete) is bool and "output" in full
        if full["exit"] == 0:
            valid = valid and ((complete and reason == "complete" and isinstance(output, str) and bool(output))
                or (complete is False and reason == "budget" and output is None
                    and type(full.get("native_budget")) is int and full["native_budget"] >= 0
                    and type(full.get("budget_remaining")) is int and full["budget_remaining"] == 0
                    and full.get("blocked_operation") in ("--retrig", "--play")))
        else:
            valid = valid and complete is False and reason == "error" and output is None
    if not valid:
        label = "MIDI" if operation == "midi_import" else "transcription"
        raise ToolError(label + " child failed to return matching structured status", 5,
                        child_returncode=result.returncode, publication_state="unknown")
    return full


@public
def midi_import(source, *, options=None, out=None, dry_run=True, harness=None):
    """Run the existing MIDI converter in an isolated, non-building child."""
    boolean(dry_run, "dry_run")
    for name, value in (("source", source), ("out", out), ("harness", harness)):
        require(name != "source" and value is None or isinstance(value, (str, Path))
                and bool(str(value)) and "\0" not in str(value), name + " must be a nonempty path", 2)
    require(options is None or isinstance(options, list)
            and all(isinstance(value, str) and "\0" not in value for value in options),
            "options must be a list of argument strings", 2)
    require(not dry_run or out is None and harness is None, "dry run forbids out and harness", 2)
    require(dry_run or out is not None, "conversion requires out", 2)
    request = dict(source=str(source), options=options if options is not None else [],
                   out=str(out) if out is not None else None, dry_run=dry_run,
                   harness=str(Path(harness or DEFAULT_HARNESS).resolve()) if not dry_run else None)
    return midi_projection(child_result([sys.executable, "-B", str(ROOT / "tools/music/midi2tl.py"),
                                         "--agent-json"], request, "midi_import"))


def transcribe_projection(full):
    """Bound delivery while receipts keep the exact completed decoder evidence."""
    document = json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    keys = ("version", "ok", "exit", "operation", "source", "native", "output", "note_count",
            "score", "replay", "costs", "record", "wav", "limitations", "message", "native_returncode",
            "complete", "stop_reason", "native_budget", "budget_remaining", "blocked_operation", "note_preview",
            "draft")
    reply = {key: full[key] for key in keys if key in full}
    reply.update(detail="compact", published=full.get("output") is not None,
                 full_result_sha256=sha(document), full_result_bytes=len(document),
                 omitted=sorted(set(full) - set(keys)))
    for key, kind in (("record", "record"), ("wav", "wav")):
        item = reply.get(key)
        if item is not None and full.get("output") is not None:
            path = Path(full["output"]) / item["path"]
            reply[key] = dict(item, artifact=dict(directory=str(path.parent), kind=kind,
                digest=item["sha256"], filename=path.name))
    if reply_bytes(reply) > 15360:
        for key in ("source", "native", "record", "wav"):
            item = reply.get(key)
            if isinstance(item, dict):
                reply[key] = {name: value for name, value in item.items() if name not in ("path", "artifact")}
                reply["omitted"].extend(key + "." + name for name in ("path", "artifact") if name in item)
        for key in ("output", "message"):
            if key in reply:
                reply[key] = None
                reply["omitted"].append(key)
    if reply_bytes(reply) > 15360:
        for key in ("limitations", "score", "replay", "note_preview"):
            if key in reply:
                reply[key] = None
                reply["omitted"].append(key)
    require(reply_bytes(reply) <= 15360, "transcription result metadata exceeds response budget", 5)
    result = OperationReply(reply)
    result.full_result_bytes = document
    return result


def transcribe_request(request):
    require(isinstance(request, dict) and set(request) <= {"source", "out", "options", "harness", "native_budget", "draft"}
            and {"source", "out", "harness"} <= set(request),
            "transcription request requires source/out/harness and optional options/native_budget", 2)
    for name in ("source", "out", "harness"):
        value = request[name]
        require(isinstance(value, str) and value and "\0" not in value,
                name + " must be a nonempty path", 2)
    options = request.get("options", [])
    require(isinstance(options, list) and all(isinstance(value, str) and "\0" not in value for value in options),
            "options must be a list of argument strings", 2)
    owned = {"--source", "--wav", "--out", "--harness", "--native-budget", "--help", "-h", "--"}
    require(not any(token.split("=", 1)[0] in owned for token in options),
            "source, out, harness, native-budget, help and -- are adapter-owned; read transcribe-options", 2)
    budget = request.get("native_budget")
    require(budget is None or type(budget) is int and budget >= 0,
            "native_budget must be a nonnegative integer or null", 2)
    draft = request.get("draft")
    require(draft is None or isinstance(draft, dict) and set(draft) == {"tool", "tool_version", "strict", "min_snr_db"}
            and isinstance(draft["tool"], str) and (draft["tool_version"] is None or isinstance(draft["tool_version"], str))
            and type(draft["strict"]) is bool and type(draft["min_snr_db"]) in (int, float)
            and math.isfinite(draft["min_snr_db"]),
            "draft must be null or tool/tool_version/strict/min_snr_db", 2)
    return dict(request, options=options, native_budget=budget)


class TranscriptionBudgetStop(Exception):
    """Normal incomplete stop outside the decoder's failure catch classes."""


def transcribe_note_preview(report):
    """Copy existing completed segment measurements without new analysis."""
    notes = report["notes"]
    keys = ("index", "start_ns", "estimated_off_ns", "frequency_millihz", "recipe", "carrier_level_db10")
    preview = [dict({key: note[key] for key in keys}, local_snr_db=note["score"].get("snr_db"))
               for note in notes[:16]]
    return dict(total=len(notes), shown=len(preview), truncated=len(notes) > len(preview), notes=preview)


TRANSCRIBE_DRAFT_LIMITATIONS = ("mono only: one note at a time; chords and polyphony are not transcribed",
                                "timbre is approximated by the nearest built-in recipes",
                                "effects, stereo and physical note-offs are not recovered")


def transcribe_draft(report, draft, dependencies, staging, options, name):
    """wav2tl only: warnings from existing measurements, the strict gate, and provenance.json."""
    snr = report["score"].get("snr_db")
    warnings = []
    if snr is None or snr < draft["min_snr_db"]:
        warnings.append("whole-signal SNR %s is below %g dB" % (
            "unavailable (%s)" % report["score"].get("snr_reason") if snr is None else "%.2f dB" % snr,
            draft["min_snr_db"]))
    if report["omitted_regions"]:
        warnings.append("%d audible region(s) were omitted from the draft (%s)" % (
            len(report["omitted_regions"]), ", ".join(sorted({row["reason"] for row in report["omitted_regions"]}))))
    if draft["strict"] and warnings:
        # Exit 4 is the existing failed-gate status; nothing is published.
        raise ToolError("strict draft check failed: " + "; ".join(warnings), 4,
                        draft=dict(warnings=warnings, snr_db=snr, strict=True, min_snr_db=draft["min_snr_db"]))
    engine = next((line.split(None, 1)[1].strip() for line in
                   (staging / "canonical.tl").read_text(encoding="utf-8").splitlines()
                   if line.startswith("engine ")), None)
    roles = {item["role"]: item for item in dependencies}
    # Basename and digests only: no absolute paths, host names or clock times.
    provenance = dict(version=1, derived="audio-transcription", draft=True, mode=report["mode"],
                      source_sha256=roles["source"]["sha256"], source_bytes=roles["source"]["bytes"],
                      source_name=name, tool=draft["tool"], tool_version=draft["tool_version"],
                      engine=engine, native_sha256=roles["native"]["sha256"],
                      implementation={role: item["sha256"] for role, item in roles.items()
                                      if role not in ("source", "native")},
                      options=options, snr_db=snr, limitations=list(TRANSCRIBE_DRAFT_LIMITATIONS),
                      warnings=warnings)
    with open(staging / "provenance.json", "x", encoding="utf-8") as stream:
        stream.write(json.dumps(clean(provenance), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
    return dict(provenance=clean(provenance), file="provenance.json", warnings=warnings, snr_db=snr,
                strict=draft["strict"], min_snr_db=draft["min_snr_db"])


def _transcribe_worker(request, result, stdout, stderr):
    """Run only in a fresh child; neither service renderer nor caches are mutated."""
    dependencies = result["dependencies"]
    def identity(path, role):
        path = Path(path).resolve()
        # Bounded blocks: re-checking a long source never holds another full copy.
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(1 << 20), b""):
                digest.update(data)
                size += len(data)
        return dict(role=role, path=str(path), sha256=digest.hexdigest(), bytes=size)
    def unchanged():
        for item in dependencies:
            require(identity(item["path"], item["role"]) == item,
                    "transcription dependency changed: " + item["role"], 5)
    try:
        source, native = Path(request["source"]).resolve(), Path(request["harness"]).resolve()
        raw_destination = Path(request["out"]).absolute()
        require(not os.path.lexists(raw_destination), "transcription output already exists", 2)
        destination = raw_destination.resolve()
        for path, role in ((source, "source"), (native, "native"), (ROOT / "tools/fm.py", "adapter"),
                           (ROOT / "tools/music/analyze.py", "analysis"),
                           (ROOT / "tools/music/channel.py", "channel"),
                           (ROOT / "tools/music/compose.py", "composer"),
                           (ROOT / "data/recipes.txt", "recipes"),
                           (ROOT / "data/tables.txt", "tables"),
                           (ROOT / "src/gen/params.def", "generator_schema")):
            dependencies.append(identity(path, role))
        result.update(source=dependencies[0], native=dependencies[1], options=request["options"])
        analysis = analysis_module()
        parser = analysis.transcribe_parser(allow_abbrev=False)
        try:
            arguments = parser.parse_args([str(source), *request["options"],
                                           "--out", str(destination), "--harness", str(native)])
        except SystemExit as error:
            raise ToolError("invalid transcription options; read transcribe-options", 2) from error
        settings = vars(arguments).copy()
        for key in ("wav", "out", "harness"):
            settings.pop(key)
        # This file is read lazily by transcribe, only for descriptor selection.
        if arguments.recipes is None:
            dependencies.append(identity(analysis.DESC_TSV, "descriptors"))
        # Invalid numeric options still belong to a structured decoder refusal.
        # Their original argv strings remain exact when JSON requires a null.
        result["normalized_options"] = clean(settings)
        compose = analysis.music_compose()
        unchanged()
        saved_harness, saved_run = compose.H, subprocess.run
        def observed(command, *args, **kwargs):
            require(isinstance(command, (list, tuple)) and len(command) >= 2
                    and str(command[0]) == str(native) and command[1] in ("--retrig", "--play"),
                    "unexpected command during isolated transcription", 5)
            budget = request["native_budget"]
            if budget is not None and result["costs"]["total_attempts"] >= budget:
                result.update(blocked_operation=command[1], blocked_argv=list(map(str, command)))
                raise TranscriptionBudgetStop()
            key = "config_attempts" if command[1] == "--retrig" else "play_attempts"
            result["costs"][key] += 1
            result["costs"]["total_attempts"] += 1
            result["budget_remaining"] = None if budget is None else budget - result["costs"]["total_attempts"]
            try:
                completed = saved_run(command, *args, **kwargs)
            except (OSError, UnicodeError) as error:
                result["native_calls"].append(dict(argv=list(map(str, command)), returncode=None, error=str(error)))
                raise ToolError("native transcription I/O failed: " + str(error), 5) from error
            result["native_calls"].append(dict(argv=list(map(str, command)), returncode=completed.returncode,
                                                stdout=completed.stdout, stderr=completed.stderr))
            if completed.returncode:
                status = completed.returncode if completed.returncode in (1, 2, 3, 4, 5) else 5
                # ToolError is outside the decoder's ValueError/OSError/SystemExit
                # catch lists, preserving the actual native status and call.
                raise ToolError("native transcription failed", status, native_returncode=completed.returncode)
            return completed
        with session_module().destination_lock(sys.modules[__name__], destination):
            require(not os.path.lexists(destination), "transcription output already exists", 2)
            # Staging names carry this destination's tag. Under its lock no live
            # worker can own a tagged directory, so one left by a killed run is swept.
            pending = ".transcribe-pending-" + hashlib.sha256(str(destination).encode()).hexdigest()[:16] + "-"
            for stale in destination.parent.glob(pending + "*"):
                with contextlib.suppress(OSError):
                    if stat.S_ISDIR(stale.lstat().st_mode) and stale.lstat().st_uid == os.geteuid():
                        shutil.rmtree(stale)
            staging = Path(tempfile.mkdtemp(prefix=pending, dir=destination.parent))
            try:
                compose.H, subprocess.run = str(native), observed
                report = analysis.jsonable(analysis.transcribe(str(source), str(staging), cp=compose, **settings))
                result["costs"]["template_render_requests"] = report["native"]["template_render_requests"]
                draft = None if request.get("draft") is None else transcribe_draft(
                    report, request["draft"], dependencies, staging, result["normalized_options"],
                    Path(request["source"]).name)
                artifacts = [dict(path=path.name, sha256=sha(path.read_bytes()), bytes=path.stat().st_size)
                             for path in sorted(staging.iterdir()) if path.is_file()]
                record = next(row for row in artifacts if row["path"] == "canonical.tl")
                wav = next(row for row in artifacts if row["path"] == "rerender.wav")
                replay = dict(exit=report["replay"]["exit"], exact_pcm=report["replay"]["exact_pcm"])
                replay.update({key: report["replay"]["play"].get(key)
                               for key in ("hash", "match", "block_indep", "clipped")})
                complete = dict(result, ok=True, exit=0, complete=True, stop_reason="complete",
                                output=str(destination), artifacts=artifacts,
                                record=record, wav=wav, report=report, note_count=len(report["notes"]),
                                note_preview=transcribe_note_preview(report),
                                score=report["score"], replay=replay, limitations=report["limitations"],
                                diagnostics=dict(stdout=stdout.getvalue(), stderr=stderr.getvalue()))
                if draft is not None:
                    complete["draft"] = draft
                # Prepare full serialization and bounded metadata before publication.
                transcribe_projection(complete)
                unchanged()
                require(not os.path.lexists(destination), "transcription output already exists", 2)
                os.rename(staging, destination)
                result.clear()
                result.update(complete)
            finally:
                compose.H, subprocess.run = saved_harness, saved_run
                if staging.exists():
                    shutil.rmtree(staging)
    except (Exception, SystemExit):
        # Decoder provenance refusals are ValueError internally; an actual input
        # change remains a host5 outcome even when the decoder stopped early.
        unchanged()
        raise


def _transcribe_child(text):
    result = dict(version=1, ok=False, exit=2, operation="transcribe", output=None,
                  complete=False, stop_reason="error", native_budget=None, budget_remaining=None,
                  blocked_operation=None, blocked_argv=None, note_preview=None,
                  source=None, native=None, note_count=None, score=None, replay=None,
                  record=None, wav=None, limitations=None, report=None, dependencies=[],
                  native_calls=[], costs=dict(config_attempts=0, play_attempts=0, total_attempts=0,
                                               template_render_requests=None))
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            try:
                request = strict_json(text)
            except (ValueError, TypeError) as error:
                raise ToolError("invalid transcription request JSON: " + str(error), 2) from error
            request = transcribe_request(request)
            result.update(native_budget=request["native_budget"], budget_remaining=request["native_budget"])
            _transcribe_worker(request, result, stdout, stderr)
        except TranscriptionBudgetStop:
            result.update(ok=True, exit=0, complete=False, stop_reason="budget")
        except ToolError as error:
            result.update(exit=error.exit, message=str(error), **error.details)
        except (OSError, ImportError, subprocess.SubprocessError) as error:
            result.update(exit=5, message=str(error))
        except MemoryError:
            result.update(exit=5, message="transcription ran out of memory; use a shorter source or a lower --max-seconds")
        except (ValueError, wave.Error, EOFError, SystemExit) as error:
            result.update(exit=2, message=str(error) or "malformed or truncated WAV input (%s)" % type(error).__name__)
    if not result["complete"]:
        result["costs"]["template_render_requests"] = None
    result["diagnostics"] = dict(stdout=stdout.getvalue(), stderr=stderr.getvalue())
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result["exit"]


@public
def transcribe(source, out, *, options=None, harness=None, native_budget=None, draft=None):
    """Run accepted approximate mono transcription in an isolated, staged child.

    draft (wav2tl only) adds draft warnings, an optional strict gate before
    publication, and a provenance.json sidecar in the published directory."""
    try:
        for name, value in (("source", source), ("out", out), ("harness", harness)):
            require(name == "harness" and value is None or isinstance(value, (str, Path))
                    and bool(str(value)) and "\0" not in str(value), name + " must be a nonempty path", 2)
        request = transcribe_request(dict(source=str(source), out=str(out), options=[] if options is None else options,
                                          harness=str(Path(harness or DEFAULT_HARNESS).resolve()), native_budget=native_budget,
                                          **({} if draft is None else dict(draft=draft))))
        return transcribe_projection(child_result([sys.executable, "-B", str(ROOT / "tools/fm.py"),
                                                   "--transcribe-json"], request, "transcribe"))
    except ToolError as error:
        details = dict(error.details, complete=False, stop_reason="error")
        if details.get("publication_state") != "unknown":
            details["published"] = False
        raise ToolError(str(error), error.exit, **details) from error


@public
def catalog_build(**kwargs):
    destination = Path(kwargs["out"]).absolute()
    require(not os.path.lexists(destination), "catalog output already exists; choose a new path", 2)
    destination = workspace_path(destination)
    for source in kwargs.get("records") or []:
        require(workspace_path(source) != destination, "catalog output must differ from its input", 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".catalog-pending-", dir=destination.parent) as staging:
        staged = Path(staging) / "catalog.tsv"
        result = catalog_module().catalog_build(api=sys.modules[__name__], **dict(kwargs, out=staged))
        if result.get("ok"):
            os.link(staged, destination)
            result["out"] = str(destination)
        return result


@public
def search(ranges=(), **kwargs):
    return catalog_module().search(ranges, api=sys.modules[__name__], **kwargs)


def revision_detail(result, detail):
    """Project a completed revision once; receipts retain its exact full bytes."""
    full = clean(result)
    document = json.dumps(full, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    def finish(value):
        answer = OperationReply(value)
        answer.full_result_bytes = document
        return answer

    if detail == "full":
        return finish(full)
    reply = clean(full)
    omitted = []
    counts = {key: len(full[key]) if full[key] is not None else None
              for key in ("moves", "skipped", "history", "sweeps", "nearest", "excluded", "zero_distance_ties")
              if key in full}
    if isinstance(full.get("sweeps"), list):
        counts.update(complete_sweeps=sum(bool(row.get("complete")) for row in full["sweeps"]),
                      incomplete_sweeps=sum(not row.get("complete") for row in full["sweeps"]))

    def omit(obj, key, prefix=""):
        if key in obj and obj[key] is not None:
            obj[key] = None
            omitted.append(prefix + key)

    for key in ("moves", "skipped", "history", "sweeps"):
        omit(reply, key)
    reply.update(detail="compact", full_result_sha256=sha(document),
                 full_result_bytes=len(document), result_counts=counts, omitted=omitted)

    def fits():
        return reply_bytes(reply) <= 15360

    if fits():
        return finish(reply)
    # Catalog rows repeat the corpus provenance; retain their ranked values first.
    removed_provenance = False
    for row in reply.get("nearest") or []:
        if isinstance(row, dict) and row.get("provenance") is not None:
            row["provenance"] = None
            removed_provenance = True
    if removed_provenance:
        omitted.append("nearest[].provenance")
    if fits():
        return finish(reply)
    # Remove complete groups, never disguise a partial ranking or sweep as full.
    for group in (("zero_distance_ties", "excluded"), ("nearest",),
                  ("anchor", "space"), ("error", "message", "reason", "missing")):
        for key in group:
            omit(reply, key)
        if fits():
            return finish(reply)
    for key in ("source", "log", "path", "patch"):
        omit(reply, key)
    if isinstance(reply.get("native"), dict):
        omit(reply["native"], "path", "native.")
    if isinstance(reply.get("accepted"), dict):
        for key in ("record", "move"):
            omit(reply["accepted"], key, "accepted.")
    if fits():
        return finish(reply)
    # One last measurement remains useful; accepted.measurement duplicates it.
    if isinstance(reply.get("accepted"), dict):
        omit(reply["accepted"], "measurement", "accepted.")
    for key in ("baseline", "measurements"):
        omit(reply, key)
        if fits():
            return finish(reply)
    # Arbitrarily long caller paths, diagnostics and names cannot defeat the cap.
    fixed = ("version", "ok", "exit", "detail", "full_result_sha256", "full_result_bytes",
             "result_counts", "budget", "consumed_renders", "render_attempts", "render_counts",
             "reached", "stop_reason", "baseline_distance", "target", "tolerance", "scales")
    minimal = {key: reply[key] for key in fixed if key in reply}
    if isinstance(full.get("native"), dict):
        minimal["native"] = {key: full["native"][key] for key in ("sha256",) if key in full["native"]}
    if "accepted" in full:
        minimal["accepted"] = ({key: full["accepted"][key]
                                for key in ("record_sha256", "hash", "take", "parent_record_sha256", "distance")
                                if key in full["accepted"]} if full["accepted"] is not None else None)
    for key in ("moves", "skipped", "history", "sweeps", "path", "measurements"):
        if key in full:
            minimal[key] = None
    minimal["omitted"] = ["full_result"]
    require(reply_bytes(minimal) <= 15360, "revision result identity exceeds response budget", 2)
    return finish(minimal)


@public
def probe_plan(source, *, patch=None, note=None, parent_record=None, harness=None):
    return catalog_module().probe_plan(source, patch=patch, note=note,
        parent_record=parent_record, harness=harness, api=sys.modules[__name__])


@public
def suggest(target, *, detail="compact", **kwargs):
    require(detail in ("compact", "full"), "detail must be compact or full", 2)
    return revision_detail(catalog_module().suggest(target, api=sys.modules[__name__], **kwargs), detail)


@public
def seek(target, *, detail="compact", **kwargs):
    require(detail in ("compact", "full"), "detail must be compact or full", 2)
    return revision_detail(catalog_module().seek(target, api=sys.modules[__name__], **kwargs), detail)


@public
def selftest(*, out, harness=None):
    path = Path(out).resolve()
    require(not path.is_relative_to(ROOT), "selftest --out must be outside the repository", 2)
    reports, outputs, statuses = {}, {}, []
    suites = (("toolkit", ROOT / "tests/toolkit/check.py"),
              ("catalog", ROOT / "tests/catalog/check.py"),
              ("seek", ROOT / "tests/seek/check.py"))
    for name, checker in suites:
        destination = path / name
        result = subprocess.run([sys.executable, "-B", str(checker), "--repo", str(ROOT),
                                 "--harness", str(harness or DEFAULT_HARNESS), "--out", str(destination)],
                                capture_output=True, text=True)
        report = destination / "results.json"
        reports[name] = json.loads(report.read_text()) if report.exists() else None
        outputs[name] = dict(stdout=result.stdout, stderr=result.stderr, exit=result.returncode)
        statuses.append(result.returncode)
    failure = next((status for status in statuses if status), 0)
    return dict(version=1, ok=not failure, exit=failure, report=reports.get("toolkit"),
                reports=reports, suites=outputs, stdout="".join(value["stdout"] for value in outputs.values()),
                stderr="".join(value["stderr"] for value in outputs.values()))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--transcribe-json"]:
        return _transcribe_child(sys.stdin.read())
    if argv == ["--transcribe-options"]:
        sys.stdout.write(analysis_module().transcribe_parser().format_help())
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", type=Path, default=DEFAULT_HARNESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--out", dest="selftest_out", type=Path)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("params").add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    reference_parser = commands.add_parser("reference")
    reference_parser.add_argument("--source", choices=tuple(REFERENCE_SPECS), default="instrument")
    reference_parser.add_argument("--section")
    reference_parser.add_argument("--offset", type=int, default=0)
    reference_parser.add_argument("--length", type=int, default=4096)
    reference_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    generator_parser = commands.add_parser("gen-events")
    generator_parser.add_argument("files", nargs="*")
    generator_parser.add_argument("--text")
    generator_parser.add_argument("--offset", type=int, default=0)
    generator_parser.add_argument("--length", type=int, default=4096)
    generator_parser.add_argument("--expected-manifest-sha256")
    generator_parser.add_argument("--detail", choices=("page", "summary"), default="page")
    generator_parser.add_argument("--start-ns", type=int)
    generator_parser.add_argument("--end-ns", type=int)
    generator_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    render_parser = commands.add_parser("render")
    render_parser.add_argument("files", nargs="*")
    render_parser.add_argument("--text")
    render_parser.add_argument("--out", type=Path)
    render_parser.add_argument("--wav", action="store_true")
    render_parser.add_argument("--log", type=Path)
    render_parser.add_argument("--parent-record")
    render_parser.add_argument("--note-text", default="")
    render_parser.add_argument("--target", action="append")
    render_parser.add_argument("--detail", choices=("compact", "full"), default="full")
    render_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    compose_parser = commands.add_parser("compose")
    compose_input = compose_parser.add_mutually_exclusive_group(required=True)
    compose_input.add_argument("--score", type=Path)
    compose_input.add_argument("--source-ref", type=Path, help="JSON file containing an exact retained score locator")
    compose_parser.add_argument("--log", required=True, type=Path)
    compose_parser.add_argument("--receipt-dir", required=True, type=Path)
    compose_parser.add_argument("--receipt-parent")
    compose_parser.add_argument("--receipt-purpose", default="")
    compose_parser.add_argument("--parent-record")
    compose_parser.add_argument("--note-text", default="")
    compose_parser.add_argument("--target", action="append")
    compose_parser.add_argument("--detail", choices=("compact", "full"), default="compact")
    compose_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for command in ("score-inspect", "score-revise"):
        sub = commands.add_parser(command)
        choice = sub.add_mutually_exclusive_group(required=True)
        choice.add_argument("--score", type=Path)
        choice.add_argument("--source-ref", type=Path)
        sub.add_argument("--receipt-dir", type=Path, required=True)
        sub.add_argument("--receipt-parent")
        sub.add_argument("--receipt-purpose", default="")
        sub.add_argument("--detail", choices=("compact", "full"), default="compact")
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if command == "score-inspect":
            sub.add_argument("--start", default="0", help="quarter coordinate, integer or rational")
            sub.add_argument("--end", help="quarter coordinate, integer or rational")
        else:
            sub.add_argument("--expected-source-sha256", required=True)
            sub.add_argument("--pointer", required=True)
            sub.add_argument("--expected", type=Path, required=True, help="JSON file containing the exact old value")
            sub.add_argument("--value", type=Path, required=True, help="JSON file containing the replacement value")
    section_parser = commands.add_parser("measure-section")
    section_parser.add_argument("files", nargs="*")
    section_parser.add_argument("--text")
    section_parser.add_argument("--start-ns", type=int, default=0)
    section_parser.add_argument("--end-ns", type=int)
    section_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sections_parser = commands.add_parser("measure-sections")
    sections_parser.add_argument("files", nargs="*")
    sections_parser.add_argument("--text")
    sections_parser.add_argument("--sections-json", required=True, help="ordered JSON array of start_ns/end_ns intervals")
    sections_parser.add_argument("--detail", choices=("compact", "full"), default="compact")
    sections_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    voice_sections_parser = commands.add_parser("measure-voice-sections")
    voice_sections_parser.add_argument("files", nargs="*")
    voice_sections_parser.add_argument("--text")
    voice_sections_parser.add_argument("--voices-json", required=True, help="ordered JSON array of distinct native voice indices")
    voice_sections_parser.add_argument("--sections-json", required=True, help="ordered JSON array of start_ns/end_ns intervals")
    voice_sections_parser.add_argument("--detail", choices=("compact", "full"), default="compact")
    voice_sections_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for command in ("describe", "compare"):
        sub = commands.add_parser(command)
        sub.add_argument("files", nargs=1 if command == "describe" else 2)
        sub.add_argument("--measure", action="store_true")
        sub.add_argument("--note", type=int)
        sub.add_argument("--detail", choices=("compact", "summary", "full", "page", "windows"))
        sub.add_argument("--offset", type=int, default=0)
        sub.add_argument("--length", type=int, default=4096)
        sub.add_argument("--expected-result-sha256")
        sub.add_argument("--start-ns", type=int, default=0)
        sub.add_argument("--end-ns", type=int)
        sub.add_argument("--window-offset", type=int, default=0)
        sub.add_argument("--window-limit", type=int, default=8)
        sub.add_argument("--receipt-dir", type=Path)
        sub.add_argument("--receipt-parent")
        sub.add_argument("--receipt-purpose", default="")
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if command == "describe":
            sub.add_argument("--out", type=Path)
        else:
            sub.add_argument("--target", action="append", help="measured dotted key and value, e.g. a.clipped=0 or b.clipped=0; unavailable keys are unchecked")
    log_parser = commands.add_parser("log")
    log_parser.add_argument("action", choices=("show", "replay"))
    log_parser.add_argument("directory", type=Path)
    log_parser.add_argument("--kind", choices=("takes", "operations"), default="takes")
    log_parser.add_argument("--offset", type=int, default=0)
    log_parser.add_argument("--limit", type=int, default=20)
    log_parser.add_argument("--expected-index-sha256")
    log_parser.add_argument("--detail", choices=("compact", "full"))
    log_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    artifact_parser = commands.add_parser("artifact_read")
    artifact_parser.add_argument("directory", type=Path)
    artifact_parser.add_argument("kind", choices=("record", "wav", "png", "midi", "sfz", "zip", "receipt", "take_log", "session_manifest", "score", *TEXT_ARTIFACT_KINDS))
    artifact_parser.add_argument("digest")
    artifact_parser.add_argument("--offset", type=int, default=0)
    artifact_parser.add_argument("--length", type=int, default=8192)
    artifact_parser.add_argument("--filename")
    artifact_parser.add_argument("--encoding", choices=("base64", "utf8"), default="base64")
    artifact_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    export_parser = commands.add_parser("session_export")
    export_parser.add_argument("sources", nargs="+")
    export_parser.add_argument("--out", required=True, type=Path)
    export_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    verify_parser = commands.add_parser("session_verify")
    verify_parser.add_argument("directory", type=Path)
    verify_parser.add_argument("--expected-manifest-sha256")
    verify_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    midi_parser = commands.add_parser("midi_import")
    midi_parser.add_argument("source")
    midi_parser.add_argument("--option", action="append", default=[], help="one converter argv token; use --option=--flag for flags")
    midi_parser.add_argument("--render", action="store_true", help="convert and render instead of the default dry run")
    midi_parser.add_argument("--out", type=Path)
    midi_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    transcribe_parser = commands.add_parser("transcribe")
    transcribe_parser.add_argument("source")
    transcribe_parser.add_argument("--out", type=Path, required=True)
    transcribe_parser.add_argument("--option", action="append", default=[], help="one transcriber argv token; use --option=--flag for flags; discover transcribe-options")
    transcribe_parser.add_argument("--native-budget", type=int, help="maximum attempted native CONFIG/play calls; exhausted budget returns incomplete work without publication")
    transcribe_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    tl2wav_parser = commands.add_parser("tl2wav", help="render .tl files or a piece directory to a new WAV (exact; never overwrites)")
    tl2wav_parser.add_argument("inputs", nargs="+", metavar="IN", help="a .tl file, several .tl files, or one piece directory")
    tl2wav_parser.add_argument("-o", "--out", "--output", dest="output", type=Path, required=True, metavar="OUT.wav")
    tl2wav_parser.add_argument("--allow-silent", action="store_true", help="write the WAV even when the render is silent")
    tl2wav_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    wav2tl_parser = commands.add_parser("wav2tl", help="draft an approximate mono TL piece from a WAV (CLI only; review before sharing; never overwrites)")
    wav2tl_parser.add_argument("source", metavar="IN.wav")
    wav2tl_parser.add_argument("-o", "--out", dest="out", type=Path, required=True, metavar="OUT_DIR", help="new piece directory")
    wav2tl_parser.add_argument("--strict", action="store_true", help="exit 4 and publish nothing when the draft has warnings (SNR below --min-snr-db or omitted regions)")
    wav2tl_parser.add_argument("--min-snr-db", type=float, default=0.0, help="whole-signal SNR warning threshold; default 0 dB (no better than silence)")
    wav2tl_parser.add_argument("--option", action="append", default=[], help="one transcriber argv token, as for transcribe; default adds --recipe-candidates 8")
    wav2tl_parser.add_argument("--native-budget", type=int, help="maximum attempted native CONFIG/play calls, shared with the fallback attempt")
    wav2tl_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    check_parser = commands.add_parser("check_patch")
    check_parser.add_argument("files", nargs="*")
    check_parser.add_argument("--text")
    check_parser.add_argument("--patch-name")
    check_parser.add_argument("--fs", type=int, default=48000)
    check_parser.add_argument("--detail", choices=("compact", "full"), default="full")
    check_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("wav", type=Path)
    analyze_parser.add_argument("--mode", choices=("overview", "loudness", "spectro"), default="overview")
    analyze_parser.add_argument("--out", type=Path)
    analyze_parser.add_argument("--detail", choices=("compact", "full"), default="compact")
    analyze_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    find_parser = commands.add_parser("find_patch")
    find_parser.add_argument("query", nargs="*")
    find_parser.add_argument("--mode", choices=("search", "describe"), default="search")
    find_parser.add_argument("--tag", action="append")
    find_parser.add_argument("--tsv", action="append")
    find_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    load_parser = commands.add_parser("load_patch")
    load_parser.add_argument("name")
    load_parser.add_argument("--patch", default="patch")
    load_parser.add_argument("--detail", choices=("full", "page"), default="full")
    load_parser.add_argument("--offset", type=int, default=0)
    load_parser.add_argument("--length", type=int, default=4096)
    load_parser.add_argument("--expected-sha256")
    load_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    catalog_parser = commands.add_parser("catalog")
    catalog_commands = catalog_parser.add_subparsers(dest="catalog_action", required=True)
    catalog_build_parser = catalog_commands.add_parser("build")
    catalog_build_parser.add_argument("--out", required=True, type=Path)
    catalog_build_parser.add_argument("--record", action="append")
    catalog_build_parser.add_argument("--batch-size", type=int, default=16)
    catalog_build_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    search_parser = commands.add_parser("search")
    search_parser.add_argument("ranges", nargs="*")
    search_parser.add_argument("--tag", action="append", default=[])
    search_parser.add_argument("--tsv", action="append")
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    suggest_parser = commands.add_parser("suggest")
    suggest_parser.add_argument("--target", action="append", required=True)
    suggest_parser.add_argument("--from", dest="source")
    suggest_parser.add_argument("--patch")
    suggest_parser.add_argument("--probe", action="store_true")
    suggest_parser.add_argument("--among", type=Path)
    suggest_parser.add_argument("--note", type=int)
    suggest_parser.add_argument("--tsv", action="append")
    suggest_parser.add_argument("--offset", type=int, default=0)
    suggest_parser.add_argument("--limit", type=int, default=20)
    suggest_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    plan_parser = commands.add_parser("probe_plan")
    plan_parser.add_argument("source")
    plan_parser.add_argument("--patch")
    plan_parser.add_argument("--note", type=int)
    plan_parser.add_argument("--parent-record")
    plan_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    seek_parser = commands.add_parser("seek")
    seek_parser.add_argument("--target", action="append", required=True)
    seek_parser.add_argument("--from", dest="source", required=True)
    seek_parser.add_argument("--log", type=Path, required=True)
    seek_parser.add_argument("--budget", type=int, required=True)
    seek_parser.add_argument("--tol", action="append")
    seek_parser.add_argument("--patch")
    seek_parser.add_argument("--note", type=int)
    seek_parser.add_argument("--tsv", action="append")
    seek_parser.add_argument("--parent-record")
    seek_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for sub in (render_parser, check_parser, analyze_parser, search_parser, suggest_parser, seek_parser, plan_parser, midi_parser, transcribe_parser, log_parser, generator_parser, section_parser, sections_parser):
        sub.add_argument("--receipt-dir", type=Path)
        sub.add_argument("--receipt-parent")
        sub.add_argument("--receipt-purpose", default="")
    for sub in (suggest_parser, seek_parser):
        sub.add_argument("--detail", choices=("compact", "full"), default="compact")
    args = parser.parse_args(argv)
    receipt_options = dict(receipt_dir=getattr(args, "receipt_dir", None),
                           receipt_parent=getattr(args, "receipt_parent", None),
                           receipt_purpose=getattr(args, "receipt_purpose", ""))
    if args.selftest:
        if args.command or args.selftest_out is None:
            parser.error("--selftest requires --out DIR and no subcommand")
        result = selftest(out=args.selftest_out, harness=args.harness)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return result["exit"]
    if args.command is None or args.selftest_out is not None:
        parser.error("a subcommand is required; global --out is only for --selftest")
    try:
        if args.command == "params":
            result = params(harness=args.harness)
        elif args.command == "reference":
            result = reference(source=args.source, section=args.section, offset=args.offset,
                               length=args.length, harness=args.harness)
        elif args.command == "gen-events":
            selection = {}
            if args.detail != "page":
                selection["detail"] = args.detail
            if args.start_ns is not None:
                selection["start_ns"] = args.start_ns
            if args.end_ns is not None:
                selection["end_ns"] = args.end_ns
            result = gen_events(args.files or None, text=args.text, harness=args.harness,
                                offset=args.offset, length=args.length,
                                expected_manifest_sha256=args.expected_manifest_sha256, **selection, **receipt_options)
        elif args.command == "render":
            result = render(args.files, text=args.text, harness=args.harness, out=args.out, wav=args.wav, log=args.log,
                            parent_record=args.parent_record, note_text=args.note_text, target=args.target,
                            detail=args.detail, **receipt_options)
        elif args.command == "compose":
            source = dict(score=score_file(args.score)) if args.score else dict(source_ref=score_file(args.source_ref))
            result = compose(**source, log=args.log, harness=args.harness,
                             parent_record=args.parent_record, note_text=args.note_text, target=args.target,
                             detail=args.detail, **receipt_options)
        elif args.command in ("score-inspect", "score-revise"):
            source = dict(score=score_file(args.score)) if args.score else dict(source_ref=score_file(args.source_ref))
            if args.command == "score-inspect":
                def quarter(value):
                    return int(value) if value is not None and re.fullmatch(r"-?[0-9]+", value) else value
                result = score_inspect(**source, start=quarter(args.start), end=quarter(args.end),
                                       detail=args.detail, **receipt_options)
            else:
                result = score_revise(**source, expected_source_sha256=args.expected_source_sha256,
                                      pointer=args.pointer, expected=score_file(args.expected), value=score_file(args.value),
                                      detail=args.detail, **receipt_options)
        elif args.command == "measure-section":
            result = measure_section(args.files or None, text=args.text, harness=args.harness,
                                     start_ns=args.start_ns, end_ns=args.end_ns, **receipt_options)
        elif args.command in ("measure-sections", "measure-voice-sections"):
            def section_object(pairs):
                result = {}
                for key, value in pairs:
                    require(key not in result, "duplicate section JSON key: " + key, 2)
                    result[key] = value
                return result
            try:
                sections = json.loads(args.sections_json, object_pairs_hook=section_object)
            except (ValueError, RecursionError) as error:
                raise ToolError("invalid sections JSON: " + str(error), 2) from error
            if args.command == "measure-voice-sections":
                try:
                    voices = json.loads(args.voices_json)
                except (ValueError, RecursionError) as error:
                    raise ToolError("invalid voices JSON: " + str(error), 2) from error
                result = measure_voice_sections(args.files or None, text=args.text, harness=args.harness,
                    voices=voices, sections=sections, detail=args.detail, **receipt_options)
            else:
                result = measure_sections(args.files or None, text=args.text, harness=args.harness,
                                          sections=sections, detail=args.detail, **receipt_options)
        elif args.command == "describe":
            args.detail = args.detail or ("compact" if args.json else "full")
            result = describe(args.files[0], harness=args.harness, measure=args.measure, note=args.note,
                              out=args.out, detail=args.detail, offset=args.offset, length=args.length,
                              expected_result_sha256=args.expected_result_sha256,
                              start_ns=args.start_ns, end_ns=args.end_ns, window_offset=args.window_offset,
                              window_limit=args.window_limit, **receipt_options)
        elif args.command == "compare":
            result = compare(*args.files, harness=args.harness, measure=args.measure, note=args.note,
                             target=args.target, detail=args.detail or "compact", offset=args.offset,
                             length=args.length, expected_result_sha256=args.expected_result_sha256,
                              start_ns=args.start_ns, end_ns=args.end_ns, window_offset=args.window_offset,
                              window_limit=args.window_limit, **receipt_options)
        elif args.command == "log":
            if args.action == "show":
                require(args.receipt_dir is None and args.receipt_parent is None and args.receipt_purpose == "",
                        "receipt options apply to log replay only", 2)
                args.detail = args.detail or ("full" if args.kind == "takes" and not args.json else "compact")
                result = log_show(args.directory, kind=args.kind, offset=args.offset, limit=args.limit,
                                  expected_index_sha256=args.expected_index_sha256, detail=args.detail)
            else:
                require(args.kind == "takes" and args.offset == 0 and args.limit == 20
                        and args.expected_index_sha256 is None,
                        "log replay supports accepted takes only", 2)
                replay_arguments = dict(detail="compact") if args.detail == "compact" else {}
                result = log_replay(args.directory, harness=args.harness, **replay_arguments, **receipt_options)
        elif args.command == "artifact_read":
            result = artifact_read(args.directory, args.kind, args.digest, offset=args.offset,
                                   length=args.length, filename=args.filename, encoding=args.encoding)
        elif args.command == "session_export":
            result = session_export(args.sources, args.out)
        elif args.command == "session_verify":
            result = session_verify(args.directory, expected_manifest_sha256=args.expected_manifest_sha256)
        elif args.command == "midi_import":
            result = midi_import(args.source, options=args.option, out=args.out, dry_run=not args.render,
                                 harness=args.harness if args.render else None, **receipt_options)
        elif args.command == "transcribe":
            result = transcribe(args.source, args.out, options=args.option, harness=args.harness,
                                native_budget=args.native_budget, **receipt_options)
        elif args.command in ("tl2wav", "wav2tl"):
            sys.modules.setdefault("fm", sys.modules[__name__])
            spec = importlib.util.spec_from_file_location("decode", ROOT / "tools/decode.py")
            sys.modules["decode"] = decode = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(decode)
            if args.command == "tl2wav":
                result = decode.tl2wav(args.inputs, args.output, harness=args.harness, allow_silent=args.allow_silent)
            else:
                result = decode.wav2tl(args.source, args.out, options=args.option, harness=args.harness,
                                       native_budget=args.native_budget, strict=args.strict, min_snr_db=args.min_snr_db)
        elif args.command == "check_patch":
            result = check_patch(args.files, text=args.text, harness=args.harness, patch_name=args.patch_name,
                                 fs=args.fs, detail=args.detail, **receipt_options)
        elif args.command == "analyze":
            result = analyze(args.wav, mode=args.mode, out=args.out, detail=args.detail, **receipt_options)
        elif args.command == "find_patch":
            result = find_patch(args.query, tags=args.tag, tsv=args.tsv, mode=args.mode, harness=args.harness)
        elif args.command == "load_patch":
            options = {} if (args.detail == "full" and args.offset == 0 and args.length == 4096
                             and args.expected_sha256 is None) else dict(
                detail=args.detail, offset=args.offset, length=args.length,
                expected_sha256=args.expected_sha256)
            result = load_patch(args.name, patch=args.patch, **options)
        elif args.command == "catalog":
            result = catalog_build(out=args.out, records=args.record, batch_size=args.batch_size, harness=args.harness)
        elif args.command == "search":
            result = search(args.ranges, tags=args.tag, tsv=args.tsv, offset=args.offset, limit=args.limit, **receipt_options)
        elif args.command == "probe_plan":
            result = probe_plan(args.source, patch=args.patch, note=args.note,
                                parent_record=args.parent_record, harness=args.harness, **receipt_options)
        elif args.command == "seek":
            result = seek(args.target, source=args.source, log=args.log, budget=args.budget, tol=args.tol,
                          patch=args.patch, note=args.note, tsv=args.tsv, harness=args.harness, parent_record=args.parent_record, detail=args.detail, **receipt_options)
        else:
            result = suggest(args.target, source=args.source, patch=args.patch, probe=args.probe,
                             among=args.among, note=args.note, tsv=args.tsv, harness=args.harness,
                             offset=args.offset, limit=args.limit, detail=args.detail, **receipt_options)
    except (ToolError, ValueError) as error:
        result = dict(version=1, ok=False, exit=getattr(error, "exit", 2), message=str(error))
    if not args.json and result["ok"] and args.command == "describe" and args.detail in ("summary", "full"):
        sys.stdout.write(result["canonical"])
        if args.measure:
            print(json.dumps({"whole_mix": result["whole_mix"], "descriptors": result["descriptors"],
                              "descriptors_reason": result.get("descriptors_reason")}, allow_nan=False))
    elif not args.json and result["ok"] and args.command == "log" and args.action == "show" and args.kind == "takes" and args.detail == "full":
        sys.stdout.write(result["text"])
    elif not args.json and args.command == "tl2wav" and "decode" in sys.modules:
        sys.modules["decode"].report(result, sys.stdout, sys.stderr)
    elif args.command == "wav2tl" and "decode" in sys.modules:
        sys.modules["decode"].wav2tl_report(result, sys.stdout, sys.stderr, args.json)
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
