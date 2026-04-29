#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/skills/openclaw-buyer"
DEFAULT_OPENCLAW_SKILLS_DIR="/root/.openclaw/workspace/skills"
TARGET_BASE="${1:-${OPENCLAW_SKILLS_DIR:-$DEFAULT_OPENCLAW_SKILLS_DIR}}"

if [[ -z "$TARGET_BASE" ]]; then
  echo "Usage: $0 <openclaw-skills-dir>" >&2
  echo "Or set OPENCLAW_SKILLS_DIR=/path/to/openclaw/skills" >&2
  exit 2
fi

if [[ ! -f "$SOURCE_DIR/SKILL.md" ]]; then
  echo "Source skill not found: $SOURCE_DIR" >&2
  exit 1
fi

if [[ "$(basename "$TARGET_BASE")" == "openclaw-buyer" ]]; then
  TARGET_DIR="$TARGET_BASE"
else
  TARGET_DIR="$TARGET_BASE/openclaw-buyer"
fi

mkdir -p "$TARGET_DIR"
cp -R "$SOURCE_DIR"/. "$TARGET_DIR"/

echo "Installed openclaw-buyer skill to $TARGET_DIR"
