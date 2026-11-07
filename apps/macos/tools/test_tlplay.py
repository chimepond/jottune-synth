#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Test the real wrapper's argument transport without launching a GUI."""
import contextlib
import io
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


def main():
    wrapper = Path(__file__).resolve().parent.parent / "tlplay"
    launch = runpy.run_path(str(wrapper), run_name="tlplay_test")["main"]
    checks = 0
    # Caller supplies an owned external directory; no system-tmp or WAV output.
    with tempfile.TemporaryDirectory(prefix="tlplay-", dir=sys.argv[1]) as tmp:
        root = Path(tmp)
        app = root / "app with spaces/IntegerFM.app"
        binary = app / "Contents/MacOS/IntegerFM"
        binary.parent.mkdir(parents=True)
        binary.write_text("test launch target\n")
        record = root / "Sodium é ' $() `ticks` & # ?.tl"
        record.write_text("native parsing remains in the app\n")
        with patch("subprocess.run") as run:
            assert launch([str(record), "--app", str(app)]) == 0
            args, kwargs = run.call_args
            assert args[0][:3] == ["/usr/bin/open", "-a", str(app.resolve())]
            parsed = urlparse(args[0][3])
            assert (parsed.scheme, parsed.netloc) == ("fmspike", "play")
            assert parse_qs(parsed.query) == {"path": [str(record.resolve())]}
            assert kwargs == {"check": True} and len(args[0]) == 4
            checks += 5
        for target, destination in [(root / "missing.tl", app), (binary, app),
                                    (record, root / "missing.app"), (root, app)]:
            with patch("subprocess.run") as run, contextlib.redirect_stderr(io.StringIO()):
                try:
                    launch([str(target), "--app", str(destination)])
                    raise AssertionError("invalid request launched")
                except SystemExit as error:
                    assert error.code == 2
                    run.assert_not_called()
                    checks += 1
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "open")), contextlib.redirect_stderr(io.StringIO()):
            try:
                launch([str(record), "--app", str(app)])
                raise AssertionError("launch failure was hidden")
            except SystemExit as error:
                assert error.code == 1
                checks += 1
        assert record.read_text() == "native parsing remains in the app\n"
        checks += 1
    print(f"TLPLAY checks={checks} ok")


if __name__ == "__main__":
    main()
