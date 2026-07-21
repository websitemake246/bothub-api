#!/usr/bin/env python3
"""
Bot Runner API — Flask-based Telegram userbot process manager.
Runs on Render and Pterodactyl.
"""

import os
import sys
import json
import signal
import time
import shutil
import threading
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 5000))
BASE_DIR = Path(__file__).parent.resolve()
BOTS_DIR = BASE_DIR / "bots"
DATA_DIR = BASE_DIR / "data"
BOTS_JSON = DATA_DIR / "bots.json"
APP_PY_URL = (
    "https://raw.githubusercontent.com/"
    "FuriousGamer414/telegram-python-userbot/main/app.py"
)
BOT_REQUIREMENTS = [
    "telethon",
    "python-dotenv",
    "aiohttp",
    "yt-dlp",
    "hachoir",
    "python-dateutil",
    "Pillow",
    "gtts",
]

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# In-memory process tracking
#   running_bots[botId] = {
#       "process": subprocess.Popen,
#       "start_time": float (epoch),
#       "log_buffer": list[str] (last ~500 lines),
#   }
# ---------------------------------------------------------------------------
running_bots: dict[str, dict] = {}
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_bots() -> dict:
    """Load bot metadata from disk (synchronised via file, not lock)."""
    if not BOTS_JSON.exists():
        return {}
    try:
        raw = BOTS_JSON.read_text() or "{}"
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_bots(data: dict) -> None:
    """Atomically write bot metadata to disk."""
    tmp = BOTS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(BOTS_JSON)


def _get_bot_dir(bot_id: str) -> Path:
    return BOTS_DIR / bot_id


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _enrich_status(meta: dict) -> dict:
    """Add live status from running_bots to a bot metadata dict."""
    bot_id = meta.get("id", "")
    with _lock:
        entry = running_bots.get(bot_id)
    if entry and entry["process"].poll() is None:
        meta["status"] = "running"
        meta["uptime"] = round(time.time() - entry["start_time"], 1)
    else:
        meta.setdefault("status", "stopped")
        meta.pop("uptime", None)
    return meta


def _append_log(bot_id: str, line: str):
    """Append a line to the in-memory log buffer."""
    with _lock:
        entry = running_bots.get(bot_id)
        if entry:
            buf = entry["log_buffer"]
            buf.append(line)
            # keep at most 2000 lines in memory
            if len(buf) > 2000:
                entry["log_buffer"] = buf[-1500:]


def _cleanup_bot(bot_id: str):
    """Remove a bot from the running process table (no kill)."""
    with _lock:
        running_bots.pop(bot_id, None)


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------

