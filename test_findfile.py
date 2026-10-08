#!/usr/bin/env python3
"""
Testes automatizados para o motor multithread SearchEngine e utilitários.
"""

import os
import tempfile
import time
from findfile import SearchEngine, highlight_match, format_size, is_binary_file

def test_search_engine():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Cria arquivos de teste
        file1 = os.path.join(tmpdir, "config_prod.json")
        file2 = os.path.join(tmpdir, "readme.txt")
        file3 = os.path.join(tmpdir, "app_worker.py")
        sub = os.path.join(tmpdir, "subfolder")
        os.makedirs(sub, exist_ok=True)
        file4 = os.path.join(sub, "secret_key.env")

        with open(file1, "w", encoding="utf-8") as f:
            f.write('{\n  "database_url": "postgres://localhost/db",\n  "api_key": "my_secret_token_123"\n}\n')

        with open(file2, "w", encoding="utf-8") as f:
            f.write("Instruções de instalação:\n1. Execute o comando pip install\n2. Configure a api_key no arquivo config\n")

        with open(file3, "w", encoding="utf-8") as f:
            f.write("import os\n\ndef run():\n    print('Worker running')\n")

        with open(file4, "w", encoding="utf-8") as f:
            f.write("API_KEY=my_secret_token_123\nDEBUG=True\n")

        print("Arquivos de teste criados com sucesso.")

        # Teste 1: Busca apenas por nome
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="config",
            search_name=True,
            search_content=False,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        assert found == 1, f"Esperado 1 match por nome, obteve {found}"
        assert results[0].file_name == "config_prod.json"
        print(f"✅ Teste 1 (Busca por nome): {found} match(es) em {elapsed*1000:.2f}ms")

        # Teste 2: Busca por conteúdo (grep multithread)
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="api_key",
            search_name=False,
            search_content=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        # api_key está em file1, file2 e file4 (file4 tem API_KEY maiúsculo, mas case_sensitive=False)
        assert found == 3, f"Esperado 3 matches de conteúdo, obteve {found}"
        print(f"✅ Teste 2 (Busca por conteúdo multithread): {found} match(es) em {elapsed*1000:.2f}ms")

        # Teste 3: Busca case-sensitive
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
        assert found == 1, f"Esperado 1 match case-sensitive, obteve {found}"
        assert "secret_key.env" in results[0].file_path
        print(f"✅ Teste 3 (Case-sensitive): {found} match(es) em {elapsed*1000:.2f}ms")

        # Teste 4: Busca com filtro de extensão
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
        assert found == 1, f"Esperado 1 match com filtro .json, obteve {found}"
        assert results[0].file_name == "config_prod.json"
        print(f"✅ Teste 4 (Filtro de extensão): {found} match(es) em {elapsed*1000:.2f}ms")

        # Teste 5: Busca por nome E conteúdo simultaneamente
        results = []
        engine = SearchEngine(
            root_dir=tmpdir,
            query="config",
            search_name=True,
            search_content=True,
            on_batch=lambda batch: results.extend(batch),
        )
        scanned, found, elapsed = engine.run()
        # "config" está no nome de file1 ("config_prod.json") e no conteúdo de file2 ("configure a api_key...")
        assert found == 2, f"Esperado 2 matches (nome e conteúdo), obteve {found}"
        print(f"✅ Teste 5 (Nome + Conteúdo simultâneo): {found} match(es) em {elapsed*1000:.2f}ms")

        # Teste 6: Cancelamento imediato
        engine = SearchEngine(
            root_dir=tmpdir,
            query="test",
            search_name=True,
            search_content=True,
        )
        engine.stop()
        scanned, found, elapsed = engine.run()
        assert engine.is_stopped()
        print(f"✅ Teste 6 (Interrupção imediata stop_event): OK")

def test_helpers():
    assert format_size(500) == "500 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(1024 * 1024 * 3) == "3.0 MB"

    highlighted = highlight_match("minha_funcao_teste.py", "funcao")
    assert len(highlighted.spans) == 1
    print("✅ Teste 7 (Utilitários e formatação): OK")

if __name__ == "__main__":
    test_search_engine()
    test_helpers()
    print("\n🎉 Todos os testes passaram com sucesso!")
