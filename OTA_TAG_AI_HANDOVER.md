# OTA tag-only 改版交接

更新：2026-10-30，Australia/Adelaide。現場時間以輸出中的時區為準。這是接續工作的狀態摘要。

最新：工具 commit b8c347e 已推送並在測試機完成維護、15 秒健康及 tags 查詢驗收；使用者已確認授權發布 v1.5.0-rc.1（第 15 節）。README 第 5.7 節補齊本次指令；RC app 更新與正式機維護／部署尚未驗收或執行。

## 1. 使用者最新決定與本輪範圍

- 使用者明確決定：**OTA 改為只看 Git tags，不再以 GitHub Release 是否發布／附件是否齊備作為更新入口。** 不要再反覆建議保留 Release、詢問是否真的要改。
- 改版動機：現場已有可用的 Python 依賴與模型，希望驗證後重用，避免每版重下載接近 1 GB；使用者提出「檢查 requirements.txt，多退少補」。
- tag-only 與減少下載是兩項相關但不同的工作：只換選版 API，不會自動消除目前完整套件下載；須一起檢討來源取得、模型／依賴重用與回退。
- **2026-10-30 已依使用者指示完成本機 tag-only 實作及驗證（詳第 11 節），尚未提交、推送、推 tag 或操作現場服務。** 先前僅寫交接的範圍已被接續改版要求取代；不要重做已完成實作。
- 使用者偏好 caveman ponytail：繁體中文、簡短、原生工具／標準庫優先、不過度設計；操作指令與成功判讀要寫入 README。
- 新 commit、tag、push、部署仍遵循本次操作授權；先前故障 RC 的授權不等於允許任意發布後續版本。不得移動已發布 tag、覆寫既有附件、清除資料庫或私有設定。
- 舊 `OTA_AI_HANDOVER.md` 是 2026-10-26 的歷史摘要，包含已過時的版本與「必須 Release」決策；**以本文件的最新使用者決定、實際程式及現場回報為準**。

## 2. 接手立即做的事

1. 讀本文件、`AGENTS.md`、適用 skills；檢查 Git 與未提交變更，不重做已完成工作。
2. **現場重試結果已收齊。** attempt `6e65daa0-9840-4a0e-816a-c625a3507f97`，phase=failed、rollback=not_needed；實際下載 671088640／943117577 bytes，未切換。使用者另回報 selected_build=v1.4.0、blocked=false、pending=0，last_health 快照核心／外部／相機全綠、PID 68818，並確認本次失敗郵件實際收到。這不是完整回退通過，也不是 AI 直接取得的即時健康。
3. 若仍在更新，保留原作業；不要為改版中止它、重啟 poll 或重跑 retry。
4. 本機實作與驗證見第 11 節；下一步僅按 README 第 6.4 節安排測試機工具維護，再規劃獲授權的新 tag 現場驗收。沒有遠端操作測試／正式機的工具，現場需使用者執行指令；不得聲稱代執行。

## 3. 工作目錄與 Git 狀態（本輪重新核對）

- workspace：`/home/ubuntu/facial_recognition`；這是開發機，不是現場機。
- remote：`https://github.com/yangtandev/facial_recognition.git`。
- 正常分支：`develop-v1.0.0`。
- HEAD：`87fd14ac369b385ad49c7bd4609ef9d86aa8c816`，正常 v1.4.0 來源。
- 另一個 detached worktree：`ota/dist/e2e-rollback-v1.4.1-rc.1`，HEAD `da64d6cd401098adb4444cd08003696c603d75be`，是故意啟動失敗的測試來源，**不得當作正常開發基底或晉升正式版**。
- 本地已見 v1.4.0、v1.4.0-rc.1／rc.2／rc.3、v1.4.1-rc.1；將來選版前仍須核對遠端，不自行猜下一個版號。
- 建立本文件前狀態：`README.md` 已修改；`OTA_AI_HANDOVER.md`、`OTA_TEST_RECORD.md`、`config.json.lock` 未追蹤。本文件也是新檔。
- 保留上述使用者工作；不要自動 stage／commit、刪 lock 檔或 reset。README 的大量更新尚未提交，也不在已發布的故障 RC 中。

## 4. 現場基準與已完成驗收

