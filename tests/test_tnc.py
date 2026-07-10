from __future__ import annotations

import asyncio
import typing as t
from typing import get_args

import pytest
import benlink.command as command
from benlink.controller import RadioController, _RadioState
from benlink.command import TncDataFragment, TncDataFragmentReceivedEvent


class _FakeConnection:
    def __init__(self):
        self.fragments: list[TncDataFragment] = []

    async def send_tnc_data_fragment(self, fragment: TncDataFragment) -> None:
        self.fragments.append(fragment)


def _dummy_state() -> _RadioState:
    return _RadioState(
        device_info=t.cast(t.Any, object()),
        beacon_settings=t.cast(t.Any, object()),
        status=t.cast(t.Any, object()),
        settings=t.cast(t.Any, object()),
        channels=[],
    )


def test_event_type_literals_are_correct() -> None:
    event_types = set(get_args(command.EventType))
    assert "RINGING_STOPPED" in event_types
    assert "RADIO_STATUS_CHANGED" in event_types
    assert "POSITION_CHANGE" in event_types


def test_send_tnc_data_fragments_and_marks_final() -> None:
    conn = _FakeConnection()
    radio = RadioController(t.cast(t.Any, conn))

    payload = bytes(range(120))

    asyncio.run(radio.send_tnc_data(payload, channel_id=7))

    assert len(conn.fragments) == 3
    assert conn.fragments[0].fragment_id == 0
    assert conn.fragments[1].fragment_id == 1
    assert conn.fragments[2].fragment_id == 2

    assert conn.fragments[0].is_final_fragment is False
    assert conn.fragments[1].is_final_fragment is False
    assert conn.fragments[2].is_final_fragment is True

    assert conn.fragments[0].channel_id == 7
    assert conn.fragments[1].channel_id == 7
    assert conn.fragments[2].channel_id == 7

    assert b"".join(fragment.data for fragment in conn.fragments) == payload


def test_send_tnc_data_rejects_too_many_fragments() -> None:
    conn = _FakeConnection()
    radio = RadioController(t.cast(t.Any, conn))

    with pytest.raises(ValueError):
        asyncio.run(radio.send_tnc_data(bytes(3201)))


def test_reassembles_tnc_fragments_and_calls_handlers() -> None:
    conn = _FakeConnection()
    radio = RadioController(t.cast(t.Any, conn))
    radio._state = _dummy_state()

    received: list[tuple[bytes, int | None]] = []
    radio.add_tnc_data_handler(lambda data, channel_id: received.append((data, channel_id)))

    radio._on_event_message(
        TncDataFragmentReceivedEvent(
            tnc_data_fragment=TncDataFragment(
                is_final_fragment=False,
                fragment_id=0,
                data=b"hello ",
                channel_id=None,
            )
        )
    )
    radio._on_event_message(
        TncDataFragmentReceivedEvent(
            tnc_data_fragment=TncDataFragment(
                is_final_fragment=True,
                fragment_id=1,
                data=b"world",
                channel_id=None,
            )
        )
    )

    assert received == [(b"hello world", None)]


def test_ignores_out_of_order_fragment_without_start() -> None:
    conn = _FakeConnection()
    radio = RadioController(t.cast(t.Any, conn))
    radio._state = _dummy_state()

    received: list[tuple[bytes, int | None]] = []
    radio.add_tnc_data_handler(lambda data, channel_id: received.append((data, channel_id)))

    radio._on_event_message(
        TncDataFragmentReceivedEvent(
            tnc_data_fragment=TncDataFragment(
                is_final_fragment=True,
                fragment_id=1,
                data=b"late",
                channel_id=3,
            )
        )
    )

    assert received == []
