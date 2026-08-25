# Codebase Exploration: Quality Hardening (bugs, improvements, test coverage, CI/CD)

**Generated:** 2026-08-25
**Feature:** Audit codegraph-lsp for correctness bugs, improvement opportunities, missing test coverage, and missing CI/CD — no tests or CI exist today.

## Overview

`codegraph` is a small (~1040 line) single-package Python 3.12 CLI. It drives external LSP servers (`pyright-langserver`, `gopls`) over JSON-RPC/stdio to extract a symbol graph, collapses it to a file-coupling graph, and runs Leiden community detection (via `igraph`/`leidenalg`) to report coupling clusters. Five source files:

- `src/codegraph/lsp.py` (215 lines) — synchronous JSON-RPC/stdio client, one background reader thread per server process.
- `src/codegraph/extract.py` (290 lines) — drives `documentSymbol`/`references`/`callHierarchy` per file, flattens hierarchical symbols, builds a `SymbolGraph`.
- `src/codegraph/graph.py` (362 lines) — builds igraph file- and symbol-level graphs, runs Leiden, formats reports.
- `src/codegraph/__main__.py` (174 lines) — argparse CLI: full extraction, `--diff` incremental mode, report/graph persistence.
- `src/codegraph/__init__.py` — empty.
- `scripts/visualise.py` — standalone script, reads a report JSON and emits Graphviz dot to stdout.

There is exactly one commit (`00d1093 done`) — this looks like freshly generated/scaffolded code that has never been iterated on. **Zero test files exist anywhere in the repo, and there is no CI config, no `.gitignore`, and no lint/type-check tooling wired into `pyproject.toml`** (pyright config exists but is IDE-only, not enforced by any script or CI).

## Bugs & Correctness Risks

### Concurrency: `LSPClient._pending` is mutated from two threads without a shared lock
`lsp.py:39-68` — `self._lock` (`threading.Lock`) protects only the `self._id` increment in `request()`. The dict write `self._pending[rid] = {...}` (main thread) and the reader thread's mutation of that same entry plus `self._pending.pop(rid)` calls in `request()`/`_dispatch()` (`lsp.py:60,65,203-210`) happen without lock coordination between the two threads. In CPython this mostly "works" because dict get/set are effectively atomic under the GIL, but it's fragile: there's no `try/except KeyError` around the pop-then-mutate sequence, and a response arriving in the exact window between a timeout's `pop()` and the reader thread's `if msg["id"] in self._pending` check is a silent drop, not a crash — but it means the timeout path can race the response path with no defined outcome. Nothing currently tests concurrent request/timeout behavior.

### Incremental `--diff` mode drops valid cross-file edges
`__main__.py:78-91` (`_merge_incremental`) — when re-extracting only the changed files listed via `--diff`, the merge keeps edges from the base graph only if **neither** endpoint's URI is in `changed_uris`, then appends the freshly extracted `delta.edges`. Delta extraction only re-runs `_extract_file` (and therefore `references`/`callHierarchy` lookups) for the changed files themselves (`extract.py:129-148`, filtered by `f.suffix in EXTENSIONS`). This means:
- Edges *from* unchanged file A *to* a symbol in changed file B are dropped by the filter (B is in `changed_uris`) and never re-added, because A is never re-extracted in delta mode.
- Only edges where the changed file is the *source* (B → something) get regenerated.

Net effect: after any `--diff` run, incoming coupling edges into changed files silently disappear from the persisted graph, understating coupling for files that were recently touched — exactly the files a user is most likely to care about. This is a functional correctness bug in the incremental mode, not just an edge case.

### `LSPClient.shutdown()` can raise past its own error handling
`lsp.py:115-123` — the `try/except Exception: pass` only wraps the `shutdown`/`exit` JSON-RPC exchange. The `finally` block's `self._proc.terminate()` then `self._proc.wait(timeout=5)` is unguarded: if the process doesn't exit within 5s, `wait()` raises `subprocess.TimeoutExpired`, which propagates out of `shutdown()` uncaught. In `__main__.py:62-65` (`_stop_clients`), this would abort shutdown of any remaining clients in the loop and propagate out of the `finally` block in `main()`, potentially masking the real exception/report if one was in flight. There's also no `kill()` fallback if `terminate()` doesn't take.

