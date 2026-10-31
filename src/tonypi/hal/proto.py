"""Wire protocol of the Hiwonder STM32 controller board ("ros_robot_controller").

Frame: AA 55 FUNC LEN DATA[LEN] CRC8, CRC-8/MAXIM (reflected poly 0x8C, init 0) over FUNC, LEN, DATA.
Multi-byte fields are little-endian. Pure functions only, so this module is testable without hardware.
"""

from __future__ import annotations

import enum
import struct
from typing import Iterator

HEADER = b"\xaa\x55"


class Func(enum.IntEnum):
    SYS = 0
    LED = 1
    BUZZER = 2
    MOTOR = 3
    PWM_SERVO = 4
    BUS_SERVO = 5
    KEY = 6
    IMU = 7
    GAMEPAD = 8
    SBUS = 9
    OLED = 10
    RGB = 11


class BusCmd(enum.IntEnum):
    MOVE = 0x01
    STOP = 0x03
    READ_POSITION = 0x05
    READ_VIN = 0x07
    READ_TEMP = 0x09
    UNLOAD = 0x0B  # torque off (verified 2026-10-28: READ_TORQUE -> 0)
    LOAD = 0x0C  # torque on (READ_TORQUE -> 1); a MOVE also re-enables torque
    READ_TORQUE = 0x0D
    SET_ID = 0x10
    READ_ID = 0x12
    SET_OFFSET = 0x20
    READ_OFFSET = 0x22
    SAVE_OFFSET = 0x24
    SET_ANGLE_LIMIT = 0x30
    READ_ANGLE_LIMIT = 0x32
    SET_VIN_LIMIT = 0x34
    READ_VIN_LIMIT = 0x36
    SET_TEMP_LIMIT = 0x38
    READ_TEMP_LIMIT = 0x3A


class PwmCmd(enum.IntEnum):
    MOVE = 0x01
    READ_POSITION = 0x05
    SET_OFFSET = 0x07
    READ_OFFSET = 0x09


# Reply layouts for bus servo reads: (id u8, cmd u8, success i8, payload...)
BUS_READ_FORMATS = {
    BusCmd.READ_POSITION: "<BBbh",
    BusCmd.READ_VIN: "<BBbH",
    BusCmd.READ_TEMP: "<BBbB",
    BusCmd.READ_TORQUE: "<BBbb",
    BusCmd.READ_ID: "<BBbB",
    BusCmd.READ_OFFSET: "<BBbb",
    BusCmd.READ_ANGLE_LIMIT: "<BBb2H",
    BusCmd.READ_VIN_LIMIT: "<BBb2H",
    BusCmd.READ_TEMP_LIMIT: "<BBbB",
}

PWM_READ_FORMATS = {
    PwmCmd.READ_POSITION: "<BBH",
    PwmCmd.READ_OFFSET: "<BBb",
}


def crc8(data: bytes) -> int:
    """Divide the LSB-first bit stream by x^8 + x^5 + x^4 + 1.

    Initial remainder and final XOR are zero. The reflected feedback taps are
    0x8C. This bit-serial implementation is derived from those CRC parameters;
    it does not use the manufacturer's lookup table or checksum routine.
    """
    remainder = 0
    for octet in data:
        for bit_index in range(8):
            input_bit = (octet >> bit_index) & 1
            feedback = input_bit ^ (remainder & 1)
            remainder >>= 1
            if feedback:
                remainder ^= 0x8C
    return remainder


def encode(func: int, payload: bytes) -> bytes:
    if len(payload) > 255:
        raise ValueError("payload too long")
    body = bytes((int(func), len(payload))) + bytes(payload)
    return HEADER + body + bytes((crc8(body),))


class Decoder:
    """Incremental frame parser. Feed raw bytes, get (func, payload) tuples. Bad CRCs are counted and dropped."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> Iterator[tuple[int, bytes]]:
        buf = self._buf
        buf.extend(data)
        while True:
            start = buf.find(HEADER)
            if start < 0:
                # keep a trailing 0xAA in case the header is split across reads
                del buf[: max(0, len(buf) - 1)]
                return
            if start:
                del buf[:start]
            if len(buf) < 4:
                return
            func, length = buf[2], buf[3]
            if func > Func.RGB:
                del buf[:2]
                continue
            end = 4 + length + 1
            if len(buf) < end:
                return
            body = bytes(buf[2 : 4 + length])
            if crc8(body) == buf[end - 1]:
                del buf[:end]
                yield func, body[2:]
            else:
                self.crc_errors += 1
                del buf[:2]


# ---- payload builders -------------------------------------------------------

def bus_move(duration_ms: int, positions: dict[int, int]) -> bytes:
    """One frame moving many servos together over duration_ms (servo-side interpolation)."""
    d = max(0, min(int(duration_ms), 0xFFFF))
    out = bytearray((BusCmd.MOVE, d & 0xFF, d >> 8, len(positions)))
    for sid, pos in positions.items():
        out += struct.pack("<BH", int(sid), max(0, min(int(pos), 1000)))
    return bytes(out)


def bus_stop(ids: list[int]) -> bytes:
    return bytes((BusCmd.STOP, len(ids), *ids))


def bus_simple(cmd: int, sid: int) -> bytes:
    return bytes((int(cmd), int(sid)))


def pwm_move(duration_ms: int, pulses: dict[int, int]) -> bytes:
    d = max(0, min(int(duration_ms), 0xFFFF))
    out = bytearray((PwmCmd.MOVE, d & 0xFF, d >> 8, len(pulses)))
    for ch, us in pulses.items():
        out += struct.pack("<BH", int(ch), max(500, min(int(us), 2500)))
    return bytes(out)


def buzzer(freq_hz: int, on_ms: int, off_ms: int, repeat: int = 1) -> bytes:
    return struct.pack("<HHHH", freq_hz, on_ms, off_ms, repeat)


def led(led_id: int, on_ms: int, off_ms: int, repeat: int = 1) -> bytes:
    return struct.pack("<BHHH", led_id, on_ms, off_ms, repeat)


def rgb(pixels: list[tuple[int, int, int, int]]) -> bytes:
    """pixels: (index starting at 1, r, g, b)."""
    out = bytearray((0x01, len(pixels)))
    for idx, r, g, b in pixels:
        out += struct.pack("<BBBB", idx - 1, r, g, b)
    return bytes(out)
