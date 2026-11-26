import asyncio
import socket

import pytest

from backend.main import install_disconnect_filter


class ResetSocket:
    """Proxy whose shutdown fails like a Windows socket already reset by the browser (WinError 10054)."""

    def __init__(self, sock):
        self._sock = sock

    def shutdown(self, how):
        raise ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host')

    def __getattr__(self, name):
        return getattr(self._sock, name)


async def close_with_reset_socket() -> list[dict]:
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    install_disconnect_filter(loop)
    accepted = asyncio.Event()
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        accepted.set()
        await asyncio.sleep(1)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    client = socket.create_connection(server.sockets[0].getsockname()[:2])
    await accepted.wait()
    transport = writers[0].transport
    transport._sock = ResetSocket(transport._sock)
    transport.close()
    await asyncio.sleep(0.2)
    client.close()
    server.close()
    return reported


@pytest.mark.asyncio
async def test_browser_disconnect_reset_is_not_reported_as_an_error():
    reported = await close_with_reset_socket()
    assert not [c for c in reported if isinstance(c.get('exception'), ConnectionResetError)]


@pytest.mark.asyncio
async def test_other_loop_errors_still_reach_the_previous_handler():
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    install_disconnect_filter(loop)
    loop.call_exception_handler({'message': 'Exception in callback something_else()', 'exception': ConnectionResetError()})
    loop.call_exception_handler({'message': 'Task exception was never retrieved', 'exception': ValueError('boom')})
    assert [type(c['exception']) for c in reported] == [ConnectionResetError, ValueError]
