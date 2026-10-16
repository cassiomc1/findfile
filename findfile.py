#!/usr/bin/env python3
"""
FindFile — fast terminal search for file names and file contents.
Multithreaded scan with a Textual interface.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
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


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------

@dataclass
class SearchResult:
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
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_mtime(timestamp: float) -> str:
    """Format a modification timestamp."""
    try:
        dt = datetime.datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def is_binary_file(filepath: str) -> bool:
    """Return True when the file looks binary."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(BINARY_SNIFF_BYTES)
            if b"\x00" in chunk:
                return True
    except Exception:
        return True
    return False


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard (pbcopy, wl-copy, or xclip)."""
    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                    return True
                except Exception:
                    pass
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
        elif sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception:
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
        elif sys.platform.startswith("linux"):
            folder = os.path.dirname(filepath)
            subprocess.Popen(
                ["xdg-open", folder],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception:
        pass
    return False


def highlight_match(
    text: str,
    query: str,
    case_sensitive: bool = False,
    use_regex: bool = False,
) -> Text:
    """Return Rich text with the term or regular expression highlighted."""
    rich_text = Text()
    if not query:
        rich_text.append(text)
        return rich_text

    try:
        pattern = query if use_regex else re.escape(query)
        flags = 0 if case_sensitive else re.IGNORECASE
        last_idx = 0
        for match in re.finditer(pattern, text, flags=flags):
            start, end = match.span()
            if start == end:
                continue
            if start > last_idx:
                rich_text.append(text[last_idx:start])
            rich_text.append(text[start:end], style="bold black on #f1c40f")
            last_idx = end

        if last_idx < len(text):
            rich_text.append(text[last_idx:])
    except Exception:
        rich_text.append(text)

    return rich_text


# -----------------------------------------------------------------------------
# Multithreaded search engine
# -----------------------------------------------------------------------------

class SearchEngine:
    """Multithreaded search over file names and file contents."""

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
        self.ext_filter = ext_filter
        self.max_results = max_results

        cpu_count = os.cpu_count() or 4
        self.max_workers = max_workers or min(32, cpu_count * 4)

        self.on_batch = on_batch
        self.on_progress = on_progress

        self.stop_event = threading.Event()
        self.limit_reached = False
        self._files_scanned = 0
        self._results_found = 0
        self._counter_lock = threading.Lock()
        self._next_result_id = 0

        self._regex_pattern: Optional[Pattern[str]] = None
        if self.use_regex and self.query:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            self._regex_pattern = re.compile(self.query, flags=flags)

    def stop(self) -> None:
        """Ask every worker to stop immediately."""
        self.stop_event.set()

    def is_stopped(self) -> bool:
        return self.stop_event.is_set()

    def run(self) -> Tuple[int, int, float]:
        """Run the search. Returns (files scanned, matches, elapsed seconds)."""
        start_time = time.perf_counter()

        if not os.path.isdir(self.root_dir) or not self.query:
            return 0, 0, 0.0

        # Bounded queues keep memory use in check.
        file_queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=1500)
        result_queue: queue.Queue[Optional[SearchResult]] = queue.Queue(maxsize=2000)

        query_bytes = self.query.encode("utf-8")
        if not self.case_sensitive and self.query.isascii():
            query_bytes = query_bytes.lower()

        query_str = self.query if self.case_sensitive else self.query.lower()

        # Thread that forwards result batches to the UI.
        dispatcher_stop = threading.Event()

        def result_dispatcher():
            batch: List[SearchResult] = []
            last_flush = time.perf_counter()
            last_progress = last_flush

            while not dispatcher_stop.is_set() or not result_queue.empty():
                try:
                    item = result_queue.get(timeout=0.03)
                    if item is not None:
                        batch.append(item)
                    result_queue.task_done()
                except queue.Empty:
                    pass

                now = time.perf_counter()
                if batch and (len(batch) >= 30 or (now - last_flush) >= 0.06):
                    if self.on_batch and (not self.stop_event.is_set() or self.limit_reached):
                        self.on_batch(batch)
                    batch = []
                    last_flush = now

                # Report progress on a timer.
                if self.on_progress and (now - last_progress) >= 0.06:
                    with self._counter_lock:
                        scanned = self._files_scanned
                        found = self._results_found
                    self.on_progress(scanned, found, now - start_time, True)
                    last_progress = now

            # Esvazia lote residual final
            if batch and self.on_batch and (not self.stop_event.is_set() or self.limit_reached):
                self.on_batch(batch)

        dispatcher_thread = threading.Thread(target=result_dispatcher, daemon=True)
        dispatcher_thread.start()

        # Content scan performed by worker threads.
        def content_worker():
            while not self.stop_event.is_set():
                try:
                    file_path = file_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                if file_path is None:
                    file_queue.task_done()
                    break

                try:
                    self._check_file_content(file_path, query_bytes, query_str, result_queue)
                except Exception:
                    pass
                finally:
                    file_queue.task_done()

        # Start content workers when content search is enabled.
        workers: List[threading.Thread] = []
        if self.search_content:
            for _ in range(self.max_workers):
                t = threading.Thread(target=content_worker, daemon=True)
                t.start()
                workers.append(t)

        # Main filesystem traversal thread
        try:
            self._crawl_filesystem(file_queue, result_queue, query_str)
        finally:
            # Tell content workers to exit.
            if self.search_content:
                for _ in range(len(workers)):
                    while not self.stop_event.is_set():
                        try:
                            file_queue.put(None, timeout=0.05)
                            break
                        except queue.Full:
                            continue

                for t in workers:
                    t.join(timeout=0.5)

            # Tell the dispatcher to exit.
            dispatcher_stop.set()
            dispatcher_thread.join(timeout=1.0)

        elapsed = time.perf_counter() - start_time

        if self.on_progress:
            with self._counter_lock:
                scanned = self._files_scanned
                found = self._results_found
            self.on_progress(scanned, found, elapsed, False)

        return self._files_scanned, self._results_found, elapsed

    def _register_result(self) -> Optional[int]:
        """Count one match and stop once the result cap is hit."""
        with self._counter_lock:
            if self._results_found >= self.max_results:
                self.limit_reached = True
                self.stop_event.set()
                return None
            self._next_result_id += 1
            self._results_found += 1
            if self._results_found >= self.max_results:
                self.limit_reached = True
                self.stop_event.set()
            return self._next_result_id

    def _crawl_filesystem(
        self,
        file_queue: queue.Queue[Optional[str]],
        result_queue: queue.Queue[Optional[SearchResult]],
        query_str: str,
    ) -> None:
        """Walk the directory tree with os.scandir."""
        stack = [self.root_dir]
        visited_dirs: Set[Tuple[int, int]] = set()

        has_wildcard = (not self.use_regex) and ("*" in self.query or "?" in self.query)

        while stack and not self.stop_event.is_set():
            current_dir = stack.pop()

            try:
                stat_res = os.stat(current_dir)
                dir_key = (stat_res.st_dev, stat_res.st_ino)
                if dir_key in visited_dirs:
                    continue
                visited_dirs.add(dir_key)
            except OSError:
                continue

            try:
                with os.scandir(current_dir) as entries:
                    for entry in entries:
                        if self.stop_event.is_set():
                            return

                        name = entry.name

                        # Skip ignored directories.
                        if entry.is_dir(follow_symlinks=False):
                            if self.ignore_caches:
                                if name.startswith(".") or name in IGNORED_DIRS:
                                    continue
                            stack.append(entry.path)
                            continue

                        # Skip if not a regular file.
                        if not entry.is_file(follow_symlinks=False):
                            continue

                        with self._counter_lock:
                            self._files_scanned += 1

                        # Optional extension filter.
                        if self.ext_filter is not None:
                            ext = os.path.splitext(name)[1].lower().lstrip(".")
                            if ext not in self.ext_filter:
                                continue

                        # 1. Name match.
                        if self.search_name:
                            if self.use_regex and self._regex_pattern is not None:
                                name_matched = bool(self._regex_pattern.search(name))
                            else:
                                name_to_check = name if self.case_sensitive else name.lower()
                                if has_wildcard:
                                    fn = (
                                        fnmatch.fnmatchcase
                                        if self.case_sensitive
                                        else fnmatch.fnmatch
                                    )
                                    name_matched = fn(name_to_check, query_str)
                                else:
                                    name_matched = query_str in name_to_check

                            if name_matched:
                                try:
                                    st = entry.stat(follow_symlinks=False)
                                    size = st.st_size
                                    mtime = st.st_mtime
                                except OSError:
                                    size, mtime = 0, 0.0

                                res_id = self._register_result()
                                if res_id is None:
                                    return

                                rel = os.path.relpath(entry.path, self.root_dir)
                                res = SearchResult(
                                    idx=res_id,
                                    match_type="name",
                                    file_path=entry.path,
                                    file_name=name,
                                    rel_path=rel,
                                    file_size=size,
                                    mtime=mtime,
                                )
                                self._put_result(result_queue, res)

                        # 2. Queue the file for a content search.
                        if self.search_content and not self.stop_event.is_set():
                            # Skip known binaries before queueing.
                            ext = os.path.splitext(name)[1].lower()
                            if ext not in BINARY_EXTENSIONS:
                                while not self.stop_event.is_set():
                                    try:
                                        file_queue.put(entry.path, timeout=0.05)
                                        break
                                    except queue.Full:
                                        continue
            except (PermissionError, OSError):
                continue

    def _check_file_content(
        self,
        file_path: str,
        query_bytes: bytes,
        query_str: str,
        result_queue: queue.Queue[Optional[SearchResult]],
    ) -> None:
        """Open the file once, reject binaries, and search its contents."""
        try:
            with open(file_path, "rb") as f:
                st = os.fstat(f.fileno())
                size = st.st_size
                if size == 0 or size > MAX_CONTENT_FILE_SIZE:
                    return

                head = f.read(BINARY_SNIFF_BYTES)
                if b"\x00" in head:
                    return

                rest = f.read()
                data = head if not rest else head + rest
                mtime = st.st_mtime

            # Fast byte path for ASCII literals and case-sensitive searches.
            use_fast_bytes = (not self.use_regex) and (
                self.case_sensitive or self.query.isascii()
            )

            if use_fast_bytes:
                data_to_search = data if self.case_sensitive else data.lower()
                if query_bytes not in data_to_search:
                    return

                fname = os.path.basename(file_path)
                rel = os.path.relpath(file_path, self.root_dir)
                matches_in_file = 0

                for line_idx, raw_line in enumerate(data.splitlines(), start=1):
                    if self.stop_event.is_set():
                        return

                    line_check = raw_line if self.case_sensitive else raw_line.lower()
                    if query_bytes in line_check:
                        text_line = raw_line.decode("utf-8", errors="replace").strip()
                        if len(text_line) > 200:
                            text_line = text_line[:197] + "..."

                        res_id = self._register_result()
                        if res_id is None:
                            return

                        res = SearchResult(
                            idx=res_id,
                            match_type="content",
                            file_path=file_path,
                            file_name=fname,
                            rel_path=rel,
                            line_number=line_idx,
                            snippet=text_line,
                            file_size=size,
                            mtime=mtime,
                        )
                        self._put_result(result_queue, res)

                        matches_in_file += 1
                        if matches_in_file >= MAX_MATCHES_PER_FILE:
                            break
            else:
                # Full Unicode path for regex and case-insensitive accented terms.
                text_data = data.decode("utf-8", errors="replace")
                if self.use_regex and self._regex_pattern is not None:
                    if not self._regex_pattern.search(text_data):
                        return
                else:
                    if query_str not in text_data.lower():
                        return

                fname = os.path.basename(file_path)
                rel = os.path.relpath(file_path, self.root_dir)
                matches_in_file = 0

                for line_idx, line in enumerate(text_data.splitlines(), start=1):
                    if self.stop_event.is_set():
                        return

                    if self.use_regex and self._regex_pattern is not None:
                        matched = bool(self._regex_pattern.search(line))
                    else:
                        matched = query_str in line.lower()

                    if matched:
                        text_line = line.strip()
                        if len(text_line) > 200:
                            text_line = text_line[:197] + "..."

                        res_id = self._register_result()
                        if res_id is None:
                            return

                        res = SearchResult(
                            idx=res_id,
                            match_type="content",
                            file_path=file_path,
                            file_name=fname,
                            rel_path=rel,
                            line_number=line_idx,
                            snippet=text_line,
                            file_size=size,
                            mtime=mtime,
                        )
                        self._put_result(result_queue, res)

                        matches_in_file += 1
                        if matches_in_file >= MAX_MATCHES_PER_FILE:
                            break

        except (PermissionError, OSError, ValueError):
            pass

    def _put_result(
        self, result_queue: queue.Queue[Optional[SearchResult]], res: SearchResult
    ) -> None:
        while not self.stop_event.is_set() or self.limit_reached:
            try:
                result_queue.put(res, timeout=0.05)
                break
            except queue.Full:
                if self.stop_event.is_set() and not self.limit_reached:
                    break
                continue


# -----------------------------------------------------------------------------
# Dialogs: folder picker, preview, and help
# -----------------------------------------------------------------------------

class CustomFolderTree(DirectoryTree):
    """Directory tree that lists folders only."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]


