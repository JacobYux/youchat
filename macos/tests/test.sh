#!/bin/sh
# Compile and run the parser tests (macOS only, needs swiftc from Xcode Command Line Tools).
set -eu
cd "$(dirname "$0")/.."
[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }
mkdir -p build
swiftc -O \
  YouChatBar/Sources/Models.swift \
  YouChatBar/Sources/UsageParsers.swift \
  tests/main.swift \
  -o build/parser-tests
./build/parser-tests
