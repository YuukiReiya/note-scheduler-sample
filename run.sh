#!/bin/bash
# Copyright 2026 Yuuki Reiya
# Licensed under the PolyForm Internal Use License 1.0.0 (see LICENSE.md).

set -e

if [ -z "$BASH_VERSION" ]; then
  echo "Error: このスクリプトは 'bash run.sh' として実行してください。" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python 3 が見つかりません。" >&2
  echo "  macOS では通常 /usr/bin/python3 が Command Line Tools に同梱されています。" >&2
  echo "  未導入なら: xcode-select --install" >&2
  exit 1
fi

PROJECTS_JSON=$("$PYTHON" -c 'import json,sys; print(json.dumps({"demo": sys.argv[1]}))' "$REPO/projects/demo")

export CLAUDE_SCHEDULER_HOME="$REPO/.state"

exec "$PYTHON" "$REPO/scheduler/dispatch.py" \
  --config-dir "$REPO" \
  --projects "$PROJECTS_JSON" \
  "$@"