| 主機 | 已知狀態 |
| --- | --- |
| 測試機 | `gini-aiface@gini-aiface-NUC11TNKi5`，`$HOME/facial_recognition`，test／demosite，Python 3.12／x86_64；唯一一台 test 主機；最近已確認健康基準 v1.4.0，PID 68818；故障 RC 重試下載不完整、切換前失敗，最新回報 selected_build=v1.4.0、blocked=false、pending=0，健康快照全綠，失敗郵件收到 |
| 正式機 | `gini@Gini-MediaServer`，`$HOME/facial_recognition`，production／demosite，Ubuntu 22.04.5；系統原為 Python 3.10.12，已平行安裝 Python 3.12.15；v1.4.0 OTA 納管成功，驗收時 PID 3713950；這是歷史 PID，不能視為現在即時值 |

- 正式機不能任意重開機；僅 AnyDesk 的遠端現場，避免中斷網路、桌面或遠端服務。正式機顯示設定曾修為 `DISPLAY=:1`、`XAUTHORITY=/run/user/1000/gdm/Xauthority`，不要改回 :0。
- 已完成：test 的 RC.2 → RC.3 → v1.4.0；production 的 legacy → v1.4.0。兩機原正常發布流程成功，production 會略過 RC。
- 兩機 15 秒唯讀診斷 pass；使用者確認真人辨識、進出方向、語音、建檔照同步正常。
- 15 秒診斷是兩次健康快照、PID／必要工作計數進度檢查，不代表完整壓測或業務測試；也不能取代更新時的首次穩定驗收。
- 測試機根目錄舊 `install.sh` 曾不支援 `--ota-check`，可用已安裝版本的入口：

```bash
cd "$HOME/.local/share/facial-ota/current" &&
python3 -m ota.diagnostics check \
  --project "$HOME/facial_recognition" \
  --expect-role test --expect-version v1.4.0 --seconds 15
```

- 最近作業若為 failed，診斷可能按設計報失敗，即使已健康回退；不要清歷史或強求 pass 來掩蓋失敗紀錄。

## 5. 未完成：真實故障 RC 回退驗收

### 已發布的故障版

- 使用者已明確核准並發布 `v1.4.1-rc.1`，commit `da64d6cd401098adb4444cd08003696c603d75be`，訊息 `test(ota): exercise release rollback`。
- Release：https://github.com/yangtandev/facial_recognition/releases/tag/v1.4.1-rc.1 ，ID `427585902`。
- 發布時間 `2026-10-29T08:22:16Z`；當時核對 draft=false、prerelease=true，Latest 正式版仍 v1.4.0。這是先前遠端核對結果，不是本文件建立時重新查詢。
- 故障只在該 worktree 的 `main.py` 頂端加 `raise RuntimeError("Intentional startup failure for OTA rollback validation")`；另更新 RELEASE_NOTES；沒有改舊 app、更新器或郵件格式。
- 目的是走原服務的發現、下載、準備、切換、啟動失敗、原更新器回退及真實失敗通知；通知地點不加「演練」後綴。
- archive：`facial-recognition-v1.4.1-rc.1.tar.gz`，asset ID `687790020`，**943117577 bytes**。
- archive SHA256：`6745c8321d855d3f1fd91964bcc5808f97cb73de075466116a586ebad034d42e`。
- `release.json`：asset ID `687790021`，531 bytes，SHA256 `48d1befa4940c118cc3a5134edb3a8ab5c113173ac245d0457373805838b88c1`。
- 適用 demosite／Python 3.12／x86_64，config_schema=data_schema=1。
- feature model SHA256：`281cebca8662831adb987a874bdcb36e73f5b1c6dc5ee5878f305e985625d99b`。
- 已完成封裝驗證：來源與實際包各 183 tests pass；全新 venv 離線安裝、pip check、CPU NMS、核心匯入通過；新版入口依預期退出。實際包相對 stable 只變 main.py、BUILD_INFO.json、RELEASE_NOTES.md，77 個 wheels／模型／更新器不變。

### 第一次現場嘗試：下載驗證失敗，尚未切換

- attempt `a4ec9d4a-dd4f-4830-a38d-1783e1f23913`；`2026-10-29T17:02:11+08:00`。
- target v1.4.1-rc.1，from v1.4.0，phase=failed、rollback=not_needed。
- 錯誤：`ValueError: Archive integrity check failed`；這表示收到的總長度不符或 SHA256 不符，**無證據可判定是哪一種，根因尚未確定**。
- 舊 v1.4.0 PID 68818 未變，核心／相機健康、pending=0、blocked=false。這只能證明切換前拒絕，不算故障啟動後回退通過。
- 該次 `releases/v1.4.1-rc.1-a4ec9d4a-dd4f-4830-a38d-1783e1f23913/package.tar.gz` 已不存在；原更新器後續 poll 會 cleanup 終態失敗目錄，SQLite 紀錄保留。
- 使用者查最近一小時及固定 `2026-10-29 16:15～17:15 Asia/Taipei` 的 poll journal 都得到 `No entries`。不能斷言歷史被刪，也不能把它當成更新未發生；詳細操作主要記在 SQLite，並非每個階段都會印到 journal。
- 開發端與測試機均用相同 `ota.core.request` 查 archive 端點、只讀開頭 32 bytes：HTTP 200、application/octet-stream、Content-Length=943117577、gzip 檔頭正確。**這未驗完整內容，不能排除中途截斷／損毀。** 不跳過校驗、不覆寫發佈檔案。

