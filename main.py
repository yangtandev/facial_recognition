from init.ann_index import AnnIndex
from typing import List, Dict, Any
from dataclasses import dataclass
from PyQt5 import QtWidgets
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtCore import QLibraryInfo, QTimer, QSocketNotifier
from init.function import *
from init.model import Detector, Comparison
import init.model  # [2026-02-06 Fix] Import module for config syncing
from init.camera import VideoCapture
import datetime
from init.say import Say_
from py_ssh import ssh
from web_ui.app import run_web_server
from ui import styles
from ui.user_show import MainWindow
from init.log import LOGGER
from PVMS_Library import config
from ultralytics import YOLOv10
from tqdm import tqdm
import requests
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont
from models import mtcnn, inception_resnet_v1
from pathlib import Path
import time
import threading
import queue
import json
import os
import subprocess
import socket
import shutil
from gtts import gTTS
from concurrent.futures import ThreadPoolExecutor  # [2026-01-13 Perf]
import paho.mqtt.client as mqtt
import sys
import termios
import warnings
import signal
warnings.filterwarnings("ignore", category=UserWarning,
                        module="google.protobuf")

main_path = os.path.dirname(__file__)


def check_empty_string_in_dict(data):
    for key, value in data.items():
        if isinstance(value, dict):
            if not check_empty_string_in_dict(value):
                return False
        elif value == "":
            return False
    return True


os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(
    QLibraryInfo.PluginsPath)
font_path = os.path.join(os.path.dirname(
    __file__), "other/NotoSansTC-VariableFont_wght.ttf")
CAMERA = {0: "inCamera", 1: "outCamera"}
HINT_VOICE_TEXTS = {
    "hint_closer": "請靠近鏡頭",
    "hint_move_back": "請稍微後退",
    "hint_look_up": "請抬頭",
    "hint_look_down": "請低頭",
    "hint_center": "請站到中間",
    "hint_look_straight": "請正視鏡頭",
    "hint_face_visible": "請完整露出臉部",
    "hint_sunset": "光線直射請遮擋",
    "hint_backlight": "請遮擋背後光源",
    "hint_occlusion": "請對準鏡頭",
    "hint_unrecognized": "無法識別",
}


def put_chinese_text(img, text, position, font_path, font_size, color, background=True):
    img_pil = Image.fromarray(img)
    draw = ImageDraw.Draw(img_pil)
    font = ImageFont.truetype(font_path, font_size)
    text_bbox = draw.textbbox(position, text, font=font)
    if background:
        draw.rectangle(text_bbox, fill='white')
    draw.text(position, text, font=font, fill=color)
    return np.array(img_pil)


