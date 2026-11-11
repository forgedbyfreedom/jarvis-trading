"""Local speech-to-text for Athena: faster-whisper on the RTX 3090 (falls back to CPU)."""
import glob, os, site, tempfile, threading, time, logging

log = logging.getLogger("jarvis.stt")
_model = None
_lock = threading.Lock()
PROMPT = ("Athena, Bryan, FBF, Forged by Freedom, BPC-157, TB-500, GHK-Cu, retatrutide, tirzepatide, semaglutide, "
          "cagrilintide, tesamorelin, ipamorelin, MOTS-c, NAD+, testosterone, Anavar, peptides, macros, hypertrophy.")


def _add_cuda_dlls():
    # pip's nvidia-cublas / nvidia-cudnn wheels ship DLLs that Windows won't find on its own
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            except Exception:
                pass


def model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _add_cuda_dlls()
            try:
                _model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
                log.info("Whisper large-v3-turbo on CUDA")
            except Exception as e:
                log.warning(f"CUDA Whisper failed ({e}); using small.en on CPU")
                _model = WhisperModel("small.en", device="cpu", compute_type="int8")
        return _model


def transcribe(audio: bytes, suffix=".webm") -> dict:
    t0 = time.time()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio)
        path = f.name
    try:
        segs, info = model().transcribe(path, language="en", beam_size=1, vad_filter=True,
                                        initial_prompt=PROMPT, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"text": text, "secs": round(time.time() - t0, 2)}


def warm():
    threading.Thread(target=model, daemon=True).start()
