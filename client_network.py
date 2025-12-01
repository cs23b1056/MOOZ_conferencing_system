# client_network.py
import time
import pyaudio
import threading
import cv2
import numpy as np
from collections import deque
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

JITTER_BUFFER_SIZE = 20

class ThreadSafeCounter:
    """A simple counter that is safe to use across multiple threads."""
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()
    def increment(self, amount):
        with self.lock:
            self.value += amount
    def get_and_reset(self):
        with self.lock:
            value = self.value
            self.value = 0
            return value

class MediaReceiver(QObject):
    """Listens on the UDP socket for video and audio packets."""
    video_frame_received = pyqtSignal(str, QImage)
    bytes_received = pyqtSignal(int)
    metrics_updated = pyqtSignal(str, int, float)

    def __init__(self, udp_socket):
        super().__init__()
        self.udp_socket = udp_socket
        self.running = True
        self.p_audio = pyaudio.PyAudio()
        self.playback_stream = self.p_audio.open(format=pyaudio.paInt16, channels=1, rate=44100, output=True, frames_per_buffer=1024)
        self.metrics = {} 

    def run(self):
        while self.running:
            try:
                data, _ = self.udp_socket.recvfrom(65536)
                self.bytes_received.emit(len(data))
                arrival_time = time.time()

                if data.startswith(b'v:'):
                    _, username_bytes, seq_num_bytes, payload = data.split(b':', 3)
                    username = username_bytes.decode('utf-8')
                    seq_num = int(seq_num_bytes)
                    
                    if username not in self.metrics:
                        self.metrics[username] = {'last_seq': seq_num - 1, 'lost': 0, 'total': 0, 'arrivals': deque(maxlen=JITTER_BUFFER_SIZE)}
                    stats = self.metrics[username]
                    stats['total'] += 1
                    lost_count = seq_num - stats['last_seq'] - 1
                    if lost_count > 0: stats['lost'] += lost_count
                    stats['last_seq'] = seq_num
                    loss_pct = (stats['lost'] / stats['total']) * 100 if stats['total'] > 0 else 0
                    stats['arrivals'].append(arrival_time)
                    jitter = 0
                    if len(stats['arrivals']) > 1:
                        inter_arrival = np.diff(list(stats['arrivals'])); jitter = np.std(inter_arrival) * 1000 # in ms
                    self.metrics_updated.emit(username, int(loss_pct), jitter)

                    frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w, ch = rgb_frame.shape
                        qt_image = QImage(rgb_frame.data, w, h, ch * w, QImage.Format.Format_RGB888)
                        self.video_frame_received.emit(username, qt_image.copy())
                
                elif data.startswith(b'a:'):
                    _, _, payload = data.split(b':', 2)
                    self.playback_stream.write(payload)
            except Exception:
                continue
    
    def stop(self): 
        self.running = False
        if self.playback_stream:
            self.playback_stream.stop_stream(); self.playback_stream.close()
        if self.p_audio:
            self.p_audio.terminate()

