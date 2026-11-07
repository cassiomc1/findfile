#!/usr/bin/env python3
"""
FindFile — fast terminal search for file names and file contents.

A Textual terminal interface on top of a small, bounded-memory, multithreaded
search engine. Content matches are reported line by line, with a highlighted
preview one keystroke away.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import io
import itertools
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Pattern, Set, Tuple

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
)

__version__ = "1.3.0"

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".tox",
    ".next",
    ".nuxt",
    "dist",
    "build",
    "target",
    "vendor",
    "Pods",
    ".terraform",
    ".bundle",
}

BINARY_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".psd", ".raw",
    # Audio / video
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac",
    # Binary documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz", ".zst", ".iso", ".dmg",
    # Executables / libraries
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".app",
    # Bytecode / databases
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".war", ".db", ".sqlite", ".sqlite3", ".parquet",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}

MAX_CONTENT_FILE_SIZE = 30 * 1024 * 1024  # 30 MB
MAX_MATCHES_PER_FILE = 40
MAX_TOTAL_RESULTS = 5000
BINARY_SNIFF_BYTES = 4096
READ_CHUNK_BYTES = 1024 * 1024
SNIPPET_MAX_CHARS = 200
BATCH_SIZE = 30
FLUSH_INTERVAL = 0.05
PROGRESS_INTERVAL = 0.05
MAX_THREADS = 32
PREVIEW_LINES_BEFORE = 15
PREVIEW_LINES_AFTER = 45
PREVIEW_MAX_LINE_CHARS = 2000
CLIPBOARD_TIMEOUT = 5
GLOB_CHARS = "*?["

# Table sort helpers, shared by the header handler and the status pill.
SORT_KEY_FUNCS: dict[str, Callable[["SearchResult"], object]] = {
    "idx": lambda result: result.idx,
    "type": lambda result: (
        result.match_type,
        result.file_name.lower(),
        result.rel_path.lower(),
    ),
    "name": lambda result: (
        result.file_name.lower(),
        result.rel_path.lower(),
        result.line_number or 0,
    ),
    "line": lambda result: (result.line_number or 0, result.rel_path.lower()),
    "snippet": lambda result: (
        result.snippet.lower(),
        result.rel_path.lower(),
        result.line_number or 0,
    ),
    "size": lambda result: (result.file_size, result.rel_path.lower()),
    "rel_path": lambda result: (result.rel_path.lower(), result.line_number or 0),
}

SORT_LABELS = {
    "idx": "#",
    "type": "Type",
    "name": "File",
    "line": "Line",
    "snippet": "Match",
    "size": "Size",
    "rel_path": "Path",
}


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass
class SearchResult:
    """One row in the results table."""

    idx: int
    match_type: str  # 'name' or 'content'
    file_path: str
    file_name: str
    rel_path: str
    line_number: Optional[int] = None
    snippet: str = ""
    file_size: int = 0
    mtime: float = 0.0


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def format_size(size_bytes: int) -> str:
    """Format a byte size as B, KB, MB, or GB."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_mtime(timestamp: float) -> str:
    """Format a modification timestamp, or '-' when it is not usable."""
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def is_binary_file(filepath: str) -> bool:
    """Return True when the file looks binary."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with open(filepath, "rb") as handle:
            if b"\x00" in handle.read(BINARY_SNIFF_BYTES):
                return True
    except OSError:
        return True
    return False


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard (pbcopy, wl-copy, xclip, or xsel)."""
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform.startswith("linux"):
        commands = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    else:
        return False

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text.encode("utf-8"),
                check=True,
                timeout=CLIPBOARD_TIMEOUT,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def open_file_in_system(filepath: str) -> bool:
    """Open a file in the system default application."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        if sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except OSError:
        pass
    return False


def reveal_in_file_manager(filepath: str) -> bool:
    """Reveal a file in Finder or the system file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", "-R", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        if sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", os.path.dirname(filepath)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except OSError:
        pass
    return False


def has_glob(query: str) -> bool:
    """Return True when a plain (non-regex) query uses shell wildcards."""
    return any(character in query for character in GLOB_CHARS)


def build_highlight_pattern(
    query: str,
    case_sensitive: bool = False,
    use_regex: bool = False,
    glob: bool = False,
) -> Optional[Pattern[str]]:
    """Compile the pattern used to highlight matches, or None if unusable.

    With ``glob=True`` a wildcard query (``*.py``, ``test_?``) is translated
    the same way the engine matches file names, so the whole matching name is
    highlighted instead of nothing.
    """
    if not query:
        return None
    try:
        if use_regex:
            pattern = query
        elif glob and has_glob(query):
            pattern = fnmatch.translate(query)
        else:
            pattern = re.escape(query)
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.compile(pattern, flags=flags)
    except re.error:
        return None


def highlight_match(
    text: str,
    query: str,
    case_sensitive: bool = False,
    use_regex: bool = False,
    pattern: Optional[Pattern[str]] = None,
) -> Text:
    """Return Rich text with the term or regular expression highlighted."""
    rich_text = Text()
    if not query or not text:
        rich_text.append(text)
        return rich_text

    compiled = pattern or build_highlight_pattern(query, case_sensitive, use_regex)
    if compiled is None:
        rich_text.append(text)
        return rich_text

    last_index = 0
    for match in compiled.finditer(text):
        start, end = match.span()
        if start == end:
            continue
        if start > last_index:
            rich_text.append(text[last_index:start])
        rich_text.append(text[start:end], style="bold black on #33ff33")
        last_index = end

    if last_index < len(text):
        rich_text.append(text[last_index:])
    return rich_text


def parse_extensions(raw: str) -> Optional[Set[str]]:
    """Parse a comma/semicolon separated extension filter.

    Accepts ``py, .js, *.ts`` and returns ``{"py", "js", "ts"}``. An empty
    string returns None, which means "every extension".
    """
    extensions = set()
    for token in raw.replace(";", ",").split(","):
        cleaned = token.strip().lower().lstrip("*").lstrip(".")
        if cleaned:
            extensions.add(cleaned)
    return extensions or None


def read_line_window(path: str, first_line: int, last_line: int) -> Tuple[str, int]:
    """Read lines ``first_line..last_line`` (1-based, inclusive) of a text file.

    Only the requested window is decoded, so previewing a multi-gigabyte log
    costs a streaming skip instead of loading the whole file. Very long lines
    are clipped. Returns the text and the first line number actually read.
    """
    first_line = max(1, first_line)
    last_line = max(first_line, last_line)
    lines: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace", newline=None) as handle:
        window = itertools.islice(handle, first_line - 1, last_line)
        for line in window:
            if len(line) > PREVIEW_MAX_LINE_CHARS:
                line = line[:PREVIEW_MAX_LINE_CHARS] + " …\n"
            lines.append(line)
    return "".join(lines), first_line


def _snippet_from_text(text: str) -> str:
    """Trim a matching line to something that fits in a table cell."""
    snippet = text.strip()
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[: SNIPPET_MAX_CHARS - 3] + "..."
    return snippet


# -----------------------------------------------------------------------------
# Multithreaded search engine
# -----------------------------------------------------------------------------

