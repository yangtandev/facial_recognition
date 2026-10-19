#!/bin/bash
set -e

# 初次安裝與既有主機共用 OTA/SMTP 設定入口；設定階段不改辨識 config。
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$PROJECT_DIR"
if [ "${1:-}" != "" ] && [ "${1:-}" != "--ota-config-only" ]; then
    echo "用法: bash install.sh [--ota-config-only]"
    exit 1
fi
if [ "${1:-}" != "--ota-config-only" ] && { [ -e config.json ] || [ -L config.json ]; }; then
    echo "已有 config.json；保留現場設定，請使用 bash install.sh --ota-config-only。"
    exit 1
fi
echo "▶ 設定主機角色與 OTA／SMTP（開發機可略過 SMTP）..."
python3 -m ota.configure --project "$PROJECT_DIR"
if [ "${1:-}" = "--ota-config-only" ]; then
    echo "▶ 設定完成；已安裝 agent 者請依 README 重啟服務；首次接入請繼續 baseline 驗收。"
    exit 0
fi

# --- 1. Sudo 提權與 Keep-alive ---
echo "▶ 為了安裝系統套件與設定服務，需要取得 sudo 權限。"
sudo -v
# Keep-alive in background
while true; do sudo -n true; sleep 60; kill -0 "$$" || exit; done 2>/dev/null &

# --- 2. 變數與路徑判定 ---
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CURRENT_USER="$USER"

cd "$PROJECT_DIR"

echo ""
echo "============================================="
echo "   Face System 系統環境全自動安裝精靈"
echo "============================================="
echo ""

# --- 3. 收集使用者輸入參數 ---
read -p "請輸入伺服器 IP [預設: 43.213.128.240]: " INPUT_SERVER_IP
SERVER_IP=${INPUT_SERVER_IP:-43.213.128.240}

read -p "請輸入伺服器使用者名稱 [預設: ubuntu]: " INPUT_SERVER_USER
SERVER_USER=${INPUT_SERVER_USER:-ubuntu}

read -p "請輸入 IN Camera RTSP 網址 [預設: rtsp://127.0.0.1/in]: " INPUT_IN_CAMERA
IN_CAMERA=${INPUT_IN_CAMERA:-"rtsp://127.0.0.1/in"}

read -p "請輸入 OUT Camera RTSP 網址 [預設: rtsp://127.0.0.1/out]: " INPUT_OUT_CAMERA
OUT_CAMERA=${INPUT_OUT_CAMERA:-"rtsp://127.0.0.1/out"}

read -p "請輸入遠端媒體路徑 (face_data_dir) [預設: /home/ubuntu/pvms-api/media]: " INPUT_FACE_DATA_DIR
FACE_DATA_DIR=${INPUT_FACE_DATA_DIR:-"/home/ubuntu/pvms-api/media"}

read -p "請輸入 API 網址 (API_url) [預設: https://demosite.api.ginibio.com/api/v1]: " INPUT_API_URL
API_URL=${INPUT_API_URL:-"https://demosite.api.ginibio.com/api/v1"}

read -p "請輸入部署位置 ID (location_ID) [預設: 1]: " INPUT_LOCATION_ID
LOCATION_ID=${INPUT_LOCATION_ID:-1}

echo ""
read -s -p "🔑 請輸入遠端伺服器 ($SERVER_USER@$SERVER_IP) 的登入密碼 (供 sshpass 使用): " SERVER_PASSWORD
echo ""

echo ""
echo "▶ 以下為您的設定："
echo "  - 專案路徑: $PROJECT_DIR"
echo "  - 執行身份: $CURRENT_USER"
echo "  - 伺服器 IP: $SERVER_IP"
echo "  - 伺服器使用者: $SERVER_USER"
echo "  - 遠端媒體路徑: $FACE_DATA_DIR"
echo "  - API 網址: $API_URL"
echo "  - 位置 ID: $LOCATION_ID"
echo "  - 進入攝影機 (IN): $IN_CAMERA"
echo "  - 離開攝影機 (OUT): $OUT_CAMERA"
echo ""
read -p "請按 Enter 鍵繼續安裝，或按 Ctrl+C 取消..."

# --- 4. 安裝系統依賴套件 ---
echo "▶ [1/6] 安裝基礎與系統依賴套件 (apt-get)..."
sudo apt-get update
sudo apt-get install -y build-essential curl wget git git-lfs software-properties-common \
    sshpass python3 python3-pip python3-venv \
    ffmpeg mosquitto mosquitto-clients \
    libxcb-xinerama0 libxcb-xfixes0 libxcb-shape0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0

# 檢查系統預設 Python3 是否滿足 >= 3.10 (針對 Ubuntu 20.04 等舊系統的防護)
PYTHON_CMD="python3"
if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "▶ 警告：系統預設 Python3 版本低於 3.10，將自動為其安裝 Python 3.10 (deadsnakes PPA)..."
    sudo add-apt-repository ppa:deadsnakes/ppa -y
    sudo apt-get update
    sudo apt-get install -y python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
fi

echo "▶ 啟動並設定 Mosquitto 服務..."
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Git LFS
echo "▶ 拉取大型模型檔案 (git lfs pull)..."
git lfs install
git lfs pull

