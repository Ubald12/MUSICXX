"""Give a song a 16-bar DJ intro: 16 bars of its own beat (vocals removed) before the vocal comes in.
Runs in the Python that has essentia; Demucs runs in the Python given as the third argument.
Usage: python intro.py song.wav "song (16 Bar Intro).wav" /path/to/python-with-demucs"""
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


def check():
    beats = np.arange(0, 120, 0.5)  # 120 BPM, 2 s bars
    song = np.random.default_rng(0).normal(0, 0.1, (60 * RATE, 2)).astype(np.float32)
    out, v = build(song, song * 0.5, beats, 8.1)
    assert v == 16 and len(out) == len(song) - 8 * RATE + 32 * RATE, (v, len(out))
    vox = np.zeros(30 * RATE, np.float32)
    vox[10 * RATE:] = np.sin(np.arange(20 * RATE) * 0.05)
    assert abs(vocal_start(vox, 0.5) - 10) < 0.2


if __name__ == "__main__":
    check()
    song, out, demucs_py = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
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
    start = vocal_start(vocals[:n].mean(1), float(np.median(np.diff(beats))))
    if start is None:
        sys.exit("No vocals found in this song, so it doesn't need an intro.")
    mixed, v = build(audio, nv, beats, start)
    if mixed is None:
        sys.exit("The vocal starts too late in this song to add an intro.")
    print(f"Vocal starts at {start:.1f} s (beat {v + 1}). Writing the intro…", flush=True)
    es.AudioWriter(filename=str(out), format=out.suffix[1:], sampleRate=RATE)(np.ascontiguousarray(mixed, np.float32))
    print(f"Intro saved: {out.name}", flush=True)