def _start_bot_process(bot_id: str, bot_dir: Path) -> subprocess.Popen | None:
    """Start the userbot process in a venv and return the Popen handle."""
    venv_python = bot_dir / "venv" / "bin" / "python3"
    if not venv_python.exists():
        # fallback: try the system python if venv missing
        venv_python = Path("python3")

    log_path = bot_dir / "bot.log"
    log_fh = open(log_path, "ab", buffering=0)  # binary for universal newline

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # load .env into environment so the userbot picks it up
    dotenv_path = bot_dir / ".env"
    if dotenv_path.exists():
        for line in dotenv_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")

    proc = subprocess.Popen(
        [str(venv_python), "app.py"],
        cwd=str(bot_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=1,  # line-buffered
    )

    # Background reader thread — copies output to both the file and the
    # in-memory log buffer.
    def _reader(pid, out_pipe):
        try:
            for raw_line in iter(out_pipe.readline, b""):
                line = raw_line.decode("utf-8", errors="replace")
                # write to log file
                log_fh.write(raw_line)
                log_fh.flush()
                # write to memory
                _append_log(bot_id, line)
        except Exception:
            pass
        finally:
            try:
                log_fh.close()
            except Exception:
                pass

    t = threading.Thread(target=_reader, args=(bot_id, proc.stdout), daemon=True)
    t.start()

    return proc


def _send_to_stdin(bot_id: str, data: str):
    """Write data + newline to the bot's stdin."""
    with _lock:
        entry = running_bots.get(bot_id)
        if entry is None:
            return False
        proc = entry["process"]
    if proc.poll() is not None:
        return False
    try:
        proc.stdin.write((data + "\n").encode("utf-8"))
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def _stop_bot(bot_id: str) -> bool:
    """SIGTERM → 2 s grace → SIGKILL.  Returns True if process existed."""
    with _lock:
        entry = running_bots.get(bot_id)
        if entry is None:
            return False
        proc = entry["process"]
        # Remove from tracking *before* killing to prevent races
        running_bots.pop(bot_id, None)
    if proc.poll() is not None:
        return True  # already dead
    try:
        proc.terminate()  # SIGTERM
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGKILL
            proc.wait(timeout=5)
    except ProcessLookupError:
        pass
    return True


# ---------------------------------------------------------------------------
# Signal handler – graceful shutdown
# ---------------------------------------------------------------------------
def _shutdown(signum, frame):
    with _lock:
        bot_ids = list(running_bots.keys())
    for bid in bot_ids:
        _stop_bot(bid)
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

# -- POST /api/bots/deploy ------------------------------------------------

@app.route("/api/bots/deploy", methods=["POST"])
def deploy_bot():
    body = request.get_json(force=True)
    bot_id = body.get("botId", "").strip()
    if not bot_id:
        return jsonify({"success": False, "error": "botId is required"}), 400

    bot_dir = _get_bot_dir(bot_id)
    if bot_dir.exists():
        return jsonify({"success": False, "error": "Bot already exists"}), 409

    # 1. Create bot directory
    bot_dir.mkdir(parents=True, exist_ok=True)

    # 2. Download app.py
    try:
        req = urllib.request.Request(
            APP_PY_URL,
            headers={"User-Agent": "HermesBotRunner/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            app_py_content = resp.read()
        (bot_dir / "app.py").write_bytes(app_py_content)
    except Exception as exc:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 502

    # 3. Write .env
    env_lines = []
    for key in ("apiId", "apiHash", "phone", "sudoUser", "botToken"):
        val = body.get(key, "")
        env_lines.append(f'{key}="{val}"')
    (bot_dir / ".env").write_text("\n".join(env_lines) + "\n")

    # 4. Write requirements.txt
    (bot_dir / "requirements.txt").write_text(
        "\n".join(BOT_REQUIREMENTS) + "\n"
    )

    # 5. Create venv
    try:
        subprocess.run(
            ["python3", "-m", "venv", "venv"],
            cwd=str(bot_dir),
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 500

    # 6. pip install
    try:
        subprocess.run(
            [str(bot_dir / "venv" / "bin" / "pip"), "install"] + BOT_REQUIREMENTS,
            cwd=str(bot_dir),
            capture_output=True,
            timeout=600,
        )
    except Exception as exc:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 500

    # 7. Save metadata
    _ensure_data_dir()
    bots = _load_bots()
    bots[bot_id] = {
        "id": bot_id,
        "name": body.get("name", bot_id),
        "status": "deployed",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    _save_bots(bots)

    return jsonify({"success": True, "bot": {"id": bot_id, "status": "deployed"}}), 201


# -- POST /api/bots/<id>/start --------------------------------------------

@app.route("/api/bots/<bot_id>/start", methods=["POST"])
def start_bot(bot_id: str):
    bot_dir = _get_bot_dir(bot_id)
    if not bot_dir.exists():
        return jsonify({"success": False, "error": "Bot not found"}), 404

    with _lock:
        if bot_id in running_bots:
            return jsonify({"success": False, "error": "Bot already running"}), 409

    proc = _start_bot_process(bot_id, bot_dir)
    if proc is None:
        return jsonify({"success": False, "error": "Failed to start process"}), 500

    with _lock:
        running_bots[bot_id] = {
            "process": proc,
            "start_time": time.time(),
            "log_buffer": [],
        }

    # After 3 seconds, check if output asks for phone number and auto-send
    def _auto_phone():
        time.sleep(3)
        with _lock:
            entry = running_bots.get(bot_id)
        if entry is None:
            return
        # Check if we got a "phone" prompt
        buf = entry["log_buffer"]
        prompt_keywords = ("phone", "number", "enter your phone", "send code")
        recent = " ".join(buf[-20:]).lower()
        if any(kw in recent for kw in prompt_keywords):
            bot_meta = _load_bots().get(bot_id, {})
            phone = bot_meta.get("phone", "")
            dotenv_path = bot_dir / ".env"
            if not phone and dotenv_path.exists():
                for line in dotenv_path.read_text().splitlines():
                    if line.startswith("phone="):
                        phone = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if phone:
                _send_to_stdin(bot_id, phone)

    threading.Thread(target=_auto_phone, daemon=True).start()

    # Update status in JSON
    bots = _load_bots()
    if bot_id in bots:
        bots[bot_id]["status"] = "starting"
        _save_bots(bots)

    return jsonify({"success": True, "status": "starting"}), 200


# -- POST /api/bots/<id>/otp ----------------------------------------------

@app.route("/api/bots/<bot_id>/otp", methods=["POST"])
def send_otp(bot_id: str):
    body = request.get_json(force=True)
    otp = body.get("otp", "").strip()
    if not otp:
        return jsonify({"success": False, "error": "otp is required"}), 400

    ok = _send_to_stdin(bot_id, otp)
    if not ok:
        return jsonify({"success": False, "error": "Bot not running or stdin closed"}), 400

    # Update status
    bots = _load_bots()
    if bot_id in bots:
        bots[bot_id]["status"] = "running"
        _save_bots(bots)

    return jsonify({"success": True, "status": "running"}), 200


# -- POST /api/bots/<id>/stop ---------------------------------------------

@app.route("/api/bots/<bot_id>/stop", methods=["POST"])
def stop_bot(bot_id: str):
    existed = _stop_bot(bot_id)

    bots = _load_bots()
    if bot_id in bots:
        bots[bot_id]["status"] = "stopped"
        _save_bots(bots)

    if not existed:
        return jsonify({"success": True, "status": "stopped"}), 200
    return jsonify({"success": True, "status": "stopped"}), 200


# -- DELETE /api/bots/<id> -------------------------------------------------

@app.route("/api/bots/<bot_id>", methods=["DELETE"])
def delete_bot(bot_id: str):
    _stop_bot(bot_id)

    bot_dir = _get_bot_dir(bot_id)
    if bot_dir.exists():
        shutil.rmtree(bot_dir, ignore_errors=True)

    bots = _load_bots()
    bots.pop(bot_id, None)
    _save_bots(bots)

    return jsonify({"success": True}), 200


# -- GET /api/bots/<id>/logs -----------------------------------------------

@app.route("/api/bots/<bot_id>/logs", methods=["GET"])
def get_logs(bot_id: str):
    bot_dir = _get_bot_dir(bot_id)
    log_path = bot_dir / "bot.log"

    if not log_path.exists():
        # fall back to in-memory buffer
        with _lock:
            entry = running_bots.get(bot_id)
        if entry:
            lines = entry["log_buffer"]
            num_lines = request.args.get("lines", "200")
            try:
                n = int(num_lines)
            except ValueError:
                n = 200
            return jsonify({"logs": "".join(lines[-n:])}), 200
        return jsonify({"error": "No logs available"}), 404

    num_lines = request.args.get("lines", "200")
    try:
        n = int(num_lines)
    except ValueError:
        n = 200

    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()
    tail = "".join(all_lines[-n:])
    return jsonify({"logs": tail}), 200


# -- GET /api/bots ---------------------------------------------------------

@app.route("/api/bots", methods=["GET"])
def list_bots():
    bots = _load_bots()
    result = []
    for bot_id, meta in bots.items():
        enriched = _enrich_status(dict(meta))
        result.append(enriched)
    return jsonify(result), 200


# -- GET /api/stats --------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
def stats():
    bots = _load_bots()
    total = len(bots)
    running = 0
    stopped = 0
    for meta in bots.values():
        s = meta.get("status", "stopped")
        # Also check live
        with _lock:
            entry = running_bots.get(meta["id"])
        if entry and entry["process"].poll() is None:
            running += 1
        else:
            if s != "running":
                stopped += 1
            else:
                stopped += 1
    return jsonify({"totalBots": total, "runningBots": running, "stoppedBots": stopped}), 200


# -- GET / (health) --------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Bot Runner API",
        "version": "1.0",
        "status": "ok",
    }), 200


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _ensure_data_dir()
    if not BOTS_JSON.exists():
        _save_bots({})
    print(f"[bothub] Starting Bot Runner API on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
