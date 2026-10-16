"""Loopback ingress relay so the review page is reachable while the gateway stays egress-free.

Docker does not publish ports for containers that are only on an `internal: true` network, and
attaching the gateway to a normal network would give it internet egress. This relay sits on both
networks and copies bytes to one fixed target. It parses nothing, stores nothing, and never logs.
With --tls-cert/--tls-key it terminates TLS, for LAN clients (compose.lan.yaml).
"""

import argparse
import asyncio
import ssl

CHUNK = 65536


async def pipe(reader, writer, idle):
    try:
        while data := await asyncio.wait_for(reader.read(CHUNK), idle):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


def tls_context(cert, key):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


async def serve(listen_host, listen_port, target_host, target_port, idle=120, limit=32, tls=None):
    slots = asyncio.Semaphore(limit)

    async def handle(client_reader, client_writer):
        if slots.locked():
            client_writer.close()
            return
        async with slots:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(target_host, target_port), 10
                )
            except Exception:
                client_writer.close()
                return
            await asyncio.gather(
                pipe(client_reader, upstream_writer, idle),
                pipe(upstream_reader, client_writer, idle),
            )

    return await asyncio.start_server(
        handle, listen_host, listen_port, ssl=tls, ssl_handshake_timeout=10 if tls else None
    )


def address(value):
    host, _, port = value.rpartition(":")
    return host, int(port)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", type=address, default=("0.0.0.0", 8000))
    parser.add_argument("--target", type=address, default=("gateway", 8000))
    parser.add_argument(
        "--tls-cert", help="PEM certificate; terminates TLS when set with --tls-key"
    )
    parser.add_argument("--tls-key", help="PEM private key")
    args = parser.parse_args()
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together")
    tls = tls_context(args.tls_cert, args.tls_key) if args.tls_cert else None

    async def run():
        server = await serve(*args.listen, *args.target, tls=tls)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
