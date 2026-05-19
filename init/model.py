import json
import os
import threading
import time
import math
from collections import defaultdict

import cv2
import numpy as np
import numba as nb
import torch
from init.log import LOGGER, PERF_LOGGER
from datetime import datetime
import pytz
import json
from PIL import Image
from init.function import crop_face_without_forehead, check_in_out_qrcode, check_in_out
from init.mediapipe_handler import MediaPipeHandler


@nb.jit
def cosine_similarity(vec1, vec2):
    """
    計算兩個向量之間的餘弦相似度。

    Parameters:
    vec1 (np.ndarray): 向量1
    vec2 (np.ndarray): 向量2

    Returns:
    float: 餘弦相似度
    """
    dot_product = np.dot(vec1, vec2)
    norm_vec1 = np.linalg.norm(vec1)
    norm_vec2 = np.linalg.norm(vec2)
    return dot_product / (norm_vec1 * norm_vec2)


# 載入設定檔
with open(os.path.join(os.path.dirname(__file__), "../config.json"), "r", encoding="utf-8") as json_file:
    CONFIG = json.load(json_file)
CAMERA = {0: "inCamera", 1: "outCamera"}
CAM_NAME_MAP = {0: "入口", 1: "出口"}
POTENTIAL_MISS_RATIO = 0.8
Z_SCORE_THRESHOLD = 1.5

test_img = cv2.imread(os.path.join(
    os.path.dirname(__file__), "../other/test_img.jpg"))
test_img = cv2.resize(test_img, (224, 224))
tensor_test_img = torch.from_numpy(test_img).unsqueeze(0).permute(0, 3, 1, 2)


