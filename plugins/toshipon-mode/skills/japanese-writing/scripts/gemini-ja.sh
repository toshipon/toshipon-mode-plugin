#!/usr/bin/env bash
# Antigravity CLI (agy) 経由で Gemini にプロンプトを投げ、応答本文を標準出力に返す。
#
#   gemini-ja.sh [-m モデル] [-e low|medium|high] [-t 時間] "プロンプト"
#   echo "プロンプト" | gemini-ja.sh
#
# agy は Gemini CLI の消費者向け提供終了 (2026-06-18) に伴う公式の移行先。
# サインイン済みの Antigravity の枠をそのまま使うため、API キーは不要。
#
# モデル一覧は `agy --model invalid -p=x` のエラー出力が最も正確。
# `agy models` は一部しか返さないことがある。
set -euo pipefail

readonly AGY="${AGY_BIN:-$(command -v agy 2>/dev/null || printf %s "$HOME/.local/bin/agy")}"
readonly DEFAULT_MODEL="gemini-3.8-flash-high"
readonly DEFAULT_TIMEOUT="0"  # 0 は完了まで待つ

die() { printf '%s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

model="$DEFAULT_MODEL"
timeout="$DEFAULT_TIMEOUT"
effort=""

while getopts ':m:e:t:h' opt; do
  case "$opt" in
    m) model="$OPTARG" ;;
    e) effort="$OPTARG" ;;
    t) timeout="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

if [ "$#" -gt 0 ]; then
  prompt="$*"
else
  prompt="$(cat)"
fi
[ -n "${prompt//[[:space:]]/}" ] || die "プロンプトが空です"

[ -x "$AGY" ] || die "agy が見つかりません: $AGY"

# -p は直後のトークンをプロンプトとして食うため -p=... で密着させる。
# --disable-slash-commands は、渡した文書の / 始まりの行が展開されるのを防ぐ。
# これを付けると --mode plan は無効になる旨の警告が出るため併用しない。
args=(
  --model "$model"
  --output-format text
  --disable-slash-commands
  --print-timeout "$timeout"
)
[ -n "$effort" ] && args+=(--effort "$effort")

exec "$AGY" "${args[@]}" -p="$prompt"
