from __future__ import annotations

import io
import json
import subprocess
import threading
import time

import pytest

from codegraph.lsp import LSPClient, ServerCapabilities


class FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass


class _ChunkedRaw(io.RawIOBase):
    """Raw pipe that delivers pre-scripted chunks, then EOF."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        if not self._chunks:
            return 0
        chunk = self._chunks.pop(0)
        n = min(len(buf), len(chunk))
        buf[:n] = chunk[:n]
        if n < len(chunk):
            self._chunks.insert(0, chunk[n:])
        return n


def FakeStdout(chunks: list[bytes]) -> io.BufferedReader:  # noqa: N802 - stands in for a class
    # Same type as Popen's stdout, so the reader is exercised for real.
    return io.BufferedReader(_ChunkedRaw(chunks))


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
    client._write_lock = threading.Lock()
    client._closed = False
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


def test_dispatch_server_request_with_colliding_id_does_not_complete_our_request():
    # The server numbers its own requests; id 1 can be ours *and* theirs.
    client = make_client()
    sent = []
    client._send = sent.append
    event = threading.Event()
    client._pending[1] = {"event": event, "result": None, "error": None}

    client._dispatch({"jsonrpc": "2.0", "id": 1, "method": "client/registerCapability"})

    assert not event.is_set()
    assert sent == [{"jsonrpc": "2.0", "id": 1, "result": None}]


def test_dispatch_answers_workspace_configuration_with_one_entry_per_item():
    client = make_client()
    sent = []
    client._send = sent.append

    client._dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "workspace/configuration",
        "params": {"items": [{"section": "python"}, {"section": "python.analysis"}]},
    })

    assert sent == [{"jsonrpc": "2.0", "id": 3, "result": [None, None]}]


def test_read_loop_eof_fails_pending_requests_immediately():
    client = make_client(stdout_chunks=[])  # server stream ends at once
    event = threading.Event()
    client._pending[1] = {"event": event, "result": None, "error": None}

    client._read_loop()

    assert event.is_set()
    assert client._pending[1]["error"] == {"message": "language server exited"}


def test_request_after_server_exit_fails_fast():
    client = make_client(stdout_chunks=[])
    client._read_loop()

    with pytest.raises(ConnectionError):
        client.request("textDocument/references", timeout=60)

    assert client._pending == {}


def test_request_in_flight_when_server_exits_raises_instead_of_timing_out():
    client = make_client()
    client._send = lambda msg: client._fail_pending()  # server dies mid-request

    with pytest.raises(RuntimeError, match="exited"):
        client.request("textDocument/references", timeout=60)


def test_send_serializes_concurrent_writers():
    client = make_client()
    stdin = client._proc.stdin

    def write(data: bytes) -> None:
        # yield the GIL between bytes so unlocked writers would interleave
        for i in range(len(data)):
            stdin.written += data[i:i + 1]
            time.sleep(0)

    stdin.write = write
    msgs = [{"jsonrpc": "2.0", "id": i, "result": "x" * 50} for i in range(8)]
    threads = [threading.Thread(target=client._send, args=(m,)) for m in msgs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(stdin.written.split(b"Content-Length")[1:]) == sorted(
        _frame(m)[len(b"Content-Length"):] for m in msgs
    )


def test_read_loop_handles_large_body_split_into_many_chunks():
    msg = {"jsonrpc": "2.0", "id": 1, "result": ["x" * 100] * 2000}  # ~200 KB
    framed = _frame(msg)
    chunks = [framed[i:i + 4096] for i in range(0, len(framed), 4096)]
    client = make_client(stdout_chunks=chunks)
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == [msg]


def test_read_loop_accepts_extra_headers_and_any_header_case():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1}).encode()
    framed = (
        b"content-length: " + str(len(body)).encode() + b"\r\n"
        b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n" + body
    )
    client = make_client(stdout_chunks=[framed])
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == [{"jsonrpc": "2.0", "id": 1, "result": 1}]


def test_read_loop_stops_cleanly_on_truncated_body():
    framed = _frame({"jsonrpc": "2.0", "id": 1, "result": "abc"})
    client = make_client(stdout_chunks=[framed[:-2]])
    dispatched = []
    client._dispatch = dispatched.append

    client._read_loop()

    assert dispatched == []
    assert client._closed


def test_call_hierarchy_uses_spec_method_names():
    # The spec names are textDocument/prepareCallHierarchy and
    # callHierarchy/outgoingCalls; servers reject anything else as unknown.
    client = make_client()
    client.capabilities = ServerCapabilities(call_hierarchy=True)
    methods = []
    client.request = lambda method, params=None, timeout=60: methods.append(method) or []

    client.prepare_call_hierarchy("file:///a.py", 1, 2)
    client.outgoing_calls({"name": "f"})

    assert methods == ["textDocument/prepareCallHierarchy", "callHierarchy/outgoingCalls"]
