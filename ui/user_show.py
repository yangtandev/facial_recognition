from PyQt5.QtWidgets import QWidget, QLabel
from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from ui.user import Ui_Show_from
from ui.dy_user import Ui_dynamic_Form
from ui.user_only import Ui_Form
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import QApplication, QPushButton, QInputDialog, QLineEdit, QMessageBox
from PyQt5 import QtCore
import os, json, time, subprocess, sys

def get_app_version():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    release_note = os.path.join(root, "release_note.md")
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
        
        if frame_num == 0:
            self.setWindowTitle(f"進入視窗")
        if frame_num == 1:
            self.setWindowTitle(f"離開視窗")

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
        self.version_label.setAlignment(QtCore.Qt.AlignCenter)
        self.version_label.setFont(self.hint2.font())
        self.version_label.setStyleSheet(self.hint2.styleSheet())
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
        # [2026-01-30 Fix] Use explicit QInputDialog to ensure centering
        dlg = QInputDialog(self)
        dlg.setWindowTitle('身分驗證')
        dlg.setLabelText('請輸入管理員密碼:')
        dlg.setTextEchoMode(QLineEdit.Password)
        
        # Force center on parent
        # Note: dlg.exec_() blocks, so we move before exec.
        # But dlg size might not be calculated yet.
        # We trust Qt parent centering usually, but if it fails (top-left),
        # we can try to force move.
        
        if dlg.exec_() == QInputDialog.Accepted:
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

    def position_version_label(self):
        if not hasattr(self, "version_label"):
            return
        img_rect = self.img1.geometry()
        hint_rect = self.hint2.geometry()
        bottom_gap = hint_rect.top() - (img_rect.top() + img_rect.height())
        available_h = img_rect.top() - bottom_gap
        label_h = min(hint_rect.height(), available_h)
        label_y = img_rect.top() - bottom_gap - label_h
        if label_h < 20 or label_y < 0:
            self.version_label.hide()
            return
        self.version_label.setGeometry(
            hint_rect.left(), int(label_y), hint_rect.width(), label_h)
        self.version_label.show()
        self.version_label.raise_()
        if hasattr(self, "btn_setting"):
            self.btn_setting.raise_()

    def win_resize(self, event):
        Proportion_X = self.width()/self.org_point[0][2]
        Proportion_Y = self.height()/self.org_point[0][0]
        blank_X = 0
        blank_Y = 0
        
        chang = min(Proportion_X, Proportion_Y)
        for i in range(1, len(self.obj)):
            height = self.org_point[i][0]*chang
            left_ = self.org_point[i][1]*chang
            width = self.org_point[i][2]*chang
            top = self.org_point[i][3]*chang
            if i == 1:
                height_hint2 = self.org_point[2][0]*chang
                blank_X = max(0, (self.width() - width )//2)
                blank_Y = max(0, (self.height() - height - height_hint2)//2)
        
            self.obj[i].setGeometry(int(left_+blank_X), int(top+blank_Y),  int(width), int(height))
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

    def __init__(self):
        super(MyThread, self).__init__()

    def run(self):
        pass
        """ while True:
            #print(current_time)
            #放置參數更新涵式
            time.sleep(0.001) """
        
