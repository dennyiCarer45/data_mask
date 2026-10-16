import asyncio
import importlib.util
import ssl
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ui_relay", Path(__file__).parents[1] / "scripts" / "ui_relay.py"
)
ui_relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui_relay)


async def test_relay_forwards_bytes_both_ways_to_fixed_target():
    async def echo(reader, writer):
        writer.write(b"upstream:" + await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = upstream.sockets[0].getsockname()[1]
    relay = await ui_relay.serve("127.0.0.1", 0, "127.0.0.1", target_port, idle=5)
    relay_port = relay.sockets[0].getsockname()[1]
    async with upstream, relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"synthetic")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 5) == b"upstream:synthetic"
        writer.close()


async def test_relay_terminates_tls_for_lan_clients(tmp_path):
    pytest.importorskip("cryptography")
    cert_spec = importlib.util.spec_from_file_location(
        "make_lan_cert", Path(__file__).parents[1] / "scripts" / "make_lan_cert.py"
    )
    make_lan_cert = importlib.util.module_from_spec(cert_spec)
    cert_spec.loader.exec_module(make_lan_cert)
    _, cert_pem, key_pem = make_lan_cert.make_certificate(["127.0.0.1"])
    (tmp_path / "cert.pem").write_bytes(cert_pem)
    (tmp_path / "key.pem").write_bytes(key_pem)

    async def echo(reader, writer):
        writer.write(b"upstream:" + await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    tls = ui_relay.tls_context(tmp_path / "cert.pem", tmp_path / "key.pem")
    relay = await ui_relay.serve(
        "127.0.0.1", 0, "127.0.0.1", upstream.sockets[0].getsockname()[1], tls=tls
    )
    client = ssl.create_default_context(cafile=tmp_path / "cert.pem")
    async with upstream, relay:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", relay.sockets[0].getsockname()[1], ssl=client
        )
        writer.write(b"synthetic")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 5) == b"upstream:synthetic"
        writer.close()
        # A plaintext client gets nothing from the TLS listener.
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", relay.sockets[0].getsockname()[1]
        )
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"upstream" not in await asyncio.wait_for(reader.read(100), 15)
        writer.close()


async def test_relay_closes_client_when_target_is_down():
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    relay = await ui_relay.serve("127.0.0.1", 0, "127.0.0.1", dead_port)
    async with relay:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", relay.sockets[0].getsockname()[1]
        )
        assert await asyncio.wait_for(reader.read(100), 5) == b""
        writer.close()
