"""HTTP upgrade handshake for RFC 6455 (section 4)."""

import base64
import hashlib
import os
from urllib.parse import urlsplit

from .exceptions import HandshakeError

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(key):
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def new_client_key():
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _read_request_line_and_headers(reader):
    """Read one HTTP/1.x request block (CRLF terminated).

    Returns (method, target, version, headers-dict). Header lookups are
    case-insensitive; repeated headers are folded with commas.
    """
    lines = []
    while True:
        line = reader.readline()
        if line == b"":
            raise HandshakeError("connection closed during handshake")
        if line in (b"\r\n", b"\n"):
            break
        lines.append(line)

    if not lines:
        raise HandshakeError("empty handshake request")

    def decode(raw):
        try:
            return raw.rstrip(b"\r\n").decode("iso-8859-1")
        except Exception:
            raise HandshakeError("non-ASCII handshake line")

    request_line = decode(lines[0])
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise HandshakeError("malformed request line: %r" % request_line)
    method, target, version = parts

    headers = {}
    for raw in lines[1:]:
        line = decode(raw)
        if ":" not in line:
            raise HandshakeError("malformed header line: %r" % line)
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if name in headers:
            headers[name] = headers[name] + "," + value
        else:
            headers[name] = value
    return method, target, version, headers


def _check_upgrade_headers(headers):
    upgrade = headers.get("upgrade", "")
    if not any(token.strip().lower() == "websocket"
               for token in upgrade.split(",")):
        raise HandshakeError("missing or bad Upgrade: websocket header")
    connection = headers.get("connection", "")
    tokens = {t.strip().lower() for t in connection.split(",")}
    if "upgrade" not in tokens:
        raise HandshakeError("missing Connection: upgrade header")


def server_handshake(reader, sock, host_header=None, subprotocols=None):
    """Read and validate an opening handshake on the server side.

    On success returns the negotiated subprotocol (or None) and leaves the
    socket switched to WebSocket framing. On failure an HTTP 4xx response is
    written and :class:`HandshakeError` is raised.
    """
    method, target, version, headers = _read_request_line_and_headers(reader)

    if method != "GET":
        return _reject(sock, 405, "Method Not Allowed",
                       "WebSocket handshake requires GET")
    if not version.startswith("HTTP/"):
        return _reject(sock, 400, "Bad Request", "malformed HTTP version")
    try:
        major, minor = version[len("HTTP/"):].split(".")
        if (int(major), int(minor)) < (1, 1):
            return _reject(sock, 400, "Bad Request",
                           "WebSocket requires HTTP/1.1 or later")
    except ValueError:
        return _reject(sock, 400, "Bad Request", "malformed HTTP version")

    try:
        _check_upgrade_headers(headers)
    except HandshakeError as exc:
        return _reject(sock, 400, "Bad Request", str(exc))

    key = headers.get("sec-websocket-key")
    if not key:
        return _reject(sock, 400, "Bad Request",
                       "missing Sec-WebSocket-Key")
    try:
        raw_key = base64.b64decode(key.encode("ascii"), validate=True)
    except Exception:
        return _reject(sock, 400, "Bad Request",
                       "Sec-WebSocket-Key is not valid base64")
    if len(raw_key) != 16:
        return _reject(sock, 400, "Bad Request",
                       "Sec-WebSocket-Key must decode to 16 bytes")

    if headers.get("sec-websocket-version", "") != "13":
        body = b"unsupported WebSocket version"
        response = (
            b"HTTP/1.1 426 Upgrade Required\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        try:
            sock.sendall(response)
        except OSError:
            pass
        raise HandshakeError("unsupported Sec-WebSocket-Version")

    if host_header is not None and headers.get("host") != host_header:
        return _reject(sock, 400, "Bad Request", "Host header mismatch")

    chosen = None
    offered = headers.get("sec-websocket-protocol")
    if offered and subprotocols:
        wanted = {p.strip() for p in offered.split(",")}
        for proto in subprotocols:
            if proto in wanted:
                chosen = proto
                break

    response_lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Accept: " + compute_accept(key),
    ]
    if chosen:
        response_lines.append("Sec-WebSocket-Protocol: " + chosen)
    response = ("\r\n".join(response_lines) + "\r\n\r\n").encode("ascii")
    try:
        sock.sendall(response)
    except OSError as exc:
        raise HandshakeError("failed to send handshake response: %s" % exc)
    return chosen


