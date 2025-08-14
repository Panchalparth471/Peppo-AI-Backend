# Peppo AI — Backend (Flask + Replicate)

**Repository:** [https://github.com/Panchalparth471/Peppo-AI-Backend](https://github.com/Panchalparth471/Peppo-AI-Backend)

This is the backend for **Peppo AI** — a Flask service that accepts text prompts and returns generated short (default 5s) videos. The server integrates with the Replicate API (via the `replicate` Python client) and includes a local sample fallback so the frontend remains functional without an API key.

This README explains how to run the backend locally, configure environment variables, the API contract the frontend expects, and how to deploy the backend (Render instructions included). This file intentionally omits Docker instructions.

---

## Features

* `POST /api/generate-video` — generates a short video from a prompt (calls Replicate)
* `POST /api/session` — creates a lightweight session id
* Session history saved to disk under `sessions/` (for simple context/audit)
* Simple prompt→file cache (`cache.json`) to avoid re-generating identical prompts
* Robust handling of many Replicate output shapes (URL strings, file-like, base64, dicts)
* Local `sample_assets/sample.mp4` fallback when Replicate is not configured or fails
* `X-Generation-Time` header included in responses for telemetry

---

## Repo layout (expected)

```
/ (repo root)
├─ app.py                 # main Flask app entrypoint
├─ requirements.txt       # Python dependencies
├─ sample_assets/         # sample.mp4 for mock responses
├─ generated_videos/      # runtime output directory for generated files
├─ sessions/              # session JSON files
├─ logs/                  # server logs
└─ README.md
```

---

## Prerequisites

* Python 3.9+ (3.11 recommended)
* pip
* A Replicate API token for real generation (optional for testing)

---

## Environment variables

Create a `.env` file or set these environment variables in your host (do NOT commit secrets):

```env
REPLICATE_API_TOKEN=your_replicate_token_here
REPLICATE_MODEL=minimax/video-01     # optional, default in code
PORT=8000                            # optional
```

* `REPLICATE_API_TOKEN` is required for real model runs. If not set, the server will return `sample_assets/sample.mp4` for `/api/generate-video` requests.

---

## Install & run locally

1. Create & activate a Python virtual environment:

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Set environment variables (example):

```bash
# macOS / Linux
export REPLICATE_API_TOKEN="r8_xxx..."
export REPLICATE_MODEL="minimax/video-01"
export PORT=8000

# Windows (PowerShell)
$env:REPLICATE_API_TOKEN = "r8_xxx..."
$env:REPLICATE_MODEL = "minimax/video-01"
$env:PORT = "8000"
```

4. Run the server (development):

```bash
python app.py
```

5. Production-like run (Gunicorn):

```bash
pip install gunicorn
gunicorn app:app -b 0.0.0.0:8000 --workers 4 --timeout 600
```

6. Health check:

```bash
curl http://localhost:8000/api/health
# -> {"ok": true}
```

---

## API endpoints

### `POST /api/session`

Create a lightweight session. Response:

```json
{ "session_id": "<uuid>" }
```

### `POST /api/generate-video`

* Request body JSON:

```json
{ "prompt": "A calm forest with falling leaves, 5s", "session_id": "optional", "options": { ... } }
```

* Response: Binary `video/mp4` (Content-Type: `video/mp4`)
* Useful response headers:

  * `X-Session-Id` — session id
  * `X-Video-Mock` — `"true"` if returning local sample
  * `X-Generation-Time` — seconds the generation took

**Notes:** The backend merges user `options` with fast defaults: 5s duration, 12 fps, 512×288 resolution, 20 steps, 1 sample. You can pass `options` to override these.

### `GET /api/session-history/<session_id>`

Returns the saved session messages JSON.

### `GET /api/list-videos`

Returns list of generated video filenames in `generated_videos/`.

### `GET /api/health`

Returns `{ "ok": true }`.

---

## Caching and files

* Prompt normalization is applied and the first generated file per normalized prompt is saved in `cache.json` (prompt -> filepath). Subsequent identical prompts return the cached file to save cost/time.
* Generated videos are saved under `generated_videos/` with UUID filenames.
* Session history saved under `sessions/<sid>.json`.

---

## Logs

Server logs are written to `logs/server.log`. Check this file for errors when generation fails or to debug unexpected Replicate output shapes.

---

## Sample fallback

If `REPLICATE_API_TOKEN` is missing or the Replicate client is not installed, the server will return `sample_assets/sample.mp4` for `/api/generate-video`. This keeps the frontend demoable for graders and in CI.

---

## Deploy to Render (simple Python service)

1. In Render, create a new **Web Service** pointing to this repository and branch.
2. **Build Command**: (install Python deps)

```
pip install -r requirements.txt
```

3. **Start Command**:

```
gunicorn app:app -b 0.0.0.0:$PORT --workers 4 --timeout 600
```

4. Add environment variables in Render dashboard:

* `REPLICATE_API_TOKEN` (secret)
* `REPLICATE_MODEL` (optional)

Render will run the build command, then use the start command. The app will be available at the provided service URL.

---

## Troubleshooting

* **`replicate` package missing / ImportError**: install via `pip install replicate` and ensure `REPLICATE_API_TOKEN` is set.
* **No downloadable file from Replicate output**: the server logs the raw `repr(output)` and attempts many extraction strategies. Check `logs/server.log` for the debug info and paste logs if you need help.
* **Timeouts / long-running requests**: use `gunicorn --timeout` increased (example uses 600s). For production-scale, consider converting to background job flow (out of scope for this README).

---

## requirements.txt (example)

```
flask
flask-cors
requests
replicate
python-dotenv
gunicorn
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Security & notes

* **Do not** expose `REPLICATE_API_TOKEN` in the frontend or commit it to source control.
* This backend stores generated artifacts and sessions on the local filesystem — fine for demos but replace with S3 or similar when scaling.

---

If you want a matching frontend example or a small script to test the API (curl examples), I can add those next.
