from __future__ import annotations

import json
import subprocess
import threading

import pytest

from codegraph.lsp import LSPClient, ServerCapabilities


class FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass


class FakeStdout:
    """Yields pre-scripted chunks, then signals EOF with b""."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def read1(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProc:
    def __init__(self, stdout_chunks: list[bytes] | None = None):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_chunks or [])
        self.terminated = False
        self.killed = False
        self._wait_calls = 0
        self.wait_side_effect: Exception | None = None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> None:
        self._wait_calls += 1
        if self.wait_side_effect is not None and self._wait_calls == 1:
            raise self.wait_side_effect


def make_client(stdout_chunks: list[bytes] | None = None) -> LSPClient:
    """Build an LSPClient without spawning a real process or handshaking."""
    client = object.__new__(LSPClient)
    client.language_id = "python"
    client._root_uri = "file:///root"
    client._id = 0
    client._pending = {}
    client._lock = threading.Lock()
    client.capabilities = ServerCapabilities()
    client._proc = FakeProc(stdout_chunks)
    return client


def _frame(msg: dict) -> bytes:
    body = json.dumps(msg).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def test_dispatch_resolves_pending_request():
    client = make_client()
    event = threading.Event()
    client._pending[1] = {"event": event, "result": None, "error": None}

    client._dispatch({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    assert event.is_set()
    assert client._pending[1]["result"] == {"ok": True}
    assert client._pending[1]["error"] is None


def test_dispatch_ignores_response_for_already_popped_id():
    client = make_client()
    # No entry for id=1 (e.g. it already timed out and was popped).
    client._dispatch({"jsonrpc": "2.0", "id": 1, "result": {}})  # must not raise


def test_dispatch_responds_null_to_server_initiated_request():
    client = make_client()
    sent = []
    client._send = lambda msg: sent.append(msg)

    client._dispatch({"jsonrpc": "2.0", "id": 7, "method": "window/showMessageRequest"})

    assert sent == [{"jsonrpc": "2.0", "id": 7, "result": None}]


def test_dispatch_ignores_notifications():
    client = make_client()
    client._send = lambda msg: pytest.fail("must not respond to notifications")

    client._dispatch({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {}})


def test_read_loop_parses_single_message_split_across_chunks():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    framed = _frame(msg)
    # split mid-header and mid-body to exercise the partial-buffer paths
    chunks = [framed[:10], framed[10:30], framed[30:]]
    client = make_client(stdout_chunks=chunks)
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == [msg]


def test_read_loop_parses_multiple_messages_in_one_chunk():
    msg_a = {"jsonrpc": "2.0", "id": 1, "result": 1}
    msg_b = {"jsonrpc": "2.0", "id": 2, "result": 2}
    client = make_client(stdout_chunks=[_frame(msg_a) + _frame(msg_b)])
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == [msg_a, msg_b]


def test_read_loop_skips_malformed_json_body():
    body = b"not json"
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    good = _frame({"jsonrpc": "2.0", "id": 1, "result": None})
    client = make_client(stdout_chunks=[header + body + good])
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == [{"jsonrpc": "2.0", "id": 1, "result": None}]


def test_request_times_out_and_cleans_up_pending_entry():
    client = make_client()
    client._send = lambda msg: None  # nobody ever replies

    with pytest.raises(TimeoutError):
        client.request("textDocument/references", timeout=0.05)

    assert client._pending == {}


def test_request_raises_runtime_error_on_lsp_error_response():
    client = make_client()

    def fake_send(msg):
        client._dispatch({"jsonrpc": "2.0", "id": msg["id"], "error": {"message": "boom"}})

    client._send = fake_send

    with pytest.raises(RuntimeError, match="boom"):
        client.request("textDocument/references", timeout=1)


def test_shutdown_kills_process_when_terminate_times_out():
    client = make_client()
    client._proc.wait_side_effect = subprocess.TimeoutExpired(cmd="server", timeout=5)
    client.request = lambda *a, **k: None
    client.notify = lambda *a, **k: None

    client.shutdown()

    assert client._proc.terminated
    assert client._proc.killed
