#!/bin/zsh
set -euo pipefail

DATA_DIR="${DATA_DIR:-/Volumes/File/qqbot-data}"

mkdir -p \
  "$DATA_DIR"/knowledge/province_rules \
  "$DATA_DIR"/knowledge/major_intro \
  "$DATA_DIR"/knowledge/admission_policy \
  "$DATA_DIR"/rag_context/groups \
  "$DATA_DIR"/rag_context/users \
  "$DATA_DIR"/rag_index \
  "$DATA_DIR"/openclaw/workspaces/qqbot/knowledge \
  "$DATA_DIR"/exports \
  "$DATA_DIR"/logs

if [[ ! -f "$DATA_DIR/rag_context/shared.md" ]]; then
  cat > "$DATA_DIR/rag_context/shared.md" <<'EOF'
# 共享高考志愿资料上下文

这里可以放经过本地 RAG 整理后的短上下文摘要。Bridge 会在调用 OpenClaw 前读取本文件，并注入 qqbot agent prompt。
EOF
fi

echo "Initialized $DATA_DIR"
