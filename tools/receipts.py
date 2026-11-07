# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Explicit, durable operation evidence, independent of the synth and transport."""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import fcntl
import functools
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time


COLUMNS = ("version", "attempt", "request_sha256", "response_sha256")
PAGE_BYTES = 15360
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReceiptError(Exception):
    def __init__(self, message, exit=5, **details):
        super().__init__(message)
        self.exit, self.details = exit, details


def require(condition, message, exit=5):
    if not condition:
        raise ReceiptError(message, exit)


def protected(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, UnicodeError, csv.Error) as error:
            raise ReceiptError(str(error)) from error
    return call


def canonical_bytes(value):
    """Canonical strict JSON; reject values whose JSON encoding loses intent."""
    def validate(item):
        if isinstance(item, dict):
            require(all(isinstance(key, str) for key in item), "receipt JSON keys must be strings", 2)
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        else:
            require(item is None or type(item) in (bool, int, float, str),
                    "receipt values must be JSON values", 2)
            require(not isinstance(item, float) or math.isfinite(item),
                    "receipt JSON numbers must be finite", 2)
    validate(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise ReceiptError("receipt JSON cannot be encoded: " + str(error), 2) from error


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate receipt JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ReceiptError("nonfinite receipt JSON number: " + value)
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        require(canonical_bytes(value) == data, "receipt document is not canonical JSON")
        return value
    except (ValueError, UnicodeError) as error:
        raise ReceiptError("invalid receipt JSON: " + str(error)) from error
    except ReceiptError as error:
        if error.exit != 5:
            raise ReceiptError(str(error)) from error
        raise


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(pending)


@contextlib.contextmanager
def locked(directory, create=False):
    directory = Path(directory).resolve()
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    else:
        require((directory / "operations.tsv").is_file(), "operation journal does not exist")
    with (directory / ".operations.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield directory


def _artifact(directory, data):
    identity = digest(data)
    path = directory / "receipts" / (identity + ".json")
    if path.exists():
        require(path.read_bytes() == data, "existing receipt artifact is corrupt: " + identity)
    else:
        atomic(path, data)
    return identity


def _document(directory, identity):
    require(isinstance(identity, str) and DIGEST.fullmatch(identity), "invalid receipt artifact identity")
    data = (directory / "receipts" / (identity + ".json")).read_bytes()
    require(digest(data) == identity, "receipt artifact digest mismatch: " + identity)
    return _json(data)


def _index_bytes(rows):
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue().encode("utf-8")


def _read(directory, missing=False):
    path = directory / "operations.tsv"
    if missing and not path.exists():
        return [], [], _index_bytes([])
    data = path.read_bytes()
    csv.field_size_limit(sys.maxsize)
    reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""), delimiter="\t")
    require(tuple(reader.fieldnames or []) == COLUMNS, "unsupported operation journal")
    rows, documents, previous = list(reader), [], set()
    for index, row in enumerate(rows, 1):
        require(None not in row and all(isinstance(row.get(key), str) for key in COLUMNS)
                and row["version"] == "1" and row["attempt"] == str(index), "corrupt operation journal row")
        request = _document(directory, row["request_sha256"])
        require(isinstance(request, dict) and set(request) == {
            "version", "kind", "attempt", "operation", "request", "provenance", "parent_request_sha256",
            "purpose", "utc", "cwd"}, "invalid operation request document")
        require(type(request["version"]) is int and request["version"] == 1
                and request["kind"] == "operation_request" and type(request["attempt"]) is int
                and request["attempt"] == index and isinstance(request["operation"], str)
                and isinstance(request["request"], dict) and isinstance(request["provenance"], dict)
                and all(isinstance(request[key], str) for key in ("purpose", "utc", "cwd")),
                "invalid operation request fields")
        require(request["parent_request_sha256"] is None or isinstance(request["parent_request_sha256"], str)
                and request["parent_request_sha256"] in previous,
                "invalid operation request parent")
        require(row["request_sha256"] not in previous, "duplicate operation request identity")
        response = None
        if row["response_sha256"] != "-":
            response = _document(directory, row["response_sha256"])
            require(isinstance(response, dict) and set(response) == {
                "version", "kind", "request_sha256", "response", "full_result_sha256", "elapsed_seconds"},
                "invalid operation response document")
            require(type(response["version"]) is int and response["version"] == 1
                    and response["kind"] == "operation_response"
                    and response["request_sha256"] == row["request_sha256"]
                    and isinstance(response["response"], dict) and "receipt" not in response["response"]
                    and type(response["elapsed_seconds"]) in (int, float)
                    and response["elapsed_seconds"] >= 0, "invalid operation response fields")
            full = response["full_result_sha256"]
            if full is not None:
                require(isinstance(_document(directory, full), dict), "full operation result must be an object")
        documents.append((request, response))
        previous.add(row["request_sha256"])
    require(_index_bytes(rows) == data, "operation journal is not canonical TSV")
    return rows, documents, data


def metadata(ticket):
    """A bounded reference; directory remains the caller's explicit argument."""
    return dict(version=1, attempt=ticket["attempt"], state="pending",
                request_sha256=ticket["request_sha256"], response_sha256=None, full_result_sha256=None)


