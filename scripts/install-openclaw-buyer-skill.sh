#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/skills/openclaw-buyer"
EXTENSION_SOURCE_DIR="$ROOT_DIR/extensions/openclaw-buyer"
DEFAULT_OPENCLAW_EXTENSION_DIR="${HOME:-/root}/.openclaw/extensions/openclaw-buyer"
TARGET_BASE="${1:-${OPENCLAW_BUYER_EXTENSION_DIR:-$DEFAULT_OPENCLAW_EXTENSION_DIR}}"
TARGET_SKILL_DIR="$TARGET_BASE/skills/openclaw-buyer"
TARGET_AGENTS_DIR="$TARGET_BASE/agents"

if [[ -z "$TARGET_BASE" ]]; then
  echo "Usage: $0 <openclaw-buyer-extension-dir>" >&2
  echo "Or set OPENCLAW_BUYER_EXTENSION_DIR=/path/to/openclaw-buyer" >&2
  exit 2
fi

if [[ ! -f "$SOURCE_DIR/SKILL.md" ]]; then
  echo "Source skill not found: $SOURCE_DIR" >&2
  exit 1
fi

if [[ ! -f "$EXTENSION_SOURCE_DIR/openclaw.plugin.json" ]]; then
  echo "Source extension metadata not found: $EXTENSION_SOURCE_DIR" >&2
  exit 1
fi

mkdir -p "$TARGET_SKILL_DIR" "$TARGET_AGENTS_DIR"
cp "$EXTENSION_SOURCE_DIR/package.json" "$TARGET_BASE/package.json"
cp "$EXTENSION_SOURCE_DIR/openclaw.plugin.json" "$TARGET_BASE/openclaw.plugin.json"
cp "$EXTENSION_SOURCE_DIR/index.js" "$TARGET_BASE/index.js"
cp "$SOURCE_DIR/SKILL.md" "$TARGET_SKILL_DIR/SKILL.md"
cp -R "$SOURCE_DIR/agents"/. "$TARGET_AGENTS_DIR"/

echo "Installed openclaw-buyer extension to $TARGET_BASE"
echo "Skill: $TARGET_SKILL_DIR/SKILL.md"
