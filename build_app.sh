#!/usr/bin/env bash
# ==============================================================================
# ⚡ FindFile - Script de Empacotamento e Instalação para macOS
# Cria o FindFile.app em /Applications com ícone e atalho CLI no Terminal
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="FindFile"
DEFAULT_TARGET="/Applications/${APP_NAME}.app"
USER_TARGET="$HOME/Applications/${APP_NAME}.app"
TARGET_APP="$DEFAULT_TARGET"
INSTALL_CLI=true

usage() {
    cat <<EOF
Uso: $0 [opções]

Opções:
  --system           Instala em /Applications (padrão)
  --user             Instala em ~/Applications (pasta de usuário)
  --target PATH      Caminho personalizado de destino para o .app
  --no-cli           Não instala o atalho 'findfile' na linha de comando
  -h, --help         Exibe esta ajuda
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --system)
            TARGET_APP="$DEFAULT_TARGET"
            shift
            ;;
        --user)
            TARGET_APP="$USER_TARGET"
            shift
            ;;
        --target)
            [[ -n "${2:-}" ]] || { echo "Erro: --target requer um caminho"; exit 1; }
            TARGET_APP="$2"
            shift 2
            ;;
        --no-cli)
            INSTALL_CLI=false
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Opção desconhecida: $1"
            usage
            ;;
    esac
done

echo "🚀 Iniciando empacotamento do FindFile..."
echo "📦 Destino do Aplicativo: $TARGET_APP"

# 1. Garantir que o ícone exista
ICON_PATH="$SCRIPT_DIR/assets/AppIcon.icns"
if [[ ! -f "$ICON_PATH" ]]; then
    echo "🎨 Gerando ícone de alta resolução (AppIcon.icns)..."
    python3 "$SCRIPT_DIR/scripts/generate_icon.py" "$ICON_PATH"
fi

# 2. Criar estrutura de pastas do bundle .app
CONTENTS_DIR="$TARGET_APP/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"

echo "📁 Criando diretórios do bundle..."
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"

# 3. Gerar Info.plist
echo "📝 Gerando Info.plist..."
cat << 'EOF' > "$CONTENTS_DIR/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>FindFile</string>
    <key>CFBundleDisplayName</key>
    <string>FindFile</string>
    <key>CFBundleIdentifier</key>
    <string>com.cassio.findfile</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>CFBundleExecutable</key>
    <string>FindFile</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# PkgInfo
echo -n "APPL????" > "$CONTENTS_DIR/PkgInfo"

# 4. Copiar ícone e arquivos do projeto
echo "📄 Copiando recursos..."
cp "$ICON_PATH" "$RESOURCES_DIR/AppIcon.icns"
cp "$SCRIPT_DIR/findfile.py" "$RESOURCES_DIR/findfile.py"
cp "$SCRIPT_DIR/requirements.txt" "$RESOURCES_DIR/requirements.txt"

# 5. Criar o launcher de execução no Terminal (Contents/Resources/findfile.command)
cat << 'EOF' > "$RESOURCES_DIR/findfile.command"
#!/usr/bin/env bash
# ⚡ FindFile Terminal Runner

printf "\033]0;%s\007" "⚡ FindFile"

# Inicia na pasta do usuário se nenhum argumento foi passado
if [ $# -eq 0 ]; then
    cd "$HOME" || exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_PY="$APP_DIR/findfile.py"

PYTHON_CANDIDATES=(
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
    "$(command -v python3 2>/dev/null)"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
)

PY_EXEC=""
for p in "${PYTHON_CANDIDATES[@]}"; do
    if [ -n "$p" ] && [ -x "$p" ]; then
        if "$p" -c "import textual, rich" >/dev/null 2>&1; then
            PY_EXEC="$p"
            break
        fi
    fi
done

if [ -z "$PY_EXEC" ]; then
    echo "⚠️ Python com as dependências 'textual' e 'rich' não foi encontrado."
    echo "Instalando dependências..."
    if command -v pip3 >/dev/null 2>&1; then
        pip3 install -r "$APP_DIR/requirements.txt"
        PY_EXEC="$(command -v python3)"
    else
        echo "Por favor instale o Python 3 e execute: pip3 install textual rich"
        read -r -p "Pressione Enter para fechar..."
        exit 1
    fi
fi

"$PY_EXEC" "$TARGET_PY" "$@"
EXIT_CODE=$?

exit $EXIT_CODE
EOF
chmod +x "$RESOURCES_DIR/findfile.command"

# 6. Criar o executável principal do bundle macOS (Contents/MacOS/FindFile)
cat << 'EOF' > "$MACOS_DIR/FindFile"
#!/usr/bin/env bash
# ⚡ FindFile macOS App Entrypoint

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$DIR/Resources"

# Abre a janela do Terminal de forma nativa via LaunchServices
open -a Terminal "$RESOURCES/findfile.command"
EOF
chmod +x "$MACOS_DIR/FindFile"

# 7. Notificar o sistema macOS (touch e lsregister)
touch "$TARGET_APP"
if [[ -x "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister" ]]; then
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET_APP" 2>/dev/null || true
fi

echo "✅ Aplicativo instalado com sucesso em: $TARGET_APP"

# 8. Instalar atalho de linha de comando CLI (findfile)
if [[ "$INSTALL_CLI" == true ]]; then
    CLI_BIN_DIR="$HOME/.local/bin"
    mkdir -p "$CLI_BIN_DIR"
    CLI_PATH="$CLI_BIN_DIR/findfile"

    cat << EOF > "$CLI_PATH"
#!/usr/bin/env bash
# ⚡ FindFile CLI Wrapper

APP_PY="$RESOURCES_DIR/findfile.py"
if [[ ! -f "\$APP_PY" ]]; then
    APP_PY="$SCRIPT_DIR/findfile.py"
fi

PYTHON_CANDIDATES=(
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
    "\$(command -v python3 2>/dev/null)"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
)

PY_EXEC=""
for p in "\${PYTHON_CANDIDATES[@]}"; do
    if [[ -n "\$p" && -x "\$p" ]]; then
        if "\$p" -c "import textual, rich" >/dev/null 2>&1; then
            PY_EXEC="\$p"
            break
        fi
    fi
done

if [[ -z "\$PY_EXEC" ]]; then
    PY_EXEC="python3"
fi

exec "\$PY_EXEC" "\$APP_PY" "\$@"
EOF
    chmod +x "$CLI_PATH"
    echo "✅ Comando CLI instalado em: $CLI_PATH"
fi

echo ""
echo "🎉 Tudo pronto!"
echo "   - Para abrir via interface: Vá em Aplicativos > FindFile (ou abra pelo Spotlight / Launchpad)"
echo "   - Para abrir via terminal: Digite 'findfile' em qualquer pasta"
echo ""
