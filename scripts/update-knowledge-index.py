#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from collections import defaultdict
from pathlib import Path


DATA_DIR = Path(os.getenv("DATA_DIR", "/Volumes/File/qqbot-data")).expanduser()
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", str(DATA_DIR / "knowledge"))).expanduser()
RAG_CONTEXT_DIR = Path(os.getenv("RAG_CONTEXT_DIR", str(DATA_DIR / "rag_context"))).expanduser()
OUTPUT = RAG_CONTEXT_DIR / "shared.md"

SUPPORTED_SUFFIXES = {".xls", ".xlsx", ".csv", ".tsv", ".pdf", ".md", ".txt"}


def iter_knowledge_files() -> list[Path]:
    if not KNOWLEDGE_DIR.exists():
        return []
    return sorted(
        p
        for p in KNOWLEDGE_DIR.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def file_label(path: Path) -> str:
    rel = path.relative_to(KNOWLEDGE_DIR)
    size_kb = max(1, round(path.stat().st_size / 1024))
    return f"- `{rel}` ({path.suffix.lower().lstrip('.')}, {size_kb} KB)"


def main() -> int:
    files = iter_knowledge_files()
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        rel = path.relative_to(KNOWLEDGE_DIR)
        group = rel.parts[0] if len(rel.parts) > 1 else "未分类"
        grouped[group].append(path)

    lines = [
        "# 共享高考志愿资料上下文",
        "",
        "本地录取资料库已接入。Bridge 会把本文件注入给 OpenClaw `qqbot` agent。",
        "",
        "## 使用规则",
        "",
        f"- 资料根目录：`{KNOWLEDGE_DIR}`",
        "- 这些资料主要是江苏历年本科批次投档线、特殊类型投档线、逐分段统计表和招生折页。",
        "- 数据使用原则：位次优先于分数；用户只给分数时，应先换算位次再比较。若是当年分数且本地缺少当年一分一段，应联网核对，无法联网时要求用户补充位次。",
        "- 江苏按院校专业组投档。结构化投档线通常不直接包含具体专业名；按专业反查学校时，应先在招生折页/招生计划/专业介绍中定位专业所属院校专业组，再用位次比对投档线。",
        "- 做冲稳保时，按用户位次与历年投档位次差距分层，并说明简短推断过程；可标注走高、走低、大小年、平稳等趋势，但必须有数据依据。",
        "- 回答涉及投档线、位次、院校专业组、招生计划时，优先使用最新年份资料；历史年份只能作为趋势和参考。",
        "- 不要把历史投档线等同于当年录取承诺，不要承诺“必录取”。",
        "- 如果用户只给分数没有位次，先要求补充位次；需要精确判断时提醒以江苏省教育考试院和高校招生章程为准。",
        "- 如需核对具体院校/专业组数据，请读取下方对应文件路径；不要凭记忆编造。",
        "",
        "## 文件索引",
        "",
        f"- 文件总数：{len(files)}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    for group in sorted(grouped):
        lines.append(f"### {group}")
        lines.extend(file_label(path) for path in grouped[group])
        lines.append("")

    RAG_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
