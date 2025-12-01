# lobby.py
import sys
import os
import random

# --- Frozen-mode (PyInstaller) robustness: ensure Qt finds its plugins ---
if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ["QT_PLUGIN_PATH"] = os.path.join(base, "PyQt6", "Qt6", "plugins")
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(base, "PyQt6", "Qt6", "plugins", "platforms")

from PyQt6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QMessageBox, QLabel
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from utils import resource_path


class LobbyDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to MOOZ")
        self.setModal(True)
        self.setObjectName("LoginDialog")

        self.username = ""
        self.server_host = ""

        # --- Robust Stylesheet Loading (works in source & PyInstaller) ---
        try:
            stylesheet_path = resource_path("style.qss")
            with open(stylesheet_path, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())
            print("Stylesheet loaded successfully.")
        except Exception as e:
            print(f"--- CRITICAL: Could not load stylesheet from {stylesheet_path} ---")
            print(f"Error: {e}")

        # --- Layout ---
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(15)

        self.title = QLabel("MOOZ")
        self.title.setStyleSheet("font-size: 28px; font-weight: bold; color: #5dade2;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(self.title)

        form_widget = QWidget()
        form_layout = QFormLayout(form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(10)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Leave blank for a random name...")
        self.server_ip_input = QLineEdit()
        self.server_ip_input.setText("127.0.0.1")  # Default for local testing

        form_layout.addRow(QLabel("Display Name:"), self.username_input)
        form_layout.addRow(QLabel("Server IP:"), self.server_ip_input)

        self.layout.addWidget(form_widget)

        self.join_button = QPushButton("Join Meeting")
        self.join_button.setObjectName("JoinButton")
        self.join_button.clicked.connect(self.on_join)
        self.join_button.setMinimumWidth(200)
        self.layout.addWidget(self.join_button)

    def on_join(self):
        username = self.username_input.text().strip()
        server_host = self.server_ip_input.text().strip()

        # Random Name Generation (if blank)
        if not username:
            adjectives = ["Quick", "Blue", "Keen", "Agile", "Bright"]
            nouns = ["Heron", "Fox", "Panda", "Lion", "Tiger"]
            username = f"{random.choice(adjectives)}-{random.choice(nouns)}-{random.randint(100, 999)}"

        if not server_host:
            QMessageBox.warning(self, "Error", "Server IP cannot be empty.")
            return

        self.username = username
        self.server_host = server_host
        self.accept()


if __name__ == '__main__':
    # Prefer software OpenGL in case of flaky GPU drivers (before QApplication)
    QGuiApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)

    # Windows freeze-support (safer for frozen apps using threads/subprocess)
    from multiprocessing import freeze_support
    freeze_support()

    app = QApplication(sys.argv)

    dialog = LobbyDialog()

    if dialog.exec() == QDialog.DialogCode.Accepted:
        # Lazy import after dialog is accepted — avoids early import errors in EXE
        from client import ConferenceClient
        client = ConferenceClient(dialog.username, dialog.server_host)
        client.show()
        sys.exit(app.exec())

    sys.exit(0)
