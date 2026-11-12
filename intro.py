"""DJ edits of a song: a 16-bar intro (16 bars of its own beat, vocals removed, before the vocal comes in) or a
vocal outro (the last 16 bars with singing play as the isolated vocals, then the song ends).
Runs in the Python that has essentia; Demucs runs in the Python given as the third argument.
Usage: python intro.py song.wav "song (16 Bar Intro).wav" /path/to/python-with-demucs [intro|outro]"""
import subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import essentia
import essentia.standard as es

essentia.log.warningActive = essentia.log.infoActive = False
RATE, BARS = 44100, 16


def vocal_start(vocals, beat):
    """Seconds where the singing starts: the first moment that stays clearly audible for a couple of seconds."""
    hop = RATE // 10
    rms = np.sqrt(np.convolve(vocals ** 2, np.ones(hop) / hop, "valid")[::hop])
    loud = rms > 0.15 * np.percentile(rms, 95)  # ad-libs and reverb tails are quieter than the actual singing
    need = int(2 * beat * 10)  # half a bar
    for i in range(len(loud) - need):
        if loud[i:i + need].mean() > 0.7:
            return i / 10
    return None


def build(song, nv, beats, start):
    """16 bars of instrumental from the vocal entry, then the song from that same beat: a 64-beat phrase loop, so
    the join is musically seamless even when the vocal enters on a pickup rather than the downbeat."""
    v = int(np.argmin(np.abs(beats - start)))
    if v + 4 * BARS >= len(beats):
        return None, 0
    a, b = (int(round(beats[i] * RATE)) for i in (v, v + 4 * BARS))
    fade = 441  # 10 ms
    intro, rest = nv[a:b].copy(), song[a:].copy()
    ramp = np.linspace(0, 1, fade)[:, None]
    rest[:fade] = rest[:fade] * ramp + nv[b:b + fade] * (1 - ramp)  # the beat carries on under the first vocal
    return np.concatenate([intro, rest]), v


def outro(song, vocals, beats, end):
    """The song until 16 bars before the last vocal, then only the vocals for those 16 bars (plus a beat to ring out)."""
    e = int(np.searchsorted(beats, end))  # first beat after the singing stops
    if e < 4 * BARS:
        return None
    a = int(round(beats[e - 4 * BARS] * RATE))
    beat = int(round(np.median(np.diff(beats)) * RATE))
    b = min(len(vocals), int(round(beats[min(e, len(beats) - 1)] * RATE)) + beat)
    tail = vocals[a:b].copy()
    fade = 441
    ramp = np.linspace(0, 1, fade)[:, None]
    tail[:fade] = tail[:fade] * ramp + song[a:a + fade] * (1 - ramp)
    tail[-beat:] *= np.linspace(1, 0, beat)[:, None] if beat <= len(tail) else 1
    return np.concatenate([song[:a], tail])


def check():
    beats = np.arange(0, 120, 0.5)  # 120 BPM, 2 s bars
    song = np.random.default_rng(0).normal(0, 0.1, (60 * RATE, 2)).astype(np.float32)
    out, v = build(song, song * 0.5, beats, 8.1)
    assert v == 16 and len(out) == len(song) - 8 * RATE + 32 * RATE, (v, len(out))
    vox = np.zeros(30 * RATE, np.float32)
    vox[10 * RATE:] = np.sin(np.arange(20 * RATE) * 0.05)
    assert abs(vocal_start(vox, 0.5) - 10) < 0.2
    assert abs(30 - vocal_start(vox[::-1], 0.5) - 30) < 0.2  # vocal end, found by reading backwards
    out = outro(song, song * 0.5, beats, 50.2)  # singing stops at 50.2 s: next beat is 50.5, 16 bars before is 18.5
    assert len(out) == int(18.5 * RATE) + int(32.5 * RATE) and np.allclose(out[20 * RATE], song[20 * RATE] * 0.5)


if __name__ == "__main__":
    check()
    song, out, demucs_py = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) > 4 else "intro"
    audio, rate = es.AudioLoader(filename=str(song))()[:2]
    if rate != RATE:
        audio = np.stack([es.Resample(inputSampleRate=rate, outputSampleRate=RATE)(np.ascontiguousarray(audio[:, c]))
                          for c in (0, 1)], 1)
    beats = np.array(es.RhythmExtractor2013(method="multifeature")(audio.mean(1))[1])
    print("Removing the vocals…", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([demucs_py, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals", "-o", tmp,
                        "--filename", "{stem}.{ext}", str(song)], check=True)
        stems = Path(tmp) / "htdemucs"
        vocals = es.AudioLoader(filename=str(stems / "vocals.wav"))()[0]
        nv = es.AudioLoader(filename=str(stems / "no_vocals.wav"))()[0]
    n = min(len(audio), len(nv))
    audio, nv = audio[:n], nv[:n]
    mono, beat = vocals[:n].mean(1), float(np.median(np.diff(beats)))
    start = vocal_start(mono, beat)
    if start is None:
        sys.exit("No vocals found in this song.")
    if mode == "outro":
        end = n / RATE - vocal_start(mono[::-1], beat)
        mixed = outro(audio, vocals[:n], beats, end)
        if mixed is None:
            sys.exit("The vocals in this song are too short for a 16-bar outro.")
        print(f"Vocals end at {end:.1f} s. Writing the vocal outro…", flush=True)
        es.AudioWriter(filename=str(out), format=out.suffix[1:], sampleRate=RATE)(np.ascontiguousarray(mixed, np.float32))
        print(f"Outro saved: {out.name}", flush=True)
        sys.exit()
    mixed, v = build(audio, nv, beats, start)
    if mixed is None:
        sys.exit("The vocal starts too late in this song to add an intro.")
    print(f"Vocal starts at {start:.1f} s (beat {v + 1}). Writing the intro…", flush=True)
    es.AudioWriter(filename=str(out), format=out.suffix[1:], sampleRate=RATE)(np.ascontiguousarray(mixed, np.float32))
    print(f"Intro saved: {out.name}", flush=True)
