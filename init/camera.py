import queue
import subprocess
import threading
import time
import cv2
import numpy as np
import os
import signal
from urllib.parse import urlparse

# --- Hardware Acceleration Auto-Detection ---

_best_decoder_info = {'name': 'cpu', 'checked': False}

def get_best_hw_accel():
    """
    Correctly detects VAAPI support under Ubuntu by checking -hwaccels.
    Caches the result to avoid re-checking.
    """
    if _best_decoder_info['checked']:
        return _best_decoder_info['name']

    _best_decoder_info['checked'] = True # Mark as checked
    try:
        print("Checking for available hardware acceleration methods...")
        # Check FFmpeg for VAAPI support in '-hwaccels', not '-decoders'
        hwaccels_output = subprocess.check_output(
            ['ffmpeg', '-hwaccels'],
            text=True,
            stderr=subprocess.DEVNULL
        )
        if 'vaapi' in hwaccels_output:
            print("✅ Found 'vaapi' hardware acceleration support.")
            _best_decoder_info['name'] = 'vaapi'
            return 'vaapi'

    except Exception as e:
        print(f"Warning: Could not check for hardware acceleration: {e}.")

    print("⚠️ No supported hardware acceleration found. Using CPU.")
    _best_decoder_info['name'] = 'cpu'
    return 'cpu'

