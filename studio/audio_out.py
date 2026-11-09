"""Výstup zvuku do reproduktorů.

1. sounddevice (PortAudio) – nejnižší latence, vlastní realtime vlákno.
   Na Fedoře: sudo dnf install portaudio
2. Qt Multimedia (QAudioSink) – funguje bez systémových knihoven navíc.
3. null – žádný výstup (testy / jen export).
"""
from __future__ import annotations

from PySide6.QtCore import QIODevice, QThread
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from . import config
from .engine import Engine

BYTES_PER_FRAME = 8  # float32 * 2 kanály


class NullOutput:
    name = "žádný výstup"

    def __init__(self, engine: Engine):
        self.engine = engine

    def start(self):
        pass

    def stop(self):
        pass


class SoundDeviceOutput:
    def __init__(self, engine: Engine):
        import sounddevice as sd  # vyhodí OSError, když chybí PortAudio
        self.sd = sd
        self.engine = engine
        self.stream = None
        dev = sd.query_devices(kind="output")
        self.name = f"PortAudio · {dev['name']}"

    def _cb(self, outdata, frames, time_info, status):
        outdata[:] = self.engine.render(frames)

    def start(self):
        self.stream = self.sd.OutputStream(
            samplerate=self.engine.sr, channels=2, dtype="float32",
            blocksize=512, latency="low", callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class _EngineDevice(QIODevice):
    """QIODevice, ze kterého si QAudioSink tahá data – generuje je engine."""

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine

    def readData(self, maxlen):
        frames = min(maxlen // BYTES_PER_FRAME, 2048)
        if frames <= 0:
            return b""
        return self.engine.render(frames).tobytes()

    def writeData(self, data):
        return -1

    def bytesAvailable(self):
        return (1 << 16) + super().bytesAvailable()

    def isSequential(self):
        return True


class _QtAudioThread(QThread):
    def __init__(self, engine: Engine, buffer_ms: int = 60):
        super().__init__()
        self.engine = engine
        self.buffer_ms = buffer_ms
        self.error = ""

    def run(self):
        fmt = QAudioFormat()
        fmt.setSampleRate(self.engine.sr)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        dev = QMediaDevices.defaultAudioOutput()
        sink = QAudioSink(dev, fmt)
        sink.setBufferSize(int(self.engine.sr * BYTES_PER_FRAME * self.buffer_ms / 1000))
        io = _EngineDevice(self.engine)
        io.open(QIODevice.OpenModeFlag.ReadOnly)
        sink.start(io)
        if sink.error() != sink.error().NoError:
            self.error = str(sink.error())
        self.exec()
        sink.stop()
        io.close()


class QtOutput:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.thread: _QtAudioThread | None = None
        self.name = f"Qt Multimedia · {QMediaDevices.defaultAudioOutput().description()}"

    def start(self):
        self.thread = _QtAudioThread(self.engine)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
            self.thread = None


def create_output(engine: Engine):
    kind = config.BACKEND
    if kind == "null":
        return NullOutput(engine)
    if kind in ("auto", "sd"):
        try:
            return SoundDeviceOutput(engine)
        except Exception:
            if kind == "sd":
                raise
    return QtOutput(engine)
