"""Minimal LSP client over JSON-RPC/stdio.

Talks to any language server that implements the standard protocol.
No external dependencies — just subprocess, json, threading.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CONTENT_LENGTH = "Content-Length"


@dataclass
class ServerCapabilities:
    document_symbol: bool = False
    references: bool = False
    call_hierarchy: bool = False
    workspace_symbol: bool = False


class LSPClient:
    """Synchronous LSP client. One instance per language server process."""

    def __init__(self, cmd: list[str], root: Path, language_id: str) -> None:
        self.language_id = language_id
        self._root_uri = root.resolve().as_uri()
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        # stdin is written from both the caller thread (requests) and the
        # reader thread (replies to server requests); serialize the writes.
        self._write_lock = threading.Lock()
        self._closed = False
        self.capabilities = ServerCapabilities()

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()

    # -- public API ----------------------------------------------------------

    def request(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        event = threading.Event()
        with self._lock:
            if self._closed:
                raise ConnectionError(f"{self.language_id} language server has exited")
            self._id += 1
            rid = self._id
            self._pending[rid] = {"event": event, "result": None, "error": None}
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except OSError:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")
        with self._lock:
            entry = self._pending.pop(rid)
        if entry["error"]:
            raise RuntimeError(f"LSP error on {method}: {entry['error']}")
        return entry["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def open_document(self, uri: str, text: str) -> None:
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": self.language_id,
                "version": 1,
                "text": text,
            },
        })

    def close_document(self, uri: str) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def document_symbols(self, uri: str) -> list[dict]:
        result = self.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        return result or []

    def references(self, uri: str, line: int, character: int) -> list[dict]:
        result = self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": False},
        })
        return result or []

    def prepare_call_hierarchy(self, uri: str, line: int, character: int) -> list[dict]:
        if not self.capabilities.call_hierarchy:
            return []
        result = self.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        return result or []

    def outgoing_calls(self, item: dict) -> list[dict]:
        if not self.capabilities.call_hierarchy:
            return []
        result = self.request("callHierarchy/outgoingCalls", {"item": item})
        return result or []

    def shutdown(self) -> None:
        try:
            self.request("shutdown", timeout=5)
            self.notify("exit")
        except Exception:
            pass
        finally:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)

    # -- protocol internals --------------------------------------------------

    def _initialize(self) -> None:
        result = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "references": {},
                    "callHierarchy": {},
                },
                "workspace": {
                    "symbol": {"dynamicRegistration": False},
                    "workspaceFolders": True,
                },
            },
            "workspaceFolders": [{"uri": self._root_uri, "name": "workspace"}],
        })
        caps = result.get("capabilities", {})
        self.capabilities = ServerCapabilities(
            document_symbol="documentSymbolProvider" in caps,
            references="referencesProvider" in caps,
            call_hierarchy="callHierarchyProvider" in caps,
            workspace_symbol="workspaceSymbolProvider" in caps,
        )
        log.info(
            "LSP %s initialized — symbols=%s refs=%s calls=%s",
            self.language_id,
            self.capabilities.document_symbol,
            self.capabilities.references,
            self.capabilities.call_hierarchy,
        )
        self.notify("initialized")

    def _send(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        header = f"{_CONTENT_LENGTH}: {len(body)}\r\n\r\n".encode()
        stdin = self._proc.stdin
        assert stdin is not None
        with self._write_lock:
            stdin.write(header + body)
            stdin.flush()

    def _read_loop(self) -> None:
        try:
            self._read_messages()
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        """Server stream ended: wake every waiter now instead of letting each time out."""
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
        for entry in pending:
            entry["error"] = {"message": "language server exited"}
            entry["event"].set()

    def _read_messages(self) -> None:
        """Read framed messages: header lines, a blank line, then exactly
        Content-Length bytes. readline()/read(n) on the buffered pipe keep this
        linear in message size (no rescanning of a growing buffer)."""
        stdout = self._proc.stdout
        assert stdout is not None
        while True:
            content_length = None
            while True:
                line = stdout.readline()
                if not line:
                    return  # EOF
                line = line.rstrip(b"\r\n")
                if not line:
                    break  # end of headers
                name, _, value = line.decode("ascii", "replace").partition(":")
                if name.strip().lower() == _CONTENT_LENGTH.lower():
                    try:
                        content_length = int(value.strip())
                    except ValueError:
                        content_length = None
            if content_length is None:
                continue  # headers without a usable length: skip to the next block
            body = stdout.read(content_length)
            if len(body) < content_length:
                return  # EOF mid-body
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            # Server-initiated request. Its id comes from the server's own
            # numbering and can equal one of our pending ids, so it must be
            # handled before response matching.
            if "id" in msg:
                self._send({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "result": self._server_request_result(msg),
                })
            # otherwise a notification — ignore (diagnostics, logs, etc.)
            return
        # response to our request
        if "id" in msg:
            with self._lock:
                entry = self._pending.get(msg["id"])
            if entry is not None:
                entry["result"] = msg.get("result")
                entry["error"] = msg.get("error")
                entry["event"].set()

    @staticmethod
    def _server_request_result(msg: dict) -> Any:
        # workspace/configuration must return one value per requested item;
        # null entries mean "use your defaults".
        if msg["method"] == "workspace/configuration":
            items = (msg.get("params") or {}).get("items", [])
            return [None] * len(items)
        return None