### Stray/dead code suggesting an unfinished edit
`extract.py:272` — `if s.kind not in INTERESTING_KINDS:  # <-- add this filter` — the filter is already applied, but the comment reads like an inline instruction/TODO marker rather than documentation. Worth cleaning up regardless of intent; if it signals the filter was added without covering a related code path elsewhere, that should be double-checked (no other call site needed the same filter, as far as this audit found).

`extract.py:159-160` — `if p.name.endswith("_test.go") and language == "go": pass` — a no-op conditional (the comment says "keep tests," but there's no corresponding `continue`/exclusion anywhere for it to negate). Dead code that should either be removed or replaced with real logic if there was ever an intent to special-case Go test files.

### Silent broad exception handling can hide real failures
- `extract.py:190-191`, `232-235`, `249-252`, `256-257` — extraction wraps per-symbol reference/call-hierarchy lookups in bare `except Exception` and logs at `warning` (or swallows entirely for `_extract_calls`'s inner loop). Reasonable for resilience against flaky LSP servers, but combined with zero tests, there's no way to know today which failure modes are actually hit in practice (timeouts vs. malformed responses vs. server crashes) or whether the fallback (reference-only edges) actually degrades as described in the README.
- `lsp.py:118-120` — `shutdown()`'s outer swallow means a server that fails to respond to `shutdown` never surfaces that fact anywhere except via whatever logging level is active.

### CLI argument/UX edge cases with no coverage
- `__main__.py:146-155` — `--diff` requires `--load-graph` to already exist, checked once; but there's no validation that the files passed to `--diff` actually exist or are within `root`. `extractor.extract(files=changed)` will call `fpath.read_text()` inside `_extract_file`, which handles `OSError` and just logs+skips (`extract.py:173-177`) — meaning a typo'd `--diff` path silently no-ops instead of erroring, which could look like "no changes detected" to a user.
- `__main__.py:137-140` — if neither `pyright-langserver` nor `gopls` is on `PATH`, the tool exits(1) with a message; this path plus the "directory doesn't exist" path (`__main__.py:133-135`) are the only two validated CLI failure modes, and neither is covered by any test.

## Improvement Opportunities

- **Type safety / linting are unenforced.** `pyproject.toml` only configures `pyright` for editor use (`basic` mode, Python 3.8 target pinned — inconsistent with `requires-python = ">=3.12"` in the same file). Nothing runs `pyright` as a check; there's no `ruff`/`black`/`mypy` dependency group and no lint step anywhere.
- **`pythonVersion = "3.8"` under `[tool.pyright]`** (pyproject.toml) contradicts `requires-python = ">=3.12"` in `[project]` — pyright will type-check against a much older stdlib/typing surface than the project actually targets, which can hide real 3.12-specific issues or produce false positives on newer syntax.
- **Generated artifacts are committed to the repo root**: `graph.json` (115KB), `report.json` (26KB), and `codegraph.svg` (8KB) are all checked into version control at the top level, alongside no `.gitignore` at all. These look like sample/demo outputs from running the tool against itself, but nothing distinguishes "checked-in example" from "accidentally committed local run output."
- **No dependency pinning for the LSP server binaries** — `pyright-langserver` and `gopls` are located via `shutil.which` (`__main__.py:39-47`) with no version check; behavior (especially call-hierarchy completeness, per the README's documented tradeoff) will silently vary across installed versions with no diagnostic surfaced beyond a debug log line.
- **`print(..., file=sys.stderr)` progress reporting** (`extract.py:146`) is mixed with the `logging` module used everywhere else — two different output mechanisms for user-facing progress vs. diagnostics, which is a minor inconsistency but complicates capturing/testing CLI output.
- **`SymbolGraph.from_dict`/`to_dict` round-trip loses data**: `to_dict()` doesn't serialize `character`, and `from_dict()` reconstructs symbols with `kind=0` and `character=0` always (`extract.py:69-114`), which means every symbol loaded from a saved graph (`--load-graph`) reports `kind_name == "unknown"` and loses its real `kind`. Since `resolve_edges` (used by both `build_file_graph` indirectly and `build_symbol_graph`/`analyze_symbols`) filters on `s.kind in INTERESTING_KINDS` (`extract.py:272`, `graph.py:169,299`), **every symbol reloaded from a saved graph is excluded from symbol-level analysis** — `--load-graph` + `--diff` incremental runs would produce an essentially empty symbol-level graph for all the "kept" (unchanged) symbols, even though file-level coupling (which doesn't filter by kind in `build_file_graph`) still works. This looks like the most impactful correctness bug found: symbol communities/cohesion reported after an incremental run reflect only the newly re-extracted files, not the full picture, with no warning to the user.
- **No `__version__` / no `--version` flag** on the CLi despite being a distributable script (`project.scripts` entry point).

## Test Coverage

Current state: **no test files exist at all** (confirmed via repo-wide search). No `tests/` directory, no `pytest`/`unittest` usage, no fixtures, no CI to run them even if they existed.

Natural seams for tests, given the module boundaries observed:
- **`lsp.py`**: `_read_loop`'s buffer/framing parser (`_HEADER_SEP`, `Content-Length` handling, partial reads, multiple messages in one chunk) is pure-ish logic that could be tested by feeding bytes directly to `LSPClient` internals or via a fake subprocess (e.g., a `subprocess.Popen`-like stub or a real toy script echoing framed JSON-RPC over stdio). `_dispatch` request/response/notification routing is also unit-testable without a real LSP server.
- **`extract.py`**: `_flatten_symbols` (hierarchical → flat qualified names), `resolve_edges` (line-based nearest-enclosing-symbol resolution), and `SymbolGraph.to_dict`/`from_dict` round-tripping are pure functions/methods over plain data — ideal for unit tests with hand-built `DocumentSymbol`-shaped dicts, and would have caught the `kind`/`character` round-trip bug above immediately.
- **`graph.py`**: `build_file_graph`, `build_symbol_graph`, `analyze`, and `analyze_symbols` all take a `SymbolGraph` and are deterministic given `seed=42` — these are natural candidates for tests asserting community/modularity output on small hand-constructed graphs, without needing igraph/leidenalg to be mocked (they're real, fast, pure-Python-interfaced deps).
- **`__main__.py`**: argument parsing, the `--diff` validation branch, and `_merge_incremental`'s set-based filtering logic are all testable without spawning real LSP servers, using fake `SymbolGraph`/`Symbol` fixtures.
- **Integration-level**: nothing in the repo exercises a real or fake LSP server end-to-end; the README documents specific server-dependent behaviors (pyright's spotty call-hierarchy, graceful fallback to reference-only edges) that are currently unverified by any test.

## CI/CD

Current state: **no CI/CD configuration exists** — no `.github/workflows/`, no other CI provider config, no `.gitignore`, no pre-commit config. `uv.lock` is present, implying `uv` is the intended dependency manager, and `pyproject.toml` already defines a `[project.scripts]` entry point (`codegraph`) and a hatchling build backend, both of which are typical prerequisites for a publish/build CI job.

Nothing to describe further here since none exists yet — this is a pure gap, not a pattern to preserve.

## Cross-Cutting Patterns

- **Dataclasses as the core data model** (`Symbol`, `Edge`, `SymbolGraph`, `CouplingPair`, `Community`, `AnalysisResult`, `ServerCapabilities`) — consistent style across all modules; any new data-carrying types should follow the same `@dataclass` (often `frozen=True` for value objects like `Symbol`) convention.
- **`logging` module used for diagnostics, `print(..., file=sys.stderr)` for user-facing progress/errors, and stdout reserved for the final human-readable report** (`print_summary`) or JSON (when `-o` is passed). Tests/tooling should respect this split rather than asserting against combined output.
- **Defensive/graceful-degradation style throughout**: capability checks (`self.capabilities.call_hierarchy`) before using optional LSP features, broad `except Exception` around per-symbol network calls, `.get()` with defaults when parsing server responses. This is a deliberate tradeoff (per the README) favoring "always produce a graph, even if less precise" over failing loudly — any bug fixes or new tests should preserve that philosophy rather than making the tool newly brittle, while still surfacing the specific issues found above (especially the silent data loss in `--diff`/`--load-graph` mode) more visibly to users.
- **`root`-relative path handling repeated in three places** (`graph.py:_relative`, `Symbol.file_path`, `analyze_symbols`'s inline URI-stripping) — the `file://` prefix stripping and root-relativization logic is duplicated with slightly different implementations rather than shared via one helper.
