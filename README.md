# 人臉辨識系統操作手冊

- [開發機](#development-host)：開發、測試、封裝及發布。
- [測試機／正式機](#field-host)：首次安裝與既有主機設定。
- [首次正式版尚未發布時的新機初裝](#initial-acceptance)：固定來源驗收；已正常啟動者由第 6 步繼續。
- [一鍵環境檢查](#host-preflight)、[一鍵 OTA 驗收](#ota-check)：免逐台手動判讀多個指令。
- [Ubuntu 22.04 並裝 Python 3.12](#python312-on-jammy)、[Qt 桌面連線修復](#qt-display)：保留原環境，依維護許可處理。
- [舊工具替代驗收](#installed-check)、[準備中與查詢排錯](#ota-progress)：不必重裝辨識程式。
- [SMTP 設定](#smtp)：自動寫入主機 ID、角色與通知設定。
- [首次接入 OTA](#ota-enrollment)：一次指令自動準備、驗收及啟用。
- [多台主機共用設定](#fleet-profile)：匯出私有部署設定，省去逐台輸入帳密。
- [一台測試機＋一台正式機的 OTA 測試步驟](#two-host-ota)：測試機收 RC／正式版，正式機只收正式版。
- [運作範例](#runtime-flow)、[發布更新](#release)、[故障處理](#operations)、[驗收方法](#acceptance)。
- [主管工作紀錄](OTA_TEST_RECORD.md)：本輪實測流程、成功依據與尚未演練項目。

## 1. 準備與主機角色

- 現場主機：Ubuntu、桌面顯示環境、Python 3.10+、攝影機、業務 API／照片來源及 MQTT。
- 主機主動對外連 GitHub HTTPS 與 SMTP；可透過 AnyDesk 首次設定，不需要 inbound SSH 或中介機。
- 本版各主機直接查詢 Git tags，並透過 SMTP 寄送失敗通知；既有 Release updater 需依第 6.4 節一次性遷移。
- 部署清單的 Python minor／CPU 架構必須與主機相符；Python 本體及系統套件另行維護。
- 每台保留自己的 config、照片、特徵、金鑰與資料庫；勿複製另一台的主機 ID 或 OTA 狀態。
- GitHub token、SMTP 密碼與本機 JSON 不加入 Git 或工具維護包。

| 角色 | 自動更新範圍 |
| --- | --- |
| development 開發機 | 不自動更新，不安裝 OTA 背景服務 |
| test 測試機 | 適用專案的較新 RC／正式版 |
| production 正式機 | 適用專案的較新正式版 |

<a id="development-host"></a>

### 1.1 開發機

1. 已有專案、venv、config 時沿用；新環境先準備必要系統套件：

   ```bash
   sudo apt update
   sudo apt install -y build-essential git git-lfs python3-venv ffmpeg libxcb-xinerama0 libxcb-xfixes0 libxcb-shape0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
   ```

2. 尚無專案才下載：

   ```bash
   git clone https://github.com/yangtandev/facial_recognition.git
   cd facial_recognition
   ```

3. 確認 Python 與現場相符，取得模型及依賴；已運行服務時先安排維護：

   ```bash
   git lfs install
   git lfs pull
   if [ ! -d venv ]; then python3 -m venv venv; fi
   venv/bin/python -m pip install -r requirements.txt
   ```

4. 已有 config.json 先備份、保留原值；尚無設定時才產生：

   ```bash
   if [ ! -f config.json ]; then venv/bin/python setting/build_config.py; fi
   venv/bin/python ui/setting_tool.py
   ```

5. 桌面環境執行 venv/bin/python main.py 實測；既有服務運行時不要再啟動第二份。
6. 開發機不需 SMTP／agent 設定。若使用設定精靈，選 development 會保存角色與主機 ID、略過 SMTP，不啟用 OTA。
7. 執行[自動測試](#acceptance)，再依[發布步驟](#release)準備 RC。

<a id="field-host"></a>

### 1.2 測試機與正式機

1. 使用原服務的桌面登入使用者操作，不以 root 執行安裝。
2. 新主機依[第 2 節](#first-install)安裝；已有 config 的主機使用[僅設定模式](#smtp)，不重跑完整安裝。
3. 測試機選 test；正式機選 production。精靈自動產生各機不同的 UUID。
4. 保留各機 API、location、攝影機、照片來源與 MQTT。
5. 依[第 4 節](#ota-enrollment)執行一次接入指令；正式機可先啟用輪詢等待首個正式版，原辨識服務不變。正式版到達後自動準備基準版本（baseline）與驗收。
6. 測試機自動選適用 RC／正式版，正式機只選人工放行的正式版；兩者都須在本機通過啟動檢查。
7. 測試機通過 RC 人工實測後，由開發人員發布正式版；測試機維持 test，也會更新成正式版。
8. 各機獨立查詢、更新與寄信，不互相等待。

<a id="first-install"></a>

## 2. 新主機初次部署

<a id="host-preflight"></a>

### 2.1 下載工具與環境檢查（尚不安裝辨識服務）

1. 在新主機的桌面終端，以日後運行服務的使用者操作；不要用 root 或 sudo bash install.sh。新部署建議 Ubuntu 24.04、Python 3.12、x86_64；仍須以實際 tag 部署清單的需求為準。
2. 尚無專案目錄時才執行以下指令；已有目錄就直接 cd，不要刪除重裝。私有 repo 在 Git 提示時使用有權限的唯讀 PAT；不要將 token 寫進 URL／指令或貼到對話。

   ```bash
   sudo apt update
   sudo apt install -y git python3-venv pciutils
   cd "$HOME"
   git clone --branch develop-v1.0.0 https://github.com/yangtandev/facial_recognition.git
   cd "$HOME/facial_recognition"
   bash install.sh --preflight
   ```

3. --preflight 自動列出使用者、家目錄、OS、Python、CPU 架構、可用磁碟、GPU 與是否已有 config；不安裝套件、不改 JSON、不切換／重啟服務、不寄信。result=info 是資訊盤點，不代表已驗證 Release／網路／相機／venv。
4. config_exists=true：停止完整安裝，改用[第 3、4 節](#smtp)。若是重做失敗的初次安裝，也先排查，不刪 config 來強行重跑。
5. 這裡取得開發分支只為使用工具，尚未部署 app；正式營運使用下節已放行版本。首次正式版尚未發布、管理者要先在新機驗證初裝時，依[第 2.3 節](#initial-acceptance)固定指定來源，不直接安裝隨時變動的分支，也不把正式機改成 test 來接收 RC。

### 2.2 安裝已放行版本

1. 管理者先確認已放行的 Git tag 含 ota/deploy.json、適用該專案，且 Python minor／架構相符。舊流程已驗收的 v1.4.0 是既有兩機的正常基準；它沒有新部署契約，不能當作新 tag-only OTA 候選。新 tag 及其他專案／環境仍須另驗。正式機僅使用正式版，測試機才使用 RC。
2. 全新主機、尚無 config 時才切換指定 tag 並安裝；先將 vX.Y.Z 換成實際已放行、帶部署契約的正式 tag。出現本機修改衝突就停止，不強制覆蓋：

   ```bash
   cd "$HOME/facial_recognition"
   FACIAL_INSTALL_VERSION=vX.Y.Z
   git fetch origin tag "$FACIAL_INSTALL_VERSION" &&
   git show "$FACIAL_INSTALL_VERSION:ota/deploy.json" >/dev/null &&
   git switch --detach "$FACIAL_INSTALL_VERSION" &&
   bash install.sh --preflight &&
   bash install.sh
   ```

3. 精靈初期：正式機填 production，測試機填 test；repo 填 yangtandev/facial_recognition，輸入 GitHub 唯讀 token、SMTP 與管理員信箱，測試信選 y。主機 UUID 與相關 JSON 自動寫入，不需抄寫 device_id／hash。
4. 輸入這台自己的伺服器、照片路徑、API、location ID 與攝影機 RTSP；不可複製另一地點的 config。其餘系統套件、模型、venv、JSON、SSH 金鑰與服務由 install.sh 處理。現有完整安裝仍從 requirements.txt 解析相依，可能下載 CUDA 依賴；不等於已安裝 Release 的 CPU 離線環境，最後仍須接入 OTA 並驗收。
5. 安裝完成先檢查現有桌面及辨識服務；Qt 連線問題依[第 7.4 節](#qt-display)處理，可只重啟辨識服務，不必一律重開主機。若系統／驅動維護確需整機重開，另排維護時段執行 `sudo reboot`，之後登入原使用者桌面／AnyDesk。
6. 在原桌面帳號確認辨識畫面、真人辨識、語音、相機、MQTT 與建檔照同步，再查原服務：

   ```bash
   cd "$HOME/facial_recognition"
   systemctl --user status facial_recognition.service --no-pager
   journalctl --user -u facial_recognition.service -n 100 --no-pager
   ```

7. 使用本版 OTA 工具接入，沿用已填的設定；正式機先啟用輪詢與通知。沒有適用正式版時維持待命；已有適用正式版時，輪詢後就會準備、備份、短暫切換辨識並驗收，啟用前須確認維護許可。若 tag 內工具較舊，先依[第 4.1 節](#ota-enrollment)更新工具。正常情況不用手填 baseline 路徑或 enable：

   ```bash
   bash install.sh --ota-setup
   bash install.sh --ota-check --expect-role production
   ```

8. 測試機將 --expect-role production 改為 --expect-role test；需確認某個版本時，加 --expect-version v1.4.0（改成實際目標）。寫入 SMTP 或 git pull 完成，都不代表 OTA 已安裝成功。
9. 接入／更新完成後，執行[第 7.1 節](#ota-check)的即時驗收與業務實測。重開機恢復屬另一項演練，僅於另行核准的維護時段進行；未演練不能記為已通過。日常新版發布後主機自行更新，不再逐台 pull 或重跑完整安裝。

- 已有 config.json 時，完整安裝會拒絕執行，避免覆蓋現場設定；改用下節。
- 不用 setting/build_config.py 遷移舊設定，避免遺失未知欄位。

<a id="initial-acceptance"></a>

### 2.3 首次正式版尚未發布：新機先做初裝驗收

- 本節保留首個正式版前的初裝流程；本輪 demosite 已有 v1.4.0 正式版，新機優先依第 2.2 節。其他尚無適用正式版的專案，仍可依此流程先驗收與待命。
- 適用全新、尚無 config 的待驗收主機；不代表已放行正式營運。本節固定來源為 c831bbdc434a328caf12d09fd1ccc70104a565ea，包含新版安裝與檢查工具，但不是已發布正式套件。
- production 是 OTA 選版角色，不會阻止管理者透過 install.sh 人工安裝指定來源；人工初裝與 OTA 接入是兩個階段。來源版缺少 BUILD_INFO.json 時，health 的 version／build_id 可為 unknown，不能當作已安裝某個 Release。
- 已完成以下第 5 步、辨識正常啟動者，直接從第 6 步繼續；不要重新 clone、切換程式或重跑完整安裝。

1. 在新主機的桌面終端、以日後運行服務的使用者操作，先裝下載工具：

   ```bash
   sudo apt update
   sudo apt install -y git python3-venv pciutils
   ```

2. 確認尚無 $HOME/facial_recognition 目錄，再下載、固定來源並盤點環境。任何一步失敗就停止，不刪既有目錄來重做；私有 repo 的 Git 驗證使用有權限的唯讀 PAT，不將 token 放入 URL：

   ```bash
   cd "$HOME"
   git clone --branch develop-v1.0.0 https://github.com/yangtandev/facial_recognition.git &&
   cd "$HOME/facial_recognition" &&
   git switch --detach c831bbdc434a328caf12d09fd1ccc70104a565ea &&
   bash install.sh --preflight
   ```

   Ubuntu 24.04／Python 3.12／x86_64 主機應看到相符資訊及 config_exists=false；若已有 config，停止完整安裝。

3. 安裝首次辨識服務（不是 sudo bash install.sh）：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh
   ```

   - 角色填 production，repo 填 yangtandev/facial_recognition；輸入唯讀 GitHub token、SMTP、管理員信箱，測試信選 y 並確認收到。
   - 伺服器 IP／帳號／密碼及照片路徑，是建檔照來源伺服器的資料，不是這台新機的登入資料，也不是 OTA 管理端。
   - IN／OUT RTSP、API、location ID 使用此現場自己的值，不盲目沿用 demosite 等預設值。
   - UUID、JSON、模型、venv 與服務由腳本處理；requirements 安裝可能下載 CUDA 依賴，尚非 Release 的 CPU 離線環境。中途失敗先查錯，不刪 config／venv 強行重跑。

4. 安裝完成先在原桌面帳號檢查服務；不能任意重開的主機，先依[第 7.4 節](#qt-display)排除桌面連線問題。確需整機重開時才另排維護時段執行 `sudo reboot`，之後登入原使用者桌面／AnyDesk。

5. 確認辨識畫面啟動，並查原服務及日誌：

   ```bash
   cd "$HOME/facial_recognition"
   systemctl --user status facial_recognition.service --no-pager
   journalctl --user -u facial_recognition.service -n 100 --no-pager
   ```

   active 只證明程序啟動，不代表真人辨識、照片同步及 OTA 已驗收。

6. 初裝啟動後，核對角色、repo、專案及 location ID；以下只讀取指定欄位，不印 token／SMTP 密碼、不查外部 API：

   ```bash
   cd "$HOME/facial_recognition"
   python3 - <<'PY'
   import json
   from pathlib import Path
   from urllib.parse import urlsplit
   from ota.configure import configuration_path
   from ota.core import read_json

   agent = read_json(configuration_path(Path.cwd()))
   server = read_json("config.json")["Server"]
   host = urlsplit(server.get("API_url", "")).hostname or ""
   print(json.dumps({"role": agent.get("role"), "repo": agent.get("repo"),
                     "project": host.split(".")[0], "location_ID": server.get("location_ID")},
                    ensure_ascii=False, indent=2))
   PY
   ```

   - role 應為 production、repo 應為 yangtandev/facial_recognition；專案／地點須符合這台現場。這裡只核對 location ID，地點名稱由 OTA 依業務 API 查詢。
   - 真人實測 IN／OUT 辨識、進出方向、語音與畫面；新增或更新一筆建檔照，確認 MQTT 收到後照片／特徵同步、實際可辨識，不能只看 MQTT 連線成功。
   - 確認管理員收到安裝時的 SMTP 測試信。尚未測試、角色填錯或通知設定要調整時才執行 bash install.sh --ota-config-only；Enter 保留原值、選 y 測信，不重裝辨識服務。
   - 保留目前正常服務；接下來更新 OTA 工具並接入，不自行 enable 不存在的服務，不改成 test，也不把目前來源冒充成正式版。

7. c831bbd 的舊工具尚不支援待命接入；先依[第 4.1 節](#ota-enrollment)取得本版 ota/ 與 install.sh，再保留現有 config／venv／服務直接接入。即使只有 RC、沒有正式 Release，也能先啟用輪詢，不必等正式版再下指令：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-setup &&
   bash install.sh --ota-check --expect-role production
   ```

   應看到 setup.phase／enrollment_state／檢查 result 為 waiting_release，三服務 active／enabled；selected_build=null 是尚未安裝首個正式套件，不是失敗。尚無正式版不寄更新失敗信；原服務程序與 MQTT 不因待命接入而重啟。

8. 發布適用同專案的新 RC，等至少一輪查詢後，依[第 7.1 節](#ota-check)查 last_poll.query_ok=true、checked_at 晚於發布時間、rc_skipped 包含該 RC；同時確認原辨識服務 MainPID／NRestarts 未變且持續正常，證明正式機有查到但沒有安裝 RC。待命狀態沒有正式 version／build ID，不偽造版號。測試機應自行更新該 RC；未啟用輪詢、查詢失敗或尚未查到該 RC，不能當作隔離測試通過。
9. RC 驗收通過、管理者推送帶部署清單的適用正式 tag 後，這台正式機會自動下載並安裝，不再執行接入指令。成功後 selected_build 與即時 health 相符、enrollment_state=managed、setup.phase=complete；首次安裝失敗保留／還原原服務及設定，寄失敗信，同一失敗版不無限重試，修正版或人工 retry 才重試。

<a id="python312-on-jammy"></a>

### 2.4 Ubuntu 22.04：並裝 Python 3.12，不替換原 3.10

- Ubuntu 22.04／Python 3.10 可運行 OTA agent，但 v1.4.0 更新包需要 Python 3.12、x86_64。OTA 只選已安裝的相符 Python，不會代裝系統 Python。
- 此處使用第三方 [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)，須納入系統套件維護。先記錄原辨識程序，再模擬安裝；若有移除套件或升級既有關鍵套件，先停下審閱。

```bash
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
sudo add-apt-repository ppa:deadsnakes/ppa &&
sudo apt update &&
sudo apt-get -s install --no-install-recommends python3.12 python3.12-venv
```

確認清單只有預期新增套件後才執行；若缺少 `add-apt-repository`，先審閱安裝 `software-properties-common` 所需變更。

```bash
sudo env NEEDRESTART_MODE=l apt-get install \
  --no-install-recommends python3.12 python3.12-venv
```

`NEEDRESTART_MODE=l` 讓 needrestart 只列出待重啟項目；仍須審閱 apt 計畫，不代表所有套件維護腳本都不會影響服務。不要另跑整機 upgrade／reboot，也不要用 alternatives 或 symlink 替換 `/usr/bin/python3`。

```bash
python3 --version
"$HOME/facial_recognition/venv/bin/python" --version
python3.12 -c 'import sys, platform, venv, ensurepip; print(sys.version); print(platform.machine()); print("venv / ensurepip OK")'
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
```

本輪結果：原系統及原 app venv 仍為 3.10.12，新增 3.12.15／x86_64，原 MainPID／NRestarts 未變。這證明並裝前置就緒；實際套件安裝與業務相容性仍需 OTA 驗收。只並裝不用重跑完整 `install.sh` 或重裝原 requirements；OTA 會另建 3.12 venv 並離線安裝包內依賴。

<a id="smtp"></a>

## 3. OTA／SMTP 設定

### 3.1 修改設定或先測試 SMTP

1. 使用本版 ota/ 與 install.sh，在原專案執行：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-config-only
   ```

2. 不用手動停止服務；有 OTA 作業進行時，精靈會等待安全時機再保存。此模式不執行 apt、不改辨識 config.json、不切換版本。
3. Enter 沿用既有值；UUID、路徑及未知欄位保留。敏感值不顯示；要清空既有 token／帳密時，直接編輯實際設定檔。
4. 精靈自動找到已安裝的 state_dir/agent.json；尚未接入則寫入 ota/agent.local.json。權限為 600，舊值保存為同目錄的 *.before-smtp.local.json。
5. 新版 poll／notify 每輪重新讀取設定，SMTP／GitHub token 改好後不用重啟；生效需等下一輪。舊版 agent 先執行一次 --ota-setup 更新工具。
6. 首次尚未接入者繼續第 4 節。需要自訂設定位置時：

   ```bash
   python3 -m ota.configure --project "$HOME/facial_recognition" --config /absolute/path/agent.json
   ```

#### 只補 GitHub token，或想立即重查

- 執行 `bash install.sh --ota-config-only`，角色保留 production／test、repo 保留原值，只在 token 欄輸入唯讀 PAT；其餘按 Enter 保留，無需測 SMTP 時選 N。精靈會定位已安裝的 `state_dir/agent.json`，不要只改已不使用的原專案副本。
- Git clone 登入成功不代表 OTA agent 已有 token。token 不貼到對話、不寫入 URL／shell 指令，也不輸出整份設定 JSON。
- 新版 agent 下輪會讀新設定，但不會中斷已開始的等待。確認 `--ota-status` 沒有進行中的更新／接入，且允許立即派工後，才可只重啟 poll；這可能立即觸發適用新版的準備與切換。

```bash
systemctl --user restart facial-ota-poll.service
bash install.sh --ota-status
journalctl --user -u facial-ota-poll.service -u facial-ota-setup.service --since "5 minutes ago" -n 80 --no-pager
```

成功查詢看 `last_poll.query_ok=true`。仍為 null 時依[第 7.5 節](#ota-progress)排查，不能直接認定是缺 token。

### 3.2 輸入欄位與設定檔

| 欄位 | 填寫 |
| --- | --- |
| 角色 | development、test、production |
| GitHub repo | owner/repo；首次從現有 Git remote 預填，已有設定沿用 |
| GitHub token | 私有 repo 必填唯讀 PAT；公開 repo 可空白 |
| SMTP 主機 | 供應商的寄信主機 |
| TLS／連接埠 | 通常 starttls＋587 或 ssl＋465，依供應商 |
| SMTP 帳密 | 寄信帳號與密碼／應用程式密碼；無需認證的 TLS relay 可空白 |
| 寄件信箱 | SMTP 允許使用的寄件地址 |
| 收件信箱 | 管理員地址，多位以逗號分隔 |

- device_id 自動產生、重設時沿用；不需要 device_token／token_sha256。
- Fine-grained PAT 僅選此 repo、Contents: Read-only；記錄到期日，組織要求時等待核准。[GitHub PAT 設定](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- shared_dir、state_dir 與系統 Python 預設自動取得；非標準部署再核對路徑。agent 使用 app／state 以外的系統 Python 3.10+，程式環境另依部署清單所需 minor 建立；主機有安裝相符 minor 時會自動選用，沒有就保留原服務並通知。
- 以下 `$HOME` 代表目前登入使用者的家目錄，不限定帳號；專案預設位於 `$HOME/facial_recognition`，狀態預設位於 `$HOME/.local/share/facial-ota`。自訂路徑時依實際設定替換，勿用 sudo 執行使用者服務指令。
- 專案名取自 config.json 的 Server.API_url 主機名稱第一段；地點以 Server.location_ID 查 API_url/locations/ID/ 的 name。
- 例如 https://demosite.api.ginibio.com/api/v1、ID 1，專案 demosite，地點可為「測試閘道」。查詢失敗沿用快取，首次失敗顯示 location ID。
- 寄信使用 Python 標準函式庫與 TLS，不需另裝郵件伺服器。[SMTP 文件](https://docs.python.org/3/library/smtplib.html)

<a id="fleet-profile"></a>

### 3.3 多台主機共用設定，免重複輸入帳密

1. 在一台已設定好的主機，匯出共用部署設定：

   ```bash
   python3 -m ota.configure --export-profile "$HOME/facial-ota-profile.local.json"
   ```

2. 檔案只含角色、repo、GitHub token 與 SMTP；不含 UUID、主機路徑或辨識 config。檔案含帳密、權限 600，請用安全檔案傳送方式提供給其他主機，不加入 Git。
3. 其他主機保留自己的 config.json，取得新版 ota/、install.sh 與該部署設定檔後執行：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-setup --profile "$HOME/facial-ota-profile.local.json" --role production
   ```

4. 測試機改用 --role test。此模式不再詢問 SMTP；每台自動產生不同 UUID、依自己 config 查專案與地點。已有主機設定優先保留，共用檔只補缺少的值；明確指定的 role 會套用。
5. 日後改帳密用 --ota-config-only；不用重新產生 UUID 或重新部署辨識程式。匯出設定只是省輸入，不會替每台機器開放遠端登入或自動傳送檔案。

<a id="ota-enrollment"></a>

## 4. 首次接入 OTA：一次指令

### 4.1 現場需要做的事

1. 確認原 facial_recognition.service 正在運作，該服務使用這台自己的 config.json。安排可接受短暫切換的維護時段。
2. 將本版 ota/ 與 install.sh 提供至原專案；不用覆寫 main.py、config 或 venv。只有 AnyDesk 時可安全傳送檔案；原專案有 Git 的主機，在管理者已將本版工具提交推送後，可用以下指令先備份，再只取出工具（不切換 app、不做 git pull）：

   ```bash
   cd "$HOME/facial_recognition"
   FACIAL_OTA_TOOLS_COMMIT=REPLACE_WITH_APPROVED_40_HEX_COMMIT
   [[ "$FACIAL_OTA_TOOLS_COMMIT" =~ ^[0-9a-f]{40}$ ]] &&
   FACIAL_OTA_TOOLS_BACKUP=$(mktemp -d "$HOME/facial-ota-tools.XXXXXX") &&
   cp -a ota install.sh "$FACIAL_OTA_TOOLS_BACKUP/" &&
   git fetch origin "$FACIAL_OTA_TOOLS_COMMIT" &&
   test "$(git rev-parse FETCH_HEAD)" = "$FACIAL_OTA_TOOLS_COMMIT" &&
   git archive --format=tar --output="$FACIAL_OTA_TOOLS_BACKUP/tools.tar" "$FACIAL_OTA_TOOLS_COMMIT" -- ota install.sh &&
   tar -xf "$FACIAL_OTA_TOOLS_BACKUP/tools.tar" -C "$PWD"
   ```

   先將佔位值換成實際已審閱並推送的工具 commit；尚未提交／推送的改版無法由 Git 取得，先完成開發端前置。一般分支更新用第 6.4 節 Git pull 流程，舊 v1.4.0 工具沒有新功能。備份目錄包含本機 OTA 帳密，請保留其私有權限、不上傳 Git；解壓不覆蓋未追蹤的 agent.local.json。固定來源／detached HEAD 的主機也不需切換原辨識程式。
3. 正式機不用先有正式 tag；它先進入 waiting_release（沿用既有 checkpoint 名稱），查 Git tags、跳過 RC，沒有可部署正式 tag 是正常待命。測試機第一次建立基準需適用且帶 ota/deploy.json 的 RC／正式 tag；歷史無契約 tag 會略過。
4. 已完成 SMTP 設定者只需執行：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-setup
   ```

5. 尚未設定者會先進入精靈；已設定者直接沿用，不重填帳密。多台主機可用上節 profile。
6. 正式機顯示 waiting_release／「輪詢與通知已啟用」即完成待命接入，不會替換原服務；通常下一輪才出現 last_poll。測試機建立套件基準後顯示 complete。兩者皆需確認原辨識／MQTT 持續正常。
7. 用[第 7.1 節](#ota-check)的一鍵驗收確認程序與三個服務；例如測試機執行 bash install.sh --ota-check --expect-role test。

### 4.2 程式自動完成的步驟

1. 沒有套件基準的正式機先備份服務／設定、安裝獨立 poll／notify；不改 app 啟動路徑、不停止辨識或 MQTT、不偽造正式版號。無適用正式版、GitHub 暫時不可達時保留原服務並稍後再查；查詢錯誤與待命狀態可從 last_poll 區分。
2. 找到適用正式版時，由獨立 setup worker 安裝；測試機則依角色選 RC／正式版。解析固定 commit，驗證部署清單、Git 來源、模型及 Python／CPU 相容性。
3. 完整鎖定依賴與現場環境吻合時驗證後重用；有差異另建環境，準備階段舊版繼續運作。只選主機已安裝且相符的系統 Python，不自動升級系統套件。
4. 備份原服務設定與現場 config，保留原程式及環境；新欄位補預設、更名按規則遷移，衝突或模型不相容就拒絕切換。
5. 短暫停止辨識／MQTT、切換新版，自動檢查版本／build ID／PID，模型、索引、GUI、必要執行緒，以及 MQTT 連線、訂閱和建檔照同步。
6. 通過啟動與穩定觀察後，登記第一個基準版本（baseline）、切換為 managed；待命輪詢自動銜接日後更新，不需要人工再接入。baseline 是日後回退的起點，不是另一種版本名稱。
7. 攝影機暫離線不單獨觸發回退；update_ready=true 代表必要啟動檢查通過，healthy=true 還要求啟用相機有新影像。恢復後仍需影像及業務實測。

### 4.3 斷線、失敗與重跑

- 執行工作交給獨立的 facial-ota-setup.service；關閉終端或 AnyDesk 不會中止背景接入。主機重開後，在使用者服務啟動時依 checkpoint 復原。
- 準備失敗不停止舊服務；切換／安裝失敗自動還原原設定與服務。照片、特徵與最新業務資料保留。
- 原版沒有健康檢查時，恢復後只能確認原程序持續運行，回退結果記為 restored_unverified；不宣稱業務已全面正常，收到信後需確認辨識。
- 恢復也失敗會記 rollback_failed／阻止後續更新，通知含失敗時間與時區；由 AnyDesk 人工修復。
- 備份在 state_dir/enrollment/作業UUID，包含 site-config.json 與 original-service.txt；替換的工具／服務檔會移入備份目錄，不直接刪除。
- SMTP 不通就先保存在本機；已接入待命或一般 OTA 者由 notify 補寄，尚無可用 notify 時由 setup worker 持續補寄。SMTP 故障不阻塞待命輪詢或回退。
- 同一接入仍在進行時重跑命令，只顯示同一工作，不重複切換；中斷的切換會先恢復原服務，不直接重做新版本。
- 已完成待命或一般 OTA 後，使用新版工具重跑 --ota-setup 會備份並更新獨立 agent、恢復 poll／notify；不重裝 app、不停止辨識、不更換 UUID。這一步用於 updater 本體維護，一般 app 更新不用跑。
- 首次正式版安裝失敗後，待命服務保留、失敗版不無限重試；排除原因後用第 7.2 節 retry 或發布修正版。重跑 --ota-setup 僅維護待命工具，不清除失敗版紀錄。尚未成功註冊待命者，修復接入問題後才重跑。
- 半套手動安裝或不明的既有服務檔不會直接覆寫；先查狀態及備份，避免把別人的設定當成自動安裝產物。
- 查結果與日誌：

   ```bash
   bash install.sh --ota-status
   journalctl --user -u facial-ota-setup.service -n 100 --no-pager
   ```

<a id="runtime-flow"></a>

## 5. 實際運作流程與範例

### 5.1 RC 到正式版

本次是一台測試機 T、一台正式機 P，已知正常基準為 v1.4.0。下表的 vX.Y.Z 是待核定的新目標版號，不是可直接執行的版本：

| 發布動作 | 測試機 T（test） | 正式機 P（production） |
| --- | --- | --- |
| 只 push branch commit，或 tag 沒有部署契約 | 不更新 | 不更新 |
| 推送帶契約 vX.Y.Z-rc.1 tag | 更新 RC，人工實測 | 不更新 |
| 修正並推送帶契約 vX.Y.Z-rc.2 tag | 更新 RC.2，再測 | 不更新 |
| 人工通過，推送帶契約 vX.Y.Z 正式 tag | 更新正式版 | 查到後更新正式版 |

- 推送帶部署契約的正式 tag 就是人工放行點。
- 新正式機已接入 waiting_release 時，RC 發布也會實際輪詢並記錄跳過；首個帶契約正式 tag 推送後自動首次安裝，不需再人工下接入指令。
- 測試機已測 v1.0.2-rc.1 時，不因 v1.0.1 發布而降版。
- 不適用專案或沒有部署契約的 tag 跳過；主機不互相等待。
- 人工發現功能問題但健康驗收已通過時，不會因人工回報而自動回退；修正後重走 RC。

### 5.2 查到新版後

1. 依角色、SemVer、專案、部署契約與失敗紀錄篩選 Git tags，保存 tag→commit 身分，拒絕移動過的 tag。
2. 確認舊版核心及 MQTT／同步正常；新版健康訊號排除僅攝影機無影像的情況。
3. 取得固定 Git commit；驗證後重用模型及完整依賴環境，否則另建 venv、只補缺少的鎖定 wheels；檢查模型與設定遷移，舊版繼續服務。
4. 備份 config，停止辨識與 MQTT，切換並啟動新版。
5. 核對版本／build ID／PID，驗證推論、索引、GUI、取像／偵測／比對執行緒與 MQTT／同步。
6. 預設啟動等待 180 秒、穩定觀察 60 秒；通過才記錄成功。
7. 新版啟動／驗收失敗就恢復舊程式、環境及 config，再驗收；準備失敗時不停止舊版。
8. 結果先保存本機；失敗由獨立 notify 服務寄信，成功不寄。

- 更新期間可漏收 MQTT；啟動／重連後主動補同步建檔照與特徵。
- 保留現場 API、地點、相機、MQTT、未知欄位；新增欄位補預設，更名依明確規則，有衝突拒絕更新。
- 回退不還原共享照片、最新特徵與業務資料。
- 重用 venv 前核對 Python ABI／CPU、完整套件名稱及版本、鎖定 wheel 的實際摘要／RECORD 與已安裝檔案摘要、pip check 及 requirements 可滿足性；不是只比 requirements 文字。新版本不對舊 venv 做 pip 增刪。
- 沿用舊 venv 的 app 用固定目錄連結；清理保留其擁有者版本，直到沒有保留中的 app 依賴它。模型經大小／SHA256 驗證後複製；缺少才用 Git LFS 取得並再驗。
- 現場需有 Git；缺模型且需 LFS 下載時也需 Git LFS。缺少工具或下載／檢查失敗都在切換前拒絕，保留舊服務。

### 5.3 攝影機離線與更新失敗

| 情況 | 處理 |
| --- | --- |
| 相機逾時／無影像，程序與必要執行緒正常 | 不判 OTA 失敗、不回退，持續重連 |
| 新版崩潰、版本／PID 不符、模型失敗、必要執行緒停止／卡住 | 驗收失敗並回退 |
| 新版正常但相機離線 | 更新可成功，完整健康 false；影像實測待補 |
| 舊版也無法通過回退驗收 | rollback_failed、阻止後續更新並寄信 |

- update_ready 代表可通過更新驗收；healthy 另包含相機有新影像。
- 僅無影像無法判斷是網路、設備或相機程式問題，也不能宣稱端到端影像已驗證。
- 持續相機離線預設 5 分鐘才寄「攝影機連線異常」，與「更新失敗」區分。
- 核心服務、MQTT／同步持續異常也依寬限通知；短暫中斷不寄。
- 同一持續異常只建立一次通知，恢復後再次異常才重建；更新／回退期間抑制額外警報。
- 驗收完成後的長期異常先依 systemd 重啟機制處理，不自動還原多日前版本／設定。

### 5.4 更新失敗、SMTP 也斷線

1. A 更新失敗回退 v1.0.0；B 成功留在 v1.0.1，各自保存結果，B 不受影響。
2. A 通知先寫本機 SQLite；SMTP 故障不阻塞回退、不刪待寄信。
3. 重開機後保留待寄事件及重試進度；恢復連線後依退避時間補送。
4. 分別保存每位收件者的 SMTP 接受結果；另一人失敗不重寄給已接受者。
5. SMTP 接受不保證信箱收到；極端回覆中斷仍可能重寄，不能保證 email 恰好一次。
6. 信件包含專案、地點、主機、原／目標／運行版本、錯誤與回退結果，以及帶時區的更新失敗、回退開始／完成／失敗與寄送時間。補寄保留事件原時間。
7. 舊歷史缺少精確時間時只能顯示既有事件時間；無法推算未記錄的斷電或回退失敗瞬間。

- 整台關機／完全斷網無法即時自行寄信；沒有信不代表所有主機均成功，需查本機狀態。
- GitHub 查詢失敗稍後再查、維持原服務；已記錄下載／準備／驗收失敗的版本需修正版或人工 retry。

### 5.5 頻率

| 設定 | 預設 |
| --- | --- |
| GitHub poll_seconds | 有 token 每輪 300 秒；無 token 最少 1200 秒 |
| 隨機延遲 | 每輪另加 0～10 秒，另計操作耗時 |
| 通知監看／寄送 | 每輪 15 秒，另加 0～10 秒與操作耗時 |
| alert_grace_seconds 持續異常寬限 | 300 秒，自首次觀察異常起算 |
| SMTP 失敗重試 | 遞增退避，最高約 5 分鐘，加隨機延遲 |

- GitHub 限流時遵守延後查詢時間，主機不會在發布瞬間全部一起更新。
- 更新／回退停機不新增額外服務警報；既有持續異常沿用原事件，確認恢復後才解除。
- 新版 agent 每輪重讀設定，調整間隔與寬限後通常無需重啟；已開始的等待會先走完。需要立即重查時，先確認沒有作業進行，再依第 3.1 節只重啟 poll。
- 服務啟動會先嘗試一輪；300／1200 秒是後續間隔，不是首輪必須等待的時間。`last_poll` 沒更新時另查日誌、執行中的作業及工具版本，不只等待或補 token。

<a id="two-host-ota"></a>

### 5.6 一台測試機＋一台正式機：分機 OTA 測試步驟

| 主機 | 桌面登入帳號／名稱 | 角色 | 自動安裝的來源 |
| --- | --- | --- | --- |
| 測試機 | gini-aiface@gini-aiface-NUC11TNKi5 | test | 適用且較新的 RC tag／正式 tag 指向的固定 commit |
| 正式機 | gini@Gini-MediaServer | production | 適用且較新的正式 tag 指向的固定 commit；略過所有 RC |

**不是每個 commit 都會 OTA。** 只 push branch commit 不觸發更新；目標 commit 必須含 `ota/deploy.json`，並由已授權的 `vX.Y.Z-rc.N`／`vX.Y.Z` tag 指向。兩機各自核對 tag→commit、專案、Python／CPU 與完整性，不跟隨可移動 branch。

本節是待執行的操作清單，不表示 tag-only 已做完現場驗收。以下版號佔位值須換成這次實際核准的 tag；舊 v1.4.1-rc.1 是故意啟動失敗、且沒有新部署契約的歷史 tag，不能用來驗收正常更新。

#### 共用前置：一次性遷移工具

1. 兩機目前已納管 v1.4.0，不重裝辨識、不重填角色、不複製另一機 config／UUID／資料庫。先查 `bash install.sh --ota-status`；有未完成 OTA／setup 時等原作業完成，不 retry 或重啟正在更新的 poll。
2. **先在測試機**依[第 6.4 節](#tag-only-migration)透過 Git 取得已推送的新工具、備份並維護 agent。完成後確認 `discovery=tags`、最新 `last_poll.source=tags`、`query_ok=true`，目前版本仍 v1.4.0、辨識 PID／業務正常。工具維護成功是 agent_upgrade，不是 app 新版本安裝成功。
3. 測試機工具驗收通過後，正式機另安排已核准的工具維護時段，依同節操作，保持 production。**要驗正式機略過 RC，正式機必須也已完成 tag-only 遷移**；舊 Release updater 查不到 Release 不算通過 tag 角色隔離。
4. 兩機用原桌面帳號與 `$HOME/facial_recognition`；不 `sudo systemctl --user`、不重開機。工具遷移完成與下一輪 app 切換的維護放行分別核對。

#### 測試機：先 RC commit，再正式版 commit

1. 開發端先依第 6.1～6.3 節完成本機驗證；取得當次 commit／push／tag 授權後，推送新的 RC tag，並提供 RC tag 與其完整 commit SHA。當輪只推 RC，不提前推正式 tag。
2. 測試機保持 test，讓原 poll 自行查詢。先執行狀態命令看進度；`preparing`／`validating` 期間等待，不以重啟或重跑安裝催進度：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-status
   ```

3. 選中版本已為目標 RC、`latest_update.phase=success` 後，驗 RC：

   ```bash
   FACIAL_OTA_RC_TAG='vX.Y.Z-rc.N'
   bash install.sh --ota-check --expect-role test --expect-version "$FACIAL_OTA_RC_TAG" --seconds 15
   ```

   成功判準：selected_build 的 version／commit／build_id 對應開發端提供的 RC tag／SHA，最新 app 作業目標為該 RC、phase=success、blocked=false；即時兩次取樣核心與 MQTT／同步正常、必要計數前進。再實測兩路影像、真人 IN／OUT、方向、語音、建檔照／特徵同步。相機暫離線的 warning 不算業務已全部驗完。
4. 同時依下節查正式機：確實查到且略過這個 RC，仍跑原正式 commit。RC 出問題就修正、用新的 rc.N 重測，不移動原 tag、不把測試機改成 production。
5. RC 業務驗收通過，管理者確認正式機可短暫切換後，開發端才推同目標正式 tag；正式 commit 的執行碼、模型、依賴及遷移需與已測 RC 一致，僅版本／說明 metadata 可另審。不要在驗完這次正式版前先讓測試機跳到更高版本的下一輪 RC。
6. 測試機維持 test，會再自動更新到正式 tag 的 commit。成功後驗正式版：

   ```bash
   FACIAL_OTA_STABLE_TAG='vX.Y.Z'
   bash install.sh --ota-status
   bash install.sh --ota-check --expect-role test --expect-version "$FACIAL_OTA_STABLE_TAG" --seconds 15
   ```

   核對正式 tag／正式 commit SHA／build ID、該次 app success，再做相同業務實測；不把先前 RC 成功當作這次正式版已安裝。

#### 正式機：略過 RC，只更新正式版 commit

1. 確認已完成上述工具遷移、角色仍 production；RC 發布前記錄原正式版本／commit、MainPID 與 NRestarts：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-status
   systemctl --user show facial_recognition.service -p MainPID -p NRestarts
   ```

2. 開發端推 RC 後，等這台完成一輪成功的 tag 查詢，再重跑上述兩個命令。判準是 `last_poll.source=tags`、`query_ok=true`、checked_at 已更新、`rc_skipped` 包含該 RC；selected_build 仍為原正式 tag／commit，PID／NRestarts 未因 RC 改變，沒有該 RC 的 app 更新作業。最初基準為 v1.4.0，後續按當時實際正式版核對。
3. RC 人工驗收未通過時，正式機持續原版。只有當管理者核准，開發端推送對應的正式 tag，才會開始自動更新；**推正式 tag 就是 production 部署放行**，沒有另一個 GitHub Release 發布開關。
4. 正式 tag 推送後等待原 poll 準備／切換／健康驗收；先查狀態，作業 success 後再驗版本：

   ```bash
   cd "$HOME/facial_recognition"
   FACIAL_OTA_STABLE_TAG='vX.Y.Z'
   bash install.sh --ota-status
   bash install.sh --ota-check --expect-role production --expect-version "$FACIAL_OTA_STABLE_TAG" --seconds 15
   ```

5. 成功判準：selected_build 對應正式 tag／正式 commit SHA，最新 app 作業為該正式版且 success、blocked=false，兩次即時健康／計數通過，並實測真人辨識、方向、語音與照片／特徵同步。正式更新切換後 PID 改變正常；「RC 時 PID 不變」與「正式更新後 PID 穩定」是不同判準。
6. 準備失敗保留原程式，啟動驗收失敗按原流程回退並寄失敗信；此時不能記成功。保留失敗歷史，核對 rollback 與實際收件，再決定修正版或明確 retry。

共同紀錄每輪 tag、commit、attempt_id、phase／rollback、即時驗收與業務結果。正常成功不寄更新失敗信；pending=0 不單獨代表更新成功。故障啟動回退、SMTP 中斷補寄及斷電演練是另外的驗收範圍，不以本節正常更新通過代替。

<a id="release"></a>

## 6. 發布更新

### 6.1 確認版本、來源與放行權限

- 只在開發機／與目標 Python minor、CPU 相符的封裝環境執行。現場僅保存唯讀 token，不授予發布權限。
- 依[專案版本規則](.gemini/skills/facial-recognition-release-rc/SKILL.md)選版、跑[測試](#acceptance)，更新 RELEASE_NOTES.md；取得核准後才 commit、建立 tag、push。RC 修正遞增 rc.N，驗收後晉升同目標正式版；不重用已發布 tag 或覆寫資產。
- 放行時 tag 必須指向已審閱並含部署清單的固定 commit，tracked 檔案無修改。不要在運行中的現場 app 切換 Git 來源。
- 以下 `vX.Y.Z-rc.N` 是必須替換的佔位值，不能原樣發布。v1.4.0-rc.3／v1.4.0 已發布，不可重跑建立；後續使用實際核准的新版本。

### 6.2 建立隨來源提交的部署清單

本版 updater 只查 Git tags，不要求 GitHub Release、附件或 flags。舊 v1.4.0 與故障 v1.4.1-rc.1 都沒有新契約，不會被新版 updater 當成可部署來源；已安裝的 v1.4.0 照常運行。

部署來源必須追蹤 `ota/deploy.json`：含格式、專案、Python minor／CPU、config／data schema、預設設定、requirements 摘要、全部模型與完整 wheels 的大小／SHA256／名稱／版本。版號與 build ID 從 tag 及解析後 commit 產生，讓相同已測來源可晉升正式 tag。模型／data schema 不相容仍需明確遷移。

在開發機用「已測完整 wheelhouse」產生清單；以下路徑是本輪既有材料，其他來源需自行換成實際已測路徑：

```bash
python3 -m ota.tag_source \
  --wheelhouse ota/dist/verify-v1.4.1-rc.1-l9lnyx3j/wheelhouse \
  --projects demosite --python 3.12 --architecture x86_64
python3 -m unittest ota.test_ota ota.test_setup ota.test_diagnostics ota.test_tag_source -q
bash -n install.sh
git diff --check
```

- 本輪沿用 77 個已測 wheels，包括 torch 2.2.2+cpu、torchvision 0.17.2+cpu；原始碼基底是正常 v1.4.0，並非故意啟動失敗的 RC worktree。
- format=1 的 requirements 僅接受 `套件==固定版本`、註解／空白與已鎖定的 `./other/*.whl`；遠端 URL、範圍、其他 pip 選項會在準備前拒絕。
- 清單產生不解析／升級依賴。requirements、模型或 wheels 改動後重新產生，並重做相應驗證；將清單與所有應用程式變更一起審閱、提交。沒有 commit／push 授權時停在本機待審狀態。
- `--projects demosite otherproject` 明列適用專案；全專案才使用 `--projects '*'`。
- 首次安裝或依賴變更時，下載缺少的鎖定 wheel；PyPI wheel 需檔名、大小、摘要吻合，CPU PyTorch 使用固定 CPU URL，私有 pvms wheel 隨來源提供。依賴有變不能保證只下載一兩個套件：已安裝不等於原 wheel 還在。
- 環境重用需要可核對的目標 wheel 材料（舊 release/wheelhouse 或已驗快取），不能只憑同名同版本及現場自行產生的 RECORD 判定相同；材料不足時另建環境並補下載。wheel 同名同版本但內容改變也會被辨識。
- `ota.package_release` 僅保留舊離線包／prepare-baseline 相容用途，不再作新 OTA 的發布入口。

### 6.3 RC tag 放行及正式晉升

只有取得本次 Git 操作授權後才建立、推送 tag。本節是流程說明，不是任意發布授權。

1. 依第 6.1 節確定目標版本與未使用的 RC 編號；審閱程式、部署清單與 RELEASE_NOTES，通過本機驗證後提交。
2. 核對欲推送的 `vX.Y.Z-rc.N` tag 指向該固定 commit，再依授權推送**該單一 tag**。lightweight／annotated tag 均可；不移動或重用已推送 tag。
3. 適用 test 主機查 tags 後自動安裝；production 按 tag 後綴略過 RC。不需要建立 Release 或上傳近 1 GB 包。
4. 核對 selected_build、latest_update、即時健康、真人辨識／進出方向／語音／建檔照同步。成功才算候選驗收；Git tag 推送成功不是現場成功證據。
5. 正式放行前，比對已測 RC 與正式目標的執行碼、模型、requirements、完整部署清單及遷移：必須相同；純版本說明可另外審閱。程式、模型或依賴有變就重做 RC。
6. 管理者核准後才推同目標的 `vX.Y.Z` 正式 tag；**推送正式 tag 本身就是 production 放行**，適用主機會自行更新。不要先推 tag 再等待 Release 發布。

只 push 一般 branch commit 不會部署。建立／修改 GitHub Release、draft 或 prerelease flags 不參與本版選版。固定 commit 透過 Git 物件驗證取得；紀錄中的 `sha256` 對 tag-only 作業表示部署清單摘要，不是 GitHub 外層 gzip 摘要。`build_id=tag:commit`、模型與 wheels 仍各有明確內容身分。

<a id="tag-only-migration"></a>

### 6.4 從舊 Release updater 遷移：先 test

應用程式 tag 不會自行替換獨立 poll 工具。先確認沒有 preparing／switching／validating／rolling_back 或未完成 setup；進行中的更新保留原作業。測試機先維護工具，正式機待 test 驗收後另行取得放行。

1. **開發端前置**：經授權將已測的改版 commit／push 至 `develop-v1.0.0`，提供新版工具 commit SHA。只推工具所在分支不會觸發 app OTA；RC／正式 tag 另依發布授權處理。尚未 commit／push 時，測試機 git pull 拿不到新版；不能將舊 v1.4.0 commit 當成新版工具。
2. 測試機以原桌面帳號確認基準、Git 分支與本機修改：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-status
   systemctl --user show facial_recognition.service -p MainPID -p NRestarts
   git status --short
   git branch --show-current
   ```

   本輪已回報基準為 v1.4.0、MainPID=68818、NRestarts=0、blocked=false；舊下載 failed 為終態，不需 retry。沒有進行中的 OTA／setup，且分支為 develop-v1.0.0、沒有待保留的本機修改時，才走下一步。detached HEAD、其他分支或本機修改先保留原狀，改用第 4.1 節固定 commit 只取工具流程，不強制 reset／checkout。
3. 開發端確認新版已推送後，備份並以 Git pull 取得工具：

   ```bash
   cd "$HOME/facial_recognition" &&
   FACIAL_OTA_TOOLS_BACKUP=$(mktemp -d "$HOME/facial-ota-tools.XXXXXX") &&
   cp -a ota install.sh "$FACIAL_OTA_TOOLS_BACKUP/" &&
   git pull --ff-only origin develop-v1.0.0 &&
   git cat-file -e HEAD:ota/tag_source.py &&
   git cat-file -e HEAD:ota/deploy.json &&
   git rev-parse HEAD
   ```

   最後 SHA 應與開發端提供的工具 commit 相符；不符或任一步報錯就停止。private config／UUID／venv 不需重填或複製。Git 只更新專案來源，運行中的獨立 agent 還需要下一步維護。
4. 維護並驗收測試機工具：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-setup &&
   bash install.sh --ota-status &&
   bash install.sh --ota-check --expect-role test --expect-version v1.4.0 --seconds 15
   systemctl --user show facial_recognition.service -p MainPID -p NRestarts
   ```

   成功判準：setup.phase=complete、discovery=tags，新一輪 last_poll.source=tags／query_ok=true，仍是 v1.4.0、PID=68818、NRestarts=0、blocked=false，15 秒驗收 result=pass。last_poll 尚未刷新時等下一輪再查，不重啟進行中的作業。任何錯誤保留完整輸出。
5. 工具維護會新增 operation=agent_upgrade 成功紀錄，不刪原失敗作業。測試機工具驗收後，接第 5.6 節新 RC tag 自動更新，再驗正式 tag；不是在現場以 git pull 手動替代每次 app OTA。正式機工具維護另依授權安排，角色保持 production。

手動工具包只作無法使用 Git 時的替代交付，不是正常流程的必要步驟。本次優先採 Git，不需先手動儲存工具包／摘要；也不為舊 poll 額外發布 GitHub Release。

### 6.5 GitHub Releases 已停用與清除

2026-10-30 依管理者指示，GitHub repo 的 4 個既有 Releases 及 8 個附件已刪除；刪除後重新查詢 Releases 為 0，8 個 Git tags 的名稱與物件 SHA 均保留不變。這不是刪 Git tags，也不刪現場 state_dir/releases、venv、wheelhouse、資料庫或回退紀錄。

- 後續不建立 GitHub Release、不上傳完整更新包；RC／正式版放行一律依第 6.3 節推送對應 tag，專案的 RELEASE_NOTES.md 仍保留版本說明。
- 兩台尚未遷移的舊 updater 只會查不到可用 Release，不能因此當作 tag-only 已接上；需先依第 6.4 節維護獨立工具。已安裝 v1.4.0 不因雲端 Release 刪除而被移除。
- 第 8 節舊 Release ID／附件摘要是歷史驗收證據；原 Release／附件下載連結已停用。保留 Git tag 不會使沒有 ota/deploy.json 的歷史版本自動符合新契約。
- 開發端清理證據在 `ota/dist/release-cleanup-2026-10-30/`：releases-before／after.json、tags-before／after.json、verification.json。保存發布資料與 tag 身分核對，不代表另外下載了所有遠端附件。

<a id="operations"></a>

## 7. 日常操作與故障處理

### 7.1 查版本、健康、待寄通知

<a id="ota-check"></a>

#### 優先使用：一鍵驗收

取得含 ota/diagnostics.py 的新版工具後，在原專案以服務帳號執行；不需啟用 venv、不需 sudo。已發布的舊套件未必含這個新指令，不能只複製新版 install.sh 而漏掉 ota/。

以下是本輪兩機已完成更新後的 15 秒驗收命令；後續請將 v1.4.0 換成實際目標。

正式機：

```bash
cd "$HOME/facial_recognition"
bash install.sh --ota-check --expect-role production --expect-version v1.4.0 --seconds 15
```

測試機（原專案工具已更新時）：

```bash
cd "$HOME/facial_recognition"
bash install.sh --ota-check --expect-role test --expect-version v1.4.0 --seconds 15
```

- 尚在 waiting_release 的正式機只用 `--ota-check --expect-role production`，不能指定尚未安裝的版本。不指定版本時核對目前選中套件，不表示它是 GitHub 最新發布版。
- 自動定位 agent 設定與 health port，唯讀查本機資料庫、setup 完成與最近成功紀錄、blocked、版本／build ID／PID、三服務 active／enabled、即時核心及 MQTT／同步健康，預設間隔 5 秒取兩次樣本，確認 boot ID／PID／重啟數穩定且必要計數增加。
- 觀察時間可用 --seconds 15（允許 1～30 秒）；白話就是「先檢查一次，等 15 秒再檢查一次」，確認兩次版本相同、程式未重啟、處理工作有進度，且核心／同步健康。並非每秒持續監測，也不取代接入／更新時的 60 秒穩定驗收。
- result=pass：上述自動檢查通過，仍須真人辨識、進出方向、語音與實際建檔照同步實測。
- result=warning：核心驗收通過，但觀察期間曾無相機新影像或有待寄通知。相機無影像不判 OTA 失敗、不回退；需排查設備／網路或待寄異常。
- result=waiting_release：原辨識程序及三服務穩定、OTA 已啟用，尚未安裝首個正式套件。此時只核對原服務路徑／PID／重啟數，不宣稱 Release 或功能健康驗收通過，不能加 --expect-version。看 last_poll.query_ok、checked_at、rc_skipped 確認真的查到並跳過 RC；warnings 會指出尚未查詢成功、首次正式版失敗或有待寄通知。
- result=fail：尚未接入、角色／版本不符、最近更新失敗、服務未啟動／未設開機啟用、核心／MQTT／計數異常等，不能宣稱安裝成功。按 reason、下方指令及日誌排查；更新正在進行或尚在啟動時，等完成再查。
- pass／warning／waiting_release 結束碼 0，fail 為 1；0 只表示該狀態檢查通過，不能一概當作正式套件健康。完整結果另看 result 與 warnings。檢查不重啟／更新／回退、不寄信、不修改 JSON 或 OTA 紀錄；不會印 token／SMTP 密碼。

<a id="installed-check"></a>

#### 原專案 install.sh 太舊：改用已安裝套件的驗收工具

若出現用法訊息、不支援 `--ota-check`，但已納管的 current 套件包含 `ota/diagnostics.py`，可直接使用下列唯讀替代指令，不必重裝、切換版本或先維護更新器：

```bash
cd "$HOME/.local/share/facial-ota/current" &&
python3 -m ota.diagnostics check \
  --project "$HOME/facial_recognition" \
  --expect-role test \
  --expect-version v1.4.0 \
  --seconds 15
```

正式機改為 `--expect-role production`。`--project` 必須是原共享設定所在目錄，不是 current 的版本目錄；以上使用預設路徑，自訂 state_dir／shared_dir 時換成實際位置。此工具只需 Python 3.10+，不需啟用 app venv。

current 不存在或不含 diagnostics 時，不可偽造 symlink 來通過驗收；改依[第 4.1 節](#ota-enrollment)更新已核准的 `ota/` 與 `install.sh`。一般 app OTA 不會自動替換原專案工具或獨立 updater；需維護 updater 本體時，再依第 4.3 節於安全時機執行 `--ota-setup`。

#### 原始指令與人工判讀（舊工具也可用）

完整狀態、即時健康及三個服務一次查詢：

```bash
cd "$HOME/facial_recognition"
bash install.sh --ota-status
curl --noproxy '*' -fsS http://127.0.0.1:8765/health | python3 -m json.tool
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
systemctl --user is-active facial_recognition.service facial-ota-poll.service facial-ota-notify.service
systemctl --user is-enabled facial_recognition.service facial-ota-poll.service facial-ota-notify.service
```

- is-active 應依序顯示三行 active，is-enabled 顯示三行 enabled；多個 unit 的整體退出碼不能代替逐項確認。NRestarts 不必為 0，但應保持穩定、不持續增加。[systemctl 文件](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
- setup.phase=complete、latest_update.phase=success，selected_build 與即時 health 的目標 version／build_id 相符、health.pid=MainPID、update_ready=true、blocked=false；這些都符合才確認 OTA 已接入及程序正常。
- 待命接入則為 setup.phase／enrollment_state=waiting_release、selected_build=null、blocked=false；不是安裝失敗，也不是某正式版本已驗收。剛啟用時 last_poll 可為 null，但服務啟動會先嘗試一輪；300／1200 秒是後續間隔，持續沒有結果要依第 7.5 節排查。
- last_poll.query_ok=true、checked_at 晚於 RC 發布、rc_skipped 包含該 RC，配合服務持續 active、原程序／設定不變，才能確認「有查詢並跳過 RC」；query_failed／query_ok=false 不算隔離測試通過。rc_skipped 最多顯示 20 筆，總數在 rc_skipped_count；日誌也會記錄本輪結果。
- latest_update 是歷史紀錄；還要查即時 health。pending=0 只代表沒有待寄通知，不能當作更新成功證據。
- `setup.target_version` 可能保留首次接入的 RC.2；日常更新成功後仍顯示舊值並非失敗。看 `selected_build`、`latest_update` 與即時 health 判斷目前版本。首次正式版納管則應看到 `enrollment_state=managed`、`setup.phase=complete`。
- `last_poll.action=enrollment_requested` 是最近派工紀錄；若 setup 已 complete、enrollment_state 已 managed、latest_update 已 success，表示首次安裝已完成，不是在等另一輪接入。`from_version=legacy` 表示原人工安裝來源沒有已納管 Release 身分，不偽造舊版號。
- `last_health` 在更新作業進行時可能暫停刷新；不能把準備前的健康快照當成更新後結果，也不能只因快照沒變就判定卡死。`preparing` 包含下載、解壓與離線安裝，依第 7.5 節確認進度。
- health 的 `boot_id` 是 app 啟動識別，不是作業系統開機 ID。更新切換後 PID／boot_id 改變正常；同一次穩定觀察期間應保持一致，計數則持續增加。
- 健康觀察需要兩次相隔數秒，確認 counters 增加、boot_id／PID 不變；可重跑 curl，或直接用新版 --ota-check。
- 預設 health port 為 8765；自訂設定時換成實際連接埠，一鍵驗收會自動讀取。所有指令都在原服務使用者登入後執行。

要進一步查即時健康與日誌時，以下採預設路徑：

```bash
cd "$HOME/.local/share/facial-ota/agent"
OTA_CFG="$HOME/.local/share/facial-ota/agent.json"
python3 -m ota.updater --config "$OTA_CFG" status
curl --noproxy '*' -sS http://127.0.0.1:8765/health | python3 -m json.tool
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
journalctl --user -u facial_recognition.service -u facial-ota-poll.service -u facial-ota-notify.service -n 100 --no-pager
```

- setup 顯示首次接入進度與錯誤；selected_build=null 可為未接入或已接入等待首個正式版，依 enrollment_state、setup.phase 與三服務區分。latest_update 顯示最近更新結果。selected_build 只是選中套件，不代表程序正常。
- 即時 health 的版本／build ID 符合目標、PID 符合 MainPID、update_ready=true、必要計數增加且 boot ID 穩定。
- 相機暫離線時可 update_ready=true、camera_ok=false、healthy=false；檢查設備／網路並待重連。
- pending 包含退避中待寄通知；last_health 是最近背景結果，以即時 health 為準。
- 查完整更新歷史／待寄錯誤，不讀 SMTP 密碼：

   ```bash
   python3 - <<'PY'
   import json, sqlite3
   from pathlib import Path
   with sqlite3.connect(Path.home() / ".local/share/facial-ota/agent.sqlite3") as db:
       for attempt, phase, data in db.execute("SELECT id,phase,data FROM attempts ORDER BY updated DESC LIMIT 20"):
           value = json.loads(data)
           print(attempt, phase, {k: value.get(k) for k in ("from_version", "target_version", "failed_at", "rollback", "rollback_failed_at", "error", "rollback_error")})
       for event, tries, due, error in db.execute("SELECT id,tries,due,error FROM outbox WHERE delivered=0"):
           print("待寄", event, "嘗試", tries, "下次時間", due, "錯誤", error)
   PY
   ```

### 7.2 暫停、重試與恢復

以下接續上節目錄與 OTA_CFG。先確認沒有正在準備、切換或驗收的作業；直接停止 poll 會中斷作業，下一輪可能將其記為中斷失敗。進度不明先用第 7.5 節唯讀排查，不以重啟、刪資料庫或清 config 強行重做。

- 暫停更新：systemctl --user stop facial-ota-poll.service；保持 notify 運行。
- 恢復：systemctl --user start facial-ota-poll.service。
- 手動更新：先停 poll，再執行 python3 -m ota.updater --config "$OTA_CFG" once；會實際更新，並非預覽。
- 重試失敗版本：python3 -m ota.updater --config "$OTA_CFG" retry --version v1.0.1；下次輪詢執行，不允許降版。
- blocked=true：先 AnyDesk 修復並驗收，再執行 python3 -m ota.updater --config "$OTA_CFG" resume。
- 手動補寄：停 notify，執行 python3 -m ota.updater --config "$OTA_CFG" notify-once，再啟動；退避中的通知等到期才寄。
- 改 SMTP／GitHub token 用第 3 節精靈，新版服務自動重讀；更新器本體或 agent 系統 Python 維護使用 --ota-setup。state_dir／shared_dir／health port 屬部署結構，變更需另行安排維護，不直接改成別台主機的值。不要用刪資料庫或重裝清待寄數。

| 結果 | 處理 |
| --- | --- |
| 新版成功 | 查程序與業務功能 |
| 準備失敗、舊版仍服務 | 修正套件／環境，發布修正版或人工 retry |
| 更新失敗、回退成功 | 確認舊版正常，保留紀錄 |
| 回退失敗／blocked | AnyDesk 修復，不強制繼續更新 |
| SMTP 待寄 | 查網路、TLS、帳密與收件地址，恢復後補送 |
| 相機離線、核心正常 | 查相機／網路，不因此回退 |

### 7.3 設定與備份

- GUI 齒輪／venv/bin/python ui/setting_tool.py 修改設定；目前 app 為 state_dir/current。
- LAN Web：http://主機IP:5000，僅限可信內網；GUI／Web 預設密碼 admin。
- OTA 驗收期間設定寫入被拒絕時，等待更新完成。
- 備份 config、共享資料、金鑰、私有 agent 設定與 state_dir SQLite／復原資料；資料庫用一致性備份或先停 poll／notify。
- 監看磁碟，保留待寄事件、歷史與回退版本，不手動清空 outbox。
- 辨識日誌在原專案 log/，報表在 reports/，排程日誌 log/reporter.log；長期保存資料先另行備份。

<a id="qt-display"></a>

### 7.4 Qt 無法連線桌面：免整機重開修復

適用 `qt.qpa.xcb: could not connect to display`。安裝模板固定 `DISPLAY=:0`、`XAUTHORITY=~/.Xauthority`，不一定符合目前桌面；找到 xcb plugin 卻無法連線，先查 display／授權，不直接重裝 PyQt 或放寬 X server 權限。

在服務帳號的實際桌面終端操作，不用 sudo。先查：

```bash
whoami
printf 'DISPLAY=%s\nXAUTHORITY=%s\n' "$DISPLAY" "${XAUTHORITY:-$HOME/.Xauthority}"
loginctl list-sessions
loginctl show-user "$USER" -p Sessions -p Display -p State
ps -C Xorg,Xwayland,Xvnc,gnome-shell,xfce4-session -o user,pid,args
ls -l /tmp/.X11-unix
```

- DISPLAY 空白只代表此終端沒有桌面變數，不代表沒有桌面。`loginctl` 的 Display 是 session ID，不是 `:N`；X11 socket `X1` 對應 `:1`，再以該使用者的 Xorg `-auth` 路徑確認授權檔。
- printf 的 `.Xauthority` 是未設定時的預設顯示值，不證明檔案存在或可用。不要張貼授權檔內容／cookie，也不要使用 `xhost +`。
- 本輪正式機實際值為 `:1`、`/run/user/1000/gdm/Xauthority`。下列是本輪修復示例，其他主機必須換成自己的值；沒有可用圖形 session 時，先安排圖形登入，不靠猜 DISPLAY 產生桌面。

```bash
export DISPLAY=:1
export XAUTHORITY=/run/user/1000/gdm/Xauthority
FACIAL_QT_PYTHON="$HOME/facial_recognition/venv/bin/python"
if [ -x "$HOME/.local/share/facial-ota/current/venv/bin/python" ]; then
  FACIAL_QT_PYTHON="$HOME/.local/share/facial-ota/current/venv/bin/python"
fi
env QT_QPA_PLATFORM=xcb "$FACIAL_QT_PYTHON" -c \
  'from PyQt5.QtWidgets import QApplication; app=QApplication([]); print("Qt display OK:", app.platformName())'
```

出現 `Qt display OK: xcb` 才繼續；此測試不啟動第二份辨識程式。失敗則保留輸出，另查授權或缺少的系統 library。

```bash
SYSTEMD_EDITOR=nano systemctl --user edit facial_recognition.service
```

在編輯區加入實際值，保留已有設定；下方 `#` 註解只是原 unit 參考內容。不要把 `$DISPLAY`／`$HOME` 當成 unit 中會展開的 shell 變數：

```ini
[Service]
Environment="DISPLAY=:1"
Environment="XAUTHORITY=/run/user/1000/gdm/Xauthority"
```

Nano 按 Ctrl+O、Enter 儲存，Ctrl+X 離開。只在終端 export 或 import-environment，仍會被原 unit 的固定 Environment 覆蓋；使用 drop-in，避免直接改會由程式重新生成的主 unit。[systemd 環境設定](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Environment=)

取得短暫中斷辨識的許可後，才只重啟此服務，不重啟桌面管理器或整台主機：

```bash
systemctl --user daemon-reload &&
systemctl --user reset-failed facial_recognition.service &&
systemctl --user restart facial_recognition.service
systemctl --user status facial_recognition.service --no-pager
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
journalctl --user -u facial_recognition.service --since "2 minutes ago" -n 80 --no-pager
```

確認畫面、真人辨識正常，重查 MainPID 穩定、NRestarts 不持續增加。若授權路徑含 `.mutter-Xwaylandauth` 等隨登入變動的名稱，這次值不能保證下次登入有效，須另處理 session 啟動整合，不要宣稱跨重開機驗收已通過。

<a id="ota-progress"></a>

### 7.5 一直 preparing／last_poll 沒更新：先查實際進度

- `preparing` 尚未代表失敗：可能正在下載、驗摘要、解壓、建立 venv、離線安裝或落盤；選中版本可能仍是舊版。各機獨立進行，不能用另一台已完成推斷這台失敗。
- 有未完成作業或 update.lock 被持有時，背景監看會跳過刷新，last_health 可能停在準備前。單看這份 JSON 不足以判斷卡死，也不能當成即時健康。
- 先在有問題的那台讀日誌與程序，不重啟進行中的 poll／setup。systemctl status 可看到 pip 等子程序；此版 pip 安裝 stdout 不寫入日誌，沒有安裝進度文字不代表沒運作。

```bash
date -Is
systemctl --user status facial-ota-poll.service facial-ota-notify.service --no-pager --full
journalctl --user -u facial-ota-poll.service -u facial-ota-setup.service --since "30 minutes ago" -n 100 --no-pager
df -h "$HOME/.local/share/facial-ota"
```

tag-only 作業依 `latest_update.target_version`／`attempt_id` 找目錄，不會產生 package.tar.gz。替換下列佔位值；自訂 state_dir 時同步修改路徑：

```bash
FACIAL_OTA_JOB="$HOME/.local/share/facial-ota/releases/<target_version>-<attempt_id>"
python3 - "$FACIAL_OTA_JOB" <<'PY_PROGRESS'
from pathlib import Path
import json
import sys
job = Path(sys.argv[1])
root = job.parent.parent
for name in ("source", "app", "app/venv"):
    path = job / name
    print(name, "exists=", path.exists(), "symlink=", path.is_symlink())
for path in sorted((root / "wheelhouse").glob("*/*.part")):
    print("wheel partial:", path.name, path.stat().st_size, "bytes")
prepared = job / "preparation.json"
if prepared.is_file():
    print("preparation:", json.loads(prepared.read_text()))
PY_PROGRESS
```

- source 表示固定 commit 正在取得／驗證；app/venv 是連結時通常沿用舊環境。preparation.json 記錄 `source_commit` 與 `environment=reused/created`，僅代表準備完成，不代表切換或健康驗收成功。
- 驗證既有環境時可能只有讀取、沒有下載或新檔案；wheel `.part` 不增長不能單獨判定卡死。快取 wheels 會保留供後續版本重用；必要時另安排磁碟維護，不在更新途中清除。

下面 package.tar.gz 範例僅適用**尚未遷移的舊 Release updater**；tag-only 不使用這個進度路徑：

```bash
FACIAL_OTA_JOB="$HOME/.local/share/facial-ota/releases/v1.4.0-99df4aad-4b0d-4f3e-960c-afd5b87d664e"
stat -c '%s bytes | %y | %n' "$FACIAL_OTA_JOB/package.tar.gz"
sleep 15
stat -c '%s bytes | %y | %n' "$FACIAL_OTA_JOB/package.tar.gz"
ls -ld "$FACIAL_OTA_JOB/app" "$FACIAL_OTA_JOB/app/venv"
```

- 檔案大小增加代表仍在下載；v1.4.0 完整 archive 為 943118689 bytes，其他版本以 release.json 的 size 為準。下載完整仍須驗摘要、解壓、安裝與驗收，不能直接記 success。
- app／venv 已出現時，配合子程序與磁碟判斷後續準備。此版單次 pip install 最長 30 分鐘；整體準備時間另含下載等階段。大小暫時不變仍需結合日誌與程序，不單憑一次取樣判定卡死。
- 若 poll 不運行、日誌出現錯誤、空間不足或持續無進展，保留狀態與輸出再排查；不要刪 config／資料庫清狀態。失敗版須排除原因後用第 7.2 節 retry，不能靠重啟假裝重試成功。
- `last_poll=null` 也可能是查 GitHub 前被服務健康、專案識別或接入作業擋住。若欄位根本缺少，可能原專案讀到舊工具；依第 7.1 節辨別。補 token 只能處理其適用的權限／限流問題，不保證解決 null。

<a id="ota-audit-logs"></a>

### 7.6 正式通知地點、故障紀錄與查詢位置

正式通知的地點欄位取自地點 API 的 name／快取；查不到名稱時用 location_ID=N 備援，無可用身分時可能為 unknown。主旨包含「專案／地點」，不會自動加演練字樣。第 8.3、8.4 節的一次性腳本只在該次通知事件的 location 加上演練標記，沒有寫回業務地點設定或身分快取；歷史演練信與事件仍保留該標記。

兩項實測共用正式程式的 update／rollback、持久佇列、mail_once／send_mail，但故障觸發方式經隔離：回退跳過 GitHub 選版／下載，改用本機已準備副本；補寄使用明確標示演練的測試事件，不另製造業務故障。因此通過範圍是第 8.3、8.4 節所列功能，不能宣稱完整正式部署與所有故障情境都已驗證。

以下為預設路徑；自訂部署以 agent.json 的 state_dir／shared_dir 為準：

| 紀錄 | 查詢位置與內容 |
| --- | --- |
| 服務與程序輸出 | journalctl --user：poll／notify／setup／辨識服務的啟停、程序輸出與未攔截錯誤 |
| 更新／回退稽核 | ~/.local/share/facial-ota/agent.sqlite3：attempts 保存各次更新的最近階段／結果，outbox 的 update 事件保存各次已記錄階段、發生時間及原因 |
| 通知／補寄狀態 | 同一資料庫 outbox／mail_recipients：通知內容、每位收件者接受狀態、累計失敗次數、目前錯誤與下次重試時間 |
| 首次接入狀態 | ~/.local/share/facial-ota/setup.json：最近接入階段與還原資訊，不是所有階段的逐筆歷史 |
| 辨識業務日誌 | ~/facial_recognition/log/faceLog 與輪轉檔；報表另見 reports/、log/reporter.log |
| 本次回退演練 | journalctl --user -u facial-ota-rollback-drill.service；~/.local/share/facial-ota/rollback-drill-akf_8cen/result.json 保存演練結果 |

日常先查服務日誌；此指令不重啟任何服務：

```bash
journalctl --user \
  -u facial-ota-poll.service \
  -u facial-ota-notify.service \
  -u facial-ota-setup.service \
  -u facial_recognition.service \
  --since today -n 200 --no-pager
```

查本次回退演練程序的額外輸出：

```bash
journalctl --user -u facial-ota-rollback-drill.service \
  --since "2026-10-27 17:20:00" --until "2026-10-27 17:25:00" --no-pager
```

更新階段主要記在資料庫，不保證每個階段都印進 journal。下列唯讀查詢列出最近階段與真正通知事件的寄送狀態，時間依執行主機當地時區顯示，不讀取 SMTP 帳密：

```bash
python3 - <<'PY'
from pathlib import Path
import json
import sqlite3

path = Path.home() / ".local/share/facial-ota/agent.sqlite3"
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
print("更新／回退階段：")
for row in db.execute("""
    SELECT seq, json_extract(body,'$.attempt_id') AS attempt_id,
           json_extract(body,'$.phase') AS phase,
           datetime(json_extract(body,'$.occurred_at'),'unixepoch','localtime') AS occurred_at,
           json_extract(body,'$.rollback') AS rollback,
           json_extract(body,'$.error') AS update_error
    FROM outbox WHERE json_extract(body,'$.kind')='update'
    ORDER BY seq DESC LIMIT 20
"""):
    print(json.dumps(dict(row), ensure_ascii=False))
print("通知／補寄狀態：")
for row in db.execute("""
    SELECT id AS event_id, delivered AS smtp_accepted,
           tries AS failed_tries, error AS latest_smtp_error
    FROM outbox
    WHERE json_extract(body,'$.kind')='alert'
       OR (json_extract(body,'$.kind')='update'
           AND json_extract(body,'$.phase') IN ('failed','rollback_failed'))
    ORDER BY seq DESC LIMIT 20
"""):
    print(json.dumps(dict(row), ensure_ascii=False))
db.close()
PY
```

- outbox 的 body.error 是事件的故障原因；outbox.error 是目前 SMTP 寄送錯誤。補寄成功會清空後者，但保留 tries 與通知本體，不能把空錯誤誤讀為從未寄送失敗。
- 目前未逐筆保存每一次 SMTP 失敗的完整歷程／traceback，mail_once 攔截的寄送錯誤也不保證出現在 journal；只能從累計次數、仍待寄時的目前錯誤及實際收件核對。
- 一般更新階段不寄信，雖然其 outbox.delivered 也會標為 1，但不代表寄出成功信；上方查詢已篩選真正通知事件。通知事件的 delivered=1 代表 SMTP 接受，仍須收件端確認實際寄達。
- SQLite 是磁碟稽核資料，不因服務重啟而清空；journal 是否跨重開機保留、保留容量與時間取決於主機設定，不能承諾永久保存，詳見 [journald 官方設定](https://www.freedesktop.org/software/systemd/man/255/journald.conf.html)。

<a id="acceptance"></a>

## 8. 驗收與測試方法

1. 開發機執行離線測試，不連真實 SMTP、不改本機 systemd：

   ```bash
   venv/bin/python -m unittest ota.test_ota ota.test_setup ota.test_diagnostics -v
   ```

2. 實機故障測試用隔離 repo／測試專案，不向正式 repo 發布故障套件。保存主機、前後版本／build ID、時間、回退及信件紀錄。
3. 依序驗證：

| 測試 | 操作 | 預期 |
| --- | --- | --- |
| 舊機設定 | 僅設定模式跑兩次 | config 不變、UUID 相同、私有設定 600 |
| 自動接入 | 原服務運行時執行 --ota-setup | 自動下載、備份、遷移、驗收、啟用，不手改服務 |
| 首次正式版待命 | production、只有 RC 或 GitHub 不可達時執行 --ota-setup | waiting_release、三服務啟用，原辨識／MQTT／config 不變，不冒充正式版、不因缺正式版寄失敗信 |
| 待命跳過 RC | 發布適用專案 RC，等輪詢後查 last_poll | query_ok=true、checked_at 晚於發布、rc_skipped 有該版；測試機更新，正式機原服務不變 |
| 首個正式版 | 待命時人工推送帶部署契約的正式 tag | 自動下載／備份／切換／驗收成 managed；失敗還原原服務、待命繼續並寄信，不無限重試壞版 |
| 多台共用設定 | 匯出 profile，在兩台不同專案匯入 | 無重複輸入帳密、UUID 不同，各自 config 保留 |
| 設定熱讀 | poll／notify 運行中修改 SMTP／token | 無需停機重啟，下一輪使用新值 |
| 中斷／重跑 | 接入中斷線、重啟，再重跑 | 同一工作復原，不重複切換；舊資料保留 |
| Agent 維護 | 接入完成後再跑新版 --ota-setup | 不停止辨識、不換 UUID，更新器工具更新 |
| 唯讀盤點／驗收 | --preflight；接入後 --ota-check --expect-role test | 不改 JSON／資料庫／服務；核對版本、PID、計數及三個服務，無相機影像只警告 |
| 重開機恢復 | 維護時段 sudo reboot，登入原使用者桌面再跑 --ota-check | 三服務 active／enabled、正確版本運行；再測影像、真人辨識、語音與建檔照 |
| SMTP | 精靈選寄測試信 | 管理員收到、密碼不顯示／入日誌 |
| 選版 | 發 RC，再發同目標正式版 | RC 僅測試機；正式版兩種角色都更新 |
| tag 契約／專案 | 無契約、移動 tag、舊版、不適用專案 | 不更新；移動／不合法契約記失敗 |
| 設定遷移 | 新欄位、更名，另測衝突 | 保留現場／未知欄位，衝突拒絕 |
| MQTT 補同步 | 維護停機時新增測試建檔照 | 啟動後不重發通知也補齊照片／特徵 |
| 準備失敗 | 摘要錯誤／下載中斷 | 舊版不中斷，失敗通知保存並寄送 |
| 相機暫離線 | 更新前／新版啟動時斷相機網路，寬限內恢復 | 更新可成功、不回退、不寄 OTA 失敗信 |
| 相機持續離線 | 離線超過寬限、多輪監看 | 一封相機異常信，恢復再斷才重新通知 |
| 核心故障 | 測試套件啟動／模型失敗 | 回退成功、最新共享資料保留 |
| 回退故障 | 隔離環境使新舊版驗收都失敗 | blocked，信含回退失敗當下時間及時區 |
| SMTP 中斷 | 產生失敗，寄信不可達，重啟後恢復 | 待寄／時間保留，恢復補送 |
| 多收件者 | 一人接受、一人拒收，修正後重試 | 已接受者不重寄，失敗者補送 |
| 中途終止／斷電 | 在準備、切換、驗收、回退中斷 | 重啟依紀錄復原，實際斷電另安排維護 |
| 主機獨立 | 不同專案，一台更新失敗 | 另一台獨立更新、不等待 |

- 相機恢復後仍需真人辨識、語音、GUI、影像與業務實測。
- 自動測試不能取代實機攝影機、MQTT broker、SMTP 供應商與斷電演練。


<a id="ota-field-acceptance"></a>

### 8.1 本輪已完成的正常更新實測流程

以下為 v1.4.0 本輪實測順序；技術指令見第 2.4、3.1、4.1、6、7.1、7.4 節。主管可直接閱讀[簡短工作紀錄](OTA_TEST_RECORD.md)。

- 建立基準：確認兩機角色、背景服務與辨識正常。正式機無已納管 Release 時先 waiting_release，不偽造正式版號。
- 發布候選版 RC.3：測試機由 RC.2 自動更新至 RC.3；正式機成功查詢，rc_skipped 包含 RC.3，原辨識 PID／重啟數保持穩定。
- 候選業務驗收：人員實測真人 IN／OUT 辨識、方向、語音、建檔照更新後的照片／特徵同步，確認正常。
- 正式機前置：Ubuntu 22.04 並裝 Python 3.12／venv，保留系統與原辨識 3.10；確認 x86_64、原程序不因並裝重啟，桌面連線設定正確。
- 人工正式放行：確認可短暫切換辨識後，以 RC.3 相同執行碼／模型／wheels 晉升並發布 v1.4.0；發布成功不是現場安裝成功證據。
- 核對安裝：測試機由 RC.3 自動更新至 v1.4.0；正式機從 legacy／waiting_release 首次安裝為 managed。兩機 latest_update.phase=success、目標版本與 build ID 相符、blocked=false。
- 即時驗收：兩機各以正確角色、目標 v1.4.0、15 秒觀察取得 result=pass；三服務 active／enabled，必要計數增加，核心、MQTT／同步與相機正常，pending=0、warnings=[]。
- 最後業務實測：操作者再次確認兩機正式版的真人辨識、方向、語音與建檔照同步正常；正常更新與角色隔離驗收完成。
- 本輪未做的現場故障情境：壞包、經 GitHub 發布故障版本的端到端回退、回退失敗、實際斷電／整機重開恢復。本機故障副本回退與 SMTP 中斷補寄已分別於第 8.3、8.4 節驗證；前述矩陣不代表每一項已通過。

<a id="ota-fault-drills"></a>

### 8.2 故障演練計畫：流程與成功判準

以下是演練計畫，各項實際完成結果另記於第 8.3、8.4 節。使用獨立測試 VM／測試主機、獨立測試 GitHub 儲存庫、測試資料與測試收件信箱；先備份並記錄正常版本、設定及照片／特徵。故障套件只發布至該測試儲存庫，避免現場設備收到；單改 state_dir 不能隔離同一使用者的 systemd 服務。正式機維持運行。

| 演練 | 怎麼測 | 判斷成功 |
| --- | --- | --- |
| 故障回退 | 從正常版本 A 更新至測試版本 B；B 套件通過下載、摘要與安裝檢查，但程式啟動後故意報錯，使新版健康驗收失敗，等待自動回退 | 運行版本恢復 A，latest_update.phase=failed、rollback=success、blocked=false；設定還原、最新共享照片／特徵保留，真人辨識／語音／同步正常，收到更新失敗與回退結果通知 |
| 郵件補寄 | 先只阻擋測試主機到測試 SMTP 的連線，再觸發一次上述更新失敗；確認 pending 大於 0，重啟測試機的 facial-ota-notify.service，確認待寄仍在；解除 SMTP 阻擋，等待自動重試 | 重啟後待寄未遺失，恢復連線後 pending=0，指定信箱實際收到該次故障通知，內容保留原故障時間與回退結果；單純寄測試信不能證明補寄功能 |
| 中斷／斷電恢復 | 在 VM 分別於 preparing、switching／validating 階段強制關機，再開機並登入原服務使用者桌面；另測正常更新成功後重開機。每一情境從乾淨快照重新開始 | 準備階段中斷時仍跑 A；切換／驗收階段中斷時依紀錄回退 A；已成功提交的更新重開機後維持 B。三服務恢復、無未結束更新、資料保留，真人辨識／語音／同步正常 |

- 回退情境的 phase=failed 代表「新版更新失敗」，rollback=success 才代表「回退成功」。第 7.1 節的 --ota-check 會因最近更新失敗回報 fail；此時須核對狀態、目前健康及業務實測，不能將該 fail 當成回退失敗。
- 壞摘要／下載失敗只測到「切換前拒絕更新」，不能替代「新版啟動失敗後回退」；不要破壞 A 或共享業務資料。若另測新舊版皆壞，預期 rollback_failed、blocked=true，須人工修復，不能算回退成功。
- SMTP 採延後重試，解除阻擋後不一定立即寄出；pending=0 表示郵件伺服器已接受，仍需到信箱核對收件。網路在伺服器接受信件後斷線時可能補寄重複信，不以絕不重複為驗收承諾。
- 上表回退判準適用已納管版本間更新；首次接入需另測。首次接入在 committed 階段中斷後會重新驗收新版，通過可完成接入，其餘未完成階段嘗試還原原服務；legacy 僅恢復程序時仍需人工業務驗證。
- VM 強制關機先驗證中斷恢復；一般重開機不能代替突然斷電。實體斷電另排專用測試設備與維護時段，完成備份後執行，並核對磁碟／資料與上述服務結果，才能記錄為實機斷電測試通過。

#### 僅能遠端連線時：保留連線的演練方式

先確認 AnyDesk／SSH 不依賴辨識程式，辨識程式停止時桌面及遠端服務仍能運作；不要停止網卡、桌面、遠端服務或整台主機。以下只操作測試環境，正式機不做故障注入；實體斷電先列待測。若無法確認遠端連線獨立，改用可由宿主機控制電源的測試 VM。

1. **回退＋補寄合併測**：先保存測試機原 SMTP 設定，將 OTA 的 SMTP host／port 暫改為 `127.0.0.1`／`1`（先確認該埠無服務），其餘保留；只讓寄信失敗，不修改防火牆或主機網路。新版每輪會重讀設定。
2. 在獨立測試儲存庫準備故障套件 B：套件本身可正常下載／安裝，只有 B 的啟動入口故意報錯；舊版 A 與共享設定／資料保持完好。目前正常的 v1.4.0 不能用來觸發這項故障，也不能只停止目前辨識程式冒充更新回退測試。
3. 讓測試機更新至 B，確認自動恢復 A，`latest_update.phase=failed`、`rollback=success`、`blocked=false`；GUI／影像及健康恢復。再確認 `pending>0`，只重啟 `facial-ota-notify.service`，待寄仍在；恢復原 SMTP 設定，確認信箱收到原故障通知及 `pending=0`。測試後還原測試儲存庫設定；不刪故障／回退歷史來取得 pass。
4. **程序中斷恢復另測**：從乾淨基準開始，在另一輪更新的 `preparing` 或 `validating` 階段，只對 `facial-ota-poll.service` 的程序發送 SIGKILL；先確認該 unit 的 `Restart=on-failure`、`RestartSec=15` 與預設整個服務群組終止行為。等更新器自動重啟後，依上表確認保留／回退 A、資料完整。不要把一般 restart 當成突發中斷，也不要將此結果記為斷電恢復通過。
5. **真正斷電另排**：需要現場人員協助開機，或獨立於作業系統的電源／管理介面；遠端智慧插座還需先確認 BIOS 支援且已設為復電自動開機。若目前沒有這些條件，無法保證重新連線，保留「實體斷電尚未演練」即可；軟體計時器無法在已斷電主機上開機。

若只想先獨立測補寄，可在無進行中更新時，先排定並確認辨識服務的自動恢復工作，再只停止測試機的辨識服務；保留 notify 運行，等待超過 `alert_grace_seconds`（預設 300 秒，另加監看間隔）產生真正異常通知。確認 pending 後恢復辨識及 SMTP，再核對收件；這能測異常通知補寄，仍不能替代更新失敗回退演練。

### 8.3 已完成：測試機本機故障副本回退

- 時間：2026-10-27 17:20:20～17:24:30（+08:00）；測試機為 gini-aiface-NUC11TNKi5，正式機未操作。
- 基準：v1.4.0、build ID 為 v1.4.0:87fd14ac369b385ad49c7bd4609ef9d86aa8c816、PID 58840，15 秒唯讀檢查 pass；設定與服務已備份。
- 準備：核對快取包 943118689 bytes、SHA256 為 6fa1db0dd62ae1adb918924eb6e5d846f085ce844625fd1329831b9f203fd559；另建安裝目錄與獨立 venv，只將副本 main.py 改成預期退出，原安裝包及原程式不改。
- 執行：以 systemd 使用者背景服務呼叫原更新器 update／rollback 流程，僅 stage 改用已準備副本；實際經 stopping、switching、validating，健康逾時後 rolling_back。這是同版故障注入，未發布新 Release，from／target 均為 v1.4.0。
- 結果：attempt_id 為 ae6ce2c1-693a-44db-9d2c-9cc4a915e379，drill_result=pass、phase=failed、rollback=success；原路徑／程式與設定核對通過，恢復 PID 68818，update_ready=true、camera_ok=true、blocked=false。
- 通知：使用者確認回退通知實際寄達；該事件 delivered=true、tries=0、error 為空，待寄數為 0，屬正常 SMTP 收件確認，尚不代表中斷補寄通過。
- 限制：未驗 GitHub 新版本選版／故障包下載或回退失敗，未另驗回退後真人辨識／語音／資料同步。演練保留失敗紀錄，因此 --ota-check 會因最近作業 failed 而失敗，不刪紀錄偽造 pass。
- 後續：SMTP 中斷補寄已於第 8.4 節完成；使用者指定暫不做程序中斷或斷電恢復。

<a id="ota-mail-retry-acceptance"></a>

### 8.4 已完成：測試機 SMTP 中斷、重啟保留與補寄

- 通知 ID：58f909d3-6cb6-4197-b6f3-dc250a96de14；主旨含「郵件補寄演練 58f909d3」。本輪使用明確標示為演練的測試通知，經原 Store 待寄佇列與 mail_once 寄送，不另製造辨識故障或偽造更新結果。
- 流程：保存當下 SMTP 設定，確認本機 port 1 拒絕連線後，暫改測試機 OTA 的 SMTP host／port 為 127.0.0.1／1；寄送失敗後只重啟 facial-ota-notify.service，再核對待寄紀錄，最後自動還原原 SMTP。
- 中斷結果：ConnectionRefusedError、失敗重試次數 1；寄信服務重啟後待寄數仍為 1，辨識 PID 維持 68818。
- 恢復結果：同一通知 delivered=true、tries=1、error 為空，總待寄數 0；當前 SMTP 與演練前保存值相同，且使用者確認實際收件，補寄驗收通過。
- 範圍：驗證 SMTP 連線不可達時的持久佇列、寄信服務重啟保留及恢復後背景補寄；未操作主機網路、桌面或正式機，未測實體斷電、多收件者部分拒收或郵件伺服器接受後斷線等其他情境。

同一測試機可用下列唯讀指令重查本次結果；mail-drill.json 是該機演練備份檔，含原 SMTP 帳密，指令只印收件狀態與設定是否相符，不列出帳密：

```bash
python3 - <<'PY'
from pathlib import Path
import json
import sqlite3

root = Path.home() / ".local/share/facial-ota"
saved = json.loads((Path.home() / "ota-rollback-backup.QjRkYS/mail-drill.json").read_text())
current = json.loads((root / "agent.json").read_text())
db = sqlite3.connect((root / "agent.sqlite3").as_uri() + "?mode=ro", uri=True)
row = db.execute(
    "SELECT delivered,tries,error FROM outbox WHERE id=?",
    (saved["event_id"],)).fetchone()
print("補寄結果:", {"SMTP已接受": bool(row[0]),
                    "失敗重試次數": row[1], "錯誤": row[2]})
print("總待寄數:", db.execute(
    "SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0])
print("原 SMTP 已恢復:", current["smtp"] == saved["smtp"])
db.close()
PY
```

### 8.5 舊 Release 流程故障驗收（歷史；兩次下載失敗，未進入回退）

依使用者要求，本輪取消本機 stage 替換與人工測試通知：使用原 GitHub 儲存庫、正式封裝工具與未改寫的常駐 OTA 服務，已發布符合選版規則的故障 RC。角色維持 test／production；測試機接收 RC、正式機略過 RC，這項選版規則仍有差異，不能稱為正式機已執行相同故障驗收。

1. 發布前核對現場基準、備份、目前遠端 tag 與下一個 RC 版號，以及相同專案下會收到 RC 的 test 主機；現有 manifest 依專案篩選，沒有單一裝置白名單。
2. 第一個故障 RC：故障包含在新套件的應用程式啟動碼，套件摘要、離線依賴與安裝準備須正常；不修改舊版或 OTA 更新器。由現場自行查詢 Release、下載、驗摘要、安裝、切換、健康驗收失敗並回退，確認實際失敗通知寄達。
3. 第二個故障 RC：暫時讓測試機 SMTP 不可達，再由另一個真實故障版本觸發更新失敗通知；核對待寄、重啟寄信服務後仍保留，還原 SMTP 後確認同一事件補寄成功。通知由原失敗處理自動產生，不手動呼叫 _enqueue、不更動事件地點，更新紀錄保留。
4. 發布正常 RC 收尾：移除應用程式故障，沿用已驗證依賴；由現場自動更新，正常成功紀錄與即時健康、真人辨識／語音／同步驗收通過後，才考慮正常版本晉升正式版。故障 RC 不得晉升正式版。

本輪郵件地點欄位使用正常 API 地點名稱／備援值，不加演練後綴；故障 Release 的說明與測試工作紀錄須清楚標示用途。證據包含 tag／來源 commit、兩項資產摘要、現場 attempt_id／階段／時間／回退結果、通知 event_id／重試次數與實際收件，不能只以 GitHub 發布成功判定現場驗收成功。

本次已獲核准且確認僅一台 test 主機，發布紀錄如下；第 3、4 步的後續版本尚未發布，程序中斷與斷電恢復仍依使用者指示暫不執行。

| 項目 | 已核對資料 |
| --- | --- |
| Release | [v1.4.1-rc.1](https://github.com/yangtandev/facial_recognition/releases/tag/v1.4.1-rc.1)，ID 427585902，published_at=2026-10-29T08:22:16Z |
| 來源 | da64d6cd401098adb4444cd08003696c603d75be，test(ota): exercise release rollback；tag 與發布說明一致，正常開發分支仍為 v1.4.0 來源 |
| 狀態 | draft=false、prerelease=true、Latest 正式版仍為 v1.4.0；production 選版會略過此 RC |
| Archive | facial-recognition-v1.4.1-rc.1.tar.gz，943117577 bytes；SHA256=6745c8321d855d3f1fd91964bcc5808f97cb73de075466116a586ebad034d42e |
| Manifest | release.json，531 bytes；SHA256=48d1befa4940c118cc3a5134edb3a8ab5c113173ac245d0457373805838b88c1；下載回本機逐位元組比對一致 |
| 封裝驗證 | 原始碼及實際包各通過 183 項測試；全新 venv 離線安裝、pip check、CPU NMS 與核心依賴匯入通過，新版入口依預期失敗 |
| 差異 | 與 v1.4.0 實際包相比，只變 main.py、BUILD_INFO.json、RELEASE_NOTES.md；77 個 wheels、模型、更新器與郵件格式均相同 |
| 現場結果 | 第一次 a4ec9d4a… 與重試 6e65daa0… 均完整性校驗失敗、rollback=not_needed；未進入切換／回退。重試只收到 671088640／943117577 bytes。2026-10-30 回報仍選 v1.4.0、blocked=false、pending=0，健康快照全綠，失敗郵件實際收到；不算故障啟動回退通過 |

在測試機查進度，讓原輪詢流程自行執行；preparing 期間不要重啟更新服務，下載與離線安裝時間另計：

```bash
cd "$HOME/facial_recognition"
bash install.sh --ota-status
journalctl --user -u facial-ota-poll.service -u facial_recognition.service \
  --since "30 minutes ago" -n 80 --no-pager
```

上述第 3、4 步為改版前計畫；後續改按 tag-only 契約安排，不直接再發 Release。

本次回退成功判準：latest_update.target_version 必須為 v1.4.1-rc.1、phase=failed、rollback=success、blocked=false，選中版本恢復 v1.4.0 且即時核心健康正常，並實際收到該次真實更新失敗通知；不能把第 8.3 節 target_version=v1.4.0 的舊演練紀錄當成本次結果。

## 9. 已簡化與仍需人工處理的項目

| 流程 | 現在做法 |
| --- | --- |
| 各台手動找版本、下載套件、填路徑 | --ota-setup 自動完成 |
| 等正式 tag 才逐台接入 | 正式機先 waiting_release；正式版放行後自行首次安裝 |
| 逐台抄 OS／Python／GPU、判讀多個驗收指令 | --preflight 盤點；--ota-check 核對服務、版本、PID、健康與進度 |
| 備份 config、編輯 systemd override、建立／啟用服務 | 首次接入程式自動完成，保留可復原備份 |
| 主機 UUID、家目錄、專案路徑、GitHub repo 預填 | 自動取得；原設定優先保留 |
| 多台重複輸入 SMTP／GitHub token | 一次匯出私有 profile，各台匯入並各自產生 UUID |
| 改 SMTP／token 後逐台 stop／restart | 新版服務每輪重讀設定 |
| 新版 app 更新、失敗回退、通知補送 | 現場背景服務自動完成 |
| 不同 Python minor | 已安裝的相符系統 Python 可自動選用；未安裝或 CPU 不符時保留原服務，不自行改 OS |
| 發布側手填 CPU／Python／輸出路徑 | 預設從封裝環境及版本自動取得 |

- 仍需一次性提供新版工具與主機權限；只有 AnyDesk、尚無 updater 的舊主機，無法由開發機憑空遠端安裝。
- API／地點／相機等現場參數、憑證與角色需由管理者提供；不從另一台複製業務 config。
- RC 人工業務驗收、正式 tag 人工放行保留；正常啟動檢查不能代替所有業務測試。
- wheelhouse 要與現場相容並經 RC 實測；正式版沿用已測依賴，不重新解析套件版本。
- updater 本體維護使用新版 --ota-setup；此版不讓正在執行的 updater 在 app 更新途中替換自己。
- 每台首次完成接入後，日常 app 發布只在開發端做一次，現場各機自行更新；不需逐台 pull、填路徑或重啟服務。