class FolderPickerModal(ModalScreen[Optional[str]]):
    """Dialog for picking a folder, with shortcuts."""

    DEFAULT_CSS = """
    FolderPickerModal {
        align: center middle;
        background: rgba(17, 17, 27, 0.82);
    }

    #folder_dialog {
        width: 86%;
        max-width: 96;
        height: 84%;
        border: solid #89b4fa;
        background: #1e1e2e;
        padding: 1 2;
    }

    #folder_title {
        text-style: bold;
        color: #cdd6f4;
        text-align: left;
    }

    #folder_subtitle {
        color: #6c7086;
        margin-bottom: 1;
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
        border: solid #45475a;
        background: #11111b;
        margin-bottom: 1;
    }

    #modal_actions {
        height: auto;
        align: right middle;
    }

    #modal_actions Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, initial_path: str = "."):
        super().__init__()
        resolved = Path(initial_path).expanduser().resolve()
        self.current_path = str(resolved if resolved.is_dir() else Path.cwd())

    def compose(self) -> ComposeResult:
        with Vertical(id="folder_dialog"):
            yield Label("Choose folder", id="folder_title")
            yield Static("Shortcuts, a typed path, or the tree below.", id="folder_subtitle")

            with Horizontal(id="quick_paths"):
                yield Button("Home", id="quick_home", variant="default")
                yield Button("Here", id="quick_cwd", variant="default")
                yield Button("Docs", id="quick_docs", variant="default")
                yield Button("Downloads", id="quick_downloads", variant="default")
                yield Button("Up", id="quick_up", variant="default")

            yield Input(value=self.current_path, id="modal_folder_input")

            # Navigation tree
            yield CustomFolderTree(self.current_path, id="folder_tree")

            with Horizontal(id="modal_actions"):
                yield Button("Cancel", id="btn_modal_cancel", variant="default")
                yield Button("Use this folder", id="btn_modal_confirm", variant="primary")

    @on(DirectoryTree.DirectorySelected)
    def on_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.current_path = str(event.path)
        self.query_one("#modal_folder_input", Input).value = self.current_path

    @on(Button.Pressed, "#quick_home")
    def go_home(self) -> None:
        self._update_tree(str(Path.home()))

    @on(Button.Pressed, "#quick_cwd")
    def go_cwd(self) -> None:
        self._update_tree(str(Path.cwd()))

    @on(Button.Pressed, "#quick_docs")
    def go_docs(self) -> None:
        docs = Path.home() / "Documents"
        if docs.is_dir():
            self._update_tree(str(docs))

    @on(Button.Pressed, "#quick_downloads")
    def go_downloads(self) -> None:
        dl = Path.home() / "Downloads"
        if dl.is_dir():
            self._update_tree(str(dl))

    @on(Button.Pressed, "#quick_up")
    def go_up(self) -> None:
        parent = Path(self.current_path).parent
        if parent.is_dir():
            self._update_tree(str(parent))

    def _update_tree(self, new_path: str) -> None:
        try:
            p = Path(new_path).expanduser().resolve()
            if p.is_dir():
                self.current_path = str(p)
                self.query_one("#modal_folder_input", Input).value = self.current_path
                tree = self.query_one("#folder_tree", CustomFolderTree)
                tree.path = p
        except Exception:
            pass

    @on(Input.Submitted, "#modal_folder_input")
    def on_input_submit(self, event: Input.Submitted) -> None:
        p = Path(event.value).expanduser().resolve()
        if p.is_dir():
            self._update_tree(str(p))

    @on(Button.Pressed, "#btn_modal_confirm")
    def action_confirm(self) -> None:
        input_val = self.query_one("#modal_folder_input", Input).value
        p = Path(input_val).expanduser().resolve()
        if p.is_dir():
            self.dismiss(str(p))
        else:
            self.dismiss(self.current_path)

    @on(Button.Pressed, "#btn_modal_cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePreviewModal(ModalScreen[None]):
    """Dialog with file metadata, a code preview, and actions."""

    DEFAULT_CSS = """
    FilePreviewModal {
        align: center middle;
        background: rgba(17, 17, 27, 0.84);
    }

    #preview_dialog {
        width: 92%;
        max-width: 128;
        height: 88%;
        border: solid #89b4fa;
        background: #1e1e2e;
        padding: 1 2;
    }

    #preview_title {
        text-style: bold;
        color: #cdd6f4;
    }

    #preview_path {
        color: #6c7086;
    }

    #preview_meta {
        color: #a6adc8;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: solid #313244;
    }

    #preview_scroll {
        height: 1fr;
        border: solid #45475a;
        background: #11111b;
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
    """

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
        safe_path = escape(self.result.file_path)
        safe_size = escape(format_size(self.result.file_size))
        safe_mtime = escape(format_mtime(self.result.mtime))

        with Vertical(id="preview_dialog"):
            yield Label(self.result.file_name, id="preview_title")
            yield Static(safe_path, id="preview_path")

            match_desc = (
                f"content, line {self.result.line_number}"
                if self.result.match_type == "content"
                else "file name"
            )
            yield Static(
                f"[bold]{safe_size}[/]   [#6c7086]·[/]   [bold]{safe_mtime}[/]"
                f"   [#6c7086]·[/]   [bold #a6e3a1]{match_desc}[/]",
                id="preview_meta",
            )

            with VerticalScroll(id="preview_scroll"):
                yield self._build_preview_widget()

            with Horizontal(id="preview_actions"):
                yield Button("Open  o", id="btn_open_file", variant="primary")
                yield Button("Reveal  f", id="btn_reveal_file", variant="default")
                yield Button("Path  c", id="btn_copy_path", variant="default")
                if self.result.snippet:
                    yield Button("Snippet  y", id="btn_copy_snippet", variant="default")
                yield Button("Close", id="btn_close", variant="default")

    def _build_preview_widget(self) -> Static:
        """Load a syntax-highlighted preview of the file."""
        p = Path(self.result.file_path)
        if not p.is_file():
            return Static("[red]File not found or unreadable.[/]")

        if is_binary_file(self.result.file_path):
            return Static(
                "[italic yellow]Binary file. Text preview is unavailable.[/]"
            )

        try:
            line_num = self.result.line_number
            if line_num is not None:
                start_l = max(1, line_num - 25)
                end_l = line_num + 25
                syntax = Syntax.from_path(
                    str(p),
                    line_numbers=True,
                    line_range=(start_l, end_l),
                    highlight_lines={line_num},
                    theme="monokai",
                    word_wrap=True,
                )
            else:
                syntax = Syntax.from_path(
                    str(p),
                    line_numbers=True,
                    line_range=(1, 80),
                    theme="monokai",
                    word_wrap=True,
                )
            return Static(syntax)
        except Exception as e:
            return Static(f"[red]Could not read preview:[/] {escape(str(e))}")

    @on(Button.Pressed, "#btn_open_file")
    def action_open_sys(self) -> None:
        open_file_in_system(self.result.file_path)
        self.notify("Opened in the default application.")

    @on(Button.Pressed, "#btn_reveal_file")
    def action_reveal_finder(self) -> None:
        reveal_in_file_manager(self.result.file_path)
        self.notify("Revealed in the file manager.")

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

    @on(Button.Pressed, "#btn_close")
    def action_dismiss_modal(self) -> None:
        self.dismiss()


class HelpModal(ModalScreen[None]):
    """Dialog listing keyboard shortcuts."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
        background: rgba(17, 17, 27, 0.82);
    }

    #help_dialog {
        width: 78%;
        max-width: 92;
        height: auto;
        max-height: 90%;
        border: solid #89b4fa;
        background: #1e1e2e;
        padding: 1 2;
    }

    #help_title {
        text-style: bold;
        color: #cdd6f4;
    }

    #help_subtitle {
        color: #6c7086;
        margin-bottom: 1;
    }

    #help_grid {
        grid-size: 2;
        grid-gutter: 0 2;
        grid-columns: 18 1fr;
        height: auto;
    }

    .help_key {
        color: #a6e3a1;
        text-style: bold;
    }

    .help_desc {
        color: #cdd6f4;
    }

    #btn_close_help {
        margin-top: 1;
        height: auto;
        align: right middle;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        shortcuts = (
            ("Enter", "Start a search from the fields"),
            ("Esc", "Stop the search or close the dialog"),
            ("Enter on row", "Open the file preview"),
            ("Column header", "Sort the table by that column"),
            ("Ctrl+F  /", "Focus the search field"),
            ("Ctrl+P", "Open the folder picker"),
            ("Ctrl+L", "Clear the results"),
            ("o", "Open the file in the default app"),
            ("f", "Reveal the file in Finder"),
            ("c", "Copy the full path"),
            ("y", "Copy the matched snippet"),
            ("F1  ?", "Show this help"),
            ("q", "Quit"),
        )
        with Vertical(id="help_dialog"):
            yield Label("Shortcuts", id="help_title")
            yield Static("The footer lists global keys. These work on the table.", id="help_subtitle")
            with Grid(id="help_grid"):
                for key, desc in shortcuts:
                    yield Static(key, classes="help_key")
                    yield Static(desc, classes="help_desc")
            with Horizontal(id="btn_close_help"):
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
    SUB_TITLE = "Names and content  ·  parallel search  ·  preview"
    COMMAND_PALETTE_BINDING = "ctrl+k"

    CSS = """
    Screen {
        background: #11111b;
        color: #cdd6f4;
    }

    Header {
        background: #11111b;
        color: #cdd6f4;
    }

    Footer {
        background: #181825;
        color: #a6adc8;
    }

    #search_panel {
        height: auto;
        border: solid #313244;
        background: #1e1e2e;
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
        color: #89b4fa;
        padding-right: 1;
    }

    Input {
        border: tall #45475a;
        background: #11111b;
    }

    Input:focus {
        border: tall #89b4fa;
    }

    #input_query, #input_folder {
        width: 1fr;
    }

    .btn_compact {
        margin-left: 1;
        min-width: 10;
    }

    #btn_search {
        min-width: 12;
    }

    #options_row Checkbox {
        margin-right: 1;
        background: transparent;
        width: auto;
    }

    #input_exts {
        width: 1fr;
    }

    #status_bar {
        height: 1;
        background: #181825;
        padding: 0 1;
        margin: 1 1 0 1;
        align: left middle;
    }

    #lbl_status {
        width: 1fr;
        color: #a6adc8;
    }

    .stat_pill {
        padding: 0 1;
        margin-left: 1;
        text-style: bold;
        background: #313244;
        color: #cdd6f4;
    }

    #pill_matches {
        color: #a6e3a1;
    }

    #pill_scanned {
        color: #89b4fa;
    }

    #pill_time {
        color: #f9e2af;
    }

    #pill_threads {
        color: #cba6f7;
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
    }

    #results_container {
        height: 1fr;
        margin: 0 1 0 1;
        border: solid #313244;
        background: #11111b;
    }

    #results_table {
        height: 100%;
        background: #11111b;
    }

    #empty_hint {
        dock: top;
        height: 100%;
        width: 100%;
        content-align: center middle;
        text-align: center;
        color: #6c7086;
        background: #11111b;
    }

    DataTable > .datatable--header {
        background: #181825;
        color: #89b4fa;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #313244;
        color: #cdd6f4;
    }

    DataTable > .datatable--even-row {
        background: #181825;
    }

    DataTable > .datatable--odd-row {
        background: #11111b;
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
        initial_content: bool = False,
        initial_regex: bool = False,
    ):
        super().__init__()
        self.initial_folder = str(Path(initial_folder).expanduser().resolve())
        self.initial_query = initial_query
        self.initial_content = initial_content
        self.initial_regex = initial_regex

        cpu_count = os.cpu_count() or 4
        self.default_workers = min(32, cpu_count * 4)

        self.current_engine: Optional[SearchEngine] = None
        self.results_map: dict[int, SearchResult] = {}
        self.is_searching = False
        self._search_id = 0
        self._total_found = 0
        self._total_scanned = 0
        self._last_query = ""
        self._last_case_sensitive = False
        self._last_use_regex = False
        self._sort_reverse: dict[str, bool] = {}

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
                yield Button("Stop", id="btn_stop", variant="error", disabled=True, classes="btn_compact")
                yield Button("Clear", id="btn_clear", variant="default", classes="btn_compact")

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
                yield Checkbox("Name", value=True, id="chk_name")
                yield Checkbox("Text", value=self.initial_content, id="chk_content")
                yield Checkbox("Case", value=False, id="chk_case")
                yield Checkbox("Regex", value=self.initial_regex, id="chk_regex")
                yield Checkbox("Skip cache", value=True, id="chk_ignore")

            with Horizontal(classes="form_row"):
                yield Label("Ext.", classes="row_label")
                yield Input(
                    placeholder="py, js, txt  ·  empty matches every extension",
                    id="input_exts",
                )

        with Horizontal(id="status_bar"):
            yield Label("Ready.", id="lbl_status")
            yield Label("0 matches", id="pill_matches", classes="stat_pill")
            yield Label("0 files", id="pill_scanned", classes="stat_pill")
            yield Label("0.0s", id="pill_time", classes="stat_pill")
            yield Label(f"{self.default_workers} threads", id="pill_threads", classes="stat_pill")

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
        """Set up table columns and focus the search field."""
        table = self.query_one("#results_table", DataTable)
        table.add_column("#", key="idx")
        table.add_column("Type", key="type")
        table.add_column("File", key="name")
        table.add_column("Line", key="line")
        table.add_column("Match", key="snippet")
        table.add_column("Size", key="size")
        table.add_column("Relative path", key="rel_path")

        # A query passed on the command line starts a search immediately.
        if self.initial_query:
            self.start_search()
        else:
            self.query_one("#input_query", Input).focus()

    # -------------------------------------------------------------------------
    # Actions and clicks
    # -------------------------------------------------------------------------

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
        self._search_id += 1
        self.stop_search()
        table = self.query_one("#results_table", DataTable)
        table.clear()
        self.results_map.clear()
        self._total_found = 0
        self._total_scanned = 0
        self.query_one("#lbl_status", Label).update("Results cleared.")
        self.query_one("#pill_matches", Label).update("0 matches")
        self.query_one("#pill_scanned", Label).update("0 files")
        self.query_one("#pill_time", Label).update("0.0s")
        self._show_empty(
            "Type a term and press Enter.\n"
            "In the table: Enter previews  ·  o opens  ·  f reveals  ·  c copies"
        )

    # -------------------------------------------------------------------------
    # Search control
    # -------------------------------------------------------------------------

    def start_search(self) -> None:
        query = self.query_one("#input_query", Input).value.strip()
        folder = self.query_one("#input_folder", Input).value.strip()

        if not query:
            self.notify("Enter a search term.", severity="warning")
            self.query_one("#input_query", Input).focus()
            return

        folder_path = Path(folder).expanduser().resolve()
        if not folder_path.is_dir():
            self.notify(f"Folder '{folder}' does not exist.", severity="error")
            return

        search_name = self.query_one("#chk_name", Checkbox).value
        search_content = self.query_one("#chk_content", Checkbox).value

        if not search_name and not search_content:
            self.notify(
                "Turn on Name, Text, or both.", severity="warning"
            )
            return

        case_sensitive = self.query_one("#chk_case", Checkbox).value
        use_regex = self.query_one("#chk_regex", Checkbox).value
        ignore_caches = self.query_one("#chk_ignore", Checkbox).value

        if use_regex:
            try:
                re.compile(query)
            except re.error as err:
                self.notify(f"Invalid regular expression: {err}", severity="error")
                return

        # Parse extensions
        ext_raw = self.query_one("#input_exts", Input).value.strip()
        ext_set: Optional[Set[str]] = None
        if ext_raw:
            ext_set = {
                e.strip().lstrip("*.").lower()
                for e in ext_raw.split(",")
                if e.strip().lstrip("*.")
            }

        # Stop any search already running and ignore its callbacks.
        self.stop_search()
        self._search_id += 1
        current_search_id = self._search_id

        self._last_query = query
        self._last_case_sensitive = case_sensitive
        self._last_use_regex = use_regex

        # Clear the table and the result map.
        table = self.query_one("#results_table", DataTable)
        table.clear()
        self.results_map.clear()
        self._total_found = 0
        self._total_scanned = 0

        # Update the visible state.
        self.is_searching = True
        self.query_one("#btn_search", Button).disabled = True
        self.query_one("#btn_stop", Button).disabled = False
        self.query_one("#lbl_status", Label).update("Searching…")
        self.query_one("#progress_bar", ProgressBar).styles.display = "block"
        self._show_empty("Scanning the folder…")

        # Start the search worker.
        self.run_search_worker(
            search_id=current_search_id,
            root_dir=str(folder_path),
            query=query,
            search_name=search_name,
            search_content=search_content,
            case_sensitive=case_sensitive,
            ignore_caches=ignore_caches,
            use_regex=use_regex,
            ext_filter=ext_set,
        )

    @work(thread=True, exclusive=True)
    def run_search_worker(
        self,
        search_id: int,
        root_dir: str,
        query: str,
        search_name: bool,
        search_content: bool,
        case_sensitive: bool,
        ignore_caches: bool,
        use_regex: bool,
        ext_filter: Optional[Set[str]],
    ) -> None:
        """Run SearchEngine on a background thread."""

        def on_batch_cb(batch: List[SearchResult]) -> None:
            self.call_from_thread(
                self._add_results_batch,
                search_id,
                batch,
                query,
                case_sensitive,
                use_regex,
            )

        def on_progress_cb(scanned: int, found: int, elapsed: float, is_running: bool) -> None:
            self.call_from_thread(
                self._update_progress_ui,
                search_id,
                scanned,
                found,
                elapsed,
                is_running,
            )

        engine = SearchEngine(
            root_dir=root_dir,
            query=query,
            search_name=search_name,
            search_content=search_content,
            case_sensitive=case_sensitive,
            ignore_caches=ignore_caches,
            use_regex=use_regex,
            ext_filter=ext_filter,
            on_batch=on_batch_cb,
            on_progress=on_progress_cb,
        )
        self.current_engine = engine

        scanned, found, elapsed = 0, 0, 0.0
        try:
            scanned, found, elapsed = engine.run()
        finally:
            self.call_from_thread(
                self._finish_search_ui,
                search_id,
                engine,
                scanned,
                found,
                elapsed,
            )

    def _add_results_batch(
        self,
        search_id: int,
        batch: List[SearchResult],
        query: str,
        case_sensitive: bool,
        use_regex: bool,
    ) -> None:
        """Append a result batch to the table on the UI thread."""
        if search_id != self._search_id:
            return

        table = self.query_one("#results_table", DataTable)
        if batch:
            self._hide_empty()

        for res in batch:
            self.results_map[res.idx] = res
            self._add_result_row(table, res, query, case_sensitive, use_regex)

    def _add_result_row(
        self,
        table: DataTable,
        res: SearchResult,
        query: str,
        case_sensitive: bool,
        use_regex: bool,
    ) -> None:
        type_badge = (
            Text("content", style="bold cyan")
            if res.match_type == "content"
            else Text("name", style="bold green")
        )

        name_text = highlight_match(res.file_name, query, case_sensitive, use_regex)
        line_str = f"L{res.line_number}" if res.line_number is not None else "-"

        snippet_text = (
            highlight_match(res.snippet, query, case_sensitive, use_regex)
            if res.snippet
            else Text(f"Matching file: {res.file_name}", style="dim")
        )

        size_str = format_size(res.file_size)

        table.add_row(
            str(res.idx),
            type_badge,
            name_text,
            line_str,
            snippet_text,
            size_str,
            res.rel_path,
            key=str(res.idx),
        )

    def _update_progress_ui(
        self,
        search_id: int,
        scanned: int,
        found: int,
        elapsed: float,
        is_running: bool,
    ) -> None:
        """Refresh the live counters."""
        if search_id != self._search_id:
            return

        self._total_scanned = scanned
        self._total_found = found

        self.query_one("#pill_matches", Label).update(f"{found} matches")
        self.query_one("#pill_scanned", Label).update(f"{scanned} files")
        self.query_one("#pill_time", Label).update(f"{elapsed:.2f}s")

        if is_running:
            speed = int(scanned / max(0.01, elapsed))
            self.query_one("#lbl_status", Label).update(f"Searching  ·  {speed:,} files/s")

    def _finish_search_ui(
        self,
        search_id: int,
        engine: SearchEngine,
        scanned: int,
        found: int,
        elapsed: float,
    ) -> None:
        """Finish the search and re-enable the buttons."""
        if search_id != self._search_id:
            return

        self.is_searching = False
        self.query_one("#btn_search", Button).disabled = False
        self.query_one("#btn_stop", Button).disabled = True
        self.query_one("#progress_bar", ProgressBar).styles.display = "none"

        if engine.limit_reached:
            status_msg = f"Stopped at the limit of {engine.max_results:,} matches."
        elif engine.is_stopped():
            status_msg = "Search stopped."
        else:
            status_msg = "Search finished."

        self.query_one("#lbl_status", Label).update(status_msg)
        if found == 0:
            self._show_empty(f"No matches for “{escape(self._last_query)}”.")
        if self.current_engine is engine:
            self.current_engine = None

    def _show_empty(self, message: str) -> None:
        hint = self.query_one("#empty_hint", Static)
        hint.update(message)
        hint.display = True

    def _hide_empty(self) -> None:
        self.query_one("#empty_hint", Static).display = False

    def stop_search(self) -> None:
        """Stop the search if one is running."""
        if self.current_engine:
            self.current_engine.stop()
        self.is_searching = False
        self.query_one("#btn_search", Button).disabled = False
        self.query_one("#btn_stop", Button).disabled = True
        self.query_one("#progress_bar", ProgressBar).styles.display = "none"

    # -------------------------------------------------------------------------
    # Result table: selection and sorting
    # -------------------------------------------------------------------------

    @on(DataTable.HeaderSelected, "#results_table")
    def on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Sort results when a column header is clicked."""
        if self.is_searching or not self.results_map:
            return

        col_key = str(event.column_key.value)
        reverse = not self._sort_reverse.get(col_key, False)
        self._sort_reverse[col_key] = reverse

        key_funcs = {
            "idx": lambda r: r.idx,
            "type": lambda r: r.match_type,
            "name": lambda r: r.file_name.lower(),
            "line": lambda r: r.line_number or 0,
            "snippet": lambda r: r.snippet.lower(),
            "size": lambda r: r.file_size,
            "rel_path": lambda r: r.rel_path.lower(),
        }
        sort_fn = key_funcs.get(col_key)
        if not sort_fn:
            return

        sorted_results = sorted(self.results_map.values(), key=sort_fn, reverse=reverse)
        table = self.query_one("#results_table", DataTable)
        table.clear()
        for res in sorted_results:
            self._add_result_row(
                table,
                res,
                self._last_query,
                self._last_case_sensitive,
                self._last_use_regex,
            )

    @on(DataTable.RowSelected, "#results_table")
    def on_result_selected(self, event: DataTable.RowSelected) -> None:
        """Enter or a double-click opens the preview."""
        try:
            row_key_val = int(str(event.row_key.value))
            result = self.results_map.get(row_key_val)
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
            row_key_val = int(str(cell_key.row_key.value))
            return self.results_map.get(row_key_val)
        except Exception:
            return None

    def on_key(self, event) -> None:
        """Handle single-key shortcuts when focus is not in a text field."""
        if isinstance(self.focused, Input):
            return

        char = event.character
        if char == "/":
            event.stop()
            self.action_focus_search()
        elif char == "o":
            res = self.get_selected_result()
            if res:
                open_file_in_system(res.file_path)
                self.notify(f"Opening '{res.file_name}'…")
        elif char == "f":
            res = self.get_selected_result()
            if res:
                reveal_in_file_manager(res.file_path)
                self.notify(f"Revealing '{res.file_name}'…")
        elif char == "c":
            res = self.get_selected_result()
            if res:
                if copy_to_clipboard(res.file_path):
                    self.notify("Path copied to the clipboard.")
                else:
                    self.notify("Could not copy the path.", severity="warning")

    # -------------------------------------------------------------------------
    # Global keyboard shortcuts
    # -------------------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#input_query", Input).focus()

    def action_open_folder_picker(self) -> None:
        self.open_folder_picker()

    def action_clear_results(self) -> None:
        self.clear_results()

    def action_stop_or_unfocus(self) -> None:
        if self.is_searching:
            self.stop_search()
        else:
            self.set_focus(None)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit_app(self) -> None:
        self.stop_search()
        self.exit()


# -----------------------------------------------------------------------------
# Command-line entry point
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="FindFile — fast terminal search for file names and file contents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  findfile.py                           # Open in the current folder
  findfile.py ~/Documents               # Open in a chosen folder
  findfile.py ~/Projects "config"       # Search file names for 'config'
  findfile.py . "def main" -c           # Search file contents for 'def main'
  findfile.py . "def\\s+test_" -c -r    # Search file contents with a regex
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
        "-r",
        "--regex",
        action="store_true",
        help="Treat the search term as a regular expression",
    )

    args = parser.parse_args()

    app = FindFileApp(
        initial_folder=args.folder,
        initial_query=args.query,
        initial_content=args.content,
        initial_regex=args.regex,
    )
    app.run()


if __name__ == "__main__":
    main()
