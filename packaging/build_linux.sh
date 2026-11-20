#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VERSION="$("$PYTHON_BIN" -c "import sys; sys.path.insert(0, 'src'); import cuaflow; print(cuaflow.__version__)")"

echo "==> Building cuaflow v${VERSION} standalone Linux bundle..."
if [[ "${SKIP_WEB_BUILD:-0}" != "1" ]]; then
  (cd "$ROOT_DIR/web" && npm ci && npm run build)
fi

"$PYTHON_BIN" -m pip install -e . pyinstaller
"$PYTHON_BIN" "$SCRIPT_DIR/generate_icon.py"
"$PYTHON_BIN" -m PyInstaller "$SCRIPT_DIR/cuaflow.spec" --noconfirm --clean
cp -f "$SCRIPT_DIR/cuaflow.ico" "$ROOT_DIR/dist/cuaflow/cuaflow.ico"

TAR_OUT="$ROOT_DIR/dist/cuaflow-portable-${VERSION}-linux-x64.tar.gz"
tar -C "$ROOT_DIR/dist" -czf "$TAR_OUT" cuaflow
echo "==> Done: $TAR_OUT"
