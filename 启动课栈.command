#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUTF8=1

on_error() {
  echo
  echo "启动失败，请查看上方提示。首次启动需要联网下载 Python 和依赖。"
  echo "按回车关闭窗口。"
  read -r _
}
trap on_error ERR

if ! command -v uv >/dev/null 2>&1; then
  echo "正在从 Astral 官方来源安装 uv…"
  installer="$(mktemp -t chaoxin-uv)"
  curl --fail --location --proto '=https' --tlsv1.2 https://astral.sh/uv/install.sh -o "$installer"
  sh "$installer"
  rm -f "$installer"
fi

echo "正在启动课栈 macOS 本地版。首次会自动准备 Python 3.13 和依赖。"
uv run --frozen python run.py "$@"