### 本次重試結果：下載不完整，未進入切換／回退

- 已讓使用者執行正常 CLI：`python3 -m ota.updater --config "$HOME/.local/share/facial-ota/agent.json" retry --version v1.4.1-rc.1`；不是 monkeypatch，也沒有新 Release。
- 實際提供的是一段 Python：先讀已有 attempt IDs，再登記 retry，等待原 poll 建立新 attempt，每 30 秒印 phase／檔案 bytes；離開 preparing 時讀 package.tar.gz 大小／完整 SHA256，保存 `$HOME/.local/share/facial-ota/download-check-<新attempt_id>.json` 並印結果。
- 旁觀程式最多觀察 2 小時；退出不會停止原 OTA。它可能在 prepared／validating 時就結束，**不代表回退已完成**。開發機有當時範本 `/tmp/facial-ota-retry-observer.py`，臨時檔可能消失；不要當作測試機上的持久路徑。
- 先前進度回報：phase=preparing，下載曾增加到 **313524224 bytes，約 33%**；這已是歷史進度，不代表仍在下載。
- 2026-10-30 使用者提供 `/home/gini-aiface/.local/share/facial-ota/download-check-6e65daa0-9840-4a0e-816a-c625a3507f97.json`：

```json
{
  "attempt_id": "6e65daa0-9840-4a0e-816a-c625a3507f97",
  "phase": "failed",
  "sha256": "6745c8321d855d3f1fd91964bcc5808f97cb73de075466116a586ebad034d42e",
  "rollback": "not_needed",
  "error": "ValueError: Archive integrity check failed",
  "downloaded_bytes": 671088640,
  "downloaded_sha256": "a992383943130e07c50cb5eb3763d652aceac4e2e2196bf3d3b343f7bc5feeac"
}
```

- 本次實收 **640 MiB（671088640 bytes）**，比預期 **943117577 bytes** 少 **272028937 bytes**；大小與 SHA256 均不符。可確認此次下載不完整，無法僅憑數字判定代理／網路／伺服器等截斷原因，也不能據此推定第一次失敗同因。
- `phase=failed`、`rollback=not_needed`：校驗在切換前拒絕本次更新，未驗證故障啟動後回退。舊 PID、健康快照與通知是否送達不得沿用先前結果。
- 使用者已補回 `bash install.sh --ota-status`：selected_build=v1.4.0（commit 87fd14ac…）、latest_update 為本次 failed／not_needed、failed_at=2026-10-30T00:57:03+08:00、blocked=false、pending=0；last_health 快照核心／MQTT／相機皆正常、PID 68818，並確認真正失敗郵件收到。setup.target_version=v1.4.0-rc.2 是舊接入紀錄，不是目前運行版本。不要再重跑 retry。
- 完整回退通過須有：target=v1.4.1-rc.1、phase=failed、rollback=success、blocked=false、恢復 v1.4.0 且即時核心健康及計數進度正常、該次真正失敗郵件實際收到。

## 6. 已通過但範圍較小的舊演練

- 使用者曾先做本機故障副本及人工 mail 事件，後來要求取消測試入口隔離，才有第 5 節真實 RC。
- 現場私有備份：`/home/gini-aiface/ota-rollback-backup.QjRkYS`。含 SMTP／agent 設定，**不可整份輸出或提交**。
- 本機回退：副本 `~/.local/share/facial-ota/rollback-drill-akf_8cen`，背景 unit `facial-ota-rollback-drill.service`；driver 替換 Agent.stage 為預備副本，其他 update／rollback 走原流程；event 地點加演練後綴。
- attempt `ae6ce2c1-693a-44db-9d2c-9cc4a915e379`，2026-10-27 17:20～17:24 +08；from／target 都是 v1.4.0，phase=failed、rollback=success，恢復 PID 68818、健康、blocked=false；失敗郵件確實寄達。
- 舊 mail 補寄：人工測試事件 `58f909d3-6cb6-4197-b6f3-dc250a96de14`，暫時 SMTP 指向 127.0.0.1:1，ConnectionRefusedError／tries=1；還原 SMTP 後背景補寄，使用者收到，delivered=true、pending=0、原 SMTP 完整恢復、辨識 PID 未變。
- 這些只算各自測試範圍通過，不能代替完整 tag／Release 更新路徑驗收。不得宣稱「正式機也已做故障回退」。
- 真實故障版搭配 SMTP 故障的補寄驗收尚未做；第二個故障 RC、正常 RC 收尾尚未發布。後續應改按新的 tag-only 目標規劃，不照舊計畫直接發 Release。
- **斷電與程序強制中斷測試依使用者指示暫停，不重啟主機、不斷網測試。**

