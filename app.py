# app.py
import os
import time
import uuid
import json
import logging
import base64
from pathlib import Path
from datetime import datetime
from typing import Tuple, Optional, Dict, Any, List
from flask import Flask, request, send_file, jsonify, make_response
from flask_cors import CORS
import requests

# optional replicate import
try:
    import replicate
except Exception:
    replicate = None

# ---------- Basic config ----------
ROOT = Path(__file__).parent.resolve()
VIDEO_DIR = ROOT / "generated_videos"
VIDEO_DIR.mkdir(exist_ok=True)
SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
SAMPLE_ASSET = ROOT / "sample_assets" / "sample.mp4"
CACHE_FILE = ROOT / "cache.json"

# env (Replicate)
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")
# Note: replicate.run takes the model slug; some model pages use "owner/model" or a version string
REPLICATE_MODEL = os.environ.get("REPLICATE_MODEL", "minimax/video-01")

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a professional prompt engineer and an assistant that creates concise, production-ready video generation briefs. "
    "When asked, include: camera angle, mood, color palette, movement, focal elements, and duration constraints (5-10s). Keep briefs short (<200 words)."
)

# Fast defaults (tuned for lower latency)
FAST_DEFAULTS: Dict[str, Any] = {
    "duration": 5,      # seconds (user requested 5s clips)
    "fps": 12,
    "width": 512,
    "height": 288,
    "steps": 20,
    "samples": 1,
    "guidance_scale": 6,
}

