# server.py
import sys
import socket
import threading
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QTextEdit, QLabel
from PyQt6.QtCore import QObject, QThread, pyqtSignal

# --- Server Configuration ---
HOST = '0.0.0.0'
TCP_PORT = 5000
VIDEO_UDP_PORT = 5001
AUDIO_UDP_PORT = 5002

# --- Server Logic (emits signals for the GUI) ---
class ConferenceServer:
    def __init__(self, log_signal, participants_signal):
        self.log_signal = log_signal
        self.participants_signal = participants_signal
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.video_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.clients = {}  # { tcp_conn: (username, udp_addr) }
        self.username_to_conn = {} # { username: tcp_conn }
        self.lock = threading.Lock()
        self.running = True

    def start(self):
        try:
            self.tcp_socket.bind((HOST, TCP_PORT)); self.tcp_socket.listen()
            self.log_signal.emit(f"✅ TCP Server listening on {HOST}:{TCP_PORT}")

            self.video_udp_socket.bind((HOST, VIDEO_UDP_PORT))
            self.log_signal.emit(f"✅ Video UDP Server listening on {HOST}:{VIDEO_UDP_PORT}")

            self.audio_udp_socket.bind((HOST, AUDIO_UDP_PORT))
            self.log_signal.emit(f"✅ Audio UDP Server listening on {HOST}:{AUDIO_UDP_PORT}")
        except Exception as e:
            self.log_signal.emit(f"❌ SERVER STARTUP FAILED: {e}"); self.running = False; return

        threading.Thread(target=self.udp_listener, args=(self.video_udp_socket,), daemon=True).start()
        threading.Thread(target=self.udp_listener, args=(self.audio_udp_socket,), daemon=True).start()

        while self.running:
            try:
                conn, addr = self.tcp_socket.accept()
                if self.running:
                    threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
            except OSError: break

    def stop(self):
        self.running = False
        with self.lock:
            for conn in list(self.clients.keys()): conn.close()
        self.tcp_socket.close(); self.video_udp_socket.close(); self.audio_udp_socket.close()
        self.log_signal.emit("Server has been shut down.")

    def udp_listener(self, sock):
        """Re-broadcasts all received UDP packets (audio/video) to all other clients."""
        while self.running:
            try:
                data, addr = sock.recvfrom(65536)
                with self.lock:
                    for _, (_, client_udp_addr) in self.clients.items():
                        if client_udp_addr != addr: sock.sendto(data, client_udp_addr)
            except Exception:
                if self.running: break

    def broadcast(self, message, sender_conn=None):
        """Sends a text-based TCP message to all connected clients."""
        with self.lock:
            for conn in list(self.clients.keys()):
                if conn != sender_conn:
                    try: conn.sendall(message.encode('utf-8'))
                    except: self.remove_client(conn)

    def remove_client(self, conn):
        """Gracefully removes a client from the server."""
        with self.lock:
            if conn in self.clients:
                username, _ = self.clients.pop(conn)
                self.username_to_conn.pop(username, None)
                
                self.log_signal.emit(f"Client '{username}' disconnected.")
                
                self.participants_signal.emit(list(self.username_to_conn.keys()))

                # Notify all other clients that this user has left
                self.broadcast(f"USER_LEFT:{username}\n")
                
                self.broadcast(f"SYSTEM:{username} has left the chat.\n")
                
                try: conn.close()
                except: pass

    def handle_client(self, conn, addr):
        """Handles a single client's entire TCP connection lifecycle."""
        username = None
        try:
            # 1. Registration
            join_msg = conn.recv(1024).decode('utf-8').strip()
            parts = join_msg.split(':')
            if parts[0] == 'JOIN' and len(parts) == 3:
                username = parts[1]; client_udp_port = int(parts[2])
                client_udp_addr = (addr[0], client_udp_port)
                with self.lock:
                    if username in self.username_to_conn:
                        self.log_signal.emit(f"Connection refused: Username '{username}' already taken.")
                        conn.sendall(b"ERROR:USERNAME_TAKEN\n"); conn.close(); return
                    self.clients[conn] = (username, client_udp_addr)
                    self.username_to_conn[username] = conn
                self.log_signal.emit(f"Client '{username}' connected from {addr[0]}")
                self.broadcast(f"SYSTEM:{username} has joined the chat.\n", conn)
                self.participants_signal.emit(list(self.username_to_conn.keys()))
            else: conn.close(); return

            # 2. Main Message Loop
            buffer = b""
            while True:
                data = conn.recv(4096)
                if not data: break
                buffer += data
                while True:
                    # Process messages in a loop in case multiple are received
                    if b':' not in buffer: break
                    header, _, rest = buffer.partition(b':')
                    command = header.decode('utf-8', errors='ignore')

                    # Handle text-based commands
                    if command in ("MSG", "PING", "SCRN_START", "SCRN_STOP", "FILE_INIT"):
                        if b'\n' in rest:
                            payload, _, buffer = rest.partition(b'\n')
                            self.handle_tcp_message(conn, username, command, payload.decode('utf-8', errors='ignore'))
                        else: break # Incomplete message
                    # Handle binary screen-share data
                    elif command == "SCRN":
                        if b':' in rest:
                            size_str, _, data_rest = rest.partition(b':')
                            try:
                                data_size = int(size_str.decode('utf-8'))
                                if len(data_rest) >= data_size:
                                    content = data_rest[:data_size]
                                    buffer = data_rest[data_size:]
                                    self.handle_tcp_binary(conn, username, "SCRN", content)
                                else: break # Incomplete binary data
                            except (ValueError, UnicodeDecodeError): buffer = b""; break
                        else: break
                    else: buffer = b""; break # Unknown command
        except (ConnectionResetError, ConnectionAbortedError): pass
        except Exception as e: self.log_signal.emit(f"Error with {username}: {e}")
        finally:
            if conn in self.clients: self.remove_client(conn)

    def handle_tcp_binary(self, conn, username, command, data):
        """Forwards binary data (like screen frames) to all other clients."""
        if command == "SCRN":
            with self.lock:
                for c in list(self.clients.keys()):
                    if c != conn:
                        try:
                            header = f"SCRN:{username}:{len(data)}:".encode('utf-8')
                            c.sendall(header + data)
                        except: self.remove_client(c)

    def handle_tcp_message(self, conn, username, command, payload):
        """Handles text-based commands from a client."""
        if command == "MSG": self.broadcast(f"CHAT:{username}:{payload}\n", conn)
        elif command == "PING": conn.sendall(f"PONG:{payload}\n".encode('utf-8'))
        elif command == "SCRN_START": self.broadcast(f"SCRN_START:{username}\n", conn)
        elif command == "SCRN_STOP": self.broadcast(f"SCRN_STOP:{username}\n", conn)
        elif command == "FILE_INIT":
            try:
                target_user, filename, filesize_str = payload.split(':', 2)
                filesize = int(filesize_str)
                threading.Thread(target=self.handle_file_transfer, args=(conn, username, target_user, filename, filesize), daemon=True).start()
            except (ValueError, IndexError): self.log_signal.emit(f"Error: Invalid FILE_INIT format from {username}")

    def handle_file_transfer(self, sender_conn, sender_username, target_username, filename, filesize):
        """Coordinates a peer-to-peer file transfer by relaying data."""
        self.log_signal.emit(f"Initiating file transfer from '{sender_username}' to '{target_username}' ({filesize} bytes)")
        receiver_conn = self.username_to_conn.get(target_username)
        if not receiver_conn:
            self.log_signal.emit(f"File transfer failed: Target user '{target_username}' not found.")
            try: sender_conn.sendall(f"SYSTEM:User '{target_username}' not found.\n".encode('utf-8'))
            except Exception as e: self.log_signal.emit(f"Error notifying sender {sender_username}: {e}")
            return
        try:
            receiver_conn.sendall(f"FILE_INCOMING:{sender_username}:{filename}:{filesize}\n".encode('utf-8'))
        except Exception as e:
            self.log_signal.emit(f"Could not notify receiver '{target_username}': {e}")
            sender_conn.sendall(f"SYSTEM:Could not reach '{target_username}' for file transfer.\n".encode('utf-8')); return
        
        bytes_transferred = 0
        try:
            while bytes_transferred < filesize:
                chunk_size = min(4096, filesize - bytes_transferred)
                chunk = sender_conn.recv(chunk_size)
                if not chunk: self.log_signal.emit(f"Sender '{sender_username}' disconnected during file transfer."); break
                receiver_conn.sendall(chunk); bytes_transferred += len(chunk)
            if bytes_transferred == filesize: self.log_signal.emit(f"File transfer from '{sender_username}' to '{target_username}' completed successfully.")
        except Exception as e: self.log_signal.emit(f"An error occurred during file transfer: {e}")

