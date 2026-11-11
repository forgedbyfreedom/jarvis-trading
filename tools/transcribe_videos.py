"""Transcribe the app explainer videos with Whisper large-v3-turbo (full beam, no vocabulary prompt)."""
import glob, json, os, sys
sys.path.insert(0, r"C:\jarvis-trading")
import stt

out = {}
for f in sorted(glob.glob(r"C:\jarvis-trading\data\videos\*.wav")):
    segs, info = stt.model().transcribe(f, language="en", beam_size=5, vad_filter=False)
    out[os.path.basename(f)[:-4]] = " ".join(s.text.strip() for s in segs)
json.dump(out, open(r"C:\jarvis-trading\data\videos\transcripts.json", "w", encoding="utf-8"), indent=2)
for k, v in out.items():
    print(f"== {k} ({len(v.split())} words)\n{v}\n")
