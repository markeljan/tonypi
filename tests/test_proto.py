import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tonypi.hal import proto  # noqa: E402


def test_crc8_check_values():
    assert proto.crc8(b"") == 0
    assert proto.crc8(b"123456789") == 0xA1  # CRC-8/MAXIM-DOW check value
    assert proto.crc8(bytes(range(256))) == 0x18


def test_crc8_covers_packet_bytes_and_accepts_its_own_check_byte():
    body = bytes.fromhex("05 02 09 01")  # bus-servo temperature read, servo 1
    assert proto.crc8(body) == 0x22
    assert proto.encode(proto.Func.BUS_SERVO, bytes.fromhex("09 01")) == bytes.fromhex("aa 55 05 02 09 01 22")
    assert proto.crc8(body + b"\x22") == 0


def test_encode_layout():
    f = proto.encode(proto.Func.BUZZER, proto.buzzer(1900, 100, 0, 1))
    assert f[:2] == b"\xaa\x55"
    assert f[2] == proto.Func.BUZZER and f[3] == 8
    assert f[-1] == proto.crc8(f[2:-1])


def test_decoder_roundtrip_with_noise_and_split():
    frames = [proto.encode(proto.Func.IMU, bytes(range(24))), proto.encode(proto.Func.SYS, b"\x04\x10\x27")]
    stream = b"\x00\xaa\x13" + frames[0] + b"\xaa" + frames[1]
    d = proto.Decoder()
    got = []
    for i in range(0, len(stream), 5):
        got += list(d.feed(stream[i : i + 5]))
    assert got == [(proto.Func.IMU, bytes(range(24))), (proto.Func.SYS, b"\x04\x10\x27")]


def test_decoder_drops_bad_crc():
    bad = bytearray(proto.encode(proto.Func.KEY, b"\x01\x20"))
    bad[-1] ^= 0xFF
    d = proto.Decoder()
    assert list(d.feed(bytes(bad))) == []
    assert d.crc_errors == 1


def test_bus_move_batches_all_servos():
    p = proto.bus_move(500, {1: 500, 2: 390, 3: 1200})
    assert p[0] == 0x01 and p[1:3] == (500).to_bytes(2, "little") and p[3] == 3
    assert p[-2:] == (1000).to_bytes(2, "little")  # clamped
