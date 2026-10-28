"""Single owner of the STM32 controller board serial link.

Exactly one Board may exist per robot (one process). Everything else talks to that process.
Fixes over the vendor SDK: reads have timeouts, streamed sensors keep the *latest* sample,
replies are matched to requests by (id, cmd), and moves are one batched frame.
"""

from __future__ import annotations

import collections
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import serial

from . import proto
from .proto import BusCmd, Func, PwmCmd


@dataclass
class ImuSample:
    t: float  # time.monotonic()
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    payload: bytes | None = None


class BoardError(RuntimeError):
    pass


class Board:
    def __init__(self, device: str = "/dev/ttyAMA0", baudrate: int = 1_000_000) -> None:
        port = serial.Serial(None, baudrate, timeout=0.02)
        port.rts = False  # must be set before open, or the MCU may reset
        port.dtr = False
        port.port = device
        port.open()
        self._port = port
        self._wlock = threading.Lock()
        self._decoder = proto.Decoder()
        self._pending: dict[tuple[int, int, int], _Pending] = {}
        self._plock = threading.Lock()
        self._bus_lock = threading.Lock()  # the board answers one bus read at a time

        self.imu: ImuSample | None = None
        self.imu_count = 0
        self.battery_mv: int | None = None
        self.battery_t: float = 0.0
        self.keys: collections.deque[tuple[float, int, int]] = collections.deque(maxlen=32)
        self.imu_listeners: list[Callable[[ImuSample], None]] = []
        self.key_listeners: list[Callable[[int, int], None]] = []
        self.rx_frames = collections.Counter()

        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, name="board-rx", daemon=True)
        self._thread.start()

    # ---- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1)
        self._port.close()

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low level ------------------------------------------------------------

    def send(self, func: int, payload: bytes) -> None:
        frame = proto.encode(func, payload)
        with self._wlock:
            self._port.write(frame)

    def _rx_loop(self) -> None:
        port = self._port
        while self._running:
            try:
                data = port.read(port.in_waiting or 1)
            except (serial.SerialException, OSError):
                time.sleep(0.1)
                continue
            if not data:
                continue
            for func, payload in self._decoder.feed(data):
                self.rx_frames[func] += 1
                try:
                    self._dispatch(func, payload)
                except Exception:  # never let one bad frame kill the reader
                    pass

    def _dispatch(self, func: int, payload: bytes) -> None:
        now = time.monotonic()
        if func == Func.IMU and len(payload) == 24:
            v = struct.unpack("<6f", payload)
            sample = ImuSample(now, v[0:3], v[3:6])
            self.imu = sample
            self.imu_count += 1
            for cb in self.imu_listeners:
                cb(sample)
        elif func == Func.SYS and len(payload) >= 3 and payload[0] == 0x04:
            self.battery_mv = struct.unpack("<H", payload[1:3])[0]
            self.battery_t = now
        elif func == Func.KEY and len(payload) >= 2:
            self.keys.append((now, payload[0], payload[1]))
            for cb in self.key_listeners:
                cb(payload[0], payload[1])
        elif func in (Func.BUS_SERVO, Func.PWM_SERVO) and len(payload) >= 2:
            key = (func, payload[0], payload[1])
            with self._plock:
                pending = self._pending.get(key)
            if pending is not None:
                pending.payload = payload
                pending.event.set()

    def _request(self, func: int, cmd: int, sid: int, timeout: float) -> bytes | None:
        key = (func, sid, cmd)
        pending = _Pending()
        with self._plock:
            self._pending[key] = pending
        try:
            self.send(func, proto.bus_simple(cmd, sid))
            if pending.event.wait(timeout):
                return pending.payload
            return None
        finally:
            with self._plock:
                self._pending.pop(key, None)

    # ---- bus servos -----------------------------------------------------------

    def bus_read(self, sid: int, cmd: BusCmd, timeout: float = 0.05, retries: int = 2):
        """Returns the decoded value(s) or None on timeout / servo-reported failure."""
        fmt = proto.BUS_READ_FORMATS[cmd]
        with self._bus_lock:
            for _ in range(retries + 1):
                payload = self._request(Func.BUS_SERVO, cmd, sid, timeout)
                if payload is None:
                    continue
                _id, _cmd, ok, *vals = struct.unpack(fmt, payload[: struct.calcsize(fmt)])
                if ok != 0:
                    continue
                return vals[0] if len(vals) == 1 else tuple(vals)
        return None

    def read_position(self, sid: int, timeout: float = 0.05) -> int | None:
        return self.bus_read(sid, BusCmd.READ_POSITION, timeout)

    def read_positions(self, ids, timeout: float = 0.05) -> dict[int, int | None]:
        return {sid: self.read_position(sid, timeout) for sid in ids}

    def move(self, duration_ms: int, positions: dict[int, int]) -> None:
        if positions:
            self.send(Func.BUS_SERVO, proto.bus_move(duration_ms, positions))

    def stop(self, ids) -> None:
        self.send(Func.BUS_SERVO, proto.bus_stop(list(ids)))

    def torque_cmd(self, sid: int, cmd: BusCmd) -> None:
        """Raw 0x0B / 0x0C. Which one means 'on' is not yet verified on this firmware."""
        self.send(Func.BUS_SERVO, proto.bus_simple(cmd, sid))

    def set_offset(self, sid: int, offset: int, save: bool = False) -> None:
        offset = max(-125, min(125, int(offset)))
        self.send(Func.BUS_SERVO, struct.pack("<BBb", BusCmd.SET_OFFSET, sid, offset))
        time.sleep(0.02)
        if save:
            self.send(Func.BUS_SERVO, proto.bus_simple(BusCmd.SAVE_OFFSET, sid))
            time.sleep(0.02)

    def set_angle_limit(self, sid: int, lo: int, hi: int) -> None:
        self.send(Func.BUS_SERVO, struct.pack("<BBHH", BusCmd.SET_ANGLE_LIMIT, sid, lo, hi))
        time.sleep(0.02)

    # ---- PWM head servos --------------------------------------------------------

    def head(self, duration_ms: int, pulses: dict[int, int]) -> None:
        self.send(Func.PWM_SERVO, proto.pwm_move(duration_ms, pulses))

    def pwm_read(self, ch: int, cmd: PwmCmd = PwmCmd.READ_POSITION, timeout: float = 0.05):
        fmt = proto.PWM_READ_FORMATS[cmd]
        payload = self._request(Func.PWM_SERVO, cmd, ch, timeout)
        if payload is None:
            return None
        return struct.unpack(fmt, payload[: struct.calcsize(fmt)])[2]

    # ---- peripherals ------------------------------------------------------------

    def beep(self, freq_hz: int = 1900, on_ms: int = 100, off_ms: int = 100, repeat: int = 1) -> None:
        # off_ms=0 leaves the buzzer on continuously (found 2026-10-28); always give it an off phase
        self.send(Func.BUZZER, proto.buzzer(freq_hz, on_ms, max(off_ms, 50), repeat))

    def buzzer_off(self) -> None:
        self.send(Func.BUZZER, proto.buzzer(0, 0, 0, 0))

    def rgb(self, pixels: list[tuple[int, int, int, int]]) -> None:
        self.send(Func.RGB, proto.rgb(pixels))

    def led(self, led_id: int, on_ms: int, off_ms: int, repeat: int = 1) -> None:
        self.send(Func.LED, proto.led(led_id, on_ms, off_ms, repeat))
