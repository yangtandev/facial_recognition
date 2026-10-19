from PyQt5.QtWidgets import QWidget, QLabel
from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from ui.user import Ui_Show_from
from ui.dy_user import Ui_dynamic_Form
from ui.user_only import Ui_Form
from PyQt5.QtGui import QIcon, QPixmap, QFont
from PyQt5.QtWidgets import (
    QApplication, QPushButton, QLineEdit, QMessageBox, QDialog,
    QVBoxLayout, QHBoxLayout
)
from PyQt5 import QtCore
from ui.window_title import LargeTitleBar
import os, json, time, subprocess, sys
from ota.health import BUILD_IDENTITY

def get_app_version():
    if BUILD_IDENTITY["version"] != "unknown":
        return BUILD_IDENTITY["version"]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    release_note = os.path.join(root, "RELEASE_NOTES.md")
    try:
        with open(release_note, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("## v"):
                    return line.split()[1]
    except Exception:
        pass
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


config_ = {}
try:
    with open(os.path.join(os.path.dirname(__file__), "../config.json"), "r", encoding="utf-8") as json_file:
        config_ = json.load(json_file)
except Exception as e:
    print("ui_show-載入失敗", e)

PRIMARY_HINT_FONT_MIN = 18
PRIMARY_HINT_FONT_MAX = 30
PRIMARY_HINT_FONT_HEIGHT_RATIO = 0.028


class AdminPasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("身分驗證")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.FramelessWindowHint)
        self.setModal(True)
        self.resize(560, 260)
        theme = config_.get("theme", "dark")

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        layout.addWidget(LargeTitleBar("身分驗證", self, show_close=True, theme=theme))

        body = QWidget()
        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(28, 24, 28, 24)
        body_layout.setSpacing(18)
        body.setLayout(body_layout)
        layout.addWidget(body, 1)

        label = QLabel("請輸入管理員密碼:")
        label.setFont(QFont("Microsoft JhengHei", 16, QFont.Bold))
        body_layout.addWidget(label)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setFont(QFont("Microsoft JhengHei", 16))
        self.password_edit.returnPressed.connect(self.accept)
        body_layout.addWidget(self.password_edit)

        button_layout = QHBoxLayout()
        self.cancel_button = QPushButton("取消")
        self.ok_button = QPushButton("確定")
        self.cancel_button.setObjectName("adminCancelButton")
        self.ok_button.setObjectName("adminOkButton")
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        button_layout.addStretch(1)
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.ok_button)
        body_layout.addLayout(button_layout)

        if theme == "light":
            body_style = """
            QWidget {
                background-color: #f0f0f0;
                color: #000000;
            }
            QLabel {
                color: #000000;
                font-size: 16pt;
                font-weight: 700;
            }
            QLineEdit {
                background-color: #ffffff;
                border: 2px solid #999;
                color: #000000;
                font-size: 16pt;
                min-height: 48px;
                padding: 8px 12px;
            }
            QPushButton#adminCancelButton, QPushButton#adminOkButton {
                background-color: #e0e0e0;
                color: #000000;
                border: 1px solid #aaa;
                border-radius: 4px;
                font-size: 16pt;
                min-height: 48px;
                min-width: 96px;
                padding: 8px 22px;
            }
            QPushButton#adminCancelButton:hover, QPushButton#adminOkButton:hover {
                background-color: #d0d0d0;
            }
            """
        else:
            body_style = """
            QWidget {
                background-color: #2b2b2b;
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
                font-size: 16pt;
                font-weight: 700;
            }
            QLineEdit {
                background-color: #3b3b3b;
                border: 2px solid #777;
                color: #ffffff;
                font-size: 16pt;
                min-height: 48px;
                padding: 8px 12px;
            }
            QPushButton#adminCancelButton, QPushButton#adminOkButton {
                background-color: #555;
                color: #ffffff;
                border: 1px solid #777;
                border-radius: 4px;
                font-size: 16pt;
                min-height: 48px;
                min-width: 96px;
                padding: 8px 22px;
            }
            QPushButton#adminCancelButton:hover, QPushButton#adminOkButton:hover {
                background-color: #666;
            }
            """
        body.setStyleSheet(body_style)

    def textValue(self):
        return self.password_edit.text()


