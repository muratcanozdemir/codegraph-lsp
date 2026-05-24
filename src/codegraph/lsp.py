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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HEADER_SEP = b"\r\n\r\n"
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
        with self._lock:
            self._id += 1
            rid = self._id
        event = threading.Event()
        self._pending[rid] = {"event": event, "result": None, "error": None}
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if not event.wait(timeout=timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")
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
        result = self.request("callHierarchy/prepareCallHierarchy", {
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
        stdin.write(header + body)
        stdin.flush()

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        buf = b""
        while True:
            chunk = stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            while buf:
                if _HEADER_SEP not in buf:
                    break
                header_end = buf.index(_HEADER_SEP)
                header_block = buf[:header_end].decode()
                content_length = None
                for line in header_block.split("\r\n"):
                    if line.startswith(_CONTENT_LENGTH):
                        content_length = int(line.split(":", 1)[1].strip())
                if content_length is None:
                    buf = buf[header_end + len(_HEADER_SEP):]
                    continue
                body_start = header_end + len(_HEADER_SEP)
                body_end = body_start + content_length
                if len(buf) < body_end:
                    break  # incomplete body, wait for more data
                body = buf[body_start:body_end]
                buf = buf[body_end:]
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        # response to our request
        if "id" in msg and msg["id"] in self._pending:
            entry = self._pending[msg["id"]]
            entry["result"] = msg.get("result")
            entry["error"] = msg.get("error")
            entry["event"].set()
            return
        # server-initiated request — respond with null
        if "id" in msg and "method" in msg:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            return
        # notification — ignore (diagnostics, logs, etc.)
