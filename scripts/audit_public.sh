#!/usr/bin/env bash
# Scan the working tree and the full git history for things that must not be public:
# secrets, internal addresses, tooling trailers. Run before opening the repository.
set -euo pipefail
cd "$(dirname "$0")/.."
PATTERNS='sk-[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]{20,}|[0-9]{6,}:[A-Za-z0-9_-]{30,}|192\.168\.[0-9.]+|10\.10\.[0-9.]+|Co-authored-by|Generated with|claude|codex'
echo "== working tree"
grep -rInE "$PATTERNS" --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=dist . || echo "clean"
echo "== history (all blobs)"
git rev-list --all | while read -r c; do git grep -InE "$PATTERNS" "$c" -- . 2>/dev/null | sed "s/^/$c:/" ; done | grep -v "scripts/audit_public.sh" || echo "clean"
echo "== commit messages"
git log --all --format='%H %s%n%b' | grep -inE 'co-authored-by|claude|codex|generated' || echo "clean"