## 7. 原 tag-only 設計檢查（實作結果見第 11 節）

以下保留改版前的設計檢查，供核對意圖；已實作內容以第 11 節及目前程式為準，尚未現場驗收。

- tag 由 `vX.Y.Z`／`vX.Y.Z-rc.N` 區分；development 不跟隨、test 接收適用 RC／正式版、production 只收正式 tag。保留數值 SemVer 比較、禁止一般降版、失敗版不無限重試。
- 正式 tag 的推送將成為部署放行動作；修改目前「tag 不觸發、發布 stable Release 才放行」的說明與流程。保留人工驗收後才推正式 tag 的要求。
- 解析 annotated／lightweight tag 到固定 commit，避免抓可移動分支。已見過同一 tag 的 commit 應保存並拒絕被移動後悄悄更新。
- 更新不應再要求存在 GitHub Release／附件／draft／prerelease flags；新的 metadata 可隨來源提交等，採最小方案決定，但仍需適用專案、Python／平台、依賴、模型、設定／資料 schema、來源 build 身分。
- **舊 tags 並非都可部署；尤其現有 v1.4.1-rc.1 是故障包。** 設計 tag-only 接入規則／metadata 契約時避免一上線就自動把任意歷史 tag 或故障 tag 當正常候選；不能默認所有 tag 都符合新規格。
- 下載原始碼時留意 `.pt`／`.pkl` 使用 Git LFS；GitHub source archive 預設可能只有 pointer。必須取得並驗證真實模型，不能假設切換到 tag 就會自帶模型。
- 原始碼 archive 的外層 gzip bytes 可能被 GitHub 重新生成；固定 commit 保證內容，不保證外層壓縮檔永久同 SHA。若依賴摘要驗證，須設計相應的檔案／metadata 契約，不能沿用自製 tar 的假設。
- **重用依賴**：Python／平台與完整目標依賴相符時，可設計重用已驗證且不再修改的既有環境；不能僅因 requirements.txt 沒變，就假設間接依賴及實際環境一致。
- **依賴有變**：保留舊環境，另建目標環境；優先使用經驗證的本機 wheels，只補下載缺檔。已安裝套件不等於安裝檔仍在快取；不能承諾新環境永遠只下載新增一兩個套件。
- 不直接在運行中／回退需要的舊 venv 做 pip 增刪。`pip install -r` 不會移除所有未列出的套件，直接按 requirements 文字差集刪除也可能誤刪間接依賴。
- 現在 requirements 多為直接依賴版本，未完整鎖定所有間接依賴及摘要；CPU 版 torch 實際為 2.2.2+cpu，不能意外解成 CUDA 大包。Python 3.10→3.12 不可直接重用舊 ABI 的 venv。
- 模型未變時驗摘要重用；不回退照片／最新業務資料，模型或資料 schema 不相容需明確遷移，不靜默重建或丟失人臉資料。
- 仍保留 staging、原程式持續服務、切換前完整驗證、原子切換、健康驗收、回退、持久 checkpoint、SQLite 失敗通知補寄等能力。
- **現場 OTA 是獨立工具，改應用程式 tag 不會自動升級舊 poll。** 必須規劃一次性的工具維護／bootstrap：更新 ota／install.sh 後維護 agent，不重裝正常辨識、不改角色、不因相容舊客戶端而擅自再發布 Release；先在 test 驗證，不能在未完成更新途中替換工具。

## 8. 現行程式、資料與文件入口

