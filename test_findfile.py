#!/usr/bin/env python3
"""
Tests for the multithreaded SearchEngine and helpers.
"""

import os
import tempfile
from findfile import SearchEngine, highlight_match, format_size, is_binary_file


def test_search_engine():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files. Accented contents below exercise Unicode matching.
        file1 = os.path.join(tmpdir, "config_prod.json")
        file2 = os.path.join(tmpdir, "readme.txt")
        file3 = os.path.join(tmpdir, "app_worker.py")
        sub = os.path.join(tmpdir, "subfolder")
        os.makedirs(sub, exist_ok=True)
        file4 = os.path.join(sub, "secret_key.env")
        file5 = os.path.join(sub, "relatorio_ação.md")
        bin_file = os.path.join(sub, "data.bin")

        with open(file1, "w", encoding="utf-8") as f:
            f.write('{\n  "database_url": "postgres://localhost/db",\n  "api_key": "my_secret_token_123"\n}\n')

        with open(file2, "w", encoding="utf-8") as f:
            f.write("Instruções de instalação:\n1. Execute o comando pip install\n2. Configure a api_key no arquivo config\n")

        with open(file3, "w", encoding="utf-8") as f:
            f.write("import os\n\ndef run_worker_42():\n    print('Worker running')\n")

        with open(file4, "w", encoding="utf-8") as f:
            f.write("API_KEY=my_secret_token_123\nDEBUG=True\n")

        with open(file5, "w", encoding="utf-8") as f:
            f.write("Título: CONFIGURAÇÃO DE PRODUÇÃO\nStatus: Operação normal\n")

        with open(bin_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR api_key")

        print("Test files created.")

        # Test 1: name only
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="config",
            search_name=True,
            search_content=False,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Expected 1 name match, got {found}"
        assert results[0].file_name == "config_prod.json"
        print(f"Test 1 (name search): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 2: multithreaded content search, skipping binaries
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="api_key",
            search_name=False,
            search_content=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        # api_key is in file1, file2, and file4 (bin_file must be skipped)
        assert found == 3, f"Expected 3 content matches, got {found}"
        print(f"Test 2 (multithreaded content search): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 3: case-sensitive search
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="API_KEY",
            search_name=False,
            search_content=True,
            case_sensitive=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Expected 1 case-sensitive match, got {found}"
        assert "secret_key.env" in results[0].file_path
        print(f"Test 3 (case-sensitive): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 4: extension filter
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="api_key",
            search_name=False,
            search_content=True,
            ext_filter={"json"},
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Expected 1 match with the .json filter, got {found}"
        assert results[0].file_name == "config_prod.json"
        print(f"Test 4 (extension filter): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 5: name and content together
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="config",
            search_name=True,
            search_content=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        # "config" is in file1's name, file2's content ("Configure"), and file5 ("CONFIGURAÇÃO")
        assert found == 3, f"Expected 3 matches (name and content), got {found}"
        print(f"Test 5 (name and content together): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 6: case-insensitive accented UTF-8
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="configuração",
            search_name=False,
            search_content=True,
            case_sensitive=False,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Expected 1 Unicode match for 'configuração', got {found}"
        assert results[0].file_name == "relatorio_ação.md"
        print(f"Test 6 (case-insensitive accented Unicode): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 7: regular expression
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query=r"def\s+run_worker_\d+\(\):",
            search_name=False,
            search_content=True,
            use_regex=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Expected 1 regex match, got {found}"
        assert results[0].file_name == "app_worker.py"
        print(f"Test 7 (regular expression): {found} match(es) in {elapsed*1000:.2f}ms")

        # Test 8: max_results cap
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="api_key",
            search_name=False,
            search_content=True,
            max_results=2,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 2 and engine.limit_reached, f"Expected 2 matches with limit_reached=True, got {found}"
        print(f"Test 8 (max_results cap): OK ({found} matches)")

        # Test 9: immediate cancellation
        engine = SearchEngine(
            root_dir=tmpdir,
            query="test",
            search_name=True,
            search_content=True,
        )
        engine.stop()
        scanned, found, elapsed = engine.run()
        assert engine.is_stopped()
        print("Test 9 (immediate stop): OK")


def test_helpers():
    assert format_size(500) == "500 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(1024 * 1024 * 3) == "3.0 MB"

    highlighted = highlight_match("minha_funcao_teste.py", "funcao")
    assert len(highlighted.spans) == 1

    highlighted_regex = highlight_match("worker_123.py", r"worker_\d+", use_regex=True)
    assert len(highlighted_regex.spans) == 1
    print("Test 10 (helpers, literal and regex highlight): OK")


if __name__ == "__main__":
    test_search_engine()
    test_helpers()
    print("\nAll tests passed.")
