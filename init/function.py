
import datetime
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
                
                start_time = datetime.datetime.strptime(start_str, "%H:%M").time()
                end_time = datetime.datetime.strptime(end_str, "%H:%M").time()
                
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
                start_time = datetime.datetime.strptime(period.get("start", "00:00"), "%H:%M").time()
                end_time = datetime.datetime.strptime(period.get("end", "00:00"), "%H:%M").time()
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
