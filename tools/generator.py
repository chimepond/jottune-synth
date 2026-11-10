# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""'s isolated native schedule adapter; no generator or TL interpreter.

The caller supplies the existing fm module. No shared runner policy is changed.
execute returns the computation; outcome/project alone apply delivery policy.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace

MAX_FILES = 17
MAX_INPUT = 4 * 1024 * 1024
MAX_STDOUT = 16 * 1024 * 1024
MAX_STDERR = 64 * 1024
CHILD_SECONDS = 30.0
CLEANUP_SECONDS = 1.0
PUBLIC_BYTES = 15360
RECEIPT_BYTES = 900
# 's selected writer uses uint32/%u and unsigned long long/%llu.
# Bound decimal token width before int, independent of Python digit policy.
_HEADER = re.compile(r"GEN_EVENTS version=1 fs=([0-9]{1,10}) ctl=([0-9]{1,10})\Z")
_FOOTER = re.compile(r"GEN_EVENTS count=([0-9]{1,20})\Z")
_EVENT = re.compile(r"GEN_EVENT frame=([0-9]{1,20}) time_ns=([0-9]{1,20}) "
                    r"lane=([0-9]{1,10}) voice=([0-9]{1,10}) kind=(on|off) "
                    r"freq_millihz=([0-9]{1,10}) velocity=([0-9]{1,10})\Z")