# logging
logging.basicConfig(
    filename=str(LOG_DIR / "server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
CORS(app)

# ---------- Simple persistent cache helpers ----------
def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.exception("Failed to load cache.json; starting fresh.")
            return {}
    return {}

def _save_cache(cache: dict):
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Failed to save cache.json")

def _normalize_prompt(p: str) -> str:
    return " ".join(p.strip().lower().split())

cache = _load_cache()

# ---------- Session helpers ----------
def create_session() -> str:
    sid = uuid.uuid4().hex
    path = SESSIONS_DIR / f"{sid}.json"
    data = {
        "id": sid,
        "created_at": datetime.utcnow().isoformat(),
        "messages": []
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return sid

def load_session(sid: str):
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def create_session_with_id(sid: str) -> str:
    path = SESSIONS_DIR / f"{sid}.json"
    if path.exists():
        return sid
    data = {"id": sid, "created_at": datetime.utcnow().isoformat(), "messages": []}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return sid

def append_session_message(sid: str, role: str, text: str, meta: Optional[dict]=None):
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        logging.warning("append_session_message: session not found %s. Creating new.", sid)
        _ = create_session_with_id(sid)
    data = load_session(sid)
    data["messages"].append({
        "role": role,
        "text": text,
        "meta": meta or {},
        "ts": datetime.utcnow().isoformat()
    })
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

# ---------- Brief generation ----------
def create_production_brief(system_prompt: str, history: list, user_prompt: str) -> Tuple[str, str]:
    history_snippet = ""
    if history:
        last = history[-6:]
        history_snippet = " | ".join([f"{m['role']}: {m['text']}" for m in last])

    brief_parts = []
    brief_parts.append(user_prompt.strip())
    if history_snippet:
        brief_parts.append(f"Context: {history_snippet}")
    brief = " — ".join(brief_parts)
    if len(brief) > 600:
        brief = brief[:600] + "..."

    assistant_reply = "Generating a short (5s) preview video based on your prompt. I'll return the video when ready."
    return brief, assistant_reply

# ---------- helpers to download and handle replicate outputs ----------
def _guess_ext_from_url(url: str) -> str:
    if url.endswith(".mp4") or ".mp4" in url:
        return ".mp4"
    if url.endswith(".webm") or ".webm" in url:
        return ".webm"
    if url.endswith(".gif") or ".gif" in url:
        return ".gif"
    return ".mp4"

def _download_to_file(url: str) -> str:
    out_path = VIDEO_DIR / f"{uuid.uuid4().hex}{_guess_ext_from_url(url)}"
    logging.info("Downloading generated video %s -> %s", url, out_path)
    r = requests.get(url, stream=True, timeout=180)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return str(out_path)

def _write_bytes_to_file(data: bytes, ext: str = ".mp4") -> str:
    out_path = VIDEO_DIR / f"{uuid.uuid4().hex}{ext}"
    Path(out_path).write_bytes(data)
    return str(out_path)

def _process_replicate_item(item) -> List[str]:
    """
    Try multiple strategies to extract/download a video from a replicate output item.
    Returns list of local file paths (0 or more).
    """
    out_paths: List[str] = []

    # 1) string URL
    try:
        if isinstance(item, str) and item.startswith("http"):
            out_paths.append(_download_to_file(item))
            return out_paths
    except Exception:
        logging.exception("Error processing string item")

    # 2) callable .url()
    try:
        url_callable = getattr(item, "url", None)
        if callable(url_callable):
            try:
                url_val = url_callable()
                if isinstance(url_val, str) and url_val.startswith("http"):
                    out_paths.append(_download_to_file(url_val))
                    return out_paths
                # Sometimes .url() returns a FileOutput-like object with .url property:
                if hasattr(url_val, "url") and isinstance(url_val.url, str) and url_val.url.startswith("http"):
                    out_paths.append(_download_to_file(url_val.url))
                    return out_paths
            except TypeError:
                # some .url attributes are property-like; handled below
                pass
            except Exception:
                logging.exception("Calling item.url() failed")
    except Exception:
        logging.exception("Checking item.url failed")

    # 3) property .url (non-callable)
    try:
        url_prop = getattr(item, "url", None)
        if isinstance(url_prop, str) and url_prop.startswith("http"):
            out_paths.append(_download_to_file(url_prop))
            return out_paths
    except Exception:
        logging.exception("item.url property check failed")

    # 4) .read() -> bytes
    try:
        read_fn = getattr(item, "read", None)
        if callable(read_fn):
            data = read_fn()
            if isinstance(data, (bytes, bytearray)):
                out_paths.append(_write_bytes_to_file(bytes(data), ".mp4"))
                return out_paths
    except Exception:
        logging.exception("Calling item.read() failed")

    # 5) .open() -> file-like
    try:
        open_fn = getattr(item, "open", None)
        if callable(open_fn):
            fobj = open_fn()
            try:
                data = fobj.read()
                if isinstance(data, (bytes, bytearray)):
                    out_paths.append(_write_bytes_to_file(bytes(data), ".mp4"))
                    return out_paths
            finally:
                try:
                    fobj.close()
                except Exception:
                    pass
    except Exception:
        logging.exception("item.open() handling failed")

    # 6) .stream() -> iterable chunks
    try:
        stream_fn = getattr(item, "stream", None)
        if callable(stream_fn):
            stream = stream_fn()
            out_path = VIDEO_DIR / f"{uuid.uuid4().hex}.mp4"
            with open(out_path, "wb") as f:
                for chunk in stream:
                    if isinstance(chunk, (bytes, bytearray)):
                        f.write(chunk)
            out_paths.append(str(out_path))
            return out_paths
    except Exception:
        logging.exception("item.stream() handling failed")

    # 7) download/save methods
    try:
        download_fn = getattr(item, "download", None) or getattr(item, "save", None)
        if callable(download_fn):
            out_path = VIDEO_DIR / f"{uuid.uuid4().hex}.mp4"
            try:
                res = download_fn(str(out_path))
                # if download_fn returns a path or writes file, check
                if isinstance(res, str) and Path(res).exists():
                    out_paths.append(res)
                    return out_paths
                if Path(out_path).exists():
                    out_paths.append(str(out_path))
                    return out_paths
            except Exception:
                logging.exception("item.download/save() failed")
    except Exception:
        logging.exception("item.download/save check failed")

    # 8) dict-like: try common keys
    try:
        if isinstance(item, dict):
            for key in ("url", "output_url", "download_url", "file", "artifact", "data"):
                v = item.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    out_paths.append(_download_to_file(v))
                    return out_paths
                elif isinstance(v, (bytes, bytearray)):
                    out_paths.append(_write_bytes_to_file(bytes(v), ".mp4"))
                    return out_paths
    except Exception:
        logging.exception("dict-like item handling failed")

    # 9) last resort debug logging
    try:
        logging.info("Unrecognized replicate output item type: %s", type(item))
        logging.info("repr(item)[:500]: %s", repr(item)[:500])
        logging.info("dir(item) (partial): %s", ", ".join(dir(item)[:200]))
    except Exception:
        pass

    return out_paths

def call_replicate_minimax(prompt: str, options: Optional[dict] = None, timeout: int = 600) -> List[str]:
    """
    Call Replicate model minimax/video-01 using replicate.run (python client).
    Uses FAST_DEFAULTS and simple caching to speed up repeated prompts.
    Returns list of local file paths of downloaded videos.
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN not configured")

    if replicate is None:
        raise RuntimeError("replicate package not installed. pip install replicate")

    # cache check
    norm = _normalize_prompt(prompt)
    cached = cache.get(norm)
    if cached and Path(cached).exists():
        logging.info("Cache hit for prompt: %s -> %s", norm, cached)
        return [cached]

    # merge options with fast defaults (user overrides allowed)
    merged_opts = {**FAST_DEFAULTS, **(options or {})}

    input_payload: Dict[str, Any] = {"prompt": prompt}
    input_payload.update(merged_opts)

    logging.info("Calling Replicate model %s prompt=%s options=%s", REPLICATE_MODEL, prompt[:120], {k: merged_opts.get(k) for k in ("duration","fps","width","height","steps","samples") if k in merged_opts})

    start = time.time()
    try:
        # replicate.run uses token from environment; ensure REPLICATE_API_TOKEN is set
        output = replicate.run(REPLICATE_MODEL, input=input_payload)
    except Exception as e:
        logging.exception("Replicate run failed: %s", e)
        raise
    elapsed = time.time() - start
    logging.info("Replicate run finished in %.2f seconds", elapsed)

    downloaded: List[str] = []

    # output can be many shapes
    if isinstance(output, list):
        for item in output:
            try:
                downloaded.extend(_process_replicate_item(item))
            except Exception:
                logging.exception("Failed to process item in output list")
    else:
        try:
            downloaded.extend(_process_replicate_item(output))
        except Exception:
            logging.exception("Failed to process top-level output")

    # if still nothing and output is dict, scan nested values
    if not downloaded and isinstance(output, dict):
        for v in output.values():
            try:
                if isinstance(v, str) and v.startswith("http"):
                    downloaded.append(_download_to_file(v))
                else:
                    downloaded.extend(_process_replicate_item(v))
            except Exception:
                logging.exception("Failed to process dict value in replicate output")

    if not downloaded:
        logging.error("No downloadable video returned by replicate. output repr: %s", repr(output)[:1000])
        raise RuntimeError(f"No downloadable video returned by replicate: {repr(output)[:500]}")

    # cache first downloaded file
    try:
        cache[norm] = downloaded[0]
        _save_cache(cache)
        logging.info("Saved generation to cache for prompt: %s -> %s", norm, downloaded[0])
    except Exception:
        logging.exception("Failed to save to cache")

    return downloaded

# ---------- Routes ----------
@app.route("/api/session", methods=["POST"])
def create_session_route():
    sid = create_session()
    return jsonify({"session_id": sid})

@app.route("/api/generate-video", methods=["POST"])
def generate_video():
    body = request.get_json(force=True)
    if not body or "prompt" not in body:
        return jsonify({"error": "please provide 'prompt' in json body"}), 400

    user_prompt = body["prompt"].strip()
    sid = body.get("session_id")
    if not sid:
        sid = create_session()
    else:
        create_session_with_id(sid)

    append_session_message(sid, "user", user_prompt, meta={"source": "frontend"})

    session_data = load_session(sid) or {}
    history = session_data.get("messages", [])
    brief, assistant_reply = create_production_brief(SYSTEM_PROMPT, history, user_prompt)
    append_session_message(sid, "assistant", assistant_reply, meta={"brief": brief})

    user_options = body.get("options", {}) or {}

    # If replicate not configured -> return mock sample
    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL or replicate is None:
        logging.warning("Replicate not configured or client missing. Returning mock sample for session %s", sid)
        if not SAMPLE_ASSET.exists():
            return jsonify({"error": "Replicate not configured and no sample available."}), 500
        out_path = VIDEO_DIR / f"{uuid.uuid4().hex}.mp4"
        out_path.write_bytes(SAMPLE_ASSET.read_bytes())
        append_session_message(sid, "assistant", f"[MOCK VIDEO SERVED] brief={brief}", meta={"video": str(out_path.name), "mock": True})
        resp = make_response(send_file(str(out_path), mimetype="video/mp4", as_attachment=False))
        resp.headers["X-Session-Id"] = sid
        resp.headers["X-Video-Mock"] = "true"
        return resp

    # Call Replicate (fast defaults merged with user options)
    gen_start = time.time()
    try:
        files = call_replicate_minimax(brief, options=user_options)
    except Exception as e:
        logging.exception("Replicate call failed for session %s: %s", sid, e)
        if SAMPLE_ASSET.exists():
            out_path = VIDEO_DIR / f"{uuid.uuid4().hex}.mp4"
            out_path.write_bytes(SAMPLE_ASSET.read_bytes())
            append_session_message(sid, "assistant", f"[MOCK VIDEO SERVED AFTER REPLICATE ERROR] brief={brief}", meta={"video": str(out_path.name), "mock": True, "replicate_error": str(e)})
            resp = make_response(send_file(str(out_path), mimetype="video/mp4", as_attachment=False))
            resp.headers["X-Session-Id"] = sid
            resp.headers["X-Video-Mock"] = "true"
            return resp
        else:
            return jsonify({"error": f"Replicate failed and no mock sample available: {e}"}), 500

    gen_elapsed = time.time() - gen_start

    if not files:
        return jsonify({"error": "Replicate returned no downloadable files"}), 500

    out_file = files[0]
    append_session_message(sid, "assistant", f"[VIDEO GENERATED] brief={brief}", meta={"video": str(Path(out_file).name), "mock": False, "elapsed": gen_elapsed})
    resp = make_response(send_file(str(out_file), mimetype="video/mp4", as_attachment=False))
    resp.headers["X-Session-Id"] = sid
    resp.headers["X-Video-Mock"] = "false"
    resp.headers["X-Generation-Time"] = f"{gen_elapsed:.2f}"
    return resp

@app.route("/api/session-history/<session_id>", methods=["GET"])
def session_history(session_id):
    s = load_session(session_id)
    if not s:
        return jsonify({"error": "session not found"}), 404
    return jsonify(s)

@app.route("/api/list-videos", methods=["GET"])
def list_videos():
    files = [f.name for f in VIDEO_DIR.iterdir() if f.suffix in (".mp4", ".webm", ".gif")]
    return jsonify({"videos": files})

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/", methods=["GET"])
def hi():
    return jsonify({"HELLO": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
