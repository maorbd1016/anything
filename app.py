"""
app.py
-------------------------------------------------------------------------
The web version of the scanner. Runs the exact same scan_engine.run_scan()
that scanner.py does -- served live instead of printed to a console.
Also exposes a way to build/refresh reference_cache.json by visiting a
link -- no terminal needed anywhere in this setup.

SECURITY NOTE: your keys are read from environment variables on whatever
machine runs this file. They never go to the browser -- the frontend
only ever talks to this server over /api/..., and this server is the
only thing that talks to Alpaca/Finnhub. That's the whole reason this
needs a real server instead of being a published page: a published page
would have to hold your keys in its own code, visible to anyone who
opens dev tools.

ADMIN_TOKEN protects /api/rebuild-cache so a stranger who finds your
URL can't trigger it -- pick any random string as its value when you
set your environment variables, and treat the resulting link like a
password (don't share it).

Run locally:
    uvicorn app:app --reload
    open http://127.0.0.1:8000

Deploy: see README.md, "Deploying the web version".
-------------------------------------------------------------------------
"""

import os
import time
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from scan_engine import run_scan, build_reference

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

# A live scan hits Alpaca several times (snapshot + news + bars). This
# cache keeps a page left open, or a couple of tabs, from re-running
# the whole scan on every single request.
CACHE_SECONDS = 20
_cache = {"data": None, "at": 0.0}

# The cache build takes several minutes (Finnhub is rate-limited), which
# is too long for a normal web request to wait on -- so it runs in a
# background thread, and the browser polls /api/rebuild-status instead
# of waiting on one long request that would likely time out.
_rebuild_status = {"state": "idle", "message": "", "done": 0, "total": 0}
_rebuild_lock = threading.Lock()

app = FastAPI()


def _run_rebuild():
    def progress_cb(stage, done, total):
        _rebuild_status.update({"state": "running", "message": stage, "done": done, "total": total})
    try:
        summary = build_reference(ALPACA_KEY, ALPACA_SECRET, FINNHUB_KEY, progress_cb=progress_cb)
        _rebuild_status.update({
            "state": "done",
            "message": f"Done -- {summary['cached']} symbols cached (of {summary['liquid_candidates']} checked).",
            "done": summary["cached"], "total": summary["cached"],
        })
    except Exception as e:
        _rebuild_status.update({"state": "error", "message": str(e)})


@app.get("/api/rebuild-cache")
def api_rebuild_cache(token: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return JSONResponse({"error": "Missing or incorrect token."}, status_code=403)
    with _rebuild_lock:
        if _rebuild_status["state"] == "running":
            return JSONResponse({"message": "Already running.", **_rebuild_status})
        _rebuild_status.update({"state": "running", "message": "Starting...", "done": 0, "total": 0})
        threading.Thread(target=_run_rebuild, daemon=True).start()
    return JSONResponse({
        "message": "Started in the background. This takes several minutes -- "
                   "refresh this page (or visit /api/rebuild-status) to check progress.",
        **_rebuild_status,
    })


@app.get("/api/rebuild-status")
def api_rebuild_status():
    return JSONResponse(_rebuild_status)


@app.get("/api/scan")
def api_scan():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["at"]) < CACHE_SECONDS:
        return JSONResponse(_cache["data"])

    if not ALPACA_KEY or not ALPACA_SECRET:
        return JSONResponse(
            {"error": "Server is missing ALPACA_API_KEY / ALPACA_SECRET_KEY environment variables."},
            status_code=500,
        )
    try:
        data = run_scan(ALPACA_KEY, ALPACA_SECRET)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "reference_cache.json not found yet. Visit /api/rebuild-cache?token=YOUR_TOKEN first "
                      "(see README.md) -- it takes a few minutes the first time."},
            status_code=500,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    _cache["data"] = data
    _cache["at"] = now
    return JSONResponse(data)


# The page can live either inside a "static" folder (how the project is
# laid out on disk) or sit next to this file at the top level (what you
# get when you drag individual files into GitHub's web uploader, which
# doesn't keep folders). Either layout works -- no rearranging needed.
_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_HERE, "static")
_INDEX_CANDIDATES = [
    os.path.join(_STATIC_DIR, "index.html"),
    os.path.join(_HERE, "index.html"),
]


@app.get("/")
def index():
    for path in _INDEX_CANDIDATES:
        if os.path.exists(path):
            return FileResponse(path)
    return JSONResponse(
        {"error": "index.html was not found next to app.py (or in a 'static' folder). "
                  "Upload index.html to the same place as app.py."},
        status_code=500,
    )


# Only mount /static if that folder actually exists -- mounting a missing
# folder crashes the whole app at startup.
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
