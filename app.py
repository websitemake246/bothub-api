#!/usr/bin/env python3
"""Bot Runner API — manages Telegram userbot processes."""

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

from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 5000))
BASE_DIR = Path(__file__).parent.resolve()
BOTS_DIR = BASE_DIR / "bots"
DATA_DIR = BASE_DIR / "data"
BOTS_JSON = DATA_DIR / "bots.json"
APP_PY_URL = "https://raw.githubusercontent.com/FuriousGamer414/telegram-python-userbot/main/app.py"
BOT_REQUIREMENTS = ["telethon", "python-dotenv", "aiohttp", "yt-dlp", "hachoir", "python-dateutil", "Pillow", "gtts"]

app = Flask(__name__)
CORS(app)

running_bots = {}
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_bots():
    if not BOTS_JSON.exists():
        return {}
    try:
        data = json.loads(BOTS_JSON.read_text() or "{}")
        if isinstance(data, list):
            data = {b.get("id", str(i)): b for i, b in enumerate(data)}
        return data
    except:
        return {}

def _save_bots(data):
    tmp = BOTS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(BOTS_JSON)

def _get_bot_dir(bot_id):
    return BOTS_DIR / bot_id

def _enrich_status(meta):
    bot_id = meta.get("id", "")
    with _lock:
        entry = running_bots.get(bot_id)
    if entry:
        meta["status"] = "running"
        meta["uptime"] = int(time.time() - entry["start_time"])
    return meta

