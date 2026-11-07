#!/usr/bin/env python3
"""
Tests for FindFile's helpers, search engine, CLI parser, and Textual UI.

Runs with plain Python (``python3 test_findfile.py``) and with pytest.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path

from textual.widgets import Checkbox, DataTable, Input, Label

from findfile import (
    FilePreviewModal,
    FindFileApp,
    FolderPickerModal,
    HelpModal,
    SearchEngine,
    build_highlight_pattern,
    build_parser,
    format_mtime,
    format_print_line,
    format_size,
    highlight_match,
    is_binary_file,
    main,
    parse_extensions,
    read_line_window,
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _write_tree(root: str, files: dict[str, object]) -> None:
    for relative, content in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content), encoding="utf-8")


def _sample_tree() -> dict[str, object]:
    return {
        "config_prod.json": '{\n  "database_url": "postgres://localhost/db",\n'
        '  "api_key": "my_secret_token_123"\n}\n',
        "readme.txt": "Instruções de instalação:\n"
        "1. Execute o comando pip install\n"
        "2. Configure a api_key no arquivo config\n",
        "app_worker.py": "import os\n\ndef run_worker_42():\n    print('Worker running')\n",
        "subfolder/secret_key.env": "API_KEY=my_secret_token_123\nDEBUG=True\n",
        "subfolder/relatorio_ação.md": "Título: CONFIGURAÇÃO DE PRODUÇÃO\nStatus: Operação normal\n",
        "subfolder/data.bin": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR api_key",
    }


def _run_engine(root: str, query: str, **kwargs):
    collected = []
    engine = SearchEngine(
        root_dir=root,
        query=query,
        on_batch=collected.extend,
        **kwargs,
    )
    scanned, found, elapsed = engine.run()
    return engine, collected, scanned, found, elapsed


def _fake_header(column_key: str):
    class Event:
        pass

    class Key:
        value = column_key

    event = Event()
    event.column_key = Key()
    return event


def _cell_plain(cell) -> str:
    return cell.plain if hasattr(cell, "plain") else str(cell)


def _widget_text(widget) -> str:
    """Read a Static/Label across Textual versions (content vs renderable)."""
    content = getattr(widget, "content", None)
    if content is None:
        content = getattr(widget, "renderable", "")
    plain = getattr(content, "plain", None)
    return plain if plain is not None else str(content)


async def _wait_for_idle(app: FindFileApp, pilot, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while app.is_searching and time.monotonic() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.01)
    assert not app.is_searching, "the search did not finish in time"
    await pilot.pause()


async def _run_ui_search(
    app: FindFileApp,
    pilot,
    query: str,
    content: bool = False,
) -> None:
    app.query_one("#input_query", Input).value = query
    app.query_one("#chk_content", Checkbox).value = content
    app.start_search()
    await _wait_for_idle(app, pilot)


# -----------------------------------------------------------------------------
# Helpers and small utilities
# -----------------------------------------------------------------------------

def test_format_size() -> None:
    assert format_size(500) == "500 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(1024 * 1024 * 3) == "3.0 MB"
    assert format_size(int(1.5 * 1024**3)) == "1.50 GB"


def test_format_mtime() -> None:
    formatted = format_mtime(1_700_000_000)
    assert len(formatted) == 16 and formatted[4] == "-"
    assert format_mtime(float("nan")) == "-"


def test_parse_extensions() -> None:
    assert parse_extensions("") is None
    assert parse_extensions("   ") is None
    assert parse_extensions("py, .JS, *.ts; txt") == {"py", "js", "ts", "txt"}
    assert parse_extensions(", ,") is None


def test_highlight_match() -> None:
    literal = highlight_match("minha_funcao_teste.py", "funcao")
    assert len(literal.spans) == 1
    assert literal.plain == "minha_funcao_teste.py"

    with_regex = highlight_match("worker_123.py", r"worker_\d+", use_regex=True)
    assert len(with_regex.spans) == 1

    accented = highlight_match("Configuração", "configuração")
    assert len(accented.spans) == 1

    empty = highlight_match("text", "")
    assert empty.plain == "text" and not empty.spans

    zero_length = highlight_match("abc", r"x*", use_regex=True)
    assert zero_length.plain == "abc"


def test_build_highlight_pattern() -> None:
    assert build_highlight_pattern("") is None
    assert build_highlight_pattern("(", use_regex=True) is None
    assert build_highlight_pattern("(", use_regex=False) is not None
    assert build_highlight_pattern("worker", use_regex=False) is not None


def test_glob_highlight_pattern() -> None:
    pattern = build_highlight_pattern("*.py", glob=True)
    assert pattern is not None
    text = highlight_match("worker.py", "*.py", pattern=pattern)
    assert [span for span in text.spans] and text.spans[0].start == 0
    assert text.spans[0].end == len("worker.py")

    # Without glob=True the wildcard is literal and highlights nothing here.
    literal = highlight_match("worker.py", "*.py")
    assert not literal.spans


def test_read_line_window() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "big.txt"
        path.write_text("".join(f"line {n}\n" for n in range(1, 1001)), encoding="utf-8")

        code, start = read_line_window(str(path), 500, 502)
        assert start == 500
        assert code == "line 500\nline 501\nline 502\n"

        code, start = read_line_window(str(path), -10, 2)
        assert start == 1
        assert code == "line 1\nline 2\n"

        long_path = Path(tmp) / "long.txt"
        long_path.write_text("x" * 50_000 + "\n", encoding="utf-8")
        code, _ = read_line_window(str(long_path), 1, 1)
        assert len(code) < 2100


def test_is_binary_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        text_file = Path(tmp) / "notes.txt"
        text_file.write_text("plain text", encoding="utf-8")
        binary_file = Path(tmp) / "raw.dat"
        binary_file.write_bytes(b"abc\x00def")
        image_file = Path(tmp) / "photo.png"
        image_file.write_bytes(b"not really a png")

        assert is_binary_file(str(text_file)) is False
        assert is_binary_file(str(binary_file)) is True
        assert is_binary_file(str(image_file)) is True


# -----------------------------------------------------------------------------
# Search engine
# -----------------------------------------------------------------------------

def test_name_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        engine, results, scanned, found, _ = _run_engine(
            tmp, "config", search_name=True, search_content=False
        )
        assert found == 1 == len(results)
        assert results[0].file_name == "config_prod.json"
        assert results[0].rel_path == "config_prod.json"
        assert results[0].file_size > 0
        assert scanned >= 6


def test_name_wildcards() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(
            tmp,
            {
                "a1.txt": "x",
                "a2.txt": "x",
                "b1.txt": "x",
                "app_worker.py": "x",
            },
        )

        _, results, _, found, _ = _run_engine(
            tmp, "a[12].txt", search_name=True, search_content=False
        )
        assert found == 2
        assert {result.file_name for result in results} == {"a1.txt", "a2.txt"}

        _, results, _, found, _ = _run_engine(
            tmp, "app_?orker.py", search_name=True, search_content=False
        )
        assert found == 1
        assert results[0].file_name == "app_worker.py"


def test_content_search_skips_binaries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp, "api_key", search_name=False, search_content=True
        )
        assert found == 3 == len(results)
        names = {result.file_name for result in results}
        assert "data.bin" not in names
        assert {result.match_type for result in results} == {"content"}


def test_case_sensitive_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp,
            "API_KEY",
            search_name=False,
            search_content=True,
            case_sensitive=True,
        )
        assert found == 1
        assert results[0].file_name == "secret_key.env"


def test_unicode_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp,
            "configuração",
            search_name=False,
            search_content=True,
            case_sensitive=False,
        )
        assert found == 1
        assert results[0].file_name == "relatorio_ação.md"


def test_case_sensitive_accented_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"acentos.txt": "Título: CONFIGURAÇÃO\nOutra linha\n"})
        _, _, _, found_upper, _ = _run_engine(
            tmp,
            "CONFIGURAÇÃO",
            search_name=False,
            search_content=True,
            case_sensitive=True,
        )
        _, _, _, found_lower, _ = _run_engine(
            tmp,
            "configuração",
            search_name=False,
            search_content=True,
            case_sensitive=True,
        )
        assert found_upper == 1
        assert found_lower == 0


def test_unreadable_file_is_counted() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "secret.txt"
        path.write_text("needle\n", encoding="utf-8")
        path.chmod(0)
        try:
            engine, _, _, found, _ = _run_engine(
                tmp, "needle", search_name=False, search_content=True
            )
        finally:
            path.chmod(0o600)
        if getattr(os, "geteuid", lambda: 1)() == 0:
            assert found == 1  # root can read anything
        else:
            assert found == 0
            assert engine.errors >= 1


def test_regex_search() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp,
            r"def\s+run_worker_\d+\(\):",
            search_name=False,
            search_content=True,
            use_regex=True,
        )
        assert found == 1
        assert results[0].file_name == "app_worker.py"
        assert results[0].line_number == 3


def test_extension_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp,
            "api_key",
            search_name=False,
            search_content=True,
            ext_filter={"json"},
        )
        assert found == 1
        assert results[0].file_name == "config_prod.json"


def test_name_and_content_together() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        _, results, _, found, _ = _run_engine(
            tmp, "config", search_name=True, search_content=True
        )
        assert found == 3
        assert {result.match_type for result in results} == {"name", "content"}


def test_ignored_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(
            tmp,
            {
                "visible.txt": "needle\n",
                "node_modules/pkg/index.js": "needle\n",
                ".hidden/secret.txt": "needle\n",
            },
        )

        _, _, _, found, _ = _run_engine(
            tmp, "needle", search_name=False, search_content=True
        )
        assert found == 1

        _, results, _, found, _ = _run_engine(
            tmp,
            "needle",
            search_name=False,
            search_content=True,
            ignore_caches=False,
        )
        assert found == 3
        assert {result.file_name for result in results} == {
            "visible.txt",
            "index.js",
            "secret.txt",
        }


def test_symlinked_files_are_searched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target.py"
        target.write_text("needle\n", encoding="utf-8")
        link = Path(tmp) / "link.py"
        try:
            link.symlink_to(target)
        except OSError:
            return  # Platform without symlink support.

        _, results, _, found, _ = _run_engine(
            tmp, "needle", search_name=False, search_content=True
        )
        assert found == 2
        assert {result.file_name for result in results} == {"target.py", "link.py"}


def test_max_results_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())
        engine, results, _, found, _ = _run_engine(
            tmp,
            "api_key",
            search_name=False,
            search_content=True,
            max_results=2,
        )
        assert found == 2 == len(results)
        assert engine.limit_reached is True


def test_max_matches_per_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"many.txt": "needle\n" * 50})
        _, results, _, found, _ = _run_engine(
            tmp, "needle", search_name=False, search_content=True
        )
        assert found == 40 == len(results)


def test_snippet_is_truncated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        long_line = ("a" * 300) + " needle " + ("b" * 300)
        _write_tree(tmp, {"long.txt": long_line + "\n"})
        _, results, _, found, _ = _run_engine(
            tmp, "needle", search_name=False, search_content=True
        )
        assert found == 1
        assert len(results[0].snippet) == 200
        assert results[0].snippet.endswith("...")


def test_stop_before_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"a.txt": "test\n"})
        collected = []
        engine = SearchEngine(
            root_dir=tmp,
            query="test",
            search_name=True,
            search_content=True,
            on_batch=collected.extend,
        )
        engine.stop()
        _, found, _ = engine.run()
        assert engine.is_stopped()
        assert found == 0 and not collected


def test_stop_mid_run_delivers_every_registered_result() -> None:
    """Regression test: cancelling must not lose results already counted."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(
            tmp,
            {f"file_{index}.txt": "needle\n" * 3 for index in range(1500)},
        )
        collected = []
        engine = SearchEngine(
            root_dir=tmp,
            query="needle",
            search_name=False,
            search_content=True,
            max_workers=8,
            on_batch=collected.extend,
        )

        threading.Thread(
            target=lambda: (time.sleep(0.03), engine.stop()),
            daemon=True,
        ).start()
        engine.run()

        # Sleep for late deliveries, then compare the engine counter with the
        # rows that actually reached the caller.
        time.sleep(0.1)
        assert engine.matches_found > 0
        assert len(collected) == engine.matches_found


