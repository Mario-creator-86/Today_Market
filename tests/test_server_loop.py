import asyncio
import socket
import struct
import sys

import pytest

import main


def test_launcher_uses_selector_loop_on_windows():
    expected = 'asyncio:SelectorEventLoop' if sys.platform == 'win32' else 'auto'
    assert main.SERVER_LOOP == expected


@pytest.mark.skipif(sys.platform != 'win32', reason='Proactor accept failure is Windows-specific')
def test_selector_server_keeps_accepting_after_reset_burst():
    async def scenario() -> bytes:
        async def handle(reader, writer):
            writer.write(b'ok')
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        def burst():
            for _ in range(400):
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('hh', 1, 0))
                try:
                    s.connect(('127.0.0.1', port))
                finally:
                    s.close()
        await asyncio.to_thread(burst)
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
        data = await asyncio.wait_for(reader.read(2), 5)
        writer.close()
        server.close()
        return data

    runner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
    try:
        assert runner.run(scenario()) == b'ok'
    finally:
        runner.close()