class SearchEngine:
    """Multithreaded search over file names and file contents.

    The engine is single-use in practice: create it, connect the callbacks,
    call :meth:`run`, and read the counters afterwards. Content files are
    streamed line by line, so memory use stays bounded even for large files.
    """

    def __init__(
        self,
        root_dir: str,
        query: str,
        search_name: bool = True,
        search_content: bool = False,
        case_sensitive: bool = False,
        ignore_caches: bool = True,
        use_regex: bool = False,
        ext_filter: Optional[Set[str]] = None,
        max_workers: Optional[int] = None,
        max_results: int = MAX_TOTAL_RESULTS,
        on_batch: Optional[Callable[[List[SearchResult]], None]] = None,
        on_progress: Optional[Callable[[int, int, float, bool], None]] = None,
    ):
        self.root_dir = os.path.abspath(os.path.expanduser(root_dir))
        self.query = query
        self.search_name = search_name
        self.search_content = search_content
        self.case_sensitive = case_sensitive
        self.ignore_caches = ignore_caches
        self.use_regex = use_regex
        if ext_filter:
            self.ext_filter = {
                str(item).lower().lstrip("*. ")
                for item in ext_filter
                if str(item).strip()
            } or None
        else:
            self.ext_filter = None
        self.max_results = max(1, int(max_results))

        cpu_count = os.cpu_count() or 4
        requested = max_workers if max_workers is not None else min(MAX_THREADS, cpu_count * 4)
        self.max_workers = max(1, min(256, int(requested)))

        self.on_batch = on_batch
        self.on_progress = on_progress

        self.stop_event = threading.Event()
        self.limit_reached = False
        self._files_scanned = 0
        self._results_found = 0
        self._errors = 0
        self._counter_lock = threading.Lock()
        self._next_result_id = 0

        # Prepared once per run from the query and the case mode.
        self._regex_pattern: Optional[Pattern[str]] = None
        self._query_bytes = b""
        self._query_text = query
        self._uses_byte_matching = False

        if self.use_regex and self.query:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            try:
                self._regex_pattern = re.compile(self.query, flags=flags)
            except re.error as exc:
                raise ValueError(f"Invalid regular expression: {exc}") from exc

    # -- public state --------------------------------------------------------

    def stop(self) -> None:
        """Ask every worker to stop as soon as possible."""
        self.stop_event.set()

    def is_stopped(self) -> bool:
        return self.stop_event.is_set()

    @property
    def files_scanned(self) -> int:
        with self._counter_lock:
            return self._files_scanned

    @property
    def matches_found(self) -> int:
        with self._counter_lock:
            return self._results_found

    @property
    def errors(self) -> int:
        with self._counter_lock:
            return self._errors

    # -- main loop -----------------------------------------------------------

    def run(self) -> Tuple[int, int, float]:
        """Run the search. Returns (files scanned, matches, elapsed seconds)."""
        start_time = time.perf_counter()

        if not self.query or not os.path.isdir(self.root_dir):
            return 0, 0, 0.0

        with self._counter_lock:
            self._files_scanned = 0
            self._results_found = 0
            self._errors = 0
            self._next_result_id = 0

        self._prepare_query()

        file_queue: "queue.Queue[Optional[str]]" = queue.Queue(
            maxsize=max(256, self.max_workers * 64)
        )
        result_queue: "queue.Queue[SearchResult]" = queue.Queue(maxsize=4096)

        dispatcher_done = threading.Event()
        dispatcher = threading.Thread(
            target=self._dispatch_results,
            args=(result_queue, dispatcher_done, start_time),
            name="findfile-dispatch",
            daemon=True,
        )
        dispatcher.start()

        workers: List[threading.Thread] = []
        if self.search_content:
            for index in range(self.max_workers):
                worker = threading.Thread(
                    target=self._content_worker,
                    args=(file_queue, result_queue),
                    name=f"findfile-scan-{index}",
                    daemon=True,
                )
                worker.start()
                workers.append(worker)

        try:
            self._crawl_filesystem(file_queue, result_queue)
        finally:
            self._finish_workers(file_queue, workers, dispatcher_done, dispatcher)

        elapsed = time.perf_counter() - start_time
        self._emit_progress(start_time, running=False)
        return self.files_scanned, self.matches_found, elapsed

    def _prepare_query(self) -> None:
        self._query_text = self.query if self.case_sensitive else self.query.lower()
        self._query_bytes = self.query.encode("utf-8")
        self._uses_byte_matching = not self.use_regex and (
            self.case_sensitive or self.query.isascii()
        )
        if self._uses_byte_matching and not self.case_sensitive:
            self._query_bytes = self._query_bytes.lower()

    def _finish_workers(
        self,
        file_queue: "queue.Queue[Optional[str]]",
        workers: List[threading.Thread],
        dispatcher_done: threading.Event,
        dispatcher: threading.Thread,
    ) -> None:
        """Let the workers drain, then let the dispatcher deliver everything."""
        if workers:
            if not self.stop_event.is_set():
                for _ in workers:
                    if not self._put_sentinel(file_queue):
                        break
            for worker in workers:
                # Content workers check stop_event between lines, so this
                # terminates promptly after a user cancel.
                worker.join()

        dispatcher_done.set()
        dispatcher.join()

    def _put_sentinel(self, file_queue: "queue.Queue[Optional[str]]") -> bool:
        while not self.stop_event.is_set():
            try:
                file_queue.put(None, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _content_worker(
        self,
        file_queue: "queue.Queue[Optional[str]]",
        result_queue: "queue.Queue[SearchResult]",
    ) -> None:
        while True:
            if self.stop_event.is_set():
                return
            try:
                file_path = file_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if file_path is None:
                return

            try:
                self._scan_file(file_path, result_queue)
            except Exception:  # noqa: BLE001 - one bad file must not kill the scan
                self._count_error()

    # -- traversal -----------------------------------------------------------

    def _crawl_filesystem(
        self,
        file_queue: "queue.Queue[Optional[str]]",
        result_queue: "queue.Queue[SearchResult]",
    ) -> None:
        """Walk the tree with os.scandir and feed name hits and content work."""
        stack = [self.root_dir]
        visited_dirs: Set[Tuple[int, int]] = set()
        has_wildcard = (not self.use_regex) and has_glob(self.query)

        while stack and not self.stop_event.is_set():
            current_dir = stack.pop()

            try:
                stat_result = os.stat(current_dir)
                dir_key = (stat_result.st_dev, stat_result.st_ino)
                if dir_key in visited_dirs:
                    continue
                visited_dirs.add(dir_key)
            except OSError:
                self._count_error()
                continue

            try:
                with os.scandir(current_dir) as entries:
                    for entry in entries:
                        if self.stop_event.is_set():
                            return

                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue

                        if is_directory:
                            if self.ignore_caches and (
                                entry.name.startswith(".") or entry.name in IGNORED_DIRS
                            ):
                                continue
                            stack.append(entry.path)
                            continue

                        try:
                            # Symlinked files are searched; symlinked folders are
                            # not followed, which keeps cycles impossible.
                            is_file = entry.is_file(follow_symlinks=True)
                        except OSError:
                            continue
                        if not is_file:
                            continue

                        extension = os.path.splitext(entry.name)[1].lower()
                        extension_plain = extension.lstrip(".")
                        if (
                            self.ext_filter is not None
                            and extension_plain not in self.ext_filter
                        ):
                            continue

                        with self._counter_lock:
                            self._files_scanned += 1

                        if self.search_name and self._name_matches(entry.name, has_wildcard):
                            if not self._emit_name_match(entry, result_queue):
                                return

                        if self.search_content:
                            explicitly_requested = (
                                self.ext_filter is not None
                                and extension_plain in self.ext_filter
                            )
                            if extension in BINARY_EXTENSIONS and not explicitly_requested:
                                continue
                            if not self._queue_file(file_queue, entry.path):
                                return
            except (PermissionError, OSError):
                self._count_error()

    def _name_matches(self, name: str, has_wildcard: bool) -> bool:
        if self.use_regex:
            return bool(self._regex_pattern and self._regex_pattern.search(name))

        candidate = name if self.case_sensitive else name.lower()
        if has_wildcard:
            return fnmatch.fnmatchcase(candidate, self._query_text)
        return self._query_text in candidate

    def _emit_name_match(
        self,
        entry: os.DirEntry,
        result_queue: "queue.Queue[SearchResult]",
    ) -> bool:
        """Register and queue a file-name match found during the crawl."""
        result_id = self._register_result()
        if result_id is None:
            return False

        try:
            stat_result = entry.stat(follow_symlinks=True)
            size = stat_result.st_size
            mtime = stat_result.st_mtime
        except OSError:
            size, mtime = 0, 0.0

        self._put_result(
            result_queue,
            SearchResult(
                idx=result_id,
                match_type="name",
                file_path=entry.path,
                file_name=entry.name,
                rel_path=self._relative_path(entry.path),
                file_size=size,
                mtime=mtime,
            ),
        )
        return True

    def _queue_file(
        self, file_queue: "queue.Queue[Optional[str]]", path: str
    ) -> bool:
        while not self.stop_event.is_set():
            try:
                file_queue.put(path, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root_dir)
        except ValueError:
            return path

    # -- content scanning ----------------------------------------------------

    def _scan_file(
        self,
        file_path: str,
        result_queue: "queue.Queue[SearchResult]",
    ) -> None:
        """Open once, reject binaries, and stream matching lines."""
        try:
            with open(file_path, "rb") as handle:
                stat_result = os.fstat(handle.fileno())
                size = stat_result.st_size
                if size == 0 or size > MAX_CONTENT_FILE_SIZE:
                    return

                head = handle.read(BINARY_SNIFF_BYTES)
                if b"\x00" in head:
                    return
                handle.seek(0)

                if self._uses_byte_matching:
                    if not self._bytes_contains(handle, self._query_bytes):
                        return
                    handle.seek(0)
                    self._scan_byte_lines(handle, file_path, stat_result, result_queue)
                else:
                    self._scan_text_lines(handle, file_path, stat_result, result_queue)
        except (OSError, ValueError):
            self._count_error()

    def _bytes_contains(self, handle, needle: bytes) -> bool:
        """Chunked substring search with a small overlap between chunks."""
        if not needle:
            return True

        keep = len(needle) - 1
        # Read at least one byte more than the needle so a match can never
        # fall between two chunks.
        read_size = max(READ_CHUNK_BYTES, len(needle) + 1)
        overlap = b""
        while True:
            chunk = handle.read(read_size)
            if not chunk:
                return False

            if self.case_sensitive:
                haystack = overlap + chunk
                overlap = chunk[-keep:] if keep else b""
            else:
                lowered = chunk.lower()
                haystack = overlap + lowered
                overlap = lowered[-keep:] if keep else b""

            if needle in haystack:
                return True

    def _scan_byte_lines(self, handle, file_path, stat_result, result_queue) -> None:
        file_name = os.path.basename(file_path)
        relative = self._relative_path(file_path)
        matches_in_file = 0

        for line_number, raw_line in enumerate(handle, start=1):
            if self.stop_event.is_set():
                return

            haystack = raw_line if self.case_sensitive else raw_line.lower()
            if self._query_bytes not in haystack:
                continue

            line_text = raw_line.decode("utf-8", errors="replace")
            if not self._emit_content(
                result_queue,
                file_path,
                file_name,
                relative,
                line_number,
                line_text,
                stat_result,
            ):
                return

            matches_in_file += 1
            if matches_in_file >= MAX_MATCHES_PER_FILE:
                return

    def _scan_text_lines(self, handle, file_path, stat_result, result_queue) -> None:
        stream = io.TextIOWrapper(
            handle,
            encoding="utf-8",
            errors="replace",
            newline=None,
        )
        file_name = os.path.basename(file_path)
        relative = self._relative_path(file_path)
        matches_in_file = 0

        try:
            for line_number, line in enumerate(stream, start=1):
                if self.stop_event.is_set():
                    return

                if self._regex_pattern is not None:
                    if not self._regex_pattern.search(line):
                        continue
                elif self.case_sensitive:
                    if self.query not in line:
                        continue
                elif self._query_text not in line.lower():
                    continue

                if not self._emit_content(
                    result_queue,
                    file_path,
                    file_name,
                    relative,
                    line_number,
                    line,
                    stat_result,
                ):
                    return

                matches_in_file += 1
                if matches_in_file >= MAX_MATCHES_PER_FILE:
                    return
        finally:
            # Detach so the outer ``with open`` owns the lifetime of the fd.
            try:
                stream.detach()
            except (OSError, ValueError):
                pass

    def _emit_content(
        self,
        result_queue: "queue.Queue[SearchResult]",
        file_path: str,
        file_name: str,
        relative: str,
        line_number: int,
        line_text: str,
        stat_result: os.stat_result,
    ) -> bool:
        result_id = self._register_result()
        if result_id is None:
            return False

        result = SearchResult(
            idx=result_id,
            match_type="content",
            file_path=file_path,
            file_name=file_name,
            rel_path=relative,
            line_number=line_number,
            snippet=_snippet_from_text(line_text),
            file_size=stat_result.st_size,
            mtime=stat_result.st_mtime,
        )
        self._put_result(result_queue, result)
        return True

    # -- result bookkeeping --------------------------------------------------

    def _register_result(self) -> Optional[int]:
        """Count one match and stop the whole search once the cap is hit."""
        with self._counter_lock:
            if self._results_found >= self.max_results:
                self.limit_reached = True
                self.stop_event.set()
                return None

            self._results_found += 1
            self._next_result_id += 1
            result_id = self._next_result_id

            if self._results_found >= self.max_results:
                self.limit_reached = True
                self.stop_event.set()

            return result_id

    def _put_result(
        self,
        result_queue: "queue.Queue[SearchResult]",
        result: SearchResult,
    ) -> None:
        # Every registered result is delivered, including results found just
        # before a cancel, so the UI counters always match the table.
        while True:
            try:
                result_queue.put(result, timeout=0.05)
                return
            except queue.Full:
                continue

    def _count_error(self) -> None:
        with self._counter_lock:
            self._errors += 1

    # -- delivery ------------------------------------------------------------

    def _dispatch_results(
        self,
        result_queue: "queue.Queue[SearchResult]",
        done: threading.Event,
        start_time: float,
    ) -> None:
        batch: List[SearchResult] = []
        last_flush = time.perf_counter()
        last_progress = last_flush

        while True:
            try:
                batch.append(result_queue.get(timeout=0.03))
            except queue.Empty:
                if done.is_set():
                    if batch:
                        self._emit_batch(batch)
                    break

            now = time.perf_counter()
            if batch and (len(batch) >= BATCH_SIZE or (now - last_flush) >= FLUSH_INTERVAL):
                self._emit_batch(batch)
                batch = []
                last_flush = now

            if (now - last_progress) >= PROGRESS_INTERVAL:
                self._emit_progress(start_time, running=True)
                last_progress = now

    def _emit_batch(self, batch: List[SearchResult]) -> None:
        if self.on_batch is None:
            return
        try:
            self.on_batch(batch)
        except Exception:  # noqa: BLE001 - a closed UI must not kill the scan
            pass

    def _emit_progress(self, start_time: float, running: bool) -> None:
        if self.on_progress is None:
            return
        elapsed = time.perf_counter() - start_time
        try:
            self.on_progress(
                self.files_scanned,
                self.matches_found,
                elapsed,
                running,
            )
        except Exception:  # noqa: BLE001 - a closed UI must not kill the scan
            pass


# -----------------------------------------------------------------------------
# Dialogs: folder picker, preview, and help
# -----------------------------------------------------------------------------

class CustomFolderTree(DirectoryTree):
    """Directory tree that lists visible folders only."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [
            path
            for path in paths
            if path.is_dir() and not path.name.startswith(".")
        ]


class FolderPickerModal(ModalScreen[Optional[str]]):
    """Dialog for picking a folder, with shortcuts, a path field, and a tree."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial_path: str = "."):
        super().__init__()
        resolved = Path(initial_path).expanduser()
        self.current_path = str(resolved if resolved.is_dir() else Path.cwd())

    def compose(self) -> ComposeResult:
        with Vertical(id="folder_dialog", classes="dialog"):
            yield Label("Choose folder", classes="dialog_title")
            yield Static(
                "Type a path and press Enter, or pick a folder in the tree.",
                classes="dialog_subtitle",
            )

            with Horizontal(id="quick_paths"):
                yield Button("Home", id="quick_home")
                yield Button("Here", id="quick_cwd")
                yield Button("Docs", id="quick_docs")
                yield Button("Downloads", id="quick_downloads")
                yield Button("Up", id="quick_up")

            yield Input(value=self.current_path, id="modal_folder_input")
            yield CustomFolderTree(self.current_path, id="folder_tree")

            with Horizontal(id="modal_actions"):
                yield Button("Cancel", id="btn_modal_cancel")
                yield Button("Use this folder", id="btn_modal_confirm", variant="primary")

    def on_mount(self) -> None:
        """Focus the path field so typing and Enter work immediately."""
        field = self.query_one("#modal_folder_input", Input)
        field.focus()
        select_all = getattr(field, "action_select_all", None)
        if select_all is not None:
            select_all()

    @on(DirectoryTree.DirectorySelected)
    def on_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.current_path = str(event.path)
        self.query_one("#modal_folder_input", Input).value = self.current_path

    @on(Button.Pressed, "#quick_home")
    def go_home(self) -> None:
        self._set_path(Path.home())

    @on(Button.Pressed, "#quick_cwd")
    def go_cwd(self) -> None:
        self._set_path(Path.cwd())

    @on(Button.Pressed, "#quick_docs")
    def go_docs(self) -> None:
        self._set_path(Path.home() / "Documents")

    @on(Button.Pressed, "#quick_downloads")
    def go_downloads(self) -> None:
        self._set_path(Path.home() / "Downloads")

    @on(Button.Pressed, "#quick_up")
    def go_up(self) -> None:
        self._set_path(Path(self.current_path).parent)

    @on(Input.Submitted, "#modal_folder_input")
    def on_input_submit(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            return
        resolved = Path(raw).expanduser()
        if resolved.is_dir():
            self.dismiss(str(resolved.resolve()))
        else:
            self.notify(f"Not a folder: {escape(raw)}", severity="warning")

    @on(Button.Pressed, "#btn_modal_confirm")
    def action_confirm(self) -> None:
        raw = self.query_one("#modal_folder_input", Input).value.strip()
        resolved = Path(raw).expanduser() if raw else Path(self.current_path)
        if resolved.is_dir():
            self.dismiss(str(resolved.resolve()))
        else:
            self.notify(f"Not a folder: {escape(raw)}", severity="warning")

    @on(Button.Pressed, "#btn_modal_cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    def _set_path(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if not resolved.is_dir():
            return

        self.current_path = str(resolved)
        self.query_one("#modal_folder_input", Input).value = self.current_path
        try:
            self.query_one("#folder_tree", CustomFolderTree).path = resolved
        except Exception:  # noqa: BLE001 - the tree is best-effort
            pass


class FilePreviewModal(ModalScreen[None]):
    """Dialog with file metadata, a syntax-highlighted preview, and actions."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("o", "open_sys", "Open"),
        Binding("f", "reveal_finder", "Finder"),
        Binding("c", "copy_path", "Copy path"),
        Binding("y", "copy_snippet", "Copy snippet"),
    ]

    def __init__(self, result: SearchResult):
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        safe_size = escape(format_size(self.result.file_size))
        safe_mtime = escape(format_mtime(self.result.mtime))
        match_desc = (
            f"content · line {self.result.line_number}"
            if self.result.match_type == "content"
            else "file name"
        )

        with Vertical(id="preview_dialog", classes="dialog"):
            yield Label(escape(self.result.file_name), classes="dialog_title")
            yield Static(escape(self.result.file_path), id="preview_path")
            yield Static(
                f"[bold]{safe_size}[/]   [#1d7d2f]·[/]   [bold]{safe_mtime}[/]"
                f"   [#1d7d2f]·[/]   [bold #ffb000]{escape(match_desc)}[/]",
                id="preview_meta",
            )

            with VerticalScroll(id="preview_scroll"):
                yield self._build_preview_widget()

            with Horizontal(id="preview_actions"):
                yield Button("Open  o", id="btn_open_file", variant="primary")
                yield Button("Reveal  f", id="btn_reveal_file")
                yield Button("Path  c", id="btn_copy_path")
                if self.result.snippet:
                    yield Button("Snippet  y", id="btn_copy_snippet")
                yield Button("Close", id="btn_close")

    def _build_preview_widget(self) -> Static:
        """Load a syntax-highlighted window around the matching line."""
        path = Path(self.result.file_path)
        if not path.is_file():
            return Static("[red]File not found or unreadable.[/]")

        if is_binary_file(self.result.file_path):
            return Static("[italic #ffb000]Binary file. Text preview is unavailable.[/]")

        try:
            line_number = self.result.line_number
            if line_number is not None:
                first_line = line_number - PREVIEW_LINES_BEFORE
                last_line = line_number + PREVIEW_LINES_AFTER
            else:
                first_line = 1
                last_line = PREVIEW_LINES_BEFORE + PREVIEW_LINES_AFTER
            code, start_line = read_line_window(str(path), first_line, last_line)
            if not code:
                return Static("[italic #1d7d2f]Empty file.[/]")
            syntax = Syntax(
                code,
                Syntax.guess_lexer(str(path), code=code),
                line_numbers=True,
                start_line=start_line,
                highlight_lines={line_number} if line_number is not None else None,
                theme="ansi_dark",
                word_wrap=True,
            )
            return Static(syntax)
        except Exception as exc:  # noqa: BLE001 - show the failure in the dialog
            return Static(f"[red]Could not read preview:[/] {escape(str(exc))}")

    @on(Button.Pressed, "#btn_open_file")
    def action_open_sys(self) -> None:
        if open_file_in_system(self.result.file_path):
            self.notify(f"Opened {escape(self.result.file_name)}.")
        else:
            self.notify("Could not open the file.", severity="warning")

    @on(Button.Pressed, "#btn_reveal_file")
    def action_reveal_finder(self) -> None:
        if reveal_in_file_manager(self.result.file_path):
            self.notify("Revealed in the file manager.")
        else:
            self.notify("Could not reveal the file.", severity="warning")

    @on(Button.Pressed, "#btn_copy_path")
    def action_copy_path(self) -> None:
        if copy_to_clipboard(self.result.file_path):
            self.notify("Path copied to the clipboard.")
        else:
            self.notify("Could not copy the path.", severity="warning")

    @on(Button.Pressed, "#btn_copy_snippet")
    def action_copy_snippet(self) -> None:
        if self.result.snippet and copy_to_clipboard(self.result.snippet):
            self.notify("Snippet copied.")
        elif not self.result.snippet:
            self.notify("This result has no snippet.", severity="warning")
        else:
            self.notify("Could not copy the snippet.", severity="warning")

    @on(Button.Pressed, "#btn_close")
    def action_dismiss_modal(self) -> None:
        self.dismiss()


class HelpModal(ModalScreen[None]):
    """Dialog listing keyboard shortcuts."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        shortcuts = (
            ("Enter", "Start a search from any field"),
            ("Esc", "Stop the search, or close a dialog"),
            ("Enter / double-click", "Open the selected row"),
            ("Column header", "Sort the table by that column"),
            ("Ctrl+F  /", "Focus the search field"),
            ("Ctrl+P", "Open the folder picker"),
            ("Ctrl+L", "Clear the results"),
            ("Ctrl+K", "Open the command palette"),
            ("o", "Open the file in the default app"),
            ("f", "Reveal the file in Finder"),
            ("c", "Copy the full path"),
            ("y", "Copy the matched snippet or file name"),
            ("F1  ?", "Show this help"),
            ("q", "Quit"),
        )
        with Vertical(id="help_dialog", classes="dialog"):
            yield Label("Shortcuts", classes="dialog_title")
            yield Static(
                "Global keys are also listed in the footer.",
                classes="dialog_subtitle",
            )
            with Grid(id="help_grid"):
                for key, description in shortcuts:
                    yield Static(key, classes="help_key")
                    yield Static(description, classes="help_desc")
            with Horizontal(id="help_actions"):
                yield Button("Close", id="btn_ok", variant="primary")

    @on(Button.Pressed, "#btn_ok")
    def close_modal(self) -> None:
        self.dismiss()


# -----------------------------------------------------------------------------
# Main Textual application
# -----------------------------------------------------------------------------

class FindFileApp(App):
    """Main FindFile Textual application."""

    TITLE = "FindFile"
    SUB_TITLE = f"v{__version__}  ·  names and content  ·  parallel search  ·  preview"
    COMMAND_PALETTE_BINDING = "ctrl+k"

    CSS = """
    Screen {
        background: #050703;
        color: #33ff33;
    }

    Header {
        background: #060f04;
        color: #7dffa8;
        text-style: bold;
        border-bottom: double #1d7d2f;
    }

    Footer {
        background: #060f04;
        color: #33ff33;
        border-top: double #1d7d2f;
        text-style: bold;
    }

    #search_panel {
        height: auto;
        border: double #33ff33;
        background: #070a04;
        padding: 1 1 0 1;
        margin: 1 1 0 1;
    }

    .form_row, #options_row {
        height: auto;
        align: left middle;
        margin-bottom: 1;
    }

    .row_label {
        width: 9;
        text-style: bold;
        color: #ffb000;
        padding-right: 1;
    }

    Input {
        border: tall #1d7d2f;
        background: #020402;
        color: #33ff33;
    }

    Input:focus {
        border: tall #7dffa8;
    }

    #input_query, #input_folder, #input_exts {
        width: 1fr;
    }

    .btn_compact {
        margin-left: 1;
        min-width: 10;
    }

    Button {
        background: #0b1406;
        color: #33ff33;
        border: tall #1d7d2f;
        text-style: bold;
    }

    Button:hover {
        background: #14300d;
        color: #7dffa8;
        border: tall #33ff33;
    }

    Button:focus {
        border: tall #ffb000;
    }

    #btn_search {
        min-width: 12;
        background: #0d240a;
        border: tall #33ff33;
        color: #7dffa8;
    }

    #btn_stop {
        color: #ffb000;
        border: tall #7d4b00;
    }

    #btn_stop:hover {
        background: #2a1c00;
        color: #ffcf5f;
        border: tall #ffb000;
    }

    #options_row Checkbox {
        margin-right: 1;
        background: transparent;
        width: auto;
    }

    #status_bar {
        height: 1;
        background: #041006;
        padding: 0 1;
        margin: 1 1 0 1;
        align: left middle;
    }

    #lbl_status {
        width: 1fr;
        color: #33ff33;
        overflow: hidden;
    }

    .stat_pill {
        padding: 0 1;
        margin-left: 1;
        text-style: bold;
        background: #0b1a08;
        color: #33ff33;
    }

    #pill_sort {
        color: #7dffa8;
        display: none;
    }

    #pill_matches {
        color: #7dffa8;
    }

    #pill_scanned {
        color: #33ff33;
    }

    #pill_time {
        color: #ffb000;
    }

    #pill_threads {
        color: #1d7d2f;
    }

    #progress_bar {
        width: 100%;
        height: 1;
        margin: 0 1;
        padding: 0;
        display: none;
    }

    #progress_bar Bar {
        width: 100%;
        color: #33ff33;
        background: #0b1a08;
    }

    #results_container {
        height: 1fr;
        margin: 0 1 0 1;
        border: double #1d7d2f;
        background: #020402;
    }

    #results_table {
        height: 100%;
        background: #020402;
    }

    #empty_hint {
        dock: top;
        height: 100%;
        width: 100%;
        content-align: center middle;
        text-align: center;
        color: #1d7d2f;
        background: #020402;
    }

    DataTable > .datatable--header {
        background: #060f04;
        color: #ffb000;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #12330d;
        color: #7dffa8;
        text-style: bold;
    }

    DataTable > .datatable--even-row {
        background: #050a04;
        color: #33ff33;
    }

    DataTable > .datatable--odd-row {
        background: #020402;
        color: #33ff33;
    }

    /* Shared dialog chrome */
    .dialog {
        border: double #33ff33;
        background: #070a04;
        padding: 1 2;
    }

    .dialog_title {
        text-style: bold;
        color: #7dffa8;
    }

    .dialog_subtitle {
        color: #1d7d2f;
        margin-bottom: 1;
    }

    FolderPickerModal {
        align: center middle;
        background: rgba(2, 4, 2, 0.88);
    }

    #folder_dialog {
        width: 86%;
        max-width: 96;
        height: 84%;
    }

    #quick_paths {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }

    #quick_paths Button {
        margin: 0 1 0 0;
        min-width: 12;
    }

    #modal_folder_input {
        margin-bottom: 1;
    }

    #folder_tree {
        height: 1fr;
        border: double #1d7d2f;
        background: #020402;
        margin-bottom: 1;
    }

    #folder_tree .tree--cursor,
    #folder_tree .tree--highlight {
        background: #12330d;
        color: #7dffa8;
    }

    #modal_actions {
        height: auto;
        align: right middle;
    }

    #modal_actions Button {
        margin-left: 1;
    }

    FilePreviewModal {
        align: center middle;
        background: rgba(2, 4, 2, 0.88);
    }

    #preview_dialog {
        width: 92%;
        max-width: 128;
        height: 88%;
    }

    #preview_path {
        color: #1d7d2f;
    }

    #preview_meta {
        color: #33ff33;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: double #1d7d2f;
    }

    #preview_scroll {
        height: 1fr;
        border: double #1d7d2f;
        background: #020402;
        padding: 0 1;
        margin-bottom: 1;
    }

    #preview_actions {
        height: auto;
        align: left middle;
    }

    #preview_actions Button {
        margin: 0 1 0 0;
    }

    HelpModal {
        align: center middle;
        background: rgba(2, 4, 2, 0.88);
    }

    #help_dialog {
        width: 78%;
        max-width: 92;
        height: auto;
        max-height: 90%;
    }

    #help_grid {
        grid-size: 2;
        grid-gutter: 0 2;
        grid-columns: 18 1fr;
        height: auto;
    }

    .help_key {
        color: #ffb000;
        text-style: bold;
    }

    .help_desc {
        color: #33ff33;
    }

    #help_actions {
        margin-top: 1;
        height: auto;
        align: right middle;
    }
    """

    BINDINGS = [
        Binding("ctrl+f", "focus_search", "Search", show=True),
        Binding("ctrl+p", "open_folder_picker", "Folder", show=True),
        Binding("ctrl+l", "clear_results", "Clear", show=True),
        Binding("escape", "stop_or_unfocus", "Stop", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        initial_folder: str = ".",
        initial_query: str = "",
        initial_name: bool = True,
        initial_content: bool = False,
        initial_regex: bool = False,
        initial_case: bool = False,
        initial_exts: str = "",
        initial_ignore: bool = True,
        workers: Optional[int] = None,
        max_results: int = MAX_TOTAL_RESULTS,
    ):
        super().__init__()
        self.initial_folder = str(Path(initial_folder).expanduser().resolve())
        self.initial_query = initial_query
        self.initial_name = initial_name
        self.initial_content = initial_content
        self.initial_regex = initial_regex
        self.initial_case = initial_case
        self.initial_exts = initial_exts
        self.initial_ignore = initial_ignore

        cpu_count = os.cpu_count() or 4
        default_workers = min(MAX_THREADS, cpu_count * 4)
        self.worker_count = max(1, min(256, int(workers or default_workers)))
        self.max_results = max(1, int(max_results))

        self.current_engine: Optional[SearchEngine] = None
        self.results_map: dict[int, SearchResult] = {}
        self.is_searching = False

        self._user_stopped = False
        self._search_id = 0
        self._total_found = 0
        self._total_scanned = 0
        self._last_query = ""
        self._last_case_sensitive = False
        self._last_use_regex = False
        self._highlight_pattern: Optional[Pattern[str]] = None
        self._name_highlight_pattern: Optional[Pattern[str]] = None

        self._sort_key: Optional[str] = None
        self._sort_reverse: dict[str, bool] = {}

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="search_panel"):
            with Horizontal(classes="form_row"):
                yield Label("Search", classes="row_label")
                yield Input(
                    value=self.initial_query,
                    placeholder="Name, text, or regular expression",
                    id="input_query",
                )
                yield Button("Search", id="btn_search", variant="primary", classes="btn_compact")
                yield Button(
                    "Stop",
                    id="btn_stop",
                    variant="error",
                    disabled=True,
                    classes="btn_compact",
                )
                yield Button("Clear", id="btn_clear", classes="btn_compact")

            with Horizontal(classes="form_row"):
                yield Label("Folder", classes="row_label")
                yield Input(
                    value=self.initial_folder,
                    placeholder="Folder path",
                    id="input_folder",
                )
                yield Button("Browse", id="btn_browse", classes="btn_compact")
                yield Button("Here", id="btn_cwd", classes="btn_compact")

            with Horizontal(id="options_row"):
                yield Label("Filters", classes="row_label")
                yield Checkbox("Name", value=self.initial_name, id="chk_name")
                yield Checkbox("Text", value=self.initial_content, id="chk_content")
                yield Checkbox("Case", value=self.initial_case, id="chk_case")
                yield Checkbox("Regex", value=self.initial_regex, id="chk_regex")
                yield Checkbox("Skip hidden", value=self.initial_ignore, id="chk_ignore")

            with Horizontal(classes="form_row"):
                yield Label("Ext.", classes="row_label")
                yield Input(
                    value=self.initial_exts,
                    placeholder="py, js, txt  ·  empty matches every extension",
                    id="input_exts",
                )

        with Horizontal(id="status_bar"):
            yield Label("Ready.", id="lbl_status")
            yield Label("", id="pill_sort", classes="stat_pill")
            yield Label("0 matches", id="pill_matches", classes="stat_pill")
            yield Label("0 files", id="pill_scanned", classes="stat_pill")
            yield Label("0.0s", id="pill_time", classes="stat_pill")
            yield Label(f"{self.worker_count} threads", id="pill_threads", classes="stat_pill")

        yield ProgressBar(id="progress_bar", show_eta=False, show_percentage=False)

        with Container(id="results_container"):
            yield Static(
                "Type a term and press Enter.\n"
                "In the table: Enter previews  ·  o opens  ·  f reveals  ·  c copies",
                id="empty_hint",
            )
            yield DataTable(id="results_table", cursor_type="row", zebra_stripes=True)

        yield Footer()

    def on_mount(self) -> None:
        """Set up the table, then focus the search field or run the CLI query."""
        table = self.query_one("#results_table", DataTable)
        table.add_column("#", key="idx", width=5)
        table.add_column("Type", key="type", width=8)
        table.add_column("File", key="name", width=24)
        table.add_column("Line", key="line", width=6)
        table.add_column("Match", key="snippet", width=44)
        table.add_column("Size", key="size", width=9)
        table.add_column("Relative path", key="rel_path", width=28)
        self._resize_columns(self.size.width)

        if self.initial_query:
            self.start_search()
        else:
            self.query_one("#input_query", Input).focus()

    def on_resize(self, event) -> None:
        self._resize_columns(event.size.width)

    def _resize_columns(self, available_width: int) -> None:
        """Give the flexible columns room that matches the terminal width."""
        table = self.query_one("#results_table", DataTable)
        columns = table.ordered_columns
        if not columns:
            return

        fixed_widths = {"idx": 4, "type": 8, "line": 6, "size": 9}
        padding = 2 * table.cell_padding * len(columns)
        usable = max(40, available_width - 8 - padding)
        flexible = max(28, usable - sum(fixed_widths.values()))

        name_width = max(12, int(flexible * 0.28))
        snippet_width = max(16, int(flexible * 0.46))
        path_width = max(8, flexible - name_width - snippet_width)

        widths = {
            **fixed_widths,
            "name": name_width,
            "snippet": snippet_width,
            "rel_path": path_width,
        }
        for column in columns:
            key = column.key.value
            if key in widths and column.width != widths[key]:
                column.width = widths[key]
        table.refresh()

    def on_unmount(self) -> None:
        if self.current_engine is not None:
            self.current_engine.stop()

    # -- search controls -----------------------------------------------------

    @on(Button.Pressed, "#btn_browse")
    def open_folder_picker(self) -> None:
        current_folder = self.query_one("#input_folder", Input).value
        modal = FolderPickerModal(current_folder)

        def on_folder_selected(selected_path: Optional[str]) -> None:
            if selected_path:
                self.query_one("#input_folder", Input).value = selected_path

        self.push_screen(modal, on_folder_selected)

    @on(Button.Pressed, "#btn_cwd")
    def set_current_dir(self) -> None:
        self.query_one("#input_folder", Input).value = str(Path.cwd())

    @on(Button.Pressed, "#btn_search")
    @on(Input.Submitted, "#input_query")
    @on(Input.Submitted, "#input_folder")
    @on(Input.Submitted, "#input_exts")
    def on_submit_search(self) -> None:
        self.start_search()

    @on(Button.Pressed, "#btn_stop")
    def stop_search_action(self) -> None:
        self.stop_search()

    @on(Button.Pressed, "#btn_clear")
    def clear_results(self) -> None:
        self._halt_engine()
        self._search_id += 1
        self.is_searching = False
        self._user_stopped = False
        self._sort_key = None
        self._sort_reverse.clear()

        table = self.query_one("#results_table", DataTable)
        table.clear()
        self.results_map.clear()
        self._total_found = 0
        self._total_scanned = 0

        self.query_one("#btn_search", Button).disabled = False
        self.query_one("#btn_stop", Button).disabled = True
        self.query_one("#progress_bar", ProgressBar).styles.display = "none"
        self.query_one("#pill_threads", Label).update(f"{self.worker_count} threads")
        self._update_sort_pill()
        self._update_counters(0, 0, 0.0)
        self.query_one("#lbl_status", Label).update("Results cleared.")
        self._show_empty(
            "Type a term and press Enter.\n"
            "In the table: Enter previews  ·  o opens  ·  f reveals  ·  c copies"
        )

    def start_search(self) -> None:
        query = self.query_one("#input_query", Input).value.strip()
        folder = self.query_one("#input_folder", Input).value.strip() or "."
        search_name = self.query_one("#chk_name", Checkbox).value
        search_content = self.query_one("#chk_content", Checkbox).value
        case_sensitive = self.query_one("#chk_case", Checkbox).value
        use_regex = self.query_one("#chk_regex", Checkbox).value
        ignore_caches = self.query_one("#chk_ignore", Checkbox).value

        if not query:
            self.notify("Enter a search term.", severity="warning")
            self.query_one("#input_query", Input).focus()
            return

        if not search_name and not search_content:
            self.notify("Turn on Name, Text, or both.", severity="warning")
            return

        folder_path = Path(folder).expanduser().resolve()
        if not folder_path.is_dir():
            self.notify(f"Folder not found: {escape(folder)}", severity="error")
            self.query_one("#input_folder", Input).focus()
            return

        if use_regex:
            try:
                re.compile(query, 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                self.notify(f"Invalid regular expression: {exc}", severity="error")
                return

        ext_filter = parse_extensions(self.query_one("#input_exts", Input).value)

        # Stop the previous search before the state and the table are reset.
        self._halt_engine()
        self._search_id += 1
        search_id = self._search_id
        self._user_stopped = False

        self._last_query = query
        self._last_case_sensitive = case_sensitive
        self._last_use_regex = use_regex
        self._highlight_pattern = build_highlight_pattern(
            query, case_sensitive, use_regex
        )
        self._name_highlight_pattern = build_highlight_pattern(
            query, case_sensitive, use_regex, glob=True
        )

        table = self.query_one("#results_table", DataTable)
        table.clear()
        self.results_map.clear()
        self._total_found = 0
        self._total_scanned = 0

        self.is_searching = True
        self.query_one("#btn_search", Button).disabled = True
        self.query_one("#btn_stop", Button).disabled = False
        self.query_one("#pill_threads", Label).update(
            f"{self.worker_count} threads" if search_content else "name only"
        )
        self.query_one("#lbl_status", Label).update("Searching…")
        self.query_one("#progress_bar", ProgressBar).styles.display = "block"
        self._update_counters(0, 0, 0.0)
        self._show_empty("Scanning the folder…")

        def on_batch(batch: List[SearchResult]) -> None:
            if search_id != self._search_id:
                return
            try:
                self.call_from_thread(self._add_results_batch, search_id, batch)
            except Exception:  # noqa: BLE001 - the app may be shutting down
                pass

        def on_progress(scanned: int, found: int, elapsed: float, running: bool) -> None:
            if search_id != self._search_id:
                return
            try:
                self.call_from_thread(
                    self._update_progress_ui,
                    search_id,
                    scanned,
                    found,
                    elapsed,
                    running,
                )
            except Exception:  # noqa: BLE001 - the app may be shutting down
                pass

        try:
            engine = SearchEngine(
                root_dir=str(folder_path),
                query=query,
                search_name=search_name,
                search_content=search_content,
                case_sensitive=case_sensitive,
                ignore_caches=ignore_caches,
                use_regex=use_regex,
                ext_filter=ext_filter,
                max_workers=self.worker_count,
                max_results=self.max_results,
                on_batch=on_batch,
                on_progress=on_progress,
            )
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            self.is_searching = False
            self.query_one("#btn_search", Button).disabled = False
            self.query_one("#btn_stop", Button).disabled = True
            self.query_one("#progress_bar", ProgressBar).styles.display = "none"
            self.query_one("#lbl_status", Label).update("Ready.")
            self._show_empty("Type a term and press Enter.")
            return

        # Register the engine before the worker starts, so an immediate Stop
        # press can always reach it.
        self.current_engine = engine
        self.run_search_worker(engine=engine, search_id=search_id)

    @work(thread=True, exclusive=True)
    def run_search_worker(self, engine: SearchEngine, search_id: int) -> None:
        """Run the engine on a background thread and report back to the UI."""
        error: Optional[str] = None
        scanned, found, elapsed = 0, 0, 0.0
        try:
            scanned, found, elapsed = engine.run()
        except Exception as exc:  # noqa: BLE001 - surface the failure in the UI
            error = str(exc)

        try:
            self.call_from_thread(
                self._finish_search_ui,
                search_id,
                engine,
                scanned,
                found,
                elapsed,
                error,
            )
        except Exception:  # noqa: BLE001 - the app may be shutting down
            pass

    def _add_results_batch(self, search_id: int, batch: List[SearchResult]) -> None:
        """Append a result batch to the table on the UI thread."""
        if search_id != self._search_id:
            return

        table = self.query_one("#results_table", DataTable)
        if batch:
            self._hide_empty()

        for result in batch:
            self.results_map[result.idx] = result
            self._add_result_row(table, result)

    def _add_result_row(self, table: DataTable, result: SearchResult) -> None:
        type_badge = (
            Text("content", style="bold #ffb000")
            if result.match_type == "content"
            else Text("name", style="bold #7dffa8")
        )

        name_text = highlight_match(
            result.file_name,
            self._last_query,
            self._last_case_sensitive,
            self._last_use_regex,
            pattern=self._name_highlight_pattern,
        )
        line_text = f"L{result.line_number}" if result.line_number is not None else "-"
        snippet_text = (
            highlight_match(
                result.snippet,
                self._last_query,
                self._last_case_sensitive,
                self._last_use_regex,
                pattern=self._highlight_pattern,
            )
            if result.snippet
            else Text(f"Matching file: {result.file_name}", style="dim")
        )

        table.add_row(
            str(result.idx),
            type_badge,
            name_text,
            line_text,
            snippet_text,
            format_size(result.file_size),
            Text(result.rel_path),
            key=str(result.idx),
        )

    def _update_progress_ui(
        self,
        search_id: int,
        scanned: int,
        found: int,
        elapsed: float,
        running: bool,
    ) -> None:
        """Refresh the live counters."""
        if search_id != self._search_id:
            return

        self._update_counters(scanned, found, elapsed)

        # A user stop freezes the label; the finish callback owns the final text.
        if running and not self._user_stopped:
            speed = int(scanned / max(0.01, elapsed))
            self.query_one("#lbl_status", Label).update(f"Searching  ·  {speed:,} files/s")

    def _update_counters(self, scanned: int, found: int, elapsed: float) -> None:
        self._total_scanned = scanned
        self._total_found = found
        self.query_one("#pill_matches", Label).update(f"{found:,} matches")
        self.query_one("#pill_scanned", Label).update(f"{scanned:,} files")
        self.query_one("#pill_time", Label).update(f"{elapsed:.2f}s")

    def _finish_search_ui(
        self,
        search_id: int,
        engine: SearchEngine,
        scanned: int,
        found: int,
        elapsed: float,
        error: Optional[str] = None,
    ) -> None:
        """Finish the search, restore the controls, and report the outcome."""
        if search_id != self._search_id:
            return

        self.is_searching = False
        self.query_one("#btn_search", Button).disabled = False
        self.query_one("#btn_stop", Button).disabled = True
        self.query_one("#progress_bar", ProgressBar).styles.display = "none"
        self._update_counters(scanned, found, elapsed)

        if self.current_engine is engine:
            self.current_engine = None

        if self._sort_key and self.results_map:
            self._apply_sort(restore_cursor=False)

        speed = int(scanned / max(0.01, elapsed))
        status_label = self.query_one("#lbl_status", Label)

        if error:
            status_label.update(f"Search failed  ·  {escape(error)}")
            if found == 0:
                self._show_empty("Search failed. See the status bar for details.")
            else:
                self._hide_empty()
            return

        if engine.limit_reached:
            status = f"Result cap reached  ·  {found:,} matches"
        elif engine.is_stopped() or self._user_stopped:
            status = f"Stopped  ·  {found:,} matches"
        else:
            status = f"Done  ·  {speed:,} files/s"

        if engine.errors:
            status = f"{status}  ·  {engine.errors:,} unreadable"
        status_label.update(status)

        if found == 0:
            if engine.is_stopped() or self._user_stopped:
                self._show_empty("Search stopped before any match.")
            elif engine.errors:
                self._show_empty(
                    f"No matches for “{escape(self._last_query)}”."
                    f"\n{engine.errors:,} files or folders could not be read."
                )
            else:
                self._show_empty(f"No matches for “{escape(self._last_query)}”.")
        else:
            self._hide_empty()

    def _show_empty(self, message: str) -> None:
        hint = self.query_one("#empty_hint", Static)
        hint.update(message)
        hint.display = True

    def _hide_empty(self) -> None:
        self.query_one("#empty_hint", Static).display = False

    def stop_search(self) -> None:
        """Stop the running search; the finish callback reports the outcome."""
        was_searching = self.is_searching
        self._user_stopped = True
        self._halt_engine()
        self.is_searching = False

        self.query_one("#btn_search", Button).disabled = False
        self.query_one("#btn_stop", Button).disabled = True
        self.query_one("#progress_bar", ProgressBar).styles.display = "none"

        if was_searching:
            self.query_one("#lbl_status", Label).update("Stopping…")

    def _halt_engine(self) -> None:
        if self.current_engine is not None:
            self.current_engine.stop()
            self.current_engine = None

    # -- result table: selection and sorting ---------------------------------

    @on(DataTable.HeaderSelected, "#results_table")
    def on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Sort results when a column header is clicked."""
        column_key = str(event.column_key.value)
        if column_key not in SORT_KEY_FUNCS:
            return

        # The first click on a column sorts ascending; later clicks toggle.
        if column_key in self._sort_reverse:
            reverse = not self._sort_reverse[column_key]
        else:
            reverse = False
        self._sort_reverse[column_key] = reverse
        self._sort_key = column_key
        self._update_sort_pill()

        if self.results_map:
            self._apply_sort()

    def _apply_sort(self, restore_cursor: bool = True) -> None:
        if not self._sort_key or not self.results_map:
            return
        sort_key = self._sort_key
        reverse = self._sort_reverse.get(sort_key, False)
        key_func = SORT_KEY_FUNCS.get(sort_key)
        if key_func is None:
            return

        table = self.query_one("#results_table", DataTable)
        try:
            current_row = table.cursor_row if table.row_count else 0
        except Exception:  # noqa: BLE001 - cursor may be unavailable
            current_row = 0

        ordered = sorted(
            self.results_map.values(),
            key=key_func,
            reverse=reverse,
        )
        table.clear()
        for result in ordered:
            self._add_result_row(table, result)

        if restore_cursor and ordered:
            table.move_cursor(row=min(current_row, len(ordered) - 1))

    def _update_sort_pill(self) -> None:
        pill = self.query_one("#pill_sort", Label)
        if not self._sort_key:
            pill.display = False
            return

        arrow = "▼" if self._sort_reverse.get(self._sort_key, False) else "▲"
        label = SORT_LABELS.get(self._sort_key, self._sort_key)
        pill.update(f"{label} {arrow}")
        pill.display = True

    @on(DataTable.RowSelected, "#results_table")
    def on_result_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row (or a second click) opens the preview."""
        try:
            row_key_value = int(str(event.row_key.value))
            result = self.results_map.get(row_key_value)
            if result:
                self.push_screen(FilePreviewModal(result))
        except (ValueError, TypeError):
            pass

    def get_selected_result(self) -> Optional[SearchResult]:
        """Return the SearchResult for the selected table row."""
        table = self.query_one("#results_table", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            row_key_value = int(str(cell_key.row_key.value))
            return self.results_map.get(row_key_value)
        except Exception:  # noqa: BLE001 - an empty cursor is not an error
            return None

    def on_key(self, event) -> None:
        """Handle single-key shortcuts when no dialog or text field is active."""
        if isinstance(self.screen, ModalScreen):
            return
        if isinstance(self.focused, Input):
            return

        character = event.character
        if not character:
            return

        if character == "/":
            event.stop()
            self.action_focus_search()
            return

        if character == "o":
            self._act_on_selected(self._open_selected)
        elif character == "f":
            self._act_on_selected(self._reveal_selected)
        elif character == "c":
            self._act_on_selected(self._copy_selected_path)
        elif character == "y":
            self._act_on_selected(self._copy_selected_snippet)
        else:
            return
        event.stop()

    def _act_on_selected(self, action: Callable[[SearchResult], None]) -> None:
        result = self.get_selected_result()
        if result is None:
            self.notify("Select a result first.", severity="warning")
            return
        action(result)

    def _open_selected(self, result: SearchResult) -> None:
        if open_file_in_system(result.file_path):
            self.notify(f"Opening {escape(result.file_name)}…")
        else:
            self.notify("Could not open the file.", severity="warning")

    def _reveal_selected(self, result: SearchResult) -> None:
        if reveal_in_file_manager(result.file_path):
            self.notify(f"Revealing {escape(result.file_name)}…")
        else:
            self.notify("Could not reveal the file.", severity="warning")

    def _copy_selected_path(self, result: SearchResult) -> None:
        if copy_to_clipboard(result.file_path):
            self.notify("Path copied to the clipboard.")
        else:
            self.notify("Could not copy the path.", severity="warning")

    def _copy_selected_snippet(self, result: SearchResult) -> None:
        text = result.snippet or result.file_name
        if copy_to_clipboard(text):
            self.notify("Snippet copied." if result.snippet else "File name copied.")
        else:
            self.notify("Could not copy the text.", severity="warning")

    # -- global keyboard shortcuts -------------------------------------------

    def action_focus_search(self) -> None:
        field = self.query_one("#input_query", Input)
        field.focus()
        select_all = getattr(field, "action_select_all", None)
        if select_all is not None:
            select_all()

    def action_open_folder_picker(self) -> None:
        self.open_folder_picker()

    def action_clear_results(self) -> None:
        self.clear_results()

    def action_stop_or_unfocus(self) -> None:
        if self.is_searching:
            self.stop_search()
            return

        table = self.query_one("#results_table", DataTable)
        if table.row_count:
            table.focus()
        else:
            self.set_focus(None)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit_app(self) -> None:
        self._halt_engine()
        self.exit()


# -----------------------------------------------------------------------------
# Command-line entry point
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="findfile",
        description="FindFile — fast terminal search for file names and file contents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  findfile.py                           # Open in the current folder
  findfile.py ~/Documents               # Open in a chosen folder
  findfile.py ~/Projects "config"       # Search file names for 'config'
  findfile.py . "def main" -c           # Search file contents for 'def main'
  findfile.py . "def\\s+test_" -c -r    # Search file contents with a regex
  findfile.py . "api_key" -c -e py,js   # Only .py and .js files
  findfile.py . "TODO" --no-name -p      # Print content matches, no UI
        """,
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder to search (default: current directory)",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="Initial search term (optional)",
    )
    parser.add_argument(
        "-c",
        "--content",
        action="store_true",
        help="Also search inside file contents",
    )
    parser.add_argument(
        "--no-name",
        action="store_true",
        help="Do not match file names; search contents only (implies -c)",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="print_only",
        action="store_true",
        help="Print matches to stdout and exit, without the UI "
        "(exit status 0 on a match, 1 on none)",
    )
    parser.add_argument(
        "-r",
        "--regex",
        action="store_true",
        help="Treat the search term as a regular expression",
    )
    parser.add_argument(
        "-s",
        "--case-sensitive",
        action="store_true",
        help="Match case exactly",
    )
    parser.add_argument(
        "-e",
        "--ext",
        default="",
        metavar="LIST",
        help="Comma-separated extension filter, for example py,js,txt",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=f"Content scan threads (default: CPU-based, max {MAX_THREADS})",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=MAX_TOTAL_RESULTS,
        metavar="N",
        help=f"Stop after N matches (default: {MAX_TOTAL_RESULTS})",
    )
    parser.add_argument(
        "--no-ignore",
        action="store_true",
        help="Also search hidden folders and development caches",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def format_print_line(result: SearchResult, display_root: str) -> str:
    """Format one result as ``path`` or ``path:line:snippet`` (grep style)."""
    path = os.path.normpath(os.path.join(display_root, result.rel_path))
    if result.match_type == "content" and result.line_number is not None:
        return f"{path}:{result.line_number}:{result.snippet}"
    return path


def run_print_mode(
    args: argparse.Namespace,
    stdout=None,
    stderr=None,
) -> int:
    """Run one search without the UI and print sorted results.

    Returns 0 when something matched, 1 when nothing did, and 130 when the
    search was interrupted.
    """
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    collected: List[SearchResult] = []
    engine = SearchEngine(
        root_dir=args.folder,
        query=args.query,
        search_name=not args.no_name,
        search_content=args.content or args.no_name,
        case_sensitive=args.case_sensitive,
        ignore_caches=not args.no_ignore,
        use_regex=args.regex,
        ext_filter=parse_extensions(args.ext),
        max_workers=args.workers,
        max_results=args.max_results,
        on_batch=collected.extend,
    )

    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
        return 130

    collected.sort(
        key=lambda result: (
            result.rel_path,
            0 if result.match_type == "name" else 1,
            result.line_number or 0,
        )
    )
    for result in collected:
        stdout.write(format_print_line(result, args.folder) + "\n")
    stdout.flush()

    if engine.limit_reached:
        stderr.write(
            f"findfile: stopped at {engine.max_results:,} results (--max-results)\n"
        )
    if engine.errors:
        stderr.write(f"findfile: {engine.errors:,} files or folders could not be read\n")
    return 0 if collected else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        parser.error(f"folder not found: {args.folder}")

    if args.workers is not None and not 1 <= args.workers <= 256:
        parser.error("--workers must be between 1 and 256")

    if args.max_results < 1:
        parser.error("--max-results must be at least 1")

    if args.regex and args.query:
        try:
            re.compile(args.query)
        except re.error as exc:
            parser.error(f"invalid regular expression: {exc}")

    if args.print_only:
        if not args.query:
            parser.error("--print needs a search term")
        return run_print_mode(args)

    app = FindFileApp(
        initial_folder=str(folder),
        initial_query=args.query,
        initial_name=not args.no_name,
        initial_content=args.content or args.no_name,
        initial_regex=args.regex,
        initial_case=args.case_sensitive,
        initial_exts=args.ext,
        initial_ignore=not args.no_ignore,
        workers=args.workers,
        max_results=args.max_results,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
