from __future__ import annotations

import typing as t


FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

KISS_CMD_DATA = 0x00


def _escape(data: bytes) -> bytes:
    return data.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(
        bytes([FEND]), bytes([FESC, TFEND])
    )


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] != FESC:
            out.append(data[i])
            i += 1
            continue

        if i + 1 >= len(data):
            break

        esc = data[i + 1]
        if esc == TFEND:
            out.append(FEND)
        elif esc == TFESC:
            out.append(FESC)
        else:
            out.append(esc)

        i += 2

    return bytes(out)


def encode_frame(payload: bytes, port: int = 0, command: int = KISS_CMD_DATA) -> bytes:
    if not 0 <= port <= 0x0F:
        raise ValueError(f"port must be in range 0-15, got {port}")
    if not 0 <= command <= 0x0F:
        raise ValueError(f"command must be in range 0-15, got {command}")

    command_byte = ((port & 0x0F) << 4) | (command & 0x0F)
    content = bytes([command_byte]) + payload
    return bytes([FEND]) + _escape(content) + bytes([FEND])


def decode_frame(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 1:
        raise ValueError("KISS frame is missing command byte")

    data = _unescape(frame)
    if len(data) < 1:
        raise ValueError("KISS frame is empty after unescaping")

    command_byte = data[0]
    port = (command_byte >> 4) & 0x0F
    command = command_byte & 0x0F
    payload = data[1:]

    return port, command, payload


class StreamDecoder:
    def __init__(self):
        self._in_frame = False
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out: list[tuple[int, int, bytes]] = []

        for byte in data:
            if byte == FEND:
                if self._in_frame and self._buffer:
                    try:
                        out.append(decode_frame(bytes(self._buffer)))
                    except ValueError:
                        pass

                self._buffer.clear()
                self._in_frame = True
                continue

            if not self._in_frame:
                continue

            self._buffer.append(byte)

        return out
