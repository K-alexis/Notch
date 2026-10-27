import sys
import math
import ctypes
import asyncio
from queue import Empty, Queue
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QSlider, QPushButton, QDialog, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import Qt, QPropertyAnimation, QRect, QRectF, QEasingCurve, QTimer, QTime, QSignalBlocker, QThread, pyqtSignal, pyqtProperty, QElapsedTimer
from PyQt6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPixmap, QLinearGradient, QPen

# Librerías de audio de Windows
try:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
except ImportError:
    print("Por favor, instala pycaw usando: pip install pycaw comtypes")
    sys.exit()

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )
    from winrt.windows.storage.streams import DataReader
    WINRT_IMPORT_ERROR = None
except ImportError as error:
    GlobalSystemMediaTransportControlsSessionManager = None
    GlobalSystemMediaTransportControlsSessionPlaybackStatus = None
    DataReader = None
    WINRT_IMPORT_ERROR = str(error)


class MediaSessionWorker(QThread):
    media_updated = pyqtSignal(str, str, bytes, str)
    timeline_updated = pyqtSignal(float, float, bool, str)
    media_error = pyqtSignal(str)
    media_notice = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = Queue()
        self.last_media = None
        self.last_status = None
        self.last_error = None

    def send_command(self, command):
        name, _, value = command.partition(":")
        self.commands.put((name, float(value) if value else None))

    def discard_commands(self):
        while True:
            try:
                self.commands.get_nowait()
            except Empty:
                return

    def stop(self):
        self.requestInterruption()
        self.wait(1500)

    def run(self):
        if WINRT_IMPORT_ERROR:
            self.media_error.emit(
                "Falta la dependencia multimedia WinRT. Ejecuta: pip install -r requirements.txt"
            )
            return
        try:
            asyncio.run(self.monitor_sessions())
        except Exception as error:
            self.media_error.emit(f"Error multimedia: {error}")

    async def monitor_sessions(self):
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        while not self.isInterruptionRequested():
            try:
                session = self.get_active_session(manager)
                if session is None:
                    self.discard_commands()
                    self.publish_media("", "", b"", "empty")
                    self.timeline_updated.emit(0.0, 0.0, False, "")
                else:
                    await self.process_commands(manager, session)
                    properties = await session.try_get_media_properties_async()
                    playback_info = session.get_playback_info()
                    try:
                        timeline = session.get_timeline_properties()
                        position = max(0.0, timeline.position.total_seconds())
                        duration = max(0.0, timeline.end_time.total_seconds())
                        can_seek = (
                            playback_info.controls.is_playback_position_enabled
                            and duration > 0
                        )
                    except (AttributeError, TypeError, ValueError):
                        position, duration, can_seek = 0.0, 0.0, False
                    self.timeline_updated.emit(
                        position, duration, can_seek, session.source_app_user_model_id
                    )
                    status = playback_info.playback_status
                    status_name = (
                        "playing"
                        if status
                        == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
                        else "paused"
                    )
                    source = session.source_app_user_model_id
                    key = (source, properties.title, properties.artist)
                    artwork = b""
                    if key != self.last_media and properties.thumbnail is not None:
                        artwork = await self.read_thumbnail(properties.thumbnail)
                    self.publish_media(
                        properties.title or "Reproducción multimedia",
                        properties.artist or source.rsplit("!", 1)[-1],
                        artwork,
                        status_name,
                        key,
                    )
                self.last_error = None
            except Exception as error:
                message = f"Error multimedia: {error}"
                if message != self.last_error:
                    self.media_error.emit(message)
                    self.last_error = message
                self.last_media = None
                self.last_status = None
            await asyncio.sleep(0.5)

    @staticmethod
    def get_active_session(manager):
        session = manager.get_current_session()
        if session is not None:
            return session
        for candidate in manager.get_sessions():
            if (
                candidate.get_playback_info().playback_status
                == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
            ):
                return candidate
        return None

    async def read_thumbnail(self, thumbnail):
        stream = await thumbnail.open_read_async()
        reader = None
        try:
            size = int(stream.size)
            if size <= 0 or size > 5 * 1024 * 1024:
                return b""
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            data = bytearray(size)
            reader.read_bytes(data)
            return bytes(data)
        finally:
            if reader is not None:
                reader.close()
            stream.close()

    async def process_commands(self, manager, session):
        while True:
            try:
                command, value = self.commands.get_nowait()
            except Empty:
                return

            try:
                active_session = self.get_active_session(manager) or session
                if command == "play_pause":
                    accepted = await active_session.try_toggle_play_pause_async()
                elif command == "previous":
                    accepted = await active_session.try_skip_previous_async()
                elif command == "next":
                    accepted = await active_session.try_skip_next_async()
                elif command in {"seek_back", "seek_forward", "seek_to"}:
                    controls = active_session.get_playback_info().controls
                    if not controls.is_playback_position_enabled:
                        self.media_notice.emit(
                            "Este reproductor no permite saltar en el vídeo."
                        )
                        continue
                    timeline = active_session.get_timeline_properties()
                    position = timeline.position.total_seconds()
                    duration = timeline.end_time.total_seconds()
                    if duration <= 0:
                        self.media_notice.emit(
                            "No se puede cambiar la posición de este contenido."
                        )
                        continue
                    if command == "seek_to":
                        target = value
                    else:
                        offset = -10 if command == "seek_back" else 10
                        target = position + offset
                    target = max(0, min(target, duration))
                    accepted = await active_session.try_change_playback_position_async(
                        int(target * 10_000_000)
                    )
                else:
                    continue
            except Exception as error:
                if not self.send_media_key_fallback(command):
                    self.media_notice.emit(
                        f"No se pudo ejecutar el control multimedia: {error}"
                    )
                continue
            if not accepted and not self.send_media_key_fallback(command):
                self.media_notice.emit(
                    "El reproductor activo rechazó este control."
                )

    @classmethod
    def send_media_key_fallback(cls, command):
        fallback_keys = {
            "previous": 0xB1,
            "play_pause": 0xB3,
            "next": 0xB0,
        }
        virtual_key = fallback_keys.get(command)
        if virtual_key is None:
            return False
        cls.send_media_key(virtual_key)
        return True

    @staticmethod
    def send_media_key(virtual_key):
        if sys.platform != "win32":
            return
        key_up = 0x0002
        ctypes.windll.user32.keybd_event(virtual_key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(virtual_key, 0, key_up, 0)

    def publish_media(self, title, artist, artwork, status, key=None):
        if (
            key == self.last_media
            and status == self.last_status
            and not artwork
        ):
            return
        self.last_media = key
        self.last_status = status
        self.media_updated.emit(title, artist, artwork, status)


class DialogCloseButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("closeButton")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Cerrar")
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            "border-radius: 8px; padding: 0; }"
            "QPushButton:hover { background: #e81123; }"
            "QPushButton:pressed { background: #c50f1f; }"
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#ffffff" if self.underMouse() else "#bfc6d1")
        painter.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center = self.rect().center()
        offset = 4
        painter.drawLine(
            center.x() - offset,
            center.y() - offset,
            center.x() + offset,
            center.y() + offset,
        )
        painter.drawLine(
            center.x() + offset,
            center.y() - offset,
            center.x() - offset,
            center.y() + offset,
        )


class DialogTitleBar(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self._drag_offset = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 12px; font-weight: bold;")
        layout.addWidget(title_label)
        layout.addStretch()

        close_button = DialogCloseButton(self)
        close_button.clicked.connect(self.window().close)
        layout.addWidget(close_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.window().move(
                event.globalPosition().toPoint() - self._drag_offset
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ConfigDialog(QDialog):
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_island = parent
        self.setWindowTitle("Personalización")
        self.setFixedSize(310, 145)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setStyleSheet(
            "QDialog { background-color: #000000; border: 1px solid #252525; }"
            "QLabel { color: white; }"
        )

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(DialogTitleBar("Personalización", self))

        title = QLabel("Elige un color de acento")
        title.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QLabel("Se aplica a ondas, volúmenes y controles")
        subtitle.setStyleSheet("font-size: 10px; color: #aaa;")
        layout.addWidget(subtitle)

        color_layout = QHBoxLayout()
        self.palette_buttons = {}
        for name, primary, secondary in self.parent_island.accent_palettes:
            button = QPushButton(self)
            button.setFixedSize(38, 38)
            button.setToolTip(name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda checked=False, palette=(name, primary, secondary): self.select_palette(palette)
            )
            color_layout.addWidget(button)
            self.palette_buttons[name] = button
        layout.addLayout(color_layout)

        self.update_selected_palette(self.parent_island.accent_palette_name)
        self.setLayout(layout)

    def select_palette(self, palette):
        name, primary, secondary = palette
        self.parent_island.apply_accent_palette(name, primary, secondary)
        self.update_selected_palette(name)

    def update_selected_palette(self, selected_name):
        for name, primary, _ in self.parent_island.accent_palettes:
            border = "#ffffff" if name == selected_name else "#555555"
            self.palette_buttons[name].setStyleSheet(
                f"QPushButton {{ background-color: {primary}; border: 3px solid "
                f"{border}; border-radius: 19px; }}"
                "QPushButton:hover { border-color: white; }"
            )

    def hideEvent(self, event):
        was_visible = getattr(self, "_was_visible", False)
        self._was_visible = False
        super().hideEvent(event)
        if was_visible:
            self.closed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self._was_visible = True


class WaveTimeline(QWidget):
    seek_requested = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.position = 0.0
        self.duration = 0.0
        self.seek_enabled = False
        self.dragging = False
        self.phase = 0.0
        self.accent_color = QColor("#43dcff")
        self.accent_secondary_color = QColor("#706fff")
        self.setMinimumHeight(38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Arrastra las ondas para cambiar el momento del vídeo")
        self.wave_timer = QTimer(self)
        self.wave_timer.setInterval(33)
        self.wave_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.wave_timer.timeout.connect(self.animate_wave)
        self.elapsed_timer = QElapsedTimer()
        self.playing = False

    def showEvent(self, event):
        super().showEvent(event)
        self.elapsed_timer.start()
        if self.playing:
            self.wave_timer.start()

    def hideEvent(self, event):
        self.wave_timer.stop()
        super().hideEvent(event)

    def animate_wave(self):
        elapsed = self.elapsed_timer.restart()
        delta = min(0.033, elapsed / 1000.0) if elapsed > 0 else 0.016
        self.phase += 2.2 * delta
        self.update()

    def set_playback_state(self, position, duration, seek_enabled):
        duration = max(0.0, duration)
        seek_enabled = seek_enabled and duration > 0
        previous_state = (
            round(self.position, 1),
            round(self.duration, 1),
            self.seek_enabled,
        )
        self.duration = duration
        self.seek_enabled = seek_enabled
        if not self.dragging:
            self.position = min(max(0.0, position), self.duration)
        current_state = (
            round(self.position, 1),
            round(self.duration, 1),
            self.seek_enabled,
        )
        if current_state != previous_state:
            self.update()

    def set_playing(self, playing):
        if self.playing == playing:
            return
        self.playing = playing
        if playing and self.isVisible():
            self.elapsed_timer.start()
            self.wave_timer.start()
        else:
            self.wave_timer.stop()
        self.update()

    def position_from_x(self, x):
        margin = 4
        progress = min(1.0, max(0.0, (x - margin) / max(1, self.width() - margin * 2)))
        return progress * self.duration

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.seek_enabled:
            self.dragging = True
            self.position = self.position_from_x(event.position().x())
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.position = self.position_from_x(event.position().x())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.dragging and event.button() == Qt.MouseButton.LeftButton:
            self.position = self.position_from_x(event.position().x())
            self.dragging = False
            self.update()
            self.seek_requested.emit(self.position)

    @staticmethod
    def format_time(seconds):
        total_seconds = max(0, int(seconds))
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        progress = self.position / self.duration if self.duration else 0
        middle_y = 15
        bar_count = max(12, width // 5)
        gap = 2
        bar_width = max(1.4, (width - gap * (bar_count - 1) - 8) / bar_count)
        start_x = 4
        gradient = QLinearGradient(0, 0, width, 0)
        gradient.setColorAt(0, self.accent_color)
        gradient.setColorAt(1, self.accent_secondary_color)

        for index in range(bar_count):
            wave = (
                math.sin(index * 0.57 + self.phase) * 0.48
                + math.sin(index * 0.21 + 1.2 - self.phase * 0.7) * 0.34
                + math.sin(index * 0.91 + 2.4 + self.phase * 0.35) * 0.18
            )
            envelope = 0.34 + 0.66 * max(0.0, 1 - abs((index / max(1, bar_count - 1)) * 2 - 1))
            bar_height = 3.5 + abs(wave) * 12 * envelope
            x = start_x + index * (bar_width + gap)
            painter.setPen(Qt.PenStyle.NoPen)
            if self.seek_enabled and x / max(1, width) <= progress:
                painter.setBrush(gradient)
            else:
                painter.setBrush(QColor(65, 75, 92, 210))
            painter.drawRoundedRect(
                QRectF(x, middle_y - bar_height / 2, bar_width, bar_height),
                bar_width / 2,
                bar_width / 2,
            )

        if self.seek_enabled:
            head_x = start_x + progress * max(1, width - start_x * 2)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(QRectF(head_x - 3, middle_y - 3, 6, 6))
        painter.setPen(QColor(158, 165, 178))
        painter.drawText(
            QRectF(2, 27, width / 2 - 3, 11),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.format_time(self.position),
        )
        painter.drawText(
            QRectF(width / 2, 27, width / 2 - 3, 11),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self.format_time(self.duration),
        )

    def set_accent_colors(self, primary, secondary):
        self.accent_color = QColor(primary)
        self.accent_secondary_color = QColor(secondary)
        self.update()


class VolumeSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSingleStep(2)
        self.setPageStep(2)
        self.wheel_delta = 0

    def wheelEvent(self, event):
        if not self.isEnabled():
            event.ignore()
            return

        self.wheel_delta += event.angleDelta().y()
        notches = int(self.wheel_delta / 120)
        if notches:
            self.wheel_delta -= notches * 120
            self.setValue(self.value() + notches * 2)
        event.accept()


class LiquidPlayButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._water_level = 0.12
        self._wave_phase = 0.0
        self._sloshing = 0.0
        self._slosh_phase = 0.0
        self._playing = False
        self.droplets = []
        self.accent_color = QColor("#43dcff")
        self.accent_secondary_color = QColor("#706fff")
        self.elapsed_timer = QElapsedTimer()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, False)
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0; }"
            "QPushButton:pressed { background: transparent; border: none; }"
        )
        self.wave_timer = QTimer(self)
        self.wave_timer.setInterval(30)
        self.wave_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.wave_timer.timeout.connect(self.advance_wave)
        self.water_animation = QPropertyAnimation(self, b"waterLevel", self)
        self.water_animation.setDuration(220)
        self.water_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event):
        super().showEvent(event)
        self.elapsed_timer.start()
        self.wave_timer.start()

    def hideEvent(self, event):
        self.wave_timer.stop()
        super().hideEvent(event)

    @pyqtProperty(float)
    def waterLevel(self):
        return self._water_level

    @waterLevel.setter
    def waterLevel(self, value):
        self._water_level = min(0.82, max(0.0, value))
        self.update()

    def set_playing(self, playing, animate=True):
        self._playing = playing
        target = 0.58 if playing else 0.0
        self.water_animation.stop()
        if animate and abs(self._water_level - target) > 0.01:
            direction = 1 if target > self._water_level else -1
            self.spawn_splash(playing, direction)
            self._sloshing = direction * 4.5
            self._slosh_phase += math.pi / 2
            self.water_animation.setStartValue(self._water_level)
            self.water_animation.setEndValue(target)
            self.water_animation.start()
        else:
            self.waterLevel = target

    def spawn_splash(self, filling, direction):
        bounds = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        surface_y = bounds.bottom() - bounds.height() * self._water_level
        for index in range(15):
            spread = (index - 7) / 7
            x = bounds.center().x() + spread * bounds.width() * 0.3
            if filling:
                y = bounds.bottom() + 1
                vx = spread * 46
                vy = -(100 + (index * 43 % 78))
            else:
                y = surface_y + math.sin(index * 2.4) * 1.5
                vx = spread * 90
                vy = -(90 + (index * 37 % 100))
            self.droplets.append({
                "x": x,
                "y": y,
                "vx": vx,
                "vy": vy,
                "radius": 1.2 + (index % 3) * 0.4,
                "life": 0.58 if filling else 0.68,
                "max_life": 0.58 if filling else 0.68,
                "direction": direction,
            })

    def advance_wave(self):
        elapsed = self.elapsed_timer.restart()
        delta = min(0.025, elapsed / 1000.0) if elapsed > 0 else 0.016
        self._wave_phase += 7.0 * delta
        self._slosh_phase += 15.0 * delta
        self._sloshing *= 0.94 ** (delta * 60)
        if abs(self._sloshing) < 0.03:
            self._sloshing = 0.0
        gravity = 460
        for droplet in self.droplets:
            droplet["x"] += droplet["vx"] * delta
            droplet["y"] += droplet["vy"] * delta
            droplet["vy"] += gravity * delta
            droplet["life"] -= delta
        self.droplets = [
            droplet for droplet in self.droplets if droplet["life"] > 0
        ]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(12, bounds.width() / 2, bounds.height() / 2)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, radius, radius)
        painter.setClipPath(clip)
        painter.fillPath(clip, QColor("#20252d"))

        gradient = QLinearGradient(bounds.left(), bounds.top(), bounds.right(), bounds.bottom())
        primary = QColor(self.accent_color)
        secondary = QColor(self.accent_secondary_color)
        primary.setAlpha(225)
        secondary.setAlpha(245)
        gradient.setColorAt(0, primary)
        gradient.setColorAt(1, secondary)
        if self._water_level > 0.005:
            surface_y = bounds.bottom() - bounds.height() * self._water_level
            wave = QPainterPath()
            wave.moveTo(bounds.left(), surface_y)
            steps = max(12, int(bounds.width() / 2))
            for step in range(steps + 1):
                x = bounds.left() + bounds.width() * step / steps
                ripple = (
                    math.sin(step / steps * math.tau * 1.4 + self._wave_phase) * 1.8
                    + math.sin(step / steps * math.tau * 2.2 - self._wave_phase * 0.7)
                    * 0.7
                    + math.sin(step / steps * math.tau + self._slosh_phase)
                    * self._sloshing
                )
                wave.lineTo(x, surface_y + ripple)
            wave.lineTo(bounds.right(), bounds.bottom())
            wave.lineTo(bounds.left(), bounds.bottom())
            wave.closeSubpath()
            painter.fillPath(wave, gradient)

        for droplet in self.droplets:
            opacity = min(1.0, droplet["life"] / 0.18)
            color = QColor(self.accent_color)
            color.setAlpha(round(230 * opacity))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = droplet["radius"]
            painter.drawEllipse(
                QRectF(
                    droplet["x"] - radius,
                    droplet["y"] - radius,
                    radius * 2,
                    radius * 2,
                )
            )

        icon_color = QColor(self.accent_color)
        icon_color.setAlpha(215)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(icon_color)
        center = bounds.center()
        if self._playing:
            bar_width = 4
            bar_height = 14
            gap = 3
            for x in (
                center.x() - gap / 2 - bar_width,
                center.x() + gap / 2,
            ):
                painter.drawRoundedRect(
                    QRectF(
                        x,
                        center.y() - bar_height / 2,
                        bar_width,
                        bar_height,
                    ),
                    1.5,
                    1.5,
                )
        else:
            play_icon = QPainterPath()
            play_icon.moveTo(center.x() - 4, center.y() - 7)
            play_icon.lineTo(center.x() + 7, center.y())
            play_icon.lineTo(center.x() - 4, center.y() + 7)
            play_icon.closeSubpath()
            painter.drawPath(play_icon)
        painter.setClipping(False)
        outline = QColor(self.accent_color)
        outline.setAlpha(220)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(clip)

    def set_accent_colors(self, primary, secondary):
        self.accent_color = QColor(primary)
        self.accent_secondary_color = QColor(secondary)
        self.update()


class SettingsGearButton(QPushButton):
    ROTATION_DURATION_MS = 750

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rotation = 0.0
        self._accent_color = QColor("#43dcff")
        self.setText("")
        self.rotation_animation = QPropertyAnimation(self, b"rotation", self)
        self.rotation_animation.setDuration(self.ROTATION_DURATION_MS)
        self.rotation_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.rotation_animation.finished.connect(self.reset_rotation)

    @pyqtProperty(float)
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, value):
        self._rotation = value
        self.update()

    def spin(self):
        self.rotation_animation.stop()
        self.rotation_animation.setStartValue(self._rotation)
        self.rotation_animation.setEndValue(self._rotation + 360)
        self.rotation_animation.start()

    def reset_rotation(self):
        self.rotation = 0.0

    def set_accent_color(self, color):
        self._accent_color = QColor(color)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QRectF(self.rect()).center()
        painter.translate(center)
        painter.rotate(self._rotation)
        gear = QPainterPath()
        teeth = 8
        outer_radius = min(self.width(), self.height()) * 0.31
        valley_radius = outer_radius * 0.72
        for index in range(teeth * 4):
            angle = (index / (teeth * 4)) * math.tau - math.pi / 2
            radius = outer_radius if index % 4 in (1, 2) else valley_radius
            x = math.cos(angle) * radius
            y = math.sin(angle) * radius
            if index == 0:
                gear.moveTo(x, y)
            else:
                gear.lineTo(x, y)
        gear.closeSubpath()
        gear.addEllipse(
            QRectF(-outer_radius * 0.38, -outer_radius * 0.38,
                   outer_radius * 0.76, outer_radius * 0.76)
        )
        gear.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(gear, self._accent_color)