@protected
def begin(directory, operation, request, provenance, parent=None, purpose=""):
    require(isinstance(operation, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", operation),
            "receipt operation must be a function name", 2)
    require(isinstance(purpose, str), "receipt_purpose must be a string", 2)
    require(parent is None or isinstance(parent, str) and DIGEST.fullmatch(parent),
            "receipt_parent must be a lowercase SHA256", 2)
    require(isinstance(request, dict) and isinstance(provenance, dict),
            "receipt request and provenance must be objects", 2)
    # Validate before creating a directory. Serialization also freezes caller data.
    request, provenance = _json(canonical_bytes(request)), _json(canonical_bytes(provenance))
    with locked(directory, create=True) as destination:
        rows, _, _ = _read(destination, missing=True)
        require(parent is None or any(row["request_sha256"] == parent for row in rows),
                "receipt parent is not a request in this operation journal", 2)
        attempt = len(rows) + 1
        document = dict(version=1, kind="operation_request", attempt=attempt, operation=operation,
                        request=request, provenance=provenance, parent_request_sha256=parent, purpose=purpose,
                        utc=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"), cwd=os.getcwd())
        identity = _artifact(destination, canonical_bytes(document))
        rows.append(dict(version="1", attempt=str(attempt), request_sha256=identity, response_sha256="-"))
        atomic(destination / "operations.tsv", _index_bytes(rows))
    return dict(directory=destination, attempt=attempt, request_sha256=identity, started=time.monotonic())


@protected
def complete(ticket, response, full_result_bytes=None):
    require(isinstance(response, dict) and "receipt" not in response,
            "receipt response must be an object without receipt metadata", 2)
    canonical_bytes(response)
    if full_result_bytes is not None:
        require(isinstance(full_result_bytes, bytes), "full result must be canonical JSON bytes", 2)
        require(isinstance(_json(full_result_bytes), dict), "full result must be a JSON object", 2)
    with locked(ticket["directory"]) as directory:
        rows, _, _ = _read(directory)
        attempt = ticket["attempt"]
        require(type(attempt) is int and 1 <= attempt <= len(rows), "receipt ticket attempt is absent")
        row = rows[attempt - 1]
        require(row["request_sha256"] == ticket["request_sha256"], "receipt ticket identity differs")
        require(row["response_sha256"] == "-", "operation receipt is already complete")
        full = _artifact(directory, full_result_bytes) if full_result_bytes is not None else None
        document = dict(version=1, kind="operation_response", request_sha256=ticket["request_sha256"],
                        response=response, full_result_sha256=full,
                        elapsed_seconds=max(0.0, time.monotonic() - ticket["started"]))
        identity = _artifact(directory, canonical_bytes(document))
        row["response_sha256"] = identity
        atomic(directory / "operations.tsv", _index_bytes(rows))
    return dict(metadata(ticket), state="complete", response_sha256=identity, full_result_sha256=full)


@protected
def show(directory, offset=0, limit=20, expected_index_sha256=None, project_entry=None):
    require(type(offset) is int and offset >= 0, "operation offset must be a nonnegative integer", 2)
    require(type(limit) is int and 1 <= limit <= 100, "operation limit must be from1 through100", 2)
    require(expected_index_sha256 is None or isinstance(expected_index_sha256, str)
            and DIGEST.fullmatch(expected_index_sha256), "expected_index_sha256 must be a lowercase SHA256", 2)
    with locked(directory) as destination:
        rows, documents, data = _read(destination)
    identity = digest(data)
    require(expected_index_sha256 is None or expected_index_sha256 == identity,
            "operation index SHA256 differs; restart discovery", 2)
    entries = []
    for row, (request, response) in zip(rows[offset:offset + limit], documents[offset:offset + limit]):
        result = response["response"] if response else {}
        entries.append(dict(attempt=int(row["attempt"]), operation=request["operation"],
                            state="complete" if response else "pending", request_sha256=row["request_sha256"],
                            response_sha256=row["response_sha256"] if response else None,
                            full_result_sha256=response["full_result_sha256"] if response else None,
                            parent_request_sha256=request["parent_request_sha256"],
                            purpose=request["purpose"][:160], purpose_truncated=len(request["purpose"]) > 160,
                            ok=result.get("ok"), exit=result.get("exit")))
        if project_entry is not None:
            entries[-1] = project_entry(entries[-1], request, response)
    reply = dict(version=1, ok=True, exit=0, kind="operations", index_sha256=identity, count=len(rows),
                 entries=entries, page={})
    while True:
        count = len(entries)
        reply["page"] = dict(offset=offset, limit=limit, total=len(rows), returned=count,
                             next_offset=offset + count if offset + count < len(rows) else None)
        if len(json.dumps(reply, ensure_ascii=True, indent=2, allow_nan=False).encode("utf-8")) <= PAGE_BYTES:
            return reply
        require(entries, "operation listing metadata exceeds byte limit", 2)
        entries.pop()
        require(entries or offset >= len(rows), "operation listing row exceeds byte limit", 2)
