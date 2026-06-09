import cv2
import mediapipe as mp
import mediapipe.python.solutions as mp_solutions
import numpy as np
from collections import deque
import json
import os

class MediaPipeHandler:
    """
    MediaPipe Face Mesh 封裝器，提供人臉偵測、關鍵點定位、以及視線 (Gaze) 檢查功能。
    """
    def __init__(self, static_image_mode=True, max_num_faces=1, min_detection_confidence=0.4):
        # 載入設定檔以取得動態 Pitch 門檻
        try:
            config_path = os.path.join(os.path.dirname(__file__), "../config.json")
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config.json in MediaPipeHandler: {e}")
            self.config = {}

        self.mp_face_mesh = mp_solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(
            static_image_mode=static_image_mode,
            max_num_faces=max_num_faces,
            refine_landmarks=True, 
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=0.5
        )
        
        # [2026-02-10 Feature] Pose Estimation for Vest ROI
        self.mp_pose = mp_solutions.pose
        self.pose_detector = self.mp_pose.Pose(
            static_image_mode=True, 
            model_complexity=1, 
            min_detection_confidence=0.5
        )
        
        # 關鍵點 Index 定義
        self.IDX_LEFT_EYE_IRIS = 468
        self.IDX_RIGHT_EYE_IRIS = 473
        self.IDX_NOSE_TIP = 1
        self.IDX_MOUTH_LEFT = 61
        self.IDX_MOUTH_RIGHT = 291
        self.IDX_LEFT_EYE_INNER = 133
        self.IDX_LEFT_EYE_OUTER = 33
        self.IDX_RIGHT_EYE_INNER = 362
        self.IDX_RIGHT_EYE_OUTER = 263
        self.IDX_RIGHT_EYE_UPPER = 159
        self.IDX_RIGHT_EYE_LOWER = 145
        self.IDX_LEFT_EYE_UPPER = 386
        self.IDX_LEFT_EYE_LOWER = 374

        self.gaze_history = deque(maxlen=5)

    def close(self):
        """Explicitly close the MediaPipe FaceMesh instance to release resources."""
        if hasattr(self, 'face_mesh') and self.face_mesh:
            self.face_mesh.close()
            self.face_mesh = None
        if hasattr(self, 'pose_detector') and self.pose_detector:
            self.pose_detector.close()
            self.pose_detector = None

    def detect_pose(self, image):
        """
        Detect full body pose landmarks.
        Returns: pose_results object (access .pose_landmarks)
        """
        if not isinstance(image, np.ndarray):
            image = np.array(image)
        
        # Ensure RGB
        if image.shape[2] == 3: # Check channels
             # Assuming input might be BGR if from OpenCV, MP needs RGB
             # However, main loop usually passes BGR to .detect() which converts it?
             # wait, detect() converts image? 
             # No, face_detector passes rgb_frame to detect().
             # So we assume input is ALREADY RGB if consistent with detect().
             pass
             
        # Actually, let's be safe. MediaPipe needs RGB.
        # But we don't know if input is RGB or BGR here easily without context.
        # Standardize: Assume input is RGB (like detect method).
        
        return self.pose_detector.process(image)

    def detect_mesh(self, image):
        """
        [2026-02-12 Feature] Returns raw 468-point landmarks for precision cropping.
        Returns: list of face_landmarks objects (access .landmark[idx].x/y)
        """
        if not isinstance(image, np.ndarray):
            image = np.array(image)
        
        # Ensure RGB (FaceMesh needs RGB)
        # Assuming input is BGR from OpenCV usually
        # But wait, self.face_mesh.process expects RGB.
        # Check detect() implementation: it calls self.face_mesh.process(image) directly.
        # In main.py, image passed to detect is typically BGR?
        # Let's check main.py: 
        # Line 651: img_pil = Image.open(...) -> convert('RGB') -> mp_handler.detect(img_np)
        # Line 202: original_frame (BGR) is used.
        # Wait, if detect() doesn't convert BGR->RGB, then face mesh running on BGR might be suboptimal but working?
        # Actually, MediaPipe documentation strongly says RGB.
        # Let's add explicit conversion here to be safe and correct.
        
        if image.shape[2] == 3:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        else:
            image_rgb = image
            
        results = self.face_mesh.process(image_rgb)
        
        if not results.multi_face_landmarks:
            return None
            
        return results.multi_face_landmarks

    def detect(self, image, landmarks=True):
        if not isinstance(image, np.ndarray):
            image = np.array(image)
        h, w, _ = image.shape
        self.last_h = h
        self.last_w = w
        results = self.face_mesh.process(image)
        self.last_results = results 
        
        if not results.multi_face_landmarks:
            return None, None, None
            
        all_boxes = []
        all_probs = []
        all_points = []
        for face_landmarks in results.multi_face_landmarks:
            coords = np.array([(lm.x * w, lm.y * h) for lm in face_landmarks.landmark])
            x_min, y_min = np.min(coords, axis=0)
            x_max, y_max = np.max(coords, axis=0)
            all_boxes.append([int(x_min), int(y_min), int(x_max), int(y_max)])
            all_probs.append(1.0)
            p = face_landmarks.landmark
            points_5 = np.array([
                [p[self.IDX_LEFT_EYE_OUTER].x * w, p[self.IDX_LEFT_EYE_OUTER].y * h],
                [p[self.IDX_RIGHT_EYE_OUTER].x * w, p[self.IDX_RIGHT_EYE_OUTER].y * h],
                [p[self.IDX_NOSE_TIP].x * w, p[self.IDX_NOSE_TIP].y * h],
                [p[self.IDX_MOUTH_LEFT].x * w, p[self.IDX_MOUTH_LEFT].y * h],
                [p[self.IDX_MOUTH_RIGHT].x * w, p[self.IDX_MOUTH_RIGHT].y * h]
            ])
            all_points.append(points_5)
        return np.array(all_boxes), np.array(all_probs), np.array(all_points)

    def get_last_mesh_points(self, index=0):
        if not hasattr(self, 'last_results') or not self.last_results.multi_face_landmarks:
            return None
        if index >= len(self.last_results.multi_face_landmarks):
            return None
        landmarks = self.last_results.multi_face_landmarks[index].landmark
        return np.array([(lm.x * self.last_w, lm.y * self.last_h) for lm in landmarks], dtype=np.float32)

    def _calculate_gaze_ratio(self, landmarks, w, h):
        p = landmarks.landmark
        def get_projection_ratio(idx_start, idx_end, idx_point):
            start = np.array([p[idx_start].x * w, p[idx_start].y * h])
            end   = np.array([p[idx_end].x * w,   p[idx_end].y * h])
            point = np.array([p[idx_point].x * w, p[idx_point].y * h])
            
            vec_line = end - start
            vec_point = point - start
            
            denom = np.dot(vec_line, vec_line)
            if denom == 0: return 0.5
            
            # Projection ratio
            return np.dot(vec_point, vec_line) / denom
        l_h = get_projection_ratio(self.IDX_LEFT_EYE_INNER, self.IDX_LEFT_EYE_OUTER, self.IDX_LEFT_EYE_IRIS)
        r_h = get_projection_ratio(self.IDX_RIGHT_EYE_INNER, self.IDX_RIGHT_EYE_OUTER, self.IDX_RIGHT_EYE_IRIS)
        l_v = get_projection_ratio(self.IDX_LEFT_EYE_UPPER, self.IDX_LEFT_EYE_LOWER, self.IDX_LEFT_EYE_IRIS)
        r_v = get_projection_ratio(self.IDX_RIGHT_EYE_UPPER, self.IDX_RIGHT_EYE_LOWER, self.IDX_RIGHT_EYE_IRIS)
        return (l_h + r_h)/2, l_h, r_h, (l_v + r_v)/2, l_v, r_v

    def _calculate_ear(self, landmarks, w, h):
        """
        計算眼睛張開度 (Eye Aspect Ratio)
        Use standard 6-point EAR approximation (or 4-point height/width if sufficient)
        """
        p = landmarks.landmark
        def dist(idx1, idx2):
            x1, y1 = p[idx1].x * w, p[idx1].y * h
            x2, y2 = p[idx2].x * w, p[idx2].y * h
            return np.sqrt((x1-x2)**2 + (y1-y2)**2)

        # Left Eye: 33-133 (Width), 159-145 (Height)
        l_h = dist(33, 133)
        l_v = dist(159, 145)
        l_ear = l_v / l_h if l_h > 0 else 0
        
        # Right Eye: 362-263 (Width), 386-374 (Height)
        r_h = dist(362, 263)
        r_v = dist(386, 374)
        r_ear = r_v / r_h if r_h > 0 else 0
        
        return (l_ear + r_ear) / 2

    def _get_head_pose_angles(self, landmarks, w, h):
        p = landmarks.landmark
        model_points = np.array([(0.0, 0.0, 0.0), (0.0, 330.0, -65.0), (-225.0, -170.0, -135.0), (225.0, -170.0, -135.0), (-150.0, 150.0, -125.0), (150.0, 150.0, -125.0)], dtype=np.float64)
        image_points = np.array([(p[1].x * w, p[1].y * h), (p[152].x * w, p[152].y * h), (p[33].x * w, p[33].y * h), (p[263].x * w, p[263].y * h), (p[61].x * w, p[61].y * h), (p[291].x * w, p[291].y * h)], dtype=np.float64)
        focal_length = w
        center = (w / 2, h / 2)
        camera_matrix = np.array([[focal_length, 0, center[0]], [0, focal_length, center[1]], [0, 0, 1]], dtype=np.float64)
        dist_coeffs = np.zeros((4, 1))
        success, rotation_vector, translation_vector = cv2.solvePnP(model_points, image_points, camera_matrix, dist_coeffs, flags=cv2.SOLVEPNP_ITERATIVE)
        if not success: return 0, 0, 0
        rmat, _ = cv2.Rodrigues(rotation_vector)
        sy = np.sqrt(rmat[0, 0] * rmat[0, 0] + rmat[1, 0] * rmat[1, 0])
        singular = sy < 1e-6
        if not singular:
            x = np.arctan2(rmat[2, 1], rmat[2, 2])
            y = np.arctan2(-rmat[2, 0], sy)
            z = np.arctan2(rmat[1, 0], rmat[0, 0])
        else:
            x = np.arctan2(-rmat[1, 2], rmat[1, 1]); y = np.arctan2(-rmat[2, 0], sy); z = 0
        def normalize_to_zero(angle):
            angle = angle % 360
            if angle > 180: angle -= 360
            if angle > 90: angle -= 180
            elif angle < -90: angle += 180
            return angle
        return normalize_to_zero(np.degrees(x)), normalize_to_zero(np.degrees(y)), normalize_to_zero(np.degrees(z))

    def check_gaze(self, index=0):
        if not hasattr(self, 'last_results') or not self.last_results.multi_face_landmarks:
            return True, "No Data", (0, 0, 0), 0.0
        landmarks = self.last_results.multi_face_landmarks[index]
        w, h = self.last_w, self.last_h
        
        # 1. 獲取基礎數值
        pitch, yaw, roll = self._get_head_pose_angles(landmarks, w, h)
        avg_h, l_h, r_h, avg_v, l_v, r_v = self._calculate_gaze_ratio(landmarks, w, h)
        self.gaze_history.append((avg_h, l_h, r_h, avg_v, l_v, r_v))
        s_avg_h, s_l_h, s_r_h, s_avg_v, s_l_v, s_r_v = np.mean(self.gaze_history, axis=0)
        
        # [2026-01-22 Fix] 新增 EAR 計算 (不直接過濾，改為回傳數值供後端判斷)
        ear = self._calculate_ear(landmarks, w, h)
        
        pose_tuple = (pitch, yaw, roll)

        # 取得動態 Pitch 門檻 (預設: 抬頭25, 低頭-15)
        pitch_up_limit = self.config.get("pitch_threshold", {}).get("up", 25)
        pitch_down_limit = self.config.get("pitch_threshold", {}).get("down", -15)

        # 1. 眼睛異常檢查 (閉眼/特徵崩潰) - 優先攔截
        # 正常張眼時 Avg V 約為 0.2~0.8。若 > 3.0 代表上下眼皮重疊導致數值爆炸。
        # [2026-01-18 Disabled by User Request]
        # if s_avg_v > 3.0:
        #     return False, "眼睛閉合", pose_tuple
        
        # [2026-01-22 Logic Change] 
        # 不在此處直接攔截 EAR < 0.22，避免誤殺天生小眼的重要長官 (如楊昌裕 EAR=0.10)。
        # 改為回傳 EAR 數值，由 model.py 進行「低 EAR 需高 Z-Score」的動態門檻判斷。
        
        # 2. 垂直判定 (Chin Policy) - 優先順序最高，攔截微小偏移
        # [2026-02-01 Enable] 恢復姿態過濾以攔截仰頭誤判 (如 09:23:30 案例 Pitch=31.6)
        if pitch > pitch_up_limit:
            return False, f"抬頭 (Pitch:{pitch:.1f} > {pitch_up_limit})", pose_tuple, ear
            
        if pitch < pitch_down_limit:
            return False, f"低頭 (Pitch:{pitch:.1f} < {pitch_down_limit})", pose_tuple, ear

        # 3. 側臉與歪頭判定 (Yaw/Roll)
        # [2026-04-14 Enable] 側臉過濾放寬至 28 度，避免誤殺 (Yaw > 28, Roll > 20)
        if abs(yaw) > 28 or abs(roll) > 20:
            return False, f"未正視鏡頭 (Yaw:{yaw:.1f}, Roll:{roll:.1f})", pose_tuple, ear

        gaze_diff = abs(l_h - r_h)

        # 4. 眼睛視線判定 (斜視 - 瞬時攔截)
        # [2026-05-26] Diff 0.20~0.23 can still be frontal and recognizable.
        if gaze_diff > 0.23 or not (0.25 < s_avg_h < 0.75):
            return False, f"斜視 (Diff: {gaze_diff:.2f})", pose_tuple, ear
            
        # [2026-01-26 New] 組合過濾 (Combo Check)
        # 針對灰色地帶：瞇眼 (EAR<0.10) 且 微斜視 (Diff>0.15) -> 視為特徵不穩
        if ear < 0.10 and gaze_diff > 0.15:
            return False, f"眼神不穩 (EAR:{ear:.2f} & Diff:{gaze_diff:.2f})", pose_tuple, ear
        
        # 回傳增加 ear 欄位
        return True, "Pass", pose_tuple, ear