def _reject(sock, status, reason, message):
    body = message.encode("utf-8")
    response = (
        ("HTTP/1.1 %d %s\r\n" % (status, reason)).encode("ascii")
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        + b"Connection: close\r\n\r\n" + body
    )
    try:
        sock.sendall(response)
    except OSError:
        pass
    raise HandshakeError(message)


def client_handshake(sock, url, subprotocols=None, extra_headers=None,
                     reader=None):
    """Perform the client opening handshake over a connected socket.

    ``reader`` optionally supplies the buffered reader wrapped around
    ``sock``; pass the same object to the connection layer afterwards so
    bytes read ahead during the handshake are not lost. Defaults to a
    fresh ``sock.makefile("rb")`` (previous behavior).

    Returns the negotiated subprotocol (or None). Raises HandshakeError if
    the server response is not a valid 101 with the correct accept token.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("ws", "wss"):
        raise HandshakeError("unsupported URL scheme: %s" % parsed.scheme)
    if parsed.scheme == "wss":
        raise HandshakeError("wss/TLS is not supported by wsmini")

    host = parsed.hostname
    if parsed.port is None:
        port = 80
    else:
        port = parsed.port
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = host + ("" if port == 80 else ":%d" % port)

    key = new_client_key()
    request_lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s" % host_header,
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
    ]
    if subprotocols:
        request_lines.append(
            "Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
    if extra_headers:
        for name, value in extra_headers.items():
            request_lines.append("%s: %s" % (name, value))
    request = ("\r\n".join(request_lines) + "\r\n\r\n").encode("utf-8")
    sock.sendall(request)

    if reader is None:
        reader = sock.makefile("rb")
    status_line = reader.readline()
    if status_line == b"":
        raise HandshakeError("server closed connection during handshake")
    try:
        status_text = status_line.rstrip(b"\r\n").decode("iso-8859-1")
    except Exception:
        raise HandshakeError("non-ASCII status line")
    parts = status_text.split(" ", 2)
    if len(parts) < 2 or parts[0] != "HTTP/1.1":
        raise HandshakeError("bad status line: %r" % status_text)
    try:
        status_code = int(parts[1])
    except ValueError:
        raise HandshakeError("bad status code in %r" % status_text)
    if status_code != 101:
        raise HandshakeError(
            "expected 101 Switching Protocols, got %d" % status_code)

    response_headers = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        text = line.rstrip(b"\r\n").decode("iso-8859-1")
        if ":" not in text:
            raise HandshakeError("malformed response header: %r" % text)
        name, value = text.split(":", 1)
        response_headers[name.strip().lower()] = value.strip()

    if response_headers.get("upgrade", "").lower() != "websocket":
        raise HandshakeError("missing Upgrade: websocket in response")
    conn_tokens = {t.strip().lower()
                   for t in response_headers.get("connection", "").split(",")}
    if "upgrade" not in conn_tokens:
        raise HandshakeError("missing Connection: upgrade in response")

    expected = compute_accept(key)
    if response_headers.get("sec-websocket-accept", "") != expected:
        raise HandshakeError("Sec-WebSocket-Accept mismatch")

    chosen = response_headers.get("sec-websocket-protocol")
    if chosen and subprotocols is not None:
        offered = {p.strip() for p in chosen.split(",")}
        valid = offered & set(subprotocols)
        if not valid:
            raise HandshakeError(
                "server selected unrequested subprotocol: %r" % chosen)
        chosen = next(iter(valid))
    elif chosen:
        chosen = chosen.split(",", 1)[0].strip()

    return chosen, reader, (host, port, path)
