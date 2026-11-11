"""Key + BPM of an audio file, printed as JSON. Runs in the MUSICXX venv (needs essentia).
Usage: python analyze.py song.wav"""
import json, sys
import essentia
import essentia.standard as es

essentia.log.warningActive = essentia.log.infoActive = False  # keep stdout clean JSON
PC = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
      "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}


def camelot(key, scale):
    """Camelot wheel code: C major = 8B, A minor = 8A, each fifth up = +1."""
    pc = PC[key] + (3 if scale == "minor" else 0)  # minor keys share a number with their relative major
    n = (8 + 7 * pc) % 12 or 12
    return f"{n}{'A' if scale == 'minor' else 'B'}"


def fold(bpm):
    """Half / double into 70-180, the range DJ software shows."""
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return bpm


if __name__ == "__main__":
    audio = es.MonoLoader(filename=sys.argv[1], sampleRate=44100)()
    bpm, _, confidence, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    if confidence < 1.5:  # unsure (triplet hi-hats fool it by 3:2 or 4:3): a second, different estimator decides
        other = es.PercivalBpmEstimator()(audio)
        bpm = other if abs(other - bpm) / bpm > 0.03 else bpm
    bpm = fold(bpm)
    key, scale, _ = es.KeyExtractor(profileType="edma")(audio)  # "edma" profile is tuned for modern electronic/pop
    assert camelot("C", "major") == "8B" and camelot("A", "minor") == "8A" and camelot("E", "major") == "12B" and fold(65) == 130 and fold(190) == 95
    print(json.dumps({"bpm": round(bpm), "key": f"{key} {scale}", "camelot": camelot(key, scale)}))
