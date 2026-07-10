from __future__ import annotations

from benlink.kiss import KISS_CMD_DATA, StreamDecoder, decode_frame, encode_frame


def test_encode_decode_round_trip() -> None:
    payload = b"hello"
    frame = encode_frame(payload, port=2, command=KISS_CMD_DATA)

    assert frame[0] == 0xC0
    assert frame[-1] == 0xC0

    port, command, decoded_payload = decode_frame(frame[1:-1])

    assert port == 2
    assert command == KISS_CMD_DATA
    assert decoded_payload == payload


def test_escaping_round_trip() -> None:
    payload = bytes([0x00, 0xC0, 0xDB, 0x01])
    frame = encode_frame(payload)
    _, _, decoded_payload = decode_frame(frame[1:-1])
    assert decoded_payload == payload


def test_stream_decoder_incremental() -> None:
    decoder = StreamDecoder()
    frame_1 = encode_frame(b"abc", port=0, command=KISS_CMD_DATA)
    frame_2 = encode_frame(b"xyz", port=1, command=KISS_CMD_DATA)

    combined = frame_1 + frame_2

    out = []
    for b in combined:
        out.extend(decoder.feed(bytes([b])))

    assert out == [
        (0, KISS_CMD_DATA, b"abc"),
        (1, KISS_CMD_DATA, b"xyz"),
    ]


def test_stream_decoder_ignores_noise_before_fend() -> None:
    decoder = StreamDecoder()
    frame = encode_frame(b"ok")

    out = decoder.feed(b"noise" + frame)

    assert out == [(0, KISS_CMD_DATA, b"ok")]