| 路徑 | 檢查重點 |
| --- | --- |
| `ota/core.py` | version_key／select_release 現依 tag SemVer 後綴選版，不讀 Release flags；Store、通知佇列、完整性／safe_extract、持久檔案操作 |
| `ota/updater.py` | releases（沿用方法名）讀 tags API，manifest 讀固定 commit 的 ota/deploy.json，stage 委派 tag_source；asset API 已移除；once、poll_enrollment、retry、rollback、recover、cleanup 與健康驗收要維持 |
| `ota/setup.py` | production waiting 接入、select／enroll 共用 tag 選版，無契約略過；保留首次納管與舊服務復原 |
| `ota/tag_source.py`、`ota/deploy.json` | 固定 Git commit、LFS 模型驗證／重用、完整 wheel 鎖定、環境重用／隔離準備、目標 wheel 內容身分與離線清單產生 |
| `ota/install_agent.py` | 獨立 agent runtime、poll／notify units；與 app runtime 分開維護 |
| `ota/package_release.py` | 僅保留舊離線包／prepare-baseline 相容用途，不再是新 OTA 入口 |
| `ota/configure.py`、`ota/diagnostics.py`、`install.sh` | 既有私有設定定位、接入／狀態／診斷 CLI 相容與遷移 |
| `ota/test_ota.py`、`ota/test_setup.py`、`ota/test_diagnostics.py` | 原 183 項基線保留回退／SMTP／復原覆蓋、更新 Release flag 斷言；另有 ota/test_tag_source.py，合計 200 項通過 |

- 現場預設 state=`$HOME/.local/share/facial-ota`；`agent.json` 為私有設定、`agent/` 獨立工具、`current` app symlink、`releases/` 各版環境、`agent.sqlite3` 作業與通知；`setup.json` 為首次接入 checkpoint。
- attempts 表每次作業只保存最新 phase／data；outbox 每個已記錄階段各有 body，可回查過程。待寄／tries／SMTP 錯誤也在 outbox；成功後 error 清空、tries 保留，沒有完整不可變的每次 SMTP 失敗歷史。
- journal：`journalctl --user -u facial-ota-poll.service -u facial-ota-notify.service -u facial-ota-setup.service -u facial_recognition.service -n 100 --no-pager`；保留期間依 journald 設定。
- `pending=0` 只表示無待寄事件，不代表更新成功；SMTP 接受不等於入信箱。普通階段 outbox.delivered=1 可能表示不需寄信，不代表曾寄信。
- startup_timeout 預設 180 秒，stable_seconds 60 秒；poll 預設 300 秒＋jitter，無 token 至少 1200 秒＋jitter。prepare 的 pip install timeout=1800 秒；preparing 也包含下載，沒有整段總時限。`last_health` 在更新期間可能是舊快照。
- README 已有接入、Qt 顯示、Python、token、唯讀診斷、發布、進度、日誌及第 8.3／8.4 局部演練、第 8.5 完整故障版計畫。更新時分清新操作方式與歷史驗收，不把歷史改寫成 tag-only 已通過。
- `OTA_TEST_RECORD.md` 是給非專業主管的工作紀錄，使用者要求**維持五句／五個簡短條列**，不要膨脹成技術報告。
- 本地 artifact／驗證紀錄在 `ota/dist/v1.4.1-rc.1/`；`execution-state.json` 的已發布狀態可參考，但舊 upload_session_id／upload_pid／last_archive_read_bytes 是過時進度欄位，**上傳已完成，不要再追那些程序**。

### 容量的實際量測（已讀取已發布 tar 統計）

壓縮包 943117577 bytes；包內檔案未壓縮合計：wheelhouse 810103931 bytes、models 147769007 bytes、其他 app 12984555 bytes。不要把未壓縮分項直接當成 gzip 中精確占比。

最大項目包含 torch 約 187 MB、主模型約 112 MB、jaxlib 約 81 MB，以及兩個 OpenCV wheels、Qt 等；目前有 77 個 wheels。未做依賴刪減驗證，不要只因容量大就移除套件。舊版每版新建空 venv，這才是當時打包完整離線安裝材料的理由，並非 tag 或 Release 的必然要求。

## 9. 政策、驗證與完成標準

- 專案 release skill：`.gemini/skills/facial-recognition-release-rc/SKILL.md`。仍遵循 SemVer、RC 驗收、tag 不可重用、正式版人工放行；其中 GitHub Release 放行段落已同步改為 tag 放行，勿反過來用舊文件否決最新決定，也勿修改使用者全域 skills。
- 其他可用 skill：`/home/ubuntu/.codex/skills/lean-caveman/SKILL.md`；需要 commit 時讀 conventional-semver-commit，先提供訊息／版本範圍再按授權動作。不要把本交接請求當 commit／push／正式部署授權。
- 改版應覆蓋：無 Release 也能 tag 發現、RC／stable 角色隔離、SemVer、專案相容、tag→commit 身分、歷史無 metadata／移動 tag 拒絕、失敗版跳過／retry；來源／模型不完整拒絕；依賴不變重用、有變另建環境且補快取；等待接入、失敗回退、SMTP 補寄／中斷復原不退化。
- 保留 test 先驗、production 後放行；新功能改版的版本範圍依最終差異重新判斷，**不要直接把故障 v1.4.1-rc.1 晉升，也不要先預設下一個正式版號**。
- 有實作變更才跑對應測試與必要整體檢查；以下是既有基線命令，Python 環境以本機實際可用者為準。過去 183 pass 不代表新 tag-only 已測試。