# --- PyQt6 Worker and GUI ---
class ServerWorker(QObject):
    log_message = pyqtSignal(str); update_participants = pyqtSignal(list)
    def __init__(self):
        super().__init__(); self.server = ConferenceServer(self.log_message, self.update_participants)
    def run(self): self.server.start()
    def stop(self): self.server.stop()

class ServerGUI(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("MOOZ Server"); self.setGeometry(100, 100, 800, 600)
        main_widget = QWidget(); main_layout = QHBoxLayout(main_widget); self.setCentralWidget(main_widget)
        participants_layout = QVBoxLayout(); participants_layout.addWidget(QLabel("<h3>Active Participants</h3>"))
        self.participants_list = QListWidget(); participants_layout.addWidget(self.participants_list); main_layout.addLayout(participants_layout, 1)
        log_layout = QVBoxLayout(); log_layout.addWidget(QLabel("<h3>Server Log</h3>"))
        self.log_display = QTextEdit(); self.log_display.setReadOnly(True); log_layout.addWidget(self.log_display); main_layout.addLayout(log_layout, 2)
        
        # Apply basic blue theme styling
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1e2a3a; color: #ecf0f1; }
            QListWidget { background-color: #2c3e50; border: 1px solid #34495e; border-radius: 5px; }
            QTextEdit { background-color: #2c3e50; border: 1px solid #34495e; border-radius: 5px; }
            QLabel { font-size: 14px; }
            h3 { color: #5dade2; }
        """)
        
        self.setup_server_thread()

    def setup_server_thread(self):
        self.server_thread = QThread(); self.server_worker = ServerWorker(); self.server_worker.moveToThread(self.server_thread)
        self.server_worker.log_message.connect(self.log_display.append)
        self.server_worker.update_participants.connect(lambda p: (self.participants_list.clear(), self.participants_list.addItems(p)))
        self.server_thread.started.connect(self.server_worker.run); self.server_thread.start()

    def closeEvent(self, event):
        self.server_worker.stop(); self.server_thread.quit(); self.server_thread.wait(); super().closeEvent(event)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    gui = ServerGUI()
    gui.show()
    sys.exit(app.exec())