def preflight(fm, positional, arguments):
    """Cheap request validation, before receipt setup or filesystem/native work."""
    fm.require(len(positional) <= 1, "gen_events accepts at most one positional files argument", 2)
    fm.require(not (positional and "files" in arguments), "files was supplied twice", 2)
    files = positional[0] if positional else arguments.get("files")
    text = arguments.get("text")
    fm.require((files is None) != (text is None), "supply exactly one of files or text", 2)
    if files is not None:
        fm.require(isinstance(files, list) and 1 <= len(files) <= MAX_FILES,
                   "files must be a nonempty list of at most 17 occurrences", 2)
        for value in files:
            fm.require(isinstance(value, (str, Path)) and bool(str(value)),
                       "each file must be a nonempty string or Path", 2)
    else:
        fm.require(isinstance(text, str) and bool(text.strip()), "text must be a nonblank string", 2)
    for key in ("harness", "receipt_dir"):
        value = arguments.get(key)
        fm.require(value is None or isinstance(value, (str, Path)), key + " must be a string or Path", 2)

    def utf8(value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as error:
                raise fm.ToolError("arguments must contain valid UTF8 strings", 2) from error
        elif isinstance(value, dict):
            for key, child in value.items():
                utf8(key)
                utf8(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                utf8(child)
    utf8(positional)
    utf8(arguments)
    if text is not None:
        fm.require(len(text.encode("utf-8")) <= MAX_INPUT, "text exceeds 4MiB input limit", 2)
    offset, length = arguments.get("offset", 0), arguments.get("length", 4096)
    fm.integer(offset, "offset")
    fm.integer(length, "length")
    fm.require(offset >= 0 and 1 <= length <= 8192,
               "offset must be nonnegative and length must be 1..8192", 2)
    guard = arguments.get("expected_manifest_sha256")
    fm.require(guard is None or isinstance(guard, str) and re.fullmatch(r"[0-9a-f]{64}", guard),
               "expected_manifest_sha256 must be a lowercase SHA256", 2)
    fm.require(offset == 0 or guard is not None, "offset > 0 requires expected_manifest_sha256", 2)
    detail = arguments.get("detail", "page")
    fm.require(detail in ("page", "summary"), "detail must be page or summary", 2)
    start, end = arguments.get("start_ns"), arguments.get("end_ns")
    for name, value in (("start_ns", start), ("end_ns", end)):
        if value is not None:
            fm.integer(value, name)
            fm.require(0 <= value <= 2**64 - 1, name + " must fit uint64", 2)
    fm.require(end is None or end >= (start or 0), "end_ns must not precede start_ns", 2)
    fm.require(detail != "page" or start is None and end is None,
               "time bounds require detail=summary", 2)
    fm.require(detail != "summary" or offset == 0 and length == 4096,
               "summary requires default offset=0 and length=4096", 2)


def _capture(fm, data, complete):
    identity = fm.sha(data)
    try:
        text, encoded = data.decode("utf-8"), None
    except UnicodeError:
        text, encoded = None, base64.b64encode(data).decode("ascii")
    return dict(text=text, base64=encoded, encoding="utf8" if text is not None else "base64",
                bytes=len(data), captured_sha256=identity, complete=complete,
                sha256=identity if complete else None)


class BoundedRunner:
    """One operation's child deadline, attempt counters and exact binary captures."""
    def __init__(self, fm):
        self.fm = fm
        self.deadline = None
        self.calls = dict(discovery=0, manifest=0, total=0)
        self.processes = []

    def run(self, path, args, identity):
        fm = self.fm
        fm.require(fm.sha(path.read_bytes()) == identity["sha256"],
                   "native executable changed during operation", 5)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise fm.ToolError("aggregate child deadline exhausted before spawn", 5)
        category = "manifest" if args[0] == "--gen-events" else "discovery"
        record = dict(operation=args[0], native_returncode=None, cancellation=None)
        captures = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": MAX_STDOUT, "stderr": MAX_STDERR}
        child, selector = None, selectors.DefaultSelector()
        reason = None
        try:
            try:
                if self.deadline is None:
                    self.deadline = time.monotonic() + CHILD_SECONDS
                fm.require(time.monotonic() < self.deadline,
                           "aggregate child deadline exhausted before spawn", 5)
                self.calls[category] += 1
                self.calls["total"] += 1
                self.processes.append(record)
                child = subprocess.Popen([str(path), *map(str, args)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                for name in captures:
                    pipe = getattr(child, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map() or child.poll() is None:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "deadline"
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        name = key.data
                        # The extra byte detects overflow without retaining it.
                        allowance = limits[name] - len(captures[name])
                        try:
                            data = os.read(key.fd, min(65536, allowance + 1))
                        except BlockingIOError:
                            continue
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        captures[name].extend(data[:allowance])
                        if len(data) > allowance:
                            reason = name + "_limit"
                            break
                    if reason:
                        break
            except OSError as error:
                reason = "spawn_failed" if child is None else "capture_read_failed"
                record["host_error"] = dict(type=type(error).__name__, message=str(error))
            if reason and child is not None:
                record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
            elif child is not None:
                child.wait(timeout=max(0, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reason = "deadline"
            if child is not None:
                record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
        except BaseException:
            # Also clean up if the host itself is interrupted; do not convert an
            # unrelated programming error or interrupt into a native verdict.
            reason = "host_interrupted"
            if child is not None:
                record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
            raise
        finally:
            try:
                if child is not None:
                    record["native_returncode"] = child.poll()
                closers = [("selector", selector.close)]
                if child is not None:
                    closers = [(name, getattr(child, name).close) for name in captures
                               if getattr(child, name) is not None] + closers
                for stage, close in closers:
                    try:
                        close()
                    except OSError as error:
                        reason = reason or "capture_close_failed"
                        record.setdefault("cleanup_errors", []).append(
                            dict(stage=stage + "_close", type=type(error).__name__, message=str(error)))
            finally:
                # A cleanup error must not erase already captured bytes or make
                # failure projection depend on missing stdout/stderr fields.
                record["cancellation"] = reason
                for name, data in captures.items():
                    record[name] = _capture(fm, bytes(data), reason is None)
        # Preserve captures even when this guard fails after a successful child.
        fm.require(fm.sha(path.read_bytes()) == identity["sha256"],
                   "native executable changed during operation", 5)
        fm.require(reason is None, "native capture cancelled: " + str(reason), 5)
        fm.require(all(record[name]["text"] is not None for name in captures),
                   "native output is not valid UTF8", 5)
        fm.require(record["native_returncode"] is not None and record["native_returncode"] >= 0,
                   "native process terminated unexpectedly", 5)
        return SimpleNamespace(returncode=record["native_returncode"],
                               stdout=record["stdout"]["text"], stderr=record["stderr"]["text"])

    @staticmethod
    def _cancel(child, selector, captures, limits):
        """Stop the owned group, bounded drain/reap, then close in the caller."""
        finish = time.monotonic() + CLEANUP_SECONDS
        errors = []
        def kill_group(sig):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(dict(stage="signal", type=type(error).__name__, message=str(error)))
        try:
            kill_group(signal.SIGTERM)
            kill_at = min(finish, time.monotonic() + 0.2)
            killed = False
            while time.monotonic() < finish:
                now = time.monotonic()
                if not killed and now >= kill_at:
                    kill_group(signal.SIGKILL)
                    killed = True
                if not selector.get_map() and child.poll() is not None and killed:
                    break
                try:
                    ready = selector.select(min(0.02, max(0, finish - now)))
                except OSError as error:
                    errors.append(dict(stage="drain", type=type(error).__name__, message=str(error)))
                    break  # Reaping cannot depend on a working capture selector.
                for key, _ in ready:
                    try:
                        data = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        errors.append(dict(stage="drain", type=type(error).__name__, message=str(error)))
                        selector.unregister(key.fileobj)
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        name = key.data
                        captures[name].extend(data[:max(0, limits[name] - len(captures[name]))])
        finally:
            # Always kill/reap independently of drain failure, even if the host
            # is interrupted while cancelling. The original capture error is
            # retained separately by run; cleanup observations never replace it.
            kill_group(signal.SIGKILL)
            try:
                child.wait(timeout=max(0, finish - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(dict(stage="reap", type=type(error).__name__, message=str(error)))
        return errors


def _snapshots(fm, directory, files, text, inputs):
    paths, total = [], 0
    sources = [("input.tl", text.encode("utf-8"))] if text is not None else [(str(p), None) for p in files]
    for ordinal, (logical, data) in enumerate(sources):
        if data is None:
            fd = os.open(fm.workspace_path(logical), os.O_RDONLY | os.O_NONBLOCK)
            try:
                fm.require(stat.S_ISREG(os.fstat(fd).st_mode), "input occurrence is not a regular file", 2)
                parts, remaining = [], MAX_INPUT - total + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(parts)
            finally:
                os.close(fd)
        total += len(data)
        fm.require(total <= MAX_INPUT, "aggregate inputs exceed 4MiB limit", 2)
        inputs.append(dict(ordinal=ordinal, logical=logical, bytes=len(data), sha256=fm.sha(data)))
        path = directory / (str(ordinal).zfill(2) + ".tl")
        path.write_bytes(data)
        paths.append(path)
    return paths


def _framing(fm, text):
    # splitlines accepts CRLF without rewriting the retained raw output.
    lines = text.splitlines()
    head = _HEADER.fullmatch(lines[0]) if lines else None
    foot = _FOOTER.fullmatch(lines[-1]) if len(lines) > 1 else None
    fm.require(head is not None and foot is not None and
               all(line.startswith("GEN_EVENT ") for line in lines[1:-1]),
               "native generator output has invalid framing", 5)
    try:
        fs, ctl, count = int(head[1]), int(head[2]), int(foot[1])
    except ValueError as error:
        raise fm.ToolError("native generator framing integer cannot be decoded", 5) from error
    fm.require(fs <= 2**32 - 1 and ctl <= 2**32 - 1 and count <= 2**64 - 1,
               "native generator framing integer exceeds writer range", 5)
    fm.require(count == len(lines) - 2, "native generator footer count differs", 5)
    return dict(manifest_complete=True, manifest_version=1, fs=fs, ctl=ctl, event_count=count)


def execute(fm, files=None, *, text=None, harness=None):
    """Return complete computation, including constructor failures and captures."""
    runner = BoundedRunner(fm)
    result = dict(version=1, ok=False, exit=5, failure_stage="snapshot", message=None,
                  inputs=[], input_set_sha256=None, native=None, provenance=None,
                  native_calls=runner.calls, processes=runner.processes, refusals=[],
                  manifest_complete=False, manifest_version=None, manifest_sha256=None,
                  manifest_bytes=None, event_count=None, fs=None, ctl=None)
    # Closure-owned identity survives a constructor exception before assignment.
    class SelectedNative(fm.Native):
        def run(self, args):
            result["native"] = dict(self.identity)
            response = runner.run(self.path, args, self.identity)
            if args[0] != "--gen-events":
                fm.require(response.returncode == 0, "native metadata discovery failed", 5)
            return response
    try:
        with tempfile.TemporaryDirectory(prefix="fm-gen-events-") as temporary:
            paths = _snapshots(fm, Path(temporary), files, text, result["inputs"])
            fm.document_header(paths)
            identity_rows = [{key: row[key] for key in ("ordinal", "bytes", "sha256")}
                             for row in result["inputs"]]
            result["input_set_sha256"] = fm.sha(fm.receipts_module().canonical_bytes(identity_rows))
            result["failure_stage"] = "provenance"
            result["provenance"] = fm.operation_provenance(dict(harness=harness), "gen_events")
            result["provenance"]["sources"]["tools/generator.py"] = fm.sha(Path(__file__).read_bytes())
            result["failure_stage"] = "discovery"
            native = SelectedNative(harness)
            result["native"] = dict(native.identity)
            result["failure_stage"] = "manifest"
            response = native.run(["--gen-events", "--", *paths])
            if response.returncode:
                try:
                    refusal = fm.refusal(response.stderr)
                except ValueError as error:
                    # Optional diagnostic integer extraction must never mask a
                    # completed native failure. Keep raw stderr and the actual
                    # first message; no guessed line, reparsing or global limit.
                    first = response.stderr.strip().splitlines()
                    refusal = dict(file=None, line=None,
                                   message=first[0] if first else "native operation failed")
                    result["diagnostic_error"] = dict(type=type(error).__name__, message=str(error))
                if refusal.get("file") in {str(path) for path in paths}:
                    ordinal = [str(path) for path in paths].index(refusal["file"])
                    refusal.update(ordinal=ordinal, logical=result["inputs"][ordinal]["logical"])
                result.update(exit=response.returncode, message="native generator manifest refused", refusals=[refusal])
            else:
                result.update(_framing(fm, response.stdout))
                capture = runner.processes[-1]["stdout"]
                result.update(ok=True, exit=0, failure_stage=None, manifest_sha256=capture["sha256"],
                              manifest_bytes=capture["bytes"])
    except fm.ToolError as error:
        result.update(ok=False, exit=error.exit if result["failure_stage"] == "snapshot" else 5,
                      message=str(error))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError) as error:
        result.update(ok=False, exit=5, message=str(error), host_error=type(error).__name__)
    return result


def _fixed(full):
    keys = ("version", "ok", "exit", "failure_stage", "manifest_complete", "manifest_version",
            "manifest_sha256", "manifest_bytes", "event_count", "fs", "ctl", "input_set_sha256", "native_calls")
    value = {key: full[key] for key in keys if key in full}
    value["native"] = {"sha256": full["native"].get("sha256")} if full.get("native") else None
    processes = full.get("processes", [])
    value["captures"] = []
    for process in processes:
        row = {key: process.get(key) for key in ("operation", "native_returncode", "cancellation")}
        for name in ("stdout", "stderr"):
            capture = process[name]
            row.update({name + "_" + key: capture[key] for key in ("bytes", "complete", "sha256")})
            row["captured_" + name + "_sha256"] = capture["captured_sha256"]
        value["captures"].append(row)
    return value


def _summary_capture(fm, full, operation):
    """Read one already captured complete stream, never invoke native work."""
    matches = [row for row in full["processes"] if row["operation"] == operation]
    fm.require(len(matches) == 1, "summary needs one complete " + operation + " capture", 5)
    process = matches[0]
    capture = process["stdout"]
    fm.require(process["native_returncode"] == 0 and process["cancellation"] is None
               and capture["complete"] is True and isinstance(capture["text"], str),
               "summary capture is not a complete native success", 5)
    data = capture["text"].encode("utf-8")
    fm.require(len(data) == capture["bytes"] and fm.sha(data) == capture["sha256"],
               "summary capture identity differs", 5)
    return capture["text"]


def _summary_limits(fm, full):
    rows = fm.listing(_summary_capture(fm, full, "--gen-params"))
    limits = {}
    for name, kind, bits in (("lane_id", "u8", 8), ("voice", "u16", 16), ("velocity", "u16", 16)):
        matches = [row for row in rows if row.get("row") == "GEN_PARAM" and row.get("name") == name]
        fm.require(len(matches) == 1, "summary needs unique native " + name + " metadata", 5)
        row = matches[0]
        fm.require(row.get("slot") == name and row.get("kinds") == "0" and row.get("type") == kind
                   and all(isinstance(row.get(key), str) and re.fullmatch(r"[0-9]{1,5}", row[key])
                           for key in ("min", "max")),
                   "summary native " + name + " metadata has invalid bounds/type", 5)
        low, high = int(row["min"]), int(row["max"])
        fm.require(0 <= low <= high < 2**bits, "summary metadata exceeds its unsigned type", 5)
        limits[name] = (low, high)
    return limits


def summarize(fm, full, start_ns=0, end_ns=None):
    """: validate native v1 output, then summarize emitted events only."""
    limits = _summary_limits(fm, full)
    text = _summary_capture(fm, full, "--gen-events")
    framing = _framing(fm, text)
    fm.require(all(full.get(key) == value for key, value in framing.items())
               and full["manifest_sha256"] == fm.sha(text.encode("utf-8"))
               and full["manifest_bytes"] == len(text.encode("utf-8")),
               "summary manifest identity/framing differs", 5)
    fs = framing["fs"]
    fm.require(fs > 0, "summary needs a positive native sample rate", 5)
    lanes = {}
    previous_frame, previous_time, previous_order = None, None, None
    for index, line in enumerate(text.splitlines()[1:-1]):
        match = _EVENT.fullmatch(line)
        fm.require(match is not None, "summary event fields differ at ordinal " + str(index), 5)
        frame, ns, lane, voice = map(int, match.group(1, 2, 3, 4))
        kind, freq, velocity = match[5], int(match[6]), int(match[7])
        fm.require(frame < 2**64 and ns < 2**64
                   and all(value < 2**32 for value in (lane, voice, freq, velocity)),
                   "summary event exceeds native writer width at ordinal " + str(index), 5)
        fm.require(limits["lane_id"][0] <= lane <= limits["lane_id"][1]
                   and limits["voice"][0] <= voice <= limits["voice"][1]
                   and (limits["velocity"][0] <= velocity <= limits["velocity"][1] if kind == "on"
                        else freq == 0 and velocity == 0),
                   "summary event exceeds native field bounds at ordinal " + str(index), 5)
        fm.require(ns == (frame * 10**9 + fs - 1) // fs and ns * fs // 10**9 == frame,
                   "summary frame/time differs at ordinal " + str(index), 5)
        order = (int(kind == "on"), lane)
        fm.require(previous_frame is None or frame >= previous_frame and ns >= previous_time
                   and (frame != previous_frame or order > previous_order),
                   "summary native event ordering differs at ordinal " + str(index), 5)
        previous_frame, previous_time, previous_order = frame, ns, order
        if lane not in lanes:
            lanes[lane] = dict(lane=lane, total_event_count=0, selected_event_count=0,
                               on_count=0, off_count=0, distinct_on_pitch_count=0,
                               on_freq_millihz_min=None, on_freq_millihz_max=None,
                               on_velocity_min=None, on_velocity_max=None, first_on=None, last_on=None,
                               _pitches=set())
        row = lanes[lane]
        row["total_event_count"] += 1
        # Every row is checked before this selection, including excluded rows.
        if ns < start_ns or end_ns is not None and ns >= end_ns:
            continue
        row["selected_event_count"] += 1
        row[kind + "_count"] += 1
        if kind == "on":
            row["_pitches"].add(freq)
            for field, value in (("on_freq_millihz", freq), ("on_velocity", velocity)):
                row[field + "_min"] = value if row[field + "_min"] is None else min(row[field + "_min"], value)
                row[field + "_max"] = value if row[field + "_max"] is None else max(row[field + "_max"], value)
            endpoint = dict(event_index=index, frame=frame, time_ns=ns, voice=voice)
            if row["first_on"] is None:
                row["first_on"] = endpoint
            row["last_on"] = endpoint
    result = [lanes[lane] for lane in sorted(lanes)]
    for row in result:
        row["distinct_on_pitch_count"] = len(row.pop("_pitches"))
    return dict(version=1, scope="emitted_generator_events_only",
                selection=dict(start_ns=start_ns, end_ns=end_ns),
                selected_event_count=sum(row["selected_event_count"] for row in result),
                selected_on_count=sum(row["on_count"] for row in result),
                selected_off_count=sum(row["off_count"] for row in result), lanes=result)


def project(fm, full, *, offset=0, length=4096, expected_manifest_sha256=None,
            detail="page", start_ns=None, end_ns=None):
    """Capture full evidence before guard/page projection; never run native work."""
    # A permitted nonnegative Python integer can have arbitrarily many digits.
    # Do not serialize an impossible offset before native/host failure precedence
    # has been resolved. A valid offset is at most the pinned capture bound.
    if detail == "summary":
        base = dict(_fixed(full), detail="summary", summary=None, omitted=["processes", "provenance"])
    else:
        base = dict(_fixed(full), detail="page", offset=offset if offset <= MAX_STDOUT else None,
                    length=0, next_offset=None,
                    eof=None, text=None, omitted=["processes", "provenance"])
    try:
        document = fm.receipts_module().canonical_bytes(full)
    except (UnicodeError, ValueError, TypeError, OverflowError, fm.receipts_module().ReceiptError):
        inner = dict(base, full_result_sha256=None, full_result_bytes=None, full_result_retention="unavailable")
        return fm.OperationReply(version=1, ok=False, exit=5, detail=detail,
            message="generator outcome cannot be encoded as exact canonical UTF8 JSON",
            operation_result=inner, full_result_sha256=None, full_result_bytes=None,
            full_result_retention="unavailable")
    base.update(full_result_sha256=fm.sha(document), full_result_bytes=len(document))
    for key in ("inputs", "refusals", "message"):
        base[key] = full.get(key)
    # Optional metadata must never consume the page's progress budget.
    for key in ("inputs", "refusals", "message"):
        if fm.reply_bytes(base) > PUBLIC_BYTES - RECEIPT_BYTES - 1024:
            base[key] = None
            base["omitted"].append(key)
    if full.get("ok") and full.get("manifest_complete"):
        try:
            if detail == "summary":
                try:
                    summary = summarize(fm, full, 0 if start_ns is None else start_ns, end_ns)
                except (fm.ToolError, UnicodeError, ValueError, TypeError, KeyError) as error:
                    raise fm.ToolError(str(error), 5) from error
            fm.require(expected_manifest_sha256 is None or expected_manifest_sha256 == full["manifest_sha256"],
                       "manifest identity changed; restart paging from the new manifest", 2)
            if detail == "summary":
                base["summary"] = summary
                for key in ("inputs", "refusals", "message"):
                    if fm.reply_bytes(base) > PUBLIC_BYTES - RECEIPT_BYTES:
                        base[key] = None
                        if key not in base["omitted"]:
                            base["omitted"].append(key)
                fm.require(fm.reply_bytes(base) <= PUBLIC_BYTES - RECEIPT_BYTES,
                           "required generator summary exceeds response byte limit", 5)
            else:
                data = full["processes"][-1]["stdout"]["text"].encode("utf-8")
                fm.require(offset <= len(data), "offset exceeds manifest length", 2)
                base["offset"] = offset
                # Reuse the shared codepoint/EOF/no-progress policy, reserving receipt
                # bytes during its binary search, then remove the private padding.
                padded = dict(base, _receipt_reserve=" " * RECEIPT_BYTES)
                base = fm.bounded_text_page(data, padded, length, "manifest")
                base.pop("_receipt_reserve")
        except fm.ToolError as error:
            base.update(ok=False, exit=error.exit,
                        failure_stage="summary" if detail == "summary" and error.exit == 5 else "delivery",
                        message=str(error))
            if detail == "summary":
                base["summary"] = None
    reply = fm.OperationReply(base)
    reply.full_result_bytes = document
    return reply


def outcome(fm, full, arguments, operation=None):
    """Adapter for fm.public, including distinct serialization/receipt failures."""
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        inner = dict(full["operation_result"])
        inner["full_result_retention"] = inner.get("full_result_retention", "unknown")
        reply = dict(version=1, ok=False, exit=5, detail=arguments.get("detail", "page"), operation_result=inner,
                     full_result_retention="unknown", receipt=full["receipt"], message=full.get("message"))
        try:
            if isinstance(reply["message"], str):
                reply["message"].encode("utf-8")
        except UnicodeError:
            reply.update(message=None, omitted=["message"])
        if fm.reply_bytes(reply) > PUBLIC_BYTES:
            reply["message"] = None
            reply["omitted"] = ["message"]
            for key in ("inputs", "refusals", "message", "text"):
                if key in inner:
                    inner[key] = None
                    inner["omitted"] = sorted(set(inner.get("omitted", [])) | {key})
        return reply
    return project(fm, full, offset=arguments.get("offset", 0), length=arguments.get("length", 4096),
                   expected_manifest_sha256=arguments.get("expected_manifest_sha256"),
                   detail=arguments.get("detail", "page"), start_ns=arguments.get("start_ns"),
                   end_ns=arguments.get("end_ns"))