# --- 5. 建立 Python 虛擬環境與安裝相依套件 ---
echo "▶ [2/6] 建立 Python 虛擬環境 (venv)..."
if [ ! -d "venv" ]; then
    $PYTHON_CMD -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt

# --- 6. SSH 金鑰設定 ---
echo "▶ [3/6] SSH 金鑰設定..."
SSH_KEY_PATH="/home/$CURRENT_USER/.ssh/id_rsa"
if [ ! -f "$SSH_KEY_PATH" ]; then
    echo "  >> 未找到 SSH 金鑰，自動產生..."
    ssh-keygen -t rsa -b 4096 -N "" -f "$SSH_KEY_PATH" -C "auto-generated@face-system"
else
    echo "  >> SSH 金鑰已存在 ($SSH_KEY_PATH)"
fi

echo "  >> 接下來將執行 sshpass+ssh-copy-id 將公鑰靜默傳送至 $SERVER_USER@$SERVER_IP"
echo "  >> (將自動輸入密碼並略過指紋驗證)"
sshpass -p "$SERVER_PASSWORD" ssh-copy-id -o StrictHostKeyChecking=no -i "${SSH_KEY_PATH}.pub" "$SERVER_USER@$SERVER_IP" || {
    echo "  ⚠️ ssh-copy-id 未完全成功，可能是網路或密碼錯誤。設定檔仍會產生，稍後可手動檢查。"
}

# --- 7. 更新 config 設定檔 ---
echo "▶ [4/6] 產生 config.json..."
# 使用 Python 內嵌腳本完美修改 config 設定
python - <<EOF
import json
import os
import sys

# 把專案目錄加進 Path 確保找到 setting 目錄
sys.path.append("$PROJECT_DIR")

try:
    from setting.build_config import default_config
except ImportError:
    print("無法載入 setting/build_config.py")
    sys.exit(1)

# 修改記憶體中的 default_config
default_config["Server"]["ip"] = "$SERVER_IP"
default_config["Server"]["username"] = "$SERVER_USER"
default_config["Server"]["ssh_key_path"] = "$SSH_KEY_PATH"
default_config["Server"]["face_data_dir"] = "$FACE_DATA_DIR"
default_config["Server"]["API_url"] = "$API_URL"
try:
    default_config["Server"]["location_ID"] = int("$LOCATION_ID")
except ValueError:
    default_config["Server"]["location_ID"] = "$LOCATION_ID"
default_config["cameraIP"]["in_camera"] = "$IN_CAMERA"
default_config["cameraIP"]["out_camera"] = "$OUT_CAMERA"

config_path = os.path.join("$PROJECT_DIR", "config.json")
with open(config_path, "w", encoding="utf-8") as f:
    json.dump(default_config, f, ensure_ascii=False, indent=2)

print("  >> config.json 已成功寫入！")
EOF

# --- 8. Systemd 用戶級服務註冊 ---
echo "▶ [5/6] 設定 Systemd 用戶級服務..."
USER_SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_SERVICE_DIR"
SERVICE_FILE_PATH="$USER_SERVICE_DIR/facial_recognition.service"

# 確認當前使用者的 XDG_RUNTIME_DIR ID (通常是 1000)
USER_ID=$(id -u "$CURRENT_USER")

# Systemd 只負責啟動 Python，螢幕偵測與視窗定位由程式自動處理
tee "$SERVICE_FILE_PATH" > /dev/null <<EOF
[Unit]
Description=Face Recognition System User Service
After=network.target graphical-session.target

[Service]
Type=simple
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
Environment=XAUTHORITY=$HOME/.Xauthority
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python -u $PROJECT_DIR/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable facial_recognition.service
# 立即啟動服務
systemctl --user start facial_recognition.service || true

# --- 10. Cronjob 排程設定 ---
echo "▶ 設定自動報表 (analytics_reporter.py) 排程..."
# 確保 log 目錄存在
mkdir -p "$PROJECT_DIR/log"
CRON_JOB="0 * * * * $PROJECT_DIR/venv/bin/python $PROJECT_DIR/analytics_reporter.py >> $PROJECT_DIR/log/reporter.log 2>&1"
# 檢查是否已經存在
(crontab -l 2>/dev/null | grep -v "analytics_reporter.py"; echo "$CRON_JOB") | crontab -
echo "  >> 每小時產出報表之 Cron Job 已生效。"

echo ""
echo "============================================="
echo " 🎉 安裝已全部完成！"
echo "============================================="
echo "您可以透過以下指令查看服務狀態："
echo "  systemctl --user status facial_recognition.service"
echo "============================================="
echo ""
echo "⚠️  ====== 重要：請手動重啟系統 ====== ⚠️"
echo ""
echo "  部分系統設定（顯示環境、systemd 服務啟動等）"
echo "  需要在重新啟動後才能完全生效。"
echo "  若未重啟，辨識畫面的視窗可能無法正常顯示。"
echo ""
echo "  請執行以下指令立即重新啟動主機："
echo ""
echo "    sudo reboot"
echo ""
echo "========================================"