class Detector:
    """
    從系統中的即時畫面中偵測人臉，並觸發衣著（反光衣、安全帽）辨識功能。
    若為主畫面 (frame_num == 0)，會進行暖機與衣著辨識。
    """

    def __init__(self, frame_num, system):
        """
        使用 MediaPipe 替代 MTCNN。
        """
        self.system = system
        self.frame_num = frame_num
        self.TIMEZONE = pytz.timezone('Asia/Taipei')
        self.stop_threads = False
        self.last_face_time = 0
        self.last_no_face_log_time = 0
        self.clothe_time = [0.0, 0.0, 0.0]
        # Vest at distance is more prone to intermittent YOLO misses than helmet.
        # Hold recent valid detections briefly so a standing user does not flicker out.
        self.clothe_hold_seconds = [2.5, 0.0, 1.5]
        # 初始化 MediaPipe 處理器
        self.mp_handler = MediaPipeHandler()

        # [2026-02-04 Feature] QR Code Detector
        self.qr_detector = cv2.QRCodeDetector()
        self.last_qr_time = 0
        self.last_qr_data = ""
        self.qr_scan_interval = 1.0  # 1 FPS limit
        self.last_qr_scan_time = 0

        # [2026-02-03 Fix] 初始化衣著偵測旗標
        # 僅在入口攝影機 (frame_num == 0) 且設定開啟時執行
        # [2026-02-06 Fix] 若開啟 "Detection" (攔截)，即使 "Show" (顯示框) 關閉，也必須執行偵測，否則會因狀態全 False 而永久攔截
        clothes_show = CONFIG.get("Clothes_show", False)
        clothes_det = CONFIG.get("Clothes_detection", False)
        self.do_clothes = (self.frame_num == 0 and (
            clothes_show or clothes_det))

        LOGGER.info(
            f"[Detector Init] Frame: {self.frame_num}, Clothes_Show: {clothes_show}, Clothes_Det: {clothes_det} -> Do_Clothes: {self.do_clothes}")

        threading.Thread(target=self.face_detector, daemon=True).start()

    def _clothes_gate_required(self):
        return self.do_clothes and (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False))

    def _is_entry_active(self):
        """
        [2026-02-06 Fix] 判斷當前是否為入口模式 (複製自 main.py 邏輯，供 Detector 使用)
        解決單鏡頭模式下，出口時段誤執行衣著偵測與阻斷的問題。
        """
        # 1. 判斷是否為單鏡頭
        ips = [CONFIG["cameraIP"]["in_camera"],
               CONFIG["cameraIP"]["out_camera"]]
        is_single_cam = (ips[0] == ips[1])

        # 雙鏡頭模式：看 frame_num
        if not is_single_cam:
            return self.frame_num == 0

        # 單鏡頭模式：看排程
        schedule_conf = CONFIG.get("Schedule", {})
        if not schedule_conf.get("enabled", False):
            return True  # 無排程預設為入口 (從嚴)

        try:
            now_time = datetime.now().time()
            periods = schedule_conf.get("in_periods", [])
            if not periods:
                start_str = schedule_conf.get("in_start", "06:00")
                end_str = schedule_conf.get("in_end", "17:00")
                periods = [{"start": start_str, "end": end_str}]

            for period in periods:
                start_time = datetime.strptime(
                    period.get("start", "00:00"), "%H:%M").time()
                end_time = datetime.strptime(
                    period.get("end", "00:00"), "%H:%M").time()
                if start_time <= end_time:
                    if start_time <= now_time <= end_time:
                        return True
                else:
                    if start_time <= now_time or now_time <= end_time:
                        return True
            return False
        except:
            return True

    def face_detector(self):
        last_box = None
        last_points = None
        last_time = 0
        last_detection_time = 0
        DETECTION_INTERVAL = self.system.state.detection_interval
        dummy_input = tensor_test_img[0]

        while not self.stop_threads:
            now = time.time()
            if self.system.state.frame[self.frame_num] is not None:
                if now - last_detection_time > DETECTION_INTERVAL:
                    last_detection_time = now

                    self.system.state.max_box[self.frame_num] = last_box
                    self.system.state.max_points[self.frame_num] = last_points
                    new_frame = self.system.state.frame[self.frame_num].copy()

                    # [2026-02-03 Fix] 執行衣著偵測
                    # 使用局部變數暫存結果，偵測完成後再一次性更新全域狀態，避免 Race Condition

                    # [2026-02-09 Refactor] 調整執行順序：先 MediaPipe (取得 Landmarks) 再 Clothes (扣環檢查)

                    new_high_res = None
                    if self.system.state.frame_high_res is not None and self.system.state.frame_high_res[self.frame_num] is not None:
                        new_high_res = self.system.state.frame_high_res[self.frame_num].copy(
                        )

                    # 1. 使用 MediaPipe 偵測人臉 (Full Frame)
                    rgb_frame = cv2.cvtColor(new_frame, cv2.COLOR_BGR2RGB)
                    boxes, _, landmarks = self.mp_handler.detect(rgb_frame)

                    box = None
                    points = None
                    legacy_face_zoom_flag = False
                    clothes_long_distance = CONFIG.get(
                        "Long_distance_mode", False)

                    is_entry_now = self._is_entry_active()
                    clothes_active = self.do_clothes and is_entry_now

                    if boxes is not None:
                        x1, y1, x2, y2 = map(int, boxes[0])
                        points = landmarks[0].copy()

                        # 基礎過濾 (ROI/Size)
                        w_source = new_frame.shape[1]

                        # [2026-02-09 Fix] Align Face ROI with UI/Clothes Mask (35% Width)
                        # Previous logic (1/6 ~ 5/6 = 66% width) was too wide, causing "SmallFace"
                        # warnings when users were visually outside the UI mask.
                        target_ratio = 0.35
                        if CONFIG[CAMERA[self.frame_num]]["close"]:
                            # Matches apply_mask logic (0.5 for close mode)
                            target_ratio = 0.5

                        center = w_source // 2
                        half_w = int(w_source * target_ratio / 2)

                        roi_x1 = max(0, center - half_w)
                        roi_x2 = min(w_source, center + half_w)

                        center_x = (x1 + x2) / 2

                        face_width = x2 - x1
                        min_face_val = self.system.state.min_face[self.frame_num]

                        # [2026-03-06 Revert] Strict threshold when clothes mode is On
                        # Avoid "請靠近" when clothes detection is on, only start when >= min_face
                        det_ratio = 1.0 if clothes_active else POTENTIAL_MISS_RATIO

                        if center_x < roi_x1 or center_x > roi_x2 or face_width < (min_face_val * det_ratio):
                            box = None
                            points = None  # 被過濾掉視為無效
                        else:
                            box = [x1, y1, x2, y2]
                            self.system.mp_detectors[self.frame_num] = self.mp_handler
                            self.last_face_time = time.time()
                    else:
                        # No boxes found
                        face_width = 0

                    if box is None:
                        # Ensure face_width is initialized if boxes was None
                        if 'face_width' not in locals():
                            face_width = 0

                    # 2. 執行衣著偵測。遠距模式只放寬服裝偵測，不放寬臉辨 min_face。
                    should_detect_clothes = clothes_active
                    current_clothes_detections = []
                    # [2026-02-12 Feature] Store detailed JSON log
                    current_clothes_details = {}

                    if should_detect_clothes:
                        local_clothes_state = [False, False, False]
                        self.system.state.clothes_display_suppressed[self.frame_num] = False
                        self.mask_frame, x_offset = self.apply_mask(new_frame)
                        try:
                            # 傳入 points (可能為 None，若無人臉或被過濾)
                            # [2026-02-12 Fix] Updated signature to return details
                            current_clothes_detections, current_clothes_details = self.clothes_detector(
                                x_offset,
                                local_clothes_state,
                                landmarks=points,
                                target_face_box=box,
                                enable_zoom=clothes_long_distance
                            )
                        except Exception as e:
                            LOGGER.error(f"衣著偵測失敗: {e}")

                        # Debounce
                        now_t = time.time()
                        for i in range(3):
                            if not local_clothes_state[i]:
                                if (now_t - self.clothe_time[i]) < self.clothe_hold_seconds[i]:
                                    local_clothes_state[i] = True
                        self.system.state.clothes = local_clothes_state
                        self.system.state.clothes_gate_pass = bool(
                            local_clothes_state[0] and local_clothes_state[2])
                        self.system.state.clothes_gate_time = now if self.system.state.clothes_gate_pass else 0.0
                    else:
                        if self.do_clothes:
                            self.system.state.clothes = [False, False, False]
                            self.system.state.clothes_gate_pass = False
                            self.system.state.clothes_gate_time = 0.0
                            self.system.state.clothes_display_suppressed[self.frame_num] = False

                    # 3. QR Code (省略，保持原位)
                    if CONFIG.get("qrcode_mode", False) and (now - self.last_qr_scan_time > self.qr_scan_interval):
                        # ... (QR Code logic unchanged) ...
                        pass

                    clothes_gate_required = self._clothes_gate_required()
                    clothes_gate_pass = bool(
                        getattr(self.system.state, "clothes_gate_pass", False) and
                        now - getattr(self.system.state,
                                      "clothes_gate_time", 0.0) <= 0.5
                    )

                    # 鐵則：服裝辨識開啟後，未同時通過安全帽+背心，不產生臉辨資料。
                    if clothes_gate_required and not clothes_gate_pass:
                        self.system.state.hint_text[self.frame_num] = "請正確著裝"
                        self.system.state.frame_data[self.frame_num] = None
                        self.system.state.gaze_status[self.frame_num] = None
                        self.system.state.head_pose[self.frame_num] = None
                        if box is not None:
                            self.system.state.max_box[self.frame_num] = box
                            self.system.state.max_points[self.frame_num] = points
                        else:
                            self.system.state.max_box[self.frame_num] = None
                            self.system.state.max_points[self.frame_num] = None
                        last_box = box
                        last_points = points
                        last_time = time.time()
                        continue

                    # 4. 處理人臉後續邏輯 (阻斷/Gaze/Update)
                    if box is not None:
                        # 正常流程 (Gaze, Update State)
                        g_pass, g_msg, g_pose, g_ear = self.mp_handler.check_gaze(
                            0)
                        self.system.state.gaze_status[self.frame_num] = (
                            g_pass, g_msg, g_pose, g_ear)
                        self.system.state.head_pose[self.frame_num] = g_pose

                        g_status = self.system.state.gaze_status[self.frame_num]
                        self.system.state.frame_data[self.frame_num] = (
                            new_frame, g_status, box, points, legacy_face_zoom_flag)
                    else:
                        self.system.state.gaze_status[self.frame_num] = None
                        self.system.state.head_pose[self.frame_num] = None
                        self.system.state.frame_data[self.frame_num] = None

                        # 這裡要處理 "沒人臉" 時的衣著狀態重置嗎？
                        # 不，衣著狀態已經在上面更新過了 (Line 167)
                        # 但如果是單鏡頭出口模式，這裡是否會執行？
                        # 若 box is None，就不會跑阻斷，也不會更新 frame_data，這是對的。

                    self.system.state.max_box[self.frame_num] = box
                    self.system.state.max_points[self.frame_num] = points
                    self.system.state.frame_mtcnn[self.frame_num] = new_frame
                    self.system.state.frame_mtcnn_high_res[self.frame_num] = new_high_res

                    last_box = box
                    last_points = points
                    last_time = time.time()

            time.sleep(0.01)

    def _crop_head_mesh(self, frame, landmarks):
        """
        [2026-02-12 Feature] Precision Head Crop using Face Mesh (468 pts)
        """
        h, w = frame.shape[:2]
        def get_pt(idx): return int(
            landmarks[idx].x * w), int(landmarks[idx].y * h)

        # 10:Top, 152:Chin, 234:Left, 454:Right
        top = get_pt(10)
        chin = get_pt(152)
        left = get_pt(234)
        right = get_pt(454)

        face_h = chin[1] - top[1]
        face_w = right[0] - left[0]

        if face_h <= 0 or face_w <= 0:
            return None

        # [2026-02-12 Tuning] Anchor-based cropping for precise control
        # Expanded pad_top to 1.5 to ensure full helmet visibility
        pad_top = int(face_h * 1.5)

        # Expanded pad_bot to 0.5 to ensure full buckle context
        pad_bot = int(face_h * 0.5)

        # Sides: Anchor at Left/Right cheeks, expanded to 0.6
        pad_side = int(face_w * 0.6)

        y1 = max(0, top[1] - pad_top)
        y2 = min(h, chin[1] + pad_bot)
        x1 = max(0, left[0] - pad_side)
        x2 = min(w, right[0] + pad_side)

        if y2 > y1 and x2 > x1:
            return frame[y1:y2, x1:x2], x1, y1
        return None, 0, 0

    def _crop_body_pose(self, frame, pose_landmarks):
        """
        [2026-02-12 Feature] Precision Body Crop using Pose
        Center-based expansion to avoid partial cuts.
        """
        h, w = frame.shape[:2]
        lm = pose_landmarks.landmark
        def get_xy(idx): return int(lm[idx].x * w), int(lm[idx].y * h)

        ls = get_xy(11)
        rs = get_xy(12)  # Shoulders
        lh = get_xy(23)
        rh = get_xy(24)  # Hips

        # Strict Visibility Check for Shoulders (Crucial for Vest)
        if lm[11].visibility < 0.6 or lm[12].visibility < 0.6:
            return None, 0, 0

        # Calculate Body Center and Dimensions
        center_x = (ls[0] + rs[0] + lh[0] + rh[0]) // 4

        # Shoulder Width
        shoulder_w = abs(rs[0] - ls[0])
        # Torso Height (Shoulder to Hip)
        torso_h = abs(lh[1] - ls[1])

        if shoulder_w <= 0 or torso_h <= 0:
            return None, 0, 0

        # Dynamic Box Size
        # Width: Expanded to 2.2 for more context
        target_w = int(shoulder_w * 2.2)
        # Height: Shoulder to Hip + Neck area
        target_h = int(torso_h * 1.5)

        # Top anchor: Mid-point of shoulders
        shoulder_mid_y = (ls[1] + rs[1]) // 2

        # Expanded top padding to 0.3
        y1 = max(0, shoulder_mid_y - int(target_h * 0.3))
        # Push remaining height down
        y2 = min(h, shoulder_mid_y + int(target_h * 0.85))
        x1 = max(0, center_x - int(target_w * 0.5))
        x2 = min(w, center_x + int(target_w * 0.5))

        if y2 > y1 and x2 > x1:
            return frame[y1:y2, x1:x2], x1, y1
        return None, 0, 0

    def clothes_detector(self, x_offset, state_buffer, landmarks=None, target_face_box=None, enable_zoom=False):
        """
        [2026-03] Simplified PPE Detection (Presence-based)
        Ensures the detected PPE is worn by the target person via spatial overlap.
        """
        if not hasattr(self.system, 'model_clothes') or self.system.model_clothes is None:
            if hasattr(self.system, 'load_clothes_model'):
                self.system.load_clothes_model()
            if not hasattr(self.system, 'model_clothes') or self.system.model_clothes is None:
                return [], {}

        full_frame = self.system.state.frame[self.frame_num]
        if full_frame is None:
            return [], {}

        detections = []
        details = {
            "helmet": {"detected": False, "crop_img": None, "crop_boxes": [], "fallback_box": None},
            "vest": {"detected": False, "crop_img": None, "crop_boxes": [], "fallback_box": None},
            "zoom_enabled": bool(enable_zoom)
        }

        fallback_cache = {}

        def get_fallback_results(region="full"):
            # Use masked frame to horizontally filter background persons.
            # Spatial validator then handles remaining edge cases.
            if region in fallback_cache:
                return fallback_cache[region]

            mask_frame, mx_offset = self.apply_mask(full_frame)
            y_offset = 0

            # For distant vests, full-height frames shrink the torso before YOLO sees it.
            # Crop to body band so the vest occupies more of the detector input.
            if enable_zoom and region.startswith("body"):
                h = mask_frame.shape[0]
                w = mask_frame.shape[1]

                use_dynamic_crop = region.startswith(
                    "body_dynamic") and target_face_box is not None
                if use_dynamic_crop:
                    fx1, fy1, fx2, fy2 = target_face_box
                    face_w = max(1, fx2 - fx1)
                    face_h = max(1, fy2 - fy1)
                    face_cx = int(((fx1 + fx2) / 2) - mx_offset)
                    dynamic_y = {
                        "body_dynamic_high": (-1.1, 4.9),
                        "body_dynamic": (-0.4, 5.8),
                        "body_dynamic_low": (0.3, 6.8),
                    }
                    y_top_mul, y_bot_mul = dynamic_y.get(
                        region, dynamic_y["body_dynamic"])

                    crop_w = max(int(face_w * 6.0), int(w * 0.65))
                    x1 = max(0, face_cx - crop_w // 2)
                    x2 = min(w, face_cx + crop_w // 2)
                    if x2 - x1 < crop_w:
                        if x1 == 0:
                            x2 = min(w, crop_w)
                        elif x2 == w:
                            x1 = max(0, w - crop_w)

                    y1 = max(0, int(fy2 + face_h * y_top_mul))
                    y2 = min(h, int(fy2 + face_h * y_bot_mul))
                    if y2 - y1 < int(h * 0.30):
                        y1 = int(h * 0.12)
                        y2 = int(h * 0.98)
                        x1 = 0
                        x2 = w

                    mask_frame = mask_frame[y1:y2, x1:x2]
                    mx_offset += x1
                    y_offset = y1
                    details["vest"].setdefault("fallback_crops", []).append({
                        "region": region,
                        "box": [mx_offset, y_offset, mx_offset + (x2 - x1), y_offset + (y2 - y1)]
                    })
                else:
                    fixed_y = {
                        "body_fixed_high": (0.04, 0.72),
                        "body_fixed": (0.12, 0.98),
                        "body_fixed_low": (0.25, 1.0),
                        "body": (0.12, 0.98),
                    }
                    y1_ratio, y2_ratio = fixed_y.get(
                        region, fixed_y["body_fixed"])
                    y1 = int(h * y1_ratio)
                    y2 = int(h * y2_ratio)
                    mask_frame = mask_frame[y1:y2, :]
                    y_offset = y1
                    details["vest"].setdefault("fallback_crops", []).append({
                        "region": region,
                        "box": [mx_offset, y_offset, mx_offset + w, y_offset + (y2 - y1)]
                    })

            fallback_conf = 0.08 if enable_zoom and region.startswith("body") else 0.12
            result = (self.system.model_clothes(
                source=mask_frame, iou=0.45, conf=fallback_conf, verbose=False)[0], mx_offset, y_offset)
            fallback_cache[region] = result
            return result

        # --- Universal Spatial Validators ---
        def is_helmet_valid(bx1, by1, bx2, by2, det_conf=0.0):
            if target_face_box is None:
                # Basic relative check for missing face_box (e.g., looking down)
                img_h, img_w = full_frame.shape[:2]
                if by1 < img_h * 0.5 and (bx2 - bx1) > img_w * 0.05:
                    return True, "Passed (No Face Box, Spatial Fallback)"
                return False, "No face box detected and not optimal helmet position"

            fx1, fy1, fx2, fy2 = target_face_box
            face_cx = (fx1 + fx2) / 2
            helmet_cx = (bx1 + bx2) / 2
            face_w = fx2 - fx1
            face_h = fy2 - fy1
            helmet_w = bx2 - bx1
            helmet_h = by2 - by1

            horizontal_aligned = abs(face_cx - helmet_cx) < face_w * 2.0
            not_too_high = by2 >= (fy1 - face_h * 0.3)
            above_chin = by1 < fy2
            width_ratio_ok = (helmet_w >= face_w *
                              0.4) and (helmet_w <= face_w * 2.5)

            # [2026-03-10 Fix] Above-face ratio: what fraction of the helmet bbox is ABOVE fy1?
            # Conf-dependent threshold: high conf real helmets (>=0.5) get lenient check,
            # low conf potential FPs get strict check.
            above_face_px = max(0, fy1 - by1)
            above_face_ratio = above_face_px / helmet_h if helmet_h > 0 else 0
            ratio_threshold = 0.25 if det_conf >= 0.5 else 0.35
            helmet_above_face = above_face_ratio >= ratio_threshold

            if not (horizontal_aligned and not_too_high and above_chin and width_ratio_ok and helmet_above_face):
                return False, (f"Reject: horiz={horizontal_aligned}, not_too_high={not_too_high}, above_chin={above_chin}, "
                               f"width_ratio={helmet_w/face_w:.2f}x, above_face_ratio={above_face_ratio:.2f}(thr={ratio_threshold}) | "
                               f"face_cx={face_cx:.0f}, helmet_cx={helmet_cx:.0f} | "
                               f"hy1,hy2={by1},{by2} fy1,fy2={fy1},{fy2} face_h={face_h}")

            return True, ""

        def is_vest_valid(bx1, by1, bx2, by2, source="crop"):
            if target_face_box is None:
                # Basic relative check for missing face_box
                img_h, img_w = full_frame.shape[:2]
                vest_w = bx2 - bx1
                vest_h = by2 - by1
                if by1 > img_h * 0.12 and vest_w > img_w * 0.04 and vest_h > img_h * 0.05:
                    return True, "Passed (No Face Box, Spatial Fallback)"
                return False, f"No face box detected and invalid vest dimensions w={vest_w}, h={vest_h}"

            fx1, fy1, fx2, fy2 = target_face_box
            face_cx = (fx1 + fx2) / 2
            vest_cx = (bx1 + bx2) / 2
            face_w = fx2 - fx1
            face_h = fy2 - fy1

            # Mid-distance handoff: face is valid enough to create a face_box, but
            # vest still needs zoom fallback. Keep horizontal binding, relax vertical scale.
            zoom_fallback = enable_zoom and source.startswith("fallback")
            horizontal_limit = face_w * (3.5 if zoom_fallback else 3.0)
            vertical_limit = face_h * (3.2 if zoom_fallback else 1.8)
            min_height_ratio = 0.35 if zoom_fallback else 0.5

            horizontal_aligned = abs(face_cx - vest_cx) < horizontal_limit
            placed_below = by2 > fy2
            not_too_far_down = by1 < (fy2 + vertical_limit)

            if not (horizontal_aligned and placed_below and not_too_far_down):
                return False, f"Reject: src={source}, horiz={horizontal_aligned}, below={placed_below}, not_far={not_too_far_down} | vy1,vy2={by1},{by2} fy2={fy2} | face_h={face_h}, v_limit={vertical_limit:.0f}"

            # Height check: a folded/handheld vest has a very small bbox height.
            vest_bbox_h = by2 - by1
            if vest_bbox_h < face_h * min_height_ratio:
                return False, f"Reject(height): src={source}, vest_h={vest_bbox_h} < min={face_h*min_height_ratio:.0f} (face_h={face_h})"

            return True, ""

        # --- Stage 1: Helmet Detection ---
        helmet_found = False

        # 1.1 Precision Crop (full head_crop + face_coverage filter)
        mesh_results = self.mp_handler.detect_mesh(full_frame)
        if mesh_results:
            head_crop, cx, cy = self._crop_head_mesh(
                full_frame, mesh_results[0].landmark)
            if head_crop is not None and head_crop.size > 0:
                details["helmet"]["crop_img"] = head_crop
                h_results = self.system.model_clothes(
                    source=head_crop, iou=0.45, conf=0.15, verbose=False)[0]
                for det in h_results.boxes:
                    if int(det.cls) == 2:
                        det_conf = float(det.conf[0])
                        rx1, ry1, rx2, ry2 = det.xyxy[0].cpu(
                        ).numpy().astype(int)
                        gx1, gy1, gx2, gy2 = rx1 + cx, ry1 + cy, rx2 + cx, ry2 + cy

                        valid, reason = is_helmet_valid(
                            gx1, gy1, gx2, gy2, det_conf=det_conf)
                        details["helmet"]["crop_boxes"].append(
                            [rx1, ry1, rx2, ry2])
                        details["helmet"]["conf"] = det_conf

                        if valid:
                            details["helmet"]["detected"] = True
                            detections.append((2, [gx1, gy1, gx2, gy2]))
                            helmet_found = True
                            # print(
                            #     f"[DEBUG-HELMET-CROP] PASS conf={det_conf:.3f}")
                            break
                        # else:
                        #     print(
                        #         f"[DEBUG-HELMET-CROP] {reason} conf={det_conf:.3f}")

        # 1.2 Fallback YOLO + Spatial Overlap
        if not helmet_found:
            fallback_results, fallback_offset, fallback_y_offset = get_fallback_results(
                "full")
            for det in fallback_results.boxes:
                if int(det.cls) == 2:
                    det_conf = float(det.conf[0])
                    if det_conf < 0.20:
                        continue
                    rx1, ry1, rx2, ry2 = det.xyxy[0].cpu().numpy().astype(int)
                    gx1 = rx1 + fallback_offset
                    gy1 = ry1 + fallback_y_offset
                    gx2 = rx2 + fallback_offset
                    gy2 = ry2 + fallback_y_offset

                    valid, reason = is_helmet_valid(
                        gx1, gy1, gx2, gy2, det_conf=det_conf)

                    if valid:
                        details["helmet"]["detected"] = True
                        details["helmet"]["fallback_box"] = [
                            gx1, gy1, gx2, gy2]
                        details["helmet"]["conf"] = det_conf
                        detections.append((2, [gx1, gy1, gx2, gy2]))
                        print(
                            f"[DEBUG-HELMET-FALLBACK] PASS conf={det_conf:.3f}")
                        break
                    else:
                        print(
                            f"[DEBUG-HELMET-FALLBACK] {reason} conf={det_conf:.3f}")

        vest_found = False

        # 2.1 Precision Crop
        rgb_frame = cv2.cvtColor(full_frame, cv2.COLOR_BGR2RGB)
        pose_res = self.mp_handler.detect_pose(rgb_frame)
        if pose_res.pose_landmarks:
            lm = pose_res.pose_landmarks.landmark
            h, w = full_frame.shape[:2]
            nose_x = int(lm[0].x * w)

            # Verify the pose is for the target person (nose aligns with face box)
            pose_is_target = True
            if target_face_box is not None:
                fx1, fy1, fx2, fy2 = target_face_box
                pose_is_target = fx1 <= nose_x <= fx2

            if pose_is_target:
                self._last_pose_lm = lm  # Store for shoulder check in is_vest_valid
                body_crop, cx, cy = self._crop_body_pose(
                    full_frame, pose_res.pose_landmarks)
            else:
                print(
                    f"[DEBUG-VEST-POSE] Skipping pose: nose_x={nose_x} outside face_box=[{fx1},{fx2}]")
                self._last_pose_lm = None
                body_crop = None

            if body_crop is not None and body_crop.size > 0:
                details["vest"]["crop_img"] = body_crop
                v_results = self.system.model_clothes(
                    source=body_crop, iou=0.45, conf=0.05, verbose=False)[0]
                for det in v_results.boxes:
                    if int(det.cls) == 0:
                        rx1, ry1, rx2, ry2 = det.xyxy[0].cpu(
                        ).numpy().astype(int)
                        gx1, gy1, gx2, gy2 = rx1 + cx, ry1 + cy, rx2 + cx, ry2 + cy

                        valid, reason = is_vest_valid(
                            gx1, gy1, gx2, gy2, source="crop")
                        details["vest"]["crop_boxes"].append(
                            [rx1, ry1, rx2, ry2])

                        if valid:
                            details["vest"]["detected"] = True
                            detections.append((0, [gx1, gy1, gx2, gy2]))
                            vest_found = True
                            break
                        # else:
                        #     print(f"[DEBUG-VEST-CROP] {reason}")

        # 2.2 Fallback YOLO + Spatial Overlap
        if not vest_found:
            if enable_zoom:
                fallback_regions = [
                    "body_dynamic_high",
                    "body_dynamic",
                    "body_dynamic_low",
                    "body_fixed_high",
                    "body_fixed",
                    "body_fixed_low",
                    "full",
                ]
                if target_face_box is None:
                    fallback_regions = [
                        "body_fixed_high",
                        "body_fixed",
                        "body_fixed_low",
                        "full",
                    ]
            else:
                fallback_regions = ["body"]

            best_vest = None
            for fallback_region in fallback_regions:
                fallback_results, fallback_offset, fallback_y_offset = get_fallback_results(
                    fallback_region)
                for det in fallback_results.boxes:
                    if int(det.cls) == 0:
                        det_conf = float(det.conf[0])
                        if det_conf < 0.08:
                            continue
                        rx1, ry1, rx2, ry2 = det.xyxy[0].cpu().numpy().astype(int)
                        gx1 = rx1 + fallback_offset
                        gy1 = ry1 + fallback_y_offset
                        gx2 = rx2 + fallback_offset
                        gy2 = ry2 + fallback_y_offset

                        valid, reason = is_vest_valid(
                            gx1, gy1, gx2, gy2, source=f"fallback:{fallback_region}")

                        if valid:
                            if best_vest is None or det_conf > best_vest["conf"]:
                                best_vest = {
                                    "box": [gx1, gy1, gx2, gy2],
                                    "conf": det_conf,
                                    "region": fallback_region,
                                }
                        else:
                            print(
                                f"[DEBUG-VEST-FALLBACK] region={fallback_region} {reason} conf={det_conf:.3f}")
            if best_vest is not None:
                details["vest"]["detected"] = True
                details["vest"]["fallback_box"] = best_vest["box"]
                details["vest"]["fallback_region"] = best_vest["region"]
                details["vest"]["conf"] = best_vest["conf"]
                detections.append((0, best_vest["box"]))
                vest_found = True
                print(
                    f"[DEBUG-VEST-FALLBACK] BEST region={best_vest['region']} conf={best_vest['conf']:.3f}")

        # Update State
        if details["helmet"]["detected"]:
            if state_buffer is not None:
                state_buffer[2] = True
            else:
                self.system.state.clothes[2] = True
            self.clothe_time[2] = time.time()

        if details["vest"]["detected"]:
            if state_buffer is not None:
                state_buffer[0] = True
            else:
                self.system.state.clothes[0] = True
            self.clothe_time[0] = time.time()

        return detections, details

    def _match_clothes_to_face_horizontal(self, face_box, clothes_detections):
        """
        [2026-02-06 Feature] Face-Centric Clothes Verification (Horizontal Only)
        驗證偵測到的裝備是否在人臉的水平範圍內 (排除路人)。
        放棄垂直檢查以避免誤判 (如低頭、高帽)，改採寬鬆的水平鄰近檢查。
        """
        has_vest = False
        has_helmet = False

        fx1, fy1, fx2, fy2 = face_box
        face_cx = (fx1 + fx2) / 2
        face_w = fx2 - fx1

        # 允許偏差範圍：臉寬的 1.5 倍 (左右各 0.75)
        # 這足以涵蓋身體寬度，但能排除明顯在旁邊的路人
        threshold = face_w * 1.5

        for cls, box in clothes_detections:
            bx1, by1, bx2, by2 = box
            box_cx = (bx1 + bx2) / 2

            if abs(box_cx - face_cx) < threshold:
                if cls == 2:
                    has_helmet = True
                elif cls == 0:
                    has_vest = True

        return has_vest, has_helmet

    def apply_mask(self, frame):
        """
        對輸入圖像應用水平遮罩（只保留中間區域）。
        [2026-02-06 Fix] 改用 35% 比例以對齊 UI 視覺遮罩，排除路人干擾。

        Parameters:
        frame (np.ndarray): 原始 BGR 圖像

        Returns:
        masked_frame (np.ndarray): 遮罩後的圖像區域
        X_offset (int): 遮罩區域的水平偏移量
        """
        # 遮罩處理，保留畫面中間的區域進行臉部偵測
        height, width, _ = frame.shape
        mask = np.zeros_like(frame)

        # [Fix] 使用 35% 比例 (左右各 17.5%)
        ratio = 0.35
        if CONFIG[CAMERA[self.frame_num]]["close"]:
            # 近距離模式可能需要寬一點? 先維持 50% 以防萬一，或者也設為 35%
            # 根據之前設定 (8 -> 75%)，這裡保守設為 0.5 (50%)
            ratio = 0.5

        center = width // 2
        half_w = int(width * ratio / 2)
        x1 = max(0, center - half_w)
        x2 = min(width, center + half_w)

        # 產生白色矩形遮罩
        cv2.rectangle(
            mask,
            (x1, 0),
            (x2, height),
            (255, 255, 255), -1
        )

        # 套用遮罩後回傳遮罩區域與偏移量
        masked_frame = cv2.bitwise_and(frame, mask)
        # 裁切出有效區域 (減少 YOLO 運算量)
        masked_frame = masked_frame[0:, x1:x2]

        return masked_frame, x1

    def equalize(self, img):
        """
        對輸入 BGR 圖像進行每通道的直方圖均衡化（增強對比）。

        Parameters:
        img (np.ndarray): 原始圖像

        Returns:
        equ_image (np.ndarray): 均衡化後的圖像
        """
        # 對 BGR 圖像做每個通道的直方圖均衡化
        b, g, r = cv2.split(img)
        b_eq = cv2.equalizeHist(b)
        g_eq = cv2.equalizeHist(g)
        r_eq = cv2.equalizeHist(r)
        equ_image = cv2.merge((b_eq, g_eq, r_eq))
        return equ_image

    def terminate(self):
        # 外部終止此執行緒
        self.stop_threads = True


class Comparison:
    """
    負責臉部向量比對與身份預測：
    - 最終決策者，統一控制辨識與顯示狀態。
    - 採用單次辨識成功即觸發的機制。
    - 引入顯示狀態保持機制，解決畫面閃爍問題。
    """

    def __init__(self, frame_num, system):
        self.system = system
        self.frame_num = frame_num
        self.stop_threads = False

        # 用於控制辨識成功後，人員名稱在畫面上停留的時間
        self.display_state = {'person_id': 'None', 'last_update': 0}
        self.last_recognition_time = 0

        self.last_api_trigger_time = {}  # 記錄每個人員上次觸發API/語音的時間，用於防止短時間重複播報

        self.DISPLAY_STATE_HOLD_SECONDS = 2  # 辨識成功後，名稱顯示的持續時間
        self.CONFIDENCE_THRESHOLD = 0.7      # 可靠辨識的信賴度門檻 (員工)
        self.VISITOR_CONF_THRESHOLD = 0.5    # 訪客辨識的信賴度門檻 (低於此值為訪客)

        # --- 新增: 潛在辨識失敗分析與統計 ---
        self.width_stats = defaultdict(int)  # 統計人臉寬度分佈 (區間:次數)
        self.last_stats_log_time = 0         # 上次輸出統計表的時間
        # 潛在失敗判定門檻 (min_face * ratio)
        self.potential_miss_ratio = POTENTIAL_MISS_RATIO
        self.last_potential_miss_log_time = 0  # 上次記錄潛在失敗的時間 (限流用)
        self.hint_clear_time = 0             # 提示文字清除時間
        self.last_hint_speak_time = 0        # 上次播報提示語音的時間
        # ---------------------------------

        self.TIMEZONE = pytz.timezone('Asia/Taipei')

        threading.Thread(target=self.face_comparison, daemon=True).start()

    def _save_potential_miss_image(self, frame, width, threshold, camera_name, reason="Unknown"):
        """
        儲存潛在辨識失敗的截圖 (寬度介於意圖區間的人臉)。
        [2026-01-30] Added reason to filename.
        """
        try:
            today_str = datetime.now().strftime('%Y_%m_%d')
            time_str = datetime.now().strftime('%H;%M;%S')

            # 決定位置標記
            cam_tag = "Out" if "Out" in camera_name or "出口" in camera_name else "In"
            if "Cam" in camera_name:  # Fallback for "Cam 0", "Cam 1"
                cam_tag = "Out" if "1" in camera_name else "In"

            # 建立目錄 img_log/potential_miss/YYYY_MM_DD
            save_dir = os.path.join(
                os.getcwd(), "img_log", "potential_miss", today_str)
            os.makedirs(save_dir, exist_ok=True)

            # Sanitize reason string for filename
            safe_reason = reason.replace(" ", "_").replace("/", "-").replace(
                ":", "").replace("(", "").replace(")", "").replace("<", "lt").replace(">", "gt")
            # Limit length to avoid OS limits
            if len(safe_reason) > 50:
                safe_reason = safe_reason[:50]

            # 檔名格式: HH;MM;SS_In_W{width}_Fail_{reason}.jpg
            filename = f"{time_str}_{cam_tag}_W{width}_Fail_{safe_reason}.jpg"
            filepath = os.path.join(save_dir, filename)

            cv2.imwrite(filepath, frame)
            return filepath
        except Exception as e:
            LOGGER.error(f"儲存潛在失敗截圖時發生錯誤: {e}")
            return None

    def _save_potential_miss_json(self, image_path, metrics, msg):
        """
        [2026-01-30 Feature] 為潛在失敗截圖產生搭配的 JSON 檔。
        """
        try:
            json_path = os.path.splitext(image_path)[0] + ".json"

            data = {
                "timestamp": datetime.now(self.TIMEZONE).isoformat(),
                "reason": msg,
                "metrics": metrics
            }

            # [2026-03-10 Fix] Add default converter for numpy types to prevent truncation
            def _default_converter(o):
                if isinstance(o, (np.integer,)):
                    return int(o)
                if isinstance(o, (np.floating,)):
                    return float(o)
                if isinstance(o, np.ndarray):
                    return o.tolist()
                return str(o)

            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False,
                          indent=2, default=_default_converter)

        except Exception as e:
            LOGGER.error(f"儲存潛在失敗 JSON 時發生錯誤: {e}")

    def check_face_quality(self, box, points, frame_w, frame_h, gaze_status):
        """
        評估人臉品質並計算懲罰係數。

        Returns:
        quality_score (float): 1.0 代表完美，0.0 代表未達標
        msg (str): 詳細的評估訊息
        """
        # ---------------------------------------------------------
        # 1. 畫面置中檢查 (Center Alignment) - UI 需求
        # ---------------------------------------------------------
        face_center_x = (box[0] + box[2]) / 2
        frame_center_x = frame_w / 2
        offset = abs(face_center_x - frame_center_x)
        limit_offset = frame_w * 0.15  # 允許偏離 15%
        margin = 5

        # [2026-01-30 Fix] Initialize current_ear to prevent UnboundLocalError if face_w <= 100
        current_ear = 1.0

        metrics = {
            'center_offset_px': float(offset),
            'center_limit_px': float(limit_offset),
            'face_width_px': float(box[2] - box[0]),
            'visibility_margin': float(margin),
            'gaze_passed': False,
            'gaze_msg': 'Init',
            'ear': 1.0,
            'v_ratio': 0.0,
            'roll_angle': 0.0,  # Not strictly calculated here, but could be added if needed
            'pitch_check': 'Pass',
            'yaw_check': 'Pass'
        }

        if offset > limit_offset:
            return 0.0, f"未置中 (偏離 {offset:.1f}px > 容許 {limit_offset:.1f}px)", metrics

        # ---------------------------------------------------------
        # 2. 特徵點完整性檢查 (Visibility) - 完整性需求
        # ---------------------------------------------------------
        for i, p in enumerate(points):
            if p[0] < margin or p[0] > frame_w - margin or \
               p[1] < margin or p[1] > frame_h - margin:
                return 0.0, f"特徵點被切除/遮擋 (點{i}座標 {p} 超出邊界)", metrics

        # ---------------------------------------------------------
        # 2.5 夕陽/強光檢查 (Sunset/Overexposure) - [2026-02-07 Feature]
        # ---------------------------------------------------------
        # Local import to avoid circular dependency
        from init.function import is_sunset_condition

        # 由於此檢查需要 crop ROI，為了效能，只在人臉足夠大時執行
        face_w = max(10, box[2] - box[0])
        if face_w > 100:
            frame_to_use = self.system.state.frame_mtcnn[self.frame_num]
            if frame_to_use is not None:
                if is_sunset_condition(frame_to_use, box, points):
                    return 0.0, "光線直射 (Sunset Mode)", metrics

        # ---------------------------------------------------------
        # 3. 3D 姿態與視線檢查 (Gaze & Pose Check) - 核心邏輯
        # ---------------------------------------------------------
        # [2026-01-11 Fix] 直接使用傳入的同步狀態，解決影像與判定錯位問題
        if face_w > 100:
            if gaze_status:
                # [2026-01-26 Fix] 兼容擴充後的 gaze_status (4 elements: pass, msg, pose, ear)
                is_looking = gaze_status[0]
                gaze_msg = gaze_status[1]

                # 預先提取 EAR，優先使用原子打包數據
                current_ear = 1.0
                pose_tuple = (0, 0, 0)
                if len(gaze_status) >= 4:
                    current_ear = gaze_status[3]
                    pose_tuple = gaze_status[2]
                # Fallback for backward compatibility
                elif hasattr(self.system.state, 'face_ear'):
                    current_ear = self.system.state.face_ear.get(
                        self.frame_num, 1.0)

                metrics['gaze_passed'] = is_looking
                metrics['gaze_msg'] = gaze_msg
                metrics['ear'] = float(current_ear)

                # [2026-02-09 V5 Logic] Extract Pose Data for Dynamic Thresholding
                pitch, yaw, roll = pose_tuple
                metrics['pitch'] = float(pitch)
                metrics['yaw'] = float(yaw)
                metrics['roll_angle'] = float(roll)

                # Detect Bad Pose (Compound Deviation)
                # 1. Yaw > 15 AND Pitch > 10 (Both distinct deviations) -> Dangerous
                # 2. Roll > 15 (Tilt) -> Dangerous
                # [2026-04-15] 修正邏輯：只要任一角度過大就是不良姿態 (or)
                is_bad_pose = (abs(yaw) > 25 or abs(
                    pitch) > 20) or abs(roll) > 20
                metrics['is_bad_pose'] = is_bad_pose

                if not is_looking:
                    return 0.0, f"{gaze_msg}", metrics

                # [2026-05-04 Fix v2] 極端姿態硬攔截
                # 根據實測校正：yaw 25~28° 的照片仍可正常辨識 (6 張實證)，
                # 因此硬攔截閾值從 25° 提升至 30°，避免誤殺。
                # is_bad_pose (yaw>25) 仍保留用於辨識階段提高 confidence 門檻。
                is_extreme_pose = abs(yaw) > 30 or abs(
                    pitch) > 25 or abs(roll) > 25
                if is_extreme_pose:
                    return 0.0, f"姿態不良 (Yaw:{yaw:.1f}° Pitch:{pitch:.1f}° Roll:{roll:.1f}°)", metrics
            else:
                # [2026-01-11 Fix] 若無 Gaze 狀態 (可能因 Race Condition 被清空)，嚴格禁止放行
                return 0.0, "Gaze Status Missing", metrics

        # ---------------------------------------------------------
        # 3.1 幾何比例檢查 (Geometry Check) - 低頭防禦
        # ---------------------------------------------------------
        # [2026-01-22 Fix] 防止極端低頭導致特徵崩壞誤判 (V-Ratio < 0.55)
        # Points: 0:LE, 1:RE, 2:Nose, 3:LM, 4:RM
        eye_y = (points[0][1] + points[1][1]) / 2
        nose_y = points[2][1]
        mouth_y = (points[3][1] + points[4][1]) / 2

        eye_nose_dist = nose_y - eye_y
        nose_mouth_dist = mouth_y - nose_y

        if eye_nose_dist > 0:
            v_ratio = nose_mouth_dist / eye_nose_dist
            metrics['v_ratio'] = float(v_ratio)
            # 正常值: 0.8 ~ 1.2, 低頭測試照: 0.18 ~ 0.40

            # 1. 極端低頭過濾 (絕對死線)
            # 殺死 9 張極端低頭測試照 (V < 0.35)
            if v_ratio < 0.35:
                metrics['pitch_check'] = 'Fail (Extreme Low)'
                return 0.0, f"低頭 (V-Ratio: {v_ratio:.2f} < 0.35)", metrics

            # 2. 低頭+遮眼 Combo 過濾 (0.35 <= V < 0.42)
            # [2026-01-22] 針對灰色地帶進行補刀
            # - 蔡準庭帽子照 (V=0.403, EAR=0.213) -> 符合雙重條件 -> KILL
            # - 楊昌裕 (V=0.47, EAR=0.10) -> V正常 -> PASS
            # - 林文明 (V=0.40, EAR=0.26) -> EAR正常 -> PASS
            if v_ratio < 0.42:
                # [2026-01-26 Refactor] Use pre-extracted EAR
                if current_ear < 0.22:
                    metrics['pitch_check'] = 'Fail (Combo Low+Cover)'
                    return 0.0, f"低頭/遮眼 (V {v_ratio:.2f}<0.42 & EAR {current_ear:.2f}<0.22)", metrics

            # [2026-05-04 Fix v2] 抬頭/眼睛超出畫面上限過濾
            # 根因：當人抬頭或仰頭使眼睛離開畫面時，v_ratio 會異常升高。
            # 正常 v_ratio 範圍 0.8~1.2。
            # [校正] 門檻從 1.5 提升至 1.6 (v=1.52 實測為正常人臉)。
            if v_ratio > 1.6:
                metrics['pitch_check'] = 'Fail (Eyes Out of Frame)'
                return 0.0, f"抬頭/眼睛超出畫面 (V-Ratio: {v_ratio:.2f} > 1.6)", metrics

        # ---------------------------------------------------------
        # 3.2 閉眼檢查 (Eye Closure Check) - [2026-01-26 Fix]
        # ---------------------------------------------------------
        # 根據測試，閉眼誤判照 EAR=0.0694，小眼(楊昌裕) EAR=0.0837。
        # 設定底層安全門檻 0.05 (極端閉眼)。
        # 中間地帶 (0.05~0.10) 交由 mp_handler 的 Combo Check 處理。
        # [2026-05-04 Fix v4] 兩層 EAR 過濾
        # 層 1：EAR < 0.10 → 直接攔截（極端閉眼）
        # 層 2：0.10 ≤ EAR < 0.15 且 v_ratio < 0.60 → 閉眼+低頭 combo
        #   根因：13;27;24 (EAR=0.139, v=0.44) 需被攔截
        #   避免誤殺：11;20;43 陳志杰 (EAR=0.12, v_ratio 正常~0.8+) 不受影響
        if current_ear < 0.10:
            return 0.0, f"眼睛閉合 (EAR: {current_ear:.4f} < 0.10)", metrics
        if current_ear < 0.15 and v_ratio < 0.60:
            return 0.0, f"眼睛閉合+低頭 (EAR: {current_ear:.4f} < 0.15 & V-Ratio: {v_ratio:.2f} < 0.60)", metrics

        # ---------------------------------------------------------
        # 4. 臉部區域清晰度檢查 (ROI Blur Detection)
        # ---------------------------------------------------------
        # [2026-01-11] 實驗數據：誤判糊臉=7.1, 正常辨識平均=20.6
        # [2026-01-18 Disabled by User Request]
        # try:
        #     x1, y1, x2, y2 = map(int, box)
        #     x1, y1 = max(0, x1), max(0, y1)
        #     x2, y2 = min(frame_w, x2), min(frame_h, y2)
        #
        #     face_roi = self.system.state.frame_mtcnn[self.frame_num][y1:y2, x1:x2]
        #     if face_roi.size > 0:
        #         gray_roi = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
        #         blur_score = cv2.Laplacian(gray_roi, cv2.CV_64F).var()
        #
        #         if blur_score < 13.0:
        #             return 0.0, f"影像模糊 (ROI Score:{blur_score:.1f} < 13.0)"
        # except Exception as e:
        #     LOGGER.error(f"清晰度檢查失敗: {e}")

        # ---------------------------------------------------------
        # 4. [2026-04-14 Fix] 計算連續姿態降權 (Continuous Score Penalty)
        # ---------------------------------------------------------
        quality_score = 1.0
        if 'yaw' in metrics and 'pitch' in metrics:
            yaw = metrics['yaw']
            pitch = metrics['pitch']
            roll = metrics.get('roll_angle', 0.0)

            # 從安全角度外開始扣分 (每度0.005)
            yaw_penalty = max(0, abs(yaw) - 20) * 0.005
            pitch_penalty = max(0, abs(pitch) - 15) * 0.005
            roll_penalty = max(0, abs(roll) - 5) * 0.005

            total_penalty = min(0.20, yaw_penalty +
                                pitch_penalty + roll_penalty)
            quality_score -= total_penalty

        return quality_score, "Pass", metrics

    def _update_display_state(self, person_id):
        """更新當前顯示的人員ID和時間"""
        self.display_state['person_id'] = person_id
        self.display_state['last_update'] = time.time()
        self.system.state.same_class[self.frame_num] = person_id
        # [2026-03-10 Fix] Do NOT reset clothes[] here — it causes a race condition
        # where Detector sets clothes=True but this timer-based reset clobbers it
        # before Comparison/main_camera can read it, resulting in perpetual "請正確著裝".
        # clothes[] is solely managed by Detector. Only control UI display via suppression flag.
        if person_id == 'None':
            self.system.state.clothes_display_suppressed[self.frame_num] = True
        else:
            self.system.state.clothes_display_suppressed[self.frame_num] = False

    def _clear_recognition_state(self):
        if self.display_state['person_id'] != 'None' or self.system.state.same_class[self.frame_num] != "None":
            self._update_display_state('None')
        self.system.state.same_people[self.frame_num] = 0.0
        self.system.state.same_zscore[self.frame_num] = 0.0
        self.system.state.same_width[self.frame_num] = 0
        if self.system.state.success_snapshot:
            self.system.state.success_snapshot[self.frame_num] = None
        if self.system.state.success_metadata:
            self.system.state.success_metadata[self.frame_num] = None

    def _clothes_gate_required(self):
        return self.frame_num == 0 and (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False))

    def _clothes_gate_is_fresh(self):
        return bool(
            getattr(self.system.state, "clothes_gate_pass", False) and
            time.time() - getattr(self.system.state, "clothes_gate_time", 0.0) <= 0.5
        )

    def face_comparison(self):
        """
        執行臉部比對的核心迴圈。
        - 提取人臉特徵。
        - 與資料庫比對並計算信賴度。
        - 如果信賴度超過門檻，則觸發成功事件。
        - 管理UI顯示狀態，並移除信賴度分數的顯示。
        - 引入 Z-Score 離群值分析，以提高在高相似度誤判情況下的準確性。
        - 新增辨識品質評級 (可靠/模糊/低信賴度) 至日誌。
        """
        last_warmup_time = 0
        dummy_input = tensor_test_img

        while not self.stop_threads:
            # 動態調整頻率
            time.sleep(self.system.state.comparison_interval)
            now = time.time()

            # [2026-02-11 Fix] Ensure display state is cleared even if no face is detected (frame_data is None)
            # This solves the issue where the sidebar avatar persists until the next detection event.
            if self.display_state['person_id'] != 'None' and \
               now - self.display_state['last_update'] > self.DISPLAY_STATE_HOLD_SECONDS:
                self._update_display_state('None')

            # [2026-01-11 Fix] 原子讀取打包數據
            # 確保 影像(frame), 狀態(gaze), 位置(box) 來自同一時間點 (Snapshot)
            data_package = self.system.state.frame_data[self.frame_num]
            if data_package is None:
                if self._clothes_gate_required() and not self._clothes_gate_is_fresh():
                    self._clear_recognition_state()
                continue  # 如果沒資料 (例如 Detector 阻斷中)，就繼續睡，別動 Hint！

            # 鐵則：服裝辨識開啟後，服裝未通過前不進入臉辨。
            if self._clothes_gate_required() and not self._clothes_gate_is_fresh():
                self._clear_recognition_state()
                continue

            # [2026-02-10 Fix] Move hint clearing logic AFTER data check
            # This prevents Race Condition where Comparison clears the hint while Detector is blocking.
            # 清除過期的 UI 提示
            if now > self.hint_clear_time:
                self.system.state.hint_text[self.frame_num] = ""

            _frame, _gaze_status, _box, _points, _legacy_face_zoom_flag = data_package

            # 使用解包出來的 frame，而不是去讀可能已經被覆蓋的 system.state.frame_mtcnn
            # 為了相容其他可能讀取這欄位的地方(如UI?)
            self.system.state.frame_mtcnn[self.frame_num] = _frame

            if _box is None or _points is None or _frame is None:
                continue

            # 取得畫面尺寸 (用於置中與邊界檢查)
            frame_curr = _frame
            frame_h, frame_w, _ = frame_curr.shape

            camera_name = CAM_NAME_MAP.get(
                self.frame_num, f"Cam {self.frame_num}")

            # 檢查臉部大小是否足夠
            face_width = _box[2] - _box[0]
            min_face_threshold = self.system.state.min_face[self.frame_num]

            # --- 統計: 記錄人臉寬度分佈 (每 10px 為一個區間) ---
            width_bin = (face_width // 10) * 10
            self.width_stats[f"{width_bin}-{width_bin+9}"] += 1

            # 定期輸出統計摘要 (每分鐘一次，方便即時驗證)
            if now - self.last_stats_log_time > 60:
                stats_str = ", ".join(
                    [f"{k}: {v}" for k, v in sorted(self.width_stats.items())])
                LOGGER.info(f"[統計] [{camera_name}] 過去一分鐘人臉寬度分佈: {stats_str}")
                self.width_stats.clear()  # 重置統計
                self.last_stats_log_time = now
            # -----------------------------------------------

            # [2026-01-08 夜間模式全域過濾]
            # 若為夜間 (18:00-06:00)，在進行任何品質或大小檢查前，先驗證「像不像人」
            current_hour = datetime.now(self.TIMEZONE).hour
            is_night_mode = (current_hour >= 18 or current_hour < 6)

            # 提取特徵向量 (為了夜間檢查或後續辨識)
            current_face_vec = None
            try:
                frame_to_use = _frame
                box_to_use = list(_box)
                points_to_use = _points.copy()
                frame_image = Image.fromarray(
                    cv2.cvtColor(frame_to_use, cv2.COLOR_BGR2RGB))
                img_cropped = crop_face_without_forehead(
                    frame_image, box_to_use, points_to_use)
                face_embedding_list = self.system.resnet(
                    img_cropped.unsqueeze(0))

                if face_embedding_list is not None and len(face_embedding_list) > 0:
                    current_face_vec = face_embedding_list[0].detach().numpy()
            except Exception as e:
                LOGGER.error(f"[{camera_name}] 特徵提取失敗: {e}")
                pass

            # 夜間強力過濾
            if is_night_mode and current_face_vec is not None:
                if self.system.state.ann_index and self.system.state.ann_index.index is not None and self.system.state.ann_index.index.ntotal > 0:
                    dists, _ = self.system.state.ann_index.search(
                        current_face_vec, k=1)
                    if dists[0] < 0.4:
                        continue

            # [2026-01-11] 判斷是否處於 "辨識成功後的顯示保持期"
            is_staff_displaying = (
                self.display_state['person_id'] != 'None' and
                self.display_state['person_id'] != '__VISITOR__' and
                (now - self.display_state['last_update']
                 < self.DISPLAY_STATE_HOLD_SECONDS)
            )

            if face_width < min_face_threshold:
                if self.display_state['person_id'] != 'None' and not is_staff_displaying:
                    self._update_display_state('None')

                potential_threshold = min_face_threshold * self.potential_miss_ratio

                if face_width >= potential_threshold:
                    if now - self.last_potential_miss_log_time > 3:
                        snapshot = _frame
                        saved_path = "無影像"
                        if snapshot is not None:
                            # [2026-01-30] Pass reason="SmallFace"
                            saved_path = self._save_potential_miss_image(
                                snapshot, face_width, min_face_threshold, camera_name, reason="SmallFace")

                        LOGGER.info(
                            f"[{camera_name}][潛在失敗] 偵測到人臉但過小 (寬度: {face_width}) - 已存檔: {saved_path}")
                        self.last_potential_miss_log_time = now

                        if not is_staff_displaying:
                            self.system.state.hint_text[self.frame_num] = "請靠近鏡頭"
                            self.hint_clear_time = now + 2.0
                            self.system.speaker.say(
                                "請靠近鏡頭", "hint_closer", priority=2)

                continue

            if face_width >= CONFIG["max_face"]:
                if not is_staff_displaying:
                    self.system.state.hint_text[self.frame_num] = "請稍微後退"
                    self.system.speaker.say(
                        "請稍微後退", "hint_move_back", priority=2)
                    self.hint_clear_time = time.time() + 1.5
                    if self.display_state['person_id'] != 'None':
                        self._update_display_state('None')
                continue

            # 檢查人臉品質 (同步版)
            quality_score, quality_msg, quality_metrics = self.check_face_quality(
                _box, _points, frame_w, frame_h, _gaze_status)

            if quality_score == 0.0:
                if is_staff_displaying:
                    continue  # 免死金牌

                LOGGER.info(f"[{camera_name}][品質過濾] {quality_msg}")

                # [2026-01-30 Feature] 潛在失敗數據收集 (大臉但被品質過濾)
                if face_width >= min_face_threshold and now - self.last_potential_miss_log_time > 1.0:
                    try:
                        snapshot = _frame
                        if snapshot is not None:
                            saved_path = self._save_potential_miss_image(
                                snapshot, face_width, min_face_threshold, camera_name, reason=quality_msg)
                            # 產生搭配的 JSON
                            if saved_path:
                                self._save_potential_miss_json(
                                    saved_path, quality_metrics, quality_msg)

                            LOGGER.info(
                                f"[{camera_name}][品質失敗收集] 寬度 {face_width} 但品質未過 - 已存檔")
                            self.last_potential_miss_log_time = now
                    except Exception as e:
                        LOGGER.error(
                            f"Save potential miss (quality) failed: {e}")

                if "低頭" in quality_msg:
                    self.system.state.hint_text[self.frame_num] = "請抬頭"
                    self.system.speaker.say("請抬頭", "hint_look_up", priority=2)
                elif "抬頭" in quality_msg:
                    self.system.state.hint_text[self.frame_num] = "請低頭"
                    self.system.speaker.say(
                        "請低頭", "hint_look_down", priority=2)
                elif "未置中" in quality_msg:
                    self.system.state.hint_text[self.frame_num] = "請站到中間"
                    self.system.speaker.say("請站到中間", "hint_center", priority=2)
                elif "斜視" in quality_msg or "未正視" in quality_msg or "側臉" in quality_msg or "影像模糊" in quality_msg:
                    self.system.state.hint_text[self.frame_num] = "請正視鏡頭"
                    self.system.speaker.say(
                        "請正視鏡頭", "hint_look_straight", priority=2)
                elif "光線直射" in quality_msg:
                    self.system.state.hint_text[self.frame_num] = "光線直射 請遮擋"
                    self.system.speaker.say(
                        "光線直射請遮擋", "hint_sunset", priority=2)
                else:
                    self.system.state.hint_text[self.frame_num] = "請對準鏡頭"
                    self.system.speaker.say(
                        "請對準鏡頭", "hint_occlusion", priority=2)

                self.hint_clear_time = now + 1.0
                continue

            if current_face_vec is None:
                try:
                    comparison_start_time = time.monotonic()
                    frame_to_use = _frame
                    box_to_use = list(_box)
                    points_to_use = _points.copy()
                    frame_image = Image.fromarray(
                        cv2.cvtColor(frame_to_use, cv2.COLOR_BGR2RGB))
                    img_cropped = crop_face_without_forehead(
                        frame_image, box_to_use, points_to_use)
                    face_embedding_list = self.system.resnet(
                        img_cropped.unsqueeze(0))
                    if face_embedding_list is None or len(face_embedding_list) == 0:
                        continue
                    current_face_vec = face_embedding_list[0].detach().numpy()
                except Exception as e:
                    LOGGER.error(f"[ERROR][{camera_name}] 臉部特徵提取失敗: {e}")
                    continue
            else:
                comparison_start_time = time.monotonic()

            try:
                if self.system.state.ann_index is None or self.system.state.ann_index.index is None or self.system.state.ann_index.index.ntotal == 0:
                    predicted_id = "None"
                    confidence = 0.0
                    z_score = 0.0
                    raw_confidence = 0.0
                    part_msg = ""
                else:
                    distances, faiss_person_ids = self.system.state.ann_index.search(
                        current_face_vec, k=self.system.state.ann_index.index.ntotal)
                    if faiss_person_ids is None or len(faiss_person_ids) == 0:
                        predicted_id = "None"
                        confidence = 0.0
                        z_score = 0.0
                        raw_confidence = 0.0
                        part_msg = ""
                    else:
                        top_k_similarities = np.array(distances)

                        # 1. Phase 1: Filter Candidates (Confidence >= 0.7 AND Z >= 1.5)
                        candidates = []

                        # Calculate population stats from All Candidates (Option SMALL Logic)
                        if len(top_k_similarities) > 1:
                            mean_score = np.mean(top_k_similarities)
                            std_dev_score = np.std(top_k_similarities)
                        else:
                            mean_score = 0
                            std_dev_score = 0

                        for i, pid in enumerate(faiss_person_ids):
                            s_raw = distances[i]
                            # [2026-04-15] 高分豁免機制
                            current_qs = quality_score
                            if s_raw >= 0.750 and current_qs < 0.99:
                                current_qs = max(current_qs, 0.99)

                            s_final = s_raw * current_qs

                            z = (s_raw - mean_score) / \
                                std_dev_score if std_dev_score > 0 else 0

                            # [2026-04-15] 四象限 Z-Score 動態門檻矩陣
                            if quality_metrics.get('is_bad_pose', False):
                                required_conf = 0.65 if z >= 2.5 else 0.85
                            else:
                                required_conf = 0.65 if z >= 2.5 else 0.70

                            # Strict Filter: Must pass BOTH thresholds
                            if s_final >= required_conf and z >= Z_SCORE_THRESHOLD:
                                candidates.append({
                                    'id': pid,
                                    'raw': s_raw,
                                    'conf': s_final,
                                    'z': z
                                })

                        # Set Default Winner (Top 1) for fallback/logging
                        best_match_id = faiss_person_ids[0]
                        raw_confidence = distances[0]

                        final_qs = quality_score
                        if raw_confidence >= 0.750 and final_qs < 0.99:
                            final_qs = max(final_qs, 0.99)
                        confidence = raw_confidence * final_qs
                        z_score = (raw_confidence - mean_score) / \
                            std_dev_score if std_dev_score > 0 else 0
                        part_msg = ""
                        is_in_candidates = False

                        # [2026-02-01 Feature] Gap Check for Ambiguity Rejection
                        # 攔截高分誤判 (High Confidence False Positive)
                        gap = 0.0
                        if len(distances) > 1:
                            gap = float(distances[0]) - float(distances[1])

                        # Dynamic Threshold Formula
                        # 如果信心度極高 (>0.80)，容忍較小的 Gap (0.02)
                        # 否則需要較大的 Gap (0.03) 以確保安全
                        gap_threshold = 0.005 if confidence >= 0.75 or z >= 2.5 else 0.015

                        if gap < gap_threshold:
                            LOGGER.info(
                                f"[{camera_name}][Gap過濾] 分數過於接近 (Gap: {gap:.4f} < {gap_threshold}) - 拒絕辨識")

                            # [2026-01-30 Feature] 潛在失敗數據收集 (Gap Fail)
                            if face_width >= min_face_threshold and now - self.last_potential_miss_log_time > 1.0:
                                try:
                                    snapshot = _frame
                                    if snapshot is not None:
                                        reason_str = f"Gap_Fail_{gap:.4f}"
                                        saved_path = self._save_potential_miss_image(
                                            snapshot, face_width, min_face_threshold, camera_name, reason=reason_str)
                                        if saved_path:
                                            self._save_potential_miss_json(
                                                saved_path, quality_metrics, f"Gap Fail: {gap:.4f}")
                                        self.last_potential_miss_log_time = now
                                except:
                                    pass

                            continue

                        # [2026-01-24 Feature] 記錄 Top-5 搜尋結果供除錯重現
                        top5_results = []
                        # Log top 5 only for debugging
                        log_k = min(5, len(faiss_person_ids))
                        for i in range(log_k):
                            pid = faiss_person_ids[i]
                            s_raw = distances[i]
                            z = (s_raw - mean_score) / \
                                std_dev_score if std_dev_score > 0 else 0
                            top5_results.append({
                                "rank": i + 1,
                                "id": pid,
                                "score": float(s_raw),
                                "z_score": float(z)
                            })

                        # 2. Single Stage Decision (Option SMALL)
                        # No T-Zone re-ranking. Just pick the best candidate that passed filters.
                        t_zone_applied = False
                        t_zone_score = None

                        if candidates:
                            # Candidates are populated in order of FAISS result (descending similarity)
                            # So candidates[0] is the best match that passed filters.
                            winner = candidates[0]
                            best_match_id = winner['id']
                            raw_confidence = winner['raw']
                            confidence = winner['conf']
                            z_score = winner['z']
                            is_in_candidates = True

                            # [2026-05-04] Stranger Rejection: Per-Person Enrollment Baseline Check
                            # If the winner's score is below their personal threshold,
                            # the face is more likely a stranger than a registered person.
                            if is_in_candidates:
                                baselines = getattr(
                                    self.system.state.ann_index, 'enrollment_baselines', {})
                                personal_thresh = baselines.get(best_match_id)
                                if personal_thresh is not None and raw_confidence < personal_thresh:
                                    LOGGER.info(
                                        f"[{camera_name}][陌生人拒絕] ID={best_match_id} "
                                        f"score={raw_confidence:.4f} < baseline={personal_thresh:.4f} → 判定為訪客"
                                    )
                                    is_in_candidates = False
                                    best_match_id = '__VISITOR__'
                        predicted_id = best_match_id

                        # [2026-01-24 Feature] 建立完整的 Snapshot Metadata (供離線重現測試)
                        if current_face_vec is not None:
                            meta = {
                                "timestamp": datetime.now(self.TIMEZONE).isoformat(),
                                "predicted_id": best_match_id,
                                "full_score": float(confidence),
                                "z_score": float(z_score),
                                "quality_score": float(quality_score),
                                # [2026-01-30] Add metrics
                                "quality_metrics": quality_metrics,
                                "t_zone_score": None,
                                "top5": top5_results,
                                "embedding": current_face_vec.tolist()
                            }
                            self.system.state.success_metadata[self.frame_num] = meta

            except Exception as e:
                LOGGER.error(f"[ERROR][{camera_name}] 預測失敗: {e}")
                continue

            # 標記每一次辨識事件（無論成功與否）
            log_time = datetime.now(
                self.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')

            staff_name = self.system.state.features_dict.get(
                "id_name", {}).get(predicted_id, "未知")

            # [2026-02-09 V5 Logic] Recalculate dynamic threshold for final decision & logging
            # [2026-05-04 Fix] Use is_extreme_pose (yaw>30°) instead of is_bad_pose (yaw>25°)
            # for the strict 0.85 gate. Rationale: yaw=25-30° faces are valid-quality and
            # should not be penalized with an impossibly high confidence threshold.
            final_required_conf = self.CONFIDENCE_THRESHOLD
            if quality_metrics.get('is_extreme_pose', False):
                final_required_conf = 0.65 if z_score >= 2.5 else 0.85
            else:
                final_required_conf = 0.65 if z_score >= 2.5 else 0.70

            # [2026-01-11 Fix] 補回遺漏的 Log 訊息定義
            quality_rating = "Low Confidence"
            if confidence >= final_required_conf:
                if z_score >= Z_SCORE_THRESHOLD:
                    quality_rating = "Reliable"
                else:
                    quality_rating = "Ambiguous (Low Z)"

            # Log output to file
            log_msg = f"[{camera_name}] ID: {predicted_id} ({staff_name}), Score: {confidence:.2f}/{final_required_conf:.2f} (Raw:{raw_confidence:.2f}), Z: {z_score:.2f}, Q: {quality_score:.2f} [{quality_rating}]{part_msg}"
            LOGGER.info(log_msg)

            if is_in_candidates and predicted_id != "None" and confidence >= final_required_conf and z_score >= Z_SCORE_THRESHOLD:
                if self.system.state.same_class[self.frame_num] != predicted_id:
                    self._update_display_state(predicted_id)

                    # 辨識成功，播放音效與打卡
                    # [2026-01-08 Refactor] 統一使用新的打卡邏輯
                    # 傳入 check_in_out 進行防抖與方向判斷
                    # [2026-01-20 Fix] 傳入 confidence 供日誌記錄
                    check_in_out(self.system, staff_name, predicted_id,
                                 self.frame_num, self.system.n_camera < 2, confidence)
                    self.last_api_trigger_time[predicted_id] = now

                    # [2026-02-09 Fix] Sync state to trigger save_img in main.py
                    self.system.state.same_people[self.frame_num] = float(
                        confidence)
                    self.system.state.same_zscore[self.frame_num] = float(
                        z_score)
                    self.system.state.same_width[self.frame_num] = int(
                        face_width)
                    # [2026-01-24 Fix] Atomic snapshot for saving
                    if self.system.state.success_metadata[self.frame_num]:
                        self.system.state.success_snapshot[self.frame_num] = (
                            frame_curr.copy(), self.system.state.success_metadata[self.frame_num])
                else:
                    self._update_display_state(predicted_id)
                    last_trigger = self.last_api_trigger_time.get(
                        predicted_id, 0)
                    if now - last_trigger > 3.0:
                        self.last_api_trigger_time[predicted_id] = now

                        _is_entry = True
                        if hasattr(self.system, 'cameras'):
                            for _cam in self.system.cameras:
                                if _cam.frame_num == self.frame_num:
                                    _is_entry = _cam._is_entry_active()
                                    break
                        cam_tag = "in" if _is_entry else "out"
                        try:
                            self.system.speaker.say(
                                f"{staff_name}{CONFIG['say'][cam_tag]}", staff_name + "_" + cam_tag, priority=1, token=predicted_id)
                        except Exception:
                            pass

            elif predicted_id != "None" and confidence >= 0.58:
                # [2026-04-14 Fix] 將未能錄取員工但具有一定置信度的辨識標記為訪客
                # 攔截因分數在 0.58~0.70 被系統忽略，但後續突然跳上 0.7 導致誤認員工的情況
                if self.system.state.same_class[self.frame_num] != '訪客':
                    self._update_display_state('訪客')
                # 依據要求，不發出聲音提示
            else:
                # Low Confidence or None
                pass

    def terminate(self):
        self.stop_threads = True
