"""Time the streaming coach: library time, first words, first full sentence, total."""
import json, re, time, urllib.request

q = "How should I adjust water and sodium in peak week for men's physique?"
req = urllib.request.Request("http://127.0.0.1:8000/api/coach/stream", data=json.dumps({"question": q, "history": []}).encode(),
                             headers={"Content-Type": "application/json"})
t0 = time.time(); first = sent = None; text = ""
with urllib.request.urlopen(req, timeout=400) as r:
    for line in r:
        ev = json.loads(line)
        if "sources" in ev:
            print(f"library done {time.time()-t0:.1f}s  sources={ev['sources'][:3]}")
        if "delta" in ev:
            text += ev["delta"]
            if first is None: first = time.time() - t0
            if sent is None and re.search(r"[.!?]\s", text): sent = time.time() - t0
        if ev.get("error"): print("ERROR", ev["error"])
        if ev.get("done"): print("done", ev)
print(f"first words {first:.1f}s, first sentence {sent:.1f}s, total {time.time()-t0:.1f}s, {len(text.split())} words")
t = time.time()
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/api/say", data=json.dumps({"text": text.split('.')[0][:200]}).encode(),
                                              headers={"Content-Type": "application/json"}), timeout=60).read()
print(f"first sentence voiced in {time.time()-t:.1f}s")
