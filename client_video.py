# client_video.py
import cv2
import numpy as np
import time
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

FRAME_RATE = 20
JPEG_QUALITY = 40

class VideoWorker(QObject):
    finished = pyqtSignal()
    frame_captured = pyqtSignal(QImage)
    bytes_sent = pyqtSignal(int)

    def __init__(self, udp_socket, server_addr, username):
        super().__init__()
        self.udp_socket = udp_socket
        self.server_addr = server_addr
        self.username = username
        self.running = True
        self.is_muted = False
        self.placeholder_frame = self._create_placeholder()
        self.seq_num = 0
        self.cap = None

    def _create_placeholder(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        text = f"{self.username}"
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size = cv2.getTextSize(text, font, 1, 2)[0]
        text_x = (frame.shape[1] - text_size[0]) // 2
        text_y = (frame.shape[0] + text_size[1]) // 2
        cv2.putText(frame, text, (text_x, text_y), font, 1, (255, 255, 255), 2)
        return frame

    def run(self):
        try:
            # --- FIX: Use cv2.CAP_DSHOW to force DirectShow backend on Windows ---
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            
            if not self.cap.isOpened():
                print("Error: Cannot open camera. It may be in use or drivers are bad.")
                self.finished.emit()
                return
        except Exception as e:
            print(f"Error opening camera: {e}")
            self.finished.emit()
            return
            
        frame_interval = 1.0 / FRAME_RATE
        while self.running:
            start_time = time.time()
            frame_to_send = None

            if self.is_muted:
                frame_to_send = self.placeholder_frame
            else:
                try:
                    ret, frame = self.cap.read()
                    if ret:
                        frame_to_send = frame
                    else:
                        print("Warning: cap.read() failed.")
                        frame_to_send = self.placeholder_frame
                except Exception as e:
                    print(f"Error reading camera frame: {e}")
                    frame_to_send = self.placeholder_frame

            if frame_to_send is not None:
                try:
                    rgb_frame = cv2.cvtColor(frame_to_send, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_frame.shape
                    qt_image = QImage(rgb_frame.data, w, h, ch * w, QImage.Format.Format_RGB888)
                    self.frame_captured.emit(qt_image.copy())

                    _, encoded_frame = cv2.imencode('.jpg', frame_to_send, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                    self.seq_num = (self.seq_num + 1) % 65536
                    header = f"v:{self.username}:{self.seq_num}:".encode('utf-8')
                    packet = header + encoded_frame.tobytes()
                    self.udp_socket.sendto(packet, self.server_addr)
                    self.bytes_sent.emit(len(packet))
                except Exception as e:
                    print(f"Video send error: {e}")

            sleep_time = frame_interval - (time.time() - start_time)
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        if self.cap:
            self.cap.release()
        self.finished.emit()

    def stop(self): self.running = False