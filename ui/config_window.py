import sys
import os
import json
import subprocess
import signal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel,
    QLineEdit, QPushButton, QCheckBox, QFormLayout,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QGroupBox, QSpinBox, QDoubleSpinBox, QApplication, QComboBox
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont, QIcon
from ui import styles

# 設定檔路徑
CONFIG_PATH = os.path.join(os.path.dirname(
    os.path.dirname(__file__)), "config.json")

# 全域狀態旗標，供 setting_tool.py 的 Watchdog 讀取
IS_RESTARTING = False


class ConfigWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("系統參數設定 (System Configuration)")
        self.resize(1000, 700)
        self.config_data = {}

        # UI 初始化
        self.init_ui()

        # 載入設定
        self.load_config()

    def init_ui(self):
        # 主佈局
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # 標題
        title_label = QLabel("Face System 設定工具")
        title_label.setFont(QFont("Microsoft JhengHei", 16, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)

        # 頁籤容器
        self.tabs = QTabWidget()
        self.tabs.setFont(QFont("Microsoft JhengHei", 11))
        main_layout.addWidget(self.tabs)

        # 初始化各分頁
        self.tab_basic = QWidget()
        self.tab_server = QWidget()
        self.tab_recognition = QWidget()
        self.tab_schedule = QWidget()
        self.tab_voice = QWidget()
        self.tab_network = QWidget()

        self.tabs.addTab(self.tab_basic, "一般設定")
        self.tabs.addTab(self.tab_server, "伺服器")
        self.tabs.addTab(self.tab_recognition, "辨識參數")
        self.tabs.addTab(self.tab_schedule, "排程 (Schedule)")
        self.tabs.addTab(self.tab_voice, "語音")
        self.tabs.addTab(self.tab_network, "網路/進階")

        # 構建各頁面內容
        self.init_tab_basic()
        self.init_tab_server()
        self.init_tab_recognition()
        self.init_tab_schedule()
        self.init_tab_voice()
        self.init_tab_network()

        # 底部按鈕區
        btn_layout = QHBoxLayout()
        self.btn_save = QPushButton("儲存設定 (Save)")
        self.btn_save.setFixedHeight(50)
        self.btn_save.setFont(QFont("Microsoft JhengHei", 12, QFont.Bold))
        self.btn_save.setStyleSheet(
            "background-color: #4CAF50; color: white; border-radius: 5px;")
        self.btn_save.clicked.connect(self.save_config)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setFixedHeight(50)
        self.btn_cancel.setFont(QFont("Microsoft JhengHei", 12))
        self.btn_cancel.clicked.connect(self.close)

        btn_layout.addStretch(1)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_save)
        main_layout.addLayout(btn_layout)

    def create_form_group(self, title):
        group = QGroupBox(title)
        group.setFont(QFont("Microsoft JhengHei", 11))
        layout = QFormLayout()
        group.setLayout(layout)
        return group, layout

    def init_tab_basic(self):
        # 使用滾動區域以支援更多內容
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        container = QWidget()
        layout = QVBoxLayout()
        container.setLayout(layout)
        scroll_area.setWidget(container)
        
        self.tab_basic.setLayout(QVBoxLayout())
        self.tab_basic.layout().addWidget(scroll_area)

        # 攝影機設定
        group_cam, form_cam = self.create_form_group("攝影機 RTSP 串流")
        self.in_camera_edit = QLineEdit()
        self.out_camera_edit = QLineEdit()
        form_cam.addRow("入口攝影機 (In):", self.in_camera_edit)
        form_cam.addRow("出口攝影機 (Out):", self.out_camera_edit)
        layout.addWidget(group_cam)

        # (解析度現在由 Qt 自動偵測，不再需要手動輸入)

        # 介面風格
        group_ui, form_ui = self.create_form_group("介面風格 (Appearance)")
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["暗色 (Dark)", "亮色 (Light)"])
        # Connect change event to preview theme (optional, maybe safe)
        # self.combo_theme.currentIndexChanged.connect(self.preview_theme)
        form_ui.addRow("主題 (Theme):", self.combo_theme)
        layout.addWidget(group_ui)

        # 功能開關
        group_feat, form_feat = self.create_form_group("功能開關")
        self.chk_clothes = QCheckBox("啟用服裝/安全帽偵測 (Clothes Detection)")
        self.chk_clothes_show = QCheckBox("顯示服裝偵測框 (Show Clothes Box)")
        self.chk_long_distance = QCheckBox("啟用遠距服裝辨識 (Clothes Zoom Mode)")
        self.chk_qrcode = QCheckBox(
            "啟用 QR Code 掃描 (QR Code Mode)")  # [2026-02-04 Feature]
        self.chk_full_screen = QCheckBox("全螢幕模式 (Full Screen)")
        self.chk_auto_open = QCheckBox("開機自動啟動 (Auto Open)")

        form_feat.addRow(self.chk_clothes)
        form_feat.addRow(self.chk_clothes_show)
        form_feat.addRow(self.chk_long_distance)
        form_feat.addRow(self.chk_qrcode)
        form_feat.addRow(self.chk_full_screen)
        form_feat.addRow(self.chk_auto_open)
        layout.addWidget(group_feat)
        layout.addStretch(1)

    def init_tab_server(self):
        layout = QVBoxLayout()
        self.tab_server.setLayout(layout)

        group, form = self.create_form_group("API 伺服器設定")

        self.srv_ip = QLineEdit()
        self.srv_user = QLineEdit()
        # 注意：config.json 中沒有 password 欄位? set_form.py 有用到，需確認 json
        self.srv_pass = QLineEdit()
        # 檢查 config.json 結構，Server 區塊有 password 嗎？ 之前的 read output 沒有看到。
        # 假設沒有 password，先保留欄位但可能不讀寫，或者如果 json 沒有就不存。

        self.srv_api_url = QLineEdit()
        self.srv_loc_id = QSpinBox()
        self.srv_loc_id.setRange(0, 999999)
        self.srv_face_dir = QLineEdit()

        form.addRow("伺服器 IP (Host):", self.srv_ip)
        form.addRow("使用者名稱 (SSH User):", self.srv_user)
        # form.addRow("密碼 (Password):", self.srv_pass) # 暫時註解，除非確認 config 有此欄位
        form.addRow("API URL:", self.srv_api_url)
        form.addRow("Location ID:", self.srv_loc_id)
        form.addRow("人臉資料目錄 (Server Path):", self.srv_face_dir)

        layout.addWidget(group)
        layout.addStretch(1)

    def init_tab_recognition(self):
        layout = QVBoxLayout()
        self.tab_recognition.setLayout(layout)

        group_face, form_face = self.create_form_group("人臉辨識參數")

        # 最小人臉 (In/Out 分開)
        self.min_face_in = QSpinBox()
        self.min_face_in.setRange(50, 1000)
        self.min_face_in.setSuffix(" px")

        self.min_face_out = QSpinBox()
        self.min_face_out.setRange(50, 1000)
        self.min_face_out.setSuffix(" px")

        self.min_face_global = QSpinBox()  # 全域 fallback
        self.min_face_global.setRange(50, 1000)
        self.min_face_global.setSuffix(" px")

        self.max_face = QSpinBox()
        self.max_face.setRange(200, 2000)
        self.max_face.setSuffix(" px")

        self.chk_debug = QCheckBox("除錯模式 (Debug Mode)")

        form_face.addRow("最小人臉 (全域預設):", self.min_face_global)
        form_face.addRow("最小人臉 (入口 In):", self.min_face_in)
        form_face.addRow("最小人臉 (出口 Out):", self.min_face_out)
        form_face.addRow("最大人臉 (過近提示):", self.max_face)
        form_face.addRow("", self.chk_debug)

        layout.addWidget(group_face)
        layout.addStretch(1)

    def init_tab_schedule(self):
        layout = QVBoxLayout()
        self.tab_schedule.setLayout(layout)

        # 說明
        info = QLabel(
            "說明：當啟用排程時，單一攝影機將依據下列時段強制切換為「入口 (In)」模式。\n不在列表中的時段將預設為「出口 (Out)」。")
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        # 開關
        self.chk_schedule_enable = QCheckBox("啟用排程控制 (Enable Schedule)")
        layout.addWidget(self.chk_schedule_enable)

        # 列表
        self.table_schedule = QTableWidget()
        self.table_schedule.setColumnCount(2)
        self.table_schedule.setHorizontalHeaderLabels(
            ["開始時間 (Start)", "結束時間 (End)"])
        self.table_schedule.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table_schedule)

        # 操作按鈕
        btn_box = QHBoxLayout()
        self.btn_add_period = QPushButton("新增時段")
        self.btn_del_period = QPushButton("刪除選取")
        self.btn_add_period.clicked.connect(self.add_period_row)
        self.btn_del_period.clicked.connect(self.del_period_row)

        btn_box.addWidget(self.btn_add_period)
        btn_box.addWidget(self.btn_del_period)
        layout.addLayout(btn_box)

    def add_period_row(self, start="06:00", end="12:00"):
        row = self.table_schedule.rowCount()
        self.table_schedule.insertRow(row)
        self.table_schedule.setItem(row, 0, QTableWidgetItem(start))
        self.table_schedule.setItem(row, 1, QTableWidgetItem(end))

    def del_period_row(self):
        row = self.table_schedule.currentRow()
        if row >= 0:
            self.table_schedule.removeRow(row)

    def init_tab_voice(self):
        layout = QVBoxLayout()
        self.tab_voice.setLayout(layout)

        group, form = self.create_form_group("語音播報內容 (TTS Text)")

        self.voice_in = QLineEdit()
        self.voice_out = QLineEdit()
        self.voice_clothes = QLineEdit()

        form.addRow("進入 (In):", self.voice_in)
        form.addRow("離開 (Out):", self.voice_out)
        form.addRow("服裝提示:", self.voice_clothes)

        layout.addWidget(group)
        layout.addStretch(1)

    def init_tab_network(self):
        layout = QVBoxLayout()
        self.tab_network.setLayout(layout)

        # 本機 IP
        group_ip, form_ip = self.create_form_group("本機網路設定 (Local IP)")
        self.local_ip = QLineEdit()
        self.local_mask = QLineEdit()
        self.local_gateway = QLineEdit()
        self.btn_set_ip = QPushButton("套用 IP 設定至系統")
        self.btn_set_ip.clicked.connect(self.apply_system_ip)

        form_ip.addRow("IP 位址:", self.local_ip)
        form_ip.addRow("子網路遮罩:", self.local_mask)
        form_ip.addRow("預設閘道:", self.local_gateway)
        form_ip.addRow("", self.btn_set_ip)
        layout.addWidget(group_ip)

        # 進階
        group_adv, form_adv = self.create_form_group("進階整合")
        self.door_api = QLineEdit()
        self.door_api.setPlaceholderText("例: 192.168.0.100 (輸入 0 表示不啟用)")
        self.chk_excel = QCheckBox("啟用 Excel API")

        door_hint = QLabel("系統將自動組成 http://{IP}:1880/open_door")
        door_hint.setStyleSheet("color: gray; font-size: 10px;")

        form_adv.addRow("開門裝置 IP:", self.door_api)
        form_adv.addRow("", door_hint)
        form_adv.addRow(self.chk_excel)
        layout.addWidget(group_adv)
        layout.addStretch(1)

    def load_config(self):
        if not os.path.exists(CONFIG_PATH):
            QMessageBox.critical(self, "錯誤", f"找不到設定檔: {CONFIG_PATH}")
            return

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.config_data = json.load(f)

            cfg = self.config_data

            # Tab 1: Basic
            self.in_camera_edit.setText(
                cfg.get("cameraIP", {}).get("in_camera", ""))
            self.out_camera_edit.setText(
                cfg.get("cameraIP", {}).get("out_camera", ""))

            # (解析度現在由 Qt 自動偵測)

            theme = cfg.get("theme", "dark")
            idx = 0 if theme == "dark" else 1
            self.combo_theme.setCurrentIndex(idx)

            self.chk_clothes.setChecked(cfg.get("Clothes_detection", False))
            self.chk_clothes_show.setChecked(cfg.get("Clothes_show", False))
            self.chk_long_distance.setChecked(cfg.get("Long_distance_mode", False))
            self.chk_qrcode.setChecked(
                cfg.get("qrcode_mode", False))  # [2026-02-04 Feature]
            self.chk_full_screen.setChecked(cfg.get("full_screen", False))
            self.chk_auto_open.setChecked(cfg.get("auto_open", False))

            # Tab 2: Server
            srv = cfg.get("Server", {})
            self.srv_ip.setText(srv.get("ip", ""))
            self.srv_user.setText(srv.get("username", ""))
            # self.srv_pass.setText(srv.get("password", ""))
            self.srv_api_url.setText(srv.get("API_url", ""))
            self.srv_loc_id.setValue(int(srv.get("location_ID", 1)))
            self.srv_face_dir.setText(srv.get("face_data_dir", ""))

            # Tab 3: Recognition
            self.min_face_global.setValue(int(cfg.get("min_face", 100)))
            self.max_face.setValue(int(cfg.get("max_face", 700)))
            self.chk_debug.setChecked(cfg.get("test_mod", False))

            # min_face for in/out (Fallback to global if not set)
            inc = cfg.get("inCamera", {})
            outc = cfg.get("outCamera", {})
            self.min_face_in.setValue(
                int(inc.get("min_face", self.min_face_global.value())))
            self.min_face_out.setValue(
                int(outc.get("min_face", self.min_face_global.value())))

            # Tab 4: Schedule (Multi-period)
            sched = cfg.get("Schedule", {})
            self.chk_schedule_enable.setChecked(sched.get("enabled", False))

            self.table_schedule.setRowCount(0)
            periods = sched.get("in_periods", [])
            # Fallback for old single period
            if not periods and "in_start" in sched:
                periods = [
                    {"start": sched["in_start"], "end": sched["in_end"]}]

            for p in periods:
                self.add_period_row(p.get("start", "00:00"),
                                    p.get("end", "00:00"))

            # Tab 5: Voice
            say = cfg.get("say", {})
            self.voice_in.setText(say.get("in", ""))
            self.voice_out.setText(say.get("out", ""))
            self.voice_clothes.setText(say.get("clothes", ""))

            # Tab 6: Network
            ip_set = cfg.get("ip_set", {})
            self.local_ip.setText(ip_set.get("ip_address", ""))
            self.local_mask.setText(ip_set.get("ip_mask", ""))
            self.local_gateway.setText(ip_set.get("ip_gateway", ""))

            door_raw = str(cfg.get("door", "0"))
            # 向下相容：若存的是完整 URL，自動擷取 IP
            if door_raw.startswith("http"):
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(door_raw)
                    door_raw = parsed.hostname or "0"
                except Exception:
                    pass
            self.door_api.setText(door_raw)
            self.chk_excel.setChecked(cfg.get("excel_api_enabled", False))

        except Exception as e:
            QMessageBox.warning(self, "警告", f"讀取設定檔時發生錯誤:\n{e}")

    def save_config(self):
        try:
            cfg = self.config_data

            # Basic
            cfg["cameraIP"]["in_camera"] = self.in_camera_edit.text().strip()
            cfg["cameraIP"]["out_camera"] = self.out_camera_edit.text().strip()

            # (解析度現在由 Qt 自動偵測)

            theme_idx = self.combo_theme.currentIndex()
            cfg["theme"] = "dark" if theme_idx == 0 else "light"

            cfg["Clothes_detection"] = self.chk_clothes.isChecked()
            cfg["Clothes_show"] = self.chk_clothes_show.isChecked()
            cfg["Long_distance_mode"] = self.chk_long_distance.isChecked()
            # [2026-02-04 Feature]
            cfg["qrcode_mode"] = self.chk_qrcode.isChecked()
            cfg["full_screen"] = self.chk_full_screen.isChecked()
            cfg["auto_open"] = self.chk_auto_open.isChecked()

            # Server
            if "Server" not in cfg:
                cfg["Server"] = {}
            cfg["Server"]["ip"] = self.srv_ip.text()
            cfg["Server"]["username"] = self.srv_user.text()
            cfg["Server"]["API_url"] = self.srv_api_url.text()
            cfg["Server"]["location_ID"] = self.srv_loc_id.value()
            cfg["Server"]["face_data_dir"] = self.srv_face_dir.text()

            # Recognition
            cfg["min_face"] = self.min_face_global.value()
            cfg["max_face"] = self.max_face.value()
            cfg["test_mod"] = self.chk_debug.isChecked()

            if "inCamera" not in cfg:
                cfg["inCamera"] = {}
            if "outCamera" not in cfg:
                cfg["outCamera"] = {}
            cfg["inCamera"]["min_face"] = self.min_face_in.value()
            cfg["outCamera"]["min_face"] = self.min_face_out.value()

            # Schedule
            if "Schedule" not in cfg:
                cfg["Schedule"] = {}
            cfg["Schedule"]["enabled"] = self.chk_schedule_enable.isChecked()

            periods = []
            for r in range(self.table_schedule.rowCount()):
                start = self.table_schedule.item(r, 0).text()
                end = self.table_schedule.item(r, 1).text()
                periods.append({"start": start, "end": end})

            cfg["Schedule"]["in_periods"] = periods
            # Clear old keys to avoid confusion
            if "in_start" in cfg["Schedule"]:
                del cfg["Schedule"]["in_start"]
            if "in_end" in cfg["Schedule"]:
                del cfg["Schedule"]["in_end"]

            # Voice
            if "say" not in cfg:
                cfg["say"] = {}
            cfg["say"]["in"] = self.voice_in.text()
            cfg["say"]["out"] = self.voice_out.text()
            cfg["say"]["clothes"] = self.voice_clothes.text()

            # Network
            if "ip_set" not in cfg:
                cfg["ip_set"] = {}
            cfg["ip_set"]["ip_address"] = self.local_ip.text()
            cfg["ip_set"]["ip_mask"] = self.local_mask.text()
            cfg["ip_set"]["ip_gateway"] = self.local_gateway.text()

            cfg["door"] = self.door_api.text()
            cfg["excel_api_enabled"] = self.chk_excel.isChecked()

            # Write to file
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)

            # 手動觸發 service 更新（如果螢幕配置有變更）
            # (已移除，因為 service 不再依賴 config_window)

            # Auto restart
            self.restart_system()

        except Exception as e:
            QMessageBox.critical(self, "儲存失敗", str(e))

    def apply_system_ip(self):
        # 呼叫外部 ip_set 模組 (需確保該模組存在)
        try:
            from setting.ip_set import set_ip
            ip = self.local_ip.text()
            mask = self.local_mask.text()
            gw = self.local_gateway.text()

            ret = QMessageBox.question(
                self, "確認", f"即將修改本機 IP 為 {ip}，這可能會導致網路中斷。\n確定執行？")
            if ret == QMessageBox.Yes:
                re = set_ip(ip, mask, gw)
                if int(re) == 0:
                    QMessageBox.information(self, "成功", "IP 修改指令已發送。")
                else:
                    QMessageBox.warning(self, "失敗", f"IP 修改失敗，代碼: {re}")
        except ImportError:
            QMessageBox.warning(self, "錯誤", "找不到 setting.ip_set 模組")
        except Exception as e:
            QMessageBox.critical(self, "錯誤", str(e))

    def restart_system(self):
        QMessageBox.information(self, "重啟中", "設定已儲存。\n系統正在重啟以套用變更...")

        global IS_RESTARTING
        IS_RESTARTING = True

        # [2026-01-30 Fix] Soft Reload via SIGHUP
        try:
            # 使用 pkill 發送 SIGHUP 訊號給 main.py
            # -f: match command line
            res = subprocess.run(["pkill", "-HUP", "-f", "main.py"])

            if res.returncode != 0:
                QMessageBox.warning(
                    self, "警告", "找不到正在運行的主程式 (main.py)。\n請確認主程式是否已啟動。")

        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"發送訊號失敗: {e}")

        sys.exit(0)


    def closeEvent(self, event):
        """Ensure application quits when window is closed."""
        QApplication.quit()
        event.accept()


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)

    # 全域樣式 (Dark Theme 雛形)
    app.setStyleSheet("""
        * { font-family: 'Noto Sans CJK TC', 'Microsoft JhengHei', sans-serif; }
        QWidget { background-color: #2b2b2b; color: #ffffff; }
        QLineEdit { background-color: #3b3b3b; border: 1px solid #555; padding: 5px; color: white; }
        QTableWidget { background-color: #3b3b3b; gridline-color: #555; color: white; }
        QHeaderView::section { background-color: #444; padding: 4px; border: 1px solid #555; color: white; }
        QTabWidget::pane { border: 1px solid #444; }
        QTabBar::tab { background: #3b3b3b; color: #aaa; padding: 8px 20px; }
        QTabBar::tab:selected { background: #505050; color: white; }
        QGroupBox { border: 1px solid #555; margin-top: 20px; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top center; padding: 0 10px; }
    """)

    window = ConfigWindow()
    window.show()
    sys.exit(app.exec_())