class DynamicIsland(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                            Qt.WindowType.WindowStaysOnTopHint | 
                            Qt.WindowType.Tool)
        
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self.setObjectName("Island")
        
        self.normal_width, self.normal_height = 140, 36
        self.expanded_width, self.expanded_height = 270, 280
        
        screen_width = QApplication.primaryScreen().geometry().width()
        self.x_center = (screen_width - self.normal_width) // 2
        self.setGeometry(self.x_center, 0, self.normal_width, self.normal_height)
        
        self.is_expanded = False
        self.current_rgb = "0, 0, 0"
        self.current_alpha = 255
        self.audio_apps = {}
        self.general_volume_endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        self.accent_palettes = (
            ("Cian", "#43dcff", "#706fff"),
            ("Violeta", "#a78bfa", "#f472b6"),
            ("Menta", "#5eead4", "#34d399"),
            ("Atardecer", "#fb923c", "#f43f5e"),
            ("Azul", "#60a5fa", "#818cf8"),
        )
        self.accent_palette_name = "Cian"
        self.accent_color = QColor("#43dcff")
        self.accent_secondary_color = QColor("#706fff")
        
        self.apply_style()
        
        # --- MODO REPOSO: RELOJ ---
        self.time_label = QLabel("", self)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setGeometry(0, 0, self.normal_width, self.normal_height)
        self.time_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: white;"
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_time)
        self.timer.start(1000) 
        self.update_time() 
        
        # --- MODO EXPANDIDO ---
        self.media_label = QLabel("Controles multimedia", self)
        self.media_label.setGeometry(12, 10, 125, 26)
        self.media_label.hide()

        self.media_art_label = QLabel(self)
        self.media_art_label.setGeometry(12, 42, 58, 58)
        self.media_art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.media_art_label.setStyleSheet(
            "background-color: #222; border: 1px solid #444; border-radius: 9px;"
        )
        self.media_art_label.setText("♪")
        self.media_art_label.setStyleSheet(
            f"background-color: #222; color: #aaa; "
            f"border: 1px solid {self.accent_color.name()}; "
            "border-radius: 9px; font-size: 24px;"
        )
        self.media_art_label.hide()

        self.media_title_label = QLabel("No hay contenido multimedia", self)
        self.media_title_label.setGeometry(78, 42, 158, 40)
        self.media_title_label.setWordWrap(True)
        self.media_title_label.setStyleSheet("font-size: 12px; font-weight: bold;")
        self.media_title_label.hide()

        self.media_artist_label = QLabel("", self)
        self.media_artist_label.setGeometry(78, 80, 158, 24)
        self.media_artist_label.setWordWrap(True)
        self.media_artist_label.setStyleSheet("font-size: 9px; color: #aaa;")
        self.media_artist_label.hide()

        self.media_timeline = WaveTimeline(self)
        self.media_timeline.set_accent_colors(
            self.accent_color, self.accent_secondary_color
        )
        self.media_timeline.setGeometry(12, 144, self.expanded_width - 24, 38)
        self.media_timeline.seek_requested.connect(self.seek_to_position)
        self.media_timeline.hide()

        volume_slider_style = self.volume_slider_style()

        self.general_volume_icon = QLabel("◖))", self)
        self.general_volume_icon.setGeometry(10, 184, 28, 24)
        self.general_volume_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.general_volume_icon.setToolTip("Volumen general de Windows")
        self.general_volume_icon.setStyleSheet("color: #c9d1df; font-size: 12px;")
        self.general_volume_icon.hide()

        self.general_volume_slider = VolumeSlider(Qt.Orientation.Horizontal, self)
        self.general_volume_slider.setGeometry(38, 185, 190, 22)
        self.general_volume_slider.setRange(0, 100)
        self.general_volume_slider.setToolTip("Volumen general de Windows")
        self.general_volume_slider.setStyleSheet(volume_slider_style)
        self.general_volume_slider.valueChanged.connect(self.change_general_volume)
        self.general_volume_slider.hide()

        self.general_volume_value = QLabel("—", self)
        self.general_volume_value.setGeometry(231, 184, 30, 24)
        self.general_volume_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.general_volume_value.setStyleSheet("color: #c9d1df; font-size: 10px;")
        self.general_volume_value.hide()

        self.media_volume_icon = QLabel("♫", self)
        self.media_volume_icon.setGeometry(10, 218, 28, 24)
        self.media_volume_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.media_volume_icon.setToolTip("Volumen de la aplicación multimedia")
        self.media_volume_icon.setStyleSheet("color: #c9d1df; font-size: 14px;")
        self.media_volume_icon.hide()

        self.media_volume_slider = VolumeSlider(Qt.Orientation.Horizontal, self)
        self.media_volume_slider.setGeometry(38, 219, 190, 22)
        self.media_volume_slider.setRange(0, 100)
        self.media_volume_slider.setToolTip("Volumen de la aplicación que reproduce el vídeo")
        self.media_volume_slider.setStyleSheet(volume_slider_style)
        self.media_volume_slider.valueChanged.connect(self.change_media_volume)
        self.media_volume_slider.hide()

        self.media_volume_value = QLabel("—", self)
        self.media_volume_value.setGeometry(231, 218, 30, 24)
        self.media_volume_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.media_volume_value.setStyleSheet("color: #c9d1df; font-size: 10px;")
        self.media_volume_value.hide()

        media_controls = (
            ("|◀", "previous", "Vídeo anterior"),
            ("−10", "seek_back", "Retroceder 10 segundos"),
            ("▶", "play_pause", "Reproducir / pausar"),
            ("+10", "seek_forward", "Adelantar 10 segundos"),
            ("▶|", "next", "Siguiente vídeo"),
        )
        self.media_buttons = []
        self.play_pause_button = None
        button_width = 34
        button_spacing = 4
        button_row_width = len(media_controls) * button_width + (
            len(media_controls) - 1
        ) * button_spacing
        button_start_x = (self.expanded_width - button_row_width) // 2
        for index, (text, command, tooltip) in enumerate(media_controls):
            button = (
                LiquidPlayButton(self)
                if command == "play_pause"
                else QPushButton(text, self)
            )
            button.setGeometry(
                button_start_x + index * (button_width + button_spacing),
                108,
                button_width,
                30,
            )
            button.setToolTip(tooltip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if command != "play_pause":
                button.setStyleSheet(self.media_button_style())
            button.clicked.connect(lambda checked=False, action=command: self.send_media_command(action))
            button.hide()
            self.media_buttons.append(button)
            if command == "play_pause":
                self.play_pause_button = button

        self.btn_settings = SettingsGearButton(self)
        self.btn_settings.setGeometry(self.expanded_width - 45, 10, 30, 30)
        self.btn_settings.setStyleSheet(
            f"background: transparent; border: 1px solid {self.accent_color.name()}; "
            f"border-radius: 9px; font-size: 18px; "
            f"color: {self.accent_color.name()};"
        )
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_settings.hide()
        
        self.media_worker = MediaSessionWorker(self)
        self.media_worker.media_updated.connect(self.update_media_display)
        self.media_worker.timeline_updated.connect(self.update_timeline_state)
        self.media_worker.media_error.connect(self.show_media_error)
        self.media_worker.media_notice.connect(self.show_media_notice)
        self.media_status = "empty"
        self.media_source = ""
        self.media_worker.start()

        self.audio_poll_interval = 1000
        self.audio_poll_paused = False
        self.refresh_audio_apps()
        self.audio_timer = QTimer(self)
        self.audio_timer.timeout.connect(self.refresh_audio_apps)
        self._left_mouse_down = self.is_left_mouse_button_down()
        self.global_click_timer = QTimer(self)
        self.global_click_timer.setInterval(50)
        self.global_click_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.global_click_timer.timeout.connect(self.check_global_click)

    def pause_audio_polling(self):
        self.audio_poll_paused = True
        self.audio_timer.stop()

    def resume_audio_polling(self):
        main_animation = getattr(self, "anim", None)
        if (
            not self.is_expanded
            or (
                main_animation
                and main_animation.state() == QPropertyAnimation.State.Running
            )
        ):
            return

        self.audio_poll_paused = False
        self.audio_timer.start(self.audio_poll_interval)
        self.global_click_timer.start()
        self.refresh_audio_apps()

    @staticmethod
    def is_left_mouse_button_down():
        if sys.platform != "win32":
            return False
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)

    def check_global_click(self):
        left_mouse_down = self.is_left_mouse_button_down()
        if left_mouse_down and not self._left_mouse_down:
            position = QCursor.pos()
            inside_main = self.geometry().contains(position)
            if self.is_expanded and not inside_main:
                self.collapse_to_normal()
        self._left_mouse_down = left_mouse_down

    def apply_style(self):
        alpha = self.current_alpha if self.is_expanded else 255
        self.background_color = QColor(
            *(int(component.strip()) for component in self.current_rgb.split(",")),
            alpha,
        )
        self.setStyleSheet(
            "QLabel { color: white; font-family: 'Segoe UI', Arial, sans-serif; }"
        )
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        radius = min(18, self.width() / 2, self.height())
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(self.width(), 0)
        path.lineTo(self.width(), self.height() - radius)
        path.quadTo(self.width(), self.height(), self.width() - radius, self.height())
        path.lineTo(radius, self.height())
        path.quadTo(0, self.height(), 0, self.height() - radius)
        path.closeSubpath()
        painter.fillPath(path, self.background_color)

    def update_time(self):
        self.time_label.setText(QTime.currentTime().toString("h:mm AP"))

    def seek_to_position(self, position):
        if self.media_status != "empty" and self.media_worker.isRunning():
            self.media_worker.send_command(f"seek_to:{position}")

    def update_timeline_state(self, position, duration, can_seek, source):
        self.media_source = source
        self.media_timeline.set_playback_state(position, duration, can_seek)
        self.update_media_volume_control()

    def update_media_volume_control(self):
        source = self.media_source.casefold()
        process_names = {
            "edge": "msedge.exe",
            "chrome": "chrome.exe",
            "firefox": "firefox.exe",
            "brave": "brave.exe",
            "opera": "opera.exe",
        }
        process_name = next(
            (process for token, process in process_names.items() if token in source),
            None,
        )
        matching_key = next(
            (
                key for key in self.audio_apps
                if process_name and (
                    key == process_name or key.startswith(f"{process_name}:")
                )
            ),
            None,
        )
        if matching_key is None and source:
            source_terms = {
                term for term in source.replace("!", ".").replace("_", ".").split(".")
                if len(term) >= 4
            }
            matching_key = next(
                (
                    key for key in self.audio_apps
                    if any(
                        term in key.casefold()
                        for term in source_terms
                    )
                ),
                None,
            )
        if matching_key is None and len(self.audio_apps) == 1:
            matching_key = next(iter(self.audio_apps))

        self.media_volume_app_key = matching_key
        app = self.audio_apps.get(matching_key) if matching_key else None
        controls = app["volumes"] if app else []
        enabled = bool(controls)
        self.media_volume_slider.setEnabled(enabled)
        self.media_volume_slider.setToolTip(
            f"Volumen de {matching_key.split(':', 1)[0]}"
            if enabled
            else "No se pudo identificar una única aplicación con audio activo"
        )
        if enabled:
            if not self.media_volume_slider.isSliderDown():
                blocker = QSignalBlocker(self.media_volume_slider)
                self.media_volume_slider.setValue(
                    round(controls[0].GetMasterVolume() * 100)
                )
                del blocker
            self.media_volume_value.setText(f"{self.media_volume_slider.value()}%")
        else:
            blocker = QSignalBlocker(self.media_volume_slider)
            self.media_volume_slider.setValue(0)
            del blocker
            self.media_volume_value.setText("—")

    def change_media_volume(self, value):
        app = self.audio_apps.get(getattr(self, "media_volume_app_key", None))
        if not app:
            return
        try:
            for control in app["volumes"]:
                control.SetMasterVolume(value / 100.0, None)
            self.media_volume_value.setText(f"{value}%")
        except Exception as error:
            self.show_media_notice(f"No se pudo cambiar el volumen: {error}")

    def update_general_volume_control(self):
        if self.general_volume_slider.isSliderDown():
            return
        volume = self.general_volume_endpoint.GetMasterVolumeLevelScalar()
        blocker = QSignalBlocker(self.general_volume_slider)
        self.general_volume_slider.setValue(round(volume * 100))
        del blocker
        self.general_volume_value.setText(f"{self.general_volume_slider.value()}%")

    def change_general_volume(self, value):
        self.general_volume_endpoint.SetMasterVolumeLevelScalar(
            value / 100.0,
            None,
        )
        self.general_volume_value.setText(f"{value}%")

    def send_media_command(self, command):
        if self.media_status == "empty" or not self.media_worker.isRunning():
            fallback_keys = {
                "previous": 0xB1,
                "play_pause": 0xB3,
                "next": 0xB0,
            }
            virtual_key = fallback_keys.get(command)
            if virtual_key is not None:
                MediaSessionWorker.send_media_key(virtual_key)
            elif command in {"seek_back", "seek_forward"}:
                self.show_media_notice(
                    "No hay una sesión multimedia disponible para cambiar la posición."
                )
            return
        self.media_worker.send_command(command)

    def update_media_display(self, title, artist, artwork, status):
        self.media_status = status
        self.media_timeline.set_playing(status == "playing")
        identity = (title, artist)
        if identity != getattr(self, "displayed_media_identity", None):
            self.displayed_media_identity = identity
            self.media_art_label.setPixmap(QPixmap())
            self.media_art_label.setText("♪")
        if artwork:
            cover = QPixmap()
            if cover.loadFromData(artwork):
                self.media_art_label.setText("")
                self.media_art_label.setPixmap(
                    cover.scaled(
                        self.media_art_label.size(),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        self.media_title_label.setText(title or "No hay contenido multimedia")
        self.media_artist_label.setText(artist)
        self.media_artist_label.setToolTip(artist)
        if self.play_pause_button is not None:
            previous_status = getattr(self, "displayed_play_pause_status", None)
            self.play_pause_button.set_playing(
                status == "playing",
                animate=(
                    previous_status in {"playing", "paused"}
                    and status in {"playing", "paused"}
                    and status != previous_status
                ),
            )
            self.displayed_play_pause_status = status
        for button in self.media_buttons:
            button.setEnabled(True)

    def show_media_error(self, message):
        self.media_timeline.set_playing(False)
        self.media_title_label.setText("Control multimedia no disponible")
        self.media_title_label.setToolTip(message)
        self.media_artist_label.setText("Revisa la instalación de WinRT")
        self.media_art_label.setPixmap(QPixmap())
        self.media_art_label.setText("♪")
        for button in self.media_buttons:
            button.setEnabled(True)

    def show_media_notice(self, message):
        self.media_artist_label.setText(message)
        self.media_artist_label.setToolTip(message)

    def open_settings(self):
        if not hasattr(self, "config_window"):
            self.config_window = ConfigDialog(self)
            self.config_window.closed.connect(self.btn_settings.spin)
        self.btn_settings.spin()
        self.config_window.show()
        self.config_window.raise_()
        self.config_window.activateWindow()

    def volume_slider_style(self):
        return (
            "QSlider::groove:horizontal { background: #39404d; height: 4px; border-radius: 2px; }"
            f"QSlider::sub-page:horizontal {{ background: {self.accent_color.name()}; "
            "border-radius: 2px; }"
            "QSlider::handle:horizontal { background: white; width: 10px; "
            "margin: -4px 0; border-radius: 5px; }"
        )

    def media_button_style(self):
        return (
            "QPushButton { background-color: #292929; color: white; "
            f"border: 1px solid {self.accent_color.name()}; "
            "border-radius: 12px; font-size: 15px; }"
            "QPushButton:hover { background-color: #454545; }"
            f"QPushButton:pressed {{ background-color: {self.accent_color.name()}; }}"
        )

    def apply_accent_palette(self, name, primary, secondary):
        self.accent_palette_name = name
        self.accent_color = QColor(primary)
        self.accent_secondary_color = QColor(secondary)
        self.media_timeline.set_accent_colors(primary, secondary)
        self.play_pause_button.set_accent_colors(primary, secondary)
        self.btn_settings.set_accent_color(primary)
        slider_style = self.volume_slider_style()
        self.general_volume_slider.setStyleSheet(slider_style)
        self.media_volume_slider.setStyleSheet(slider_style)
        self.media_art_label.setStyleSheet(
            "background-color: #222; color: #aaa; "
            f"border: 1px solid {self.accent_color.name()}; "
            "border-radius: 9px; font-size: 24px;"
        )
        for button in self.media_buttons:
            if button is not self.play_pause_button:
                button.setStyleSheet(self.media_button_style())
        self.btn_settings.setStyleSheet(
            f"background: transparent; border: 1px solid {self.accent_color.name()}; "
            f"border-radius: 9px; font-size: 18px; "
            f"color: {self.accent_color.name()};"
        )

    def refresh_audio_apps(self):
        self.update_general_volume_control()
        active_apps = {}
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            process = session.Process
            if process is None or session.State != 1:
                continue

            process_name = process.name()
            display_name = session.DisplayName.strip()
            if display_name:
                app_key = f"{process_name.casefold()}:{display_name.casefold()}"
            else:
                app_key = process_name.casefold()

            if app_key not in active_apps:
                active_apps[app_key] = {
                    "volumes": [],
                }
            active_apps[app_key]["volumes"].append(
                session._ctl.QueryInterface(ISimpleAudioVolume)
            )

        self.audio_apps = active_apps
        self.update_media_volume_control()

    def expand_notch(self):
        if self.is_expanded:
            return
        if hasattr(self, "anim"):
            self.anim.stop()
        self.pause_audio_polling()
        self.is_expanded = True
        self.apply_style()
        screen = QApplication.primaryScreen().availableGeometry()
        new_x = screen.left() + (screen.width() - self.expanded_width) // 2
        self.anim = QPropertyAnimation(self, b"geometry")
        self.anim.setDuration(180)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.setEndValue(
            QRect(new_x, 0, self.expanded_width, self.expanded_height)
        )
        self.anim.finished.connect(self.resume_audio_polling)
        self.time_label.setGeometry(0, 254, self.expanded_width, 24)
        self.time_label.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        self.media_label.show()
        self.media_art_label.show()
        self.media_title_label.show()
        self.media_artist_label.show()
        self.media_timeline.show()
        self.general_volume_icon.show()
        self.general_volume_slider.show()
        self.general_volume_value.show()
        self.media_volume_icon.show()
        self.media_volume_slider.show()
        self.media_volume_value.show()
        for button in self.media_buttons:
            button.show()
        self.btn_settings.show()
        self.anim.start()

    def collapse_to_normal(self):
        if hasattr(self, "anim"):
            self.anim.stop()
        self.pause_audio_polling()
        self.global_click_timer.stop()
        self.is_expanded = False
        self.apply_style()
        self.update_media_volume_control()
        self.media_label.hide()
        self.media_art_label.hide()
        self.media_title_label.hide()
        self.media_artist_label.hide()
        self.media_timeline.hide()
        self.general_volume_icon.hide()
        self.general_volume_slider.hide()
        self.general_volume_value.hide()
        self.media_volume_icon.hide()
        self.media_volume_slider.hide()
        self.media_volume_value.hide()
        for button in self.media_buttons:
            button.hide()
        self.btn_settings.hide()
        if hasattr(self, "config_window"):
            self.config_window.hide()
        self.time_label.setGeometry(0, 0, self.normal_width, self.normal_height)
        self.time_label.setStyleSheet("font-size: 15px; font-weight: bold; color: white;")
        self.anim = QPropertyAnimation(self, b"geometry")
        self.anim.setDuration(140)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.setEndValue(
            QRect(self.x_center, 0, self.normal_width, self.normal_height)
        )
        self.anim.finished.connect(self.resume_audio_polling)
        self.anim.start()

    def mousePressEvent(self, event):
        if event.position().y() > self.normal_height and self.is_expanded:
            return
        if not self.is_expanded:
            self.expand_notch()
        else:
            self.collapse_to_normal()

if __name__ == "__main__":
    if sys.platform == "win32":
        console_window = ctypes.windll.kernel32.GetConsoleWindow()
        if console_window:
            ctypes.windll.user32.ShowWindow(console_window, 0)
    app = QApplication(sys.argv)
    island = DynamicIsland()
    app.aboutToQuit.connect(island.media_worker.stop)
    island.show()
    sys.exit(app.exec())