#!/bin/sh
# Build a single-file executable: dist/youchat (Python zipapp, stdlib only).
# Copy it to the server with:  scp dist/youchat myserver:~/.local/bin/youchat
set -eu
cd "$(dirname "$0")/.."
mkdir -p dist
rm -rf build/zipapp && mkdir -p build/zipapp
cp -R src/youchat build/zipapp/
find build/zipapp -name '__pycache__' -prune -exec rm -rf {} +
echo 'import youchat.__main__  # noqa: F401  (version guard + CLI)' > build/zipapp/__main__.py
python3 -m zipapp build/zipapp -c -p "/usr/bin/env python3" -o dist/youchat
chmod +x dist/youchat
echo "built dist/youchat ($(wc -c < dist/youchat | tr -d ' ') bytes)"