```bash
python3 -m unittest ota.test_ota ota.test_setup ota.test_diagnostics ota.test_tag_source -q
bash -n install.sh
git diff --check
```

- 所有現場指令用原桌面帳號與 `$HOME`，不 `sudo systemctl --user`；不把開發機 `/home/ubuntu` 路徑強套到現場。
- 不輸出／提交 GitHub token、SMTP 密碼或完整 agent.json；需要檢查時只輸出白名單非敏感欄位。網路／.git 寫入可能要權限核准，未成功不得宣稱完成。

## 10. 新對話啟動文字

```text
caveman ponytail。
請完整讀取 /home/ubuntu/facial_recognition/OTA_TAG_AI_HANDOVER.md 與 AGENTS.md，接續已在本機實作並通過 200 項測試的 tag-only OTA；先核對現況，不重做已完成改版。
現場已有的 Python 依賴與模型要驗證後重用，避免每次重下載整包；保留角色隔離、完整性驗證、健康檢查、回退與郵件補寄。
故障 v1.4.1-rc.1 重試已確認下載截斷、未切換；舊 v1.4.0 保留、blocked=false、pending=0、失敗郵件收到。下一步測試機工具維護及真正 tag-only 現場驗收，完整回退仍未通過；不要把故障 RC 當正常基底。
按 README 第 6.4 節說明下一步，確認已授權範圍；不要自行 commit、push、推新 tag 或部署正式機。
```


## 11. 2026-10-30 本機實作完成，現場 tag-only 待驗

- 新 `ota/deploy.json`：format=1、demosite／Python 3.12／x86_64、schema=1；8 個模型、77 個完整鎖定 wheels。沿用既有測過的 CPU wheels，沒有新增／刪減套件或重新解析版本；torch=2.2.2+cpu、torchvision=0.17.2+cpu。清單版號／commit／build ID 由目標 tag 產生，不含自指 commit。
- GitHub `/tags` API 分頁＋ETag；取 API 的解析後 commit，支援 annotated／lightweight tags。持久保存 repo＋tag→commit，變更則拒絕，`last_poll.moved_tags` 可查；不讀 Release、draft、prerelease 或 assets。無 `ota/deploy.json` 的歷史 tag 快取略過，包括舊故障 RC。
- 準備來源用 Git 的固定 commit、`transfer.fsckObjects`、sparse checkout、跳過 LFS smudge；不抓可移動 branch、不假設 GitHub 外層 gzip 摘要永久相同。來源需完整 tracked prerequisites；拒絕 symlink／submodule。
- 模型需與清單／LFS pointer 的 size／SHA256 相符；優先驗現有模型後複製，缺少才用 Git LFS 取固定 commit 的指定檔，再驗摘要。feature model／data schema 變更仍拒絕自動切換。
- 環境重用：核對 Python minor／CPU／SOABI、完整名稱／版本、鎖定 wheel 實際 SHA256／RECORD 與 installed RECORD 檔案摘要、pip check、requirements dry-run。bootstrap pip／setuptools／wheel 未列入鎖定時可額外存在。OpenCV 兩套件共用檔案可符合鎖定集合任一 RECORD 的已知摘要，未知修改仍拒絕。requirements 摘要正規化 CRLF→LF；format=1 僅允許固定版本、註解／空白及鎖定的本地 wheel，不允許 URL／範圍／pip 選項。
- 符合時 app/venv 連到固定已驗環境；不移動／複製 venv，不對舊 venv pip 增刪。cleanup 保護環境所在舊 release，直到沒有保留中的 app 依賴。同名同版本但 wheel 內容改變也不會誤用；材料不足時不只憑版本重用。依賴不同／檢查不過則另建新 venv；先驗本地 wheelhouse／舊包 wheels，缺少才按鎖定內容下載，安裝不再解析間接依賴。驗證快取目前長期保留；磁碟不足拒絕準備。
- staging／原子切換／健康穩定觀察／回退／SQLite checkpoint／SMTP 佇列保留。一般輪詢、production 待命與首次接入都走 tags；等待狀態字串 `waiting_release` 沿用以相容既有 checkpoint。retry 保留原派工規則，不能在已消耗 retry 後再次以失敗清單誤擋 worker。
- `status.discovery=tags`、`last_poll.source=tags`；latest_update 另顯示 operation，便於區分 agent_upgrade 與 app 更新。tag-only 作業的 sha256 是部署清單摘要，內容來源另以 Git commit 驗證，模型／wheels 各自 SHA256。
- 本機驗證：200 tests pass、bash -n、git diff --check；真實 Git 以本地 repository 取固定 HEAD、sparse checkout、LFS 保持 pointer 通過；不是已完成 GitHub／現場新 tag 更新。
- 已對舊故障 RC 的驗證 venv 做「僅依賴」唯讀驗證：77／77 套件吻合、ABI／目標 wheels 的 SHA256＋RECORD（含 pygame 搬移標頭／numba scripts）／已安裝 RECORD（含 OpenCV 重疊）／pip check／requirements 通過。再以新 app 目錄實際執行環境準備，結果 reused、wheel 取得與下載次數 0。未啟動故障 main.py，也未以故障來源當正常 app 基底。
- README 第 4／5／6／7 節、專案 `.gemini/skills/facial-recognition-release-rc/SKILL.md` 已同步 tag 契約、推正式 tag 即放行、先 test 工具維護及進度查詢；第 8 節保留舊 Release 驗收歷史並補上兩次下載失敗。主管工作紀錄仍五個條列。
- 工具維護包：`ota/dist/tag-only-tools.tar.gz` 及同目錄 `.sha256`；本機證據 `ota/dist/tag-only-validation.json`。只含 ota Python 工具／範例／部署清單、install.sh、README，無模型、venv、私有設定或 token。尚未傳送或在現場執行。
- **下一步**：先測試機依 README 第 6.4 節備份／維護工具，確認未重啟 app、selected_build 保持 v1.4.0、discovery/source=tags、查詢正常，再安排獲明確授權的可部署新 tag 驗收。沒有新 tag 就無法完成真正 tag-only 更新／故障啟動回退。現場無遠端工具，需使用者執行；正式機維持不操作。
- 未 stage／commit／push／建立或推送 tag／發布 Release／改現場服務。新程式、測試、deploy.json 與文件均在正常 develop-v1.0.0 工作目錄待審；HEAD 未變，舊 worktree 不動。先前 README／未追蹤文件及 config.json.lock 保留。


