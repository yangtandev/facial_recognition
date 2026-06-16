
import datetime
import hashlib
import json
from PVMS_Library import config
import os
import threading
import time
import uuid
import requests
import torch
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageEnhance

from init.log import LOGGER

with open(os.path.join(os.path.dirname(__file__), "../config.json"), "r", encoding="utf-8") as json_file:
    CONFIG = json.load(json_file)

API = config.API(str(CONFIG["Server"]["API_url"]), int(CONFIG["Server"]["location_ID"]))
API_QUEUE_TTL_SECONDS = 24 * 60 * 60
API_QUEUE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "../data/pending_api_calls.json"))


def frame_hash(frame):
    """Return a stable hash for a concrete frame array."""
    if frame is None:
        return ""
    try:
        arr = np.ascontiguousarray(frame)
        h = hashlib.sha256()
        h.update(str(arr.shape).encode("utf-8"))
        h.update(str(arr.dtype).encode("utf-8"))
        h.update(arr.tobytes())
        return h.hexdigest()
    except Exception as exc:
        LOGGER.warning(f"frame_hash failed: {exc}")
        return ""


def stable_json_hash(data):
    """Hash JSON-like data with stable key order."""
    try:
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception as exc:
        LOGGER.warning(f"stable_json_hash failed: {exc}")
        return ""


def file_tree_stat_hash(directory):
    """Hash file names, sizes, and mtimes under a directory."""
    try:
        h = hashlib.sha256()
        if not os.path.isdir(directory):
            return ""
        for root, _, files in os.walk(directory):
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, directory)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                h.update(rel.encode("utf-8"))
                h.update(str(st.st_size).encode("ascii"))
                h.update(str(st.st_mtime_ns).encode("ascii"))
        return h.hexdigest()
    except Exception as exc:
        LOGGER.warning(f"file_tree_stat_hash failed: {exc}")
        return ""


