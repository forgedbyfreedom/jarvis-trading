"""
Feeds for the Command Center's FBF Radio mini-player.

Playlists = FBF Radio (podcast episodes) + every playlist the FBF Workout Mix plugin publishes
(/wp-json/fbf-music/v1/playlists), so a new playlist added on the site shows up here on its own.
"""
from __future__ import annotations

import html
import threading
import time

import requests

WP = "https://forgedbyfreedom.net/wp-json"
RADIO_FEED = f"{WP}/fbf-radio/v1/episodes"
MUSIC_PLAYLISTS = f"{WP}/fbf-music/v1/playlists"
MUSIC_TRACKS = f"{WP}/fbf-music/v1/tracks"
HEAD = {"User-Agent": "FBF-Jarvis/4", "Accept": "application/json"}
TTL = 600

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _get(url: str, params: dict | None = None):
    key = url + repr(sorted((params or {}).items()))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    r = requests.get(url, params=params, headers=HEAD, timeout=20)
    r.raise_for_status()
    val = r.json()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _track(t: dict) -> dict | None:
    audio = t.get("audio") or t.get("audio_url") or t.get("url")
    if not audio:
        return None
    return {
        "id": t.get("id"),
        "title": html.unescape(t.get("title") or "Untitled"),
        "artist": html.unescape(t.get("artist") or t.get("show") or "Forged by Freedom"),
        "audio": audio,
        "cover": t.get("cover") or t.get("image") or "",
        "duration": t.get("duration") or 0,
    }


def playlists() -> list[dict]:
    out = []
    try:
        d = _get(RADIO_FEED)
        eps = d.get("episodes", d) if isinstance(d, dict) else d
        out.append({"id": "radio", "title": "FBF Radio", "count": len(eps), "kind": "episodes"})
    except Exception:
        out.append({"id": "radio", "title": "FBF Radio", "count": None, "kind": "episodes"})
    try:
        for p in _get(MUSIC_PLAYLISTS):
            out.append({"id": p["id"], "title": html.unescape(p.get("title") or p["id"]),
                        "count": p.get("count"), "kind": "music"})
    except Exception:
        out += [{"id": "workout", "title": "FBF Workout Mix", "count": None, "kind": "music"},
                {"id": "gamecock", "title": "Gamecock Country", "count": None, "kind": "music"}]
    return out


def tracks(playlist: str) -> dict:
    playlist = (playlist or "radio").strip().lower()
    if not playlist.replace("-", "").replace("_", "").isalnum():
        raise ValueError("bad playlist id")
    if playlist == "radio":
        d = _get(RADIO_FEED)
        raw = d.get("episodes", d) if isinstance(d, dict) else d
        title = "FBF Radio"
    else:
        d = _get(MUSIC_TRACKS, {"playlist": playlist})
        raw = d.get("tracks", [])
        title = (d.get("playlist") or {}).get("title") or playlist
    items = [x for x in (_track(t) for t in raw) if x]
    return {"id": playlist, "title": html.unescape(title), "tracks": items}