def test_invalid_regex_raises() -> None:
    try:
        SearchEngine(root_dir=".", query="(", use_regex=True)
    except ValueError as exc:
        assert "Invalid regular expression" in str(exc)
    else:
        raise AssertionError("SearchEngine should reject an invalid regex")


def test_missing_root_and_empty_query() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _, found, _ = SearchEngine(
            root_dir=os.path.join(tmp, "missing"),
            query="x",
        ).run()
        assert found == 0

        _, found, _ = SearchEngine(root_dir=tmp, query="").run()
        assert found == 0


# -----------------------------------------------------------------------------
# CLI parser
# -----------------------------------------------------------------------------

def test_cli_parser_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [".", "needle", "-c", "-r", "-s", "-e", "py,js", "-w", "4", "--max-results", "10"]
    )
    assert args.folder == "."
    assert args.query == "needle"
    assert args.content and args.regex and args.case_sensitive
    assert args.ext == "py,js"
    assert args.workers == 4
    assert args.max_results == 10
    assert args.no_ignore is False


def test_cli_print_mode() -> None:
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, _sample_tree())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main([tmp, "api_key", "--no-name", "-p"])
        lines = out.getvalue().splitlines()
        assert status == 0
        assert lines == sorted(lines)
        assert any(line.endswith("config_prod.json:3:\"api_key\": \"my_secret_token_123\"")
                   for line in lines)
        # --no-name means content only: no bare-path name rows.
        assert all(line.count(":") >= 2 for line in lines)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main([tmp, "*.py", "-p"])
        assert status == 0
        assert out.getvalue().splitlines() == [os.path.join(tmp, "app_worker.py")]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main([tmp, "no_such_term_anywhere", "-p", "-c"])
        assert status == 1
        assert out.getvalue() == ""