## 12. 2026-10-30 兩機操作清單與 GitHub Release 清除

- 使用者確認只有一台 test 與一台 production，要求分別列 OTA 步驟；允許刪除 repo 全部 Releases，之後不再使用。此授權是刪 GitHub Releases，不是 commit／push／推新 tag 或部署正式機授權。
- 遠端已刪四個 Release ID：427585902（v1.4.1-rc.1）、424539160（v1.4.0）、424471337（v1.4.0-rc.3）、420467231（v1.4.0-rc.2），合計八個附件。刪除後 API 回傳空清單；八個 Git refs/tags 的物件型別與 SHA 與刪除前逐項相同。未刪 tag 或本機／現場環境。
- 清理證據保存 `ota/dist/release-cleanup-2026-10-30/`，含完整 Release metadata、刪除前後 tag 身分及 verification.json；不是新增下載所有舊大附件。第 5／6／8 節所述 Release 發布與資產摘要是歷史，雲端附件與 Release 已不存在。
- README 第 5.6 節分列實際主機：test（gini-aiface-NUC11TNKi5）先 RC commit、再正式 commit；production（Gini-MediaServer）先實證查到且略過 RC，只有正式 tag 放行後更新正式 commit。單純 branch commit 不觸發 OTA，固定來源由具部署契約的 tag 決定。
- 先測試機完成 tag-only 工具維護並驗收，再依正式機維護授權處理正式機工具；兩機皆已遷移，才可將 production 的 rc_skipped、原版本／commit／PID 不變視為 tag 角色隔離證據。仍在跑舊 poll 而查不到 Release 不算通過。
- README 第 6.5 節記錄 Release 停用與清理，原 Release-only 來源不再作 OTA 入口。新 tag 及 tag-only 現場更新／故障啟動回退仍未發布或驗收；既有 v1.4.0 運行與本機回退材料保留。
- 本輪只有文件與工具包更新、雲端 Release 刪除，未改 OTA 程式。上輪 200 tests 與真實 77 套件零下載重用證據仍適用程式版本；本輪另外驗證文件 shell 語法、工具包內容／摘要與 CLI 匯入。先前錯將歷史郵件 UUID 的 4197 改成 4200，已還原正確 ID `58f909d3-6cb6-4197-b6f3-dc250a96de14`。


