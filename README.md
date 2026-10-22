# 人臉辨識系統操作手冊

- [開發機](#development-host)：開發、測試、封裝及發布。
- [測試機／正式機](#field-host)：首次安裝與既有主機設定。
- [一鍵環境檢查](#host-preflight)、[一鍵 OTA 驗收](#ota-check)：免逐台手動判讀多個指令。
- [SMTP 設定](#smtp)：自動寫入主機 ID、角色與通知設定。
- [首次接入 OTA](#ota-enrollment)：一次指令自動準備、驗收及啟用。
- [多台主機共用設定](#fleet-profile)：匯出私有部署設定，省去逐台輸入帳密。
- [運作範例](#runtime-flow)、[發布更新](#release)、[故障處理](#operations)、[驗收方法](#acceptance)。

## 1. 準備與主機角色

- 現場主機：Ubuntu、桌面顯示環境、Python 3.10+、攝影機、業務 API／照片來源及 MQTT。
- 主機主動對外連 GitHub HTTPS 與 SMTP；可透過 AnyDesk 首次設定，不需要 inbound SSH 或中介機。
- 各主機直接查詢 GitHub Release，並透過 SMTP 寄送失敗通知。
- Release 的 Python minor／CPU 架構必須與主機相符；Python 本體及系統套件另行維護。
- 每台保留自己的 config、照片、特徵、金鑰與資料庫；勿複製另一台的主機 ID 或 OTA 狀態。
- GitHub token、SMTP 密碼與本機 JSON 不加入 Git、不放入 Release。

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
5. 依[第 4 節](#ota-enrollment)執行一次接入指令；程式自動準備基準版本（baseline）、驗收及啟用服務。
6. 測試機自動選適用 RC／正式版，正式機只選人工放行的正式版；兩者都須在本機通過啟動檢查。
7. 測試機通過 RC 人工實測後，由開發人員發布正式版；測試機維持 test，也會更新成正式版。
8. 各機獨立查詢、更新與寄信，不互相等待。

<a id="first-install"></a>

## 2. 新主機初次部署

<a id="host-preflight"></a>

### 2.1 下載工具與環境檢查（尚不安裝辨識服務）

1. 在新主機的桌面終端，以日後運行服務的使用者操作；不要用 root 或 sudo bash install.sh。新部署建議 Ubuntu 24.04、Python 3.12、x86_64；仍須以實際 Release 的需求為準。
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
5. 這裡取得開發分支只為使用工具，尚未部署 app；正式機不要直接執行完整 install.sh 安裝此分支。尚無通過實測、涵蓋該專案的正式 Release 時，先停在此處，不把正式機改成 test 來接收 RC。

### 2.2 安裝已放行版本

1. 管理者先確認 GitHub Release 已發布、附完整 .tar.gz／release.json、適用該專案，且 Python minor／架構相符。正式機僅使用正式版，測試機才使用 RC。以下 v1.4.0 是操作範例，不表示它已經發布；請換成實際已放行版本。
2. 全新主機、尚無 config 時才切換指定 tag 並安裝；出現本機修改衝突就停止，不強制覆蓋：

   ```bash
   cd "$HOME/facial_recognition"
   FACIAL_INSTALL_VERSION=v1.4.0
   git fetch origin tag "$FACIAL_INSTALL_VERSION" &&
   git switch --detach "$FACIAL_INSTALL_VERSION" &&
   bash install.sh --preflight &&
   bash install.sh
   ```

3. 精靈初期：正式機填 production，測試機填 test；repo 填 yangtandev/facial_recognition，輸入 GitHub 唯讀 token、SMTP 與管理員信箱，測試信選 y。主機 UUID 與相關 JSON 自動寫入，不需抄寫 device_id／hash。
4. 輸入這台自己的伺服器、照片路徑、API、location ID 與攝影機 RTSP；不可複製另一地點的 config。其餘系統套件、模型、venv、JSON、SSH 金鑰與服務由 install.sh 處理。現有完整安裝仍從 requirements.txt 解析相依，可能下載 CUDA 依賴；不等於已安裝 Release 的 CPU 離線環境，最後仍須接入 OTA 並驗收。
5. 安裝完成後，在允許中斷服務的時段執行；重開後重新登入原使用者桌面／AnyDesk：

   ```bash
   sudo reboot
   ```

6. 登入後確認辨識畫面、真人辨識、語音、相機、MQTT 與建檔照同步，再查原服務：

   ```bash
   cd "$HOME/facial_recognition"
   systemctl --user status facial_recognition.service --no-pager
   journalctl --user -u facial_recognition.service -n 100 --no-pager
   ```

7. 接入 OTA，沿用已填的設定；自動下載適用 Release、準備隔離環境、備份、切換、穩定驗收及啟用背景服務。正常情況不用自己下載套件／填 baseline 路徑／手動 enable：

   ```bash
   bash install.sh --ota-setup
   bash install.sh --ota-check --expect-role production
   ```

8. 測試機將 --expect-role production 改為 --expect-role test；需確認某個版本時，加 --expect-version v1.4.0（改成實際目標）。寫入 SMTP 或 git pull 完成，都不代表 OTA 已安裝成功。
9. 接入後再安排一次重開機／桌面登入，重跑同一條 --ota-check，並確認辨識與照片同步。日常新版發布後主機自行更新，不再逐台 pull 或重跑完整安裝。

- 已有 config.json 時，完整安裝會拒絕執行，避免覆蓋現場設定；改用下節。
- 不用 setting/build_config.py 遷移舊設定，避免遺失未知欄位。

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
- shared_dir、state_dir 與系統 Python 預設自動取得；非標準部署再核對路徑。agent 使用 app／state 以外的系統 Python 3.10+，程式環境另依 Release 所需 minor 建立；主機有安裝相符 minor 時會自動選用，沒有就保留原服務並通知。
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
2. 將本版 ota/ 與 install.sh 提供至原專案；不用覆寫正在運行的 main.py、config 或 venv。
3. 開發端須先發布適用專案、附完整更新包與 release.json 的 GitHub Release。只有 tag／source archive 不足以接入；正式機沒有適用正式 Release 時不會改裝 RC。
4. 已完成 SMTP 設定者只需執行：

   ```bash
   cd "$HOME/facial_recognition"
   bash install.sh --ota-setup
   ```

5. 尚未設定者會先進入精靈；已設定者直接沿用，不重填帳密。多台主機可用上節 profile。
6. 畫面顯示 complete／「OTA 已啟用」後，確認影像、真人辨識、語音與照片同步。後續適用版本由主機自行更新。
7. 用[第 7.1 節](#ota-check)的一鍵驗收確認程序與三個服務；例如測試機執行 bash install.sh --ota-check --expect-role test。

### 4.2 程式自動完成的步驟

1. 依角色、專案與 SemVer 選 Release，下載完整更新包、驗證摘要及 Python／CPU 相容性。
2. 另建程式環境；準備階段舊版繼續運作。只選主機已安裝且相符的系統 Python，不自動升級系統套件。
3. 備份原服務設定與現場 config，保留原程式及環境；新欄位補預設、更名按規則遷移，衝突或模型不相容就拒絕切換。
4. 短暫停止辨識／MQTT、切換新版，自動檢查版本／build ID／PID、模型推論、人臉索引、GUI、取像／偵測／比對執行緒，以及 MQTT 連線、訂閱和建檔照同步。
5. 通過啟動與穩定觀察後，登記第一個基準版本（baseline），安裝並啟用 poll／notify。baseline 是日後回退的起點，不是另一種版本名稱。
6. 攝影機暫離線不單獨觸發回退；update_ready=true 代表必要啟動檢查通過，healthy=true 還要求啟用相機有新影像。恢復後仍需影像及業務實測。

### 4.3 斷線、失敗與重跑

- 執行工作交給獨立的 facial-ota-setup.service；關閉終端或 AnyDesk 不會中止背景接入。主機重開後，在使用者服務啟動時依 checkpoint 復原。
- 準備失敗不停止舊服務；切換／安裝失敗自動還原原設定與服務。照片、特徵與最新業務資料保留。
- 原版沒有健康檢查時，恢復後只能確認原程序持續運行，回退結果記為 restored_unverified；不宣稱業務已全面正常，收到信後需確認辨識。
- 恢復也失敗會記 rollback_failed／阻止後續更新，通知含失敗時間與時區；由 AnyDesk 人工修復。
- 備份在 state_dir/enrollment/作業UUID，包含 site-config.json 與 original-service.txt；替換的工具／服務檔會移入備份目錄，不直接刪除。
- SMTP 不通就先保存在本機，setup 背景工作持續補寄；完成後停用該一次性工作，一般監看由 notify 負責。
- 同一接入仍在進行時重跑命令，只顯示同一工作，不重複切換；中斷的切換會先恢復原服務，不直接重做新版本。
- 已完成 OTA 後，使用新版工具重跑 --ota-setup 會備份並更新獨立 agent、恢復 poll／notify；不重裝 app、不停止辨識、不更換 UUID。這一步用於 updater 本體維護，一般 app 更新不用跑。
- 初次接入失敗後明確重跑 --ota-setup 可再試；一般自動更新仍遵守失敗版本不無限重試的規則。
- 半套手動安裝或不明的既有服務檔不會直接覆寫；先查狀態及備份，避免把別人的設定當成自動安裝產物。
- 查結果與日誌：

   ```bash
   bash install.sh --ota-status
   journalctl --user -u facial-ota-setup.service -n 100 --no-pager
   ```

<a id="runtime-flow"></a>

## 5. 實際運作流程與範例

### 5.1 RC 到正式版

假設測試機 T、正式機 A／B 都在 v1.0.0，套件適用各機專案：

| 發布動作 | 測試機 T | 正式機 A／B |
| --- | --- | --- |
| 只 push commit／tag，或 Release 為 draft | 不更新 | 不更新 |
| 發布 v1.0.1-rc.1，勾選 pre-release | 更新 RC，人工實測 | 不更新 |
| 修正並發布 v1.0.1-rc.2 | 更新 RC.2，再測 | 不更新 |
| 人工通過，發布 v1.0.1 正式 Release | 更新正式版 | 各自查到後更新 |

- 完整正式 Release 發布就是人工放行點。
- 測試機已測 v1.0.2-rc.1 時，不因 v1.0.1 發布而降版。
- 不適用專案的 Release 跳過；主機不互相等待。
- 人工發現功能問題但健康驗收已通過時，不會因人工回報而自動回退；修正後重走 RC。

### 5.2 查到新版後

1. 依角色、SemVer、專案與失敗紀錄篩選 GitHub Release。
2. 確認舊版核心及 MQTT／同步正常；新版健康訊號排除僅攝影機無影像的情況。
3. 下載、驗證摘要、準備隔離環境、檢查模型與設定遷移；舊版繼續服務。
4. 備份 config，停止辨識與 MQTT，切換並啟動新版。
5. 核對版本／build ID／PID，驗證推論、索引、GUI、取像／偵測／比對執行緒與 MQTT／同步。
6. 預設啟動等待 180 秒、穩定觀察 60 秒；通過才記錄成功。
7. 新版啟動／驗收失敗就恢復舊程式、環境及 config，再驗收；準備失敗時不停止舊版。
8. 結果先保存本機；失敗由獨立 notify 服務寄信，成功不寄。

- 更新期間可漏收 MQTT；啟動／重連後主動補同步建檔照與特徵。
- 保留現場 API、地點、相機、MQTT、未知欄位；新增欄位補預設，更名依明確規則，有衝突拒絕更新。
- 回退不還原共享照片、最新特徵與業務資料。

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
- 間隔與寬限改 state_dir/agent.json，再重啟 poll／notify。

<a id="release"></a>

## 6. 發布更新

執行位置：開發機／與現場 Python minor、CPU 相同的封裝環境。

1. 依[專案版本規則](.gemini/skills/facial-recognition-release-rc/SKILL.md)選版，先跑[測試](#acceptance)。
2. config 有變動時驗證預設與 setting/ota_migrations.json 更名規則，不覆蓋現場值。
3. 提交發布內容及 RELEASE_NOTES.md，確認 tag、來源 commit、版本一致；必要檔案納入 Git、tracked 工作目錄乾淨。
4. 取得模型，以乾淨 wheelhouse 準備每套件唯一、已測的 wheel，封裝新目錄：

   ```bash
   git lfs pull
   venv/bin/python -m pip download --only-binary=:all: -r requirements.txt -d ota/wheelhouse
   venv/bin/python -m ota.package_release --version v1.4.0-rc.2 --wheelhouse ota/wheelhouse --projects demosite
   ```

5. 版本依現有 tag 選擇，不覆寫已發布 RC；上例 v1.4.0-rc.2 只是示例。Python minor、CPU 自動取封裝環境，輸出預設 ota/dist/版號；需與現場相符。跨環境才明確指定 --target-python／--architecture／--output。--projects demosite otherproject 指定多專案，共用版才明確用 --projects '*'。
6. 建立相符 tag 的 draft Release，上傳 .tar.gz 與 release.json，核對完整才發布；RC 勾 pre-release。
7. 測試機完成人工業務／故障驗收；有問題遞增 rc.N，不覆寫既有 tag／資產。
8. 通過並人工放行後，保留已測程式、模型、依賴與遷移規則，重新封裝同目標正式版，如 v1.0.1-rc.2 → v1.0.1，核對最終套件並發布，不勾 pre-release。
9. 各機自行更新、失敗寄信；要確認某台成功，依下節查實際運行狀態。

- 單純 push tag 不觸發 OTA，必須發布含完整資產的 Release。[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#list-releases)
- 使用封裝工具完整套件，不用 GitHub 自動 source archive。
- embedding 模型、資料 schema、不相容 config 變動先完成遷移設計與驗收。

<a id="operations"></a>

## 7. 日常操作與故障處理

### 7.1 查版本、健康、待寄通知

<a id="ota-check"></a>

#### 優先使用：一鍵驗收

取得含 ota/diagnostics.py 的新版工具後，在原專案以服務帳號執行；不需啟用 venv、不需 sudo。已發布的舊套件未必含這個新指令，不能只複製新版 install.sh 而漏掉 ota/。

```bash
cd "$HOME/facial_recognition"
bash install.sh --ota-check --expect-role test
```

- 正式機改用 --expect-role production；需要鎖定目標可加 --expect-version v1.4.0-rc.2（測試機範例，正式機請用正式版號）。不指定版本時核對目前選中套件，不表示它是 GitHub 最新發布版。
- 自動定位 agent 設定與 health port，唯讀查本機資料庫、setup 完成與最近成功紀錄、blocked、版本／build ID／PID、三服務 active／enabled、即時核心及 MQTT／同步健康，預設間隔 5 秒取兩次樣本，確認 boot ID／PID／重啟數穩定且必要計數增加。
- 觀察時間可用 --seconds 15（允許 1～30 秒）；這是短時複查，不取代接入／更新時的 60 秒穩定驗收。
- result=pass：上述自動檢查通過，仍須真人辨識、進出方向、語音與實際建檔照同步實測。
- result=warning：核心驗收通過，但觀察期間曾無相機新影像或有待寄通知。相機無影像不判 OTA 失敗、不回退；需排查設備／網路或待寄異常。
- result=fail：尚未接入、角色／版本不符、最近更新失敗、服務未啟動／未設開機啟用、核心／MQTT／計數異常等，不能宣稱安裝成功。按 reason、下方指令及日誌排查；更新正在進行或尚在啟動時，等完成再查。
- pass／warning 結束碼 0，fail 為 1；自動化可判斷結束碼，完整健康另看 result 與 warnings。檢查不重啟／更新／回退、不寄信、不修改 JSON 或 OTA 紀錄；不會印 token／SMTP 密碼。

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
- latest_update 是歷史紀錄；還要查即時 health。pending=0 只代表沒有待寄通知，不能當作更新成功證據。
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

- setup 顯示首次接入進度與錯誤，尚未接入時 selected_build 可為 null；latest_update 顯示最近更新結果、回退及時間。selected_build 只是選中套件，不代表程序正常。
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

以下接續上節目錄與 OTA_CFG。

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
| 多台共用設定 | 匯出 profile，在兩台不同專案匯入 | 無重複輸入帳密、UUID 不同，各自 config 保留 |
| 設定熱讀 | poll／notify 運行中修改 SMTP／token | 無需停機重啟，下一輪使用新值 |
| 中斷／重跑 | 接入中斷線、重啟，再重跑 | 同一工作復原，不重複切換；舊資料保留 |
| Agent 維護 | 接入完成後再跑新版 --ota-setup | 不停止辨識、不換 UUID，更新器工具更新 |
| 唯讀盤點／驗收 | --preflight；接入後 --ota-check --expect-role test | 不改 JSON／資料庫／服務；核對版本、PID、計數及三個服務，無相機影像只警告 |
| 重開機恢復 | 維護時段 sudo reboot，登入原使用者桌面再跑 --ota-check | 三服務 active／enabled、正確版本運行；再測影像、真人辨識、語音與建檔照 |
| SMTP | 精靈選寄測試信 | 管理員收到、密碼不顯示／入日誌 |
| 選版 | 發 RC，再發同目標正式版 | RC 僅測試機；正式版兩種角色都更新 |
| 草稿／專案 | 發 draft、舊版、不適用專案 | 不更新 |
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


## 9. 已簡化與仍需人工處理的項目

| 流程 | 現在做法 |
| --- | --- |
| 各台手動找版本、下載套件、填路徑 | --ota-setup 自動完成 |
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
- RC 人工業務驗收、正式 Release 人工放行保留；正常啟動檢查不能代替所有業務測試。
- wheelhouse 要與現場相容並經 RC 實測；正式版沿用已測依賴，不重新解析套件版本。
- updater 本體維護使用新版 --ota-setup；此版不讓正在執行的 updater 在 app 更新途中替換自己。
- 每台首次完成接入後，日常 app 發布只在開發端做一次，現場各機自行更新；不需逐台 pull、填路徑或重啟服務。