class CameraSystem:
    def __init__(self, ip, frame_num, n, system, config_data):
        self.system = system
        self.camera = VideoCapture(ip, config_data=config_data)
        self.frame_num = frame_num
        self.stop_threads = False
        self.image = cv2.imread(os.path.join(
            os.path.dirname(__file__), "other/mask.png"))
        if CONFIG[CAMERA[frame_num]]["close"]:
            width = self.image.shape[1]
            self.image = self.image[0:, width // 5: 4 * width // 5]
        self.show_frame = self.image.copy() if self.image is not None else np.array([])
        self.speak_time = 0
        self.save_img_time = {}
        self.save_name_last = ""
        self.clothes_de = (CONFIG["Clothes_show"] and self.frame_num == 0)
        self.last_visitor_face_img = None
        self.detect = Detector(frame_num, system)
        self.compar = Comparison(frame_num, system)
        threading.Thread(target=self.main_camera, daemon=True).start()
        self.n_camera = n < 2
        LOGGER.info(
            f"CameraSystem init: frame_num={self.frame_num}, ip={ip}, n={n}, single_cam={self.n_camera}")
        if frame_num == 1 and n < 2:
            return
        self.win = MainWindow(self.updata_screen, frame_num)
        self.img1_size = (self.win.img1.width(), self.win.img1.height())
        self.img2_size = (self.win.img2.width(), self.win.img2.height())
        if n == 1:
            self.win.setWindowTitle(f"進出視窗")
        self.win.closeEvent = self.terminate
        if self.frame_num == 0 and CONFIG["Clothes_show"]:
            self.win.img3.setPixmap(QPixmap(f'{main_path}/other/helmet_R.png'))
            self.win.img4.setPixmap(QPixmap(f'{main_path}/other/vest_R.png'))
            self.win.img3.setStyleSheet(
                "QLabel{background-color: rgba(255,0,0,255);}")
            self.win.img4.setStyleSheet(
                "QLabel{background-color: rgba(255,0,0,255);}")
            self.win.img3.setScaledContents(True)
            self.win.img4.setScaledContents(True)

    def _is_entry_active(self):
        """
        判斷當前鏡頭是否處於「入口」模式。
        - 排程啟用：所有鏡頭依同一排程切換方向。
        - 排程未啟用：雙鏡頭 frame_num == 0 為入口；單鏡頭預設入口。
        """
        schedule_conf = CONFIG.get("Schedule", {})
        if schedule_conf.get("enabled", False):
            try:
                result = is_schedule_entry_active(schedule_conf)
                return True if result is None else result
            except Exception as e:
                LOGGER.error(f"Schedule display logic error: {e}")
                return True

        if not self.n_camera:
            return self.frame_num == 0

        return True

    def main_camera(self):
        frame_count = 0
        while not self.stop_threads:
            original_frame = self.camera.read()
            if original_frame is None or original_frame.size == 0:
                time.sleep(0.01)
                continue

            # 更新 AI 與存檔用的原圖
            self.system.state.frame[self.frame_num] = original_frame
            self.system.state.frame_high_res[self.frame_num] = original_frame

            # [效能優化] 先縮圖，再繪圖 (Process Small, Display Small, Save Big)
            # 原本在 1080p 上做 PIL 中文繪圖太慢，導致延遲。
            # 改為縮小到 960px 寬 (1/2 尺寸, 1/4 像素量)，速度提升 4 倍。
            h, w = original_frame.shape[:2]
            target_w = 960
            scale = target_w / w if w > target_w else 1.0

            if scale < 1.0:
                target_h = int(h * scale)
                now_frame = cv2.resize(original_frame, (target_w, target_h))
            else:
                now_frame = original_frame.copy()

            font_size = int(60 * scale)  # 字體也跟著縮放
            if font_size < 20:
                font_size = 20

            if self.system.state.max_box[self.frame_num] is not None:
                # 座標轉換: 原圖 -> 小圖
                ox1, oy1, ox2, oy2 = self.system.state.max_box[self.frame_num]
                x1, y1, x2, y2 = int(
                    ox1*scale), int(oy1*scale), int(ox2*scale), int(oy2*scale)

                cv2.rectangle(now_frame, (x1, y1), (x2, y2),
                              (255, 0, 0), max(2, int(6*scale)))
                current_class = self.system.state.same_class[self.frame_num]
                hint_msg = self.system.state.hint_text[self.frame_num]
                text_y = y1 - int(55*scale) if y1 - \
                    int(55*scale) > 10 else y2 + 10

                # [2026-02-10 UX] Global Visibility Rule
                # If Clothes Detection is ON: Strict 100% threshold (User Request)
                # If Clothes Detection is OFF: Standard 80% threshold (Allow "Please come closer" hint)
                current_width = (x2 - x1) / scale
                target_min = self.system.state.min_face[self.frame_num]

                # [2026-03-06 Revert] Require 100% distance to show UI when clothes check is active
                is_entry = self._is_entry_active()
                clothes_active = is_entry and (CONFIG.get(
                    "Clothes_show", False) or CONFIG.get("Clothes_detection", False))
                vis_ratio = 1.0 if clothes_active else 0.8

                # [2026-04-30 Fix] 預先定義 passed_gate 避免 UnboundLocalError
                need_check_clothes = self.frame_num == 0 and (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False))
                is_clothes_pass = bool(
                    getattr(self.system.state, "clothes_gate_pass", False) and
                    time.time() - getattr(self.system.state, "clothes_gate_time", 0.0) <= 0.5
                )
                passed_gate = (not need_check_clothes) or is_clothes_pass

                if current_width >= (target_min * vis_ratio):
                    staff_name_display = self.system.state.features_dict.get(
                        "id_name", {}).get(current_class, "辨識中")

                    # [2026-03-06 Fix] 顯示邏輯重構：採用巢狀結構確保互斥
                    # Only block by clothes if they are close enough (>= target_min) to trigger clothes detection
                    blocked_by_clothes = (not passed_gate) and (
                        current_class != "None" and current_class != "__VISITOR__") and (current_width >= target_min)

                    if blocked_by_clothes:
                        now_frame = put_chinese_text(
                            now_frame, "請正確著裝", (x1, text_y), font_path, font_size, (255, 85, 0))
                    else:
                        # 通過檢查 (或無需檢查) 後，才執行原本的顯示邏輯
                        if hint_msg:
                            now_frame = put_chinese_text(
                                now_frame, hint_msg, (x1, text_y), font_path, font_size, (255, 85, 0))
                        elif current_class == "__VISITOR__":
                            now_frame = put_chinese_text(
                                now_frame, "訪客", (x1, text_y), font_path, font_size, (0, 0, 255))
                            try:
                                # 訪客頭像截取仍需使用原圖 (保持解析度)
                                if oy2 > oy1 and ox2 > ox1:
                                    self.last_visitor_face_img = original_frame[max(
                                        0, oy1):min(h, oy2), max(0, ox1):min(w, ox2)].copy()
                            except Exception:
                                pass
                        elif current_class != "None" and staff_name_display:
                            # [2026-01-19 Fix] Reset visitor img to avoid showing previous person
                            self.last_visitor_face_img = None
                            now_frame = put_chinese_text(
                                now_frame, staff_name_display, (x1, text_y), font_path, font_size, (205, 0, 0))
                        else:
                            # [2026-01-19 Fix] Reset visitor img
                            self.last_visitor_face_img = None
                            now_frame = put_chinese_text(
                                now_frame, "辨識中", (x1, text_y), font_path, font_size, (0, 0, 0))

                # 辨識後處理邏輯 (保持不變)
                if self.system.state.same_people[self.frame_num] > 0:
                    confidence = self.system.state.same_people[self.frame_num]
                    if current_class not in ["None", "__VISITOR__"]:
                        success_staff_name = self.system.state.features_dict.get(
                            "id_name", {}).get(current_class, "未知員工")

                        # [2026-02-03 Fix] 修正存檔與放行邏輯
                        # 1. 只有通過衣著檢查才放行 + 存檔
                        # 2. 未通過則提示請著裝，且不存入成功日誌
                        # 3. 出口永遠放行 (need_check_clothes 為 False)
                        if passed_gate:
                            # [2026-03-06] check_in_out (voice/API/gate) is now handled in
                            # Comparison.face_comparison after the clothes gate passes.
                            # Here we only handle image saving.
                            z_score = self.system.state.same_zscore[self.frame_num]
                            width_val = self.system.state.same_width[self.frame_num]

                            # [2026-01-24 Fix] 使用原子打包的 success_snapshot，避免 Race Condition
                            snapshot = self.system.state.success_snapshot[self.frame_num]
                            if snapshot is not None:
                                saved_img, meta = snapshot
                            else:
                                saved_img = None
                                meta = self.system.state.success_metadata[self.frame_num]

                            if saved_img is not None:
                                meta, snapshot_ok, snapshot_reason = self._prepare_snapshot_metadata(
                                    saved_img, meta)
                                if snapshot_ok:
                                    self.save_img(
                                        saved_img, "face", success_staff_name, confidence, z_score, width_val, metadata=meta)
                                else:
                                    log_name = f"{success_staff_name}_{snapshot_reason}"
                                    self.save_img(
                                        saved_img, "potential_miss", log_name, confidence, z_score, width_val, metadata=meta)
                            else:
                                if isinstance(meta, dict):
                                    meta = dict(meta)
                                    meta["snapshot_missing"] = True
                                    meta["save_requested_at"] = datetime.datetime.now().isoformat()
                                LOGGER.warning(
                                    f"[SnapshotMissing] Skip success face save: camera={self.frame_num}, staff={success_staff_name}")
                                self.save_img(
                                    self.system.state.frame_high_res[self.frame_num], "potential_miss", f"{success_staff_name}_SnapshotMissing", confidence, z_score, width_val, metadata=meta)
                        else:
                            # [2026-02-03 Fix] 衣著檢查未通過的回饋
                            # 1. 語音提示
                            self.system.speaker.say(
                                CONFIG["say"]["clothes"], "hint_clothes", priority=2)
                            # 2. 畫面提示 (這裡設定僅供下次循環參考，即時繪圖已在上方處理)
                            self.system.state.hint_text[self.frame_num] = "請正確著裝"

                            # 3. [2026-02-03 Fix] 存入 potential_miss 供稽核
                            # 即使未放行，也要記錄是「誰」因為「什麼原因」被擋下
                            snapshot = self.system.state.success_snapshot[self.frame_num]
                            if snapshot is not None:
                                saved_img, meta = snapshot
                            else:
                                saved_img = self.system.state.frame_high_res[self.frame_num]
                                meta = self.system.state.success_metadata[self.frame_num]

                            z_score = self.system.state.same_zscore[self.frame_num]
                            width_val = self.system.state.same_width[self.frame_num]

                            # 在檔名中標註失敗原因
                            log_name = f"{success_staff_name}_ClothesFail"

                            if saved_img is not None:
                                self.save_img(
                                    saved_img, "potential_miss", log_name, confidence, z_score, width_val, metadata=meta)

                    self.system.state.same_people[self.frame_num] = 0.0
                    self.system.state.success_snapshot[self.frame_num] = None
                    self.system.state.success_metadata[self.frame_num] = None

            self.show_frame = now_frame

    def _prepare_snapshot_metadata(self, saved_img, metadata):
        if not isinstance(metadata, dict):
            LOGGER.warning(
                f"[SuccessGateFail] camera={self.frame_num}, reason=MetadataMissing")
            return {"success_gate_failed": True, "success_gate_reason": "MetadataMissing"}, False, "MetadataMissing"

        meta = dict(metadata)
        saved_hash = frame_hash(saved_img)
        decision_hash = meta.get("decision_frame_hash")
        meta["saved_frame_hash"] = saved_hash
        meta["snapshot_hash_match"] = bool(
            decision_hash and saved_hash and saved_hash == decision_hash)
        meta["save_requested_at"] = datetime.datetime.now().isoformat()

        if not decision_hash or saved_hash != decision_hash:
            decision_hash_log = str(decision_hash or "")
            saved_hash_log = str(saved_hash or "")
            LOGGER.warning(
                f"[SnapshotMismatch] camera={self.frame_num}, frame_id={meta.get('frame_id')}, "
                f"decision_hash={decision_hash_log[:12]}, saved_hash={saved_hash_log[:12]}")
            meta["success_gate_failed"] = True
            meta["success_gate_reason"] = "SnapshotMismatch"
            return meta, False, "SnapshotMismatch"

        quality_score = float(meta.get("quality_score", 0.0) or 0.0)
        quality_msg = str(meta.get("quality_msg", ""))
        if quality_score <= 0.0 or quality_msg != "Pass":
            LOGGER.warning(
                f"[SuccessGateFail] camera={self.frame_num}, frame_id={meta.get('frame_id')}, "
                f"quality_score={quality_score}, quality_msg={quality_msg}")
            meta["success_gate_failed"] = True
            meta["success_gate_reason"] = "QualityNotPass"
            return meta, False, "QualityNotPass"

        meta["success_gate_failed"] = False
        meta["success_gate_reason"] = ""
        return meta, True, ""

    def updata_screen(self):
        time.sleep(0.5)
        self.win.my_thread.signal_update_img.connect(self.win.update_img)
        self.win.my_thread.signal_update_hint.connect(self.win.update_hint)
        if self.clothes_de:
            self.win.my_thread.signal_update_bgcolor.connect(
                self.win.update_bgcolor)
            self.win.my_thread.signal_update_visibility.connect(
                self.win.update_visibility)

        while not self.stop_threads:
            if self.show_frame.shape[0] == 0:
                time.sleep(0.2)
                continue
            try:
                self.win.my_thread.signal_update_img.emit(
                    self.win.img1, self.show_main())
                self.win.my_thread.signal_update_img.emit(
                    self.win.img2, self.shwo_head())

                # [2026-02-03 Fix] 僅在入口模式且開啟顯示時，才更新服裝圖示
                if self.clothes_de:
                    bg_objs = [self.win.img3, self.win.img4]
                    if self._is_entry_active():
                        # 入口模式：顯示 + 更新圖片 + 恢復背景
                        img3, img4 = self.show_save()
                        # 注意：如果原本邏輯就是紅色底，這裡只是確保切回來時恢復
                        bg_colors = [
                            "background-color: rgba(255,0,0,255);", "background-color: rgba(255,0,0,255);"]

                        self.win.my_thread.signal_update_visibility.emit(
                            bg_objs, True)  # Show
                        self.win.my_thread.signal_update_img.emit(
                            self.win.img3, img3)
                        self.win.my_thread.signal_update_img.emit(
                            self.win.img4, img4)
                        self.win.my_thread.signal_update_bgcolor.emit(
                            bg_objs, bg_colors)
                    else:
                        # 出口模式：直接隱藏元件 (解決白框殘留問題)
                        self.win.my_thread.signal_update_visibility.emit(
                            bg_objs, False)  # Hide

                color, txt = self.show_hint()
                self.win.my_thread.signal_update_hint.emit(
                    self.win.hint, color, txt)
            except Exception:
                pass
            time.sleep(0.065)

    def show_main(self):
        alpha = 0.1
        show_img = cv2.addWeighted(cv2.resize(
            self.image, (self.show_frame.shape[1], self.show_frame.shape[0])), alpha, self.show_frame, 1 - alpha, 0)
        show_img = cv2.resize(show_img, self.img1_size)
        show_img = cv2.cvtColor(show_img, cv2.COLOR_BGR2RGB)
        h, w, ch = show_img.shape
        return QPixmap.fromImage(QImage(show_img, w, h, ch * w, QImage.Format_RGB888))

    def shwo_head(self):
        path = f'{main_path}/other/clear_img.png'
        current_class = self.system.state.same_class[self.frame_num]

        # [2026-02-03 Fix] 入口衣著不合格時，強制隱藏大頭貼
        # 修正：改用 _is_entry_active() 以支援單鏡頭排程切換
        need_check_clothes = self.frame_num == 0 and (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False))
        is_clothes_pass = bool(
            getattr(self.system.state, "clothes_gate_pass", False) and
            time.time() - getattr(self.system.state, "clothes_gate_time", 0.0) <= 0.5
        )
        if need_check_clothes and not is_clothes_pass:
            # 若為辨識成功狀態但沒穿衣服，暫時視為 None 以隱藏照片
            # 但若原本就是 VISITOR 或 None，則保持原樣 (交給下方邏輯處理)
            if current_class not in ["__VISITOR__", "None"]:
                current_class = "None"

        if current_class == "__VISITOR__":
            if self.last_visitor_face_img is not None and self.last_visitor_face_img.size > 0:
                try:
                    img_rgb = cv2.cvtColor(
                        self.last_visitor_face_img, cv2.COLOR_BGR2RGB)
                    h, w, ch = img_rgb.shape
                    return QPixmap.fromImage(QImage(img_rgb.data, w, h, ch * w, QImage.Format_RGB888))
                except Exception:
                    pass
            path = f'{main_path}/other/mask.png'
        elif current_class != "None":
            path = self.system.state.profile_dict.get(current_class, path)
        return QPixmap(path)

    def show_save(self):
        h, v = "helmet_R.png", "vest_R.png"
        # [2026-03-10 Fix] Don't show green icons when suppressed
        # (e.g., avatar has disappeared but person is still standing there)
        if not getattr(self.system.state, 'clothes_display_suppressed', [False, False])[self.frame_num]:
            if self.system.state.clothes[0]:
                v = "vest_G.png"
            if self.system.state.clothes[2]:
                h = "helmet_G.png"
        return QPixmap(f'{main_path}/other/{h}'), QPixmap(f'{main_path}/other/{v}')

    def show_hint(self):
        # [2026-02-10 UX] Global Visibility Rule for Side Panel
        # Clothes ON: Strict 100%, Clothes OFF: 80% (Standard)
        box = self.system.state.max_box[self.frame_num]

        if box is not None:
            w = box[2] - box[0]
            target_min = self.system.state.min_face[self.frame_num]
            is_entry = self._is_entry_active()
            clothes_active = is_entry and (CONFIG.get(
                "Clothes_show", False) or CONFIG.get("Clothes_detection", False))
            vis_ratio = 1.0 if clothes_active else 0.8

            if w < (target_min * vis_ratio):
                return 'background-color: transparent;', ""
        else:
            return 'background-color: transparent;', ""

        # [2026-02-10 Fix] Priority Check for ANY Hint
        # If there is any hint text (e.g., "請正確著裝", "請靠近", "請站到中間"),
        # suppress the Side Panel status to avoid misleading "Identifying" state.
        current_hint = self.system.state.hint_text[self.frame_num]
        if current_hint:
            return 'background-color: transparent;', ""

        current_class = self.system.state.same_class[self.frame_num]

        # [2026-02-03 Fix] 顯示邏輯：若被衣著檢查攔截，UI 提示也應改為 "請正確著裝"
        # 修正：只有在 "偵測到人" (current_class != None) 時才檢查衣著並攔截
        # 修正 (User Feedback): 左側欄位空間不足，"請正確著裝" 會被切掉。改回顯示 "辨識中" 即可 (主畫面已有提示)。
        need_check_clothes = self.frame_num == 0 and (CONFIG.get("Clothes_detection", False) or CONFIG.get("Clothes_show", False))
        is_clothes_pass = bool(
            getattr(self.system.state, "clothes_gate_pass", False) and
            time.time() - getattr(self.system.state, "clothes_gate_time", 0.0) <= 0.5
        )

        if current_class == "__VISITOR__":
            return 'color: rgb(0, 0, 255); background-color: rgb(255, 255, 255); font: 24pt "微軟正黑體";', "訪客"
        elif current_class != "None":
            # 檢查是否被攔截
            # 只有當人臉大小 >= min_face (target_min) 時才有可能被服裝機制攔截
            target_min = self.system.state.min_face[self.frame_num]
            current_w = 0
            box = self.system.state.max_box[self.frame_num]
            if box is not None:
                current_w = box[2] - box[0]

            if need_check_clothes and not is_clothes_pass and current_w >= target_min:
                # 攔截時，隱藏文字 (回傳空白)
                # User Request: 不顯示 "辨識中"，也不顯示 "請正確著裝" (Side Panel 保持乾淨)
                return 'background-color: transparent;', ""

            name = self.system.state.features_dict.get(
                "id_name", {}).get(current_class, "辨識中")
            return 'color: rgb(0, 170, 0); background-color: rgb(255, 255, 255); font: 24pt "微軟正黑體";', name

        # [2026-02-10 UX] Hide status text if face is too small/distant
        # Default state (No recognition yet)
        box = self.system.state.max_box[self.frame_num]
        if box is not None:
            # box is [x1, y1, x2, y2] in original resolution (from Detector)
            w = box[2] - box[0]
            target_min = self.system.state.min_face[self.frame_num]

            # [Refinement] Revert buffer to 1.0 as per user request
            if w >= target_min:
                return 'color: rgb(0, 85, 255); background-color: rgb(255, 255, 255); font: 24pt "微軟正黑體";', "辨識中"

        # Too small or no box -> Show empty
        return 'background-color: transparent;', ""

    def save_img(self, img, path, staffname="", conf=0.0, z_score=0.0, width=0, metadata=None):
        # [2026-01-13 Perf] Offload disk I/O to background thread
        # [2026-02-10 Feature] Pass camera tag (In/Out) to filename
        # Priority: 1. check_in_out result (last_direction), 2. _is_entry_active() fallback
        camera_tag = self.system.state.last_direction[self.frame_num]
        LOGGER.info(
            f"DEBUG: save_img reading last_direction[{self.frame_num}] = {camera_tag}")

        if not camera_tag:
            is_entry = self._is_entry_active()
            camera_tag = "In" if is_entry else "Out"

        self.system.io_pool.submit(self._save_img_task, img.copy(
        ), path, staffname, conf, z_score, width, metadata, camera_tag)

    def _save_img_task(self, img, path, staffname, conf, z_score, width, metadata=None, camera_tag=""):
        try:
            dt = datetime.datetime.today()
            d_str, t_str = dt.strftime("%Y_%m_%d"), dt.strftime("%H;%M;%S")
            os.makedirs(f"{main_path}/img_log/{path}/{d_str}", exist_ok=True)

            tag_str = f"_{camera_tag}" if camera_tag else ""

            # [2026-02-10 Fix] Split debounce timer by direction (face_In vs face_Out)
            debounce_key = f"{path}{tag_str}"

            last_time = self.save_img_time.get(debounce_key, 0)
            if time.time() - last_time > 5 or (self.save_name_last != staffname and staffname != ""):
                fname_base = f"{t_str}{tag_str}_{staffname}_C{int(conf*100)}_Z{z_score:.2f}_W{width}" if staffname else f"{t_str}{tag_str}"
                save_dir = f"{main_path}/img_log/{path}/{d_str}"
                fname = f"{fname_base}.png"
                png_path = f"{save_dir}/{fname}"
                cv2.imwrite(png_path, img, [cv2.IMWRITE_PNG_COMPRESSION, 3])

                # [2026-01-19 Feature] Save Snapshot Metadata for Replay/Debugging
                if metadata:
                    json_path = f"{save_dir}/{fname_base}.json"
                    try:
                        metadata = dict(metadata)
                        metadata["lossless_frame_file"] = fname
                        metadata["lossless_frame_hash"] = frame_hash(img)
                        metadata["lossless_frame_format"] = "png"

                        # Convert numpy types to native python types for JSON serialization
                        def default_converter(o):
                            if isinstance(o, np.integer):
                                return int(o)
                            elif isinstance(o, np.floating):
                                return float(o)
                            elif isinstance(o, np.ndarray):
                                return o.tolist()
                            return str(o)

                        with open(json_path, 'w', encoding='utf-8') as jf:
                            json.dump(
                                metadata, jf, default=default_converter, indent=2, ensure_ascii=False)
                    except Exception as je:
                        LOGGER.error(f"Failed to save metadata JSON: {je}")

                self.save_img_time[debounce_key] = time.time()
                if staffname:
                    self.save_name_last = staffname

        except Exception as e:
            LOGGER.error(f"Async save_img failed: {e}")

    def terminate(self, event):
        print(f"Terminating CameraSystem for window {self.frame_num}...")
        self.stop_threads = True

        # [2026-01-19 Fix] Wait for UI thread worker
        if hasattr(self, 'win') and hasattr(self.win, 'my_thread'):
            self.win.my_thread.exit()
            self.win.my_thread.wait(100)  # Wait max 100ms

        # Terminate components
        self.camera.terminate()
        self.detect.terminate()
        self.compar.terminate()

        event.accept()


