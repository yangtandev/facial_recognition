# 人臉辨識系統操作手冊

- [開發機](#development-host)：開發、測試、封裝及發布。
- [測試機／正式機](#field-host)：首次安裝與既有主機設定。
- [SMTP 設定](#smtp)：自動寫入主機 ID、角色與通知設定。
- [首次接入 OTA](#ota-enrollment)：建立可驗證、可回退的基準版本。
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
5. 依[第 4 節](#ota-enrollment)人工準備並驗收健康版 baseline，再安裝及啟用 agent。
6. 測試機 baseline 可用健康版 RC／正式版；正式機僅用已驗收、人工放行的正式版。
7. 測試機通過 RC 人工實測後，由開發人員發布正式版；測試機維持 test，也會更新成正式版。
8. 各機獨立查詢、更新與寄信，不互相等待。

<a id="first-install"></a>

## 2. 新主機初次部署

1. 準備 GitHub repo／必要 token、SMTP 帳密、管理員信箱，以及現場伺服器、照片路徑、API、location ID、攝影機 RTSP。
2. 下載並安裝：

   ```bash
   git clone https://github.com/yangtandev/facial_recognition.git
   cd facial_recognition
   bash install.sh
   ```

3. 安裝初期先輸入角色及 SMTP；密碼隱藏輸入。可選寄測試信，確認管理員收到。
4. 繼續輸入現場參數；安裝完成依提示安排重啟並登入桌面。
5. 確認辨識畫面、真人辨識、語音、相機、MQTT 與建檔照同步。
6. 查服務：

   ```bash
   systemctl --user status facial_recognition.service --no-pager
   journalctl --user -u facial_recognition.service -n 100 --no-pager
   ```

7. 需要自動更新時繼續[首次接入](#ota-enrollment)；安裝程式／寫入 SMTP 不等於已啟用 OTA。

- 已有 config.json 時，完整安裝會拒絕執行，避免覆蓋現場設定；改用下節。
- 不用 setting/build_config.py 遷移舊設定，避免遺失未知欄位。

<a id="smtp"></a>

## 3. OTA／SMTP 設定

### 3.1 既有主機或修改設定

1. 將本版 ota/ 與 install.sh 提供至原專案；不要覆寫正在運行的主程式。
2. 已安裝 agent 者先確認更新已結束，再停止 poll／notify；首次尚未安裝者略過：

   ```bash
   systemctl --user stop facial-ota-poll.service facial-ota-notify.service
   ```

3. 在原專案執行：

   ```bash
   cd /home/ubuntu/facial_recognition
   bash install.sh --ota-config-only
   ```

4. 此模式僅設定 OTA／SMTP，不執行 apt、不改辨識 config.json、不建立／重啟辨識服務。
5. 已有設定時 Enter 可沿用；UUID、路徑及其他欄位保留。敏感欄位不顯示既有值；要清空既有 token／帳密時，直接編輯實際設定檔。
6. 若 state_dir/agent.json 已存在，精靈自動修改該檔；未安裝則寫入 ota/agent.local.json。自訂路徑可執行：

   ```bash
   python3 -m ota.configure --project /home/ubuntu/facial_recognition --config /absolute/path/agent.json
   ```

7. 精靈顯示實際寫入路徑；權限 600，舊設定備份在同目錄的 *.before-smtp.local.json。
8. 首次接入繼續第 4 節；已安裝者完成後重啟：

   ```bash
   systemctl --user restart facial-ota-poll.service facial-ota-notify.service
   ```

### 3.2 輸入欄位與設定檔

| 欄位 | 填寫 |
| --- | --- |
| 角色 | development、test、production |
| GitHub repo | owner/repo |
| GitHub token | 私有 repo 必填唯讀 PAT；公開 repo 可空白 |
| SMTP 主機 | 供應商的寄信主機 |
| TLS／連接埠 | 通常 starttls＋587 或 ssl＋465，依供應商 |
| SMTP 帳密 | 寄信帳號與密碼／應用程式密碼；無需認證的 TLS relay 可空白 |
| 寄件信箱 | SMTP 允許使用的寄件地址 |
| 收件信箱 | 管理員地址，多位以逗號分隔 |

- device_id 自動產生、重設時沿用；不需要 device_token／token_sha256。
- Fine-grained PAT 僅選此 repo、Contents: Read-only；記錄到期日，組織要求時等待核准。[GitHub PAT 設定](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- 核對 shared_dir（原專案）、state_dir（原專案及可清除版本目錄之外）、python（app／state 以外的系統 Python 3.10+，minor 與 Release 相同）。
- 預設狀態目錄為登入使用者的 ~/.local/share/facial-ota；以下指令以 ubuntu 為例，自訂時對應替換。
- 專案名取自 config.json 的 Server.API_url 主機名稱第一段；地點以 Server.location_ID 查 API_url/locations/ID/ 的 name。
- 例如 https://demosite.api.ginibio.com/api/v1、ID 1，專案 demosite，地點可為「測試閘道」。查詢失敗沿用快取，首次失敗顯示 location ID。
- 寄信使用 Python 標準函式庫與 TLS，不需另裝郵件伺服器。[SMTP 文件](https://docs.python.org/3/library/smtplib.html)

<a id="ota-enrollment"></a>

## 4. 首次接入 OTA

### 4.1 備份與準備 baseline

1. 舊機不會因發布 tag 取得 OTA；每台需先人工接入一次。
2. 備份設定與原服務內容，另備份照片、特徵、業務資料與金鑰：

   ```bash
   cd /home/ubuntu/facial_recognition
   OTA_BACKUP_DIR=$(mktemp -d /home/ubuntu/facial-before-ota.XXXXXX)
   install -m 600 config.json "$OTA_BACKUP_DIR/config.json"
   systemctl --user cat facial_recognition.service > "$OTA_BACKUP_DIR/facial_recognition.service.txt"
   ```

3. 完成 SMTP 設定，確認新版必要 config 欄位齊全；只補缺少項目、保留現場值。首次 baseline 準備不會自動遷移 config。
4. 從已核對的 GitHub Release 取得完整 .tar.gz 與 release.json；Python minor／CPU 相符。
5. 測試機可選健康 RC／正式版；正式機僅選人工放行的健康正式版。替換套件路徑：

   ```bash
   python3 -m ota.updater --config ota/agent.local.json prepare-baseline --archive /path/to/package.tar.gz --manifest /path/to/release.json
   ```

6. 記下輸出的 app 絕對路徑；準備階段不停止舊服務。

### 4.2 人工切換並驗收

1. 安排可用 AnyDesk 介入的維護時段，建立 override：

   ```bash
   mkdir -p /home/ubuntu/.config/systemd/user/facial_recognition.service.d
   nano /home/ubuntu/.config/systemd/user/facial_recognition.service.d/10-bootstrap.conf
   ```

2. 填入以下內容，所有 <BASELINE_APP> 換成準備好的 app 絕對路徑；health port 非 8765 時也調整：

   ```ini
   [Service]
   WorkingDirectory=<BASELINE_APP>
   ExecStart=
   ExecStart=<BASELINE_APP>/venv/bin/python -u <BASELINE_APP>/main.py
   Environment=FACIAL_OTA_HEALTH_PORT=8765
   ```

3. 重啟並驗收：

   ```bash
   systemctl --user daemon-reload
   systemctl --user restart facial_recognition.service
   python3 -m ota.updater --config ota/agent.local.json verify-baseline --baseline /absolute/baseline/app
   ```

4. 確認版本、程序、GUI、模型／辨識與 MQTT／同步；相機可用時也做真人辨識、語音及同步實測。
5. 相機暫離線不單獨使驗收失敗，影像端到端實測待連線恢復後完成。
6. 程式驗收未通過就停止接入，恢復原服務設定／config，再啟動。

> 首次人工切換尚未受 OTA 自動回退保護；驗收通過才登記為 baseline。

### 4.3 安裝與啟用

1. 安裝獨立 agent：

   ```bash
   python3 -m ota.install_agent --config ota/agent.local.json --baseline /absolute/baseline/app
   ```

2. 查狀態並執行一次通知檢查；SMTP 測試信應已確認送達：

   ```bash
   cd /home/ubuntu/.local/share/facial-ota/agent
   OTA_CFG=/home/ubuntu/.local/share/facial-ota/agent.json
   python3 -m ota.updater --config "$OTA_CFG" status
   python3 -m ota.updater --config "$OTA_CFG" notify-once
   ```

3. 確認後啟用：

   ```bash
   systemctl --user enable --now facial-ota-poll.service facial-ota-notify.service
   ```

4. 實際設定在 state_dir/agent.json；只改範例檔不影響已安裝服務。
5. poll／notify 使用固定目錄的獨立程式；一般 app OTA 不自動替換 agent 本體。

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
   venv/bin/python -m ota.package_release --version v1.0.1-rc.1 --wheelhouse ota/wheelhouse --output ota/dist/v1.0.1-rc.1 --target-python 3.10 --architecture x86_64 --projects demosite
   ```

5. 替換版本、Python minor、CPU 與專案；--projects demosite otherproject 指定多專案，共用版才明確用 --projects '*'。
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

在現場以原服務帳號操作，以下採預設路徑：

```bash
cd /home/ubuntu/.local/share/facial-ota/agent
OTA_CFG=/home/ubuntu/.local/share/facial-ota/agent.json
python3 -m ota.updater --config "$OTA_CFG" status
curl --noproxy '*' -sS http://127.0.0.1:8765/health | python3 -m json.tool
systemctl --user show facial_recognition.service -p MainPID -p NRestarts
journalctl --user -u facial_recognition.service -u facial-ota-poll.service -u facial-ota-notify.service -n 100 --no-pager
```

- latest_update 顯示最近更新結果、回退及時間；selected_build 只是選中套件，不代表程序正常。
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
- 改 SMTP／GitHub token 用第 3 節精靈並重啟服務，不刪資料庫或重裝來清待寄數。

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
   venv/bin/python -m unittest ota.test_ota -v
   ```

2. 實機故障測試用隔離 repo／測試專案，不向正式 repo 發布故障套件。保存主機、前後版本／build ID、時間、回退及信件紀錄。
3. 依序驗證：

| 測試 | 操作 | 預期 |
| --- | --- | --- |
| 舊機設定 | 備份 config，僅設定模式跑兩次 | config 不變、UUID 相同、私有設定 600 |
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
