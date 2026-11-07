# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
""" final-output section statistics over an existing complete native render.

The caller supplies the shared fm adapter. There is no TL parser, synthesis,
resampling, downmix, per-voice isolation or persistent audio publication here.
"""
from __future__ import annotations

from array import array
import base64
import fcntl
import hashlib
import io
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import wave


def preflight(fm, positional, arguments):
    """Validate interval intent before receipt setup or native work."""
    fm.require(len(positional) <= 1, "measure_section accepts at most one files argument", 2)
    fm.require(not (positional and "files" in arguments), "files was supplied twice", 2)
    files = positional[0] if positional else arguments.get("files")
    text = arguments.get("text")
    fm.require(files is None or isinstance(files, (list, tuple)), "files must be a list of paths", 2)
    for path in files or ():
        fm.require(isinstance(path, (str, Path)) and bool(str(path)) and "\x00" not in str(path),
                   "each file must be a nonempty path without NUL", 2)
    fm.require(text is None or isinstance(text, str), "text must be a string", 2)
    supplied_text = text is not None and bool(text.strip())
    fm.require(bool(files) != supplied_text,
               "provide exactly one nonempty input: files or text", 2)
    for key, value in (("start_ns", arguments.get("start_ns", 0)),
                       ("end_ns", arguments.get("end_ns"))):
        if key == "end_ns" and value is None:
            continue
        fm.integer(value, key)
        fm.require(0 <= value < 2 ** 64, key + " must fit uint64", 2)
    end = arguments.get("end_ns")
    fm.require(end is None or end > arguments.get("start_ns", 0),
               "end_ns must exceed start_ns", 2)
    # Strict UTF8 also prevents receipt setup from publishing unusable requests.
    def utf8(value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as error:
                raise fm.ToolError("arguments must contain valid UTF8 strings", 2) from error
        elif isinstance(value, (list, tuple)):
            for child in value:
                utf8(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                utf8(key)
                utf8(child)
    utf8(positional)
    utf8(arguments)


def _statistics(count, peak, energy):
    """Keep integer evidence exact even above a JSON consumer's 53-bit range."""
    return dict(sample_count=count, peak_lsb=peak, sum_squares=str(energy),
                rms_milli_lsb=math.isqrt(energy * 1000000 // count),
                peak_dbfs=20 * math.log10(peak / 32768) if peak else None,
                rms_dbfs=10 * math.log10(energy / (count * 32768 ** 2)) if energy else None)


def measure_wav(fm, data, *, fs, channels, frames, length_ns, start_ns=0, end_ns=None):
    """Read final PCM, verify its entire geometry, and select sample timestamps.

    Native produced the complete WAV before this function runs. Chunked reads
    bound only this routine's additional sample storage, not native/full-WAV
    memory, disk use or render work. Native's own RIFF/PCM writer is the source.
    """
    stop = length_ns if end_ns is None else end_ns
    fm.require(0 <= start_ns < stop <= length_ns,
               "section must satisfy 0 <= start_ns < end_ns <= declared length_ns", 2)
    fm.require(channels in (1, 2, 4, 16) and fs > 0 and frames > 0
               and frames == length_ns * fs // 10 ** 9,
               "native section geometry is inconsistent", 5)
    pcm_bytes = frames * channels * 2
    fm.require(pcm_bytes <= 2 ** 32 - 37 and fs * channels * 2 < 2 ** 32,
               "native WAV geometry exceeds its writer format", 5)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + pcm_bytes, b"WAVE",
                         b"fmt ", 16, 1, channels, fs, fs * channels * 2,
                         channels * 2, 16, b"data", pcm_bytes)
    fm.require(data[:44] == header and len(data) == 44 + pcm_bytes,
               "native WAV header or PCM byte count is inconsistent", 5)
    raw_start = (start_ns * fs + 10 ** 9 - 1) // 10 ** 9
    raw_end = (stop * fs + 10 ** 9 - 1) // 10 ** 9
    first, last = min(raw_start, frames), min(raw_end, frames)
    fm.require(first < last, "section contains no available sample frames", 2)
    peaks, energies = [0] * channels, [0] * channels
    selected_sha = hashlib.sha256()
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            fm.require(audio.getcomptype() == "NONE" and audio.getsampwidth() == 2
                       and audio.getnchannels() == channels and audio.getframerate() == fs
                       and audio.getnframes() == frames,
                       "native WAV geometry differs from native output", 5)
            # A selected prefix must not conceal a truncated physical suffix.
            # Read through the declared last frame, then require the PCM byte
            # count to agree with the verified native writer header.
            audio.setpos(frames - 1)
            fm.require(len(audio.readframes(1)) == channels * 2
                       and len(data) == 44 + frames * channels * 2,
                       "native WAV is truncated or has inconsistent PCM bytes", 5)
            audio.setpos(first)
            remaining = last - first
            while remaining:
                take = min(remaining, 16384)
                block = audio.readframes(take)
                fm.require(len(block) == take * channels * 2, "native WAV section has a short read", 5)
                selected_sha.update(block)
                values = array("h")
                fm.require(values.itemsize == 2, "host signed-short width is unsupported", 5)
                values.frombytes(block)
                if sys.byteorder != "little":
                    values.byteswap()
                for index, value in enumerate(values):
                    channel = index % channels
                    magnitude = abs(value)
                    if magnitude > peaks[channel]:
                        peaks[channel] = magnitude
                    energies[channel] += value * value
                remaining -= take
    except (wave.Error, EOFError) as error:
        raise fm.ToolError("native WAV cannot be read: " + str(error), 5) from error
    count = last - first
    if channels == 1:
        semantics = "mono final output; combined is the same channel"
    elif channels == 2:
        semantics = "left/right final output; combined pools interleaved samples without downmix"
    else:
        semantics = ("AmbiX ACN/SN3D field components in channel order; combined pools component "
                     "energy, not decoded ears or perceived loudness")
    return dict(fs=fs, channels=channels, total_frames=frames, length_ns=length_ns,
                selection=dict(start_ns=start_ns, end_ns=end_ns, resolved_end_ns=stop,
                    requested_start_frame=raw_start, requested_end_frame=raw_end,
                    start_frame=first, end_frame=last, frames=count,
                    end_clipped=raw_end > frames, history="complete_session_from_frame_0",
                    stage="final_signed16_output", rounding="ceil(ns*fs/1e9); intersect physical EOF",
                    sample_time="frame_index/fs", pcm_sha256=selected_sha.hexdigest()),
                channel_semantics=semantics,
                combined=_statistics(count * channels, max(peaks), sum(energies)),
                per_channel=[dict(channel=i, **_statistics(count, peaks[i], energies[i]))
                             for i in range(channels)],
                cost=dict(normal_success_native_children=3, native_render_passes=2,
                    temporary_wav_bytes=len(data), total_pcm_bytes=frames * channels * 2,
                    selected_pcm_bytes=count * channels * 2, persistent_wav=False,
                    scope="normal successful operation body; optional receipts are separate",
                    resource_note="Full native rendering, full temporary WAV and full WAV in host memory; "
                                  "section size provides no CPU, RAM or disk cap."))


def execute(fm, files=None, *, text=None, harness=None, start_ns=0, end_ns=None):
    """Reuse native acceptance and the exact complete output of that invocation."""
    with fm.input_files(files, text) as inputs:
        native = fm.Native(harness)
        accepted = fm.native_render(native, inputs, True)
    lengths = [row["raw"]["NS"] for row in accepted["_rows"]
               if row["id"] == "header_length_ns"]
    fm.require(len(lengths) == 1 and type(lengths[0]) is int,
               "native canonical record has no unique length_ns", 5)
    measured = measure_wav(fm, accepted["_wav_bytes"], fs=accepted["fs"],
                           channels=accepted["channels"], frames=accepted["samples"],
                           length_ns=lengths[0], start_ns=start_ns, end_ns=end_ns)
    keys = ("version", "ok", "exit", "native", "record_sha256", "canonical", "hash",
            "expected", "match", "block_indep", "clipped", "checks", "checks_pass")
    return dict({key: accepted[key] for key in keys}, operation="measure_section",
                checks_scope="full_record", **measured)


#  is opt-in. None of these limits or paths affect  above.
MAX_SECTIONS = 32
MAX_PCM = 512 * 1024 * 1024
MAX_STDOUT = 16 * 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_CANONICAL = 16 * 1024 * 1024
CHUNK = 65536
CHILD_SECONDS = 120.0
CLEANUP_SECONDS = 1.0
PUBLIC_BYTES = 15360


def preflight_sections(fm, positional, arguments):
    """Validate batch intent before receipt I/O or native construction."""
    allowed = {"files", "text", "harness", "sections", "detail", "receipt_dir",
               "receipt_parent", "receipt_purpose"}
    fm.require(not set(arguments) - allowed, "unknown measure_sections argument", 2)
    fm.require(arguments.get("detail", "compact") in ("compact", "full"),
               "detail must be compact or full", 2)
    for key in ("harness", "receipt_dir"):
        value = arguments.get(key)
        fm.require(value is None or isinstance(value, (str, Path)), key + " must be a string or Path", 2)
        fm.require(value is None or "\x00" not in str(value), key + " must not contain NUL", 2)
    sections = arguments.get("sections")
    fm.require(isinstance(sections, list) and 1 <= len(sections) <= MAX_SECTIONS,
               "sections must be a list of 1..32 objects", 2)
    for index, section in enumerate(sections):
        try:
            fm.require(isinstance(section, dict) and not set(section) - {"start_ns", "end_ns"},
                       "each section admits only start_ns and end_ns", 2)
            start, end = section.get("start_ns", 0), section.get("end_ns")
            fm.integer(start, "start_ns")
            fm.require(0 <= start < 2 ** 64, "start_ns must fit uint64", 2)
            if end is not None:
                fm.integer(end, "end_ns")
                fm.require(0 <= end < 2 ** 64, "end_ns must fit uint64", 2)
                fm.require(end > start, "end_ns must exceed start_ns", 2)
        except fm.ToolError as error:
            raise fm.ToolError(str(error), error.exit, section_index=index) from error
    # Validate the closed, shallow section grammar before the legacy UTF8 walk,
    # so an invalid nested/cyclic section value cannot recurse during prework.
    preflight(fm, positional, arguments)


class PipeAnalyzer:
    """Incremental final-output evidence; never retains complete PCM.

    Protocol faults are deferred until the caller has the native verdict:
    absence/truncation following native refusal must not erase that refusal.
    An oversized physical stream is an independent capture-budget failure.
    """
    def __init__(self, fm, sections):
        self.fm, self.sections = fm, sections
        self.header, self.carry = bytearray(), b""
        self.stream_bytes, self.pcm_bytes, self.seen_frames = 0, 0, 0
        self.stream_sha = hashlib.sha256()
        self.error, self.geometry, self.windows = None, None, []
        self.max_read_bytes, self.max_carry_bytes = 0, 0

    def feed(self, block):
        fm = self.fm
        fm.require(len(block) <= CHUNK, "PCM capture chunk exceeds its bound", 5)
        self.max_read_bytes = max(self.max_read_bytes, len(block))
        self.stream_bytes += len(block)
        self.stream_sha.update(block)
        fm.require(self.stream_bytes <= MAX_PCM + 44, "PCM capture exceeds output byte limit", 5)
        if self.error is not None:
            return
        try:
            if len(self.header) < 44:
                take = min(44 - len(self.header), len(block))
                self.header.extend(block[:take])
                block = block[take:]
                if len(self.header) < 44:
                    return
                fields = struct.unpack("<4sI4s4sIHHIIHH4sI", self.header)
                (riff, riff_size, wave_id, fmt, fmt_size, kind, channels,
                 fs, rate, align, bits, data_id, pcm_bytes) = fields
                fm.require(riff == b"RIFF" and wave_id == b"WAVE" and fmt == b"fmt "
                           and fmt_size == 16 and kind == 1 and channels in (1, 2, 4, 16)
                           and fs > 0 and bits == 16 and data_id == b"data"
                           and align == channels * 2 and rate == fs * align
                           and riff_size == 36 + pcm_bytes and pcm_bytes > 0
                           and pcm_bytes % align == 0,
                           "native pipe WAV header is inconsistent", 5)
                fm.require(pcm_bytes <= MAX_PCM, "native pipe PCM exceeds output byte limit", 5)
                frames = pcm_bytes // align
                self.geometry = dict(fs=fs, channels=channels, frames=frames)
                self.pcm_bytes = pcm_bytes
                for section in self.sections:
                    start, end = section.get("start_ns", 0), section.get("end_ns")
                    first = min((start * fs + 10 ** 9 - 1) // 10 ** 9, frames)
                    last = frames if end is None else min((end * fs + 10 ** 9 - 1) // 10 ** 9, frames)
                    self.windows.append(dict(first=first, last=last, sha=hashlib.sha256(),
                                             peaks=[0] * channels, energies=[0] * channels))
            fm.require(self.stream_bytes <= 44 + self.pcm_bytes,
                       "native pipe has extra PCM bytes", 5)
            channels = self.geometry["channels"]
            frame_bytes = channels * 2
            data = self.carry + block
            stop = len(data) // frame_bytes * frame_bytes
            complete, self.carry = data[:stop], data[stop:]
            self.max_carry_bytes = max(self.max_carry_bytes, len(self.carry))
            values = array("h")
            fm.require(values.itemsize == 2, "host signed-short width is unsupported", 5)
            values.frombytes(complete)
            if sys.byteorder != "little":
                values.byteswap()
            next_frame = self.seen_frames + stop // frame_bytes
            for window in self.windows:
                first, last = max(window["first"], self.seen_frames), min(window["last"], next_frame)
                if first >= last:
                    continue
                lo, hi = (first - self.seen_frames) * channels, (last - self.seen_frames) * channels
                window["sha"].update(complete[lo * 2:hi * 2])
                peaks, energies = window["peaks"], window["energies"]
                for index in range(lo, hi):
                    value, channel = values[index], index % channels
                    peaks[channel] = max(peaks[channel], abs(value))
                    energies[channel] += value * value
            self.seen_frames = next_frame
        except fm.ToolError as error:
            self.error = error

    def finish(self, *, fs, channels, frames, length_ns):
        fm = self.fm
        if self.error is not None:
            raise self.error
        fm.require(self.geometry == dict(fs=fs, channels=channels, frames=frames)
                   and frames == length_ns * fs // 10 ** 9 and frames > 0
                   and self.stream_bytes == 44 + self.pcm_bytes and not self.carry
                   and self.seen_frames == frames, "native pipe or canonical geometry is inconsistent", 5)
        result = []
        for index, (section, window) in enumerate(zip(self.sections, self.windows)):
            start, end = section.get("start_ns", 0), section.get("end_ns")
            stop = length_ns if end is None else end
            if not 0 <= start < stop <= length_ns:
                raise fm.ToolError("section must satisfy 0 <= start_ns < end_ns <= declared length_ns", 2,
                                   section_index=index)
            first, last = window["first"], window["last"]
            if first >= last:
                raise fm.ToolError("section contains no available sample frames", 2, section_index=index)
            raw_start = (start * fs + 10 ** 9 - 1) // 10 ** 9
            raw_end = (stop * fs + 10 ** 9 - 1) // 10 ** 9
            count, peaks, energies = last - first, window["peaks"], window["energies"]
            result.append(dict(index=index, selection=dict(start_ns=start, end_ns=end,
                resolved_end_ns=stop, requested_start_frame=raw_start, requested_end_frame=raw_end,
                start_frame=first, end_frame=last, frames=count, end_clipped=raw_end > frames,
                history="complete_session_from_frame_0", stage="final_signed16_output",
                rounding="ceil(ns*fs/1e9); intersect physical EOF", sample_time="frame_index/fs",
                pcm_sha256=window["sha"].hexdigest()),
                combined=_statistics(count * channels, max(peaks), sum(energies)),
                per_channel=[dict(channel=i, **_statistics(count, peaks[i], energies[i]))
                             for i in range(channels)]))
        return result


def _capture(data, complete):
    identity = hashlib.sha256(data).hexdigest()
    try:
        text, encoded = data.decode("utf-8"), None
    except UnicodeError:
        text, encoded = None, base64.b64encode(data).decode("ascii")
    return dict(text=text, base64=encoded, encoding="utf8" if text is not None else "base64",
                bytes=len(data), captured_sha256=identity, complete=complete,
                sha256=identity if complete else None)


class BatchRunner:
    """One request's three children and concurrent bounded pipe captures."""
    def __init__(self, fm, analyzer, *, pipe_flag="--wav-fd"):
        self.fm, self.analyzer = fm, analyzer
        self.pipe_flag = pipe_flag
        self.deadline, self.processes = None, []
        self.native_identity = None
        self.started_children = 0
        self.started_records = []

    def run(self, path, args, identity):
        fm = self.fm
        self.native_identity = dict(identity)
        fm.require(fm.sha(path.read_bytes()) == identity["sha256"],
                   "native executable changed during operation", 5)
        if self.deadline is None:
            self.deadline = time.monotonic() + CHILD_SECONDS
        fm.require(time.monotonic() < self.deadline, "aggregate child deadline exhausted before spawn", 5)
        is_play = str(args[0]) == "--play"
        record = dict(operation=str(args[0]), native_returncode=None, cancellation=None)
        captures = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": MAX_STDOUT, "stderr": MAX_STDERR}
        child, read_fd, write_fd, reason = None, None, None, None
        selector = selectors.DefaultSelector()
        close_errors = []
        try:
            try:
                command = [str(path), *map(str, args)]
                if is_play:
                    read_fd, write_fd = os.pipe()
                    if write_fd < 3:
                        replacement = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 3)
                        os.close(write_fd)
                        write_fd = replacement
                    command[2:2] = [self.pipe_flag, str(write_fd), "--pcm-limit", str(MAX_PCM)]
                self.processes.append(record)
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, pass_fds=() if write_fd is None else (write_fd,),
                    start_new_session=True)
                self.started_children += 1
                self.started_records.append(record)
                if write_fd is not None:
                    os.close(write_fd)
                    write_fd = None
                for name in captures:
                    pipe = getattr(child, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                if read_fd is not None:
                    os.set_blocking(read_fd, False)
                    selector.register(read_fd, selectors.EVENT_READ, "pcm")
                while selector.get_map() or child.poll() is None:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "deadline"
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        name = key.data
                        allowance = limits[name] - len(captures[name]) if name != "pcm" else CHUNK
                        try:
                            block = os.read(key.fd, min(CHUNK, allowance + 1))
                        except BlockingIOError:
                            continue
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        if name == "pcm":
                            try:
                                self.analyzer.feed(block)
                            except fm.ToolError as error:
                                record["host_error"] = dict(type=type(error).__name__, message=str(error))
                                reason = "pcm_limit"
                                break
                        else:
                            captures[name].extend(block[:allowance])
                            if len(block) > allowance:
                                reason = name + "_limit"
                                break
                    if reason:
                        break
                if reason:
                    record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
                else:
                    child.wait(timeout=max(0, self.deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                reason = "deadline" if isinstance(error, subprocess.TimeoutExpired) else (
                    "spawn_failed" if child is None else "capture_failed")
                record["host_error"] = dict(type=type(error).__name__, message=str(error))
                if child is not None:
                    record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
        except BaseException:
            reason = "host_interrupted"
            if child is not None:
                record["cleanup_errors"] = self._cancel(child, selector, captures, limits)
            raise
        finally:
            if child is not None:
                record["native_returncode"] = child.poll()
            closers = [("selector", selector.close)]
            if child is not None:
                closers += [(name, getattr(child, name).close) for name in captures]
            for name, fd in (("pcm_read", read_fd), ("pcm_write", write_fd)):
                if fd is not None:
                    closers.append((name, lambda fd=fd: os.close(fd)))
            for name, close in closers:
                try:
                    close()
                except OSError as error:
                    close_errors.append(dict(stage=name, type=type(error).__name__, message=str(error)))
            if close_errors:
                reason = reason or "capture_close_failed"
                record.setdefault("cleanup_errors", []).extend(close_errors)
            record["cancellation"] = reason
            for name, data in captures.items():
                record[name] = _capture(bytes(data), reason is None)
        fm.require(fm.sha(path.read_bytes()) == identity["sha256"],
                   "native executable changed during operation", 5)
        fm.require(reason is None, "native capture cancelled: " + str(reason), 5)
        fm.require(record["native_returncode"] is not None and record["native_returncode"] >= 0,
                   "native process terminated unexpectedly", 5)
        fm.require(all(record[name]["text"] is not None for name in captures),
                   "native output is not valid UTF8", 5)
        return SimpleNamespace(returncode=record["native_returncode"],
                               stdout=record["stdout"]["text"], stderr=record["stderr"]["text"])

    @staticmethod
    def _cancel(child, selector, captures, limits):
        finish = time.monotonic() + CLEANUP_SECONDS
        errors = []
        def kill_group(sig):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(dict(stage="signal", type=type(error).__name__, message=str(error)))
        kill_group(signal.SIGTERM)
        kill_at = min(finish, time.monotonic() + 0.2)
        killed = False
        try:
            while time.monotonic() < finish:
                now = time.monotonic()
                if not killed and now >= kill_at:
                    kill_group(signal.SIGKILL)
                    killed = True
                if killed and not selector.get_map() and child.poll() is not None:
                    break
                for key, _ in selector.select(min(0.02, max(0, finish - now))):
                    try:
                        block = os.read(key.fd, CHUNK)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                    elif key.data != "pcm":
                        name = key.data
                        captures[name].extend(block[:max(0, limits[name] - len(captures[name]))])
        except OSError as error:
            errors.append(dict(stage="drain", type=type(error).__name__, message=str(error)))
        finally:
            kill_group(signal.SIGKILL)
            try:
                child.wait(timeout=max(0, finish - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(dict(stage="reap", type=type(error).__name__, message=str(error)))
            if child.poll() is None:
                errors.append(dict(stage="reap", message="child remains unreaped; cleanup bound exhausted"))
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(dict(stage="group_status", type=type(error).__name__, message=str(error)))
            else:
                errors.append(dict(stage="group_status", message="process group still exists after cleanup effort"))
        return errors


def execute_sections(fm, files=None, *, text=None, harness=None, sections):
    """Measure all windows from one accepted full-history native pipe stream."""
    analyzer = PipeAnalyzer(fm, sections)
    runner = BatchRunner(fm, analyzer)
    native, accepted = None, {}
    class PipeNative(fm.Native):
        def run(self, args):
            return runner.run(self.path, args, self.identity)
    def cost():
        return dict(normal_success_native_children=3, native_children=len(runner.processes),
            native_render_passes=2 if accepted else None, temporary_wav_bytes=0,
            temporary_audio_files=0, persistent_wav=False, total_pcm_bytes=analyzer.pcm_bytes,
            captured_stream_bytes=analyzer.stream_bytes, captured_stream_sha256=analyzer.stream_sha.hexdigest(),
            max_read_bytes=analyzer.max_read_bytes, max_carry_bytes=analyzer.max_carry_bytes,
            native_pcm_buffers=2, limits=dict(pcm_bytes=MAX_PCM, sections=MAX_SECTIONS,
                stdout_bytes=MAX_STDOUT, stderr_bytes=MAX_STDERR, canonical_bytes=MAX_CANONICAL,
                child_seconds=CHILD_SECONDS, cleanup_seconds=CLEANUP_SECONDS, chunk_bytes=CHUNK),
            scope="operation body; optional receipt and filesystem work have no aggregate deadline",
            resource_note="No audio file or full Python PCM; native retains two full PCM buffers. "
                          "Limits do not bound total RSS, filesystem latency or receipt storage.")
    try:
        with fm.input_files(files, text) as inputs, tempfile.TemporaryDirectory(prefix="fm-sections-") as temporary:
            try:
                native = PipeNative(harness)
            except fm.ToolError as error:
                if runner.processes and runner.processes[-1]["native_returncode"] == 0:
                    raise fm.ToolError("native metadata cannot be verified: " + str(error), 5) from error
                raise
            record = Path(temporary) / "canonical.tl"
            result = native.run(["--play", "--metrics", "--merged", record, "--", *inputs])
            if result.returncode not in (0, 3):
                raise fm.ToolError("native render failed", result.returncode,
                                   refusals=[fm.refusal(result.stderr)])
            try:
                parsed = fm.play_fields(result.stdout)
            except (fm.ToolError, ValueError, TypeError, KeyError) as error:
                raise fm.ToolError("native PLAY cannot be verified: " + str(error), 5) from error
            accepted = dict(native_returncode=result.returncode,
                            **{key: parsed[key] for key in ("hash", "expected", "match", "block_indep", "clipped")})
            with record.open("rb") as stream:
                data = stream.read(MAX_CANONICAL + 1)
            fm.require(len(data) <= MAX_CANONICAL, "native canonical exceeds text capture limit", 5)
            try:
                rows = native.metadata.canonical(data)
                canonical = data.decode("utf-8")
            except (fm.ToolError, UnicodeError, ValueError, TypeError, KeyError) as error:
                raise fm.ToolError("native canonical cannot be verified: " + str(error), 5) from error
            accepted["record_sha256"] = fm.sha(data)
            lengths = [row["raw"]["NS"] for row in rows if row["id"] == "header_length_ns"]
            rates = [row["raw"]["HZ"] for row in rows if row["id"] == "header_fs"]
            fm.require(len(lengths) == 1 and type(lengths[0]) is int,
                       "native canonical record has no unique length_ns", 5)
            fm.require(len(rates) == 1 and type(rates[0]) is int and rates[0] == parsed["fs"],
                       "native canonical sample rate differs from PLAY", 5)
            measured = analyzer.finish(fs=parsed["fs"], channels=parsed["channels"],
                                       frames=parsed["samples"], length_ns=lengths[0])
            reply = dict(version=1, ok=True, exit=result.returncode, operation="measure_sections",
                         native=native.identity, canonical=canonical, **accepted)
            reply["checks"], reply["checks_pass"] = fm.checks(reply)
            channels = parsed["channels"]
            semantics = ("mono final output; combined is the same channel" if channels == 1 else
                "left/right final output; combined pools interleaved samples without downmix" if channels == 2 else
                "AmbiX ACN/SN3D field components in channel order; combined pools component "
                "energy, not decoded ears or perceived loudness")
            reply.update(fs=parsed["fs"], channels=channels, total_frames=parsed["samples"],
                         length_ns=lengths[0], channel_semantics=semantics, checks_scope="full_record",
                         sections=measured, section_count=len(measured), cost=cost(), processes=runner.processes)
            reply["cost"]["selected_pcm_bytes"] = sum(row["selection"]["frames"] * channels * 2 for row in measured)
            reply["cost"]["selected_pcm_counts_overlaps"] = True
            return reply
    except (fm.ToolError, OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        details = dict(getattr(error, "details", {}), operation="measure_sections", cost=cost(),
                       processes=runner.processes, **accepted)
        if native is not None:
            details["native"] = native.identity
        elif runner.native_identity is not None:
            details["native"] = runner.native_identity
        if runner.processes:
            details.setdefault("native_returncode", runner.processes[-1]["native_returncode"])
        raise fm.ToolError(str(error), error.exit if isinstance(error, fm.ToolError) else 5, **details) from error


def outcome_sections(fm, full, arguments, operation, *, voice=False):
    """Exact full evidence with a bounded, explicitly lossy compact view."""
    keys = ("version", "ok", "exit", "operation", "record_sha256", "hash", "native_returncode",
            "full_result_sha256", "full_result_bytes", "section_count")
    if voice:
        keys += ("stage", "sample_format", "full_mix_complete", "dry_complete", "canonical_complete",
                 "accepted_measurements", "voice_count", "selected_voice_count", "native_children",
                 "native_attempts", "planned_full_mix_render_passes", "planned_dry_render_passes",
                 "full_mix_render_passes", "dry_render_passes", "dry_block_indep", "dry_partition",
                 "stem_payload_sha256", "stem_stream_sha256", "stem_payload_bytes", "expected",
                 "match", "block_indep", "clipped", "checks_pass", "fs", "total_frames")
    def known(value):
        kept = {}
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    try:
                        item = value[key]
                        if isinstance(item, str):
                            item.encode("utf-8")
                        if type(item) in (str, int, bool, type(None)) and fm.reply_bytes({key: item}) <= 1024:
                            kept[key] = value[key]
                    except (UnicodeError, ValueError, TypeError, RecursionError):
                        pass
            if voice and isinstance(value.get("native"), dict):
                native = value["native"]
                identity = native.get("sha256")
                if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity):
                    kept["native"] = dict(sha256=identity)
                    if type(native.get("bytes")) is int and 0 <= native["bytes"] < 2 ** 64:
                        kept["native"]["bytes"] = native["bytes"]
        return kept
    if "operation_result" in full and full.get("receipt", {}).get("state") == "unknown":
        reply = dict(version=1, ok=False, exit=5, operation=operation,
                     message="operation completed but receipt publication failed",
                     operation_result=known(full["operation_result"]),
                     receipt=dict(state="unknown"), full_result_retention="unknown",
                     omitted=["outer_diagnostic", "operation_result.details"])
        try:
            fm.receipts_module().canonical_bytes(full["receipt"])
            if fm.reply_bytes(full["receipt"]) <= 900:
                reply["receipt"] = full["receipt"]
        except (UnicodeError, ValueError, TypeError, RecursionError, fm.receipts_module().ReceiptError):
            reply["receipt"] = dict(state="unknown")
        return reply
    try:
        document = fm.receipts_module().canonical_bytes(full)
    except (UnicodeError, ValueError, TypeError, RecursionError, fm.receipts_module().ReceiptError):
        return dict(version=1, ok=False, exit=5, operation=operation,
                    message="measurement outcome cannot be encoded as exact canonical UTF8 JSON",
                    operation_result=known(full), full_result_sha256=None, full_result_bytes=None,
                    full_result_retention="unavailable")
    reply = dict(full, detail=arguments.get("detail", "compact"),
                 full_result_sha256=fm.sha(document), full_result_bytes=len(document),
                 full_result_retention="not_retained_unless_returned_receipt_confirms")
    if reply["detail"] == "compact":
        omitted = []
        for key in ("canonical", "processes"):
            if key in reply:
                reply[key] = None
                omitted.append(key)
        if isinstance(reply.get("sections"), list):
            field = "per_voice" if voice else "per_channel"
            reply["sections"] = [dict(row, **{field: None}) for row in reply["sections"]]
            omitted.append("sections." + field)
        reply["omitted"] = omitted
        # The public wrapper can append <=900 bytes of receipt metadata. Extra
        # indentation when nesting that object is covered by twice that budget.
        compact_limit = PUBLIC_BYTES - 1800
        if fm.reply_bytes(reply) > compact_limit and "sections" in reply:
            reply["sections"] = None
            omitted.append("sections")
        if fm.reply_bytes(reply) > compact_limit:
            kept = known(reply)
            kept.update(detail="compact", full_result_retention=reply["full_result_retention"],
                        omitted=sorted(set(full) - set(kept)))
            reply = kept
    result = fm.OperationReply(reply)
    result.full_result_bytes = document
    return result


#  uses the same process transport, with a distinct raw-voice format and
# outcome. None of its validation, units or partial-stage states alter .
VOICE_STAGE = "voice_out_pre_fx"
VOICE_FORMAT = "s32le_q2_30"
MAX_VOICES = 64


def _voice_require(fm, condition, message, status, **details):
    if not condition:
        raise fm.ToolError(message, status, **details)


def preflight_voice_sections(fm, positional, arguments):
    voices = arguments.get("voices")
    fm.require(isinstance(voices, list) and 1 <= len(voices) <= MAX_VOICES,
               "voices must be a list of 1..64 distinct integer indices", 2)
    for index, value in enumerate(voices):
        _voice_require(fm, type(value) is int and 0 <= value < MAX_VOICES,
                   "each voice must be an integer from 0 to 63", 2, voice_selection_index=index)
    fm.require(len(set(voices)) == len(voices), "voices must not contain duplicates", 2)
    preflight_sections(fm, positional, {key: value for key, value in arguments.items() if key != "voices"})


def _q30_statistics(count, peak, energy):
    return dict(sample_count=count, peak_q30_lsb=peak, sum_squares_q30_lsb2=str(energy),
                rms_milli_q30_lsb=math.isqrt(energy * 1000000 // count),
                peak_db_q30_unity=20 * math.log10(peak / 2 ** 30) if peak else None,
                rms_db_q30_unity=10 * math.log10(energy / (count * 2 ** 60)) if energy else None)


class VoicePipeAnalyzer:
    """Exact Q30 words, bounded chunks, selected voices; no complete PCM copy."""
    def __init__(self, fm, sections, voices):
        self.fm, self.sections, self.voices = fm, sections, voices
        self.header, self.carry = bytearray(), b""
        self.stream_bytes = self.pcm_bytes = self.seen_frames = 0
        self.stream_sha, self.payload_sha = hashlib.sha256(), hashlib.sha256()
        self.fnv = 14695981039346656037
        self.error, self.geometry, self.windows = None, None, []
        self.max_read_bytes = self.max_carry_bytes = 0

    def feed(self, block):
        fm = self.fm
        fm.require(len(block) <= CHUNK, "stem capture chunk exceeds its bound", 5)
        self.max_read_bytes = max(self.max_read_bytes, len(block))
        self.stream_bytes += len(block)
        self.stream_sha.update(block)
        fm.require(self.stream_bytes <= MAX_PCM + 44, "stem capture exceeds output byte limit", 5)
        if self.error is not None:
            return
        try:
            if len(self.header) < 44:
                take = min(44 - len(self.header), len(block))
                self.header.extend(block[:take])
                block = block[take:]
                if len(self.header) < 44:
                    return
                (riff, size, wave_id, fmt, fmt_size, kind, channels, fs, rate,
                 align, bits, data_id, size_data) = struct.unpack("<4sI4s4sIHHIIHH4sI", self.header)
                fm.require(riff == b"RIFF" and wave_id == b"WAVE" and fmt == b"fmt "
                           and fmt_size == 16 and kind == 1 and 1 <= channels <= MAX_VOICES
                           and fs > 0 and bits == 32 and data_id == b"data"
                           and align == channels * 4 and rate == fs * align
                           and size == 36 + size_data and size_data > 0 and size_data % align == 0,
                           "native stem header is inconsistent", 5)
                fm.require(size_data <= MAX_PCM, "native stem payload exceeds output byte limit", 5)
                frames = size_data // align
                self.geometry = dict(fs=fs, channels=channels, frames=frames)
                self.pcm_bytes = size_data
                for section in self.sections:
                    start, end = section.get("start_ns", 0), section.get("end_ns")
                    self.windows.append(dict(first=min((start * fs + 10 ** 9 - 1) // 10 ** 9, frames),
                        last=frames if end is None else min((end * fs + 10 ** 9 - 1) // 10 ** 9, frames),
                        sha=[hashlib.sha256() for _ in self.voices], peaks=[0] * len(self.voices),
                        energies=[0] * len(self.voices)))
            fm.require(self.stream_bytes <= 44 + self.pcm_bytes, "native stem stream has extra bytes", 5)
            self.payload_sha.update(block)
            value = self.fnv
            for byte in block:
                value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
            self.fnv = value
            channels = self.geometry["channels"]
            data = self.carry + block
            stop = len(data) // (channels * 4) * channels * 4
            complete, self.carry = data[:stop], data[stop:]
            self.max_carry_bytes = max(self.max_carry_bytes, len(self.carry))
            values = array("i")
            fm.require(values.itemsize == 4, "host signed-int width is unsupported", 5)
            values.frombytes(complete)
            if sys.byteorder != "little":
                values.byteswap()
            following = self.seen_frames + stop // (channels * 4)
            for window in self.windows:
                first, last = max(window["first"], self.seen_frames), min(window["last"], following)
                if first >= last:
                    continue
                lo, hi = (first - self.seen_frames) * channels, (last - self.seen_frames) * channels
                for selected, voice in enumerate(self.voices):
                    if voice >= channels:  # Existence is a post-native usage refusal.
                        continue
                    words = values[lo + voice:hi:channels]
                    for word in words:
                        window["peaks"][selected] = max(window["peaks"][selected], abs(word))
                        window["energies"][selected] += word * word
                    if sys.byteorder != "little":
                        words.byteswap()
                    window["sha"][selected].update(words.tobytes())
            self.seen_frames = following
        except fm.ToolError as error:
            self.error = error

    def verify_stream(self, play, stems):
        fm = self.fm
        if self.error is not None:
            raise self.error
        geometry = dict(fs=play["fs"], channels=play["voices"], frames=play["samples"])
        fm.require(self.geometry == geometry and self.stream_bytes == 44 + self.pcm_bytes
                   and not self.carry and self.seen_frames == play["samples"],
                   "native stem stream is incomplete or differs from PLAY", 5)
        expected = dict(format=VOICE_FORMAT, stage=VOICE_STAGE, fs=str(play["fs"]),
            frames=str(play["samples"]), voices=str(play["voices"]), bytes=str(self.pcm_bytes),
            hash=f"{self.fnv:016x}", partition="1", passes="1", block_indep="unchecked")
        fm.require(stems == expected, "native STEMS metadata or payload hash is inconsistent", 5)

    def measurements(self, length_ns, voice_map):
        fm, fs = self.fm, self.geometry["fs"]
        frames = self.geometry["frames"]
        fm.require(frames == length_ns * fs // 10 ** 9, "canonical stem length differs", 5)
        for index, voice in enumerate(self.voices):
            _voice_require(fm, voice in voice_map, "selected voice is not declared", 2,
                       voice_selection_index=index, voice=voice)
        result = []
        for index, (section, window) in enumerate(zip(self.sections, self.windows)):
            start, end = section.get("start_ns", 0), section.get("end_ns")
            stop = length_ns if end is None else end
            _voice_require(fm, 0 <= start < stop <= length_ns,
                       "section must satisfy 0 <= start_ns < end_ns <= declared length_ns", 2, section_index=index)
            first, last = window["first"], window["last"]
            _voice_require(fm, first < last, "section contains no available sample frames", 2, section_index=index)
            count = last - first
            raw_start, raw_end = (start * fs + 10 ** 9 - 1) // 10 ** 9, (stop * fs + 10 ** 9 - 1) // 10 ** 9
            selection = dict(start_ns=start, end_ns=end, resolved_end_ns=stop,
                requested_start_frame=raw_start, requested_end_frame=raw_end, start_frame=first,
                end_frame=last, frames=count, end_clipped=raw_end > frames,
                history="complete_session_from_frame_0", stage=VOICE_STAGE, sample_format=VOICE_FORMAT,
                rounding="ceil(ns*fs/1e9); intersect physical EOF", sample_time="frame_index/fs")
            result.append(dict(index=index, selection=selection, per_voice=[dict(index=i, voice=voice,
                patch=voice_map[voice], pcm_sha256=window["sha"][i].hexdigest(),
                **_q30_statistics(count, window["peaks"][i], window["energies"][i]))
                for i, voice in enumerate(self.voices)]))
        return result


def _voice_protocol(fm, capture, tag):
    """Only complete, unique ASCII lines establish protocol evidence."""
    data = (capture["text"].encode("utf-8") if capture.get("text") is not None else
            base64.b64decode(capture.get("base64") or "", validate=True))
    lines = [line for line in data.splitlines(keepends=True) if line.startswith(tag.encode() + b" ")]
    fm.require(len(lines) == 1 and lines[0].endswith(b"\n"), "native output has no unique complete " + tag, 5)
    words = lines[0].decode("ascii").strip().split()[1:]
    row = {}
    for word in words:
        fm.require("=" in word, "malformed native " + tag + " field", 5)
        key, value = word.split("=", 1)
        fm.require(key and value and key not in row, "duplicate or empty native " + tag + " field", 5)
        row[key] = value
    return row


def _voice_play(fm, capture):
    row = _voice_protocol(fm, capture, "PLAY")
    for key in ("fs", "samples", "voices", "clipped", "events", "streams"):
        fm.require(re.fullmatch(r"[0-9]+", row.get(key, "")) is not None
                   and int(row[key]) < 2 ** 32, "invalid PLAY " + key, 5)
    fm.require(0 < int(row["fs"]) and 0 < int(row["samples"]) and 1 <= int(row["voices"]) <= MAX_VOICES,
               "invalid PLAY stem geometry", 5)
    fm.require(row.get("channels", "1") in ("1", "2", "4", "16"), "invalid PLAY channel count", 5)
    fm.require(re.fullmatch(r"[0-9a-f]{16}", row.get("hash", "")) is not None
               and (row.get("expected") == "-" or re.fullmatch(r"[0-9a-f]{16}", row.get("expected", "")))
               and row.get("block_indep") in ("yes", "NO")
               and row.get("match") == ("-" if row["expected"] == "-" else
                                        "yes" if row["hash"] == row["expected"] else "NO"),
               "invalid PLAY hash/check fields", 5)
    parsed = fm.play_fields("PLAY " + " ".join(key + "=" + value for key, value in row.items()) + "\n")
    fm.require(parsed["channels"] in (1, 2, 4, 16), "invalid PLAY channel count", 5)
    return parsed


def execute_voice_sections(fm, files=None, *, text=None, harness=None, voices, sections):
    analyzer = VoicePipeAnalyzer(fm, sections, voices)
    runner = BatchRunner(fm, analyzer, pipe_flag="--stems-fd")
    native = None
    evidence = dict(stage=VOICE_STAGE, sample_format=VOICE_FORMAT, full_mix_complete=False,
        dry_complete=False, canonical_complete=False, accepted_measurements=False,
        planned_full_mix_render_passes=2, planned_dry_render_passes=1,
        full_mix_render_passes=0, dry_render_passes=0, dry_block_indep="unchecked", dry_partition="1",
        selected_voice_count=len(voices), section_count=len(sections))
    class StemNative(fm.Native):
        def run(self, args):
            return runner.run(self.path, args, self.identity)
    def cost():
        counts = [evidence["full_mix_render_passes"], evidence["dry_render_passes"]]
        return dict(normal_success_native_children=3, native_children=runner.started_children,
            native_attempts=len(runner.processes), native_render_passes=sum(counts) if None not in counts else None,
            full_mix_render_passes=counts[0], dry_render_passes=counts[1],
            temporary_audio_files=0, temporary_wav_bytes=0, persistent_wav=False, native_pcm_buffers=2,
            total_stem_bytes=analyzer.pcm_bytes, captured_stream_bytes=analyzer.stream_bytes,
            captured_stream_sha256=analyzer.stream_sha.hexdigest(), max_read_bytes=analyzer.max_read_bytes,
            max_carry_bytes=analyzer.max_carry_bytes,
            limits=dict(pcm_bytes=MAX_PCM, voices=MAX_VOICES, sections=MAX_SECTIONS, stdout_bytes=MAX_STDOUT,
                stderr_bytes=MAX_STDERR, canonical_bytes=MAX_CANONICAL, child_seconds=CHILD_SECONDS,
                cleanup_seconds=CLEANUP_SECONDS, chunk_bytes=CHUNK),
            scope="operation body; optional receipts and filesystem work have no aggregate deadline",
            resource_note="Three planned synthesis passes; two full native final16 buffers; all voices transported. "
                "No audio file or full Python PCM. Cooperative deadline can overrun by a bounded analysis step; "
                "limits do not bound assets, total RSS, filesystem latency or receipt storage.")
    def collect(record):
        errors, play, voice_map, length = [], None, None, None
        process = next((p for p in reversed(runner.processes) if p["operation"] == "--play"), None)
        if process is None or not any(process is row for row in runner.started_records):
            return errors, play, voice_map, length
        evidence.update(full_mix_complete=None, dry_complete=None, canonical_complete=None,
                        full_mix_render_passes=None, dry_render_passes=None)
        capture = process.get("stdout", {})
        try:
            play = _voice_play(fm, capture)
            evidence.update(full_mix_complete=True, full_mix_render_passes=2, voice_count=play["voices"], fs=play["fs"],
                total_frames=play["samples"], **{key: play[key] for key in ("hash", "expected", "match", "block_indep", "clipped")})
            evidence["checks"], evidence["checks_pass"] = fm.checks(evidence)
        except (fm.ToolError, ValueError, KeyError, UnicodeError) as error:
            errors.append(str(error))
        try:
            stems = _voice_protocol(fm, capture, "STEMS")
            fm.require(play is not None, "STEMS has no verified full-mix PLAY", 5)
            analyzer.verify_stream(play, stems)
            evidence.update(dry_complete=True, dry_render_passes=1, stem_payload_bytes=analyzer.pcm_bytes,
                stem_payload_sha256=analyzer.payload_sha.hexdigest(), stem_stream_sha256=analyzer.stream_sha.hexdigest())
        except (fm.ToolError, ValueError, KeyError, UnicodeError) as error:
            if process.get("native_returncode") is not None and analyzer.geometry is not None:
                if analyzer.stream_bytes < 44 + analyzer.pcm_bytes:
                    evidence["dry_complete"] = False
            errors.append(str(error))
        try:
            fd = os.open(record, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                fm.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "native canonical is not a regular file", 5)
                data = stream.read(MAX_CANONICAL + 1)
            fm.require(len(data) <= MAX_CANONICAL, "native canonical exceeds text capture limit", 5)
            fm.require(native is not None and play is not None, "canonical has no verified native PLAY", 5)
            rows = native.metadata.canonical(data)
            def unique(identifier, field):
                values = [row["raw"][field] for row in rows if row["id"] == identifier]
                fm.require(len(values) == 1, "canonical has no unique " + identifier, 5)
                return values[0]
            length = unique("header_length_ns", "NS")
            fm.require(type(length) is int and 0 < length < 2 ** 64
                       and unique("header_fs", "HZ") == play["fs"]
                       and length * play["fs"] // 10 ** 9 == play["samples"]
                       and unique("header_hash", "HASH") == play["hash"], "canonical stem geometry/hash differs", 5)
            declared = [row["raw"] for row in rows if row["id"] == "header_voice"]
            fm.require(len(declared) == play["voices"] and all(type(row["INDEX"]) is int for row in declared)
                       and sorted(row["INDEX"] for row in declared) == list(range(play["voices"])),
                       "canonical voice map differs", 5)
            voice_map = {row["INDEX"]: row["PATCH"] for row in declared}
            evidence.update(canonical_complete=True, canonical=data.decode("utf-8"), record_sha256=fm.sha(data),
                            length_ns=length, voice_map=[dict(voice=i, patch=voice_map[i]) for i in sorted(voice_map)])
        except FileNotFoundError as error:
            if process.get("native_returncode") is not None:
                evidence["canonical_complete"] = False
            errors.append(str(error))
        except (fm.ToolError, OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            errors.append(str(error))
        return errors, play, voice_map, length
    try:
        with fm.input_files(files, text) as inputs, tempfile.TemporaryDirectory(prefix="fm-voice-sections-") as temporary:
            record, failure, result = Path(temporary) / "canonical.tl", None, None
            try:
                native = StemNative(harness)
                result = native.run(["--play", "--metrics", "--merged", record, "--", *inputs])
            except (fm.ToolError, OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
                failure = error
            errors, play, voice_map, length = collect(record)
            if errors:
                evidence["protocol_errors"] = errors
            if failure is not None:
                if native is None and runner.processes and runner.processes[-1]["native_returncode"] == 0:
                    raise fm.ToolError("native metadata cannot be verified: " + str(failure), 5) from failure
                raise failure
            _voice_require(fm, result.returncode in (0, 3), "native render failed", result.returncode,
                       refusals=[fm.refusal(result.stderr)])
            fm.require(not errors and all(evidence[key] is True for key in
                       ("full_mix_complete", "dry_complete", "canonical_complete")),
                       "native dry output cannot be verified", 5)
            fm.require(play["block_indep"] is True and result.returncode == (3 if play["match"] is False else 0),
                       "native exit differs from full-mix checks", 5)
            measured = analyzer.measurements(length, voice_map)
            evidence.update(accepted_measurements=True, sections=measured,
                input_sources=[str(p) for p in (files or [])] if text is None else ["input.tl"],
                request_text_sha256=fm.sha(text.encode("utf-8")) if text is not None else None)
            return dict(version=1, ok=True, exit=result.returncode, operation="measure_voice_sections",
                native=native.identity, native_returncode=result.returncode, checks_scope="full_record",
                native_children=runner.started_children, native_attempts=len(runner.processes),
                cost=cost(), processes=runner.processes, **evidence)
    except (fm.ToolError, OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        details = dict(getattr(error, "details", {}), operation="measure_voice_sections", **evidence)
        details.update(cost=cost(), native_children=runner.started_children, native_attempts=len(runner.processes),
                       processes=runner.processes)
        identity = native.identity if native is not None else runner.native_identity
        if identity is not None:
            details["native"] = identity
        if runner.processes:
            details["native_returncode"] = runner.processes[-1]["native_returncode"]
        status = error.exit if isinstance(error, fm.ToolError) else 5
        actual = details.get("native_returncode")
        if actual is not None and actual not in (0, 1, 2, 3, 5):
            status = 5
        raise fm.ToolError(str(error), status, **details) from error


def outcome_voice_sections(fm, full, arguments, operation):
    return outcome_sections(fm, full, arguments, operation, voice=True)