class TCPReceiver(QObject):
    """Listens on the TCP socket for text commands and binary data headers."""
    message_received = pyqtSignal(str)
    screen_share_started = pyqtSignal(str)
    screen_share_stopped = pyqtSignal(str)
    screen_frame_received = pyqtSignal(str, bytes)
    file_incoming = pyqtSignal(str, str, int)
    file_data_received = pyqtSignal(bytes)
    bytes_received = pyqtSignal(int)
    user_left = pyqtSignal(str)

    def __init__(self, tcp_socket):
        super().__init__()
        self.tcp_socket = tcp_socket
        self.running = True
        self.is_receiving_file = False

    def run(self):
        buffer = b""
        while self.running:
            try:
                data = self.tcp_socket.recv(4096)
                if not data: break
                self.bytes_received.emit(len(data))

                if self.is_receiving_file:
                    self.file_data_received.emit(data)
                    continue

                buffer += data
                while True:
                    if b':' not in buffer: break
                    header, _, rest = buffer.partition(b':')
                    try: command = header.decode('utf-8')
                    except UnicodeDecodeError: buffer = b""; break

                    if command in ("SYSTEM", "CHAT", "PONG", "SCRN_START", "SCRN_STOP", "FILE_INCOMING", "USER_LEFT"):
                        if b'\n' in rest:
                            payload, _, buffer = rest.partition(b'\n')
                            self.process_command(command, payload.decode('utf-8'))
                        else: break
                    elif command == "SCRN":
                        if b':' in rest:
                            username_bytes, _, data_rest = rest.partition(b':')
                            username = username_bytes.decode('utf-8')
                            if b':' in data_rest:
                                size_str, _, frame_data = data_rest.partition(b':')
                                try:
                                    frame_size = int(size_str)
                                    if len(frame_data) >= frame_size:
                                        self.screen_frame_received.emit(username, frame_data[:frame_size])
                                        buffer = frame_data[frame_size:]
                                    else: break
                                except (ValueError, UnicodeDecodeError): buffer = b""; break
                            else: break
                        else: break
                    else: buffer = b""; break
            except Exception as e:
                if self.running: print(f"TCP Receive Error: {e}")
                break
    
    def process_command(self, command, payload):
        if command == "SCRN_START": self.screen_share_started.emit(payload)
        elif command == "SCRN_STOP": self.screen_share_stopped.emit(payload)
        elif command == "FILE_INCOMING":
            from_user, filename, filesize = payload.split(':', 2)
            self.file_incoming.emit(from_user, filename, int(filesize))
            self.is_receiving_file = True
        elif command == "USER_LEFT":
            self.user_left.emit(payload)
        else:
            self.message_received.emit(f"{command}:{payload}")

    def stop_file_receive_mode(self): self.is_receiving_file = False
    def stop(self): self.running = False

class FileReceiverWorker(QObject):
    """Handles writing an incoming file to disk."""
    progress = pyqtSignal(int)
    finished = pyqtSignal(str)

    def __init__(self, tcp_receiver, save_path, filesize):
        super().__init__()
        self.tcp_receiver = tcp_receiver
        self.save_path = save_path
        self.filesize = filesize
        self.bytes_received = 0
        self.file = None

    def run(self):
        try:
            self.file = open(self.save_path, "wb")
            self.tcp_receiver.file_data_received.connect(self.write_chunk)
        except Exception as e:
            self.finish(f"Error opening file: {e}")
    
    def write_chunk(self, chunk):
        try:
            bytes_to_write = min(len(chunk), self.filesize - self.bytes_received)
            self.file.write(chunk[:bytes_to_write])
            self.bytes_received += bytes_to_write
            percentage = (self.bytes_received / self.filesize) * 100 if self.filesize > 0 else 100
            self.progress.emit(int(percentage))
            if self.bytes_received >= self.filesize:
                self.finish("File received successfully!")
        except Exception as e:
            self.finish(f"Error receiving file: {e}")
    
    def finish(self, message):
        if self.file: self.file.close()
        self.tcp_receiver.file_data_received.disconnect(self.write_chunk)
        self.tcp_receiver.stop_file_receive_mode()
        self.finished.emit(message)

class FileSenderWorker(QObject):
    """Handles reading a file from disk and sending it over TCP."""
    finished = pyqtSignal(str)
    bytes_sent = pyqtSignal(int)

    def __init__(self, tcp_socket, filepath):
        super().__init__()
        self.tcp_socket = tcp_socket
        self.filepath = filepath

    def run(self):
        try:
            with open(self.filepath, "rb") as f:
                while chunk := f.read(4096):
                    self.tcp_socket.sendall(chunk)
                    self.bytes_sent.emit(len(chunk))
            self.finished.emit("File sent successfully.")
        except Exception as e:
            self.finished.emit(f"Error sending file: {e}")