with open(os.path.join(os.path.dirname(__file__), "config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)
if not check_empty_string_in_dict(CONFIG):
    os._exit(0)
API = config.API(str(CONFIG["Server"]["API_url"]),
                 int(CONFIG["Server"]["location_ID"]))


@dataclass
class GlobalState:
    max_box: List[Any] = None
    same_people: List[float] = None
    same_zscore: List[float] = None
    same_width: List[int] = None
    same_class: List[str] = None
    frame: List[Any] = None
    frame_mtcnn: List[Any] = None
    frame_high_res: List[Any] = None
    frame_mtcnn_high_res: List[Any] = None
    success_frame: List[Any] = None
    # [2026-01-19 Feature] Snapshot metadata for debugging
    success_metadata: List[Any] = None
    # [2026-01-24 Fix] 原子打包 (frame, metadata)，避免 Race Condition
    success_snapshot: List[Any] = None
    # [2026-02-10 Feature] Sync In/Out direction from check_in_out
    last_direction: List[str] = None
    clothes: List[bool] = None
    clothes_gate_pass: bool = False
    clothes_gate_time: float = 0.0
    check_time: Dict[str, List[Any]] = None
    features_dict: Dict[str, Any] = None
    profile_dict: Dict[str, str] = None
    display_history: List[list] = None
    leave: int = 0
    min_face: List[int] = None
    max_points: List[Any] = None
    last_speak_time: Dict[str, float] = None
    ann_index: Any = None
    detection_interval: float = 0.1
    comparison_interval: float = 0.1
    hint_text: List[str] = None
    gaze_status: List[Any] = None
    frame_data: List[Any] = None
    head_pose: List[Any] = None
    # [2026-01-19] Part-based features for verification
    part_features: Dict[str, Dict[str, Any]] = None


class FaceRecognitionSystem:
    def __init__(self):
        self.n_camera = 2
        self.state = GlobalState()
        self.state.max_box = [None] * self.n_camera
        self.state.same_people = [0.0] * self.n_camera
        self.state.same_zscore = [0.0] * self.n_camera
        self.state.same_width = [0] * self.n_camera
        self.state.same_class = ["None"] * self.n_camera
        self.state.frame, self.state.frame_mtcnn = [None, None], [None, None]
        self.state.frame_high_res, self.state.frame_mtcnn_high_res = [
            None, None], [None, None]
        self.state.success_frame = [None, None]
        self.state.success_metadata = [None, None]  # Initialize
        # [2026-01-24 Fix] 原子打包 (frame, metadata)
        self.state.success_snapshot = [None, None]
        self.state.last_direction = ["In", "Out"]  # Default directions
        self.state.clothes = [False, False, False]
        self.state.clothes_gate_pass = False
        self.state.clothes_gate_time = 0.0
        self.state.clothes_display_suppressed = [
            False, False]  # [2026-03-10] Per-camera suppression
        self.state.check_time, self.state.features_dict, self.state.profile_dict = {}, {}, {}
        self.state.part_features = {}  # Initialize empty
        self.state.display_history = [[], []]
        gm = CONFIG.get("min_face", 100)
        self.state.min_face = [CONFIG.get("inCamera", {}).get(
            "min_face", gm), CONFIG.get("outCamera", {}).get("min_face", gm)]
        self.state.max_points = [None, None]
        self.state.last_speak_time = {}
        self.state.ann_index = AnnIndex()
        self.state.hint_text = ["", ""]
        self.state.gaze_status = [None] * self.n_camera
        self.state.frame_data = [None] * self.n_camera
        self.state.head_pose = [None] * self.n_camera
        self.mp_detectors = {}
        self.resnet = inception_resnet_v1.InceptionResnetV1(
            pretrained='vggface2').eval()

        # [2026-02-09 Fix] 使用動態載入方法，支援熱更新
        if CONFIG.get("Clothes_show", False) or CONFIG.get("Clothes_detection", False):
            self.load_clothes_model()

        self.speaker = Say_()
        self.local_media_path = os.path.join(
            os.path.dirname(__file__), "media")

        for d in ["descriptors", "pic_bak"]:
            os.makedirs(os.path.join(self.local_media_path, d), exist_ok=True)
        self.update_lock = threading.Lock()

        # [2026-01-13 Perf] Thread pool for non-blocking I/O (e.g., image saving)
        self.io_pool = ThreadPoolExecutor(max_workers=2)
        self._shutdown_flag = False
        self._network_tasks_done = {"sync": False, "voice": False, "mqtt": False}

        # [2026-04-24 Offline Resilience] Non-blocking asset rebuild on startup
        self._rebuild_assets()

        # Load features AFTER rebuild is complete
        self._load_features_and_profiles()

        # [Perf] Enable auto-tune to maximize runtime FPS based on hardware capability
        self._auto_tune_performance()

    def load_clothes_model(self):
        """
        Dynamically load the YOLOv10 clothes detection model.
        Safe to call multiple times (idempotent).
        """
        if hasattr(self, 'model_clothes') and self.model_clothes is not None:
            return

        try:
            LOGGER.info("正在載入衣著辨識模型 (YOLOv10 OpenVINO)...")
            models_dir = Path(f'{os.path.dirname(__file__)}/models')
            model_name = 'best_cloth2'
            int8_model_det_path = models_dir/'int8' / \
                f'{model_name}_openvino_model/{model_name}.xml'

            if not int8_model_det_path.exists():
                LOGGER.error(f"模型檔案不存在: {int8_model_det_path}")
                return

            self.model_clothes = YOLOv10(
                int8_model_det_path.parent, task='detect')
            LOGGER.info("衣著辨識模型載入成功。")

        except Exception as e:
            LOGGER.error(f"載入衣著模型失敗: {e}")

    def _is_network_available(self, timeout=3):
        """[2026-04-24] Quick TCP connect test to the server SSH port."""
        try:
            s = socket.create_connection(
                (CONFIG["Server"]["ip"], 22), timeout=timeout)
            s.close()
            return True
        except (OSError, socket.timeout):
            return False

    def _is_api_available(self, timeout=3):
        """Quick API reachability check before calling PVMS_Library helpers."""
        api_url = CONFIG.get("Server", {}).get("API_url", "")
        if not api_url:
            return False
        try:
            requests.get(api_url, timeout=timeout)
            return True
        except requests.exceptions.RequestException as e:
            LOGGER.warning(f"API unavailable, skipping today log refresh: {e}")
            return False

    def _rebuild_assets(self, force_voice=False):
        """[2026-04-24 Offline Resilience] Non-blocking, offline-safe asset rebuild.
        Skips rsync if no network. Uses incremental voice rebuild (no rmtree).
        Falls back to existing local data when offline."""
        LOGGER.info("Starting mandatory asset rebuild...")
        dp = os.path.join(self.local_media_path, "descriptors")
        pb = os.path.join(self.local_media_path, "pic_bak")

        # [2026-04-24] Network-aware sync: skip if offline
        if self._is_network_available():
            LOGGER.info("Network available, syncing with server...")
            sync_ok = self._sync_files_with_server()
            if sync_ok:
                self._network_tasks_done["sync"] = True
            else:
                LOGGER.warning("Server sync failed, continuing with local data.")
        else:
            LOGGER.warning("No network detected, skipping server sync. Using local data.")

        # 1. Voice (IO Bound) - Incremental rebuild (never delete existing files)
        self._incremental_voice_rebuild(force=force_voice)

        # 2. Descriptors (CPU Bound) - Run SECOND
        if os.path.exists(dp):
            shutil.rmtree(dp)
        os.makedirs(dp, exist_ok=True)

        if os.path.isdir(pb):
            pic_files = [f for f in os.listdir(
                pb) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if pic_files:
                LOGGER.info(
                    f"Stage 2/2: Rebuilding descriptors for {len(pic_files)} images...")
                # [2026-01-24 Fix] 改用 MediaPipe，與辨識端保持一致
                from init.mediapipe_handler import MediaPipeHandler
                mp_handler = MediaPipeHandler(
                    static_image_mode=True, max_num_faces=1)
                try:
                    for f in tqdm(pic_files, desc="[Descriptor Gen]"):
                        try:
                            img_pil = Image.open(
                                os.path.join(pb, f)).convert('RGB')
                            img_np = np.array(img_pil)
                            boxes, _, points = mp_handler.detect(img_np)
                            if boxes is not None:
                                emb = self.resnet(crop_face_without_forehead(
                                    img_pil, boxes[0], points[0]).unsqueeze(0))
                                np.save(os.path.join(
                                    dp, f"{os.path.splitext(f)[0]}.npy"), emb[0].detach().numpy())
                        except Exception:
                            pass
                finally:
                    mp_handler.close()

            # [2026-01-24 Fix] Force delete stale index to ensure rebuild from new descriptors
            index_path = os.path.join(self.local_media_path, "faiss.index")
            if os.path.exists(index_path):
                os.remove(index_path)
                LOGGER.info(f"Removed stale index: {index_path}")

        LOGGER.info("Assets rebuild complete.")

    def _incremental_voice_rebuild(self, force=False):
        """[2026-04-24 Offline Resilience] Incremental voice rebuild.
        Only generates missing voice files. Never deletes existing files upfront.
        If gTTS fails (no network), existing files are preserved."""
        pb = os.path.join(self.local_media_path, "pic_bak")
        vp = os.path.join(main_path, "voice")
        os.makedirs(vp, exist_ok=True)

        if not os.path.isdir(pb):
            return

        generic_texts = {}
        for key, val in CONFIG.get("say", {}).items():
            txt = val.replace("name_", "") if "name_" in val else val
            generic_texts[key] = txt
        hint_texts = dict(HINT_VOICE_TEXTS)
        hint_texts["hint_clothes"] = generic_texts.get("clothes", "請正確著裝")

        names = set()
        for f in os.listdir(pb):
            if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                try:
                    names.add(os.path.splitext(f)[0].split("_")[-1])
                except Exception:
                    pass

        # Build expected file set
        expected_files = set()
        tasks = []
        for key, txt in generic_texts.items():
            fn = f"_{key}.mp3"
            expected_files.add(fn)
            if force or not os.path.isfile(os.path.join(vp, fn)):
                tasks.append((fn, txt))
        for key, txt in hint_texts.items():
            fn = f"{key}.mp3"
            expected_files.add(fn)
            if force or not os.path.isfile(os.path.join(vp, fn)):
                tasks.append((fn, txt))
        for name in names:
            for key, txt in generic_texts.items():
                fn = f"{name}_{key}.mp3"
                expected_files.add(fn)
                if force or not os.path.isfile(os.path.join(vp, fn)):
                    tasks.append((fn, f"{name}{txt}"))

        # Remove orphaned voice files (people removed from pic_bak)
        existing_files = set(f for f in os.listdir(vp) if f.endswith('.mp3'))
        orphaned = existing_files - expected_files
        for f in orphaned:
            try:
                os.remove(os.path.join(vp, f))
                LOGGER.info(f"Removed orphaned voice file: {f}")
            except Exception:
                pass

        if not tasks:
            LOGGER.info("Voice files up-to-date, no generation needed.")
            self._network_tasks_done["voice"] = True
            return

        self._network_tasks_done["voice"] = False
        LOGGER.info(f"Stage 1/2: Generating {len(tasks)} voice files...")
        gen_success = 0
        gen_fail = 0

        def gen_one_voice(filename, text):
            target = os.path.join(vp, filename)
            tmp_target = f"{target}.{os.getpid()}.tmp"
            try:
                tts = gTTS(text=text, lang='zh-tw')
                tts.save(tmp_target)
                os.replace(tmp_target, target)
                return True
            except Exception:
                try:
                    if os.path.exists(tmp_target):
                        os.remove(tmp_target)
                except Exception:
                    pass
                return False

        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(gen_one_voice, fn, txt): fn
                       for fn, txt in tasks}
            for f in tqdm(futures, desc="[Voice Gen     ]"):
                try:
                    if f.result():
                        gen_success += 1
                    else:
                        gen_fail += 1
                except Exception:
                    gen_fail += 1

        if gen_fail == 0:
            self._network_tasks_done["voice"] = True
            LOGGER.info(f"Voice generation complete: {gen_success} files generated.")
        else:
            LOGGER.warning(f"Voice generation partial: {gen_success} OK, {gen_fail} failed (likely no network). Will retry later.")

    def _auto_tune_performance(self):
        img, ts = np.random.randint(
            0, 255, (600, 800, 3), dtype=np.uint8), torch.randn(1, 3, 160, 160)
        from init.mediapipe_handler import MediaPipeHandler
        mp = MediaPipeHandler()
        try:
            mp.detect(img)
            self.resnet(ts)
        except Exception:
            pass
        t0 = time.time()
        for _ in range(5):
            mp.detect(img)
        dt = (time.time() - t0) / 5
        t0 = time.time()
        for _ in range(5):
            self.resnet(ts)
        rt = (time.time() - t0) / 5
        self.state.detection_interval = max(0.10, min(0.5, dt * 2.0))
        self.state.comparison_interval = max(0.10, min(0.5, rt * 1.5))
        LOGGER.info(
            f"Auto-Tuning: Det {1/self.state.detection_interval:.1f} FPS, Rec {1/self.state.comparison_interval:.1f} FPS")

    def run(self):
        # [2026-01-19 Fix TTY] Backup terminal settings at startup
        try:
            self.original_tty_settings = termios.tcgetattr(sys.stdin)
        except Exception:
            self.original_tty_settings = None

        app = QApplication(sys.argv)

        # [2026-01-30 Feature] Apply Global Theme
        try:
            theme = CONFIG.get("theme", "dark")
            app.setStyleSheet(styles.get_stylesheet(theme))
        except Exception as e:
            print(f"Failed to apply theme: {e}")

        safe_shutdown_pipe_read, self.safe_shutdown_pipe_write = os.pipe()
        def signal_handler(sig, frame): os.write(
            self.safe_shutdown_pipe_write, b'x')
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        self.shutdown_notifier = QSocketNotifier(
            safe_shutdown_pipe_read, QSocketNotifier.Read)
        self.shutdown_notifier.activated.connect(
            lambda: (os.read(safe_shutdown_pipe_read, 1), self._safe_shutdown()))

        # [2026-01-30 Feature] Soft Reload via SIGHUP
        signal.signal(signal.SIGHUP, self._handle_sighup)

        # [2026-02-01 Feature] Start Web Config Server
        try:
            web_port = 5000
            threading.Thread(target=run_web_server, args=(
                web_port,), daemon=True).start()
            LOGGER.info(f"Web Config Server started on port {web_port}")
        except Exception as e:
            LOGGER.error(f"Failed to start Web Server: {e}")

        self._load_features_and_profiles()

        # Keep the recognition UI on the critical startup path. Network/API
        # refreshes are best-effort and must not block window creation after
        # an outage or partial network recovery.
        try:
            LOGGER.info("Setting up camera windows...")
            self.setup_cameras()
            LOGGER.info("Camera windows initialized.")
        except Exception as e:
            LOGGER.exception(f"Failed to setup camera windows: {e}")
            raise

        self.setup_mqtt_client()
        self._start_inout_log_loop()

        # [2026-04-24 Offline Resilience] Start background network retry loop
        self._start_network_retry_loop()
        try:
            ret = app.exec_()
        finally:
            # [2026-01-19 Fix] Force exit to prevent hanging/high-load due to daemon threads spinning
            # or C++ resource cleanup issues (OpenCV/MediaPipe).

            # [2026-01-19 Fix TTY] Synchronously restore terminal state BEFORE exit using Python termios
            # This is more reliable than os.system('stty sane')
            if self.original_tty_settings:
                print("Restoring terminal settings via termios...")
                try:
                    termios.tcsetattr(
                        sys.stdin, termios.TCSADRAIN, self.original_tty_settings)
                except Exception:
                    pass

            # [2026-01-19 Fix TTY] Launch a detached "rescuer" process.
            # Even if we restore termios above, background C++ threads (OpenCV/FFmpeg)
            # might corrupt the TTY during the final os._exit().
            # This external 'stty sane' will run 0.1s AFTER we die to clean up any mess.
            # start_new_session=True ensures it survives our os._exit().
            try:
                subprocess.Popen(
                    "sleep 0.1; stty sane",
                    shell=True,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception:
                pass

            print("Force exiting system...")
            os._exit(0)

    def setup_mqtt_client(self):
        s_ip = CONFIG.get("Server", {}).get("ip", "localhost")
        m_conf = CONFIG.get("MQTT", {})
        self.mqtt_broker_host = m_conf.get("broker_ip", s_ip)
        self.mqtt_port, self.mqtt_topic = m_conf.get(
            "port", 1883), m_conf.get("topic", "pvms/faces/updated")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self.on_mqtt_connect
        client.on_message = self.on_mqtt_message
        try:
            client.connect(self.mqtt_broker_host, self.mqtt_port, 60)
            client.loop_start()
            self._network_tasks_done["mqtt"] = True
            LOGGER.info("MQTT connected successfully.")
        except Exception as e:
            LOGGER.warning(f"MQTT connection failed: {e}. Will retry in background.")

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(self.mqtt_topic)
            self.update_data_and_model(initial_run=True)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            self.update_data_and_model(initial_run=False)
        except Exception:
            pass

    def update_data_and_model(self, initial_run=True):
        if not self.update_lock.acquire(blocking=False):
            return
        try:
            if not self._sync_files_with_server():
                return
            pb, dp = os.path.join(self.local_media_path, "pic_bak"), os.path.join(
                self.local_media_path, "descriptors")
            pic_map = {os.path.splitext(f)[0]: f for f in os.listdir(
                pb) if f.lower().endswith(('.png', '.jpg', '.jpeg'))}
            local_desc = set(os.path.splitext(f)[0] for f in os.listdir(
                dp) if f.lower().endswith('.npy'))
            new_files = [pic_map[b] for b in pic_map if b not in local_desc or os.path.getmtime(
                os.path.join(pb, pic_map[b])) > os.path.getmtime(os.path.join(dp, f"{b}.npy"))]
            deleted = [f"{n}.npy" for n in local_desc - set(pic_map.keys())]
            if new_files or deleted:
                for f in deleted:
                    try:
                        os.remove(os.path.join(dp, f))
                    except Exception:
                        pass
                self._generate_new_descriptors(new_files, pb, dp)
                f, _, _, _, _ = self._load_features_from_disk()
                self.state.features_dict = f
                self._update_profile_dict()
                self._load_or_build_index(force_rebuild=True)
                self._incremental_voice_rebuild()
        finally:
            self.update_lock.release()

    def _sync_files_with_server(self):
        s_dir = CONFIG["Server"]["face_data_dir"]
        s_conf = {"ip": CONFIG["Server"]["ip"],
                  "username": CONFIG["Server"]["username"]}
        sp, dp = os.path.join(s_dir, "pic_bak").replace(
            '\\', '/'), os.path.join(self.local_media_path, "pic_bak").replace('\\', '/')
        return ssh.sync_with_rsync(s_conf, sp, dp)

    def _process_deleted_descriptors(self, deleted_files, descriptors_path):
        for f in deleted_files:
            try:
                os.remove(os.path.join(descriptors_path, f))
            except OSError:
                pass

    def _generate_new_descriptors(self, new_files, pb, dp):
        # [2026-01-24 Fix] 改用 MediaPipe，與辨識端保持一致
        from init.mediapipe_handler import MediaPipeHandler
        mp_handler = MediaPipeHandler(static_image_mode=True, max_num_faces=1)
        try:
            for f in tqdm(new_files, desc="[1/2] Descriptor Generation"):
                try:
                    img_pil = Image.open(os.path.join(pb, f)).convert('RGB')
                    img_np = np.array(img_pil)
                    boxes, _, points = mp_handler.detect(img_np)
                    if boxes is not None:
                        emb = self.resnet(crop_face_without_forehead(
                            img_pil, boxes[0], points[0]).unsqueeze(0))
                        np.save(os.path.join(
                            dp, f"{os.path.splitext(f)[0]}.npy"), emb[0].detach().numpy())
                except Exception:
                    pass
        finally:
            mp_handler.close()

    def _load_features_from_disk(self):
        dp = os.path.join(self.local_media_path, "descriptors")
        xt, yt, xv, yv = [], [], [], []
        feat = {"id_name": {}}
        for f in [f for f in os.listdir(dp) if f.lower().endswith('.npy')]:
            cat, name = f.split("_")[0], f.split("_")[-1].split(".")[0]
            load = np.load(os.path.join(dp, f))
            if cat not in feat:
                feat[cat] = []
                xv.append(load)
                yv.append(cat)
            else:
                xt.append(load)
                yt.append(cat)
            feat[cat].append(load)
            feat["id_name"][cat] = name
        return feat, xt, yt, xv, yv

    def _load_features_and_profiles(self):
        try:
            f, _, _, _, _ = self._load_features_from_disk()
            self.state.features_dict = f
            self._update_profile_dict()
            self._load_or_build_index(force_rebuild=False)
            # [2026-02-01 Optimization] Disable unused Part Feature Generation to speed up startup
            # self._load_part_features()
        except Exception as e:
            LOGGER.warning(f"Failed to load features/profiles: {e}", exc_info=True)

    def _load_part_features(self):
        """
        [2026-01-19] Pre-calculate Eye/Nose/Mouth embeddings from enrollment photos.
        This enables part-based verification to reject high-confidence misidentifications.
        """
        pb = os.path.join(self.local_media_path, "pic_bak")
        if not os.path.isdir(pb):
            return

        from init.mediapipe_handler import MediaPipeHandler
        mp_handler = MediaPipeHandler(max_num_faces=1)  # Temporary instance

        try:
            pic_files = [f for f in os.listdir(
                pb) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if not pic_files:
                return

            LOGGER.info(f"Loading Part Features for {len(pic_files)} users...")
            part_db = {}

            for f in tqdm(pic_files, desc="[Part Feature Gen]"):
                try:
                    # Filename: G07_..._Name.jpg
                    bn = os.path.splitext(f)[0]
                    staff_id = bn.split('_')[0]

                    img_path = os.path.join(pb, f)
                    img_bgr = cv2.imread(img_path)
                    if img_bgr is None:
                        continue
                    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

                    boxes, _, points = mp_handler.detect(img_rgb)
                    if boxes is not None:
                        # Get part crops (Eye, Nose, Mouth)
                        # Note: We convert to PIL inside get_parts_crop if needed, or pass PIL
                        img_pil = Image.fromarray(img_rgb)
                        parts_tensors, _ = get_parts_crop(
                            img_pil, points[0])  # Ignore coords here

                        parts_emb = {}
                        for p_name, p_tensor in parts_tensors.items():
                            emb = self.resnet(
                                p_tensor.unsqueeze(0)).detach().numpy()[0]
                            parts_emb[p_name] = emb

                        part_db[staff_id] = parts_emb
                except Exception as e:
                    pass

            self.state.part_features = part_db
            LOGGER.info(f"Loaded Part Features for {len(part_db)} users.")
        finally:
            mp_handler.close()

    def _update_profile_dict(self):
        pb = os.path.join(self.local_media_path, "pic_bak")
        if not os.path.isdir(pb):
            self.state.profile_dict = {}
            return
        lps = {}
        for f in os.listdir(pb):
            try:
                bn = os.path.splitext(f)[0]
                pts = bn.split('_', 1)
                if len(pts) < 2:
                    continue
                c, sk = pts[0].upper(), pts[1]
                if c not in lps or sk > lps[c]['sk']:
                    lps[c] = {'path': os.path.join(pb, f), 'sk': sk}
            except Exception:
                continue
        self.state.profile_dict = {c: d['path'] for c, d in lps.items()}

    def _load_or_build_index(self, force_rebuild=False):
        if not self.state.features_dict or not any(self.state.features_dict.values()):
            return

        # 嘗試載入現有索引
        if not force_rebuild and self.state.ann_index.load():
            # [2026-01-18 Fix] 強化同步檢查：不只檢查數量，還檢查 ID 集合是否一致
            # 避免 "刪一增一" 導致數量相同但內容過期的問題

            # 1. 檢查數量
            current_count = sum(
                len(v) for k, v in self.state.features_dict.items() if k != 'id_name')
            if self.state.ann_index.index and self.state.ann_index.index.ntotal == current_count:
                # 2. 檢查 ID 內容 (確保索引內的 ID 都在目前的 features_dict 中)
                # self.state.ann_index.id_map 儲存了索引中每個向量對應的 Person ID
                cached_ids = set(self.state.ann_index.id_map)
                current_ids = set(
                    k for k in self.state.features_dict.keys() if k != 'id_name')

                # 如果快取中的 ID 集合與目前的 ID 集合完全一致，才視為有效
                if cached_ids == current_ids:
                    return
                else:
                    LOGGER.warning("索引 ID 與目前檔案不符 (可能是刪除/新增導致數量巧合)，強制重建索引。")
            else:
                LOGGER.info(
                    f"索引數量不符 (Index: {self.state.ann_index.index.ntotal}, Files: {current_count})，重建索引。")

        # 重建索引
        self.state.ann_index.build(self.state.features_dict)

    def setup_cameras(self):
        ips = [CONFIG["cameraIP"]["in_camera"],
               CONFIG["cameraIP"]["out_camera"]]
        n = 2 if ips[0] != ips[1] else 1
        self.n_camera = n

        # [2026-01-30 Fix] Ensure only ONE CameraSystem is created if n=1 (Same IP)
        if n == 1:
            # Only process the first IP (index 0)
            target_source = [(0, ips[0])]
        else:
            # Process both
            target_source = enumerate(ips)

        self.cameras = [CameraSystem(ip, i, n, self, CONFIG)
                        for i, ip in target_source if ip != "0"]

        for cam in self.cameras:
            if CONFIG.get("full_screen", False):
                cam.win.showFullScreen()
            else:
                cam.win.showNormal()
            cam.win.activateWindow()
            cam.win.raise_()

    def _handle_sighup(self, signum, frame):
        """Handle SIGHUP signal to reload configuration."""
        LOGGER.info("Received SIGHUP. Scheduling configuration reload...")
        # Schedule reload in the main thread event loop
        QTimer.singleShot(0, self._reload_configuration)

    def _reload_configuration(self):
        """Reload configuration and restart camera systems."""
        LOGGER.info("Reloading configuration and restarting subsystems...")
        app = QApplication.instance()
        old_quit_on_last_window = app.quitOnLastWindowClosed() if app else True
        if app:
            app.setQuitOnLastWindowClosed(False)

        # 1. Terminate existing cameras
        if hasattr(self, 'cameras'):
            for cam in self.cameras:
                try:
                    if hasattr(cam, 'win'):
                        cam.win.close()  # This triggers cam.terminate
                    QApplication.processEvents()
                    if not getattr(cam, 'stop_threads', False):
                        class _ReloadCloseEvent:
                            def accept(self): pass
                        cam.terminate(_ReloadCloseEvent())
                except Exception as e:
                    LOGGER.error(f"Error closing camera window: {e}")

            self.cameras = []

        # 2. Reload Config
        global CONFIG
        try:
            with open(os.path.join(os.path.dirname(__file__), "config.json"), "r", encoding="utf-8") as f:
                new_config = json.load(f)

            # [2026-01-30 Fix] Check if asset rebuild is needed
            # Rebuild if 'say' (voice content) or 'Server' (staff list source) changed
            say_changed = new_config.get("say") != CONFIG.get("say")
            need_rebuild = say_changed or \
                           (new_config.get("Server") != CONFIG.get("Server"))

            # Update global CONFIG
            CONFIG.clear()
            CONFIG.update(new_config)
            LOGGER.info("Configuration reloaded from disk.")

            # [2026-01-30 Fix] Reload Theme
            try:
                theme = CONFIG.get("theme", "dark")
                from ui import styles
                QApplication.instance().setStyleSheet(styles.get_stylesheet(theme))
            except Exception as e:
                LOGGER.error(f"Failed to reload theme: {e}")

            # [2026-01-30 Fix] Reset Speaker
            try:
                if hasattr(self, 'speaker'):
                    self.speaker.reset()

                # Rebuild assets if needed (Voice & Descriptors)
                if need_rebuild:
                    LOGGER.info(
                        "Config changed (Say/Server), triggering asset rebuild...")
                    # This handles syncing, voice generation, and descriptor generation
                    # Note: This is blocking, UI might freeze briefly
                    self._rebuild_assets(force_voice=say_changed)
                    # [2026-04-24 Fix] Reload features into memory after disk rebuild
                    # Without this, self.state.features_dict and ann_index remain stale
                    self._load_features_and_profiles()
                    # [2026-04-24 Fix] Restart retry loop if any network tasks failed during reload
                    self._start_network_retry_loop()
                else:
                    LOGGER.info(
                        "Config changed (Params only), skipping asset rebuild.")

            except Exception as e:
                LOGGER.error(f"Failed to reset speaker or rebuild assets: {e}")

            # [2026-01-30 Fix] Sync function.py CONFIG global variable
            # function.py loads its own CONFIG copy on import, which becomes stale on reload.
            # We must explicitly update it.
            import init.function as function
            function.CONFIG = CONFIG

            # [2026-02-06 Fix] Sync init.model CONFIG global variable
            # Detector logic in init/model.py relies on its own CONFIG copy.
            init.model.CONFIG = CONFIG

            # [2026-05-30 Fix] Update dynamic runtime variables from new CONFIG
            gm = CONFIG.get("min_face", 100)
            self.state.min_face = [CONFIG.get("inCamera", {}).get(
                "min_face", gm), CONFIG.get("outCamera", {}).get("min_face", gm)]

            LOGGER.info(
                "Synced configuration to function and init.model modules.")

        except Exception as e:
            LOGGER.error(f"Failed to reload config.json: {e}")
            if app:
                app.setQuitOnLastWindowClosed(old_quit_on_last_window)
            return

        # 3. Re-setup cameras with new config
        # setup_cameras uses global CONFIG
        try:
            self.setup_cameras()
            LOGGER.info("Cameras re-initialized.")
        except Exception as e:
            LOGGER.error(f"Failed to setup cameras: {e}")
        finally:
            if app:
                app.setQuitOnLastWindowClosed(old_quit_on_last_window)

    def update_inout_log(self):
        """Refresh today's in/out state once.

        This may touch the remote API through PVMS_Library. It is intentionally
        called from a daemon worker so a slow or half-restored network cannot
        block Qt window creation.
        """
        try:
            if not self._is_api_available(timeout=3):
                return
            LOGGER.info("Refreshing today's in/out log...")
            lj = API.Scan_today_log()
            for sid in lj.keys():
                if lj[sid]["state"] == "enter":
                    self.state.check_time[sid] = [False, time.time()-100]
                elif sid in self.state.check_time:
                    self.state.check_time[sid] = [True, 0]
                    t = threading.Timer(5, clear_leave_employee, (self, sid, ))
                    t.daemon = True
                    t.start()
            LOGGER.info("Today's in/out log refreshed.")
        except Exception as e:
            LOGGER.warning(f"Failed to refresh today's in/out log: {e}", exc_info=True)

    def _start_inout_log_loop(self):
        """Start periodic today-log refresh without blocking startup."""
        if hasattr(self, '_inout_log_thread') and self._inout_log_thread.is_alive():
            LOGGER.info("In/out log updater already running.")
            return

        def _worker():
            LOGGER.info("Starting background in/out log updater (interval=300s)...")
            while not self._shutdown_flag:
                self.update_inout_log()
                for _ in range(300):
                    if self._shutdown_flag:
                        break
                    time.sleep(1)

        self._inout_log_thread = threading.Thread(
            target=_worker, daemon=True, name="inout-log-updater")
        self._inout_log_thread.start()

    def _start_network_retry_loop(self):
        """[2026-04-24 Offline Resilience] Background thread that retries all
        network-dependent tasks every 15 seconds until all succeed."""
        if all(self._network_tasks_done.values()):
            LOGGER.info("All network tasks already done at startup. No retry loop needed.")
            return

        if hasattr(self, '_retry_thread') and self._retry_thread.is_alive():
            LOGGER.info("Network retry loop already running.")
            return

        LOGGER.info("Starting background network retry loop (interval=15s)...")

        def _retry_worker():
            while not self._shutdown_flag:
                time.sleep(15)
                if self._shutdown_flag:
                    break
                if not self._is_network_available(timeout=3):
                    continue

                LOGGER.info("Network detected! Resuming pending tasks...")

                # Task 1: Sync files with server
                if not self._network_tasks_done["sync"]:
                    try:
                        if self._sync_files_with_server():
                            self._network_tasks_done["sync"] = True
                            LOGGER.info("[Retry] Server sync completed.")
                            # After sync, rebuild descriptors incrementally
                            try:
                                self.update_data_and_model(initial_run=True)
                            except Exception as e:
                                LOGGER.error(f"[Retry] Incremental model update failed: {e}")
                        else:
                            LOGGER.warning("[Retry] Server sync failed, will retry.")
                    except Exception as e:
                        LOGGER.error(f"[Retry] Sync exception: {e}")

                # Task 2: Voice files
                if not self._network_tasks_done["voice"]:
                    try:
                        self._incremental_voice_rebuild()
                    except Exception as e:
                        LOGGER.error(f"[Retry] Voice rebuild failed: {e}")

                # Task 3: MQTT reconnect
                if not self._network_tasks_done["mqtt"]:
                    try:
                        self.setup_mqtt_client()
                    except Exception as e:
                        LOGGER.error(f"[Retry] MQTT reconnect failed: {e}")

                # Check if all done
                if all(self._network_tasks_done.values()):
                    LOGGER.info("All network-dependent tasks completed successfully. Stopping retry loop.")
                    break

        self._retry_thread = threading.Thread(target=_retry_worker, daemon=True, name="net-retry")
        self._retry_thread.start()

    def _safe_shutdown(self):
        self._shutdown_flag = True
        if hasattr(self, 'speaker'):
            self.speaker.terminate()
        # [2026-01-13 Perf] Shutdown IO pool
        if hasattr(self, 'io_pool'):
            self.io_pool.shutdown(wait=False)
        QApplication.closeAllWindows()


if __name__ == "__main__":
    try:
        FaceRecognitionSystem().run()
    except Exception as e:
        LOGGER.exception(f"Fatal startup/runtime error: {e}")
        os._exit(1)
