# client.py
import sys
import time
import socket
import os
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QTextEdit, QLineEdit, QPushButton, QGridLayout,
                             QFileDialog, QDialog, QProgressBar, QMessageBox, QStatusBar,
                             QDockWidget, QTabWidget, QStackedWidget, QFrame, QInputDialog)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer, QSize
from PyQt6.QtGui import QImage, QPixmap
import qtawesome as qta
import numpy as np
import cv2

# Import workers from their separate files
from client_video import VideoWorker
from client_audio import AudioWorker
from client_screen import ScreenShareWorker
from client_network import (MediaReceiver, TCPReceiver, FileReceiverWorker,
                            FileSenderWorker, ThreadSafeCounter)

from utils import resource_path

# --- Client Configuration ---
SERVER_PORT = 5000
VIDEO_UDP_PORT = 5001
AUDIO_UDP_PORT = 5002


class ScreenShareViewer(QDialog):
    """A dialog window to view a specific user's screen share."""
    def __init__(self, username, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Screen Share from {username}")
        self.setGeometry(150, 150, 1280, 720)
        self.layout = QVBoxLayout(self)
        self.image_label = QLabel("Waiting for screen share stream...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black;")
        self.layout.addWidget(self.image_label)

    def update_frame(self, frame_bytes):
        np_arr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is not None:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_frame.shape
            qt_image = QImage(rgb_frame.data, w, h, ch * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            self.image_label.setPixmap(pixmap.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def closeEvent(self, event):
        self.parent().on_share_viewer_closed(self.windowTitle())
        super().closeEvent(event)


class FileProgressDialog(QDialog):
    """A dialog to show file transfer progress."""
    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Transfer")
        self.setModal(True)
        self.layout = QVBoxLayout(self)
        self.label = QLabel(f"Receiving: {filename}")
        self.progress_bar = QProgressBar()
        self.layout.addWidget(self.label)
        self.layout.addWidget(self.progress_bar)

    def update_progress(self, value):
        self.progress_bar.setValue(value)


class ConferenceClient(QMainWindow):
    """The main MOOZ conference window."""

    def update_participant_display(self):
        """Updates the participant list widget and tab count."""
        sorted_names = sorted(list(self.participant_names))
        self.participants_list.clear()
        self.participants_list.append("\n".join(sorted_names))
        count = len(self.participant_names)
        self.tab_widget.setTabText(1, f"Participants ({count})")

    def __init__(self, username, server_host):
        super().__init__()
        self.username = username
        self.server_host = server_host
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.threads = {}
        self.workers = {}
        self.video_cells = {}
        self.is_sharing_screen = False
        self.screen_share_viewers = {}
        self.current_page = 0
        self.users_per_page = 9  # 3x3 grid

        self.bytes_sent_counter = ThreadSafeCounter()
        self.bytes_received_counter = ThreadSafeCounter()
        self.user_metrics = {}

        self.participant_names = {self.username}

        self.init_ui()
        self.connect_to_server()

        self.ping_timer = QTimer(self)
        self.ping_timer.timeout.connect(self.send_ping)
        self.ping_timer.start(5000)

        self.metrics_timer = QTimer(self)
        self.metrics_timer.timeout.connect(self.update_metrics_display)
        self.metrics_timer.start(1000)

    def init_ui(self):
        self.setWindowTitle(f"MOOZ - {self.username}")
        self.setMinimumSize(1024, 768)
        self.setGeometry(100, 100, 1280, 720)

        # --- Robust Stylesheet Loading ---
        try:
            qss_path = resource_path("style.qss")
            with open(qss_path, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())
            print(f"Loaded stylesheet: {qss_path}")
        except Exception as e:
            print(f"style.qss not loaded ({e}). Using default style.")

        # --- Main Layout ---
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- Stage ---
        self.stage_area = QWidget()
        self.stage_layout = QVBoxLayout(self.stage_area)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)

        # Stacked widget to switch between Gallery and Presenter
        self.main_stack = QStackedWidget(self.stage_area)

        # 1. Gallery View
        self.video_grid_frame = QWidget()
        gallery_layout = QVBoxLayout(self.video_grid_frame)
        self.video_grid = QGridLayout()
        self.video_grid.setSpacing(10)
        gallery_layout.addLayout(self.video_grid)
        gallery_layout.addStretch(1)  # Pushes grid to top

        # Pagination Bar
        pagination_bar = QWidget()
        pagination_layout = QHBoxLayout(pagination_bar)
        self.prev_button = QPushButton(qta.icon('fa5s.chevron-left', color='#ecf0f1'), "")
        self.next_button = QPushButton(qta.icon('fa5s.chevron-right', color='#ecf0f1'), "")
        self.page_label = QLabel("Page 1 / 1")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_button.clicked.connect(self.prev_page)
        self.next_button.clicked.connect(self.next_page)
        pagination_layout.addStretch()
        pagination_layout.addWidget(self.prev_button)
        pagination_layout.addWidget(self.page_label, 1)
        pagination_layout.addWidget(self.next_button)
        pagination_layout.addStretch()
        gallery_layout.addWidget(pagination_bar)

        # 2. Presenter View
        self.presenter_label = QLabel("Waiting for presenter...")
        self.presenter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.presenter_label.setStyleSheet("background-color: #000; font-size: 20px;")

        self.main_stack.addWidget(self.video_grid_frame)  # Index 0
        self.main_stack.addWidget(self.presenter_label)   # Index 1

        self.stage_layout.addWidget(self.main_stack)

        # --- Floating Self-View ---
        self.self_view = QFrame(self.main_stack)
        self.self_view.setObjectName("SelfView")
        self.self_view.setFixedSize(240, 180)  # 4:3 Aspect ratio
        self_view_layout = QVBoxLayout(self.self_view)
        self_view_layout.setContentsMargins(2, 2, 2, 2)
        self_view_layout.setSpacing(2)
        self.self_view_label = QLabel()
        self.self_view_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.self_view_label.setStyleSheet("background-color: #111; border-radius: 8px;")
        self_view_layout.addWidget(self.self_view_label)

        # --- Control Bar ---
        controls_bar = QWidget()
        controls_bar.setObjectName("ControlsBar")
        controls_layout = QHBoxLayout(controls_bar)
        controls_bar.setFixedHeight(80)

        # Left (Latency)
        left_controls = QWidget()
        left_layout = QHBoxLayout(left_controls)
        self.rtt_label = QLabel("RTT: -- ms")
        self.rtt_label.setObjectName("StatusLabel")
        left_layout.addWidget(self.rtt_label)
        controls_layout.addWidget(left_controls, 1, Qt.AlignmentFlag.AlignLeft)

        # Center (Main Buttons)
        center_controls = QWidget()
        center_layout = QHBoxLayout(center_controls)
        self.mic_button = QPushButton(qta.icon('fa5s.microphone', color='#ecf0f1'), "")
        self.mic_button.setObjectName("ControlButton")
        self.cam_button = QPushButton(qta.icon('fa5s.video', color='#ecf0f1'), "")
        self.cam_button.setObjectName("ControlButton")
        self.screen_share_button = QPushButton(qta.icon('fa5s.desktop', color='#ecf0f1'), "")
        self.screen_share_button.setObjectName("ControlButton")
        self.file_share_button = QPushButton(qta.icon('fa5s.paperclip', color='#ecf0f1'), "")
        self.file_share_button.setObjectName("ControlButton")
        self.end_call_button = QPushButton(qta.icon('fa5s.phone-slash', color='#ecf0f1'), "")
        self.end_call_button.setObjectName("LeaveButton")
        self.mic_button.clicked.connect(self.toggle_mic)
        self.cam_button.clicked.connect(self.toggle_camera)
        self.screen_share_button.clicked.connect(self.toggle_screen_share)
        self.file_share_button.clicked.connect(self.initiate_file_transfer)
        self.end_call_button.clicked.connect(self.close)
        center_layout.setSpacing(15)
        center_layout.addWidget(self.mic_button)
        center_layout.addWidget(self.cam_button)
        center_layout.addWidget(self.screen_share_button)
        center_layout.addWidget(self.file_share_button)
        center_layout.addSpacing(30)
        center_layout.addWidget(self.end_call_button)
        controls_layout.addWidget(center_controls, 0, Qt.AlignmentFlag.AlignCenter)  # Align center

        # Right (Toggles)
        right_controls = QWidget()
        right_layout = QHBoxLayout(right_controls)
        self.participants_button = QPushButton(qta.icon('fa5s.users', color='#ecf0f1'), "")
        self.participants_button.setObjectName("ControlButton")
        self.chat_button = QPushButton(qta.icon('fa5s.comment-alt', color='#ecf0f1'), "")
        self.chat_button.setObjectName("ControlButton")
        self.participants_button.clicked.connect(self.toggle_participants_panel)
        self.chat_button.clicked.connect(self.toggle_chat_panel)
        right_layout.addWidget(self.participants_button)
        right_layout.addWidget(self.chat_button)
        controls_layout.addWidget(right_controls, 1, Qt.AlignmentFlag.AlignRight)

        # --- Add main stage and controls to layout ---
        main_layout.addWidget(self.stage_area, 1)  # 1 = stretch factor
        main_layout.addWidget(controls_bar)

        # --- Side Panel (Dock) ---
        self.side_panel_dock = QDockWidget("Panel", self)
        self.side_panel_dock.setObjectName("SidePanel")
        self.side_panel_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.side_panel_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)  # Remove title bar

        # Tab widget for Chat and Participants
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("SidePanelTabs")

        # Chat Tab
        chat_tab = QWidget()
        chat_layout = QVBoxLayout(chat_tab)
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Type a message...")
        self.chat_input.returnPressed.connect(self.send_chat_message)
        chat_layout.addWidget(self.chat_display)
        chat_layout.addWidget(self.chat_input)

        # Participants Tab
        participants_tab = QWidget()
        part_layout = QVBoxLayout(participants_tab)
        self.participants_list = QTextEdit()
        self.participants_list.setReadOnly(True)
        part_layout.addWidget(self.participants_list)

        self.tab_widget.addTab(chat_tab, "Chat")
        self.tab_widget.addTab(participants_tab, f"Participants ({len(self.participant_names)})")

        self.side_panel_dock.setWidget(self.tab_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.side_panel_dock)
        self.side_panel_dock.hide()

        # Status Bar
        self.setStatusBar(QStatusBar(self))
        self.bandwidth_label = QLabel("BW: --/-- KB/s")
        self.loss_jitter_label = QLabel("Loss/Jitter: --% / -- ms")
        self.statusBar().addPermanentWidget(self.bandwidth_label, 1)
        self.statusBar().addPermanentWidget(self.loss_jitter_label, 2)

    def connect_to_server(self):
        try:
            self.tcp_socket.connect((self.server_host, SERVER_PORT))
            self.udp_socket.bind(('', 0))
            _, udp_port = self.udp_socket.getsockname()
            self.tcp_socket.sendall(f"JOIN:{self.username}:{udp_port}\n".encode('utf-8'))

            tcp_receiver = TCPReceiver(self.tcp_socket)
            self.start_worker('tcp_receiver', tcp_receiver)
            tcp_receiver.message_received.connect(self.handle_server_message)
            tcp_receiver.screen_share_started.connect(self.handle_screen_share_started)
            tcp_receiver.screen_share_stopped.connect(self.handle_screen_share_stopped)
            tcp_receiver.screen_frame_received.connect(self.update_screen_share_view)
            tcp_receiver.file_incoming.connect(self.handle_incoming_file)
            tcp_receiver.bytes_received.connect(self.bytes_received_counter.increment)
            tcp_receiver.user_left.connect(self.handle_user_left)

            media_receiver = MediaReceiver(self.udp_socket)
            self.start_worker('media_receiver', media_receiver)
            media_receiver.video_frame_received.connect(self.update_video_grid)
            media_receiver.bytes_received.connect(self.bytes_received_counter.increment)
            media_receiver.metrics_updated.connect(self.update_user_metrics)

            video_worker = VideoWorker(self.udp_socket, (self.server_host, VIDEO_UDP_PORT), self.username)
            self.start_worker('video_worker', video_worker)
            video_worker.frame_captured.connect(self.update_self_view)  # Connect to self-view
            video_worker.bytes_sent.connect(self.bytes_sent_counter.increment)

            audio_worker = AudioWorker(self.udp_socket, (self.server_host, AUDIO_UDP_PORT), self.username)
            self.start_worker('audio_worker', audio_worker)
            audio_worker.bytes_sent.connect(self.bytes_sent_counter.increment)

        except Exception as e:
            QMessageBox.critical(self, "Connection Error", f"Failed to connect to server at {self.server_host}: {e}")
            self.close()

    def start_worker(self, name, worker_instance):
        thread = QThread()
        worker_instance.moveToThread(thread)
        thread.started.connect(worker_instance.run)
        thread.start()
        self.threads[name] = thread
        self.workers[name] = worker_instance

    def update_self_view(self, image):
        pixmap = QPixmap.fromImage(image)
        self.self_view_label.setPixmap(
            pixmap.scaled(self.self_view.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def update_video_grid(self, username, image):
        if username == self.username:  # Don't add self to main grid
            self.update_self_view(image)
            return

        # Add to participant list
        if username not in self.participant_names:
            self.participant_names.add(username)
            self.update_participant_display()
            cell = QWidget()
            cell.setObjectName("VideoCell")
            layout = QVBoxLayout(cell)
            video_label = QLabel()
            video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            video_label.setStyleSheet("background-color: #1a1a1a; border-radius: 8px;")
            name_label = QLabel(username)
            name_label.setObjectName("NameLabel")
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(video_label, 1)
            layout.addWidget(name_label, 0)
            layout.setContentsMargins(5, 5, 5, 5)
            layout.setSpacing(5)
            self.video_cells[username] = cell
            self.reorganize_grid()

        if username in self.get_users_on_current_page():
            video_label = self.video_cells[username].findChild(QLabel)
            if video_label:
                pixmap = QPixmap.fromImage(image)
                video_label.setPixmap(
                    pixmap.scaled(video_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )

    def reorganize_grid(self):
        for i in reversed(range(self.video_grid.count())):
            widget = self.video_grid.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        usernames_on_page = self.get_users_on_current_page()
        cols = 3
        for i, username in enumerate(usernames_on_page):
            row, col = divmod(i, cols)
            self.video_grid.addWidget(self.video_cells[username], row, col)
        self.update_pagination_controls()

    def get_users_on_current_page(self):
        all_usernames = sorted([u for u in self.video_cells.keys() if u != self.username])
        start = self.current_page * self.users_per_page
        return all_usernames[start: start + self.users_per_page]

    def update_pagination_controls(self):
        num_users = len(self.video_cells)
        if self.username in self.video_cells:
            num_users -= 1  # Don't count self
        total_pages = max(1, (num_users + self.users_per_page - 1) // self.users_per_page)
        self.page_label.setText(f"Page {self.current_page + 1} / {total_pages}")
        self.prev_button.setEnabled(self.current_page > 0)
        self.next_button.setEnabled(self.current_page < total_pages - 1)

    def next_page(self):
        num_users = len(self.video_cells)
        if self.username in self.video_cells:
            num_users -= 1
        total_pages = max(1, (num_users + self.users_per_page - 1) // self.users_per_page)
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.reorganize_grid()

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.reorganize_grid()

    def handle_user_left(self, username):
        print(f"Handling user left: {username}")
        if username in self.video_cells:
            widget_to_remove = self.video_cells.pop(username)
            widget_to_remove.setParent(None)
            widget_to_remove.deleteLater()
            self.user_metrics.pop(username, None)
            self.reorganize_grid()
            if username in self.participant_names:
                self.participant_names.remove(username)
            self.update_participant_display()

    def handle_server_message(self, message):
        cmd, payload = message.split(':', 1)
        if cmd == "SYSTEM":
            self.append_chat_message(f"<i style='color:#5dade2;'>[ {payload} ]</i>")
        elif cmd == "CHAT":
            username, text = payload.split(':', 1)
            self.append_chat_message(f"<b style='color:#3498db;'>{username}</b>: {text}")
        elif cmd == "PONG":
            ping_time = float(payload)
            rtt = (time.time() - ping_time) * 1000
            self.rtt_label.setText(f"RTT: {rtt:.1f}ms")

    def send_chat_message(self):
        message = self.chat_input.text().strip()
        if message:
            self.tcp_socket.sendall(f"MSG:{message}\n".encode('utf-8'))
            self.append_chat_message(f"<b style='color:#5dade2;'>You</b>: {message}")
            self.chat_input.clear()

    def append_chat_message(self, html):
        self.chat_display.append(html)

    def send_ping(self):
        self.tcp_socket.sendall(f"PING:{time.time()}\n".encode('utf-8'))

    def update_metrics_display(self):
        up_kbps = self.bytes_sent_counter.get_and_reset() / 1024
        down_kbps = self.bytes_received_counter.get_and_reset() / 1024
        self.bandwidth_label.setText(f"BW: {up_kbps:.1f} Up / {down_kbps:.1f} Down (KB/s)")

        total_loss, total_jitter, count = 0, 0.0, 0
        for metrics in self.user_metrics.values():
            total_loss += metrics['loss']
            total_jitter += metrics['jitter']
            count += 1
        avg_loss = (total_loss / count) if count > 0 else 0
        avg_jitter = (total_jitter / count) if count > 0 else 0
        self.loss_jitter_label.setText(f"Loss: {avg_loss:.1f}% | Jitter: {avg_jitter:.1f}ms (avg)")

    def update_user_metrics(self, username, loss, jitter):
        self.user_metrics[username] = {'loss': loss, 'jitter': jitter}

    def handle_screen_share_started(self, username):
        if self.username == username:
            return
        if username not in self.screen_share_viewers:
            viewer = ScreenShareViewer(username, self)
            self.screen_share_viewers[username] = viewer
            viewer.show()
            self.main_stack.setCurrentWidget(viewer.image_label)
            self.current_presenter = username

    def handle_screen_share_stopped(self, username):
        self.main_stack.setCurrentWidget(self.video_grid_frame)
        self.current_presenter = None
        if username in self.screen_share_viewers:
            self.screen_share_viewers.pop(username).close()

    def update_screen_share_view(self, username, frame_bytes):
        if username == self.current_presenter:
            np_arr = np.frombuffer(frame_bytes, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if frame is not None:
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_frame.shape
                qt_image = QImage(rgb_frame.data, w, h, ch * w, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(qt_image)
                self.presenter_label.setPixmap(
                    pixmap.scaled(self.presenter_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
        if username in self.screen_share_viewers:
            self.screen_share_viewers[username].update_frame(frame_bytes)

    def on_share_viewer_closed(self, title):
        self.screen_share_viewers.pop(title.replace("Screen Share from ", ""), None)

    def handle_incoming_file(self, from_user, filename, filesize):
        reply = QMessageBox.question(
            self, "Incoming File",
            f"Accept file '{filename}' ({filesize/1024:.2f} KB) from {from_user}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            save_path, _ = QFileDialog.getSaveFileName(self, "Save File", filename)
            if save_path:
                self.progress_dialog = FileProgressDialog(filename, self)
                receiver_worker = FileReceiverWorker(self.workers['tcp_receiver'], save_path, filesize)
                self.start_worker('file_receiver', receiver_worker)
                receiver_worker.progress.connect(self.progress_dialog.update_progress)
                receiver_worker.finished.connect(self.on_file_receive_finished)
                self.progress_dialog.show()

    def on_file_receive_finished(self, message):
        self.progress_dialog.close()
        QMessageBox.information(self, "File Transfer", message)
        self.threads['file_receiver'].quit()
        self.threads['file_receiver'].wait()
        del self.workers['file_receiver']
        del self.threads['file_receiver']

    def initiate_file_transfer(self):
        users = [u for u in self.video_cells.keys() if u != self.username]
        if not users:
            QMessageBox.warning(self, "Send File", "No other users to send a file to.")
            return
        target_user, ok = QInputDialog.getItem(self, "Send File", "Select recipient:", users, 0, False)
        if not ok or not target_user:
            return
        filepath, _ = QFileDialog.getOpenFileName(self, "Select File to Send")
        if not filepath:
            return
        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        self.tcp_socket.sendall(f"FILE_INIT:{target_user}:{filename}:{filesize}\n".encode('utf-8'))
        sender_worker = FileSenderWorker(self.tcp_socket, filepath)
        self.start_worker('file_sender', sender_worker)
        sender_worker.finished.connect(self.on_file_send_finished)
        sender_worker.bytes_sent.connect(self.bytes_sent_counter.increment)

    def on_file_send_finished(self, message):
        QMessageBox.information(self, "File Transfer", message)
        self.threads['file_sender'].quit()
        self.threads['file_sender'].wait()
        del self.workers['file_sender']
        del self.threads['file_sender']

    def toggle_mic(self):
        if 'audio_worker' in self.workers:
            worker = self.workers['audio_worker']
            worker.is_muted = not worker.is_muted
            icon = 'fa5s.microphone-slash' if worker.is_muted else 'fa5s.microphone'
            self.mic_button.setIcon(qta.icon(icon, color='#ecf0f1'))
            self.mic_button.setObjectName("LeaveButton" if worker.is_muted else "ControlButton")
            self.mic_button.setStyleSheet(self.styleSheet())  # Refresh style

    def toggle_camera(self):
        if 'video_worker' in self.workers:
            worker = self.workers['video_worker']
            worker.is_muted = not worker.is_muted
            icon = 'fa5s.video-slash' if worker.is_muted else 'fa5s.video'
            self.cam_button.setIcon(qta.icon(icon, color='#ecf0f1'))
            self.cam_button.setObjectName("LeaveButton" if worker.is_muted else "ControlButton")
            self.cam_button.setStyleSheet(self.styleSheet())

    def toggle_screen_share(self):
        self.is_sharing_screen = not self.is_sharing_screen
        if self.is_sharing_screen:
            self.screen_share_button.setIcon(qta.icon('fa5s.stop-circle', color='#ecf0f1'))
            self.cam_button.setEnabled(False)
            self.screen_share_button.setObjectName("LeaveButton")  # Make it red
            self.main_stack.setCurrentWidget(self.presenter_label)  # Show presenter stage
            self.current_presenter = self.username
            if 'video_worker' in self.workers:
                self.workers['video_worker'].is_muted = True
                self.cam_button.setIcon(qta.icon('fa5s.video-slash', color='#ecf0f1'))
            self.tcp_socket.sendall(f"SCRN_START:{self.username}\n".encode('utf-8'))
            share_worker = ScreenShareWorker(self.tcp_socket)
            self.start_worker('screen_share_worker', share_worker)
            share_worker.bytes_sent.connect(self.bytes_sent_counter.increment)
        else:
            self.screen_share_button.setIcon(qta.icon('fa5s.desktop', color='#ecf0f1'))
            self.cam_button.setEnabled(True)
            self.screen_share_button.setObjectName("ControlButton")
            self.main_stack.setCurrentWidget(self.video_grid_frame)  # Show gallery
            self.current_presenter = None
            if 'screen_share_worker' in self.workers:
                self.workers.pop('screen_share_worker').stop()
            self.tcp_socket.sendall(f"SCRN_STOP:{self.username}\n".encode('utf-8'))
            if 'video_worker' in self.workers and not self.workers['video_worker'].is_muted:
                self.cam_button.setIcon(qta.icon('fa5s.video', color='#ecf0f1'))
        self.screen_share_button.setStyleSheet(self.styleSheet())

    def toggle_chat_panel(self):
        self.tab_widget.setCurrentIndex(0)  # Go to Chat tab
        if self.side_panel_dock.isVisible():
            self.side_panel_dock.hide()
            self.chat_button.setObjectName("ControlButton")
        else:
            self.side_panel_dock.show()
            self.chat_button.setObjectName("ActiveButton")
            self.participants_button.setObjectName("ControlButton")
        self.chat_button.setStyleSheet(self.styleSheet())
        self.participants_button.setStyleSheet(self.styleSheet())

    def toggle_participants_panel(self):
        self.tab_widget.setCurrentIndex(1)  # Go to Participants tab
        if self.side_panel_dock.isVisible():
            self.side_panel_dock.hide()
            self.participants_button.setObjectName("ControlButton")
        else:
            self.side_panel_dock.show()
            self.participants_button.setObjectName("ActiveButton")
            self.chat_button.setObjectName("ControlButton")
        self.chat_button.setStyleSheet(self.styleSheet())
        self.participants_button.setStyleSheet(self.styleSheet())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep self-view pinned to top right of the main stack
        top_right = self.main_stack.geometry().topRight()
        self.self_view.move(top_right.x() - self.self_view.width() - 10, top_right.y() + 10)
        self.self_view.raise_()

    def closeEvent(self, event):
        reply = QMessageBox.question(
            self, "Leave MOOZ", "Are you sure you want to leave the meeting?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.ping_timer.stop()
            self.metrics_timer.stop()
            for worker in self.workers.values():
                if hasattr(worker, 'stop'):
                    worker.stop()
            time.sleep(0.1)
            self.tcp_socket.close()
            self.udp_socket.close()
            for thread in self.threads.values():
                thread.quit()
                thread.wait()
            event.accept()
        else:
            event.ignore()