def download_file(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "BotRunner/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())

# ---------------------------------------------------------------------------
# Background install
# ---------------------------------------------------------------------------
def install_bot_async(bot_id, bot_dir):
    """Run venv + pip install in background thread."""
    try:
        subprocess.run(["python3", "-m", "venv", "venv"], cwd=str(bot_dir), capture_output=True, timeout=120)
        pip = str(bot_dir / "venv" / "bin" / "pip")
        subprocess.run([pip, "install", "-q"] + BOT_REQUIREMENTS, cwd=str(bot_dir), capture_output=True, timeout=600)
        bots = _load_bots()
        if bot_id in bots:
            bots[bot_id]["status"] = "ready"
            _save_bots(bots)
    except Exception as e:
        bots = _load_bots()
        if bot_id in bots:
            bots[bot_id]["status"] = "error"
            bots[bot_id]["error"] = str(e)
            _save_bots(bots)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({"service": "Bot Runner API", "status": "ok", "version": "1.0"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/api/bots/deploy", methods=["POST"])
def deploy_bot():
    body = request.get_json(force=True)
    bot_id = body.get("botId", "").strip()
    if not bot_id:
        return jsonify({"success": False, "error": "botId is required"}), 400

    bot_dir = _get_bot_dir(bot_id)
    if bot_dir.exists():
        return jsonify({"success": False, "error": "Bot already exists"}), 409

    # 1. Create dir + download app.py
    bot_dir.mkdir(parents=True, exist_ok=True)
    try:
        download_file(APP_PY_URL, bot_dir / "app.py")
    except Exception as e:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"success": False, "error": f"Download failed: {e}"}), 502

    # 2. Write .env
    env_lines = []
    for key in ("apiId", "apiHash", "phone", "sudoUser", "botToken"):
        val = body.get(key, "")
        env_lines.append(f'{key}="{val}"')
    (bot_dir / ".env").write_text("\n".join(env_lines) + "\n")

    # 3. Write requirements.txt
    (bot_dir / "requirements.txt").write_text("\n".join(BOT_REQUIREMENTS) + "\n")

    # 4. Create venv synchronously
    try:
        subprocess.run(["python3", "-m", "venv", "venv"], cwd=str(bot_dir), capture_output=True, timeout=120)
    except Exception as e:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(e)}), 500

    # 5. Save metadata with status "installing"
    _ensure_data_dir()
    bots = _load_bots()
    bots[bot_id] = {
        "id": bot_id,
        "name": body.get("name", bot_id),
        "status": "installing",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    _save_bots(bots)

    # 6. pip install in background thread
    t = threading.Thread(target=install_bot_async, args=(bot_id, bot_dir), daemon=True)
    t.start()

    return jsonify({"success": True, "bot": {"id": bot_id, "status": "installing"}}), 201


@app.route("/api/bots/<bot_id>/start", methods=["POST"])
def start_bot(bot_id):
    bot_dir = _get_bot_dir(bot_id)
    if not bot_dir.exists():
        return jsonify({"success": False, "error": "Bot not found"}), 404

    with _lock:
        if bot_id in running_bots:
            return jsonify({"success": True, "status": "already running"})

    # Check if install is complete
    venv_python = bot_dir / "venv" / "bin" / "python3"
    if not venv_python.exists():
        bots = _load_bots()
        status = bots.get(bot_id, {}).get("status", "unknown")
        if status == "installing":
            return jsonify({"success": False, "error": "Still installing dependencies. Please wait a moment and try again."}), 409
        return jsonify({"success": False, "error": "Virtual environment not found. Re-deploy."}), 500

    # Load env
    env_file = bot_dir / ".env"
    env = dict(os.environ)
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')

    app_py = bot_dir / "app.py"
    log_file = bot_dir / "bot.log"
    if log_file.exists():
        log_file.write_text("")  # Clear old logs

    # Start bot process
    proc = subprocess.Popen(
        [str(venv_python), str(app_py)],
        cwd=str(bot_dir),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Reader thread
    log_lines = []
    def read_output():
        for line in proc.stdout:
            log_lines.append(line)
            with open(log_file, "a") as f:
                f.write(line)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    with _lock:
        running_bots[bot_id] = {"process": proc, "start_time": time.time(), "log_lines": log_lines}

    # Auto-send phone after 3 seconds
    def auto_phone():
        time.sleep(3)
        with _lock:
            entry = running_bots.get(bot_id)
        if entry and entry["process"].poll() is None:
            try:
                entry["process"].stdin.write(env.get("phone", "") + "\n")
                entry["process"].stdin.flush()
            except:
                pass
    threading.Thread(target=auto_phone, daemon=True).start()

    # Track exit
    def on_exit():
        proc.wait()
        with _lock:
            running_bots.pop(bot_id, None)
        bots = _load_bots()
        if bot_id in bots:
            bots[bot_id]["status"] = "stopped"
            _save_bots(bots)
    threading.Thread(target=on_exit, daemon=True).start()

    bots = _load_bots()
    if bot_id in bots:
        bots[bot_id]["status"] = "starting"
        _save_bots(bots)

    return jsonify({"success": True, "status": "starting"}), 200


@app.route("/api/bots/<bot_id>/otp", methods=["POST"])
def send_otp(bot_id):
    body = request.get_json(force=True)
    otp = body.get("otp", "").strip()
    if not otp:
        return jsonify({"success": False, "error": "otp is required"}), 400

    with _lock:
        entry = running_bots.get(bot_id)
    if not entry:
        return jsonify({"success": False, "error": "Bot not running"}), 404

    try:
        entry["process"].stdin.write(otp + "\n")
        entry["process"].stdin.flush()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Update status after delay
    def update():
        time.sleep(5)
        bots = _load_bots()
        if bot_id in bots:
            bots[bot_id]["status"] = "running"
            _save_bots(bots)
    threading.Thread(target=update, daemon=True).start()

    return jsonify({"success": True, "status": "running"}), 200


@app.route("/api/bots/<bot_id>/stop", methods=["POST"])
def stop_bot(bot_id):
    with _lock:
        entry = running_bots.pop(bot_id, None)
    if entry:
        try:
            entry["process"].terminate()
            time.sleep(2)
            if entry["process"].poll() is None:
                entry["process"].kill()
        except:
            pass

    bots = _load_bots()
    if bot_id in bots:
        bots[bot_id]["status"] = "stopped"
        _save_bots(bots)

    return jsonify({"success": True, "status": "stopped"}), 200


@app.route("/api/bots/<bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    # Stop first
    with _lock:
        entry = running_bots.pop(bot_id, None)
    if entry:
        try:
            entry["process"].terminate()
            time.sleep(1)
            if entry["process"].poll() is None:
                entry["process"].kill()
        except:
            pass

    # Remove directory
    bot_dir = _get_bot_dir(bot_id)
    try:
        shutil.rmtree(bot_dir, ignore_errors=True)
    except:
        pass

    # Remove from DB
    bots = _load_bots()
    bots.pop(bot_id, None)
    _save_bots(bots)

    return jsonify({"success": True}), 200


@app.route("/api/bots/<bot_id>/logs", methods=["GET"])
def get_logs(bot_id):
    log_file = _get_bot_dir(bot_id) / "bot.log"
    logs = ""
    if log_file.exists():
        try:
            logs = log_file.read_text()
        except:
            pass
    lines = int(request.args.get("lines", 200))
    log_lines = logs.split("\n")[-lines:]
    return jsonify({"logs": "\n".join(log_lines)}), 200


@app.route("/api/bots", methods=["GET"])
def list_bots():
    _ensure_data_dir()
    bots = _load_bots()
    result = []
    for bot_id, meta in bots.items():
        meta = _enrich_status(meta)
        result.append(meta)
    return jsonify(result), 200


@app.route("/api/stats", methods=["GET"])
def stats():
    bots = _load_bots()
    running = 0
    for bot_id in bots:
        with _lock:
            if bot_id in running_bots:
                running += 1
    return jsonify({"totalBots": len(bots), "runningBots": running, "stoppedBots": len(bots) - running}), 200


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BOTS_DIR.mkdir(parents=True, exist_ok=True)


# Graceful shutdown
def shutdown_handler(sig, frame):
    with _lock:
        for bot_id, entry in list(running_bots.items()):
            try:
                entry["process"].terminate()
            except:
                pass
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# Init
_ensure_data_dir()
if not BOTS_JSON.exists():
    _save_bots({})

if __name__ == "__main__":
    print(f"[bothub] Bot Runner API on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
