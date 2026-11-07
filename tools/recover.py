# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Bounded client-side recovery through an already connected MCP client.

No transport setup, native work, retries or resume. The cooperative local
filesystem must support exclusive creation and same-directory hard links.
"""
import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re


_MAX_INT = (1 << 63) - 1
_TEXT_KINDS = frozenset(("authored_tl", "generator_manifest"))
_KINDS = frozenset(("record", "receipt", "take_log", "session_manifest", "png", "score")) | _TEXT_KINDS


class _Failure(Exception):
    def __init__(self, category, stage, message, exit=5, remote_exit=None):
        super().__init__(message)
        self.category, self.stage, self.exit = category, stage, exit
        self.remote_exit = remote_exit


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _integer(value):
    return type(value) is int and 0 <= value <= _MAX_INT


def _status(value):
    return type(value) is int and -_MAX_INT - 1 <= value <= _MAX_INT


def _need(condition, message, category="plan", stage="preflight", exit=2):
    if not condition:
        raise _Failure(category, stage, message, exit)


def _validate(plan, max_artifact_bytes, max_total_bytes, max_calls, page_bytes):
    _need(type(plan) is list and 1 <= len(plan) <= 256, "plan must contain 1 through256 artifacts")
    for key, value in (("max_artifact_bytes", max_artifact_bytes),
                       ("max_total_bytes", max_total_bytes), ("max_calls", max_calls)):
        _need(_integer(value), key + " must be an integer from0 through2^63-1")
    _need(type(page_bytes) is int and 1 <= page_bytes <= 1048576,
          "page_bytes must be an integer from1 through1048576")
    # Bound serialized input without constructing an unbounded JSON string.
    encoded = bytearray()
    try:
        for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).iterencode(plan):
            chunk = part.encode("utf-8")
            _need(len(encoded) + len(chunk) < 262144, "canonical plan exceeds256KiB")
            encoded.extend(chunk)
        encoded.extend(b"\n")
        copied = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise _Failure("plan", "preflight", "plan must be strict UTF8 JSON", 2) from error
    names, known_total, minimum_calls = set(), 0, 0
    for index, item in enumerate(copied):
        _need(type(item) is dict and {"name", "directory", "kind", "digest"} <= item.keys()
              and item.keys() <= {"name", "directory", "kind", "digest", "filename", "size"},
              "artifact %d has unsupported or missing fields" % index)
        name = item["name"]
        _need(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
              and not name.endswith(".") and name.casefold() != "manifest.json",
              "artifact %d needs a bounded output basename" % index)
        _need(name.casefold() not in names, "output basenames must be unique ignoring ASCII case")
        names.add(name.casefold())
        directory = item["directory"]
        _need(isinstance(directory, str) and directory and "\0" not in directory
              and len(directory.encode("utf-8")) <= 4096, "artifact directory must be bounded nonempty UTF8")
        _need(item["kind"] in _KINDS if isinstance(item["kind"], str) else False,
              "unsupported artifact kind")
        _need(isinstance(item["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", item["digest"]),
              "artifact digest must be lowercase SHA256")
        filename = item.get("filename")
        if item["kind"] == "score" or item["kind"] in _TEXT_KINDS:
            _need(isinstance(filename, str) and re.fullmatch(r"[0-9a-f]{64}\.json", filename),
                  item["kind"] + " filename must be its parent receipt SHA256 plus .json")
            _need(item["kind"] not in _TEXT_KINDS or page_bytes <= 8192,
                  "plans containing retained text views require page_bytes at most8192")
        elif "filename" in item or item["kind"] == "png":
            suffix = ".png" if item["kind"] == "png" else ".tl"
            _need(item["kind"] in ("record", "png") and isinstance(filename, str) and filename
                  and not any(c in filename for c in ("/", "\\", "\0"))
                  and Path(filename).suffix.lower() == suffix,
                  "filename must be one matching record or PNG basename")
        if "size" in item:
            size = item["size"]
            _need(_integer(size), "artifact size must be an integer from0 through2^63-1")
            _need(size <= max_artifact_bytes, "known artifact exceeds byte budget", "budget")
            known_total += size
            minimum_calls += max(1, (size + page_bytes - 1) // page_bytes)
        else:
            minimum_calls += 1
    _need(known_total <= max_total_bytes, "known artifacts exceed total byte budget", "budget")
    _need(minimum_calls <= max_calls, "minimum artifact calls exceed call budget", "budget")
    return copied, hashlib.sha256(encoded).hexdigest()


def _diagnostic(error, item_index=None):
    # Error strings may contain arbitrary remote/local data; this is a bounded
    # diagnostic, never an exact retained transport reply or source artifact.
    message = str(error).encode("utf-8", errors="backslashreplace")[:1024].decode("utf-8", errors="ignore")
    result = dict(category=error.category, stage=error.stage, message=message,
                  item_index=item_index)
    if error.remote_exit is not None:
        result["remote_exit"] = error.remote_exit
    return result


def _page(envelope, item, offset, requested, size):
    def need(condition, message):
        _need(condition, message, "protocol", "page", 5)
    need(type(getattr(envelope, "is_error", None)) is bool, "MCP reply lacks is_error boolean")
    result = getattr(envelope, "structured_content", None)
    if envelope.is_error:
        remote_exit = result.get("exit") if type(result) is dict else None
        need(remote_exit is None or _status(remote_exit), "MCP tool error has invalid exit status")
        raise _Failure("remote", "dispatch", "MCP artifact_read tool error",
                       remote_exit if remote_exit else 5, remote_exit)
    need(type(result) is dict, "MCP reply lacks structured_content object")
    need(type(result.get("version")) is int and result["version"] == 1, "unsupported artifact reply version")
    need(type(result.get("ok")) is bool and _status(result.get("exit")),
         "artifact reply lacks exact status fields")
    if not result["ok"] or result["exit"] != 0:
        code = result["exit"] if result["exit"] != 0 else 5
        message = result.get("message", "artifact_read refused")
        raise _Failure("remote", "dispatch", message if isinstance(message, str) else "artifact_read refused",
                       code, result["exit"])
    need(result.get("kind") == item["kind"] and result.get("sha256") == item["digest"],
         "artifact reply identity differs from plan")
    for field in ("size", "offset", "length"):
        need(_integer(result.get(field)), "artifact reply has invalid " + field)
    actual_size, length = result["size"], result["length"]
    need(result["offset"] == offset and offset <= actual_size, "artifact offset differs or exceeds size")
    need(size is None or actual_size == size, "artifact size changed between pages")
    need("size" not in item or actual_size == item["size"], "artifact size differs from plan")
    need(length <= requested and length <= actual_size - offset, "artifact page exceeds requested range")
    need(type(result.get("eof")) is bool and result["eof"] == (offset + length == actual_size),
         "artifact EOF disagrees with page geometry")
    need(length > 0 or result["eof"], "artifact page made no progress")
    text = result.get("data_base64")
    need(isinstance(text, str) and len(text) <= 4 * ((requested + 2) // 3),
         "artifact base64 exceeds the requested bound")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _Failure("protocol", "page", "invalid artifact base64") from error
    need(len(raw) == length and base64.b64encode(raw).decode("ascii") == text,
         "artifact base64 is noncanonical or has wrong length")
    return actual_size, raw, result["eof"]


def _fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(partial, target, published):
    os.link(partial, target)  # Atomic no-replace publication on the same filesystem.
    published()
    os.unlink(partial)
    _fsync_directory(target.parent)


async def recover_artifacts(client, plan, out, *, max_artifact_bytes,
                            max_total_bytes, max_calls, page_bytes=4096):
    """Copy explicit artifacts and independently verify complete byte identities.

    Returns a bounded outcome and, when local IO permits, manifest.json in a new
    output directory. Byte budgets count each logical output occurrence. Calls
    count helper dispatch attempts, not SDK retries. Cancellation propagates;
    interrupted files are preserved, with no promised final manifest or resume.
    """
    limits = dict(max_artifact_bytes=max_artifact_bytes, max_total_bytes=max_total_bytes,
                  max_calls=max_calls, page_bytes=page_bytes)
    outcome = dict(version=1, ok=False, exit=5, plan_sha256=None,
                   counts=dict(artifacts=0, published=0, verified=0, calls=0, bytes_written=0),
                   failure=None, manifest=None)
    try:
        snapshot, identity = _validate(plan, **limits)
        directory = Path(out)
        _need(not os.path.lexists(directory), "output directory already exists")
        _need(directory.parent.is_dir(), "output parent directory must already exist")
    except _Failure as error:
        outcome.update(exit=error.exit, failure=_diagnostic(error))
        return outcome
    except OSError as error:
        outcome.update(exit=5, failure=_diagnostic(_Failure("io", "preflight", str(error))))
        return outcome
    except (TypeError, ValueError) as error:
        failure = _Failure("plan", "preflight", str(error), 2)
        outcome.update(exit=2, failure=_diagnostic(failure))
        return outcome
    outcome["plan_sha256"] = identity
    counts = outcome["counts"]
    counts["artifacts"] = len(snapshot)
    states = [dict(index=i, name=item["name"], state="not_started", calls=0,
                   bytes_written=0, size=None, verified=False, published=False)
              for i, item in enumerate(snapshot)]
    manifest = dict(version=1, plan=snapshot, plan_sha256=identity,
                    limits=limits, artifacts=states)
    created, current, stage, interrupted = False, None, "mkdir", None
    try:
        directory.mkdir(exist_ok=False)
        created = True
        for current, (item, state) in enumerate(zip(snapshot, states)):
            state["state"] = "started"
            offset, size, stream = 0, None, None
            digest = hashlib.sha256()
            partial = directory / (".partial-%03d" % current)
            try:
                while True:
                    _need(counts["calls"] < max_calls, "artifact call budget exhausted", "budget", "dispatch")
                    available = max_total_bytes - counts["bytes_written"]
                    requested = max(1, min(page_bytes, available,
                                          max_artifact_bytes if size is None else size - offset))
                    arguments = {k: item[k] for k in ("directory", "kind", "digest")}
                    if "filename" in item:
                        arguments["filename"] = item["filename"]
                    arguments.update(offset=offset, length=requested, encoding="base64")
                    stage = "dispatch"
                    counts["calls"] += 1
                    state["calls"] += 1
                    try:
                        envelope = await client.call_tool("artifact_read", arguments)
                    except Exception as error:
                        raise _Failure("transport", "dispatch", str(error)) from error
                    size, raw, eof = _page(envelope, item, offset, requested, size)
                    state["size"] = size
                    _need(size <= max_artifact_bytes, "artifact exceeds byte budget", "budget", "page")
                    _need(size - offset <= available, "artifact exceeds remaining total budget", "budget", "page")
                    stage = "write"
                    if stream is None:
                        stream = partial.open("xb")
                    written = stream.write(raw)
                    counts["bytes_written"] += written
                    state["bytes_written"] += written
                    if written != len(raw):
                        raise _Failure("io", "write", "short artifact write")
                    digest.update(raw)
                    offset += written
                    if eof:
                        break
                if offset != size or digest.hexdigest() != item["digest"]:
                    raise _Failure("verification", "digest", "recovered bytes differ from planned SHA256")
                stage = "file_fsync"
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                stream = None
                state.update(verified=True, state="verified")
                counts["verified"] += 1
                stage = "publish"
                def published():
                    state.update(published=True, state="published")
                    counts["published"] += 1
                _publish(partial, directory / item["name"], published)
            finally:
                if stream is not None:
                    # Preserve the primary failure or cancellation. Successful
                    # writes already flush/fsync/close in the checked path.
                    with contextlib.suppress(OSError):
                        stream.close()
        outcome.update(ok=True, exit=0)
    except _Failure as error:
        outcome.update(exit=error.exit, failure=_diagnostic(error, current))
    except OSError as error:
        outcome.update(exit=5, failure=_diagnostic(_Failure("io", stage, str(error)), current))
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
        interrupted = error
        outcome.update(exit=5, failure=_diagnostic(_Failure("cancelled", stage, "recovery interrupted"), current))
    if created:
        # Manifest is a final snapshot, not a journal. A failed publication may
        # leave verified staging or a published target; state records that truth.
        manifest.update(ok=outcome["ok"], exit=outcome["exit"], counts=counts.copy(), failure=outcome["failure"])
        data = _canonical(manifest)
        if len(data) > 1048576:
            raise AssertionError("bounded recovery manifest exceeded1MiB")
        descriptor = dict(name="manifest.json", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                          verified=False, published=False)
        outcome["manifest"] = descriptor
        try:
            temporary = directory / ".manifest-partial"
            with temporary.open("xb") as stream:
                if stream.write(data) != len(data):
                    raise OSError("short manifest write")
                stream.flush()
                os.fsync(stream.fileno())
            descriptor["verified"] = True
            _publish(temporary, directory / "manifest.json", lambda: descriptor.update(published=True))
        except OSError as error:
            prior = outcome["failure"]
            outcome.update(ok=False, exit=5, failure=_diagnostic(_Failure("io", "manifest", str(error))))
            if prior is not None:
                outcome["prior_failure"] = prior
    if len(json.dumps(outcome, ensure_ascii=True, indent=2).encode("utf-8")) > 15360:
        raise AssertionError("bounded recovery outcome exceeded15360bytes")
    if interrupted is not None:
        raise interrupted
    return outcome
