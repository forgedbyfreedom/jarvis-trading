"""Add a document to the FBF RAG library in one /ingest call (the service chunks it).
Send the whole document: separate calls under the same source name overwrite each other's chunks.
Usage: python tools/ingest_doc.py <file> <source-name>"""
import os, sys, requests

def secret(name):
    for f in (r"C:\jarvis-trading\.env.local", os.path.expanduser(r"~\fbf-ops\credentials.env")):
        if os.path.exists(f):
            for line in open(f, encoding="utf-8"):
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get(name, "")

if __name__ == "__main__":
    path, source = sys.argv[1], sys.argv[2]
    url = (secret("FBF_AI_URL") or "https://ai.serverborn.com").rstrip("/")
    r = requests.post(url + "/ingest", json={"source": source, "text": open(path, encoding="utf-8").read()},
                      headers={"X-API-Key": secret("FBF_AI_API_KEY")}, timeout=300)
    print(r.status_code, r.text[:300])
