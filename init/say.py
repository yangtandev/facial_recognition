#寫成函式
import time
from gtts import gTTS
import os, threading
import queue
import subprocess
import shutil
from init.log import LOGGER

class Say_:
    """
    語音播報類別，使用系統級 mpg123 播放器。
    解決在高 CPU 負載 (MediaPipe) 下 Pygame Mixer 造成的斷斷續續與 UI 凍結問題。
    """
    def __init__(self):
        self.queue = queue.Queue()
        self.generation = 0 
        self.gen_lock = threading.Lock()
        
        # 優先級狀態 (0=Idle, 1=High, 2=Normal)
        self.current_priority = 0
        self.priority_lock = threading.Lock()

        self.path = os.path.join(os.path.dirname(__file__), "../voice/")
        self.stop_threads = False
        self.playback_timeout_sec = 3.0
        self.playback_error_cooldown_sec = 5.0
        self.last_playback_failure_time = 0.0
        
        # 追蹤當前播放進程
        self.current_process = None
        self.process_lock = threading.Lock()

        # Token 播放狀態追蹤
        self.last_start_time = {} # {token: timestamp}
        self.last_end_time = {}   # {token: timestamp}
        self.status_lock = threading.Lock()

        self.last_queued_item = None 

        if not os.path.isdir(self.path):
            os.makedirs(self.path)

        th = threading.Thread(target=self.speak, name="speak")
        th.daemon = True
        th.start()

    def say(self, text, filename, priority=2, token=None):
        now = time.time()
        # 基礎去重
        if self.last_queued_item:
            last_text, last_time = self.last_queued_item
            if text == last_text and (now - last_time) < 1.2:
                return
        
        self.last_queued_item = (text, now)

        with self.priority_lock:
            # 決定是否播放
            is_busy = (self.current_priority != 0) or (not self.queue.empty())
            
            if priority == 2:
                if is_busy: return
                self.current_priority = 2
                self._enqueue(text, filename, 2, preempt=False, token=token)

            elif priority == 1:
                if self.current_priority == 1: return
                
                if self.current_priority == 2:
                    LOGGER.info(f"中斷提示語音，插播重要訊息: {text}")
                    self._bump_generation()
                    self._kill_current_process() # 強制停止外部播放器
                    self.current_priority = 1
                    self._enqueue(text, filename, 1, preempt=True, token=token)
                else:
                    self.current_priority = 1
                    self._enqueue(text, filename, 1, preempt=False, token=token)

    def _enqueue(self, text, filename, priority, preempt=False, token=None):
        if token:
            with self.status_lock:
                self.last_start_time[token] = time.time()
        
        with self.gen_lock:
            gen = self.generation
        self.queue.put((gen, text, filename, priority, preempt, token))

    def _bump_generation(self):
        with self.gen_lock:
            self.generation += 1

    def _kill_current_process(self):
        """殺死當前的 mpg123 進程"""
        with self.process_lock:
            if self.current_process and self.current_process.poll() is None:
                try:
                    self.current_process.terminate()
                    self.current_process.wait(timeout=0.2)
                except:
                    pass
                self.current_process = None

    def _fallback_voice_path(self, filename_base):
        for suffix in ("_in", "_out", "_clothes"):
            if filename_base.endswith(suffix):
                path = os.path.join(self.path, f"{suffix}.mp3")
                if os.path.isfile(path) and os.path.getsize(path) >= 100:
                    return path
        return None

    def _player_commands(self, full_path):
        commands = []
        if shutil.which("ffplay"):
            commands.append((
                "ffplay",
                ["ffplay", "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error", full_path],
            ))
        if shutil.which("mpg123"):
            commands.append(("mpg123", ["mpg123", "-q", full_path]))
        return commands

    def _play_file(self, full_path):
        now = time.time()
        if now - self.last_playback_failure_time < self.playback_error_cooldown_sec:
            LOGGER.warning(
                f"語音播放略過: 前次播放器失敗，冷卻 {self.playback_error_cooldown_sec:.0f}s 中")
            return False

        last_error = ""
        for player_name, cmd in self._player_commands(full_path):
            try:
                LOGGER.info(f"語音播放開始: {os.path.basename(full_path)} ({player_name})")
                with self.process_lock:
                    self.current_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )

                try:
                    _, stderr = self.current_process.communicate(
                        timeout=self.playback_timeout_sec)
                except subprocess.TimeoutExpired:
                    self._kill_current_process()
                    last_error = f"{player_name} timeout after {self.playback_timeout_sec:.1f}s"
                    LOGGER.warning(f"語音播放逾時，改試下一個播放器: {last_error}")
                    continue

                return_code = self.current_process.returncode
                if return_code == 0:
                    LOGGER.info(f"語音播放完成: {os.path.basename(full_path)} ({player_name})")
                    return True

                err_text = ""
                if stderr:
                    err_text = stderr.decode("utf-8", errors="ignore").strip().splitlines()
                    err_text = " | ".join(err_text[-3:])
                last_error = f"{player_name} exit={return_code} {err_text}".strip()
                LOGGER.warning(f"語音播放器失敗，改試下一個: {last_error}")
            except Exception as e:
                last_error = f"{player_name} exception: {e}"
                LOGGER.warning(f"語音播放器例外，改試下一個: {last_error}")
            finally:
                with self.process_lock:
                    self.current_process = None

        LOGGER.error(f"語音播放失敗，沒有可用播放器或音訊設備不可用: {last_error}")
        self.last_playback_failure_time = time.time()
        return False

    def speak(self):
        while not self.stop_threads:
            try:
                gen, text, filename_base, priority, preempt, token = self.queue.get(timeout=0.1)
                
                # 檢查版本
                with self.gen_lock:
                    if gen != self.generation:
                        if token:
                            with self.status_lock: self.last_end_time[token] = time.time()
                        if self.queue.empty():
                            with self.priority_lock: self.current_priority = 0
                        continue

                # 同步狀態
                with self.priority_lock:
                    self.current_priority = priority

                filename = filename_base + ".mp3"
                full_path = os.path.join(self.path, filename)
                
                try:
                    # Voice generation depends on the internet. Never do it in
                    # the playback path, or offline recognition can wait on gTTS
                    # for tens of seconds before speaking.
                    if not os.path.isfile(full_path):
                        fallback_path = self._fallback_voice_path(filename_base)
                        if fallback_path:
                            LOGGER.warning(
                                f"語音檔不存在，使用本地 fallback: {filename} -> {os.path.basename(fallback_path)}")
                            full_path = fallback_path
                        else:
                            LOGGER.warning(f"語音檔不存在且無 fallback，略過播放: {filename}")
                            continue

                    # 播放前再次檢查
                    with self.gen_lock:
                        if gen != self.generation: continue

                    if os.path.getsize(full_path) < 100: continue

                    self._play_file(full_path)
                    
                except Exception as e:
                    LOGGER.error(f"播放進程發生錯誤: {e}")
                
                finally:
                    if token:
                        with self.status_lock: self.last_end_time[token] = time.time()
                    
                    if self.queue.empty():
                        with self.priority_lock:
                            self.current_priority = 0
                    
            except queue.Empty:
                continue
            except Exception as e:
                LOGGER.error(f"Speak Thread Error: {e}")
                time.sleep(0.5)

    def is_busy(self):
        with self.process_lock:
            return self.current_process is not None and self.current_process.poll() is None

    def reset(self):
        """[2026-01-30 Fix] Reset speaker state on configuration reload."""
        self._kill_current_process()
        with self.queue.mutex:
            self.queue.queue.clear()
        with self.priority_lock:
            self.current_priority = 0
        self.last_queued_item = None
        LOGGER.info("Speaker state reset.")

    def terminate(self):
        self.stop_threads = True
        self._kill_current_process()
