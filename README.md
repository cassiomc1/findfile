# ⚡ FindFile - Buscador Ultrarrápido de Arquivos e Conteúdo com TUI

Um utilitário de terminal interativo (TUI) moderno, responsivo e de alto desempenho para buscar arquivos pelo **nome** e/ou pelo **conteúdo** em modo **multithread**, desenvolvido com **Python 3** e **Textual/Rich**.

---

## 🚀 Principais Funcionalidades

- **⚡ Motor Multithread Ultrarrápido**:
  - Varredura de diretórios em alta velocidade via chamadas C (`os.scandir`).
  - Pool de threads com detecção de CPU para processamento paralelo simultâneo de arquivos.
  - Leitura unificada por arquivo (`fstat` + detecção binária + conteúdo em uma única abertura de descritor), operando em nível de *bytes* para termos ASCII e *Unicode* completo para termos acentuados ou expressões regulares.
  - Pula arquivos binários automaticamente (imagens, vídeos, PDFs, executáveis, sqlite, etc.) e caches pesados (`.git`, `node_modules`, `.venv`, etc.).
- **🖥️ Interface TUI Moderna e Fluida**:
  - Construída com Textual e paleta de cores escura moderna.
  - Destaque em tempo real do termo ou expressão regular dentro dos nomes de arquivos e trechos de código.
  - Ordenação interativa clicando nos cabeçalhos das colunas (`#`, `Tipo`, `Arquivo`, `Linha`, `Tamanho`, `Caminho Relativo`).
  - Atualização progressiva dos resultados em lotes com proteção contra sobrecarga de linhas.
  - Métricas ao vivo de arquivos verificados, resultados encontrados, tempo decorrido, threads ativas e velocidade (arquivos/s).
- **📁 Seletor Visual de Pastas**:
  - Modal interativo para navegar e escolher pastas com atalhos rápidos (`Home`, `Atual`, `Documentos`, `Downloads`, `Subir`).
  - Campo editável para digitar ou colar qualquer caminho de diretório.
- **🔍 Modos de Busca Flexíveis**:
  - **No Nome do Arquivo**: Encontra arquivos cujo nome contenha o termo (com suporte a wildcards `*` e `?` ou Regex).
  - **No Conteúdo (Grep)**: Varre o texto interno dos arquivos e exibe o número da linha e o trecho correspondente.
  - **Ambos Simultaneamente**: Encontra tanto ocorrências no nome quanto dentro do código.
  - **Expressão Regular (Regex)**: Suporte completo a padrões avançados de busca tanto no nome quanto no conteúdo.
  - **Case Sensitive**: Diferenciação opcional de maiúsculas e minúsculas (incluindo suporte a caracteres acentuados UTF-8).
  - **Filtro de Extensões**: Permite limitar a busca a extensões específicas (ex: `py, js, ts, txt`).
- **📄 Prévia e Ações Rápidas de Arquivo**:
  - Ao selecionar um resultado na tabela (`Enter`), abre um modal com metadados e **prévia do código com realce de sintaxe** e linha destacada.
  - Ações diretas:
    - `o`: Abrir o arquivo no aplicativo padrão do sistema.
    - `f`: Revelar o arquivo no Finder (macOS) ou gerenciador de arquivos.
    - `c`: Copiar o caminho completo para a área de transferência.
    - `y`: Copiar o trecho da linha encontrada (no modal de prévia).

---

## 📦 Instalação

### Pré-requisitos
- Python 3.9+ (compatível com Python 3.10, 3.11, 3.12, 3.13 e 3.14)
- macOS ou Linux

### 1. Clonar o repositório
```bash
git clone https://github.com/cassio/findfile.git
cd findfile
```

### 2. Instalar dependências
```bash
pip install -r requirements.txt
```
*(As dependências são apenas `textual` e `rich`)*

### 3. Empacotar como Aplicativo macOS (FindFile.app)
Você pode empacotar o FindFile como um aplicativo nativo (`FindFile.app`) diretamente na sua pasta de **Aplicativos** (`/Applications`), com ícone de alta resolução e comando global de terminal:

```bash
chmod +x build_app.sh
./build_app.sh
```

Isso irá:
- Instalar `/Applications/FindFile.app` (visível no Finder, Launchpad, Spotlight `Cmd + Espaço` e Dock).
- Criar o comando global `findfile` no seu terminal (`~/.local/bin/findfile`), permitindo rodar `findfile` diretamente de qualquer pasta.

---

## 🎮 Como Usar

### Execução Básica
Basta executar o script diretamente no terminal:

```bash
python3 findfile.py
```
ou torne-o executável e rode:
```bash
chmod +x findfile.py
./findfile.py
```

### Parâmetros de Linha de Comando (Opcionais)
Você pode abrir o FindFile já apontando para um diretório e até com um termo e flags pré-configurados:

```bash
# Abrir apontando para a pasta Documentos
python3 findfile.py ~/Documents

# Abrir na pasta de projetos já buscando "database" no nome
python3 findfile.py ~/Projetos "database"

# Buscar "def main" diretamente no conteúdo dos arquivos (-c / --content)
python3 findfile.py ~/Projetos "def main" -c

# Buscar usando Expressão Regular no conteúdo (-c -r)
python3 findfile.py ~/Projetos "def\s+test_\w+" -c -r
```

---

## ⌨️ Atalhos de Teclado

| Tecla | Ação |
| :--- | :--- |
| **`Enter`** (nos inputs) | Dispara a busca imediatamente |
| **`Esc`** | Cancela busca em andamento ou fecha modais abertos |
| **`Enter` / Clique Duplo** (na tabela) | Abre a prévia do arquivo com código colorido e linha destacada |
| **Clique no Cabeçalho** (na tabela) | Ordena os resultados pela coluna clicada (alterna crescente/decrescente) |
| **`Ctrl + F`** ou **`/`** | Foca no campo de busca de texto |
| **`Ctrl + P`** | Abre o modal de seleção visual de pastas |
| **`Ctrl + L`** | Limpa a lista de resultados e contadores |
| **`o`** (na tabela ou prévia) | Abre o arquivo no aplicativo padrão do sistema |
| **`f`** (na tabela ou prévia) | Revela o arquivo no Finder |
| **`c`** (na tabela ou prévia) | Copia o caminho absoluto do arquivo para o clipboard |
| **`y`** (na prévia) | Copia o trecho de código da linha encontrada |
| **`F1`** ou **`?`** | Exibe a tela de ajuda com todos os atalhos |
| **`q`** | Encerra o aplicativo |

---

## 🧪 Testes Automatizados

O projeto inclui uma suíte completa de testes unitários que valida a busca por nome, busca por conteúdo multithread, acentuação UTF-8, expressões regulares, case-sensitivity, filtros de extensão, limite de resultados e cancelamento por token:

```bash
python3 test_findfile.py
```
