"""Thread-per-connection WebSocket server (stdlib only)."""

import socket
import threading

from .connection import WSConnection, SocketReader, STATE_OPEN
from .exceptions import HandshakeError, WebSocketError
from .handshake import server_handshake


class WSServer:
    """Listens for WebSocket connections and dispatches them to threads.

    ``handler`` is called with one OPEN :class:`WSConnection` per client,
    on its own daemon thread. Connection-level keyword arguments
    (``max_message_size``, ``ping_interval``, ...) are forwarded to each
    WSConnection.
    """

    def __init__(self, host, port, handler, subprotocols=None,
                 host_header=None, **conn_options):
        self.handler = handler
        self.subprotocols = subprotocols
        self.host_header = host_header
        self.conn_options = conn_options
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET,
                                  socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(128)
        self.address = self._listener.getsockname()
        self._shutdown = threading.Event()
        self._thread = None
        self._clients = []
        self._clients_lock = threading.Lock()

    def serve_forever(self):
        """Accept loop; run in the calling thread until shutdown()."""
        self._listener.settimeout(0.2)
        while not self._shutdown.is_set():
            try:
                client_sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._run_client,
                                      args=(client_sock,), daemon=True)
            thread.start()

    def start(self):
        """Spawn the accept loop on a background thread; return self."""
        self._thread = threading.Thread(target=self.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def shutdown(self):
        self._shutdown.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._clients_lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    def _run_client(self, client_sock):
        reader = SocketReader(client_sock)
        try:
            chosen = server_handshake(reader, client_sock,
                                      host_header=self.host_header,
                                      subprotocols=self.subprotocols)
        except HandshakeError:
            try:
                client_sock.close()
            except OSError:
                pass
            return
        except OSError:
            return
        conn = WSConnection(client_sock, reader=reader, is_client=False,
                            state=STATE_OPEN, subprotocol=chosen,
                            **self.conn_options)
        with self._clients_lock:
            self._clients.append(conn)
        try:
            self.handler(conn)
        except WebSocketError:
            pass  # normal protocol/close outcomes already handled
        except Exception:
            try:
                conn.close(1011, "internal error")
            except Exception:
                pass
        finally:
            with self._clients_lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            if conn.state != "CLOSED":
                try:
                    conn.close()
                except Exception:
                    pass


def serve(host, port, handler, subprotocols=None, start=True,
          **conn_options):
    """Create a :class:`WSServer`; by default also starts its accept loop
    on a background thread. Returns the server (use ``.address`` for the
    bound address, ``.shutdown()`` to stop)."""
    server = WSServer(host, port, handler, subprotocols=subprotocols,
                      **conn_options)
    if start:
        server.start()
    return server
