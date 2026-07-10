from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import pty
import signal

from benlink.controller import RadioController
from benlink.kiss import KISS_CMD_DATA, StreamDecoder, encode_frame


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose benlink TNC as a KISS serial device via PTY"
    )
    parser.add_argument("uuid", help="Bluetooth device UUID, e.g. XX:XX:XX:XX:XX:XX")
    parser.add_argument(
        "--transport",
        choices=["ble", "rfcomm"],
        default="ble",
        help="Bluetooth transport to use",
    )
    parser.add_argument(
        "--rfcomm-channel",
        type=int,
        default=1,
        help="RFCOMM channel when using --transport rfcomm",
    )
    parser.add_argument(
        "--symlink",
        help="Optional stable symlink path for the PTY, e.g. /tmp/n76-kiss",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="KISS port number (default: 0)",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=None,
        help="Optional radio channel_id to set on outbound TNC fragments",
    )
    parser.add_argument(
        "--enable-kiss",
        action="store_true",
        help="Set radio setting kiss_en=True on connect",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.transport == "ble":
        radio = RadioController.new_ble(args.uuid)
    else:
        radio = RadioController.new_rfcomm(args.uuid, channel=args.rfcomm_channel)

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    if args.symlink:
        try:
            if os.path.islink(args.symlink) or os.path.exists(args.symlink):
                os.unlink(args.symlink)
            os.symlink(slave_path, args.symlink)
        except OSError as exc:
            raise RuntimeError(f"Failed to create symlink {args.symlink}: {exc}") from exc

    print(f"KISS PTY ready: {slave_path}")
    if args.symlink:
        print(f"KISS symlink: {args.symlink} -> {slave_path}")
    print("Press Ctrl+C to stop")

    decoder = StreamDecoder()
    tx_queue: asyncio.Queue[bytes] = asyncio.Queue()
    stop_event = asyncio.Event()

    async def tx_worker() -> None:
        while True:
            payload = await tx_queue.get()
            try:
                await radio.send_tnc_data(payload, channel_id=args.channel_id)
            except Exception as exc:
                print(f"[WARN] Failed to send KISS payload to radio: {exc}")

    def on_pty_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            return

        for port, command, payload in decoder.feed(data):
            if port != args.port:
                continue
            if command != KISS_CMD_DATA:
                continue
            tx_queue.put_nowait(payload)

    def on_tnc_data(payload: bytes, channel_id: int | None) -> None:
        del channel_id
        frame = encode_frame(payload, port=args.port, command=KISS_CMD_DATA)
        try:
            os.write(master_fd, frame)
        except OSError as exc:
            print(f"[WARN] Failed writing frame to PTY: {exc}")

    loop = asyncio.get_running_loop()

    async with radio:
        await radio.enable_event("DATA_RXD")
        if args.enable_kiss:
            await radio.set_settings(kiss_en=True)

        remove_tnc_handler = radio.add_tnc_data_handler(on_tnc_data)
        loop.add_reader(master_fd, on_pty_readable)
        tx_task = asyncio.create_task(tx_worker())

        def _stop() -> None:
            stop_event.set()

        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)

        try:
            await stop_event.wait()
        finally:
            tx_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tx_task

            remove_tnc_handler()
            loop.remove_reader(master_fd)
            os.close(master_fd)
            os.close(slave_fd)

            if args.symlink and os.path.islink(args.symlink):
                os.unlink(args.symlink)


def main() -> None:
    parser = _make_parser()
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