def test_format_print_line() -> None:
    from findfile import SearchResult

    name_hit = SearchResult(1, "name", "/r/a/b.txt", "b.txt", "a/b.txt")
    content_hit = SearchResult(2, "content", "/r/a/b.txt", "b.txt", "a/b.txt", 7, "hi")
    assert format_print_line(name_hit, ".") == "a/b.txt"
    assert format_print_line(content_hit, "/r") == "/r/a/b.txt:7:hi"


def test_cli_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.folder == "."
    assert args.query == ""
    assert not args.content and not args.regex
    assert not args.no_name and not args.print_only


# -----------------------------------------------------------------------------
# Textual UI (headless)
# -----------------------------------------------------------------------------

def test_app_search_and_counters() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(
                tmp,
                {
                    "alpha.py": "header\nhello world\n",
                    "beta.txt": "hello world\n",
                },
            )
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "hello", content=True)

                table = app.query_one("#results_table", DataTable)
                assert table.row_count == 2
                assert "Done" in _widget_text(app.query_one("#lbl_status", Label))
                assert "2 matches" in _widget_text(app.query_one("#pill_matches", Label))
                assert app.query_one("#empty_hint").display is False

    asyncio.run(scenario())


def test_app_no_match_message() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"alpha.txt": "nothing here\n"})
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "absent-term", content=True)

                table = app.query_one("#results_table", DataTable)
                assert table.row_count == 0
                hint = _widget_text(app.query_one("#empty_hint"))
                assert "No matches" in hint
                assert app.query_one("#empty_hint").display is True

    asyncio.run(scenario())


