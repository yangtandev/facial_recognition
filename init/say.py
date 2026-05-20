#寫成函式
import time
from gtts import gTTS
import os, threading
import queue
import subprocess
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

                    # --- 使用系統 ffplay 播放 (取代不穩定的 mpg123) ---
                    # [2026-02-01 Fix] mpg123 segfaults in systemd environment (Code -11)
                    # ffplay is more robust.
                    with self.process_lock:
                        self.current_process = subprocess.Popen(
                            ['ffplay', '-nodisp', '-autoexit', '-hide_banner', full_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                    
                    # 等待播放完成或被殺死
                    self.current_process.wait()
                    
                except Exception as e:
                    LOGGER.error(f"播放進程發生錯誤: {e}")
                
                finally:
                    if token:
                        with self.status_lock: self.last_end_time[token] = time.time()
                    
                    # 釋放資源
                    with self.process_lock:
                        self.current_process = None

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