class MainWindow(QWidget, Ui_Form):
    def __init__(self, fun, frame_num, parent=None):
        super(MainWindow, self).__init__(parent)
        self.setupUi(self)
        self.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "face-detection.png")))
        self.resizeEvent = self.win_resize
        self.obj = [self, self.img1, self.hint2, self.img2, self.img3, self.img4, self.hint]
        
        # [2026-01-30 Fix] Always reload config on init to support soft reload
        self.reload_config()
        
        # [2026-01-30 Fix] Clear hardcoded styles (white bg) from auto-generated UI to allow Dark Theme
        try:
            # user_only.py hardcodes white backgrounds on these labels
            self.img1.setStyleSheet("")
            self.hint.setStyleSheet("")
            self.hint2.setStyleSheet("")
            # Also clear any others if inherited from other UIs (though user_show uses user_only)
            self.min_face.setStyleSheet("")
            self.in_voice.setStyleSheet("")
            self.out_voice.setStyleSheet("")
            self.clothes_voice.setStyleSheet("")
        except Exception: pass
        self.apply_primary_hint_style()
        
        if frame_num == 0:
            self.setWindowTitle(f"進入視窗")
        if frame_num == 1:
            self.setWindowTitle(f"離開視窗")
        self.title_label = QLabel(self.title_text_html(self.windowTitle()), self)
        self.title_label.setAlignment(self.hint2.alignment())
        self.title_label.setWordWrap(True)
        self.title_label.hide()

        self.img1.setScaledContents(True)
        self.img2.setScaledContents(True)
        self.img3.setStyleSheet("QLabel{background-color: rgba(255,255,255,0);}")
        self.img4.setStyleSheet("QLabel{background-color: rgba(255,255,255,0);}")

        self.org_point = []
        for i in range( len(self.obj)):
            height = self.obj[i].geometry().height()
            left_ = self.obj[i].geometry().left()
            width = self.obj[i].geometry().width()
            top = self.obj[i].geometry().top()
            self.org_point.append([height, left_, width, top])
        
        self.frame_num = frame_num
        self.app_version = f"v{get_app_version().lstrip('v')}"
        self.my_thread = MyThread()
        self.my_thread.run = fun
        self.my_thread.start()
        self.update_screen()
        
        # [2026-01-30 Feature] Add Settings Button
        self.btn_setting = QPushButton("⚙", self)
        self.btn_setting.setGeometry(10, 10, 40, 40)
        self.btn_setting.setStyleSheet("background-color: rgba(0,0,0,100); color: white; border-radius: 5px; font-size: 20px;")
        self.btn_setting.clicked.connect(self.open_settings)
        self.btn_setting.show()
        self.btn_setting.raise_()

        self.version_label = QLabel(self.app_version, self)
        version_font = self.hint2.font()
        version_font.setPointSize(14)
        self.version_label.setFont(version_font)
        self.version_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.version_label.setStyleSheet(
            self.version_label_style(self.hint2.styleSheet()))
        self.version_label.hide()
        self.win_resize(None)
        self.btn_setting.raise_()

        # 設定定時器

    def reload_config(self):
        global config_
        try:
            with open(os.path.join(os.path.dirname(__file__), "../config.json"), "r", encoding="utf-8") as json_file:
                config_ = json.load(json_file)
        except Exception as e:
            print("ui_show-重新載入失敗", e)
        # [2026-01-19 Fix] 移除 30 秒自動重置視窗大小的機制，允許使用者手動調整版面
        # self.timer = QTimer(self)
        # self.timer.timeout.connect(self.update_screen)
        # self.timer.start(30000)  # 每1000毫秒（1秒）更新一次

    def open_settings(self):
        """Open the external setting tool with password protection."""
        dlg = AdminPasswordDialog(self)
        
        # Force center on parent
        # Note: dlg.exec_() blocks, so we move before exec.
        # But dlg size might not be calculated yet.
        # We trust Qt parent centering usually, but if it fails (top-left),
        # we can try to force move.
        
        if dlg.exec_() == QDialog.Accepted:
            text = dlg.textValue()
            # Default password is 'admin', or matching the server password if available?
            # Let's use 'admin' for simplicity as requested "Option A".
            if text == 'admin':
                try:
                    # Launch setting_tool.py as a separate process
                    # [2026-02-09 Fix] setting_tool.py moved to ui/ directory
                    # user_show.py is in ui/, so setting_tool.py is in the same directory
                    tool_path = os.path.join(os.path.dirname(__file__), "setting_tool.py")
                    
                    # [2026-01-30 Fix] Calculate global geometry for correct centering (even in fullscreen)
                    global_pos = self.mapToGlobal(QtCore.QPoint(0, 0))
                    
                    args = [
                        sys.executable, tool_path,
                        "--parent_x", str(global_pos.x()),
                        "--parent_y", str(global_pos.y()),
                        "--parent_w", str(self.width()),
                        "--parent_h", str(self.height())
                    ]
                    
                    subprocess.Popen(args)
                except Exception as e:
                    QMessageBox.critical(self, "錯誤", f"無法啟動設定工具: {e}")
            else:
                QMessageBox.warning(self, "錯誤", "密碼錯誤")

    def update_img( self, obj, pixmap:QPixmap):
        obj.setPixmap(pixmap)

    def update_bgcolor(self, obj, color):
        for i in range(len(obj)):
            obj[i].setStyleSheet(color[i])

    def update_visibility(self, obj, visible):
        for i in range(len(obj)):
            obj[i].setVisible(visible)

    def update_hint(self, obj, color, txt):
        obj.setStyleSheet(color)
        obj.setText(txt)
        if obj is self.hint2:
            self.apply_primary_hint_style()
        if obj is self.hint2 and hasattr(self, "version_label"):
            self.version_label.setStyleSheet(self.version_label_style(color))

    def apply_primary_hint_style(self):
        font = QFont("Microsoft JhengHei", self.primary_hint_font_size(), QFont.Bold)
        self.hint2.setFont(font)
        self.hint2.setWordWrap(True)

    def primary_hint_font_size(self):
        return max(
            PRIMARY_HINT_FONT_MIN,
            min(PRIMARY_HINT_FONT_MAX, int(self.height() * PRIMARY_HINT_FONT_HEIGHT_RATIO)))

    def primary_hint_text_color(self):
        return "black" if config_.get("theme", "dark") == "light" else "white"

    def version_label_style(self, style):
        keep = []
        for part in style.split(";"):
            rule = part.strip()
            if not rule:
                continue
            prop = rule.split(":", 1)[0].strip().lower()
            if prop in ("background", "background-color", "font", "font-size"):
                continue
            keep.append(rule)
        keep.extend(["background-color: transparent", "font-size: 14pt"])
        return "; ".join(keep) + ";"

    def position_version_label(self):
        if not hasattr(self, "version_label"):
            return
        if self.width() < 20 or self.height() < 20:
            self.version_label.hide()
            return
        self.version_label.adjustSize()
        margin = 10
        self.version_label.move(
            self.width() - self.version_label.width() - margin,
            self.height() - self.version_label.height() - margin)
        self.version_label.show()
        self.version_label.raise_()
        if hasattr(self, "btn_setting"):
            self.btn_setting.raise_()

    def position_title_label(self):
        if not hasattr(self, "title_label"):
            return

        show_title = not config_.get("full_screen", True)
        self.title_label.setVisible(show_title)
        if not show_title:
            if hasattr(self, "btn_setting"):
                self.btn_setting.setGeometry(10, 10, 40, 40)
            return

        hint_geom = self.hint2.geometry()
        title_y = max(0, self.height() - hint_geom.y() - hint_geom.height())
        self.title_label.setGeometry(
            hint_geom.x(), title_y, hint_geom.width(), hint_geom.height())
        self.title_label.setFont(QFont("Microsoft JhengHei", self.primary_hint_font_size(), QFont.Bold))
        self.title_label.setAlignment(self.hint2.alignment())
        self.title_label.setStyleSheet(
            f"background-color: transparent; color: {self.primary_hint_text_color()};")
        self.title_label.raise_()
        if hasattr(self, "btn_setting"):
            self.btn_setting.setGeometry(10, 18, 40, 40)
            self.btn_setting.raise_()

    def setWindowTitle(self, title):
        super().setWindowTitle(title)
        if hasattr(self, "title_label"):
            self.title_label.setText(self.title_text_html(title))

    def title_text_html(self, title):
        return f'<html><head/><body><p align="center">&nbsp;<br/>{title}</p></body></html>'

    def win_resize(self, event):
        Proportion_X = self.width()/self.org_point[0][2]
        Proportion_Y = self.height()/self.org_point[0][0]
        chang = min(Proportion_X, Proportion_Y)

        img_base_h = self.org_point[1][0] * chang
        img_base_w = self.org_point[1][2] * chang
        hint2_base_h = self.org_point[2][0] * chang
        blank_X = max(0, (self.width() - img_base_w) // 2)
        blank_Y = max(0, (self.height() - img_base_h - hint2_base_h) // 2)

        hint2_y = int(self.org_point[2][3] * chang + blank_Y)
        hint2_h = int(hint2_base_h)
        title_y = max(0, self.height() - hint2_y - hint2_h)
        title_h = hint2_h
        camera_y = title_y + title_h
        camera_bottom = hint2_y
        camera_h = max(1, camera_bottom - camera_y)

        for i in range(1, len(self.obj)):
            height = self.org_point[i][0]*chang
            left_ = self.org_point[i][1]*chang
            width = self.org_point[i][2]*chang
            top = self.org_point[i][3]*chang
        
            new_top = int(top + blank_Y)
            new_height = int(height)
            if i == 1:
                new_top = camera_y
                new_height = camera_h
            elif i != 2:
                original_img_h = max(1, self.org_point[1][0])
                rel_top = self.org_point[i][3] / original_img_h
                rel_height = self.org_point[i][0] / original_img_h
                new_top = int(camera_y + rel_top * camera_h)
                new_height = int(max(1, rel_height * camera_h))
            self.obj[i].setGeometry(int(left_+blank_X), new_top, int(width), new_height)
        self.apply_primary_hint_style()
        self.position_title_label()
        self.position_version_label()
        pass

    def update_screen(self):
        desktop = QApplication.desktop()
        screen_count = desktop.screenCount()
        n = 2
        if config_["cameraIP"]["in_camera"] == config_["cameraIP"]["out_camera"]:
            n = 1
        elif config_["cameraIP"]["in_camera"] == "0" or config_["cameraIP"]["out_camera"] == "0":
            n = 1

        # [2026-04-25 Fix] 螢幕等待重試機制（非阻塞且攔截顯示）
        # 如果是雙螢幕配置且為「全螢幕模式」，才需要確保系統真的抓到 2 個獨立螢幕
        needs_retry = False
        if config_.get("full_screen", True) and n == 2:
            if screen_count < 2:
                needs_retry = True
            else:
                geom0 = desktop.screenGeometry(0)
                geom1 = desktop.screenGeometry(1)
                if geom0 == geom1:
                    needs_retry = True
        
        if needs_retry:
            if not hasattr(self, '_screen_retry_count'):
                self._screen_retry_count = 0
            if self._screen_retry_count < 40:
                self._screen_retry_count += 1
                print(f"[ScreenDetect] 等待正確的雙螢幕配置... ({self._screen_retry_count}/40)")
                QTimer.singleShot(2000, self.update_screen)
                return  # 在雙螢幕準備好之前，先不要顯示視窗，避免被作業系統強制綁定在同一個螢幕

        # 準備好後再進行排版
        if not config_.get("full_screen", True):
            avail_rect = desktop.availableGeometry(0)
            x_offset, y_offset = avail_rect.x(), avail_rect.y()
            w, h = avail_rect.width(), avail_rect.height()

            if n == 1:
                 self.setGeometry(x_offset, y_offset, w // 2, h)
            else:
                if self.frame_num == 0:
                    self.setGeometry(x_offset, y_offset, w // 2, h)
                elif self.frame_num == 1:
                    self.setGeometry(x_offset + (w // 2), y_offset, w // 2, h)
            
            self.showNormal()
        else:
            # 全螢幕模式
            if screen_count > 1:
                # 取得目標螢幕的完整解析度範圍並直接套用
                rect = desktop.screenGeometry(self.frame_num)
                self.setGeometry(rect)
            else:
                # 單螢幕分割模式
                helf_w = desktop.screenGeometry(0).width()
                helf_h = desktop.screenGeometry(0).height()
                if helf_h > helf_w:
                    self.setGeometry(0, self.frame_num * helf_h // 2, helf_w, helf_h // 2)
                else:
                    self.setGeometry(self.frame_num * helf_w // 2, 0, helf_w // 2, helf_h)

            if config_["full_screen"]:
                # 使用 showFullScreen() 替代 showMaximized() 避免 X11 的視窗管理器干擾位置
                self.showFullScreen()

class MyThread(QThread):
    signal_update_img = pyqtSignal(QLabel, QPixmap)
    signal_update_bgcolor = pyqtSignal(list, list)
    signal_update_visibility = pyqtSignal(list, bool)
    signal_update_hint = pyqtSignal(QLabel, str, str)
    signal_update_title = pyqtSignal(str)

    def __init__(self):
        super(MyThread, self).__init__()

    def run(self):
        pass
        """ while True:
            #print(current_time)
            #放置參數更新涵式
            time.sleep(0.001) """
        