def test_app_stopped_message() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()

                # Deterministic: finish a search that was stopped before start.
                app._last_query = "term"
                app._search_id += 1
                engine = SearchEngine(root_dir=tmp, query="term")
                engine.stop()
                app._finish_search_ui(app._search_id, engine, 0, 0, 0.01, None)
                await pilot.pause()

                assert "Stopped" in _widget_text(app.query_one("#lbl_status", Label))
                assert "stopped before any match" in _widget_text(
                    app.query_one("#empty_hint")
                ).lower()

    asyncio.run(scenario())


def test_app_invalid_regex_is_rejected() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = FindFileApp(initial_folder=tmp)
            messages: list[str] = []
            app.notify = lambda message, **kwargs: messages.append(str(message))  # type: ignore[method-assign]

            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                app.query_one("#input_query", Input).value = "("
                app.query_one("#chk_regex", Checkbox).value = True
                app.start_search()
                await pilot.pause()

                assert app.is_searching is False
                assert any("Invalid regular expression" in message for message in messages)
                assert app.query_one("#results_table", DataTable).row_count == 0

    asyncio.run(scenario())


def test_app_clear_results() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"alpha.py": "hello\n"})
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "hello")

                app.clear_results()
                await pilot.pause()

                table = app.query_one("#results_table", DataTable)
                assert table.row_count == 0
                assert app.results_map == {}
                assert "cleared" in _widget_text(app.query_one("#lbl_status", Label)).lower()
                assert app.query_one("#empty_hint").display is True

    asyncio.run(scenario())


