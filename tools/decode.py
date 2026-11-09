#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Command-line TL-to-WAV rendering (fm.py tl2wav) and WAV-to-TL drafts (fm.py wav2tl).

tl2wav is the exact direction: it renders through the same native
`harness --play FILES --wav` path as fm.py render, so the WAV bytes equal the
harness output for the same input files. It never overwrites an existing
output and refuses a silent render unless allow_silent is set.

wav2tl is the approximate direction and a command-line tool only: a thin
alias over fm.py transcribe (same isolated child, staging, lock and
never-overwrite publication) that widens the recipe shortlist, labels the
result a draft and adds a provenance.json sidecar to the piece directory.
"""
from __future__ import annotations

import errno
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

if "fm" not in sys.modules:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import fm

SILENT_FIX = ("a piece is a directory: pass the piece directory or all of its part files "
              "(DIR/session.tl and DIR/*.tl), or --allow-silent to keep a silent WAV")


def _source(inputs):
    fm.require(isinstance(inputs, (list, tuple)) and inputs, "tl2wav needs a .tl file, several .tl files or a piece directory", 2)
    paths = [fm.workspace_path(item) for item in inputs]
    directories = [path for path in paths if path.is_dir()]
    if directories:
        fm.require(len(paths) == 1, "pass one piece directory or a list of .tl files, not both", 2)
        return paths[0]
    for path in paths:
        fm.require(path.is_file(), "input not found: " + str(path), 2)
    return paths


def _output(out):
    fm.require(out is not None, "tl2wav requires -o OUT.wav", 2)
    path = fm.workspace_path(out)
    fm.require(not os.path.lexists(path), "output exists; refusing to overwrite: " + str(path), 2)
    fm.require(path.parent.is_dir(), "output directory does not exist: " + str(path.parent), 2)
    return path


def _publish(path, data):
    """Write data to a new path; an existing file is never replaced."""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".tl2wav-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except OSError as error:
                if isinstance(error, FileExistsError) or error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise
                with open(path, "xb") as stream:  # no hard links here; exclusive create still never replaces
                    stream.write(data)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except FileExistsError as error:
        raise fm.ToolError("output exists; refusing to overwrite: " + str(path), 2) from error
    except OSError as error:
        raise fm.ToolError("cannot write output %s: %s" % (path, error.strerror or error), 5) from error


def tl2wav(inputs, out, *, harness=None, allow_silent=False):
    """Render TL files or a piece directory to a new WAV file."""
    fm.boolean(allow_silent, "allow_silent")
    source = _source(inputs)
    path = _output(out)
    native = fm.Native(harness)
    plays = []
    run = native.run

    def capture(args):
        result = run(args)
        if args and str(args[0]) == "--play":
            plays.append(result.stderr)
        return result

    native.run = capture
    try:
        reply = fm.resolve_source(source if isinstance(source, Path) else list(source), native, True)
    except fm.ToolError as error:
        refusals = error.details.get("refusals") or []
        detail = refusals[0].get("message") if refusals else None
        raise fm.ToolError(str(error) + (": " + detail if detail else ""), error.exit) from error
    # Only the final --play renders the WAV; a piece directory first reads its header.
    warnings = [line for line in (plays[-1] if plays else "").splitlines() if line.strip()]
    warnings += ["piece declares stream %s but has no %s.tl" % (name, name)
                 for name in reply.get("absent_sibling_streams", [])]
    data = reply["_wav_bytes"]
    with wave.open(io.BytesIO(data), "rb") as stream:
        frames, rate, channels = stream.getnframes(), stream.getframerate(), stream.getnchannels()
    result = dict(version=1, ok=True, exit=0, out=str(path), sha256=fm.sha(data), frames=frames,
                  sample_rate=rate, channels=channels, events=reply.get("events"), peak=reply.get("peak"),
                  saved_hash=("absent" if reply.get("expected") is None
                              else "match" if reply.get("match") else "mismatch"),
                  sources=reply.get("source_files", [str(item) for item in ([source] if isinstance(source, Path) else source)]),
                  warnings=warnings)
    if result["saved_hash"] == "mismatch":
        return dict(result, ok=False, exit=3, out=None,
                    message="rendered audio does not match the saved hash %s (rendered %s); no WAV written"
                    % (reply.get("expected"), reply.get("hash")))
    if not result["peak"] and not allow_silent:
        return dict(result, ok=False, exit=2, out=None,
                    message="render is silent (events=%s, peak=0); %s" % (result["events"], SILENT_FIX))
    _publish(path, data)
    return result


def report(result, stdout, stderr):
    """Plain-text form of a tl2wav result for the terminal."""
    for line in result.get("warnings", []):
        stderr.write("warning: " + line + "\n")
    if result.get("ok"):
        stdout.write("wrote %s: %d frames, %d Hz, %d channel(s), events %s, peak %s, saved hash %s, sha256 %s\n"
                     % (result["out"], result["frames"], result["sample_rate"], result["channels"],
                        result["events"], result["peak"], result["saved_hash"], result["sha256"]))
    else:
        stderr.write("fm.py tl2wav: " + result.get("message", "failed") + "\n")


WIDE_RECIPE_CANDIDATES = 8  # the round-trip research found a shortlist of 8 where 1 failed


def _tool_version(harness):
    """The existing native version line, e.g. 'JotTune Synth 0.1.0', or None."""
    try:
        reply = subprocess.run([str(Path(harness or fm.DEFAULT_HARNESS).resolve()), "--version"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    lines = reply.stdout.strip().splitlines()
    return lines[0].strip() if reply.returncode == 0 and lines else None


def wav2tl(source, out, *, options=(), harness=None, native_budget=None, strict=False, min_snr_db=0.0):
    """Draft an approximate mono TL piece directory from a WAV through fm.transcribe."""
    fm.boolean(strict, "strict")
    fm.require(type(min_snr_db) in (int, float) and math.isfinite(min_snr_db), "min_snr_db must be a finite number", 2)
    fm.require(isinstance(options, (list, tuple)) and all(isinstance(item, str) for item in options),
               "options must be a list of argument strings", 2)
    fm.require(out is not None, "wav2tl requires -o OUT_DIR", 2)
    source, destination = fm.workspace_path(source), fm.workspace_path(out)
    fm.require(source.is_file(), "input not found: " + str(source), 2)
    fm.require(not os.path.lexists(destination), "output exists; refusing to overwrite: " + str(destination), 2)
    options = list(options)
    # An explicit shortlist or recipe list is the caller's; otherwise widen it.
    widened = not {item.split("=", 1)[0] for item in options} & {"--recipe-candidates", "--recipes"}
    draft = dict(tool="fm.py wav2tl", tool_version=_tool_version(harness), strict=strict, min_snr_db=float(min_snr_db))
    attempt = (["--recipe-candidates", str(WIDE_RECIPE_CANDIDATES)] if widened else []) + options
    result = fm.transcribe(source, destination, options=attempt, harness=harness, native_budget=native_budget, draft=draft)
    fallback = None
    fallback_warning = None
    spent = (result.get("costs") or {}).get("total_attempts") or 0
    # Fall back to the analyzer's single nearest recipe only after a decoder refusal
    # of the widened run itself (it reached native calls), never after a budget stop.
    if (widened and not result.get("ok") and result.get("exit") == 2 and result.get("stop_reason") == "error"
            and spent and not os.path.lexists(destination) and (native_budget is None or native_budget > spent)):
        if strict:
            # A fallback is itself a warning. Refuse it before another child can
            # publish a draft; its own quality gate cannot see this wrapper warning.
            fallback_warning = ("the %d-recipe shortlist was refused (%s); strict mode refuses "
                                "the single nearest recipe fallback" % (WIDE_RECIPE_CANDIDATES, result.get("message")))
            result = dict(result, ok=False, exit=4, message="strict draft check failed: " + fallback_warning)
        else:
            fallback = dict(from_recipe_candidates=WIDE_RECIPE_CANDIDATES, to_recipe_candidates=1,
                            reason=result.get("message"), native_attempts=spent)
            result = fm.transcribe(source, destination, options=options, harness=harness, draft=draft,
                                   native_budget=None if native_budget is None else native_budget - spent)
    # The lower-level transcribe API treats a budget stop as normal partial
    # progress. This command promises a written draft, so an unpublished result
    # must be a failed gate, not a successful "wrote None" terminal outcome.
    if result.get("ok") and not result.get("published"):
        result = dict(result, ok=False, exit=4,
                      message="transcription stopped before draft publication (%s); no TL written"
                              % (result.get("stop_reason") or "incomplete result"))
    info = result.get("draft") or {}
    warnings = list(info.get("warnings") or [])
    if fallback_warning is not None:
        warnings.append(fallback_warning)
    if fallback is not None:
        warnings.append("the %d-recipe shortlist was refused (%s); the single nearest recipe was used"
                        % (WIDE_RECIPE_CANDIDATES, fallback["reason"]))
    snr = info.get("snr_db", (result.get("score") or {}).get("snr_db"))
    limitations = list(fm.TRANSCRIBE_DRAFT_LIMITATIONS)
    limitations += [item for item in result.get("limitations") or [] if item not in limitations]
    return dict(result, operation="wav2tl", draft=True, snr_db=snr, limitations=limitations, warnings=warnings,
                provenance=info.get("provenance"), provenance_file=info.get("file"), strict=strict,
                min_snr_db=float(min_snr_db), recipe_candidates=(WIDE_RECIPE_CANDIDATES if widened and fallback is None
                                                                else None), fallback=fallback)


def banner(result):
    """The one-line draft notice that wav2tl always prints on stderr."""
    snr = result.get("snr_db")
    measured = "whole-signal SNR %.2f dB" % snr if isinstance(snr, (int, float)) else "SNR unavailable"
    return ("wav2tl: DRAFT approximate mono transcription (%s); review it before sharing; "
            "limits: mono only, polyphony and chords unsupported, timbre approximated by built-in recipes\n" % measured)


def wav2tl_report(result, stdout, stderr, as_json=False):
    """Banner and warnings on stderr; JSON or a one-line summary otherwise."""
    stderr.write(banner(result))
    for line in result.get("warnings") or []:
        stderr.write("warning: " + line + "\n")
    if as_json:
        stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
    elif result.get("ok") and result.get("published") and result.get("output"):
        stdout.write("wrote %s: %s note(s); render it with fm.py tl2wav %s -o OUT.wav; provenance in %s\n"
                     % (result.get("output"), result.get("note_count"), result.get("output"),
                        result.get("provenance_file")))
    else:
        stderr.write("fm.py wav2tl: " + str(result.get("message") or "no draft was published") + "\n")
