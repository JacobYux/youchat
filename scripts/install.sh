#!/bin/sh
# One-line installer for youchat:
#   curl -fsSL https://raw.githubusercontent.com/JacobYux/youchat/main/scripts/install.sh | sh
set -eu

REPO_URL="${YOUCHAT_REPO_URL:-https://github.com/JacobYux/youchat.git}"
BIN_DIR="${YOUCHAT_BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/youchat"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 (>= 3.8) is required but was not found in PATH." >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    echo "error: youchat requires Python >= 3.8 (found $(python3 -V 2>&1))." >&2
    exit 1
fi

mkdir -p "$BIN_DIR"

if [ -f "scripts/build.sh" ] && [ -d "src/youchat" ]; then
    echo "==> Building youchat from current directory..."
    ./scripts/build.sh
    cp dist/youchat "$TARGET"
else
    if ! command -v git >/dev/null 2>&1; then
        echo "error: git is required to fetch $REPO_URL." >&2
        exit 1
    fi
    TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t youchat-install)"
    trap 'rm -rf "$TMP_DIR"' EXIT INT TERM
    echo "==> Cloning youchat from $REPO_URL..."
    git clone --depth 1 -q "$REPO_URL" "$TMP_DIR/youchat"
    echo "==> Building single-file executable..."
    (cd "$TMP_DIR/youchat" && ./scripts/build.sh)
    cp "$TMP_DIR/youchat/dist/youchat" "$TARGET"
fi

chmod +x "$TARGET"
echo "==> Installed youchat to $TARGET ($("$TARGET" --version 2>/dev/null || echo 'ready'))"

case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo ""
        echo "NOTE: $BIN_DIR is not in your PATH yet. Add this to ~/.zshrc or ~/.bashrc:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

cat <<'EOF'

Next steps:
  1. Start local hub + runner service (macOS launchd):
       youchat service install --hub
  2. Register MCP server with Claude Code / Codex / Antigravity:
       youchat setup
       youchat hook install-agy
EOF