def test_app_sort_toggles_direction() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(
                tmp,
                {
                    "zeta.txt": "hit\n",
                    "alpha.txt": "hit\n",
                    "middle.txt": "hit\n",
                },
            )
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "hit", content=True)

                table = app.query_one("#results_table", DataTable)
                app.on_header_selected(_fake_header("name"))
                await pilot.pause()
                assert _cell_plain(table.get_row_at(0)[2]) == "alpha.txt"
                assert "▲" in _widget_text(app.query_one("#pill_sort", Label))
                assert app.query_one("#pill_sort").display is True

                app.on_header_selected(_fake_header("name"))
                await pilot.pause()
                assert _cell_plain(table.get_row_at(0)[2]) == "zeta.txt"
                assert "▼" in _widget_text(app.query_one("#pill_sort", Label))

    asyncio.run(scenario())


def test_app_preview_modal_opens_and_closes() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"alpha.py": "line one\nhello world\n"})
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "hello", content=True)

                result = next(iter(app.results_map.values()))
                app.push_screen(FilePreviewModal(result))
                await pilot.pause()
                assert isinstance(app.screen, FilePreviewModal)
                assert "alpha.py" in _widget_text(app.screen.query_one(".dialog_title"))

                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, FilePreviewModal)

    asyncio.run(scenario())


def test_app_folder_picker_opens_and_confirms() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "chosen"
            target.mkdir()
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                app.open_folder_picker()
                await pilot.pause()
                assert isinstance(app.screen, FolderPickerModal)

                field = app.screen.query_one("#modal_folder_input", Input)
                field.value = str(target)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()

                assert not isinstance(app.screen, FolderPickerModal)
                assert app.query_one("#input_folder", Input).value == str(target.resolve())

    asyncio.run(scenario())


def test_app_initial_query_autostarts() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"alpha.txt": "hello\n", "beta.txt": "bye\n"})
            app = FindFileApp(initial_folder=tmp, initial_query="alpha")
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _wait_for_idle(app, pilot)

                table = app.query_one("#results_table", DataTable)
                assert table.row_count == 1
                assert _cell_plain(table.get_row_at(0)[2]) == "alpha.txt"

    asyncio.run(scenario())


def test_app_renders_markup_like_file_names_literally() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"[red]evil.txt": "hello\n"})
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "evil")

                table = app.query_one("#results_table", DataTable)
                assert table.row_count == 1
                assert _cell_plain(table.get_row_at(0)[2]) == "[red]evil.txt"

    asyncio.run(scenario())


def test_app_slash_focuses_the_search_field() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(tmp, {"alpha.txt": "hello\n"})
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await _run_ui_search(app, pilot, "hello")

                table = app.query_one("#results_table", DataTable)
                table.focus()
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()

                assert app.focused is app.query_one("#input_query", Input)

    asyncio.run(scenario())


def test_app_help_modal_opens_and_closes() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = FindFileApp(initial_folder=tmp)
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                app.action_show_help()
                await pilot.pause()
                assert isinstance(app.screen, HelpModal)

                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, HelpModal)

    asyncio.run(scenario())


# -----------------------------------------------------------------------------
# Plain-Python runner (pytest also discovers the same functions)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 - the runner reports every failure
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")

    print(f"\n{len(tests) - failures}/{len(tests)} tests passed.")
    raise SystemExit(1 if failures else 0)
