import json
import os

# 預設的設定資料
default_config = {
    "ip_set": {
        "ip_address": "127.0.0.1",
        "ip_gateway": "192.168.31.254",
        "ip_mask": "255.255.255.0"
    },
    "cameraIP": {
        "in_camera": "rtsp://192.168.50.71",
        "out_camera": "rtsp://192.168.50.71"
    },
    "camera_default_width": 1920,
    "camera_default_height": 1080,
    "ffprobe_timeout": None,
    "Server": {
        "ip": "43.213.128.240",
        "username": "ubuntu",
        "ssh_key_path": "/home/ubuntu/.ssh/id_rsa",
        "face_data_dir": "/home/ubuntu/pvms-api/media",
        "API_url": "https://demosite.api.ginibio.com/api/v1",
        "location_ID": 1
    },
    "say": {
        "in": "請進入",
        "out": "請離開",
        "clothes": "請正確著裝",
        "hint_eye_occluded": "眼部遮擋",
        "hint_nose_occluded": "鼻子被遮擋",
        "hint_mouth_occluded": "嘴巴被遮擋",
        "hint_nose_mouth_occluded": "口鼻被遮擋"
    },
    "inCamera": {
        "close": False,
        "min_face": 300
    },
    "outCamera": {
        "close": False,
        "min_face": 300
    },
    "door": "0",
    "Schedule": {
        "enabled": False,
        "in_periods": [
            {"start": "06:00", "end": "12:00"},
            {"start": "13:00", "end": "17:00"}
        ]
    },
    "Clothes_detection": False,
    "Clothes_show": False,
    "min_face": 450,
    "max_face": 700,
    "test_mod": False,
    "auto_open": True,
    "full_screen": False,
    "excel_api_enabled": False,
    "qrcode_mode": False,
    "theme": "dark",
    "Long_distance_mode": False
}


def merge_config(default, current):
    """合併 default 與 current，回傳更新後的 config"""
    updated = {}
    for key, value in default.items():
        if key in current:
            if isinstance(value, dict):
                updated[key] = merge_config(value, current[key])
            else:
                updated[key] = current[key]
        else:
            updated[key] = value  # 新增缺少的 key

    return updated  # 移除多餘的 key（不包含於 default）


def update_config_file(config_path='config.json'):
    if not os.path.exists(config_path):
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)
        print(f"已建立預設設定檔: {config_path}")
        return

    # 讀取現有 config 並與 default 比對
    with open(config_path, 'r', encoding='utf-8') as f:
        current_config = json.load(f)

    updated_config = merge_config(default_config, current_config)

    # 檢查是否有多餘的 key 並移除
    def prune_extra_keys(default, current):
        if isinstance(default, dict):
            return {k: prune_extra_keys(default[k], current[k]) for k in default if k in current}
        else:
            return current

    final_config = prune_extra_keys(default_config, updated_config)

    # 寫回更新後的 config
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(final_config, f, ensure_ascii=False, indent=2)
    print(f"已更新設定檔: {config_path}")


if __name__ == "__main__":
    # 使用範例
    update_config_file("config.json")