def git_head_commit(root_dir):
    """Return current git HEAD commit without spawning git."""
    try:
        git_dir = os.path.join(root_dir, ".git")
        head_path = os.path.join(git_dir, "HEAD")
        with open(head_path, "r", encoding="utf-8") as f:
            head = f.read().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            ref_path = os.path.join(git_dir, ref)
            with open(ref_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return head
    except Exception:
        return ""

def remove_old_files(directory, n=2000, m=100):
    """
    移除指定資料夾中最舊的 m 個檔案（當檔案總數超過 n 時）。

    :param directory: 要清理的資料夾路徑
    :param n: 檔案總數超過此數量才啟動清理
    :param m: 要刪除的最舊檔案數量
    """
    # 取得資料夾底下所有檔案的完整路徑
    files = [os.path.join(directory, f) for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
    
    # 如果檔案數量超過 n，進行處理
    if len(files) > n:
        # 按照檔案的最後修改時間進行排序
        files.sort(key=lambda x: os.path.getmtime(x))
        
        # 刪除最舊的 m 個檔案
        for file_to_delete in files[:m]:
            try:
                os.remove(file_to_delete)
                print(f"Deleted: {file_to_delete}")
            except Exception as e:
                print(f"Failed to delete {file_to_delete}: {e}")
    else:
        print("No need to delete files.")

def check_in_out(system, staff_name, staff_id, camera_num, n, confidence):
    """
    根據攝影機與時間控制簽到/簽離邏輯，並執行 API 上傳與語音播放。

    :param system: 系統物件(包含狀態 state 與 speaker)
    :param staff_name: 人員名稱
    :param staff_id: 人員 ID
    :param camera_num: 攝影機編號(0 為進入, 1 為離開)
    :param n: 是否為單鏡頭模式(True=單鏡頭自動切換, False=雙鏡頭)
    :param confidence: 辨識信賴度
    :return: 是否為離開狀態(0=非離開, 1=已離開超過2秒)
    """
    leave = 0
    if not staff_id in system.state.check_time.keys():
        system.state.check_time[staff_id] = [True, 0]

    now = time.time()

    # [2026-01-08 修正] 移除全域語音鎖，恢復針對個人的短防抖 (Debounce)
    # 解決 "A正在播音時，B被擋住導致有圖無聲/沒刷入" 的問題
    # 設定 2.5 秒個人防抖：
    # 1. 確保同一人在 "請進入/請離開" 語音期間不被重複觸發
    # 2. 確保不同人之間可以隨時插播與排隊，不再互相干擾
    # if (now - system.state.check_time[staff_id][1]) < 2.5:
    #     return leave
    
    # 簽到/簽離邏輯判斷
    is_check_in_action = False
    is_check_out_action = False
    
    # [2026-01-27 Feature] Time-based Schedule (Overrides standard logic)
    schedule_conf = CONFIG.get("Schedule", {})
    schedule_active = False
    
    if schedule_conf.get("enabled", False) and n:
        try:
            # Use local time
            now_dt = datetime.datetime.now()
            now_time = now_dt.time()
            
            is_in_period = False
            
            # Support multiple periods (Prioritize new list format)
            periods = schedule_conf.get("in_periods", [])
            if not periods:
                # Fallback to legacy single period
                start_str = schedule_conf.get("in_start", "06:00")
                end_str = schedule_conf.get("in_end", "17:00")
                periods = [{"start": start_str, "end": end_str}]
            
            for period in periods:
                start_str = period.get("start", "00:00")
                end_str = period.get("end", "00:00")
                
                s_str = start_str if start_str.count(':') == 2 else start_str + ':00'
                e_str = end_str if end_str.count(':') == 2 else end_str + ':00'
                start_time = datetime.datetime.strptime(s_str, "%H:%M:%S").time()
                end_time = datetime.datetime.strptime(e_str, "%H:%M:%S").time()
                
                # Check if current time is within this period
                if start_time <= end_time:
                    if start_time <= now_time <= end_time:
                        is_in_period = True
                        break
                else:
                    # Cross-midnight case
                    if start_time <= now_time or now_time <= end_time:
                        is_in_period = True
                        break
                
            if is_in_period:
                is_check_in_action = True
            else:
                is_check_out_action = True
                
            schedule_active = True
        except Exception as e:
            LOGGER.error(f"Schedule Logic Error: {e}")
    elif schedule_conf.get("enabled", False):
        LOGGER.debug(f"Schedule ignored in dual-camera mode for camera_num={camera_num}")

    if not schedule_active:
        if n:  # 單鏡頭模式：根據狀態自動判斷
            if system.state.check_time[staff_id][0]:
                is_check_in_action = True
            else:
                is_check_out_action = True
        else:  # 雙鏡頭模式：根據攝影機編號判斷
            if camera_num == 0:
                is_check_in_action = True
            elif camera_num == 1:
                is_check_out_action = True
            else:
                LOGGER.warning(f"Unexpected camera_num {camera_num} in double-camera mode; defaulting to exit")
                is_check_out_action = True

    if (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False)) and (n or camera_num == 0):
        clothes_gate_ok = bool(
            getattr(system.state, "clothes_gate_pass", False) and
            time.time() - getattr(system.state, "clothes_gate_time", 0.0) <= 0.5
        )
        if not clothes_gate_ok:
            LOGGER.info(
                f"[服裝硬閘] staff_id={staff_id} 已辨識但未同時通過背心與安全帽，取消刷入/開門"
            )
            return leave

    LOGGER.info(f"check_in_out: staff_id={staff_id}, camera_num={camera_num}, n_single={n}, schedule_active={schedule_active}, in_action={is_check_in_action}, out_action={is_check_out_action}")

    # 執行簽到
    if is_check_in_action:
        log_metrics(staff_name, camera_num, confidence, action='enter')
        # [2026-02-10 Feature] Sync direction to GlobalState for file saving
        if system.state.last_direction: 
            system.state.last_direction[camera_num] = "In"
            LOGGER.info(f"DEBUG: Setting last_direction[{camera_num}] to In")
        
        async_api_call(
            func=API.face_recognition_in,
            args=(staff_id,),
            callback=log_api_result,
            system=system,
            staff_id=staff_id,
            action='in'
        )
        # 語音播報：簽到成功為最高優先權 (Priority=1)，可插播提示語音，但會排隊等待其他簽到語音
        system.speaker.say(f"{staff_name}{CONFIG['say']['in']}", staff_name + "_in", priority=1, token=staff_id)

    # 執行簽離
    elif is_check_out_action:
        log_metrics(staff_name, camera_num, confidence, action='exit')
        # [2026-02-10 Feature] Sync direction to GlobalState for file saving
        if system.state.last_direction: 
            system.state.last_direction[camera_num] = "Out"
            LOGGER.info(f"DEBUG: Setting last_direction[{camera_num}] to Out")

        async_api_call(
            func=API.face_recognition_out,
            args=(staff_id,),
            callback=log_api_result,
            system=system,
            staff_id=staff_id,
            action='out'
        )
        # 語音播報：簽離成功為最高優先權 (Priority=1)
        system.speaker.say(f"{staff_name}{CONFIG['say']['out']}", staff_name + "_out", priority=1, token=staff_id)

    if CONFIG.get("excel_api_enabled", False) and "demosite" in CONFIG["Server"]["API_url"]:
        threading.Timer(1, check_in_out_excel, (staff_name,)).start()

    # 開門控制
    if CONFIG["door"] != "0":
        threading.Timer(0, open_door).start()

    # 原本的離開判斷保留，雖在主流程未被使用，但保持結構完整
    # [2026-01-20 Fix] 防止 Race Condition 導致 KeyError
    # 若在執行到此處時 staff_id 被其他執行緒(如 clear_leave_employee)刪除，則視為初始狀態
    last_time = system.state.check_time.get(staff_id, [True, 0])[1]
    if (now - last_time) >= 2:
        leave = 1

    return leave

def check_in_out_qrcode(system, verification, staff_id, camera_num):
    """
    處理 QR Code 刷入刷出邏輯。
    
    :param system: 系統物件
    :param verification: 6碼驗證碼
    :param staff_id: 人員 ID
    :param camera_num: 攝影機編號 (0=In, 1=Out)
    """
    # [2026-02-05 Fix] 確保人員狀態已初始化 (用於單鏡頭自動切換)
    if not staff_id in system.state.check_time.keys():
        system.state.check_time[staff_id] = [True, 0] # [True=可進, LastTime]

    # 判斷是否為單鏡頭模式
    ips = [CONFIG["cameraIP"]["in_camera"], CONFIG["cameraIP"]["out_camera"]]
    is_single_cam = (ips[0] == ips[1])

    # 判斷進出方向
    direction = "exit"
    
    # 1. 排程優先 (Schedule)
    schedule_conf = CONFIG.get("Schedule", {})
    is_scheduled_mode = False
    
    if schedule_conf.get("enabled", False) and is_single_cam:
        try:
            now_time = datetime.datetime.now().time()
            periods = schedule_conf.get("in_periods", [])
            if not periods:
                start_str = schedule_conf.get("in_start", "06:00")
                end_str = schedule_conf.get("in_end", "17:00")
                periods = [{"start": start_str, "end": end_str}]
            
            is_in_period = False
            for period in periods:
                s_str = period.get("start", "00:00")
                e_str = period.get("end", "00:00")
                s_str = s_str if s_str.count(':') == 2 else s_str + ':00'
                e_str = e_str if e_str.count(':') == 2 else e_str + ':00'
                start_time = datetime.datetime.strptime(s_str, "%H:%M:%S").time()
                end_time = datetime.datetime.strptime(e_str, "%H:%M:%S").time()
                if start_time <= end_time:
                    if start_time <= now_time <= end_time: is_in_period = True; break
                else:
                    if start_time <= now_time or now_time <= end_time: is_in_period = True; break
            
            if is_in_period: direction = "enter"
            else: direction = "exit"
            is_scheduled_mode = True
        except: pass
    elif schedule_conf.get("enabled", False):
        LOGGER.debug(f"Schedule ignored in dual-camera QR mode for camera_num={camera_num}")
    
    # 2. 自動切換 / 鏡頭判斷
    if not is_scheduled_mode:
        if is_single_cam:
            # 單鏡頭自動切換 (Auto Toggle)
            # check_time[0] == True 表示 "在外面/可進" -> Enter
            # check_time[0] == False 表示 "在裡面/可出" -> Exit
            if system.state.check_time[staff_id][0]:
                direction = "enter"
            else:
                direction = "exit"
        else:
            # 雙鏡頭固定位
            if camera_num == 0:
                direction = "enter"
            elif camera_num == 1:
                direction = "exit"
            else:
                LOGGER.warning(f"Unexpected camera_num {camera_num} in double-camera QR mode; defaulting to exit")
                direction = "exit"

    LOGGER.info(f"check_in_out_qrcode: staff_id={staff_id}, camera_num={camera_num}, single_cam={is_single_cam}, schedule_active={is_scheduled_mode}, direction={direction}")

    # 準備 API 資料
    data = {
        "verification": verification,
        "staff_id": staff_id,
        "location": int(CONFIG["Server"]["location_ID"]),
        "direction": direction
    }
    
    LOGGER.info(f"[QRCode] 掃描到 {staff_id} ({verification}), 方向: {direction} (SingleCam:{is_single_cam})")
    
    # 非同步呼叫 API
    def api_task():
        url = f"{CONFIG['Server']['API_url']}/accesses/qrcode_logs/"
        try:
            # 這裡不使用 async_api_call 因為它綁定了很多臉辨的 callback
            # 我們直接用 requests
            res = requests.post(url, json=data, timeout=5)
            if res.status_code in [200, 201, 202]:
                LOGGER.info(f"[QRCode] 上傳成功: {res.status_code}")
                
                    # [2026-02-05 Fix] 成功後更新人員狀態 (Check Time)
                # 這對於單鏡頭自動切換至關重要
                now = time.time()
                try:
                    if direction == 'enter':
                        system.state.check_time[staff_id] = [False, now] # 設為 "已在內"
                        if system.state.last_direction: system.state.last_direction[camera_num] = "In"
                    elif direction == 'exit':
                        system.state.check_time[staff_id][1] = now
                        if system.state.last_direction: system.state.last_direction[camera_num] = "Out"
                        # 延遲重置 (模擬離開)
                        threading.Timer(5, clear_leave_employee, (system, staff_id)).start()
                    LOGGER.info(f"[QRCode] 更新人員 {staff_id} 狀態為 {direction}")

                    
                    # [2026-02-05 Feature] 觸發 UI 顯示 (顯示大頭貼與姓名)
                    # 需找到對應的 CameraSystem 並更新其 Comparison 狀態
                    if hasattr(system, 'cameras'):
                        for cam in system.cameras:
                            if cam.frame_num == camera_num:
                                # 呼叫 _update_display_state 以顯示人員資訊並設定 2秒自動清除
                                if hasattr(cam, 'compar'):
                                    cam.compar._update_display_state(staff_id)
                                break
                                
                except Exception as e:
                    LOGGER.error(f"[QRCode] 更新狀態失敗: {e}")

                # 取得人員姓名 (從 features_dict)
                staff_name = system.state.features_dict.get("id_name", {}).get(staff_id, staff_id)
                
                # 語音播報
                if direction == "enter":
                    system.speaker.say(f"{staff_name}{CONFIG['say']['in']}", f"qr_{staff_id}_in", priority=1)
                else:
                    system.speaker.say(f"{staff_name}{CONFIG['say']['out']}", f"qr_{staff_id}_out", priority=1)
            else:
                LOGGER.error(f"[QRCode] 上傳失敗: {res.status_code} - {res.text}")
                system.speaker.say("驗證失敗", "qr_fail", priority=1)
                
        except Exception as e:
            LOGGER.error(f"[QRCode] API 錯誤: {e}")
            system.speaker.say("連線失敗", "qr_error", priority=1)

    threading.Thread(target=api_task).start()

def check_in_out_excel(staff_name):
    """
    發送 HTTP POST 請求至 Excel attendance API 用於 demo 匯入。

    :param staff_name: 簽到人員的名字
    """
    url = f"http://{CONFIG['ip_set']['ip_address']}:8080/attendance-record"
    data = {"name": staff_name, "time": datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S"), "location": "gini"}
    
    try:
        response = requests.post(url, json=data, timeout=5) # Timeout set to 5 seconds
        print("excel", response.status_code)
    except requests.exceptions.RequestException as e:
        print(f"Excel匯入失敗: {e}")
        LOGGER.error(f"Excel匯入失敗: {e}")

def open_door():
    """
    透過設定的門禁 IP 組成完整 URL，觸發開門操作，並寫入 log。
    URL 固定格式: http://{門禁裝置IP}:1880/open_door
    """
    door_val = CONFIG["door"]
    # 向下相容：若使用者存的是完整 URL 就直接用，否則視為 IP 自動組裝
    if door_val.startswith("http"):
        url = door_val
    else:
        url = f"http://{door_val}:1880/open_door"
    try:
        r = requests.get(url, timeout=5)
        LOGGER.info(f"{time.time()} : 開門 {r} (URL: {url})")
    except requests.exceptions.RequestException as e:
        LOGGER.error(f"開門失敗: {e} (URL: {url})")
        
def clear_leave_employee(system, staff_id):
    """
    在延遲一段時間後清除人員的 check_in/out 記錄，用於重置簽到狀態。

    :param system: 系統物件，需包含 state.check_time 結構
    :param staff_id: 人員 ID
    """
    if staff_id in system.state.check_time.keys():
        del system.state.check_time[staff_id]
        print(f"clear {staff_id}")
        LOGGER.info(f"clear {staff_id}")

def log_metrics(employee, camera_num, confidence=None, action=None):
    """
    將簽到或簽離的事件記錄進 LOGGER 並列印。

    :param employee: 人員名稱
    :param camera_num: 攝影機編號
    :param confidence: 辨識信賴度 (可選)
    :param action: 'enter' 或 'exit'，若提供則使用此方向描述
    """
    if action == 'enter':
        inoutType = "進入"
    elif action == 'exit':
        inoutType = "離開"
    else:
        inoutType = "進入" if camera_num == 0 else "離開" if camera_num == 1 else "?"

    log_cam_num = camera_num if camera_num in [0, 1] else camera_num
    conf_str = f", 信賴度: {confidence:.2%}" if confidence is not None else ""
    log_message = f"攝影機編號:{log_cam_num}, 人員:{employee} {inoutType}{conf_str}"
    print(log_message)
    LOGGER.info(log_message)


import threading

def diagnose_network():
    """
    診斷網路連線狀況，用於上傳失敗時釐清原因。
    測試目標: 
    1. 外部網路 (Google DNS 8.8.8.8)
    2. 專案伺服器 API
    """
    results = []
    
    # 1. Ping Google DNS (8.8.8.8) - 測試外網連通性
    # -c 1: 發送一次
    # -W 2: 等待2秒
    response = os.system("ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1")
    if response == 0:
        results.append("外部網路(8.8.8.8): 連通")
    else:
        results.append("外部網路(8.8.8.8): 無法連線")
        
    # 2. 測試伺服器連線 (使用 requests)
    server_url = CONFIG.get("Server", {}).get("API_url", "")
    if server_url:
        try:
            # 只測試連線，不在此乎回傳內容，設定短 timeout
            requests.get(server_url, timeout=3)
            results.append(f"伺服器({server_url}): 連通")
        except Exception as e:
            results.append(f"伺服器({server_url}): 無法連線 ({e})")
    else:
        results.append("伺服器URL未設定")
        
    return ", ".join(results)

def _apply_api_success(func, result, callback=None, system=None, staff_id=None, action=None):
    LOGGER.info(f"[{func.__name__}] 成功，取得 {result}")
    if callback:
        callback(result)

    if system and staff_id and action:
        now = time.time()
        try:
            if action == 'in':
                system.state.check_time[staff_id] = [False, now]
            elif action == 'out':
                if staff_id not in system.state.check_time:
                    system.state.check_time[staff_id] = [True, now]
                else:
                    system.state.check_time[staff_id][1] = now
                threading.Timer(5, clear_leave_employee, (system, staff_id)).start()
            LOGGER.info(f"人員 {staff_id} 狀態已在API成功後更新為 {action}")
        except Exception as e:
            LOGGER.error(f"API成功後更新人員 {staff_id} 狀態失敗: {e}")


def _load_api_queue_locked():
    if not os.path.exists(API_QUEUE_PATH):
        return []
    try:
        with open(API_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as e:
        LOGGER.error(f"讀取 API pending queue 失敗: {e}")
    return []


def _save_api_queue_locked(queue_items):
    os.makedirs(os.path.dirname(API_QUEUE_PATH), exist_ok=True)
    tmp_path = f"{API_QUEUE_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(queue_items, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, API_QUEUE_PATH)


def _prune_expired_api_calls(queue_items):
    now = time.time()
    kept, expired = [], []
    for item in queue_items:
        if item.get("expires_at", 0) <= now:
            expired.append(item)
        else:
            kept.append(item)
    if expired:
        LOGGER.warning(f"Discarded {len(expired)} expired pending API call(s).")
    return kept


def _is_non_retryable_api_status(status_code):
    return 400 <= status_code < 500 and status_code not in (408, 409, 425, 429)


def _resolve_api_func(func_name):
    func = getattr(API, func_name, None)
    if func is None:
        raise RuntimeError(f"Unknown API function: {func_name}")
    return func


def _resolve_api_callback(callback_name):
    if not callback_name:
        return None
    callback = globals().get(callback_name)
    if callback is None:
        LOGGER.warning(f"Unknown API callback ignored: {callback_name}")
    return callback


def _ensure_api_retry_worker(system):
    if system is None:
        return

    if not hasattr(system, "_api_retry_lock"):
        system._api_retry_lock = threading.Lock()

    worker = getattr(system, "_api_retry_thread", None)
    if worker and worker.is_alive():
        return

    def retry_worker():
        LOGGER.info("Starting persistent API FIFO queue worker...")
        while not getattr(system, "_shutdown_flag", False):
            item = None
            with system._api_retry_lock:
                queue_items = _prune_expired_api_calls(_load_api_queue_locked())
                if queue_items:
                    queue_items.sort(key=lambda x: x.get("created_at", 0))
                    item = queue_items[0]
                _save_api_queue_locked(queue_items)

            if item is None:
                time.sleep(1)
                continue

            try:
                func = _resolve_api_func(item["func_name"])
                result = func(*tuple(item.get("args", [])))
                if result in [201, 202]:
                    _apply_api_success(
                        func, result,
                        callback=_resolve_api_callback(item.get("callback_name")),
                        system=system,
                        staff_id=item.get("staff_id"),
                        action=item.get("action"),
                    )
                    with system._api_retry_lock:
                        queue_items = _load_api_queue_locked()
                        queue_items = [
                            q for q in queue_items if q.get("id") != item.get("id")]
                        _save_api_queue_locked(queue_items)
                    LOGGER.info(
                        f"[{item['func_name']}] pending API call flushed "
                        f"(created_at={item['created_at']:.0f}, attempts={item.get('attempts', 0) + 1})")
                    continue
                if _is_non_retryable_api_status(result):
                    with system._api_retry_lock:
                        queue_items = _load_api_queue_locked()
                        queue_items = [
                            q for q in queue_items if q.get("id") != item.get("id")]
                        _save_api_queue_locked(queue_items)
                    LOGGER.error(
                        f"[{item['func_name']}] discarded non-retryable API call "
                        f"(status={result}, staff_id={item.get('staff_id')}, "
                        f"action={item.get('action')}, args={item.get('args')})")
                    continue
                raise RuntimeError(f"API 回傳代碼 {result} (非預期的 201 或 202)")
            except Exception as e:
                attempts = int(item.get("attempts", 0)) + 1
                LOGGER.warning(
                    f"[{item.get('func_name')}] pending API call failed, will retry: {e}")
                with system._api_retry_lock:
                    queue_items = _load_api_queue_locked()
                    for q in queue_items:
                        if q.get("id") == item.get("id"):
                            q["attempts"] = attempts
                            q["last_error"] = str(e)
                            q["last_attempt_at"] = time.time()
                            break
                    _save_api_queue_locked(_prune_expired_api_calls(queue_items))
                time.sleep(5)

    system._api_retry_thread = threading.Thread(
        target=retry_worker, daemon=True, name="api-retry-queue")
    system._api_retry_thread.start()


def _enqueue_api_retry(func, args=(), callback=None, system=None, staff_id=None, action=None):
    if system is None:
        return
    _ensure_api_retry_worker(system)
    now = time.time()
    item = {
        "id": uuid.uuid4().hex,
        "func_name": func.__name__,
        "args": list(args),
        "callback_name": callback.__name__ if callback else None,
        "staff_id": staff_id,
        "action": action,
        "created_at": now,
        "expires_at": now + API_QUEUE_TTL_SECONDS,
        "attempts": 0,
        "last_error": None,
    }
    with system._api_retry_lock:
        queue_items = _prune_expired_api_calls(_load_api_queue_locked())
        queue_items.append(item)
        queue_items.sort(key=lambda x: x.get("created_at", 0))
        _save_api_queue_locked(queue_items)
        queue_size = len(queue_items)
    LOGGER.info(
        f"[{func.__name__}] API call queued "
        f"(staff_id={staff_id}, action={action}, queue_size={queue_size}, ttl=24h)")


def async_api_call(func, args=(), callback=None, max_retries=20, retry_delay=0.5, system=None, staff_id=None, action=None):
    """
    將 API 呼叫寫入持久化 FIFO 佇列，並由背景 worker 依序送出。
    
    :param func: 要執行的函數
    :param args: 傳給函數的參數 (tuple)
    :param callback: 成功時執行 callback(result)
    :param max_retries: 最大重試次數
    :param retry_delay: 每次重試間隔秒數
    :param system: 全域系統物件
    :param staff_id: 要更新狀態的人員ID
    :param action: 'in' 或 'out'，決定如何更新狀態
    """
    _enqueue_api_retry(
        func,
        args=args,
        callback=callback,
        system=system,
        staff_id=staff_id,
        action=action,
    )

def log_api_result(res):
    """
    將 API 回傳結果寫入日誌與列印。

    :param res: API 回傳的結果(通常是 HTTP status code)
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now_str} 上傳資料庫結果: {res}")
    LOGGER.info(f"{now_str} 上傳資料庫結果:" + str(res))

def fixed_image_standardization(image_tensor):
    processed_tensor = (image_tensor - 127.5) / 128.0
    return processed_tensor

def crop_face_without_forehead(image, box, points, image_size=160):
    """
    Option SMALL (Method J) Implementation:
    - Square Crop (2.0 * Eye Distance)
    - Centered on Eyes/Nose
    - Square Padding (Black Background)
    - No Mask
    - Area Resize
    """
    # Convert PIL to Numpy (RGB)
    img_np = np.array(image)
    
    # Points: 0=LE, 1=RE, 2=Nose
    le = points[0]
    re = points[1]
    nose = points[2]
    
    # Calculate Geometry
    eye_dist = np.linalg.norm(le - re)
    
    eye_center = (le + re) / 2
    t_center_x = (eye_center[0] + nose[0]) / 2
    t_center_y = (eye_center[1] + nose[1]) / 2
    
    # Shift center down 0.2 * eye_dist
    t_center_y += eye_dist * 0.2
    
    # Crop Size: 2.0 * Eye Distance
    size = int(eye_dist * 2.0)
    
    x1 = int(t_center_x - size/2)
    y1 = int(t_center_y - size/2)
    x2 = x1 + size
    y2 = y1 + size
    
    # Square Padding Logic
    h, w = img_np.shape[:2]
    square_img = np.zeros((size, size, 3), dtype=np.uint8)
    
    src_x1 = max(0, x1); src_y1 = max(0, y1)
    src_x2 = min(w, x2); src_y2 = min(h, y2)
    
    dst_x1 = src_x1 - x1; dst_y1 = src_y1 - y1
    dst_x2 = dst_x1 + (src_x2 - src_x1); dst_y2 = dst_y1 + (src_y2 - src_y1)
    
    if src_x2 > src_x1 and src_y2 > src_y1:
        square_img[dst_y1:dst_y2, dst_x1:dst_x2] = img_np[src_y1:src_y2, src_x1:src_x2]
        
    # Resize (Area Interpolation)
    img_resized = cv2.resize(square_img, (image_size, image_size), interpolation=cv2.INTER_AREA)
    
    # To Tensor & Standardize
    face_tensor = torch.from_numpy(img_resized).permute(2, 0, 1).float()
    standardized_face = fixed_image_standardization(face_tensor)
    
    return standardized_face

# [2026-01-19 Feature] Part-Based Cropping
def crop_and_pad(img, cx, cy, w, h, target_size=160):
    """Helper to crop a region and resize it to target size."""
    x1 = int(cx - w/2)
    y1 = int(cy - h/2)
    x2 = int(cx + w/2)
    y2 = int(cy + h/2)
    
    # Crop (PIL handles out of bounds by padding with 0 if using proper method, 
    # but crop() just clamps. We want context.)
    crop = img.crop((x1, y1, x2, y2))
    crop = crop.resize((target_size, target_size), Image.Resampling.BILINEAR)
    return crop

def get_parts_crop(image_pil, landmarks):
    """
    Crop Eye, Nose, Mouth regions based on MediaPipe landmarks.
    Returns:
        parts_tensors (dict): {'eye': tensor, ...} ready for ResNet.
        parts_coords (dict): {'eye': [cx, cy, w, h], ...} for logging/debugging.
    
    Landmarks indices (5 points from MediaPipeHandler):
    0=L_Eye, 1=R_Eye, 2=Nose, 3=L_Mouth, 4=R_Mouth
    """
    parts_tensors = {}
    parts_coords = {}
    
    # [2026-01-20 New Feature] T-Zone Long Crop (Eyebrows + Eyes + Nose + Philtrum)
    # Replaces individual Eye/Nose/Mouth checks for better stability against expression/glasses.
    # Center: Midpoint between Eye-Center and Nose
    eye_center_x = (landmarks[0][0] + landmarks[1][0]) / 2
    eye_center_y = (landmarks[0][1] + landmarks[1][1]) / 2
    nose_x, nose_y = landmarks[2]
    
    eye_dist = np.linalg.norm(landmarks[0] - landmarks[1])
    
    t_center_x = (eye_center_x + nose_x) / 2
    t_center_y = (eye_center_y + nose_y) / 2
    
    # Shift center down slightly for "Long" version to include Philtrum without cutting forehead
    t_long_cy = t_center_y + eye_dist * 0.2
    
    # Dimensions: 2.0x EyeDist Width, 3.0x EyeDist Height
    crop_w = eye_dist * 2.0
    crop_h = eye_dist * 3.0
    
    parts_tensors['t_zone'] = _process_part_tensor(crop_and_pad(image_pil, t_center_x, t_long_cy, crop_w, crop_h))
    parts_coords['t_zone'] = [float(x) for x in [t_center_x, t_long_cy, crop_w, crop_h]]
    
    return parts_tensors, parts_coords

def _process_part_tensor(img_pil):
    """Standardize a part crop to tensor."""
    # No extra sharpening for parts (keep it raw)
    face_tensor = torch.from_numpy(np.array(img_pil)).permute(2, 0, 1).float()
    processed_tensor = (face_tensor - 127.5) / 128.0
    return processed_tensor


def _clip_roi(x1, y1, x2, y2, frame_w, frame_h):
    x1 = max(0, min(frame_w, int(round(x1))))
    y1 = max(0, min(frame_h, int(round(y1))))
    x2 = max(0, min(frame_w, int(round(x2))))
    y2 = max(0, min(frame_h, int(round(y2))))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _region_from_points(points, frame_w, frame_h, pad_x, pad_y):
    if points is None or len(points) == 0:
        return None
    pts = np.asarray(points, dtype=np.float32)
    x1 = float(np.min(pts[:, 0])) - pad_x
    y1 = float(np.min(pts[:, 1])) - pad_y
    x2 = float(np.max(pts[:, 0])) + pad_x
    y2 = float(np.max(pts[:, 1])) + pad_y
    return _clip_roi(x1, y1, x2, y2, frame_w, frame_h)


def _image_region_stats(frame, rect):
    if rect is None:
        return {
            "valid": False,
            "mean_y": 0.0,
            "std_y": 0.0,
            "dark_ratio": 0.0,
            "very_dark_ratio": 0.0,
            "bright_ratio": 0.0,
            "specular_ratio": 0.0,
            "laplacian": 0.0,
            "edge_density": 0.0,
            "saturation_mean": 0.0,
        }
    x1, y1, x2, y2 = rect
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return _image_region_stats(frame, None)

    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[:, :, 0]
    cr = ycrcb[:, :, 1]
    cb = ycrcb[:, :, 2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    edges = cv2.Canny(gray, 40, 120)
    skin_mask = (
        (y > 45) &
        (cr > 120) & (cr < 180) &
        (cb > 70) & (cb < 150)
    )

    return {
        "valid": True,
        "mean_y": float(np.mean(y)),
        "std_y": float(np.std(y)),
        "dark_ratio": float(np.mean(y < 60)),
        "very_dark_ratio": float(np.mean(y < 38)),
        "bright_ratio": float(np.mean(y > 220)),
        "specular_ratio": float(np.mean(y > 245)),
        "laplacian": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "edge_density": float(np.mean(edges > 0)),
        "saturation_mean": float(np.mean(hsv[:, :, 1])),
        "skin_ratio": float(np.mean(skin_mask)),
    }


def _parse_gaze_status(gaze_status):
    parsed = {
        "passed": None,
        "msg": "",
        "pitch": 0.0,
        "yaw": 0.0,
        "roll": 0.0,
        "ear": None,
    }
    if not gaze_status:
        return parsed

    try:
        parsed["passed"] = bool(gaze_status[0])
        parsed["msg"] = str(gaze_status[1]) if len(gaze_status) > 1 else ""
        if len(gaze_status) >= 3 and gaze_status[2] is not None:
            pitch, yaw, roll = gaze_status[2]
            parsed["pitch"] = float(pitch)
            parsed["yaw"] = float(yaw)
            parsed["roll"] = float(roll)
        if len(gaze_status) >= 4 and gaze_status[3] is not None:
            parsed["ear"] = float(gaze_status[3])
    except Exception:
        pass
    return parsed


def analyze_eye_occlusion(frame, mesh_points=None, box=None, gaze_status=None):
    """
    Detect strong eye-area occlusion while allowing clear eyeglasses.
    Dark sunglasses / opaque goggles count as occlusion by policy.
    """
    result = {
        "enabled": True,
        "has_mesh": False,
        "eye_occluded": False,
        "reason": "no_mesh",
        "occluded_eye_side": "",
        "clear_glasses_exempt": False,
    }

    try:
        if frame is None or mesh_points is None:
            return result

        mesh = np.asarray(mesh_points, dtype=np.float32)
        if mesh.ndim != 2 or mesh.shape[0] < 478 or mesh.shape[1] < 2:
            result["reason"] = "insufficient_mesh"
            return result

        frame_h, frame_w = frame.shape[:2]
        left_idx = [33, 133, 159, 145, 160, 158, 153, 144, 468, 469, 470, 471, 472]
        right_idx = [263, 362, 386, 374, 387, 385, 380, 373, 473, 474, 475, 476, 477]
        left_pts = mesh[left_idx]
        right_pts = mesh[right_idx]

        left_outer = mesh[33]
        right_outer = mesh[263]
        eye_dist = float(np.linalg.norm(left_outer - right_outer))
        if eye_dist <= 0:
            result["reason"] = "invalid_eye_distance"
            return result

        pad_x = max(8.0, eye_dist * 0.08)
        pad_y = max(8.0, eye_dist * 0.12)
        left_rect = _region_from_points(left_pts, frame_w, frame_h, pad_x, pad_y)
        right_rect = _region_from_points(right_pts, frame_w, frame_h, pad_x, pad_y)
        band_rect = _region_from_points(
            np.vstack([left_pts, right_pts]), frame_w, frame_h,
            max(12.0, eye_dist * 0.12), max(10.0, eye_dist * 0.16)
        )

        if box is None:
            face_rect = _region_from_points(mesh[[10, 152, 234, 454]], frame_w, frame_h, eye_dist * 0.10, eye_dist * 0.10)
        else:
            x1, y1, x2, y2 = box
            face_rect = _clip_roi(x1, y1, x2, y2, frame_w, frame_h)
        face_box_w = float(face_rect[2] - face_rect[0]) if face_rect is not None else 0.0

        def _subject_side_for_rect(rect):
            if rect is None or face_rect is None:
                return ""
            eye_cx = (rect[0] + rect[2]) / 2.0
            face_cx = (face_rect[0] + face_rect[2]) / 2.0
            # Camera image left is the subject's right eye, and vice versa.
            return "right" if eye_cx < face_cx else "left"

        left_subject_side = _subject_side_for_rect(left_rect)
        right_subject_side = _subject_side_for_rect(right_rect)

        left_stats = _image_region_stats(frame, left_rect)
        right_stats = _image_region_stats(frame, right_rect)
        band_stats = _image_region_stats(frame, band_rect)
        face_stats = _image_region_stats(frame, face_rect)

        result.update({
            "has_mesh": True,
            "reason": "pass",
            "left_eye": left_stats,
            "right_eye": right_stats,
            "eye_band": band_stats,
            "face_region": face_stats,
            "eye_distance_px": eye_dist,
            "face_box_width_px": face_box_w,
            "left_eye_subject_side": left_subject_side,
            "right_eye_subject_side": right_subject_side,
        })

        if not (left_stats["valid"] and right_stats["valid"] and band_stats["valid"] and face_stats["valid"]):
            result["reason"] = "invalid_roi"
            return result

        left_dark = left_stats["dark_ratio"] > 0.42 and left_stats["mean_y"] < 90
        right_dark = right_stats["dark_ratio"] > 0.42 and right_stats["mean_y"] < 90
        both_eyes_dark = left_dark and right_dark
        gaze = _parse_gaze_status(gaze_status)
        face_mean = face_stats["mean_y"]
        eye_mean = band_stats["mean_y"]
        eye_darker_than_face = face_mean >= 70 and eye_mean < max(78.0, face_mean * 0.72)

        dark_cover_resolution_ok = face_box_w >= 520.0 and eye_dist > 320.0
        sunglasses_resolution_ok = face_box_w >= 430.0 and eye_dist > 260.0
        small_sunglasses_resolution_ok = face_box_w >= 360.0 and eye_dist > 220.0

        opaque_eye_cover = (
            dark_cover_resolution_ok and
            both_eyes_dark and
            eye_darker_than_face and
            band_stats["dark_ratio"] > 0.45 and
            (band_stats["edge_density"] < 0.015 or band_stats["dark_ratio"] > 0.65) # [2026-06-09 Fix] Avoid dark real eyes misfire
        )
        very_dark_eye_cover = (
            dark_cover_resolution_ok and
            both_eyes_dark and
            face_mean >= 60 and
            band_stats["very_dark_ratio"] > 0.28 and
            eye_mean < 75
        )
        dark_lens_texture = (
            band_stats["edge_density"] > 0.018 or
            band_stats["very_dark_ratio"] > 0.18 or
            min(left_stats["very_dark_ratio"], right_stats["very_dark_ratio"]) > 0.12
        )
        dark_sunglasses_or_goggles = (
            sunglasses_resolution_ok and
            both_eyes_dark and
            face_mean >= 75 and
            eye_darker_than_face and
            band_stats["dark_ratio"] > 0.52 and
            band_stats["skin_ratio"] < max(0.78, face_stats["skin_ratio"] - 0.08) and
            dark_lens_texture
        )

        visible_clear_glasses = (
            not both_eyes_dark and
            band_stats["specular_ratio"] > 0.003 and
            band_stats["edge_density"] > 0.020 and
            band_stats["dark_ratio"] < 0.40
        )
        result["clear_glasses_exempt"] = bool(visible_clear_glasses)
        small_dark_sunglasses_or_goggles = (
            not visible_clear_glasses and
            small_sunglasses_resolution_ok and
            both_eyes_dark and
            face_mean >= 55 and
            band_stats["dark_ratio"] > 0.58 and
            band_stats["very_dark_ratio"] > 0.42 and
            band_stats["skin_ratio"] < max(0.55, face_stats["skin_ratio"] - 0.18) and
            min(left_stats["dark_ratio"], right_stats["dark_ratio"]) > 0.58 and # [2026-06-10 Fix] Revert to 0.58 to spare clear glasses with glare
            min(left_stats["very_dark_ratio"], right_stats["very_dark_ratio"]) > 0.32 and
            band_stats["edge_density"] > 0.012
        )
        result["small_dark_sunglasses_or_goggles"] = bool(
            small_dark_sunglasses_or_goggles)

        skin_colored_eye_cover = (
            gaze["ear"] is not None and gaze["ear"] < 0.20 and
            face_box_w >= 430.0 and
            eye_dist > 150 and
            band_stats["skin_ratio"] > 0.94 and
            band_stats["skin_ratio"] - face_stats["skin_ratio"] > 0.10 and
            left_stats["skin_ratio"] > 0.88 and
            right_stats["skin_ratio"] > 0.88 and
            eye_mean > face_mean + 4.0 and
            band_stats["dark_ratio"] < 0.12 and
            band_stats["std_y"] < 25.0 and
            band_stats["edge_density"] < 0.016 and
            band_stats["specular_ratio"] < 0.003
        )
        skin_colored_side_eye_cover = (
            gaze["passed"] is False and
            ("未正視" in gaze["msg"] or abs(gaze["yaw"]) > 25.0) and
            face_box_w >= 430.0 and
            eye_dist > 150 and
            band_stats["skin_ratio"] > 0.94 and
            left_stats["skin_ratio"] > 0.88 and
            right_stats["skin_ratio"] > 0.88 and
            band_stats["dark_ratio"] < 0.12 and
            band_stats["std_y"] < 16.0 and
            band_stats["edge_density"] < 0.006 and
            left_stats["edge_density"] < 0.006 and
            right_stats["edge_density"] < 0.006 and
            band_stats["specular_ratio"] < 0.003
        )
        left_skin_shaded = (
            left_stats["skin_ratio"] > 0.65 and # [2026-06-09 Fix v5] Real uncovered eye might have lower skin ratio
            left_stats["dark_ratio"] > 0.35 and # [2026-06-10 Fix] Relax from 0.20 to avoid catching normal eye shadows
            left_stats["very_dark_ratio"] > 0.06 and
            left_stats["mean_y"] < face_mean - 5.0
        )
        right_skin_shaded = (
            right_stats["skin_ratio"] > 0.65 and # [2026-06-09 Fix v5]
            right_stats["dark_ratio"] > 0.35 and # [2026-06-10 Fix]
            right_stats["very_dark_ratio"] > 0.06 and
            right_stats["mean_y"] < face_mean - 5.0
        )
        skin_colored_single_eye_cover = (
            face_box_w >= 430.0 and
            eye_dist > 150 and
            band_stats["skin_ratio"] > 0.85 and # [2026-06-09 Fix v5] Reduce from 0.88
            band_stats["skin_ratio"] - face_stats["skin_ratio"] > 0.08 and # [2026-06-10 Fix] Relax from 0.03 to 0.08
            band_stats["dark_ratio"] < 0.25 and # [2026-06-10 Fix] Relax from 0.22
            band_stats["specular_ratio"] < 0.003 and
            (
                abs(left_stats["mean_y"] - right_stats["mean_y"]) > 16.0 or
                abs(left_stats["dark_ratio"] - right_stats["dark_ratio"]) > 0.20
            ) and
            (left_skin_shaded ^ right_skin_shaded)
        )
        skin_colored_double_eye_cover = (
            face_box_w >= 430.0 and
            eye_dist > 150 and
            band_stats["skin_ratio"] > 0.975 and
            band_stats["skin_ratio"] - face_stats["skin_ratio"] > 0.16 and
            eye_mean > face_mean + 16.0 and
            band_stats["dark_ratio"] < 0.04 and
            band_stats["edge_density"] < 0.015 and
            band_stats["specular_ratio"] < 0.003
        )
        horizontal_skin_palm_double_eye_cover = (
            not visible_clear_glasses and
            face_box_w >= 430.0 and
            eye_dist > 240.0 and
            face_stats["skin_ratio"] > 0.70 and
            band_stats["skin_ratio"] > 0.95 and
            band_stats["skin_ratio"] - face_stats["skin_ratio"] > 0.12 and  # [2026-06-09 Fix] 0.08→0.12：正常膚色眼區不需如此寬鬆的diff門檻
            left_stats["skin_ratio"] > 0.90 and
            right_stats["skin_ratio"] > 0.90 and
            eye_mean > face_mean + 8.0 and
            band_stats["dark_ratio"] < 0.16 and
            band_stats["very_dark_ratio"] < 0.03 and
            band_stats["edge_density"] < 0.022 and
            band_stats["laplacian"] < 12.0 and  # [2026-06-09 Fix] 18→12：收緊清晰度，避免自然膚色誤判
            min(left_stats["laplacian"], right_stats["laplacian"]) < 14.0 and
            band_stats["specular_ratio"] < 0.003
        )

        def _single_eye_skin_cover(eye_stats, other_stats):
            return (
                face_box_w >= 430.0 and
                eye_dist > 260.0 and
                face_stats["skin_ratio"] > 0.70 and
                (
                    other_stats["edge_density"] > 0.002 or
                    other_stats["laplacian"] > 8.0
                ) and
                other_stats["skin_ratio"] > 0.88 and
                other_stats["dark_ratio"] < 0.12 and
                eye_stats["skin_ratio"] > 0.74 and
                eye_stats["skin_ratio"] < other_stats["skin_ratio"] - 0.08 and
                eye_stats["mean_y"] < other_stats["mean_y"] - 18.0 and
                eye_stats["dark_ratio"] > max(0.26, other_stats["dark_ratio"] + 0.18) and
                eye_stats["very_dark_ratio"] > other_stats["very_dark_ratio"] + 0.05 and
                eye_stats["saturation_mean"] > other_stats["saturation_mean"] + 8.0 and
                eye_stats["saturation_mean"] > face_stats["saturation_mean"] + 8.0 and
                eye_stats["specular_ratio"] < 0.003
            )

        left_single_eye_skin_cover = _single_eye_skin_cover(left_stats, right_stats)
        right_single_eye_skin_cover = _single_eye_skin_cover(right_stats, left_stats)

        def _side_angle_single_eye_skin_cover(eye_stats, other_stats):
            return False

        left_side_angle_eye_skin_cover = (
            not left_single_eye_skin_cover and
            _side_angle_single_eye_skin_cover(left_stats, right_stats)
        )
        right_side_angle_eye_skin_cover = (
            not right_single_eye_skin_cover and
            _side_angle_single_eye_skin_cover(right_stats, left_stats)
        )

        def _bright_single_eye_skin_cover(eye_stats, other_stats):
            return (
                face_box_w >= 430.0 and
                eye_dist > 240.0 and
                face_stats["skin_ratio"] > 0.75 and
                band_stats["skin_ratio"] > 0.93 and
                band_stats["dark_ratio"] < 0.12 and
                band_stats["specular_ratio"] < 0.003 and
                eye_stats["skin_ratio"] > 0.95 and
                other_stats["skin_ratio"] > 0.82 and
                eye_stats["skin_ratio"] > other_stats["skin_ratio"] + 0.07 and
                eye_stats["mean_y"] > face_mean + 10.0 and
                eye_stats["mean_y"] > other_stats["mean_y"] + 22.0 and
                eye_stats["dark_ratio"] < 0.08 and
                other_stats["dark_ratio"] > 0.14 and
                eye_stats["saturation_mean"] < other_stats["saturation_mean"] - 10.0 and
                eye_stats["edge_density"] < 0.026 and
                eye_stats["specular_ratio"] < 0.003
            )

        left_bright_single_eye_skin_cover = _bright_single_eye_skin_cover(
            left_stats, right_stats)
        right_bright_single_eye_skin_cover = _bright_single_eye_skin_cover(
            right_stats, left_stats)
        independent_double_eye_cover = (
            face_box_w >= 430.0 and
            eye_dist > 260.0 and
            face_stats["skin_ratio"] > 0.75 and
            face_stats["specular_ratio"] < 0.003 and
            face_stats["edge_density"] < 0.025 and
            left_stats["skin_ratio"] > 0.82 and
            right_stats["skin_ratio"] > 0.82 and
            left_stats["dark_ratio"] > 0.24 and
            right_stats["dark_ratio"] > 0.24 and
            band_stats["dark_ratio"] > 0.16 and
            band_stats["skin_ratio"] > face_stats["skin_ratio"] + 0.11 and
            band_stats["saturation_mean"] > face_stats["saturation_mean"] + 8.0 and
            abs(left_stats["mean_y"] - right_stats["mean_y"]) < 18.0 and
            band_stats["edge_density"] < 0.020 and
            band_stats["specular_ratio"] < 0.003
        )
        non_skin_double_eye_cover = (
            not visible_clear_glasses and
            face_box_w >= 400.0 and
            eye_dist > 250.0 and
            face_stats["skin_ratio"] > 0.45 and
            band_stats["skin_ratio"] < face_stats["skin_ratio"] - 0.17 and
            band_stats["skin_ratio"] < 0.52 and
            left_stats["skin_ratio"] < 0.72 and
            right_stats["skin_ratio"] < 0.72 and
            band_stats["edge_density"] > max(0.045, face_stats["edge_density"] * 1.25) and
            band_stats["laplacian"] > max(28.0, face_stats["laplacian"] * 1.00) and
            band_stats["specular_ratio"] < 0.003
        )

        shadowed_but_skin_visible = (
            band_stats["skin_ratio"] > 0.50 and
            face_stats["skin_ratio"] > 0.45 and
            band_stats["very_dark_ratio"] < 0.35 and
            band_stats["edge_density"] < 0.030 and
            band_stats["specular_ratio"] < 0.003
        )

        if (
            skin_colored_eye_cover or
            skin_colored_side_eye_cover or
            skin_colored_single_eye_cover or
            skin_colored_double_eye_cover or
            left_single_eye_skin_cover or
            right_single_eye_skin_cover or
            left_side_angle_eye_skin_cover or
            right_side_angle_eye_skin_cover or
            left_bright_single_eye_skin_cover or
            right_bright_single_eye_skin_cover or
            horizontal_skin_palm_double_eye_cover or
            independent_double_eye_cover or
            non_skin_double_eye_cover
        ):
            result["eye_occluded"] = True
            if left_single_eye_skin_cover:
                result["reason"] = "left_eye_skin_cover"
                result["occluded_eye_side"] = "left"
            elif right_single_eye_skin_cover:
                result["reason"] = "right_eye_skin_cover"
                result["occluded_eye_side"] = "right"
            elif left_side_angle_eye_skin_cover:
                result["reason"] = "left_eye_side_angle_skin_cover"
                result["occluded_eye_side"] = "left"
            elif right_side_angle_eye_skin_cover:
                result["reason"] = "right_eye_side_angle_skin_cover"
                result["occluded_eye_side"] = "right"
            elif left_bright_single_eye_skin_cover:
                result["reason"] = "left_eye_bright_skin_cover"
                result["occluded_eye_side"] = left_subject_side or "left"
            elif right_bright_single_eye_skin_cover:
                result["reason"] = "right_eye_bright_skin_cover"
                result["occluded_eye_side"] = right_subject_side or "right"
            elif horizontal_skin_palm_double_eye_cover:
                result["reason"] = "horizontal_skin_palm_double_eye_cover"
                result["occluded_eye_side"] = "both"
            elif independent_double_eye_cover:
                result["reason"] = "both_eye_skin_cover"
                result["occluded_eye_side"] = "both"
            elif non_skin_double_eye_cover:
                result["reason"] = "non_skin_double_eye_cover"
                result["occluded_eye_side"] = "both"
            elif skin_colored_side_eye_cover:
                result["reason"] = "skin_colored_side_eye_cover"
            elif skin_colored_single_eye_cover:
                result["reason"] = "skin_colored_single_eye_cover"
                result["occluded_eye_side"] = (
                    (left_subject_side if left_skin_shaded else right_subject_side) or
                    ("left" if left_skin_shaded else "right")
                )
            elif skin_colored_double_eye_cover:
                result["reason"] = "skin_colored_double_eye_cover"
                result["occluded_eye_side"] = "both"
            else:
                result["reason"] = "skin_colored_eye_cover"
                result["occluded_eye_side"] = "both"
            return result

        if (
            opaque_eye_cover or
            very_dark_eye_cover or
            dark_sunglasses_or_goggles or
            small_dark_sunglasses_or_goggles
        ):
            result["eye_occluded"] = True
            if dark_sunglasses_or_goggles or small_dark_sunglasses_or_goggles:
                result["reason"] = "dark_sunglasses_or_opaque_goggles"
            elif very_dark_eye_cover:
                result["reason"] = "very_dark_eye_cover"
            else:
                result["reason"] = "opaque_eye_cover"
            result["occluded_eye_side"] = "both"

        if result["eye_occluded"]:
            return result

        if shadowed_but_skin_visible:
            result["reason"] = "shadowed_skin_visible"
            return result

        return result
    except Exception as e:
        LOGGER.error(f"Eye occlusion analysis failed: {e}")
        result["reason"] = "error"
        return result


def analyze_face_occlusion(frame, mesh_points=None, points=None, box=None, gaze_status=None, hand_boxes=None):
    """
    Detect meaningful facial-feature occlusion. Clear eyeglasses pass; opaque eye
    covers and lower-face cloth/masks reject.
    """
    if hand_boxes and box is not None:
        hx1, hy1, hx2, hy2 = box
        face_area = max(1, (hx2 - hx1) * (hy2 - hy1))
        
        for hand in hand_boxes:
            hx, hy, hxw, hyh = hand
            # Calculate intersection
            ix1 = max(hx1, hx)
            iy1 = max(hy1, hy)
            ix2 = min(hx2, hxw)
            iy2 = min(hy2, hyh)
            
            if ix1 < ix2 and iy1 < iy2:
                intersection = (ix2 - ix1) * (iy2 - iy1)
                iof = intersection / face_area
                if iof > 0.10:
                    return {
                        "enabled": True,
                        "eye_occlusion": {},
                        "lower_face_occluded": True,
                        "face_occluded": True,
                        "reason": "hand_block",
                    }
                    
    eye_metrics = analyze_eye_occlusion(
        frame, mesh_points=mesh_points, box=box, gaze_status=gaze_status)
    result = {
        "enabled": True,
        "eye_occlusion": eye_metrics,
        "lower_face_occluded": False,
        "face_occluded": bool(eye_metrics.get("eye_occluded", False)),
        "reason": eye_metrics.get("reason", "pass") if eye_metrics.get("eye_occluded", False) else "pass",
    }
    if result["face_occluded"]:
        return result

    try:
        if frame is None:
            result["reason"] = "no_frame"
            return result

        frame_h, frame_w = frame.shape[:2]
        mesh = None
        if mesh_points is not None:
            mesh = np.asarray(mesh_points, dtype=np.float32)
            if mesh.ndim != 2 or mesh.shape[0] < 478 or mesh.shape[1] < 2:
                mesh = None

        if box is None:
            if mesh is not None:
                face_rect = _region_from_points(mesh[[10, 152, 234, 454]], frame_w, frame_h, 8, 8)
            else:
                face_rect = None
        else:
            x1, y1, x2, y2 = box
            face_rect = _clip_roi(x1, y1, x2, y2, frame_w, frame_h)

        if mesh is not None:
            mouth_idx = [0, 13, 14, 17, 61, 78, 81, 82, 87, 88, 95, 146, 178, 191, 267, 291, 308, 310, 312, 317, 318, 324, 375, 402, 415]
            lower_idx = [1, 2, 4, 5, 17, 61, 78, 95, 98, 152, 164, 200, 291, 308, 324, 327, 364, 379, 397]
            nose_idx = [1, 2, 4, 5, 94, 98, 168, 195, 197, 327]
            eye_dist = float(np.linalg.norm(mesh[33] - mesh[263]))
            pad_x = max(10.0, eye_dist * 0.12)
            pad_y = max(10.0, eye_dist * 0.12)
            mouth_rect = _region_from_points(mesh[mouth_idx], frame_w, frame_h, pad_x, pad_y)
            lower_rect = _region_from_points(mesh[lower_idx], frame_w, frame_h, pad_x, max(14.0, eye_dist * 0.18))
            nose_rect = _region_from_points(mesh[nose_idx], frame_w, frame_h, max(8.0, eye_dist * 0.08), max(8.0, eye_dist * 0.08))
            eye_y = float((mesh[33][1] + mesh[263][1]) / 2.0)
            nose_y = float(mesh[1][1])
            mouth_y = float((mesh[61][1] + mesh[291][1]) / 2.0)
        elif points is not None and len(points) >= 5:
            pts = np.asarray(points, dtype=np.float32)
            eye_dist = float(np.linalg.norm(pts[0] - pts[1]))
            nose = pts[2]
            mouth_pts = pts[[3, 4]]
            mouth_rect = _region_from_points(mouth_pts, frame_w, frame_h, max(12.0, eye_dist * 0.18), max(12.0, eye_dist * 0.18))
            lower_rect = _region_from_points(np.vstack([nose, mouth_pts]), frame_w, frame_h, max(14.0, eye_dist * 0.22), max(16.0, eye_dist * 0.30))
            nose_rect = _region_from_points(np.asarray([nose]), frame_w, frame_h, max(10.0, eye_dist * 0.10), max(10.0, eye_dist * 0.10))
            eye_y = float((pts[0][1] + pts[1][1]) / 2.0)
            nose_y = float(nose[1])
            mouth_y = float(np.mean(mouth_pts[:, 1]))
        else:
            result["reason"] = "no_landmarks"
            return result

        lower_box_rect = None
        if face_rect is not None:
            fx1, fy1, fx2, fy2 = face_rect
            lower_start = min(fy2 - 1, max(fy1, int(round(mouth_y - max(6.0, eye_dist * 0.12)))))
            if mouth_y <= nose_y:
                lower_start = min(fy2 - 1, max(fy1, int(round(fy1 + (fy2 - fy1) * 0.58))))
            lower_box_rect = _clip_roi(fx1, lower_start, fx2, fy2, frame_w, frame_h)

        face_stats = _image_region_stats(frame, face_rect)
        mouth_stats = _image_region_stats(frame, mouth_rect)
        lower_stats = _image_region_stats(frame, lower_rect)
        lower_box_stats = _image_region_stats(frame, lower_box_rect)
        nose_stats = _image_region_stats(frame, nose_rect)
        eye_nose_dist = nose_y - eye_y
        local_v_ratio = ((mouth_y - nose_y) / eye_nose_dist) if eye_nose_dist > 0 else 0.0
        result.update({
            "face_region": face_stats,
            "mouth_region": mouth_stats,
            "lower_face_region": lower_stats,
            "lower_box_region": lower_box_stats,
            "nose_region": nose_stats,
            "local_v_ratio": float(local_v_ratio),
        })

        if not (face_stats["valid"] and mouth_stats["valid"] and lower_stats["valid"]):
            result["reason"] = "invalid_roi"
            return result

        face_mean = face_stats["mean_y"]
        face_box_w = float(face_rect[2] - face_rect[0]) if face_rect is not None else 0.0
        lower_mean = lower_stats["mean_y"]
        mouth_mean = mouth_stats["mean_y"]
        lower_box_mean = lower_box_stats["mean_y"]
        face_skin = face_stats["skin_ratio"]
        lower_skin = lower_stats["skin_ratio"]
        lower_box_skin = lower_box_stats["skin_ratio"]
        mouth_skin = mouth_stats["skin_ratio"]
        nose_skin = nose_stats["skin_ratio"] if nose_stats.get("valid", False) else 0.0
        gaze = _parse_gaze_status(gaze_status)
        gaze_msg = gaze["msg"]
        pose_occlusion_context = (
            "低頭" in gaze_msg or
            "未正視" in gaze_msg or
            abs(gaze["pitch"]) > 15.0 or
            abs(gaze["roll"]) > 20.0 or
            abs(gaze["yaw"]) > 15.0 or
            local_v_ratio < 0.42 or  # [2026-06-09 Fix] 0.55→0.42：0.42~0.55為正常微低頭，膚色完整不應觸發occlusion
            local_v_ratio > 1.50 or
            (gaze["ear"] is not None and gaze["ear"] < 0.11)
        )
        pose_unstable = (
            gaze["passed"] is False and (
                "低頭" in gaze_msg or
                "未正視" in gaze_msg or
                abs(gaze["pitch"]) > 15.0 or
                abs(gaze["roll"]) > 20.0 or
                abs(gaze["yaw"]) > 15.0
            )
        )
        generic_lower_resolution_ok = face_box_w >= 430.0 and eye_dist > 260.0
        severe_face_underexposed = (
            face_mean < 45.0 and
            face_stats["dark_ratio"] > 0.90 and
            face_stats["skin_ratio"] < 0.12
        )
        near_lens_foreground_occlusion = False
        near_lens_foreground_ratio = 0.0
        near_lens_foreground_component_ratio = 0.0
        if face_rect is not None and face_box_w >= 430.0:
            fx1, fy1, fx2, fy2 = face_rect
            fw = max(1, fx2 - fx1)
            fh = max(1, fy2 - fy1)
            side_rect = _clip_roi(
                fx1 - fw * 0.65, fy1 - fh * 0.10,
                fx1 + fw * 0.20, fy2 + fh * 0.05,
                frame_w, frame_h)
            if side_rect is not None:
                sx1, sy1, sx2, sy2 = side_rect
                side_roi = frame[sy1:sy2, sx1:sx2]
                if side_roi.size > 0:
                    side_ycrcb = cv2.cvtColor(side_roi, cv2.COLOR_BGR2YCrCb)
                    side_y = side_ycrcb[:, :, 0]
                    side_cr = side_ycrcb[:, :, 1]
                    side_cb = side_ycrcb[:, :, 2]
                    side_hsv = cv2.cvtColor(side_roi, cv2.COLOR_BGR2HSV)
                    side_sat = side_hsv[:, :, 1]
                    foreground_mask = (
                        (side_y > 35) & (side_y < 190) &
                        (side_cr > 135) & (side_cb < 135) &
                        (side_sat > 45)
                    )
                    near_lens_foreground_ratio = float(
                        np.mean(foreground_mask))
                    labels_count, _, component_stats, _ = cv2.connectedComponentsWithStats(
                        foreground_mask.astype(np.uint8), 8)
                    max_component_area = 0
                    for label_idx in range(1, labels_count):
                        max_component_area = max(
                            max_component_area,
                            int(component_stats[label_idx, cv2.CC_STAT_AREA]))
                    face_area = float(max(1, fw * fh))
                    near_lens_foreground_component_ratio = float(
                        max_component_area / face_area)
                    near_lens_foreground_occlusion = (
                        near_lens_foreground_component_ratio > 0.80 and
                        near_lens_foreground_ratio > 0.80 and
                        face_stats["skin_ratio"] > 0.60
                    )
        result["near_lens_foreground_occlusion"] = bool(
            near_lens_foreground_occlusion)
        result["near_lens_foreground_ratio"] = float(
            near_lens_foreground_ratio)
        result["near_lens_foreground_component_ratio"] = float(
            near_lens_foreground_component_ratio)
        near_face_foreground_occlusion = False
        near_face_skin_component_ratio = 0.0
        near_face_dark_component_ratio = 0.0
        near_face_foreground_distance_ratio = 999.0
        if face_rect is not None and face_box_w >= 330.0:
            fx1, fy1, fx2, fy2 = face_rect
            fw = max(1, fx2 - fx1)
            fh = max(1, fy2 - fy1)
            near_rect = _clip_roi(
                fx1 - fw * 0.80, fy1 - fh * 0.15,
                fx2 + fw * 0.80, fy2 + fh * 0.35,
                frame_w, frame_h)
            if near_rect is not None:
                nx1, ny1, nx2, ny2 = near_rect
                near_roi = frame[ny1:ny2, nx1:nx2]
                if near_roi.size > 0:
                    near_ycrcb = cv2.cvtColor(near_roi, cv2.COLOR_BGR2YCrCb)
                    near_y = near_ycrcb[:, :, 0]
                    near_cr = near_ycrcb[:, :, 1]
                    near_cb = near_ycrcb[:, :, 2]
                    near_hsv = cv2.cvtColor(near_roi, cv2.COLOR_BGR2HSV)
                    near_sat = near_hsv[:, :, 1]
                    outside_mask = np.ones(near_y.shape, dtype=np.uint8)
                    lx1 = max(0, fx1 - nx1)
                    lx2 = min(nx2 - nx1, fx2 - nx1)
                    ly1 = max(0, fy1 - ny1)
                    ly2 = min(ny2 - ny1, fy2 - ny1)
                    if lx2 > lx1 and ly2 > ly1:
                        outside_mask[ly1:ly2, lx1:lx2] = 0

                    def _largest_near_component(mask_bool):
                        labels_count, _, component_stats, _ = cv2.connectedComponentsWithStats(
                            mask_bool.astype(np.uint8), 8)
                        best = {
                            "area": 0,
                            "distance": float(max(frame_w, frame_h)),
                            "side_adjacent": False,
                            "vertical_overlap_ratio": 0.0,
                            "width_ratio": 0.0,
                            "height_ratio": 0.0,
                        }
                        for label_idx in range(1, labels_count):
                            area = int(component_stats[label_idx, cv2.CC_STAT_AREA])
                            cx = int(component_stats[label_idx, cv2.CC_STAT_LEFT])
                            cy = int(component_stats[label_idx, cv2.CC_STAT_TOP])
                            cw = int(component_stats[label_idx, cv2.CC_STAT_WIDTH])
                            ch = int(component_stats[label_idx, cv2.CC_STAT_HEIGHT])
                            ox1, oy1 = nx1 + cx, ny1 + cy
                            ox2, oy2 = ox1 + cw, oy1 + ch
                            dx = max(fx1 - ox2, ox1 - fx2, 0)
                            dy = max(fy1 - oy2, oy1 - fy2, 0)
                            dist = float((dx * dx + dy * dy) ** 0.5)
                            vertical_overlap = max(
                                0, min(oy2, fy2) - max(oy1, fy1))
                            vertical_overlap_ratio = float(
                                vertical_overlap / max(1, fh))
                            right_side = ox1 >= fx2 - fw * 0.08
                            left_side = ox2 <= fx1 + fw * 0.08
                            horizontal_gap = min(
                                abs(ox1 - fx2), abs(fx1 - ox2))
                            side_adjacent = (
                                (right_side or left_side) and
                                horizontal_gap <= fw * 0.08 and
                                vertical_overlap_ratio >= 0.30
                            )
                            if area > best["area"]:
                                best = {
                                    "area": area,
                                    "distance": dist,
                                    "side_adjacent": bool(side_adjacent),
                                    "vertical_overlap_ratio": vertical_overlap_ratio,
                                    "width_ratio": float(cw / max(1, fw)),
                                    "height_ratio": float(ch / max(1, fh)),
                                }
                        return best

                    valid_near = outside_mask.astype(bool)
                    skin_like = (
                        valid_near &
                        (near_y > 35) & (near_y < 230) &
                        (near_cr > 135) & (near_cb < 150) &
                        (near_sat > 35)
                    )
                    dark_object = (
                        valid_near &
                        (near_y < 105) &
                        (near_sat < 155)
                    )
                    skin_component = _largest_near_component(skin_like)
                    dark_component = _largest_near_component(dark_object)
                    skin_area = skin_component["area"]
                    dark_area = dark_component["area"]
                    face_area = float(max(1, fw * fh))
                    near_face_skin_component_ratio = float(skin_area / face_area)
                    near_face_dark_component_ratio = float(dark_area / face_area)
                    near_face_foreground_distance_ratio = float(
                        min(skin_component["distance"], dark_component["distance"]) /
                        max(1, fw))
                    side_dark_object = (
                        dark_component["side_adjacent"] and
                        near_face_dark_component_ratio > 0.28 and
                        dark_component["vertical_overlap_ratio"] > 0.36 and
                        dark_component["height_ratio"] > 0.35 and
                        face_box_w < 430.0 and
                        face_stats["edge_density"] > 0.050 and
                        face_stats["skin_ratio"] > 0.93 and
                        local_v_ratio < 0.70
                    )
                    side_hand_foreground = (
                        skin_component["side_adjacent"] and
                        near_face_skin_component_ratio > 1.35 and
                        skin_component["vertical_overlap_ratio"] > 0.45 and
                        skin_component["height_ratio"] > 0.75
                    )
                    side_palm_eye_face_foreground = (
                        (skin_component["side_adjacent"] or
                         near_face_foreground_distance_ratio <= 0.02) and
                        face_box_w >= 430.0 and
                        abs(gaze["pitch"]) <= 13.0 and
                        abs(gaze["yaw"]) <= 12.0 and
                        abs(gaze["roll"]) <= 10.0 and
                        0.30 <= near_face_skin_component_ratio <= 0.45 and
                        skin_component["vertical_overlap_ratio"] > 0.88 and
                        skin_component["height_ratio"] > 0.78 and
                        face_stats["skin_ratio"] > 0.72 and
                        local_v_ratio > 0.65 and
                        eye_metrics.get("eye_band", {}).get("skin_ratio", 1.0) < 0.90 and
                        mouth_skin < 0.90 and
                        mouth_stats["dark_ratio"] > 0.18
                    )
                    center_hand_foreground = (
                        near_face_skin_component_ratio > 1.35 and
                        skin_component["vertical_overlap_ratio"] > 0.85 and
                        skin_component["height_ratio"] > 1.20 and
                        face_box_w < 430.0 and
                        face_stats["std_y"] > 35.0 and
                        lower_stats["std_y"] > 32.0 and
                        lower_stats["edge_density"] < 0.012
                    )
                    near_face_foreground_occlusion = (
                        side_dark_object or
                        side_hand_foreground or
                        side_palm_eye_face_foreground or
                        center_hand_foreground
                    )
                    result["near_face_side_dark_object"] = bool(side_dark_object)
                    result["near_face_side_hand_foreground"] = bool(
                        side_hand_foreground)
                    result["near_face_side_palm_eye_face_foreground"] = bool(
                        side_palm_eye_face_foreground)
                    result["near_face_center_hand_foreground"] = bool(
                        center_hand_foreground)
                    result["near_face_skin_side_adjacent"] = bool(
                        skin_component["side_adjacent"])
                    result["near_face_dark_side_adjacent"] = bool(
                        dark_component["side_adjacent"])
                    result["near_face_skin_vertical_overlap_ratio"] = float(
                        skin_component["vertical_overlap_ratio"])
                    result["near_face_dark_vertical_overlap_ratio"] = float(
                        dark_component["vertical_overlap_ratio"])
                    result["near_face_skin_height_ratio"] = float(
                        skin_component["height_ratio"])
                    result["near_face_dark_height_ratio"] = float(
                        dark_component["height_ratio"])
        result["near_face_foreground_occlusion"] = bool(
            near_face_foreground_occlusion)
        result["near_face_skin_component_ratio"] = float(
            near_face_skin_component_ratio)
        result["near_face_dark_component_ratio"] = float(
            near_face_dark_component_ratio)
        result["near_face_foreground_distance_ratio"] = float(
            near_face_foreground_distance_ratio)

        lower_much_darker = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            face_mean >= 70 and
            lower_mean < max(86.0, face_mean * 0.74) and
            lower_stats["dark_ratio"] > 0.36
        )
        mouth_much_darker = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            face_mean >= 70 and
            mouth_mean < max(82.0, face_mean * 0.72) and
            mouth_stats["dark_ratio"] > 0.42
        )
        skin_missing_lower_face = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            face_skin > 0.20 and
            lower_skin < max(0.18, face_skin * 0.45) and
            mouth_skin < max(0.16, face_skin * 0.42)
        )
        nose_mouth_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            nose_stats["skin_ratio"] < max(0.16, face_skin * 0.40) and
            mouth_skin < max(0.16, face_skin * 0.40)
        )
        cloth_texture = (
            lower_stats["edge_density"] > 0.012 or
            lower_stats["std_y"] > 18.0 or
            lower_box_stats["edge_density"] > 0.010 or
            lower_box_stats["std_y"] > 18.0
        )
        mouth_nose_clearly_visible = (
            nose_stats.get("valid", False) and
            mouth_skin > 0.85 and
            nose_skin > 0.85 and
            mouth_mean > face_mean + 4.0 and
            nose_stats["mean_y"] > face_mean + 6.0 and
            mouth_stats["dark_ratio"] < 0.16 and
            nose_stats["dark_ratio"] < 0.12
        )
        lower_face_skin_visible = (
            nose_stats.get("valid", False) and
            mouth_skin > 0.82 and
            nose_skin > 0.82 and
            lower_skin > 0.82 and
            lower_box_skin > 0.82
        )
        nose_clearly_visible = (
            nose_stats.get("valid", False) and
            nose_skin > 0.84 and
            nose_stats["dark_ratio"] < 0.18 and
            nose_stats["edge_density"] < 0.055
        )
        mask_nose_mouth_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            not pose_unstable and  # [2026-06-09 Fix] 低頭/姿態不穩時膚色偵測失準，不判定口鼻遮擋
            lower_box_stats["valid"] and
            nose_stats.get("valid", False) and
            face_mean >= 70 and
            face_skin > 0.50 and  # [2026-06-09 Fix v2] face_skin<0.50=全臉膚色偵測崩壞(低頭/光線)，結果不可信
            mouth_skin < 0.32 and
            lower_box_skin < 0.42 and
            nose_skin < 0.80 and
            lower_box_stats["saturation_mean"] > face_stats["saturation_mean"] - 8.0 and
            lower_box_stats["edge_density"] > 0.018
        )
        small_face_mask_nose_mouth_cover = (
            300.0 <= face_box_w < 430.0 and
            eye_dist > 180.0 and
            not severe_face_underexposed and
            lower_box_stats["valid"] and
            nose_stats.get("valid", False) and
            face_mean >= 70 and
            mouth_skin < 0.12 and
            lower_skin < 0.48 and
            lower_box_skin < 0.35 and
            lower_box_stats["edge_density"] > 0.025 and
            mouth_stats["saturation_mean"] > face_stats["saturation_mean"] + 6.0 and
            lower_box_stats["saturation_mean"] > face_stats["saturation_mean"] - 6.0
        )
        smooth_light_mask_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            not pose_unstable and
            lower_box_stats["valid"] and
            mouth_skin < 0.45 and
            lower_box_skin < 0.45 and
            lower_box_stats["edge_density"] < 0.035 and
            lower_box_stats["dark_ratio"] < 0.15 and
            face_mean >= 80
        )
        visible_nose_mouth_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            lower_box_stats["valid"] and
            nose_clearly_visible and
            mouth_skin < 0.72 and
            lower_box_skin < 0.68 and
            lower_box_stats["edge_density"] > 0.035 and
            lower_box_stats["saturation_mean"] < face_stats["saturation_mean"] - 20.0
        )
        lower_box_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            lower_box_stats["valid"] and
            not mouth_nose_clearly_visible and
            face_mean >= 70 and
            lower_box_mean < max(92.0, face_mean * 0.78) and
            lower_box_stats["dark_ratio"] > 0.32 and
            lower_box_skin < max(0.28, face_skin * 0.62)
        ) or (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            lower_box_stats["valid"] and
            not mouth_nose_clearly_visible and
            face_mean >= 70 and
            lower_box_mean < max(90.0, face_mean * 0.72) and
            lower_box_stats["dark_ratio"] > 0.50 and
            lower_box_skin < 0.35 and
            cloth_texture
        )
        patterned_lower_cover = (
            lower_box_stats["valid"] and
            not lower_face_skin_visible and
            face_mean >= 70 and
            eye_dist > 150 and
            lower_box_mean < face_mean - 4.0 and
            lower_box_stats["std_y"] > 24.0 and
            lower_box_stats["edge_density"] > max(0.060, face_stats["edge_density"] * 1.15) and
            lower_box_stats["saturation_mean"] < face_stats["saturation_mean"] - 12.0
        )
        patterned_lower_cover_bright = (
            pose_unstable and
            lower_box_stats["valid"] and
            not lower_face_skin_visible and
            face_mean >= 70 and
            eye_dist > 150 and
            lower_box_stats["std_y"] > 30.0 and
            lower_box_stats["edge_density"] > max(0.055, face_stats["edge_density"] * 1.25) and
            lower_box_stats["saturation_mean"] < face_stats["saturation_mean"] - 15.0
        )
        skin_colored_lower_cover = (
            pose_unstable and
            face_mean >= 60 and
            eye_dist > 150 and
            mouth_skin > 0.90 and
            nose_skin > 0.82 and
            lower_skin > 0.92 and
            lower_box_skin > 0.90 and
            mouth_skin - face_skin > 0.04 and
            mouth_mean > face_mean + 4.0 and
            lower_mean > face_mean + 3.0 and
            mouth_stats["dark_ratio"] < 0.10
        )
        skin_colored_mouth_cover = (
            pose_unstable and
            face_mean >= 60 and
            eye_dist > 150 and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            mouth_skin > 0.94 and
            nose_skin > 0.88 and
            lower_skin > 0.86 and
            lower_box_skin > 0.86 and
            mouth_mean > face_mean + 10.0 and
            lower_mean > face_mean + 6.0 and
            mouth_stats["dark_ratio"] < 0.11 and
            lower_box_stats["dark_ratio"] < 0.18 and
            mouth_stats["edge_density"] < 0.024 and
            lower_box_stats["edge_density"] < 0.024
        )
        low_head_visible_nose = (
            generic_lower_resolution_ok and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            -22.0 <= gaze["pitch"] <= -10.0 and
            abs(gaze["yaw"]) <= 14.0 and
            local_v_ratio < 0.52 and
            nose_skin > 0.92 and
            mouth_skin > 0.90 and
            nose_stats["dark_ratio"] < 0.16 and
            mouth_stats["dark_ratio"] < 0.16 and
            nose_stats["edge_density"] < 0.036 and
            mouth_stats["edge_density"] < 0.038
        )
        hood_visible_nose_mouth = (
            generic_lower_resolution_ok and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            not severe_face_underexposed and
            abs(gaze["yaw"]) <= 12.0 and
            abs(gaze["roll"]) <= 12.0 and
            0.55 <= local_v_ratio <= 1.25 and
            nose_skin > 0.92 and
            mouth_skin > 0.90 and
            nose_stats["dark_ratio"] < 0.16 and
            mouth_stats["dark_ratio"] < 0.16 and
            nose_stats["edge_density"] < 0.020 and
            mouth_stats["edge_density"] < 0.020 and
            face_stats["dark_ratio"] > 0.24 and
            lower_box_stats["dark_ratio"] > 0.24 and
            lower_box_skin < 0.82 and
            near_face_skin_component_ratio < 0.14 and
            not skin_component["side_adjacent"]
        )
        skin_colored_nose_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            not low_head_visible_nose and
            not hood_visible_nose_mouth and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_mean >= 60 and
            nose_skin > 0.90 and
            mouth_skin > 0.90 and
            nose_stats["mean_y"] > face_mean + 10.0 and
            mouth_mean > face_mean + 8.0 and
            nose_stats["dark_ratio"] < 0.16 and
            nose_stats["edge_density"] < 0.028 and
            nose_stats["laplacian"] < 30.0 and
            lower_stats["saturation_mean"] < face_stats["saturation_mean"] - 2.0 and
            lower_box_skin < 0.78 and
            lower_box_stats["dark_ratio"] > 0.26
        )
        high_head_visible_lower_face = (
            generic_lower_resolution_ok and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            gaze["pitch"] > 6.0 and
            local_v_ratio > 1.35 and
            abs(gaze["yaw"]) <= 18.0 and
            abs(gaze["roll"]) <= 10.0 and
            mouth_skin > 0.88 and
            nose_skin > 0.88 and
            lower_box_skin > 0.82 and
            mouth_stats["dark_ratio"] < 0.24 and
            nose_stats["dark_ratio"] < 0.18 and
            lower_box_stats["dark_ratio"] < 0.24 and
            mouth_stats["edge_density"] < 0.012 and
            nose_stats["edge_density"] < 0.012 and
            lower_box_stats["edge_density"] < 0.014
        )
        high_head_closed_eye_visible_lower_face = (
            generic_lower_resolution_ok and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            not severe_face_underexposed and
            gaze["pitch"] > 6.0 and
            1.15 <= local_v_ratio <= 1.45 and
            gaze["ear"] is not None and
            gaze["ear"] < 0.12 and
            abs(gaze["yaw"]) <= 12.0 and
            abs(gaze["roll"]) <= 10.0 and
            nose_skin > 0.92 and
            mouth_skin > 0.93 and
            mouth_stats["dark_ratio"] < 0.14 and
            nose_stats["dark_ratio"] < 0.14 and
            mouth_stats["edge_density"] < 0.012 and
            nose_stats["edge_density"] < 0.012 and
            near_face_skin_component_ratio < 0.20
        )
        normal_pose_visible_lower_face = (
            generic_lower_resolution_ok and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            not severe_face_underexposed and
            abs(gaze["pitch"]) <= 8.0 and
            abs(gaze["yaw"]) <= 15.0 and
            abs(gaze["roll"]) <= 10.0 and
            0.70 <= local_v_ratio <= 1.05 and
            nose_skin > 0.92 and
            mouth_skin > 0.90 and
            lower_box_skin > 0.86 and
            nose_stats["dark_ratio"] < 0.16 and
            mouth_stats["dark_ratio"] < 0.16 and
            lower_box_stats["dark_ratio"] < 0.18 and
            near_face_skin_component_ratio > 0.48 and
            not skin_component["side_adjacent"]
        )
        skin_colored_nose_mouth_hand_cover = (
            generic_lower_resolution_ok and
            (pose_occlusion_context or lower_box_stats["mean_y"] > face_mean + 8.0) and # [2026-06-09 Fix v4] Allow normal pose if bright hand is present
            not high_head_visible_lower_face and
            not high_head_closed_eye_visible_lower_face and
            not hood_visible_nose_mouth and
            not normal_pose_visible_lower_face and
            not mouth_nose_clearly_visible and  # [2026-06-09 Fix v2] 口鼻膚色完美可見(>0.85)時不觸發
            not (mouth_skin > 0.95 and nose_skin > 0.95 and lower_box_stats["mean_y"] < face_mean + 8.0) and # [2026-06-09 Fix v3] 完全膚色且無遮擋且不亮時才放行，若太亮可能是手


            -12.0 <= gaze["pitch"] <= 14.0 and
            0.52 <= local_v_ratio <= 1.28 and
            abs(gaze["yaw"]) <= 15.0 and
            abs(gaze["roll"]) <= 15.0 and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_skin > 0.72 and
            mouth_skin > 0.88 and
            nose_skin > 0.86 and
            lower_skin > 0.76 and
            lower_box_skin > 0.78 and
            mouth_stats["edge_density"] < 0.040 and
            nose_stats["edge_density"] < 0.035 and
            lower_box_stats["edge_density"] < 0.032 and
            lower_box_stats["laplacian"] < 35.0 and
            mouth_stats["laplacian"] < 36.0
        )
        skin_colored_palm_nose_mouth_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_mean >= 60 and
            face_skin > 0.70 and
            mouth_skin > 0.94 and
            nose_skin > 0.88 and
            lower_skin > 0.92 and
            lower_box_skin > 0.88 and
            mouth_mean > face_mean + 12.0 and
            lower_mean > face_mean + 10.0 and
            mouth_stats["dark_ratio"] < 0.12 and
            nose_stats["dark_ratio"] < 0.14 and
            lower_box_stats["dark_ratio"] < 0.14 and
            mouth_stats["edge_density"] < 0.020 and
            nose_stats["edge_density"] < 0.020 and
            lower_box_stats["edge_density"] < 0.020 and
            mouth_stats["laplacian"] < 30.0 and
            nose_stats["laplacian"] < 30.0 and
            lower_box_stats["laplacian"] < 30.0
        )
        skin_colored_palm_nose_mouth_tip_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_mean >= 60 and
            face_skin > 0.70 and
            mouth_skin > 0.98 and
            nose_skin > 0.92 and
            lower_skin > 0.94 and
            lower_box_skin > 0.90 and
            mouth_mean > face_mean + 18.0 and
            lower_mean > face_mean + 10.0 and
            nose_stats["mean_y"] <= face_mean + 2.0 and
            0.13 <= nose_stats["dark_ratio"] < 0.18 and
            mouth_stats["dark_ratio"] < 0.05 and
            lower_box_stats["dark_ratio"] < 0.10 and
            mouth_stats["edge_density"] < 0.026 and
            lower_box_stats["edge_density"] < 0.026 and
            mouth_stats["laplacian"] < 35.0 and
            lower_box_stats["laplacian"] < 35.0
        )
        skin_colored_palm_mouth_cover = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_mean >= 60 and
            face_skin > 0.70 and
            mouth_skin > 0.96 and
            lower_skin > 0.94 and
            lower_box_skin > 0.90 and
            mouth_mean > face_mean + 15.0 and
            lower_mean > face_mean + 10.0 and
            lower_box_mean > face_mean + 12.0 and
            mouth_stats["dark_ratio"] < 0.07 and
            lower_box_stats["dark_ratio"] < 0.10 and
            mouth_stats["edge_density"] < 0.026 and
            lower_box_stats["edge_density"] < 0.026 and
            mouth_stats["laplacian"] < 35.0 and
            lower_box_stats["laplacian"] < 35.0
        )
        skin_colored_palm_mouth_cover_lower_box = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            face_mean >= 60 and
            face_skin > 0.70 and
            mouth_skin > 0.94 and
            lower_skin > 0.90 and
            lower_box_skin > 0.90 and
            mouth_mean > face_mean + 6.0 and
            lower_mean > face_mean + 9.0 and
            lower_box_mean > face_mean + 12.0 and
            mouth_stats["dark_ratio"] < 0.18 and
            lower_box_stats["dark_ratio"] < 0.10 and
            mouth_stats["edge_density"] < 0.030 and
            lower_box_stats["edge_density"] < 0.026 and
            mouth_stats["laplacian"] < 36.0 and
            lower_box_stats["laplacian"] < 26.0
        )

        eye_band_stats = eye_metrics.get("eye_band", {}) if isinstance(eye_metrics, dict) else {}
        left_eye_stats = eye_metrics.get("left_eye", {}) if isinstance(eye_metrics, dict) else {}
        right_eye_stats = eye_metrics.get("right_eye", {}) if isinstance(eye_metrics, dict) else {}
        normal_frontal_detail_context = (
            face_box_w >= 100.0 and
            not severe_face_underexposed and
            abs(gaze["yaw"]) <= 16.0 and
            abs(gaze["roll"]) <= 15.0 and
            -18.0 <= gaze["pitch"] <= 10.0 and
            0.45 <= local_v_ratio <= 1.05 and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"]
        )
        lower_feature_missing_score = 0
        lower_feature_missing_score += int(mouth_skin > 0.80)
        lower_feature_missing_score += int(nose_skin > 0.80)
        lower_feature_missing_score += int(lower_box_skin > 0.78)
        lower_feature_missing_score += int(mouth_stats["edge_density"] < 0.050)
        lower_feature_missing_score += int(nose_stats["edge_density"] < 0.075)
        lower_feature_missing_score += int(lower_box_stats["edge_density"] < 0.050)
        lower_feature_missing_score += int(near_face_skin_component_ratio > 0.32)
        lower_feature_missing_score += int(
            skin_component["side_adjacent"] or
            skin_component["vertical_overlap_ratio"] > 0.36 or
            near_face_foreground_distance_ratio <= 0.02
        )
        lower_feature_foreground_evidence = (
            near_face_foreground_occlusion or
            near_lens_foreground_occlusion or
            near_face_skin_component_ratio >= 0.24 or
            (
                near_face_skin_component_ratio >= 0.18 and
                (
                    skin_component["side_adjacent"] or
                    skin_component["vertical_overlap_ratio"] > 0.36
                )
            )
        )
        lower_feature_detail_loss = (
            normal_frontal_detail_context and
            lower_feature_missing_score >= (9 if face_mean < 105.0 else 7) and
            lower_feature_foreground_evidence and
            (
                near_face_skin_component_ratio >= 0.18 or
                mouth_stats["edge_density"] < 0.020 or
                nose_stats["edge_density"] < 0.055 or
                lower_box_stats["edge_density"] < 0.040 or
                mouth_stats["laplacian"] < 22.0 or
                nose_stats["laplacian"] < 40.0
            )
        )
        broad_feature_visibility_loss = (
            face_box_w >= 430.0 and
            not severe_face_underexposed and
            nose_stats.get("valid", False) and
            lower_box_stats["valid"] and
            -16.0 <= gaze["pitch"] <= 10.0 and
            abs(gaze["yaw"]) <= 18.5 and
            abs(gaze["roll"]) <= 15.0 and
            0.42 <= local_v_ratio <= 1.08 and
            lower_feature_missing_score >= (8 if face_mean < 105.0 else 6) and
            not mouth_nose_clearly_visible and
            not high_head_visible_lower_face and
            not high_head_closed_eye_visible_lower_face and
            not hood_visible_nose_mouth and
            (
                near_face_skin_component_ratio >= 0.28 or
                (
                    skin_component["side_adjacent"] and
                    skin_component["vertical_overlap_ratio"] > 0.58 and
                    skin_component["height_ratio"] > 0.48
                )
            ) and
            (
                mouth_stats["edge_density"] < 0.024 or
                mouth_stats["laplacian"] < 22.0 or
                nose_stats["edge_density"] < 0.050 or
                lower_box_stats["edge_density"] < 0.032
            )
        )
        skin_like_lower_feature_loss = (
            (lower_feature_detail_loss or broad_feature_visibility_loss) and
            not high_head_visible_lower_face and
            not high_head_closed_eye_visible_lower_face and
            not hood_visible_nose_mouth and
            not (
                mouth_skin > 0.95 and
                nose_skin > 0.95 and
                lower_box_skin > 0.95 and
                lower_box_mean < face_mean + 8.0 and
                near_face_skin_component_ratio < 0.30
            )
        )
        mouth_expression_deformation = (
            normal_frontal_detail_context and
            near_face_skin_component_ratio < 0.30 and
            0.62 <= local_v_ratio <= 0.95 and
            mouth_stats["dark_ratio"] > 0.18 and
            (
                (mouth_skin < 0.88 and mouth_stats["edge_density"] < 0.055) or
                (mouth_skin > 0.90 and mouth_stats["edge_density"] < 0.023) or
                (mouth_skin < 0.91 and lower_box_skin < 0.90 and mouth_stats["dark_ratio"] > 0.22) or
                (
                    gaze["ear"] is not None and
                    gaze["ear"] < 0.20 and
                    lower_box_skin < 0.88
                )
            )
        )
        cheek_squeeze_deformation = (
            normal_frontal_detail_context and
            0.45 <= local_v_ratio <= 0.62 and
            abs(gaze["pitch"]) <= 8.0 and
            near_face_skin_component_ratio > 0.50 and
            lower_box_skin > 0.78 and
            mouth_skin < 0.78 and
            mouth_stats["dark_ratio"] > 0.45
        )
        expression_deformation = (
            mouth_expression_deformation or cheek_squeeze_deformation
        )
        large_side_skin_eye_hand_cover = (
            isinstance(eye_metrics, dict) and
            not eye_metrics.get("eye_occluded", False) and
            eye_metrics.get("reason") == "shadowed_skin_visible" and
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            face_box_w >= 540.0 and
            abs(gaze["pitch"]) <= 13.0 and
            abs(gaze["yaw"]) <= 32.0 and
            abs(gaze["roll"]) <= 15.0 and
            skin_component["side_adjacent"] and
            0.12 <= near_face_skin_component_ratio <= 0.55 and
            skin_component["vertical_overlap_ratio"] > 0.65 and
            skin_component["height_ratio"] > 0.48 and
            eye_band_stats.get("skin_ratio", 0.0) > 0.90 and
            eye_band_stats.get("dark_ratio", 1.0) < 0.18 and
            eye_band_stats.get("edge_density", 1.0) < 0.032 and
            left_eye_stats.get("skin_ratio", 0.0) > 0.82 and
            right_eye_stats.get("skin_ratio", 0.0) > 0.82 and
            abs(left_eye_stats.get("mean_y", 0.0) -
                right_eye_stats.get("mean_y", 0.0)) > 20.0
        )
        large_center_skin_eye_hand_cover = (
            isinstance(eye_metrics, dict) and
            not eye_metrics.get("eye_occluded", False) and
            eye_metrics.get("reason") == "shadowed_skin_visible" and
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            face_box_w >= 540.0 and
            abs(gaze["pitch"]) <= 8.0 and
            abs(gaze["yaw"]) <= 26.0 and
            abs(gaze["roll"]) <= 10.0 and
            0.24 <= near_face_skin_component_ratio <= 0.60 and
            skin_component["vertical_overlap_ratio"] > 0.50 and
            skin_component["height_ratio"] > 0.50 and
            eye_band_stats.get("skin_ratio", 0.0) > 0.86 and
            0.14 < eye_band_stats.get("dark_ratio", 0.0) < 0.24 and
            eye_band_stats.get("edge_density", 1.0) < 0.010 and
            max(left_eye_stats.get("dark_ratio", 0.0),
                right_eye_stats.get("dark_ratio", 0.0)) > 0.25 and
            max(left_eye_stats.get("edge_density", 1.0),
                right_eye_stats.get("edge_density", 1.0)) < 0.014 and
            abs(left_eye_stats.get("mean_y", 0.0) -
                right_eye_stats.get("mean_y", 0.0)) > 6.5
        )
        broad_eye_skin_hand_cover = (
            isinstance(eye_metrics, dict) and
            not eye_metrics.get("eye_occluded", False) and
            eye_metrics.get("reason") == "shadowed_skin_visible" and
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            abs(gaze["pitch"]) <= 15.0 and
            lower_box_stats["valid"] and
            eye_band_stats.get("skin_ratio", 0.0) > 0.86 and
            0.12 < eye_band_stats.get("dark_ratio", 0.0) < 0.24 and
            eye_band_stats.get("edge_density", 1.0) < 0.024 and
            eye_band_stats.get("laplacian", 999.0) < 12.0 and  # [2026-06-09 Fix] 22->12: prevent false positives on clear skin
            left_eye_stats.get("skin_ratio", 0.0) > 0.82 and
            right_eye_stats.get("skin_ratio", 0.0) > 0.82 and
            max(left_eye_stats.get("edge_density", 1.0),
                right_eye_stats.get("edge_density", 1.0)) < 0.032 and
            abs(left_eye_stats.get("mean_y", 0.0) -
                right_eye_stats.get("mean_y", 0.0)) < 20.0 and
            eye_band_stats.get("saturation_mean", 0.0) > face_stats["saturation_mean"] + 4.0 and
            lower_box_stats["dark_ratio"] > 0.28 and
            lower_box_skin < 0.82 and
            mouth_mean < face_mean - 3.0
        )
        skin_like_eye_feature_cover = (
            isinstance(eye_metrics, dict) and
            not eye_metrics.get("eye_occluded", False) and
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            abs(gaze["pitch"]) <= 15.0 and
            abs(gaze["yaw"]) <= 15.0 and
            abs(gaze["roll"]) <= 15.0 and
            face_skin > 0.86 and
            eye_band_stats.get("skin_ratio", 0.0) > 0.90 and
            left_eye_stats.get("skin_ratio", 0.0) > 0.86 and
            right_eye_stats.get("skin_ratio", 0.0) > 0.94 and
            max(left_eye_stats.get("bright_ratio", 1.0),
                right_eye_stats.get("bright_ratio", 1.0)) < 0.035 and
            max(left_eye_stats.get("specular_ratio", 1.0),
                right_eye_stats.get("specular_ratio", 1.0)) < 0.006 and
            (
                abs(left_eye_stats.get("mean_y", 0.0) -
                    right_eye_stats.get("mean_y", 0.0)) > 28.0 or
                (
                    left_eye_stats.get("skin_ratio", 0.0) > 0.93 and
                    right_eye_stats.get("skin_ratio", 0.0) > 0.93 and
                    eye_band_stats.get("edge_density", 0.0) > 0.040 and
                    max(left_eye_stats.get("laplacian", 999.0),
                        right_eye_stats.get("laplacian", 999.0)) < 62.0
                )
            ) and
            mouth_stats["laplacian"] < 36.0 and
            mouth_mean < face_mean - 8.0
        )
        face_deformation = (
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            abs(gaze["yaw"]) <= 18.0 and
            abs(gaze["roll"]) <= 15.0 and
            not (gaze["pitch"] > 14.0 and local_v_ratio > 1.18) and
            0.75 <= local_v_ratio <= 2.20 and
            nose_stats.get("valid", False) and
            nose_skin > 0.82 and
            mouth_skin > 0.74 and
            nose_stats["dark_ratio"] < 0.22 and
            near_face_skin_component_ratio < 0.45 and
            not near_face_foreground_occlusion and
            (
                (
                    gaze["ear"] is not None and
                    gaze["ear"] < 0.16 and
                    (
                        local_v_ratio <= 1.18 or
                        (1.18 < local_v_ratio <= 1.45 and gaze["ear"] < 0.08) or
                        (local_v_ratio > 1.45 and gaze["ear"] < 0.08)
                    )
                ) or
                (mouth_skin < 0.84 and mouth_stats["dark_ratio"] > 0.18) or
                (
                    local_v_ratio > 1.45 and
                    mouth_stats["dark_ratio"] > 0.12 and
                    mouth_stats["edge_density"] < 0.012
                ) or
                (
                    local_v_ratio > 1.18 and
                    gaze["pitch"] <= 6.0 and
                    eye_band_stats.get("dark_ratio", 0.0) > 0.16 and
                    eye_band_stats.get("edge_density", 1.0) < 0.014
                ) or
                (
                    local_v_ratio < 1.02 and
                    gaze["ear"] is not None and
                    gaze["ear"] < 0.09 and
                    eye_band_stats.get("dark_ratio", 0.0) > 0.16 and
                    eye_band_stats.get("edge_density", 1.0) < 0.010
                )
            )
        ) or expression_deformation

        result["low_head_visible_nose"] = bool(low_head_visible_nose)
        result["high_head_visible_lower_face"] = bool(
            high_head_visible_lower_face)
        result["high_head_closed_eye_visible_lower_face"] = bool(
            high_head_closed_eye_visible_lower_face)
        result["hood_visible_nose_mouth"] = bool(hood_visible_nose_mouth)
        result["normal_pose_visible_lower_face"] = bool(
            normal_pose_visible_lower_face)
        result["face_deformation"] = bool(face_deformation)
        result["expression_deformation"] = bool(expression_deformation)
        result["mouth_expression_deformation"] = bool(
            mouth_expression_deformation)
        result["cheek_squeeze_deformation"] = bool(
            cheek_squeeze_deformation)
        result["skin_like_lower_feature_loss"] = bool(
            skin_like_lower_feature_loss)
        result["lower_feature_detail_loss"] = bool(
            lower_feature_detail_loss)
        result["broad_feature_visibility_loss"] = bool(
            broad_feature_visibility_loss)
        result["lower_feature_foreground_evidence"] = bool(
            lower_feature_foreground_evidence)
        result["lower_feature_missing_score"] = int(
            lower_feature_missing_score)
        result["large_side_skin_eye_hand_cover"] = bool(
            large_side_skin_eye_hand_cover)
        result["large_center_skin_eye_hand_cover"] = bool(
            large_center_skin_eye_hand_cover)

        foreground_feature_occlusion_over_deformation = (
            face_deformation and
            generic_lower_resolution_ok and
            not severe_face_underexposed and
            (
                near_face_skin_component_ratio >= 0.24 or
                broad_feature_visibility_loss
            ) and
            eye_band_stats.get("skin_ratio", 0.0) > 0.90 and
            abs(gaze["yaw"]) <= 18.0 and
            abs(gaze["roll"]) <= 15.0
        )
        result["foreground_feature_occlusion_over_deformation"] = bool(
            foreground_feature_occlusion_over_deformation)
        if foreground_feature_occlusion_over_deformation:
            eye_metrics["eye_occluded"] = True
            eye_metrics["reason"] = "skin_feature_occlusion_over_deformation"
            eye_metrics["occluded_eye_side"] = "both"
            result["eye_occlusion"] = eye_metrics
            result["face_occluded"] = True
            result["reason"] = eye_metrics["reason"]
            return result

        if face_deformation:
            result["reason"] = "face_deformation"
            return result

        if large_side_skin_eye_hand_cover or large_center_skin_eye_hand_cover or broad_eye_skin_hand_cover or skin_like_eye_feature_cover:
            eye_metrics["eye_occluded"] = True
            if large_side_skin_eye_hand_cover or large_center_skin_eye_hand_cover:
                eye_metrics["reason"] = (
                    "large_side_skin_eye_hand_cover"
                    if large_side_skin_eye_hand_cover
                    else "large_center_skin_eye_hand_cover"
                )
                eye_metrics["occluded_eye_side"] = (
                    left_eye_stats.get("dark_ratio", 0.0) > right_eye_stats.get("dark_ratio", 0.0)
                    and (eye_metrics.get("left_eye_subject_side") or "left")
                    or (eye_metrics.get("right_eye_subject_side") or "right")
                )
            elif skin_like_eye_feature_cover and abs(left_eye_stats.get("mean_y", 0.0) -
                                                    right_eye_stats.get("mean_y", 0.0)) > 28.0:
                eye_metrics["reason"] = "single_eye_skin_hand_cover"
                eye_metrics["occluded_eye_side"] = (
                    left_eye_stats.get("mean_y", 0.0) < right_eye_stats.get("mean_y", 0.0)
                    and (eye_metrics.get("left_eye_subject_side") or "left")
                    or (eye_metrics.get("right_eye_subject_side") or "right")
                )
            else:
                eye_metrics["reason"] = "both_eye_skin_hand_cover"
                eye_metrics["occluded_eye_side"] = "both"
            result["eye_occlusion"] = eye_metrics
            result["face_occluded"] = True
            result["reason"] = eye_metrics["reason"]
            return result

        lower_face_occluded = (
            (lower_much_darker and mouth_much_darker) or
            (skin_missing_lower_face and cloth_texture) or
            mask_nose_mouth_cover or
            small_face_mask_nose_mouth_cover or
            smooth_light_mask_cover or  # [2026-06-09 Fix] Add smooth light mask cover
            visible_nose_mouth_cover or
            skin_colored_palm_nose_mouth_cover or
            skin_colored_palm_nose_mouth_tip_cover or
            skin_colored_palm_mouth_cover or
            skin_colored_palm_mouth_cover_lower_box or
            skin_colored_nose_mouth_hand_cover or
            nose_mouth_cover or
            lower_box_cover or
            patterned_lower_cover or
            patterned_lower_cover_bright or
            skin_colored_lower_cover or
            skin_colored_mouth_cover or
            skin_colored_nose_cover or
            skin_like_lower_feature_loss or
            near_lens_foreground_occlusion or
            near_face_foreground_occlusion
        )

        if lower_face_occluded:
            result["lower_face_occluded"] = True
            result["face_occluded"] = True
            if near_face_foreground_occlusion:
                result["reason"] = "near_face_foreground_occlusion"
            elif near_lens_foreground_occlusion:
                result["reason"] = "near_lens_foreground_occlusion"
            elif visible_nose_mouth_cover:
                result["reason"] = "visible_nose_mouth_cover"
            elif mask_nose_mouth_cover:
                result["reason"] = "mask_nose_mouth_cover"
            elif small_face_mask_nose_mouth_cover:
                result["reason"] = "small_face_mask_nose_mouth_cover"
            elif skin_colored_palm_nose_mouth_cover:
                result["reason"] = "skin_colored_palm_nose_mouth_cover"
            elif skin_colored_palm_nose_mouth_tip_cover:
                result["reason"] = "skin_colored_palm_nose_mouth_tip_cover"
            elif skin_colored_palm_mouth_cover:
                result["reason"] = "skin_colored_palm_mouth_cover"
            elif skin_colored_palm_mouth_cover_lower_box:
                result["reason"] = "skin_colored_palm_mouth_cover_lower_box"
            elif skin_colored_nose_mouth_hand_cover:
                result["reason"] = "skin_colored_nose_mouth_hand_cover"
            elif nose_mouth_cover:
                result["reason"] = "nose_mouth_occluded"
            elif skin_colored_nose_cover:
                result["reason"] = "skin_colored_nose_cover"
            elif skin_like_lower_feature_loss:
                result["reason"] = "skin_like_lower_feature_loss"
            elif skin_colored_mouth_cover:
                result["reason"] = "skin_colored_mouth_cover"
            elif skin_colored_lower_cover:
                result["reason"] = "skin_colored_lower_face_cover"
            elif patterned_lower_cover_bright:
                result["reason"] = "patterned_lower_face_cover_bright"
            elif patterned_lower_cover:
                result["reason"] = "patterned_lower_face_cover"
            elif skin_missing_lower_face:
                result["reason"] = "lower_face_skin_missing"
            elif lower_box_cover:
                result["reason"] = "lower_face_cover"
            else:
                result["reason"] = "dark_lower_face_cover"

        return result
    except Exception as e:
        LOGGER.error(f"Face occlusion analysis failed: {e}")
        result["reason"] = "error"
        return result


def describe_face_occlusion(metrics):
    """Return user-facing detail for an occlusion quality reject."""
    detail = {
        "display_text": "請完整露出臉部",
        "voice_text": "請完整露出臉部",
        "voice_key": "hint_face_visible",
        "quality_msg": "臉部遮擋 (FaceOcclusion)",
        "part": "face",
    }
    if not isinstance(metrics, dict):
        return detail

    eye_metrics = metrics.get("eye_occlusion", {})
    if isinstance(eye_metrics, dict) and eye_metrics.get("eye_occluded", False):
        side = eye_metrics.get("occluded_eye_side", "")
        # [2026-06-09 Fix] Merge left/right/both eye occlusion display and voice texts
        detail.update({
            "display_text": "眼部遮擋",
            "voice_text": "眼部遮擋",
            "voice_key": "hint_eye_occluded",
            "quality_msg": f"眼部遮擋 (EyeOcclusion{'Both' if side == 'both' else side.capitalize()})",
            "part": "both_eyes" if side == "both" else f"{side}_eye",
        })
        return detail

    reason = str(metrics.get("reason", ""))
    if reason in (
        "nose_mouth_occluded",
        "mask_nose_mouth_cover",
        "small_face_mask_nose_mouth_cover",
        "skin_colored_palm_nose_mouth_cover",
        "skin_colored_palm_nose_mouth_tip_cover",
        "skin_colored_nose_mouth_hand_cover",
        "skin_like_lower_feature_loss",
    ):
        detail.update({
            "display_text": "無法識別" if reason == "skin_like_lower_feature_loss" else "口鼻被遮擋",
            "voice_text": "無法識別" if reason == "skin_like_lower_feature_loss" else "口鼻被遮擋",
            "voice_key": "hint_unrecognized" if reason == "skin_like_lower_feature_loss" else "hint_nose_mouth_occluded",
            "quality_msg": "臉部細節遮擋 (FaceDetailOcclusion)" if reason == "skin_like_lower_feature_loss" else "口鼻遮擋 (NoseMouthOcclusion)",
            "part": "nose_mouth",
        })
    elif reason in ("skin_colored_nose_cover",):
        detail.update({
            "display_text": "鼻子被遮擋",
            "voice_text": "鼻子被遮擋",
            "voice_key": "hint_nose_occluded",
            "quality_msg": "鼻子遮擋 (NoseOcclusion)",
            "part": "nose",
        })
    elif reason in (
        "skin_colored_mouth_cover",
        "skin_colored_palm_mouth_cover",
        "skin_colored_palm_mouth_cover_lower_box",
        "visible_nose_mouth_cover",
    ):
        detail.update({
            "display_text": "嘴巴被遮擋",
            "voice_text": "嘴巴被遮擋",
            "voice_key": "hint_mouth_occluded",
            "quality_msg": "嘴巴遮擋 (MouthOcclusion)",
            "part": "mouth",
        })
    elif reason in (
        "skin_colored_lower_face_cover",
        "patterned_lower_face_cover_bright",
        "patterned_lower_face_cover",
        "lower_face_skin_missing",
        "lower_face_cover",
        "dark_lower_face_cover",
    ):
        detail.update({
            "display_text": "口鼻被遮擋",
            "voice_text": "口鼻被遮擋",
            "voice_key": "hint_nose_mouth_occluded",
            "quality_msg": "口鼻遮擋 (NoseMouthOcclusion)",
            "part": "nose_mouth",
        })
    return detail


def analyze_head_cover_shadow(frame, mesh_points=None, points=None, box=None):
    """Detect strong hood/veil shadow around the face without using identity gap."""
    result = {
        "is_head_cover_shadow": False,
        "reason": "pass",
    }

    try:
        if frame is None or box is None:
            result["reason"] = "no_frame_or_box"
            return result

        frame_h, frame_w = frame.shape[:2]
        x1, y1, x2, y2 = box
        face_rect = _clip_roi(x1, y1, x2, y2, frame_w, frame_h)
        if face_rect is None:
            result["reason"] = "invalid_face_rect"
            return result

        mesh = None
        if mesh_points is not None:
            mesh = np.asarray(mesh_points, dtype=np.float32)
            if mesh.ndim != 2 or mesh.shape[0] < 478 or mesh.shape[1] < 2:
                mesh = None

        eye_band_rect = None
        mouth_rect = None
        nose_rect = None
        lower_box_rect = None
        eye_dist = 0.0

        if mesh is not None:
            left_idx = [33, 133, 159, 145, 160, 158, 153, 144, 468, 469, 470, 471, 472]
            right_idx = [263, 362, 386, 374, 387, 385, 380, 373, 473, 474, 475, 476, 477]
            mouth_idx = [0, 13, 14, 17, 61, 78, 81, 82, 87, 88, 95, 146, 178, 191, 267, 291, 308, 310, 312, 317, 318, 324, 375, 402, 415]
            nose_idx = [1, 2, 4, 5, 94, 98, 168, 195, 197, 327]
            eye_dist = float(np.linalg.norm(mesh[33] - mesh[263]))
            eye_band_rect = _region_from_points(
                np.vstack([mesh[left_idx], mesh[right_idx]]), frame_w, frame_h,
                max(12.0, eye_dist * 0.12), max(10.0, eye_dist * 0.16)
            )
            mouth_rect = _region_from_points(
                mesh[mouth_idx], frame_w, frame_h,
                max(10.0, eye_dist * 0.12), max(10.0, eye_dist * 0.12)
            )
            nose_rect = _region_from_points(
                mesh[nose_idx], frame_w, frame_h,
                max(8.0, eye_dist * 0.08), max(8.0, eye_dist * 0.08)
            )
            mouth_y = float((mesh[61][1] + mesh[291][1]) / 2.0)
        elif points is not None and len(points) >= 5:
            pts = np.asarray(points, dtype=np.float32)
            eye_dist = float(np.linalg.norm(pts[0] - pts[1]))
            mouth_pts = pts[[3, 4]]
            eye_band_rect = _region_from_points(
                pts[[0, 1]], frame_w, frame_h,
                max(12.0, eye_dist * 0.18), max(10.0, eye_dist * 0.16)
            )
            mouth_rect = _region_from_points(
                mouth_pts, frame_w, frame_h,
                max(12.0, eye_dist * 0.18), max(12.0, eye_dist * 0.18)
            )
            nose_rect = _region_from_points(
                np.asarray([pts[2]]), frame_w, frame_h,
                max(10.0, eye_dist * 0.10), max(10.0, eye_dist * 0.10)
            )
            mouth_y = float(np.mean(mouth_pts[:, 1]))
        else:
            result["reason"] = "no_landmarks"
            return result

        fx1, fy1, fx2, fy2 = face_rect
        face_w = float(fx2 - fx1)
        face_h = float(fy2 - fy1)
        lower_start = min(fy2 - 1, max(fy1, int(round(mouth_y - max(6.0, eye_dist * 0.12)))))
        lower_box_rect = _clip_roi(fx1, lower_start, fx2, fy2, frame_w, frame_h)

        face_stats = _image_region_stats(frame, face_rect)
        eye_stats = _image_region_stats(frame, eye_band_rect)
        mouth_stats = _image_region_stats(frame, mouth_rect)
        nose_stats = _image_region_stats(frame, nose_rect)
        lower_box_stats = _image_region_stats(frame, lower_box_rect)

        y_channel = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)[:, :, 0]
        bg = y_channel[
            max(0, fy1 - int(face_h * 1.2)):min(frame_h, fy1 + int(face_h * 0.2)),
            max(0, fx1 - int(face_w * 0.8)):min(frame_w, fx2 + int(face_w * 0.8))
        ]
        bg_mean = float(np.mean(bg)) if bg.size else 0.0
        bg_bright_ratio = float(np.mean(bg > 210)) if bg.size else 0.0
        bg_very_bright_ratio = float(np.mean(bg > 235)) if bg.size else 0.0
        contrast = bg_mean - face_stats["mean_y"]

        result.update({
            "face_region": face_stats,
            "eye_band": eye_stats,
            "mouth_region": mouth_stats,
            "nose_region": nose_stats,
            "lower_box_region": lower_box_stats,
            "background_mean_y": bg_mean,
            "background_bright_ratio": bg_bright_ratio,
            "background_very_bright_ratio": bg_very_bright_ratio,
            "background_face_contrast": contrast,
            "face_width_px": face_w,
        })

        head_cover_shadow = (
            face_w >= 430.0 and
            face_stats["valid"] and
            eye_stats["valid"] and
            mouth_stats["valid"] and
            nose_stats["valid"] and
            lower_box_stats["valid"] and
            face_stats["mean_y"] < 62.0 and
            face_stats["dark_ratio"] > 0.58 and
            face_stats["very_dark_ratio"] > 0.18 and
            face_stats["skin_ratio"] < 0.68 and
            face_stats["edge_density"] > 0.030 and
            face_stats["laplacian"] > 35.0 and
            eye_stats["dark_ratio"] > 0.45 and
            lower_box_stats["dark_ratio"] > 0.60 and
            nose_stats["skin_ratio"] > 0.75 and
            mouth_stats["skin_ratio"] > 0.65 and
            bg_bright_ratio > 0.08 and
            bg_very_bright_ratio > 0.04 and
            contrast > 55.0
        )

        if head_cover_shadow:
            result["is_head_cover_shadow"] = True
            result["reason"] = "head_cover_shadow"

        return result
    except Exception as e:
        LOGGER.error(f"Head cover shadow analysis failed: {e}")
        result["reason"] = "error"
        return result

def is_sunset_condition(frame, box, points):
    """
    Check if the image exhibits 'sunset' characteristics (Overexposure + Redness).
    
    Logic:
    1. Overexposure Ratio: > 7% pixels with V > 240 (Using Center 60% Crop)
    2. Red/Blue Ratio: > 1.3 (Red dominance)
    
    Returns:
    is_sunset (bool): True if both conditions met.
    """

    # Calculate Entropy if needed (for extreme overexposure)
    def calculate_entropy(img_roi):
        try:
            gray = cv2.cvtColor(img_roi, cv2.COLOR_BGR2GRAY)
            # Calculate Histogram
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
            # Normalize
            prob = hist / (hist.sum() + 1e-6)
            # Entropy: -sum(p * log2(p))
            # Remove zeros for log calculation
            prob = prob[prob > 0]
            ent = -np.sum(prob * np.log2(prob))
            return ent
        except: return 0.0

    try:
        x1, y1, x2, y2 = map(int, box)
        h, w, _ = frame.shape
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False
            
        # [2026-02-07 Fix] Use Center 60% Crop to ignore background overexposure
        roi_h, roi_w, _ = roi.shape
        margin_x = int(roi_w * 0.2)
        margin_y = int(roi_h * 0.2)
        
        center_roi = roi[margin_y:roi_h-margin_y, margin_x:roi_w-margin_x]
        
        # Fallback if center crop is too small
        if center_roi.size == 0: center_roi = roi
            
        # 1. Overexposure (Saturation)
        hsv_roi = cv2.cvtColor(center_roi, cv2.COLOR_BGR2HSV)
        v_channel = hsv_roi[:, :, 2]
        overexposed_ratio = np.sum(v_channel > 240) / v_channel.size
        
        # 2. R/B Ratio
        b_mean = np.mean(center_roi[:, :, 0])
        r_mean = np.mean(center_roi[:, :, 2])
        rb_ratio = r_mean / (b_mean + 1e-6)
        
        # 3. Horizontal Ratio (Left vs Right Face) - Only Horizontal!
        # Map nose point to center roi coordinates
        nose_x_frame = int(points[2][0])
        
        nose_x_center = nose_x_frame - (x1 + margin_x)
        c_h, c_w = v_channel.shape
        h_ratio = 1.0
        
        if 0 < nose_x_center < c_w:
            left = v_channel[:, :nose_x_center]
            right = v_channel[:, nose_x_center:]
            l_br = np.mean(left)
            r_br = np.mean(right)
            h_ratio = max(l_br, r_br) / (min(l_br, r_br) + 1e-6)
        
        # Thresholds:
        # 1. Extreme Overexposure (Conditional Killer)
        if overexposed_ratio > 0.15:
            # [2026-02-07 Fix] Check Entropy to save valid detailed images
            # Threshold lowered to 7.2 to save 00:00 batch (Ent ~7.4), while killing bad ones (Ent ~7.0)
            ent = calculate_entropy(center_roi)
            if ent > 7.2:
                return False # SAVE (High detail)
            return True # KILL (Low detail / Washed out)
            
        # 2. Moderate Overexposure (Conditional Killer)
        if overexposed_ratio > 0.07:
            # Only kill if Horizontal lighting is uneven (Side Shadow)
            # Ignore Vertical Shadow (to save Huang Shi-Yu)
            if h_ratio > 1.5:
                return True
            
        return False
    except Exception as e:
        LOGGER.error(f"Sunset check failed: {e}")
        return False


def analyze_low_light(frame, box=None):
    """
    Measure low-light risk on face ROI when available, otherwise center ROI.
    Uses Y channel so color cast has less impact than raw RGB/BGR means.
    """
    try:
        h, w = frame.shape[:2]
        if box is not None:
            x1, y1, x2, y2 = map(int, box)
            pad_x = int(max(4, (x2 - x1) * 0.12))
            pad_y = int(max(4, (y2 - y1) * 0.12))
            x1 = max(0, x1 - pad_x)
            y1 = max(0, y1 - pad_y)
            x2 = min(w, x2 + pad_x)
            y2 = min(h, y2 + pad_y)
            roi = frame[y1:y2, x1:x2]
        else:
            x1, x2 = int(w * 0.35), int(w * 0.65)
            y1, y2 = int(h * 0.25), int(h * 0.75)
            roi = frame[y1:y2, x1:x2]

        if roi.size == 0:
            roi = frame

        y_channel = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)[:, :, 0]
        mean_y = float(np.mean(y_channel))
        std_y = float(np.std(y_channel))
        dark_ratio = float(np.mean(y_channel < 45))
        very_dark_ratio = float(np.mean(y_channel < 30))

        too_dark = mean_y < 55 or (mean_y < 70 and dark_ratio > 0.35) or very_dark_ratio > 0.35
        return {
            "mean_y": mean_y,
            "std_y": std_y,
            "dark_ratio": dark_ratio,
            "very_dark_ratio": very_dark_ratio,
            "too_dark": bool(too_dark)
        }
    except Exception as e:
        LOGGER.error(f"Low-light analysis failed: {e}")
        return {
            "mean_y": 255.0,
            "std_y": 0.0,
            "dark_ratio": 0.0,
            "very_dark_ratio": 0.0,
            "too_dark": False
        }


def enhance_low_light_frame(frame, gamma=0.72, clahe_clip=1.6, clahe_grid=(8, 8)):
    """
    Conservative low-light enhancement for recognition.
    Only adjusts luminance (Y), preserving chroma to avoid skin-color distortion.
    """
    try:
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        y, cr, cb = cv2.split(ycrcb)

        table = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)]).astype("uint8")
        y_gamma = cv2.LUT(y, table)

        clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=clahe_grid)
        y_enhanced = clahe.apply(y_gamma)

        # Blend with gamma result to avoid CLAHE halos/noise amplification.
        y_final = cv2.addWeighted(y_gamma, 0.65, y_enhanced, 0.35, 0)
        merged = cv2.merge((y_final, cr, cb))
        return cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
    except Exception as e:
        LOGGER.error(f"Low-light enhancement failed: {e}")
        return frame


def analyze_backlight_glare(frame, box):
    """
    Detect light interference on the face itself. External light sources around
    the face are intentionally ignored; only face-region washout, flare streaks,
    and local overexposure that damages facial details should reject.
    """
    try:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = map(int, box)
        fw = max(1, x2 - x1)
        fh = max(1, y2 - y1)

        fx1, fy1 = max(0, x1), max(0, y1)
        fx2, fy2 = min(w, x2), min(h, y2)
        y_channel = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)[:, :, 0]
        face_y = y_channel[fy1:fy2, fx1:fx2]
        if face_y.size == 0:
            return {"is_backlight_glare": False}

        face_mean = float(np.mean(face_y))
        face_std = float(np.std(face_y))
        face_p50 = float(np.percentile(face_y, 50))
        face_p95 = float(np.percentile(face_y, 95))
        face_p99 = float(np.percentile(face_y, 99))

        face_bgr = frame[fy1:fy2, fx1:fx2]
        face_gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
        face_lap = float(cv2.Laplacian(face_gray, cv2.CV_64F).var())

        face_hsv = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2HSV)
        upper_y1 = int(fh * 0.12)
        upper_y2 = max(upper_y1 + 1, int(fh * 0.62))
        upper_y = face_y[upper_y1:upper_y2, :]
        upper_s = face_hsv[upper_y1:upper_y2, :, 1]
        upper_h = face_hsv[upper_y1:upper_y2, :, 0]
        upper_gray = face_gray[upper_y1:upper_y2, :]
        upper_lap = (
            float(cv2.Laplacian(upper_gray, cv2.CV_64F).var())
            if upper_gray.size else 0.0
        )
        face_flare_streak_ratio = 0.0
        face_flare_component_ratio = 0.0
        face_flare_span_ratio = 0.0
        face_flare_height_ratio = 0.0
        if upper_y.size:
            flare_mask = (
                ((upper_y > 170) & (upper_s < 95)) |
                ((upper_y > 150) & (upper_s > 20) &
                 (upper_h >= 75) & (upper_h <= 120))
            ).astype(np.uint8)
            face_flare_streak_ratio = float(np.mean(flare_mask))
            num, _, stats, _ = cv2.connectedComponentsWithStats(
                flare_mask, 8)
            largest_area = 0
            largest_width = 0
            largest_height = 0
            for label_idx in range(1, num):
                area = int(stats[label_idx, cv2.CC_STAT_AREA])
                comp_width = int(stats[label_idx, cv2.CC_STAT_WIDTH])
                comp_height = int(stats[label_idx, cv2.CC_STAT_HEIGHT])
                if area > largest_area:
                    largest_area = area
                    largest_width = comp_width
                    largest_height = comp_height
            face_flare_component_ratio = float(
                largest_area / max(1, upper_y.size))
            face_flare_span_ratio = float(largest_width / max(1, fw))
            face_flare_height_ratio = float(largest_height / max(1, upper_y.shape[0]))

        hot_mask = ((face_y > 235) & (face_hsv[:, :, 1] < 120)).astype(np.uint8)
        hot_ratio = float(np.mean(hot_mask)) if hot_mask.size else 0.0
        num, _, stats, _ = cv2.connectedComponentsWithStats(hot_mask, 8)
        largest_hot = int(stats[1:, cv2.CC_STAT_AREA].max()) if num > 1 else 0
        hot_component_ratio = float(largest_hot / max(1, face_y.size))

        face_flare_affected = (
            fw >= 430 and
            face_mean < 140 and
            face_std < 35 and
            face_lap < 120 and
            upper_lap < 50 and
            face_flare_streak_ratio > 0.025 and
            face_flare_component_ratio > 0.010 and
            face_flare_span_ratio > 0.15
        )
        hot_patch_detail_loss = (
            fw >= 300 and
            face_std < 40 and
            face_lap < 65 and
            upper_lap < 45 and
            hot_ratio > 0.050 and
            hot_component_ratio > 0.025
        )
        washed_face_glare = (
            fw >= 430 and
            20 <= face_lap < 70 and
            face_mean > 150 and
            face_std < 45 and
            face_flare_streak_ratio > 0.035
        )
        split_light_shadow_damage = (
            fw >= 430 and
            face_std < 35 and
            face_lap < 95 and
            face_p95 - face_p50 > 55 and
            face_p99 > 205 and
            face_flare_component_ratio > 0.008 and
            face_flare_span_ratio > 0.12
        )
        face_light_interference = bool(
            face_flare_affected or
            hot_patch_detail_loss or
            washed_face_glare or
            split_light_shadow_damage
        )

        return {
            "is_backlight_glare": face_light_interference,
            "is_face_light_interference": face_light_interference,
            "face_mean_y": face_mean,
            "face_std_y": face_std,
            "face_p50_y": face_p50,
            "face_p95_y": face_p95,
            "face_p99_y": face_p99,
            "face_laplacian": face_lap,
            "upper_laplacian": upper_lap,
            "washed_face_glare": bool(washed_face_glare),
            "face_flare_streak_ratio": face_flare_streak_ratio,
            "face_flare_component_ratio": face_flare_component_ratio,
            "face_flare_span_ratio": face_flare_span_ratio,
            "face_flare_height_ratio": face_flare_height_ratio,
            "face_flare_affected": bool(face_flare_affected),
            "hot_ratio": hot_ratio,
            "hot_component_ratio": hot_component_ratio,
            "hot_patch_detail_loss": bool(hot_patch_detail_loss),
            "split_light_shadow_damage": bool(split_light_shadow_damage),
        }
    except Exception as e:
        LOGGER.error(f"Face light interference analysis failed: {e}")
        return {"is_backlight_glare": False}


def analyze_face_blur(frame, box, pose=None):
    """Detect face blur with Laplacian plus Sobel energy, avoiding low-texture false positives."""
    try:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = map(int, box)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        face = frame[y1:y2, x1:x2]
        if face.size == 0:
            return {"is_blur": False}

        gray = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = float(np.mean(sobel_x * sobel_x + sobel_y * sobel_y))
        face_w = x2 - x1

        # Fixed-size inner-face metrics reduce site/face-width dependence. The
        # crop avoids hair/background and measures the same scale for every site.
        normalized_lap = 999.0
        normalized_tenengrad = 99999.0
        normalized_edge_density = 1.0
        normalized_std_y = 0.0
        normalized_hard_blur = False
        vertical_smear_strength = 0.0
        vertical_smear_width_ratio = 0.0
        vertical_smear_edge_density = 1.0
        try:
            fh, fw_face = face.shape[:2]
            ix1 = int(fw_face * 0.12)
            ix2 = int(fw_face * 0.88)
            iy1 = int(fh * 0.18)
            iy2 = int(fh * 0.82)
            inner_face = face[iy1:iy2, ix1:ix2]
            if inner_face.size > 0:
                norm_gray = cv2.cvtColor(
                    cv2.resize(inner_face, (224, 224), interpolation=cv2.INTER_AREA),
                    cv2.COLOR_BGR2GRAY,
                )
                normalized_lap = float(cv2.Laplacian(norm_gray, cv2.CV_64F).var())
                norm_sobel_x = cv2.Sobel(norm_gray, cv2.CV_64F, 1, 0, ksize=3)
                norm_sobel_y = cv2.Sobel(norm_gray, cv2.CV_64F, 0, 1, ksize=3)
                normalized_tenengrad = float(np.mean(norm_sobel_x * norm_sobel_x + norm_sobel_y * norm_sobel_y))
                normalized_edge_density = float(np.mean(cv2.Canny(norm_gray, 45, 110) > 0))
                normalized_std_y = float(norm_gray.std())
                normalized_mean_y = float(norm_gray.mean())
                
                # [2026-06-16 Fix] Relax blur threshold in low light to tolerate denoising
                lap_thresh = 8.0 if normalized_mean_y < 105.0 else 12.0
                edge_thresh = 0.004 if normalized_mean_y < 105.0 else 0.008
                
                normalized_hard_blur = (
                    normalized_lap < lap_thresh and
                    normalized_tenengrad < 550.0 and
                    normalized_edge_density < edge_thresh
                )
                smear_region = norm_gray[int(224 * 0.10):int(224 * 0.88), :]
                if smear_region.size > 0:
                    col_mean = np.mean(smear_region, axis=0).astype(np.float32)
                    kernel = np.ones(13, dtype=np.float32) / 13.0
                    col_smooth = np.convolve(col_mean, kernel, mode="same")
                    baseline = float(np.median(col_smooth))
                    spread = float(np.std(col_smooth))
                    threshold = baseline + max(7.0, spread * 0.75)
                    bright_cols = col_smooth > threshold
                    if np.any(bright_cols):
                        runs = []
                        start = None
                        for idx, is_bright in enumerate(bright_cols):
                            if is_bright and start is None:
                                start = idx
                            elif not is_bright and start is not None:
                                runs.append((start, idx))
                                start = None
                        if start is not None:
                            runs.append((start, len(bright_cols)))
                        run = max(runs, key=lambda item: item[1] - item[0])
                        run_w = max(1, run[1] - run[0])
                        vertical_smear_width_ratio = float(run_w / len(bright_cols))
                        vertical_smear_strength = float(np.max(col_smooth) - baseline)
                        streak = smear_region[:, run[0]:run[1]]
                        if streak.size > 0:
                            vertical_smear_edge_density = float(
                                np.mean(cv2.Canny(streak, 45, 110) > 0))
        except Exception:
            normalized_hard_blur = False

        pose_yaw = 0.0
        try:
            if pose is not None and len(pose) >= 2:
                pose_yaw = abs(float(pose[1]))
        except Exception:
            pose_yaw = 0.0

        y_channel = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)[:, :, 0]
        fw = max(1, x2 - x1)
        fh = max(1, y2 - y1)
        bg = y_channel[max(0, y1 - int(fh * 1.2)):min(h, y1 + int(fh * 0.2)),
                       max(0, x1 - int(fw * 0.8)):min(w, x2 + int(fw * 0.8))]
        bright_ratio = float(np.mean(bg > 210)) if bg.size else 0.0
        very_bright_ratio = float(np.mean(bg > 235)) if bg.size else 0.0

        severe_low_texture_blur = (
            face_w < 430 and
            lap < 20 and
            tenengrad < 800 and
            bright_ratio > 0.12 and
            very_bright_ratio > 0.05
        )
        mid_low_texture_blur = (
            340 <= face_w < 390 and
            lap < 18 and
            tenengrad < 700 and
            bright_ratio < 0.08 and # [2026-06-09 Fix v5] Relax from 0.04 to catch W340 blur
            very_bright_ratio < 0.04
        )
        low_light_small_face_blur = (
            340 <= face_w < 390 and
            18 <= lap < 30 and
            tenengrad < 550 and
            bright_ratio < 0.04 and
            very_bright_ratio < 0.02
        )
        low_light_tiny_face_blur = (
            300 <= face_w < 340 and
            28 <= lap < 42 and
            tenengrad < 650 and
            bright_ratio < 0.06 and
            very_bright_ratio < 0.02
        )
        bright_tiny_face_low_texture_blur = (
            300 <= face_w < 340 and
            lap < 18 and
            tenengrad < 560 and
            bright_ratio > 0.18 and
            very_bright_ratio < 0.04
        )
        bright_small_face_low_texture_blur = (
            330 <= face_w < 390 and
            lap < 28 and
            tenengrad < 480 and
            bright_ratio > 0.08 and
            very_bright_ratio < 0.04
        )
        frontal_bright_low_texture_blur = (
            430 <= face_w < 490 and
            pose_yaw < 18.0 and
            lap < 12 and
            tenengrad < 540 and
            bright_ratio > 0.12 and
            very_bright_ratio < 0.05
        )
        bright_low_texture_blur = (
            430 <= face_w < 490 and
            lap < 26 and
            tenengrad < 650 and
            bright_ratio > 0.27 and
            very_bright_ratio < 0.05
        )
        visible_low_texture_blur = (
            (
                330 <= face_w < 430 and
                30 <= lap < 45 and
                tenengrad < 1000 and
                bright_ratio < 0.04 and
                very_bright_ratio < 0.02
            ) or
            (
                400 <= face_w < 470 and
                25 <= lap < 45 and
                tenengrad < 700 and
                bright_ratio < 0.02 and
                very_bright_ratio < 0.005
            )
        )
        large_borderline_soft_blur = (
            face_w >= 600 and
            bright_ratio > 0.25 and
            lap < 8.0 and
            tenengrad < 450.0 and
            normalized_lap < 12.0 and
            normalized_tenengrad < 650.0 and
            normalized_edge_density < 0.010
        )
        normalized_low_texture_blur = (
            normalized_lap < 13.0 and
            normalized_tenengrad < 820.0 and
            normalized_edge_density < 0.018
        )
        normalized_motion_blur = (
            normalized_lap < 14.0 and
            normalized_tenengrad < 500.0 and
            normalized_edge_density < 0.006
        )
        medium_face_low_texture_blur = (
            400 <= face_w < 430 and
            lap < 28 and
            tenengrad < 700 and
            bright_ratio < 0.05 and
            very_bright_ratio < 0.02
        )
        glare_smear_blur = face_w < 400 and lap < 50 and tenengrad > 1500 and very_bright_ratio > 0.15
        blur_small_face_glare = face_w < 340 and lap < 130 and tenengrad < 1400 and very_bright_ratio > 0.20
        side_motion_blur = (
            400 <= face_w < 500 and
            pose_yaw > 25.0 and
            lap < 30 and
            tenengrad < 450
        )
        medium_dark_soft_blur = (
            430 <= face_w < 490 and
            lap < 16 and
            tenengrad < 900 and
            bright_ratio < 0.18 and
            very_bright_ratio < 0.08
        )
        medium_vertical_motion_blur = (
            400 <= face_w < 470 and
            pose_yaw < 16.0 and
            lap < 24.0 and
            tenengrad < 1300.0 and
            normalized_lap < 26.0 and
            normalized_tenengrad < 1900.0 and
            normalized_edge_density < 0.032 and
            normalized_std_y > 30.0 and
            0.10 <= vertical_smear_width_ratio <= 0.25 and
            vertical_smear_strength >= 35.0 and
            vertical_smear_edge_density < 0.026
        )
        large_motion_smear_blur = (
            490 <= face_w < 540 and
            60 <= lap < 85 and
            tenengrad < 2300 and
            pose_yaw > 12.0 and
            bright_ratio > 0.04 and
            bright_ratio < 0.10 and
            very_bright_ratio < 0.06
        )
        large_low_detail_blur = (
            face_w >= 540 and
            25 <= lap < 35 and
            1100 < tenengrad < 1800 and
            bright_ratio < 0.10 and
            very_bright_ratio < 0.06 and
            float(gray.mean()) >= 105.0 # [2026-06-16 Fix] Do not trigger low detail blur in dark/denoised images
        )
        large_soft_motion_blur = (
            490 <= face_w < 540 and
            pose_yaw < 18.0 and
            45 <= lap < 70 and
            tenengrad < 1900 and
            bright_ratio < 0.12 and
            very_bright_ratio < 0.07
        )
        large_frontal_low_texture_blur = (
            490 <= face_w < 540 and
            pose_yaw < 18.0 and
            lap < 23 and
            tenengrad < 430
        )
        normalized_detail_soft_blur = (
            face_w >= 400 and
            pose_yaw < 14.0 and
            28.0 <= lap < 30.0 and
            normalized_lap < 36.0 and
            normalized_tenengrad < 3200.0 and
            normalized_edge_density < 0.048 and
            normalized_std_y < 42.0
        )
        normalized_low_detail_soft_blur = (
            face_w >= 400 and
            pose_yaw < 14.0 and
            18.0 <= lap < 28.0 and
            normalized_lap < 16.0 and
            normalized_tenengrad < 1700.0 and
            normalized_edge_density < 0.018 and
            normalized_std_y < 42.0
        )
        normalized_detail_blur_reject = (
            pose_yaw < 18.0 and
            normalized_std_y < 42.0 and
            (
                normalized_detail_soft_blur or
                normalized_low_detail_soft_blur
            )
        )
        dark_low_texture_blur = (
            lap < 25 and
            tenengrad < 500 and # [2026-06-10 Fix] Strict tenengrad to separate clear large faces (ten=535) from blurry (ten=493)
            bright_ratio < 0.08 and
            very_bright_ratio < 0.02
        )
        huge_face_low_texture_blur = (
            face_w >= 540 and
            lap < 11.5 and # [2026-06-10 Fix] Relax to 11.5 to spare clear massive faces
            tenengrad < 400 and
            bright_ratio < 0.15 # [2026-06-10 Fix] Revert to 0.15 to avoid catching reflective vests
        )

        # [2026-06-10] Rain droplet blur: water droplets on lens cover distort/occlude
        # facial features. Global Laplacian may be normal or even high (droplet edges
        # create contrast), but eye-region texture is severely degraded and many
        # scattered small bright blobs appear from droplet reflections.
        rain_droplet_blur = False
        rain_eye_lap = 0.0
        rain_eye_ten = 0.0
        rain_blob_count = 0
        if face_w >= 450:
            face_roi_h = face.shape[0]
            face_gray = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
            eye_y1_r = int(face_roi_h * 0.2)
            eye_y2_r = int(face_roi_h * 0.45)
            eye_region = face_gray[eye_y1_r:eye_y2_r, :]
            if eye_region.size > 0:
                rain_eye_lap = float(cv2.Laplacian(eye_region, cv2.CV_64F).var())
                sobel_ex = cv2.Sobel(eye_region, cv2.CV_64F, 1, 0, ksize=3)
                sobel_ey = cv2.Sobel(eye_region, cv2.CV_64F, 0, 1, ksize=3)
                rain_eye_ten = float(np.mean(sobel_ex ** 2 + sobel_ey ** 2))

                bright_mask_r = (face_gray > 180).astype(np.uint8)
                num_labels_r, _, blob_stats_r, _ = cv2.connectedComponentsWithStats(bright_mask_r, 8)
                rain_blob_count = sum(
                    1 for i in range(1, num_labels_r)
                    if 5 < blob_stats_r[i, cv2.CC_STAT_AREA] < 200
                )

                rain_droplet_blur = (
                    rain_blob_count >= 40 and
                    rain_eye_ten < 300
                ) or (
                    rain_blob_count >= 30 and
                    rain_eye_ten < 250
                ) or (
                    rain_blob_count >= 35 and # [2026-06-10 Fix] 20->35: prevent slight texture/sweat/glasses from triggering
                    rain_eye_ten < 350 and
                    face_w >= 500
                )
        feature_low_texture_quality_risk = (
            face_w >= 430 and
            pose_yaw < 18.0 and
            (
                (
                    lap < 30 and
                    tenengrad < 1000 and
                    rain_eye_lap < 15 and
                    rain_eye_ten < 1000 and
                    bright_ratio < 0.18 and
                    very_bright_ratio < 0.08
                ) or (
                    lap < 48 and
                    tenengrad < 950 and
                    rain_eye_lap < 12 and
                    rain_eye_ten < 700 and
                    bright_ratio < 0.16 and
                    very_bright_ratio < 0.07
                )
            )
        )
        wet_lens_low_texture_quality_risk = (
            face_w >= 430 and
            pose_yaw < 18.0 and
            lap < 48 and
            tenengrad < 1100 and
            rain_eye_lap < 14 and
            rain_eye_ten < 950 and
            0.07 <= bright_ratio < 0.18 and
            very_bright_ratio < 0.08
        )
        wet_lens_low_detail_occlusion = (
            face_w >= 540 and
            pose_yaw < 18.0 and
            lap < 26.0 and
            tenengrad < 850.0 and
            normalized_edge_density < 0.020 and
            rain_eye_lap < 9.5 and
            rain_eye_ten < 260.0
        )
        wet_lens_vertical_smear_occlusion = (
            face_w >= 430 and
            pose_yaw < 18.0 and
            (feature_low_texture_quality_risk or wet_lens_low_texture_quality_risk) and
            lap < 36.0 and
            tenengrad < 1150.0 and
            rain_eye_lap < 12.0 and
            rain_eye_ten < 520.0 and
            0.035 <= vertical_smear_width_ratio <= 0.22 and
            vertical_smear_strength >= 10.0 and
            vertical_smear_edge_density < 0.040
        )
        face_detail_occlusion = wet_lens_low_detail_occlusion

        hard_blur_reject = (
            normalized_hard_blur or
            normalized_low_texture_blur or
            normalized_motion_blur or
            large_borderline_soft_blur or
            severe_low_texture_blur or
            mid_low_texture_blur or
            low_light_small_face_blur or
            low_light_tiny_face_blur or
            bright_tiny_face_low_texture_blur or
            bright_small_face_low_texture_blur or
            frontal_bright_low_texture_blur or
            bright_low_texture_blur or
            visible_low_texture_blur or
            medium_face_low_texture_blur or
            glare_smear_blur or
            blur_small_face_glare or
            side_motion_blur or
            medium_dark_soft_blur or
            medium_vertical_motion_blur or
            large_motion_smear_blur or
            large_low_detail_blur or
            large_soft_motion_blur or
            large_frontal_low_texture_blur or
            normalized_detail_blur_reject or
            huge_face_low_texture_blur or
            dark_low_texture_blur or
            face_detail_occlusion
            # rain_droplet_blur or
            # wet_lens_vertical_smear_occlusion
        )

        return {
            "is_blur": bool(hard_blur_reject),
            "laplacian": lap,
            "tenengrad": tenengrad,
            "normalized_laplacian": normalized_lap,
            "normalized_tenengrad": normalized_tenengrad,
            "normalized_edge_density": normalized_edge_density,
            "normalized_std_y": normalized_std_y,
            "pose_yaw_abs": pose_yaw,
            "background_bright_ratio": bright_ratio,
            "background_very_bright_ratio": very_bright_ratio,
            "normalized_hard_blur": bool(normalized_hard_blur),
            "normalized_low_texture_blur": bool(normalized_low_texture_blur),
            "normalized_motion_blur": bool(normalized_motion_blur),
            "large_borderline_soft_blur": bool(large_borderline_soft_blur),
            "blur_mid_face": bool(severe_low_texture_blur or glare_smear_blur),
            "severe_low_texture_blur": bool(severe_low_texture_blur),
            "mid_low_texture_blur": bool(mid_low_texture_blur),
            "low_light_small_face_blur": bool(low_light_small_face_blur),
            "low_light_tiny_face_blur": bool(low_light_tiny_face_blur),
            "bright_tiny_face_low_texture_blur": bool(bright_tiny_face_low_texture_blur),
            "bright_small_face_low_texture_blur": bool(bright_small_face_low_texture_blur),
            "frontal_bright_low_texture_blur": bool(frontal_bright_low_texture_blur),
            "bright_low_texture_blur": bool(bright_low_texture_blur),
            "visible_low_texture_blur": bool(visible_low_texture_blur),
            "medium_face_low_texture_blur": bool(medium_face_low_texture_blur),
            "glare_smear_blur": bool(glare_smear_blur),
            "blur_small_face_glare": bool(blur_small_face_glare),
            "side_motion_blur": bool(side_motion_blur),
            "medium_dark_soft_blur": bool(medium_dark_soft_blur),
            "medium_vertical_motion_blur": bool(medium_vertical_motion_blur),
            "large_motion_smear_blur": bool(large_motion_smear_blur),
            "large_low_detail_blur": bool(large_low_detail_blur),
            "large_soft_motion_blur": bool(large_soft_motion_blur),
            "large_frontal_low_texture_blur": bool(large_frontal_low_texture_blur),
            "normalized_detail_soft_blur": bool(normalized_detail_soft_blur),
            "normalized_low_detail_soft_blur": bool(normalized_low_detail_soft_blur),
            "normalized_detail_blur_reject": bool(normalized_detail_blur_reject),
            "huge_face_low_texture_blur": bool(huge_face_low_texture_blur),
            "rain_droplet_blur": bool(rain_droplet_blur),
            "face_detail_occlusion": bool(face_detail_occlusion),
            "wet_lens_low_detail_occlusion": bool(wet_lens_low_detail_occlusion),
            "wet_lens_vertical_smear_occlusion": bool(wet_lens_vertical_smear_occlusion),
            "feature_low_texture_quality_risk": bool(feature_low_texture_quality_risk),
            "wet_lens_low_texture_quality_risk": bool(wet_lens_low_texture_quality_risk),
            "rain_eye_laplacian": rain_eye_lap,
            "rain_eye_tenengrad": rain_eye_ten,
            "rain_blob_count": rain_blob_count,
            "vertical_smear_strength": vertical_smear_strength,
            "vertical_smear_width_ratio": vertical_smear_width_ratio,
            "vertical_smear_edge_density": vertical_smear_edge_density,
        }
    except Exception as e:
        LOGGER.error(f"Face blur analysis failed: {e}")
        return {"is_blur": False}