## 13. 2026-10-30 測試機基準已核對，工具改用 Git 取得

- 測試機最新回報 selected_build=v1.4.0、PID=68818、NRestarts=0、blocked=false、pending=0，last_health 快照全綠；最近作業仍是 6e65daa0… 下載失敗／rollback=not_needed。尚未完成 tag-only 工具維護；不要將此基準回報當遷移成功。
- 使用者選擇透過 Git 取得工具，不手動傳工具包。README 第 6.4 節已改為：開發端經授權 commit／push → 測試機檢查分支與本機修改 → 備份、git pull --ff-only → --ota-setup → 15 秒驗收。固定 commit／detached 情況仍可用第 4.1 節只取工具。
- **開發端前置仍未完成**：改版未 commit／push，HEAD 仍 87fd14ac…；沒有授權不得自行提交、推送、推 tag。現在 git pull 舊遠端不能取得未提交改版；提供的步驟需等新版工具確實推送、給出工具 commit SHA 後執行。不能假造新 SHA 或讓使用者用舊 v1.4.0 來源維護成所謂新版。
- 測試機已貼基準，下一個需要現場補的資訊是 git status --short／git branch --show-current（選用一般 pull 或固定來源工具路徑），以及前置完成後的維護輸出；不要再次要求下載工具包。正式機未操作。


## 14. 2026-10-30 已授權 commit／push 工具分支

- 使用者明確要求「請先commit、push」，授權已覆蓋本輪 tag-only 工具提交及正常推送 `develop-v1.0.0`；不包含新 tag、Release 或現場部署。第 13 節的未授權／未推送狀態是當時紀錄，不可再用來反覆阻擋本次提交。
- 本文件隨工具提交，記錄提交前狀態；最終 commit SHA 與推送成功應以 Git log、遠端分支及本輪完成回覆核對，不在同一提交內自指 SHA。
- 提交包含 OTA 程式、deploy.json、對應測試、README、專案發布政策、RELEASE_NOTES 的 Unreleased 說明、最新交接及五條主管工作紀錄；舊 OTA_AI_HANDOVER.md 與 config.json.lock 留原處，不納入本輪提交。
- 測試機目前只完成原 v1.4.0 基準回報，尚未維護 tag-only agent；分支推送後先核對該機 Git 分支／本機修改，依 README 第 6.4 節 pull 取得新版並執行 --ota-setup，再回報狀態／15 秒驗收。RC app 更新需另有獲授權的新 RC tag，不能將工具分支 push 視為已發布 RC。


## 15. 2026-10-30 測試機工具遷移通過，RC tag 已獲授權

- 工具 commit `b8c347e384ddb1624c3abc45e2f9e0e1b43504e8` 已提交、推送並由測試機 Git pull 取得。現場 origin 顯示 GitHub canonical repo 為 yangtandev/face_system；原 facial_recognition URL 仍可存取。
- 測試機 agent_upgrade attempt `339b8fe1-d66a-4e14-93dd-a8f7f4932ba0` success、setup complete、discovery=tags；仍選中 v1.4.0、PID 68818、NRestarts=0、blocked=false、pending=0。15 秒唯讀檢查 pass、warnings=[]、兩路相機健康，辨識未因工具維護重啟。
- 後續補回 last_poll：2026-10-30T15:40:38+08:00、query_ok=true、role=test、source=tags、moved_tags=[]、action=no_update。工具遷移及實際 tags 查詢驗收完成；不等於 RC app 更新已通過。
- 已提出以 v1.4.0 為 stable 基準、功能改版目標 v1.5.0-rc.1，基於 b8c347e 的已測執行碼／8 模型／77 鎖定 wheels，僅更新 RC 說明與操作文件。遠端尚未使用此 tag，舊故障 v1.4.1-rc.1 不作新候選基底。
- 使用者回覆「確定。這些測試指令都有記載README中嗎」，已授權候選說明 commit／push 及建立、推送 v1.5.0-rc.1；不需再問相同 tag 授權。README 第 5.7 節加入本次精確版號的狀態、日誌、唯讀 preparation／環境重用及 15 秒驗收指令。
- 本文件隨 RC metadata commit 記錄發布前狀態；最終 tag／commit 與推送成功以 Git 及完成回覆核對。推 RC tag 後由測試機原 poll 自動更新，不建立 GitHub Release。正式機維持不操作，不推正式 tag；RC app 成功、業務實測與故障回退仍待現場回報。