def _ignore_sigint():
    """Function to be called in the child process to ignore SIGINT."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)

# --- VideoCapture Class ---

class VideoCapture:
    """
    Custom VideoCapture class using FFmpeg. It auto-detects and attempts to use 
    hardware acceleration, falling back to a robust MJPEG pipe if HW fails.
    """
    def __init__(self, rtsp_url, config_data=None):
        self.rtsp_url = rtsp_url
        self.q = queue.Queue(maxsize=2)
        self.stop_threads = False
        self.proc = None
        self.config = config_data or {}
        
        # Auto-detect the best hardware acceleration method on initialization
        self.hw_accel_method = get_best_hw_accel()
        
        self.width = None
        self.height = None
        
        self.thread = threading.Thread(target=self._reader_manager)
        self.thread.daemon = True
        self.thread.start()

    def _connect_timeout_us(self):
        try:
            seconds = float(self.config.get("camera_connect_timeout_sec", 5))
        except (TypeError, ValueError):
            seconds = 5.0
        return str(max(1, int(seconds * 1_000_000)))

    def _reconnect_delay(self):
        try:
            return max(1.0, float(self.config.get("camera_reconnect_delay_sec", 2)))
        except (TypeError, ValueError):
            return 2.0

    def _rtsp_input_options(self):
        return [
            '-rtsp_transport', 'tcp',
            '-rtsp_flags', 'prefer_tcp',
            '-timeout', self._connect_timeout_us(),
        ]

    def _get_video_info_for_raw_pipeline(self):
        """Uses ffprobe to get resolution needed for the raw video pipeline."""
        print("Probing video stream for resolution (for raw pipeline)...")
        try:
            command = [
                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0',
            ] + self._rtsp_input_options() + [self.rtsp_url]
            timeout = self.config.get("ffprobe_timeout") or 5  # [2026-04-24] Default 5s to prevent hang
            output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
            self.width, self.height = map(int, output.split('x'))
            print(f"Raw Pipeline: Detected resolution {self.width}x{self.height}.")
            return True
        except Exception as e:
            print(f"Raw Pipeline: ffprobe failed: {e}. Cannot use raw video pipeline.")
            return False

    def _start_raw_video_pipeline(self, use_hw_accel=False, error_tolerant=False):
        """Attempts to start a raw video pipeline, with optional HW accel and error tolerance."""
        pipeline_type = "HW Accel Raw" if use_hw_accel else "Err-Tolerant Raw"
        print(f"Attempting {pipeline_type} pipeline for {self.rtsp_url}...")

        if not self._get_video_info_for_raw_pipeline():
            return False # Cannot proceed without resolution

        if self.width is None or self.height is None:
            raise RuntimeError(f"Failed to determine stream resolution for {pipeline_type}.")

        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        
        # Use prefer_tcp flag, which is more robust for forcing TCP transport
        # [2026-01-14 Latency Fix] Combined flags to prevent overwriting
        fflags = []
        if error_tolerant:
            fflags.append('discardcorrupt')
            command.extend(['-err_detect', 'ignore_err'])
            
        if fflags:
            command.extend(['-fflags', '+'.join(fflags)])
            
        command.extend(self._rtsp_input_options())
        command.extend(['-flags', 'low_delay'])

        if use_hw_accel:
            # Add the appropriate hardware acceleration arguments if VAAPI is detected
            if self.hw_accel_method == 'vaapi':
                command.extend(['-hwaccel', 'vaapi'])
        # Software decode: let FFmpeg select the codec from the stream.
        # Some test/USB RTSP sources publish H264 while production cameras can be HEVC.
            
        command.extend([
            '-i', self.rtsp_url,
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-'
        ])

        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, preexec_fn=_ignore_sigint)
        
        frame_size = self.width * self.height * 3
        print(f"{pipeline_type}: FFmpeg process started. Reading {frame_size} byte raw frames ({self.width}x{self.height}).")

        while not self.stop_threads:
            in_bytes = self.proc.stdout.read(frame_size)
            if not in_bytes:
                print(f"{pipeline_type}: FFmpeg stdout pipe closed unexpectedly.")
                return False
            if len(in_bytes) != frame_size:
                print(f"{pipeline_type}: Warning: Incomplete frame (expected {frame_size}, got {len(in_bytes)}). Dropping.")
                continue

            frame = np.frombuffer(in_bytes, dtype=np.uint8).reshape((self.height, self.width, 3))
            if frame is not None:
                if self.q.full(): self.q.get_nowait()
                self.q.put(frame)
        
        return True # Loop exited because of stop_threads

    def _start_sw_mjpeg_pipeline(self):
        """Starts the robust but slower MJPEG software pipeline."""
        print(f"Starting software MJPEG pipeline for {self.rtsp_url}...")
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            *self._rtsp_input_options(),
            '-flags', 'low_delay', # [2026-01-13 Latency Fix]
            '-i', self.rtsp_url,
            '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '2', '-'
        ]
        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, preexec_fn=_ignore_sigint)
        
        image_buffer = bytearray()
        while not self.stop_threads:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                print("Software MJPEG: FFmpeg stdout pipe closed. Will attempt to reconnect.")
                return False

            image_buffer.extend(chunk)
            a, b = image_buffer.find(b'\xff\xd8'), image_buffer.find(b'\xff\xd9')
            if a != -1 and b != -1 and b > a:
                frame = cv2.imdecode(np.frombuffer(image_buffer[a:b+2], dtype=np.uint8), cv2.IMREAD_COLOR)
                image_buffer = image_buffer[b+2:]
                if frame is not None:
                    if self.q.full(): self.q.get_nowait()
                    self.q.put(frame)
        return True # Loop exited because of stop_threads

    def _reader_manager(self):
        """Manages the video pipeline, attempting HW accel first, then err-tolerant raw, then SW MJPEG."""
        while not self.stop_threads:
            pipeline_success = False
            try:
                # 1. Try Hardware Accelerated Raw Video Pipeline
                if self.hw_accel_method != 'cpu':
                    try:
                        print("Trying HW accelerated raw pipeline...")
                        pipeline_success = self._start_raw_video_pipeline(use_hw_accel=True)
                    except Exception as e:
                        print(f"HW accelerated raw pipeline failed: {e}. Falling back to software.")
                        pipeline_success = False
                
                # 2. If HW failed (or not available), try Error-Tolerant Raw Video Pipeline (Software)
                if not pipeline_success and not self.stop_threads:
                    try:
                        print("Trying error-tolerant raw pipeline...")
                        pipeline_success = self._start_raw_video_pipeline(use_hw_accel=False, error_tolerant=True)
                    except Exception as e:
                        print(f"Error-tolerant raw pipeline failed: {e}.")
                        pipeline_success = False

                # 3. If all above failed, fall back to robust Software MJPEG Pipeline
                if not pipeline_success and not self.stop_threads:
                    print("Trying software MJPEG pipeline (final fallback)...")
                    pipeline_success = self._start_sw_mjpeg_pipeline()

            except Exception as e:
                print(f"Unhandled error in reader manager: {e}")
            finally:
                if self.proc:
                    # Clean up the process if it's still running
                    if self.proc.poll() is None:
                        print(f"FFmpeg process {self.proc.pid} still running. Sending SIGTERM.")
                        self.proc.terminate()
                        try:
                            self.proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            print(f"FFmpeg process {self.proc.pid} did not terminate in time. Sending SIGKILL.")
                            self.proc.kill()
                    self.proc = None # Clear handle
            
            if not self.stop_threads:
                delay = self._reconnect_delay()
                print(f"Pipeline ended. Reconnecting in {delay:g} seconds...")
                time.sleep(delay)

    def read(self):
        """Retrieves the latest frame from the queue."""
        try:
            return self.q.get(timeout=2)
        except queue.Empty:
            return None

    def terminate(self):
        """Stops the reader thread and terminates the FFmpeg subprocess gracefully."""
        print(f"Terminating camera connection to {self.rtsp_url}...")
        self.stop_threads = True
        
        # [2026-01-19 Fix] Avoid race condition between terminate() and _reader_manager()
        # Instead of killing manually and waiting here, we send a signal to interrupt
        # the blocking read() in the thread, then wait for the thread to cleanup.
        
        # Capture the process object locally
        proc = self.proc
        
        # Send SIGTERM to interrupt ffmpeg immediately (breaking the blocking read)
        if proc and proc.poll() is None:
            try:
                print(f"Signal SIGTERM to FFmpeg {proc.pid} to interrupt stream...")
                proc.terminate()
            except Exception: pass
            
        # Wait for the manager thread to finish cleanup (it has the finally block)
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
            if self.thread.is_alive():
                print("Warning: Camera thread did not exit in time.")
                
        print(f"Camera connection for {self.rtsp_url} terminated.")
