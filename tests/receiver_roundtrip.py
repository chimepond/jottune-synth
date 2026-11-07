#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Compare native MIDI round trips and optional real open-source receivers.

No installs, device I/O or retained audio. Native tests require an already-built
harness. Select sfizz_render and/or FluidSynth plus a SoundFont explicitly;
omitted receivers are unrun, never counted as passing. Waveform measurements
have no adopted perceptual threshold. Preserve the original TL for exact sound.
"""
from array import array
import argparse
import copy
import io
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tools/music")]
import compose
import fm
import interchange
import midi2tl
import midi_export


def pcm(data):
    with wave.open(io.BytesIO(data) if isinstance(data, bytes) else str(data), "rb") as wav:
        assert wav.getsampwidth() == 2 and wav.getcomptype() == "NONE"
        words = array("h", wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            words.byteswap()
        return words, wav.getframerate(), wav.getnchannels()


def delta(a, b):
    """Unaligned, unscaled, zero-padded error; no score threshold or time warp."""
    count = max(len(a), len(b))
    assert count
    errors = [(b[i] if i < len(b) else 0) - (a[i] if i < len(a) else 0)
              for i in range(count)]
    energy = sum(x * x for x in a)
    squared = sum(x * x for x in errors)
    common = min(len(a), len(b))
    common_squared = sum(x * x for x in errors[:common])
    common_energy = sum(x * x for x in a[:common])
    return dict(source_samples=len(a), returned_samples=len(b),
                unequal_samples=sum(x != 0 for x in errors),
                max_error_pcm16=max(map(abs, errors)),
                rms_error_pcm16=math.sqrt(squared / count),
                snr_db=10 * math.log10(energy / squared) if energy and squared else None,
                exact_equal=list(a) == list(b), alignment_frames=0, gain_adjusted=False,
                zero_padded=True, common_window_samples=common,
                common_window_rms_error_pcm16=math.sqrt(common_squared / common) if common else None,
                common_window_snr_db=10 * math.log10(common_energy / common_squared)
                    if common_energy and common_squared else None)


def rows(accepted):
    return [r for r in accepted["_rows"] if r["section"] == "event"]


def event_delta(a, b):
    """Ordinal pairing for these unambiguous single-voice fixtures, no best fit."""
    assert len(a) == len(b), "lost or extra performance event"
    result = dict(events=len(a), max_time_ns=0, max_pitch_cents=0.0, max_velocity_q15=0)
    for left, right in zip(a, b):
        assert left["id"] == right["id"], "changed performance event kind/order"
        x, y = left["raw"], right["raw"]
        result["max_time_ns"] = max(result["max_time_ns"], abs(x["T_NS"] - y["T_NS"]))
        if "FREQ_MILLIHZ" in x:
            result["max_pitch_cents"] = max(result["max_pitch_cents"],
                abs(1200 * math.log2(y["FREQ_MILLIHZ"] / x["FREQ_MILLIHZ"])))
        if "VELOCITY_Q15" in x:
            result["max_velocity_q15"] = max(result["max_velocity_q15"],
                abs(x["VELOCITY_Q15"] - y["VELOCITY_Q15"]))
    return result


def aligned_observation(source, returned, silence):
    """Report a bounded lag/gain fit separately; never turn it into a pass rule."""
    start, end = 64, min(len(source), len(returned)) - 4
    x = source[start:end]
    energy = sum(v*v for v in x)
    trials = []
    for lag in range(-4,5):
        y = [returned[i+lag]-silence for i in range(start,end)]
        gain = sum(a*b for a,b in zip(x,y)) / energy
        error = sum((b-gain*a)**2 for a,b in zip(x,y))
        trials.append(dict(lag_frames=lag,gain=gain,rms_error_pcm16=math.sqrt(error/len(x))))
    return dict(hypothesis_only=True,quality_threshold=None,source_window_frames=[start,end],
                silence_bias_subtracted=silence,lag_search_frames=[-4,4],
                best_fit=min(trials,key=lambda t:t["rms_error_pcm16"]),
                caveat="Excludes the first 64 frames; describes these pairs only, not a correction or receiver guarantee")


def fixture(native, directory, *, fractional=False):
    p = compose.Piece("roundtrip", 120, 4, "Independent note-performance control")
    p.dir = str(directory)
    p.length_ns = 1_200_000_000
    p.stream("notes", "lead")
    p.patch("bell", *compose.recipe("bell"))
    p.add_voices("bell", 1, "explicit single voice")
    for at, frequency, velocity in ((0, 440000, 32767), (400000000, 523251, 20641),
                                    (800000000, 659255, 32767)):
        p.on_f("notes", at + (123456 if fractional else 0), 0, frequency, velocity, "")
        if fractional:
            p.pitch_f("notes", at + 100123456, 0, frequency + 1000, "")
        p.off("notes", at + 200000000 + (654321 if fractional else 0), 0)
    p.write()
    accepted = fm.native_render(native, p.files(), want_wav=True)
    assert accepted["ok"]
    return accepted


def native_roundtrips(native, out):
    results = []
    for fractional in (False, True):
        original = fixture(native, out / ("fractional" if fractional else "plain"), fractional=fractional)
        x, fs, channels = pcm(original["_wav_bytes"])
        returned_audio = []
        for kind in (0, 1):
            data, report = midi_export.encode(original, channels={0: 1}, midi_format=kind)
            config = midi2tl.cfg_of("--map", "1=bell:notes", "--voices", "1", "--tail", "200",
                                   "--rpn", "--fs", "48000")
            conversion = midi2tl.Conversion(data, config)
            assert not conversion.problems(), conversion.problems()
            p = conversion.piece("returned", str(out / f"returned-{fractional}-{kind}"), "performance.mid")
            p.write()
            returned = fm.native_render(native, p.files(), want_wav=True)
            assert returned["ok"]
            # The same recipe is explicit. The importer applies no automatic levelling here.
            patch = lambda r: [{k:v for k,v in row["raw"].items() if k != "PATCH"}
                               for row in r["_rows"] if row["section"] == "patch"]
            assert patch(original) == patch(returned)
            y, rfs, rchannels = pcm(returned["_wav_bytes"])
            assert (rfs, rchannels) == (fs, channels)
            notes = event_delta(rows(original), rows(returned))
            assert notes["max_time_ns"] <= report["timing"]["max_abs_error_ns"] + 1
            assert notes["max_velocity_q15"] <= report["velocity"]["max_abs_imported_error_q15"]
            minimum_frequency = min(r["raw"]["FREQ_MILLIHZ"] for r in rows(original)
                                    if "FREQ_MILLIHZ" in r["raw"])
            # The importer rounds its returned frequency to integer millihertz.
            millihertz_quantum = 1200 * math.log2(1 + 1 / minimum_frequency)
            assert notes["max_pitch_cents"] <= report["pitch"]["max_abs_error_cents"] + millihertz_quantum
            record = out / f"returned-{fractional}-{kind}.tl"
            record.write_bytes(returned["_record_bytes"])
            assert fm.native_render(native, [record], want_wav=True)["_wav_bytes"] == returned["_wav_bytes"]
            results.append(dict(case="fractional" if fractional else "plain", midi_format=kind,
                                events=notes, waveform=delta(x,y), import_tail_ms=200,
                                patch="explicit original bell recipe; no level fitting"))
            returned_audio.append(returned["_wav_bytes"])
        assert returned_audio[0] == returned_audio[1], "SMF0/1 changed imported playback"
    # Sensitive comparators must expose deliberate missing events and changes.
    base = rows(original)
    for field, value in (("T_NS", 12345678), ("FREQ_MILLIHZ", 220000), ("VELOCITY_Q15", 1)):
        changed = copy.deepcopy(base)
        changed[0]["raw"][field] = value
        assert event_delta(base, changed) != event_delta(base, base)
    try:
        event_delta(base, base[:-1])
    except AssertionError:
        pass
    else:
        raise AssertionError("missing-event control was not detected")
    assert delta([1,2,3],[1,2,3])["exact_equal"]
    assert delta([1,2,3],[1,2,4])["max_error_pcm16"] == 1
    return results


def midi_fixture(notes, end_ms=1200):
    """Independent SMF0 fixture: 500PPQN/500000us, one tick per millisecond."""
    events = [(0, b"\xff\x51\x03\x07\xa1\x20")]
    for on, off, key, velocity in notes:
        events += [(on, bytes((0x90,key,velocity))), (off, bytes((0x80,key,0)))]
    # sfizz 1.2.3 ignores a trailing meta-event's delta with --use-eot.
    events.append((end_ms, bytes((0x80,127,0))))
    events.sort(key=lambda x:x[0])
    track, previous = bytearray(), 0
    for when, message in events:
        n, encoded = when - previous, [((when - previous) & 127)]
        while n > 127:
            n >>= 7
            encoded.insert(0,128 | (n & 127))
        track.extend(bytes(encoded) + message)
        previous = when
    track.extend(b"\0\xff\x2f\0")
    return b"MThd" + struct.pack(">IHHH",6,0,1,500) + b"MTrk" + struct.pack(">I",len(track)) + track


def run(command):
    completed = subprocess.run([str(x) for x in command], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed


def sfz_receiver(native, harness, out, receiver):
    help_ = run([receiver,"--help"])
    assert "--sfz" in help_.stdout and "--quality" in help_.stdout
    bank = out / "bank"
    exported = interchange.export(ROOT / "tests/golden/48000/bell.tl", harness=harness,
        profile="sfz-bank", patch="bell", notes=[60,64], velocities=[40,100], hold_ms=100,
        tail_ms=200, max_samples=4, accept_loss=True, receiver_profile="sfizz-ram", out=bank)
    assert exported["ok"] and interchange.verify(bank)["ok"]
    count = 0
    diagnostics = set()

    def render(sfz, notes, end_ms=1200, data=None):
        nonlocal count
        count += 1
        mid, wav = out / f"receiver-{count}.mid", out / f"receiver-{count}.wav"
        mid.write_bytes(midi_fixture(notes,end_ms) if data is None else data)
        result = run([receiver,"--sfz",sfz,"--midi",mid,"--wav",wav,"--samplerate","48000",
                      "--blocksize","64","--quality","2","--polyphony","64","--use-eot"])
        diagnostics.update(line for line in result.stderr.splitlines() if line.strip())
        values, fs, channels = pcm(wav)
        assert (fs,channels) == (48000,2) and values
        return values

    instrument = bank / "instrument.sfz"
    silence = render(instrument,[(0,10,61,40)])
    assert min(silence) == max(silence)
    baseline, observations = {}, []
    for key in (60,64):
        references = {}
        for velocity in (40,100):
            sample = f"n{key:03d}-v{velocity:03d}.wav"
            oracle = bank / f"oracle-{key}-{velocity}.sfz"
            oracle.write_text(f"<control> hint_ram_based=1\n<region> sample={sample} key={key} "
                              f"pitch_keycenter={key} amp_veltrack=0 loop_mode=one_shot\n")
            references[velocity] = render(oracle,[(0,10,key,velocity)])
            assert references[velocity] != silence
            # Detect a cut sample past the receiver's former 8192-frame preload edge.
            assert references[velocity][8192*2:14400*2] != silence[8192*2:14400*2]
        assert references[40] != references[100]
        for velocity in (1,40,70,71,100,127):
            actual = render(instrument,[(0,10,key,velocity)])
            assert actual == references[40 if velocity <= 70 else 100], "wrong sample/layer or receiver cutoff"
        assert render(instrument,[(0,400,key,40)]) == references[40], "one-shot reacted to key-up"
        baseline[key] = references[40]
        source, fs, channels = pcm(bank / f"n{key:03d}-v040.wav")
        observations.append(dict(key=key, comparison="native mono vs receiver left; no gain/bias fit",
            waveform=delta(source,references[40][0::2]),
            separate_aligned_fit=aligned_observation(source,references[40][0::2],silence[0])))
    moved = out / "relocated bank with spaces"
    shutil.copytree(bank,moved)
    assert interchange.verify(moved)["ok"]
    assert render(moved / "instrument.sfz",[(0,10,60,40)]) == baseline[60]
    # Deliberate export mutations must change playback and fail bundle verification.
    for mutation in ("velocity","note-off","missing"):
        directory = out / mutation
        shutil.copytree(bank,directory)
        sfz = directory / "instrument.sfz"
        if mutation == "velocity":
            sfz.write_text(sfz.read_text().replace("amp_veltrack=0","amp_veltrack=100"))
            assert render(sfz,[(0,10,60,1)]) != render(sfz,[(0,10,60,70)])
        elif mutation == "note-off":
            sfz.write_text(sfz.read_text().replace("loop_mode=one_shot","loop_mode=no_loop"))
            assert render(sfz,[(0,10,60,40)]) != render(sfz,[(0,400,60,40)])
        else:
            (directory / "n060-v040.wav").unlink()
            assert render(sfz,[(0,10,60,40)]) == silence
        assert not interchange.verify(directory)["ok"]
    whole = out / "whole-song"
    assert interchange.export(ROOT / "tests/golden/fx/crush-stereo.tl", harness=harness,
        profile="sfz", key=72, receiver_profile="sfizz-ram", out=whole)["ok"]
    stereo, fs, channels = pcm(whole / "audio.wav")
    assert channels == 2
    end_ms = math.ceil(len(stereo) / channels / fs * 1000) + 200
    first = render(whole / "instrument.sfz",[(0,10,72,1)],end_ms)
    assert first == render(whole / "instrument.sfz",[(0,end_ms-100,72,127)],end_ms)
    assert min(first) != max(first)
    source = fm.native_render(native,[bank / "n060-v040.tl"])
    projected = [render(instrument,[],data=midi_export.encode(source,channels={0:1},midi_format=k)[0])
                 for k in (0,1)]
    assert projected[0] == projected[1] and min(projected[0]) != max(projected[0])
    return dict(renders=count, profile="sfizz-ram", bank_observations=observations,
                stereo_whole_song=delta(stereo,first),
                diagnostics=sorted(line.replace(str(out),"<temporary>") for line in diagnostics),
                native_audio_equivalence=False, physical_playback=False)


def fluid_receiver(native, out, receiver, soundfont):
    version = run([receiver,"--version"]).stdout.splitlines()[0]
    original = fixture(native,out / "fluid-source")
    x, fs, channels = pcm(original["_wav_bytes"])
    def render(data, label, font=soundfont):
        mid, wav = out / (label+".mid"), out / (label+".wav")
        mid.write_bytes(data)
        run([receiver,"-ni","-R","0","-C","0","-r",str(fs),"-F",wav,"-T","wav","-O","s16",
             "-o","synth.cpu-cores=1",font,mid])
        values, rfs, rchannels = pcm(wav)
        assert (rfs,rchannels)==(fs,2) and values
        return values
    played=[render(midi_export.encode(original,channels={0:1},midi_format=k)[0],f"fluid-{k}")
            for k in (0,1)]
    assert played[0] == played[1], "SMF0/1 changed external playback"
    silence=render(midi_fixture([],1200),"fluid-silence")
    assert min(silence)>=-1 and max(silence)<=1, "unexpected receiver background signal"
    assert max(map(abs,played[0]))>max(map(abs,silence))
    shifted=copy.deepcopy(original)
    for event in rows(shifted):
        if "FREQ_MILLIHZ" in event["raw"]:
            event["raw"]["FREQ_MILLIHZ"]*=2
    assert render(midi_export.encode(shifted,channels={0:1})[0],"fluid-octave") != played[0]
    # A generic SoundFont changes the sound. The mono mean is explicit, not hidden alignment.
    mono=[(a+b)/2 for a,b in zip(played[0][0::2],played[0][1::2])]
    return dict(receiver=version,renders=4,comparison="native mono vs receiver stereo mean",
                waveform=delta(x,mono),soundfont=soundfont.name,
                native_audio_equivalence=False,physical_playback=False)


def audio_roundtrip(native, harness, out):
    # Optional existing NumPy analysis, with only WAV + an explicit candidate set.
    p = compose.Piece("separated",120,4,"Separated mono inverse control")
    p.dir = str(out / "audio-source")
    p.length_ns = 3_800_000_000
    p.stream("notes","separated notes")
    expected = [(200_000_000,500_000_000,220000,"marimba"),
                (1_700_000_000,2_000_000_000,330000,"organ")]
    for voice,(on,off,frequency,recipe) in enumerate(expected):
        p.patch(recipe,*compose.recipe(recipe))
        p.add_voices(recipe,1,"explicit source voice")
        p.on_f("notes",on,voice,frequency,32767,"")
        p.off("notes",off,voice)
    p.write()
    original=fm.native_render(native,p.files(),want_wav=True)
    source=out / "audio-source.wav"
    source.write_bytes(original["_wav_bytes"])
    results=[]
    for name,options in (("baseline",[]),("aligned-hold",["--align-ms","5","--fit-hold"])):
        result=fm.transcribe(str(source),out / name,harness=harness,
            options=["--recipes","marimba","organ",*options],native_budget=512)
        assert result["ok"],result
        report=json.loads((out / name / "report.json").read_text())
        assert len(report["notes"])==len(expected)
        notes=[]
        for truth,recovered in zip(expected,report["notes"]):
            on,off,frequency,recipe=truth
            notes.append(dict(recipe_correct=recipe==recovered["recipe"],
                onset_error_ms=(recovered["start_ns"]-on)/1e6,
                off_error_ms=(recovered["estimated_off_ns"]-off)/1e6,
                pitch_error_cents=1200*math.log2(recovered["frequency_millihz"]/frequency)))
        assert all(note["recipe_correct"] for note in notes), "wrong candidate recipe in separated-note control"
        reconstructed=fm.native_render(native,[out / name / "canonical.tl"],want_wav=True)
        assert reconstructed["_wav_bytes"]==(out / name / "rerender.wav").read_bytes()
        x,fs,channels=pcm(original["_wav_bytes"])
        y,rfs,rchannels=pcm(reconstructed["_wav_bytes"])
        assert (fs,channels)==(rfs,rchannels)==(48000,1)
        results.append(dict(profile=name,notes=notes,waveform=delta(x,y),
            canonical_replay_exact=True,original_tl_given_to_decoder=False))
    return dict(cases=results,candidate_recipes=["marimba","organ"],
                general_audio_inverse=False,polyphonic_or_fx_recovery=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness",type=Path,default=fm.DEFAULT_HARNESS)
    parser.add_argument("--sfizz-render",type=Path)
    parser.add_argument("--fluidsynth",type=Path)
    parser.add_argument("--soundfont",type=Path)
    parser.add_argument("--transcribe",action="store_true",help="also exercise the existing mono transcriber; requires NumPy")
    args=parser.parse_args()
    if bool(args.fluidsynth) != bool(args.soundfont):
        parser.error("--fluidsynth and --soundfont must be supplied together")
    for path in (args.harness,args.sfizz_render,args.fluidsynth,args.soundfont):
        if path is not None and not path.is_file():
            parser.error("missing input: "+str(path))
    native=fm.Native(args.harness)
    with tempfile.TemporaryDirectory(prefix="jottune-receiver-") as temporary:
        out=Path(temporary)
        result=dict(native_roundtrips=native_roundtrips(native,out),sfizz="not run",fluidsynth="not run")
        if args.sfizz_render:
            result["sfizz"]=sfz_receiver(native,args.harness,out,args.sfizz_render.resolve())
        if args.fluidsynth:
            result["fluidsynth"]=fluid_receiver(native,out,args.fluidsynth.resolve(),args.soundfont.resolve())
        result["audio_transcription"]=audio_roundtrip(native,args.harness,out) if args.transcribe else "not run"
        print(json.dumps(result,sort_keys=True,allow_nan=False))
    print("PASS selected receiver/round-trip controls; no arbitrary WAV-to-TL or listening claim")


if __name__=="__main__":
    main()
