"""Non-blocking keys for an attended terminal; Ctrl+C remains a signal.

值守终端的非阻塞按键控制；Ctrl+C 仍作为信号处理。
    LEFT  -> "home"   smooth return to the initial pose, then pause / 平滑回初始位并暂停
    RIGHT -> "resume" resume from a fresh observation / 从新观测恢复策略
    SPACE -> hold in place and pause / 原地保持并暂停
"""
import os
import select
import sys
import termios


class RehomeRequested(Exception):
    pass


class PauseRequested(Exception):
    """The operator pressed SPACE in the deployment terminal."""


class TerminalControls:
    def __init__(self, stream=None):
        self.stream = sys.stdin if stream is None else stream
        self.fd = None
        self.saved = None
        self.buffer = b""

    def open(self):
        self.fd = self.stream.fileno()
        if not os.isatty(self.fd):
            raise RuntimeError("Keyboard deployment requires an attended terminal")
        self.saved = termios.tcgetattr(self.fd)
        settings = termios.tcgetattr(self.fd)
        settings[3] &= ~(termios.ICANON | termios.ECHO)
        # Keep ISIG so Ctrl+C also interrupts a blocking homing operation.
        # 保留 ISIG，Ctrl+C 可以中断阻塞中的回零。
        settings[6][termios.VMIN] = 1
        settings[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, settings)

    def close(self):
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            finally:
                self.saved = None

    def pause_requested(self):
        """Give SPACE priority while keeping arrow keys buffered for poll()."""
        if self.fd is None:
            return False
        while select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 1024)
            if not data:
                raise RuntimeError("Deployment terminal disconnected")
            if b" " in data:
                self.buffer = b""
                return True
            self.buffer += data
        return False

    def check_pause(self):
        if self.pause_requested():
            raise PauseRequested("SPACE pressed")

    def poll(self):
        """Decode buffered ANSI arrow sequences into events. / 把缓冲的方向键转义序列解码为事件。"""
        self.check_pause()
        events = []
        while self.buffer:
            if self.buffer[:1] != b"\x1b":
                self.buffer = self.buffer[1:]
                continue
            if len(self.buffer) < 2:
                break
            if self.buffer[1:2] not in (b"[", b"O"):
                self.buffer = self.buffer[1:]
                continue
            if len(self.buffer) < 3:
                break
            if self.buffer[2:3] == b"D":
                events.append("home")
            elif self.buffer[2:3] == b"C":
                events.append("resume")
            self.buffer = self.buffer[3:]
        return events

    def discard(self):
        """Drop autorepeat / keys typed while busy. / 丢弃忙碌期间积压的按键。"""
        self.poll()
        self.buffer = b""
