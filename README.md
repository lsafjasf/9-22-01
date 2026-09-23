# wsmini

A small, stdlib-only WebSocket (RFC 6455) client/server library for
Python 3. No TLS (`wss://` is rejected by design); no third-party
dependencies.

## Usage

```python
from wsmini import serve, connect

# Server: one handler thread per connection.
def echo(conn):
    while True:
        msg = conn.recv_message()   # str for text, bytes for binary,
        if msg is None:             # None = peer closed cleanly
            break
        conn.send_message(msg)

server = serve("127.0.0.1", 9000, echo, ping_interval=30)

# Client
conn = connect("ws://127.0.0.1:9000/chat")
conn.send_message("hello")
print(conn.recv_message())
conn.close(1000, "done")
server.shutdown()
```

`WSConnection` options (accepted by both `serve(...)` and `connect(...)`):
`max_message_size` (default 16 MiB), `max_fragments` (default 1024),
`ping_interval` / `ping_timeout` (heartbeat, disabled by default),
`close_timeout` (default 5 s).

## Protocol behavior and where it is tested

Run everything (70 tests):

    python3 -m unittest discover -s tests -v

| Behavior | Implementation | Tests |
| --- | --- | --- |
| Server handshake: GET only, HTTP/1.1+, `Upgrade`/`Connection` tokens, base64 16-byte `Sec-WebSocket-Key`, `Sec-WebSocket-Version: 13` (else 426 + version header), 101 + `Sec-WebSocket-Accept` | `wsmini/handshake.py` `server_handshake` | `tests/test_handshake.py` |
| Client handshake: sends key, requires 101, verifies accept token and upgrade headers, rejects unrequested subprotocols | `wsmini/handshake.py` `client_handshake` | `tests/test_handshake.py` |
| Masking: clients mask, servers don't; wrong masking from either side is a 1002 protocol error | `wsmini/framing.py`, `WSConnection._check_frame_shape` | `tests/test_framing.py` |
| Length encoding: 7-bit / 16-bit / 64-bit lengths, high-bit-set 64-bit length rejected | `wsmini/framing.py` `encode_frame`/`read_frame` | `tests/test_framing.py` |
| Fragmentation: reassembly of continuation frames, interleaved control frames allowed, control frames never fragmented | `WSConnection._handle_frame` | `tests/test_fragmentation.py` |
| Close handshake: bidirectional, carries code + reason, echoes peer's close, no data frames after close | `WSConnection.close` / `_handle_control` | `tests/test_close.py` |
| State machine: CONNECTING → OPEN → CLOSING → CLOSED | `wsmini/connection.py` | `tests/test_connection.py`, `tests/test_close.py` |
| Abnormal termination: heartbeat timeout (1006) and fatal frame violations (close with 1002/1007/1008/1009, then drop) | `WSConnection._on_timeout_tick` / `_fail_protocol` | `tests/test_connection.py` |
| Limits: `max_message_size` → close 1009, `max_fragments` → close 1008; single-frame length checked before reading the payload | `read_frame(max_payload=...)`, `WSConnection._check_message_limits` | `tests/test_connection.py` |
| End-to-end over TCP: echo, 200 KB fragmented message, concurrent clients, live heartbeat | `wsmini/server.py`, `wsmini/client.py` | `tests/test_integration.py` |

## Compatibility with the previous snapshot

The pre-existing modules (`exceptions.py`, `framing.py`, `handshake.py`)
keep their public API; changes are additive only:

- `framing.Frame` gained a `masked` attribute (new trailing constructor
  parameter, defaults to `False`).
- `framing.read_frame(reader, max_payload=None)` gained an optional
  keyword; `read_frame(reader)` behaves exactly as before.
- `handshake.client_handshake(..., reader=None)` gained an optional
  keyword; omitting it keeps the old `sock.makefile("rb")` behavior.
- `handshake.server_handshake` now also sends a 400 response when the
  `Upgrade`/`Connection` headers are missing (previously it raised
  without replying); the `HandshakeError` contract is unchanged.

The names `wsmini/__init__.py` always advertised (`WSConnection`,
`serve`, `connect`, `STATE_*`) now exist and are the supported surface.
New modules: `connection.py`, `server.py`, `client.py`.

### API notes for callers

- `recv_message(timeout=None)` returns `str`/`bytes`, or `None` on a
  clean peer close (inspect `conn.close_code` / `conn.close_reason`).
  Raises `TimeoutError` on application timeout, `ProtocolError` on fatal
  frame violations, `CloseError` on abnormal loss (incl. heartbeat
  timeout) or when called on a closed connection.
- `send_message(data, fragment_size=None)` only works while OPEN; after
  a close frame is sent or received it raises `WebSocketError`.
- `close(code=1000, reason="")` performs the full bidirectional
  handshake (send close, wait up to `close_timeout` for the peer's
  close, close the socket) and is idempotent.
- Heartbeat is opt-in via `ping_interval`; while waiting in
  `recv_message`, pings are answered automatically and any inbound frame
  counts as liveness.
