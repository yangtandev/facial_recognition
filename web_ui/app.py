from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
import json
import subprocess
import signal
from ota.core import save_config

app = Flask(__name__)
app.secret_key = 'facial_recognition_secret_key_2026'

# Path to config.json (Relative to this file: ../config.json)
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(
    os.path.dirname(__file__)), "config.json"))


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config_file(data):
    save_config(CONFIG_PATH, data)

# [2026-02-01 Fix] Disable caching for all responses to ensure UI updates immediately


@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/')
def index():
    if not session.get('logged_in'):
        return render_template('login.html')
    return render_template('index.html')


@app.route('/login', methods=['POST'])
def login():
    password = request.form.get('password')
    # Hardcoded password matching local UI
    if password == 'admin':
        session['logged_in'] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "密碼錯誤"})


@app.route('/api/config', methods=['GET'])
def get_config():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def update_config():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        new_config = request.json

        save_config_file(new_config)

        return jsonify({"success": True})
    except BlockingIOError:
        return jsonify({"success": False, "message": "OTA 驗收期間暫停設定寫入，請稍後再試。"}), 409
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/log', methods=['GET'])
def get_logs():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    log_type = request.args.get('type', 'app')  # app or system
    lines = request.args.get('lines', 100)

    try:
        if log_type == 'app':
            log_path = os.path.join(os.path.dirname(
                os.path.dirname(__file__)), "log", "faceLog")
            if not os.path.exists(log_path):
                return jsonify({"content": "Log file not found."})

            try:
                # Use system tail for efficiency
                res = subprocess.run(
                    ["tail", "-n", str(lines), log_path], capture_output=True, text=True)
                return jsonify({"content": res.stdout})
            except:
                # Fallback python read
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return jsonify({"content": f.read()[-10000:]})

        elif log_type == 'system':
            # journalctl
            cmd = ["journalctl", "--user", "-u",
                   "facial_recognition.service", "-n", str(lines), "--no-pager"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                msg = f"讀取失敗 (Code {res.returncode})。\n請確認權限。\n提示: 執行 `sudo usermod -aG systemd-journal $USER` 並重登。"
                return jsonify({"content": msg})
            return jsonify({"content": res.stdout})

    except Exception as e:
        return jsonify({"content": f"Error reading logs: {e}"})


@app.route('/api/restart', methods=['POST'])
def restart_system():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # Soft Reload via SIGHUP
        # Target: main.py
        subprocess.run(["pkill", "-HUP", "-f", "main.py"])
        return jsonify({"success": True, "message": "重啟訊號已發送"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/hard_restart', methods=['POST'])
def hard_restart_system():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # Hard Restart via Systemd
        # This kills the web server itself.
        subprocess.Popen(["systemctl", "--user", "restart",
                         "facial_recognition.service"])
        return jsonify({"success": True, "message": "系統正在強制重啟，網頁將會斷線..."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


def run_web_server(port=5000):
    # Disable Flask banner to keep stdout clean
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
