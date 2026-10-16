#!/usr/bin/env bash
# ==============================================================================
# FindFile — package and install the macOS app.
# Creates FindFile.app and a terminal command.
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
Usage: $0 [options]

Options:
  --system           Install to /Applications (default)
  --user             Install to ~/Applications
  --target PATH      Custom destination for the .app bundle
  --no-cli           Do not install the 'findfile' terminal command
  -h, --help         Show this help
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
            [[ -n "${2:-}" ]] || { echo "Error: --target requires a path"; exit 1; }
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
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

echo "Packaging FindFile..."
echo "App destination: $TARGET_APP"

# 1. Make sure the icon exists
ICON_PATH="$SCRIPT_DIR/assets/AppIcon.icns"
if [[ ! -f "$ICON_PATH" ]]; then
    echo "Generating AppIcon.icns..."
    python3 "$SCRIPT_DIR/scripts/generate_icon.py" "$ICON_PATH"
fi

# 2. Create the .app bundle directories
CONTENTS_DIR="$TARGET_APP/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"

echo "Creating bundle directories..."
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"

# 3. Write Info.plist
echo "Writing Info.plist..."
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
    <string>1.1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.1.0</string>
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

# 4. Copy the icon and project files
echo "Copying resources..."
cp "$ICON_PATH" "$RESOURCES_DIR/AppIcon.icns"
cp "$SCRIPT_DIR/findfile.py" "$RESOURCES_DIR/findfile.py"
cp "$SCRIPT_DIR/requirements.txt" "$RESOURCES_DIR/requirements.txt"

# 5. Terminal launcher (Contents/Resources/findfile.command)
cat << 'EOF' > "$RESOURCES_DIR/findfile.command"
#!/usr/bin/env bash
# ⚡ FindFile Terminal Runner

printf "\033]0;%s\007" "⚡ FindFile"

# Start in the home folder when no arguments were passed.
if [ $# -eq 0 ]; then
    cd "$HOME" || exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_PY="$APP_DIR/findfile.py"

find_python() {
    local candidates=()
    # Prefer Python.framework (newest first), then Homebrew and the active environment.
    while IFS= read -r p; do
        candidates+=("$p")
    done < <(ls -1r /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null || true)

    candidates+=(
        "$(command -v python3 2>/dev/null || true)"
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "/usr/bin/python3"
    )

    for p in "${candidates[@]}"; do
        if [ -n "$p" ] && [ -x "$p" ]; then
            if "$p" -c "import textual, rich" >/dev/null 2>&1; then
                echo "$p"
                return 0
            fi
        fi
    done
    return 1
}

PY_EXEC="$(find_python || true)"

if [ -z "$PY_EXEC" ]; then
    echo "Python with 'textual' and 'rich' was not found."
    echo "Installing dependencies..."
    if command -v pip3 >/dev/null 2>&1; then
        pip3 install -r "$APP_DIR/requirements.txt"
        PY_EXEC="$(find_python || command -v python3)"
    else
        echo "Install Python 3, then run: pip3 install textual rich"
        read -r -p "Press Enter to close..."
        exit 1
    fi
fi

"$PY_EXEC" "$TARGET_PY" "$@"
EXIT_CODE=$?

exit $EXIT_CODE
EOF
chmod +x "$RESOURCES_DIR/findfile.command"

# 6. Bundle executable (Contents/MacOS/FindFile)
cat << 'EOF' > "$MACOS_DIR/FindFile"
#!/usr/bin/env bash
# ⚡ FindFile macOS App Entrypoint

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$DIR/Resources"

# Open a Terminal window through Launch Services.
open -a Terminal "$RESOURCES/findfile.command"
EOF
chmod +x "$MACOS_DIR/FindFile"

# 7. Register the app with Launch Services
touch "$TARGET_APP"
if [[ -x "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister" ]]; then
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET_APP" 2>/dev/null || true
fi

echo "App installed at: $TARGET_APP"

# 8. Install the findfile terminal command
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

PY_EXEC=""
while IFS= read -r p; do
    if [[ -n "\$p" && -x "\$p" ]] && "\$p" -c "import textual, rich" >/dev/null 2>&1; then
        PY_EXEC="\$p"
        break
    fi
done < <(
    ls -1r /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null || true
    command -v python3 2>/dev/null || true
    printf "%s\n" "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3"
)

if [[ -z "\$PY_EXEC" ]]; then
    PY_EXEC="python3"
fi

exec "\$PY_EXEC" "\$APP_PY" "\$@"
EOF
    chmod +x "$CLI_PATH"
    echo "Terminal command installed at: $CLI_PATH"
fi

echo ""
echo "Done."
echo "   - App: Applications > FindFile, or Spotlight / Launchpad"
echo "   - Terminal: run 'findfile' from any folder"
echo ""
