# client_screen.py
import time
import cv2
import numpy as np
import mss
from PyQt6.QtCore import QObject, pyqtSignal

class ScreenShareWorker(QObject):
    finished = pyqtSignal()
    bytes_sent = pyqtSignal(int)

    def __init__(self, tcp_socket):
        super().__init__()
        self.tcp_socket = tcp_socket
        self.running = True

    def run(self):
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] # Main monitor
                while self.running:
                    start_time = time.time()
                    img = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    
                    # Resize to save bandwidth, e.g., 1080p
                    frame_resized = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_AREA)
                    
                    _, encoded_frame = cv2.imencode('.jpg', frame_resized, [int(cv2.IMWRITE_JPEG_QUALITY), 50]) # Quality 50
                    
                    data = encoded_frame.tobytes()
                    # Prepend header: SCRN:<data_size>:
                    header = f"SCRN:{len(data)}:".encode('utf-8')
                    self.tcp_socket.sendall(header + data)
                    self.bytes_sent.emit(len(header) + len(data))
                    
                    # Aim for ~10 FPS
                    time.sleep(max(0, 0.1 - (time.time() - start_time))) 
        except Exception as e:
            print(f"Screen sharing send error: {e}")
        finally:
            self.finished.emit()

    def stop(self): self.running = False