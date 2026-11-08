"""
FORGED BY FREEDOM — RAG Chat Module
Queries local ChromaDB (fitness/nutrition/coaching knowledge base)
Generates grounded answers via Ollama LLM.
All local. No external APIs.
"""
import chromadb
import ollama
import os
import time
import logging
from typing import Optional

log = logging.getLogger("jarvis.chat")

# ── CONFIG (override via env vars if needed) ──────────────────────────
CHROMA_DB_PATH = os.environ.get("CHROMA_DB_PATH", "C:/AI/chroma_db")
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL    = os.environ.get("EMBED_MODEL", "nomic-embed-text")
LLM_MODEL      = os.environ.get("LLM_MODEL", "llama3")
COLLECTION     = os.environ.get("RAG_COLLECTION", "forged-freedom-ai")
TOP_K          = int(os.environ.get("RAG_TOP_K", "8"))

ollama_client = ollama.Client(host=OLLAMA_HOST)

# ── CHROMA CLIENT (lazy) ──────────────────────────────────────────────
_client: Optional[chromadb.PersistentClient] = None

def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        if not os.path.exists(CHROMA_DB_PATH):
            raise FileNotFoundError(f"ChromaDB not found at {CHROMA_DB_PATH}")
        _client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        log.info(f"Connected to ChromaDB at {CHROMA_DB_PATH}")
    return _client

def _get_collection():
    client = _get_client()
    try:
        return client.get_collection(COLLECTION)
    except Exception:
        # Fallback: grab the first collection
        collections = client.list_collections()
        if not collections:
            raise RuntimeError(f"No collections in {CHROMA_DB_PATH}. Run import_to_chroma_local.py first.")
        log.warning(f"Collection '{COLLECTION}' not found. Using '{collections[0].name}'")
        return client.get_collection(collections[0].name)

# ── EMBED ─────────────────────────────────────────────────────────────
def _embed(text: str) -> list[float]:
    resp = ollama_client.embed(model=EMBED_MODEL, input=[text])
    return resp["embeddings"][0]

# ── SEARCH ────────────────────────────────────────────────────────────
def search(query: str, top_k: int = TOP_K) -> list[dict]:
    """Vector search against the local ChromaDB."""
    coll = _get_collection()
    qv = _embed(query)
    results = coll.query(
        query_embeddings=[qv],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )
    hits = []
    if results and results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] and results["metadatas"][0] else {}
            dist = results["distances"][0][i] if results["distances"] and results["distances"][0] else 0.0
            hits.append({
                "id":       doc_id,
                "text":     results["documents"][0][i],
                "metadata": meta,
                "distance": dist,
                "score":    round(max(0, 1 - dist), 4),
            })
    return hits

# ── PROMPT ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are FORGED BY FREEDOM AI — a personal knowledge assistant for a fitness coach.
Your knowledge base contains:
- Interviews with elite coaches (Greg Doucette, Layne Norton, etc.)
- Nutrition protocols, supplement research, training programs
- Coaching notes, member programs, Q&A transcripts

RULES:
1. Ground EVERY answer in the provided context chunks. Do not invent.
2. Cite the source (speaker/coach) from metadata when available.
3. If context is thin or off-topic, say "My knowledge base has limited info on this" and give your best grounded take.
4. Be direct, practical, specific. The user is a coach — no beginner explanations unless asked.
5. Use markdown: **bold** key terms, bullet lists for protocols, headers for sections.
6. If the question is about trading/stocks/crypto and context doesn't cover it, say "That's outside my fitness knowledge base" and suggest they check the trading dashboard.
"""

# ── GENERATE ──────────────────────────────────────────────────────────
def generate_answer(query: str, context: list[dict]) -> dict:
    context_parts = []
    for i, chunk in enumerate(context, 1):
        meta = chunk.get("metadata", {})
        source = (meta.get("speaker") or meta.get("source") or
                  meta.get("namespace") or meta.get("collection") or "unknown")
        context_parts.append(f"[{i}] Source: {source}\n{chunk['text']}")

    context_str = "\n\n───\n\n".join(context_parts)
    user_msg = (
        f"KNOWLEDGE BASE CONTEXT:\n\n{context_str}\n\n"
        f"═══\n\nQUESTION: {query}\n\nANSWER:"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    t0 = time.time()
    try:
        resp = ollama_client.chat(model=LLM_MODEL, messages=messages)
        answer = resp["message"]["content"].strip()
    except Exception as e:
        log.error(f"LLM generation failed: {e}")
        best = context[0] if context else None
        answer = (
            f"⚡ **LLM error** ({e}). Best matching passage from the knowledge base:\n\n"
            f"{best['text'][:1000] if best else 'No results found.'}"
        )

    latency_ms = round((time.time() - t0) * 1000)

    return {
        "answer":           answer,
        "sources": [
            {
                "source":  c.get("metadata", {}).get("speaker",
                               c.get("metadata", {}).get("source", "unknown")),
                "snippet": c["text"][:250] + ("…" if len(c["text"]) > 250 else ""),
                "score":   c.get("score", 0),
            }
            for c in context[:5]
        ],
        "chunks_retrieved": len(context),
        "latency_ms":       latency_ms,
        "model":            LLM_MODEL,
    }

# ── PUBLIC API ────────────────────────────────────────────────────────
def chat(message: str) -> dict:
    """Full RAG pipeline: embed → search → generate → return."""
    t0 = time.time()
    context = search(message)

    if not context:
        return {
            "answer": "Searched the knowledge base — no strong match found. "
                      "Try asking about a specific topic: training volume, nutrition "
                      "protocols, supplements, a specific coach's philosophy.",
            "sources":          [],
            "chunks_retrieved": 0,
            "latency_ms":       round((time.time() - t0) * 1000),
        }

    result = generate_answer(message, context)
    result["latency_ms"] = round((time.time() - t0) * 1000)
    return result


def health_check() -> dict:
    """Verify ChromaDB + Ollama are reachable."""
    out = {"chroma": False, "ollama_embed": False, "ollama_llm": False, "details": {}}
    try:
        coll = _get_collection()
        n = coll.count()
        out["chroma"] = True
        out["details"]["chroma_docs"] = n
        out["details"]["collection"] = coll.name
    except Exception as e:
        out["details"]["chroma_error"] = str(e)
    try:
        v = _embed("test")
        out["ollama_embed"] = True
        out["details"]["embed_dims"] = len(v)
    except Exception as e:
        out["details"]["embed_error"] = str(e)
    try:
        ollama_client.chat(model=LLM_MODEL, messages=[{"role":"user","content":"hi"}])
        out["ollama_llm"] = True
    except Exception as e:
        out["details"]["llm_error"] = str(e)
    return out
