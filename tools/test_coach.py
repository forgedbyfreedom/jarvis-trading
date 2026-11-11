"""Quick end-to-end check of the Athena coach: RAG library + local model."""
import json, subprocess, time, urllib.request

def ask(q, hist):
    req = urllib.request.Request("http://127.0.0.1:8000/api/coach", data=json.dumps({"question": q, "history": hist}).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    d = json.load(urllib.request.urlopen(req, timeout=400))
    print(f"== {q}\nmodel={d.get('model')} grounded={d.get('grounded')} secs={d.get('seconds')} wall={time.time()-t:.0f}s")
    print((d.get("answer") or d.get("error") or "")[:700], "\n")
    return d

a = ask("What is the half-life of BPC-157 and how often is it typically dosed in the research?", [])
ask("And what about TB-500?", [{"role": "user", "text": "What is the half-life of BPC-157?"}, {"role": "ai", "text": a.get("answer", "")}])
print(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True, text=True).stdout)
