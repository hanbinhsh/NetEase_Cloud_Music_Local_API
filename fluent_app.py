import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import FluentWindow


SETTINGS_PATH = Path("player_settings.json")


@dataclass
class PlayerSettings:
    show_neighbor_covers: bool = True
    show_spectrum: bool = False
    show_original_lyric: bool = True
    show_translated_lyric: bool = True
    show_roman_lyric: bool = True
    prefer_word_by_word: bool = True
    show_play_mode: bool = True
    shortcut_prev: str = "Ctrl+Alt+Left"
    shortcut_next: str = "Ctrl+Alt+Right"
    shortcut_play_pause: str = "Ctrl+Alt+Space"


class StartInterface(QWidget):
    def __init__(self, start_cb, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        title = QLabel("网易云本地 API 服务")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        desc = QLabel("点击按钮启动后台服务，然后在“网页播放器”页面查看效果")
        desc.setWordWrap(True)

        self.btn = QPushButton("启动服务")
        self.btn.clicked.connect(start_cb)
        self.status = QLabel("服务状态：未启动")

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addSpacing(16)
        layout.addWidget(self.btn)
        layout.addWidget(self.status)
        layout.addStretch(1)


class SettingsInterface(QWidget):
    def __init__(self, settings: PlayerSettings, on_change, parent=None):
        super().__init__(parent)
        self.on_change = on_change
        self.settings = settings

        main = QVBoxLayout(self)
        main.setContentsMargins(24, 24, 24, 24)

        display_group = QGroupBox("显示设置")
        display_layout = QVBoxLayout(display_group)

        self.cb_neighbor = QCheckBox("显示下一首/上一首封面")
        self.cb_spectrum = QCheckBox("显示音频频谱")
        self.cb_original = QCheckBox("显示歌词原文")
        self.cb_translated = QCheckBox("显示歌词译文")
        self.cb_roman = QCheckBox("显示歌词罗马音")
        self.cb_word = QCheckBox("优先使用逐字歌词")
        self.cb_mode = QCheckBox("显示播放模式")

        for cb in [
            self.cb_neighbor,
            self.cb_spectrum,
            self.cb_original,
            self.cb_translated,
            self.cb_roman,
            self.cb_word,
            self.cb_mode,
        ]:
            display_layout.addWidget(cb)
            cb.stateChanged.connect(self._emit_change)

        shortcut_group = QGroupBox("快捷键设置")
        shortcut_form = QFormLayout(shortcut_group)
        self.ks_prev = QKeySequenceEdit()
        self.ks_next = QKeySequenceEdit()
        self.ks_play = QKeySequenceEdit()
        shortcut_form.addRow("上一曲", self.ks_prev)
        shortcut_form.addRow("下一曲", self.ks_next)
        shortcut_form.addRow("播放/暂停", self.ks_play)

        for editor in [self.ks_prev, self.ks_next, self.ks_play]:
            editor.editingFinished.connect(self._emit_change)

        main.addWidget(display_group)
        main.addWidget(shortcut_group)
        main.addStretch(1)

        self.load_to_form()

    def load_to_form(self):
        s = self.settings
        self.cb_neighbor.setChecked(s.show_neighbor_covers)
        self.cb_spectrum.setChecked(s.show_spectrum)
        self.cb_original.setChecked(s.show_original_lyric)
        self.cb_translated.setChecked(s.show_translated_lyric)
        self.cb_roman.setChecked(s.show_roman_lyric)
        self.cb_word.setChecked(s.prefer_word_by_word)
        self.cb_mode.setChecked(s.show_play_mode)
        self.ks_prev.setKeySequence(QKeySequence(s.shortcut_prev))
        self.ks_next.setKeySequence(QKeySequence(s.shortcut_next))
        self.ks_play.setKeySequence(QKeySequence(s.shortcut_play_pause))

    def _emit_change(self):
        self.settings.show_neighbor_covers = self.cb_neighbor.isChecked()
        self.settings.show_spectrum = self.cb_spectrum.isChecked()
        self.settings.show_original_lyric = self.cb_original.isChecked()
        self.settings.show_translated_lyric = self.cb_translated.isChecked()
        self.settings.show_roman_lyric = self.cb_roman.isChecked()
        self.settings.prefer_word_by_word = self.cb_word.isChecked()
        self.settings.show_play_mode = self.cb_mode.isChecked()
        self.settings.shortcut_prev = self.ks_prev.keySequence().toString() or "Ctrl+Alt+Left"
        self.settings.shortcut_next = self.ks_next.keySequence().toString() or "Ctrl+Alt+Right"
        self.settings.shortcut_play_pause = self.ks_play.keySequence().toString() or "Ctrl+Alt+Space"
        self.on_change()


class WebPlayerInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.view = QWebEngineView()
        layout.addWidget(self.view)

    def load_with_settings(self, settings: PlayerSettings):
        params = {
            "base_url": "http://127.0.0.1:18726",
            "showNeighborCovers": int(settings.show_neighbor_covers),
            "showSpectrum": int(settings.show_spectrum),
            "showOriginalLyric": int(settings.show_original_lyric),
            "showTranslatedLyric": int(settings.show_translated_lyric),
            "showRomanLyric": int(settings.show_roman_lyric),
            "preferWordByWord": int(settings.prefer_word_by_word),
            "showPlayMode": int(settings.show_play_mode),
        }
        player_path = Path(__file__).resolve().parent / "player" / "player.html"
        url = QUrl.fromLocalFile(str(player_path))
        url.setQuery(urlencode(params))
        self.view.load(url)


class MusicFluentWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.resize(1200, 820)
        self.setWindowTitle("NetEase Cloud Music Local API - Fluent")

        self.settings = self._load_settings()
        self.service_process = None
        self.shortcuts = []

        self.start_interface = StartInterface(self.start_service, self)
        self.settings_interface = SettingsInterface(self.settings, self.on_settings_changed, self)
        self.web_interface = WebPlayerInterface(self)

        self.addSubInterface(self.start_interface, FIF.PLAY, "主界面")
        self.addSubInterface(self.settings_interface, FIF.SETTING, "设置")
        self.addSubInterface(self.web_interface, FIF.GLOBE, "网页播放器")

        self.on_settings_changed()

    def _load_settings(self) -> PlayerSettings:
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                return PlayerSettings(**data)
            except Exception:
                pass
        return PlayerSettings()

    def _save_settings(self):
        SETTINGS_PATH.write_text(json.dumps(asdict(self.settings), ensure_ascii=False, indent=2), encoding="utf-8")

    def on_settings_changed(self):
        self._save_settings()
        self.web_interface.load_with_settings(self.settings)
        self._setup_shortcuts()

    def start_service(self):
        if self.service_process and self.service_process.poll() is None:
            self.start_interface.status.setText("服务状态：已启动")
            return

        self.service_process = subprocess.Popen([sys.executable, "main.py"], cwd=Path(__file__).resolve().parent)
        self.start_interface.status.setText(f"服务状态：已启动 (PID={self.service_process.pid})")

    def _setup_shortcuts(self):
        for shortcut in self.shortcuts:
            shortcut.setParent(None)
        self.shortcuts.clear()

        mapping = [
            (self.settings.shortcut_prev, self._send_media_prev),
            (self.settings.shortcut_next, self._send_media_next),
            (self.settings.shortcut_play_pause, self._send_media_play_pause),
        ]
        for key, callback in mapping:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    @staticmethod
    def _tap_vk(vk_code: int):
        if os.name != "nt":
            return
        import ctypes

        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)

    def _send_media_prev(self):
        self._tap_vk(0xB1)

    def _send_media_next(self):
        self._tap_vk(0xB0)

    def _send_media_play_pause(self):
        self._tap_vk(0xB3)

    def closeEvent(self, event):
        if self.service_process and self.service_process.poll() is None:
            self.service_process.terminate()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MusicFluentWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
