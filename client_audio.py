# client_audio.py
import pyaudio
from PyQt6.QtCore import QObject, pyqtSignal

class AudioWorker(QObject):
    finished = pyqtSignal()
    bytes_sent = pyqtSignal(int)

    def __init__(self, udp_socket, server_addr, username):
        super().__init__()
        self.udp_socket = udp_socket
        self.server_addr = server_addr
        self.username = username
        self.running = True
        self.is_muted = False
        self.p_audio = None
        self.stream = None

    def run(self):
        try:
            self.p_audio = pyaudio.PyAudio()
            self.stream = self.p_audio.open(format=pyaudio.paInt16, channels=1, rate=44100, input=True, frames_per_buffer=1024)
        except Exception as e:
            print(f"Error opening audio stream: {e}")
            self.finished.emit()
            return

        while self.running:
            try:
                data = self.stream.read(1024, exception_on_overflow=False)
                if not self.is_muted:
                    header = f"a:{self.username}:".encode('utf-8')
                    packet = header + data
                    self.udp_socket.sendto(packet, self.server_addr)
                    self.bytes_sent.emit(len(packet))
            except Exception as e:
                if self.running:
                    print(f"Audio send error: {e}")
                break
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        if self.p_audio:
            self.p_audio.terminate()
        self.finished.emit()

    def stop(self): self.running = False