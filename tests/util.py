"""Shared helpers for wsmini tests."""

import socket

from wsmini.connection import WSConnection, STATE_OPEN


def make_pair(**kwargs):
    """Return (client, server) OPEN WSConnections over a socketpair.

    Keyword arguments may be prefixed with ``client_`` / ``server_`` to
    configure only one side; unprefixed kwargs apply to both.
    """
    client_opts = {"is_client": True, "state": STATE_OPEN}
    server_opts = {"is_client": False, "state": STATE_OPEN}
    for key, value in kwargs.items():
        if key.startswith("client_"):
            client_opts[key[len("client_"):]] = value
        elif key.startswith("server_"):
            server_opts[key[len("server_"):]] = value
        else:
            client_opts[key] = value
            server_opts[key] = value
    a, b = socket.socketpair()
    return WSConnection(a, **client_opts), WSConnection(b, **server_opts)


def raw_pair(**kwargs):
    """Return (raw_socket, conn): a WSConnection plus the raw peer end,
    for tests that need to hand-craft bytes on the wire."""
    client, server = make_pair(**kwargs)
    return client.sock, server
