#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import math
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import websockets


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DATA_DIR = Path("/Volumes/File/qqbot-data")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(ENV_FILE)


@dataclass(frozen=True)
class Settings:
    napcat_ws_url: str
    napcat_access_token: str
    openclaw_agent: str
    openclaw_channel: str
    openclaw_cli: str
    bot_qq: str
    group_require_mention: bool
    allowed_groups: Set[str]
    blocked_users: Set[str]
    admins: Set[str]
    data_dir: Path
    db_path: Path
    rag_context_dir: Path
    rag_context_max_chars: int
    knowledge_topn: int
    ollama_url: str
    embedding_model: str
    max_input_chars: int
    max_reply_chars_per_msg: int
    openclaw_timeout: int
    user_cooldown_seconds: int
    group_cooldown_seconds: int
    log_level: str


def csv_set(name: str) -> Set[str]:
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip()}


def env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    db_path = Path(os.getenv("DB_PATH", str(data_dir / "qqbot.sqlite3"))).expanduser()
    return Settings(
        napcat_ws_url=os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001"),
        napcat_access_token=os.getenv("NAPCAT_ACCESS_TOKEN", ""),
        openclaw_agent=os.getenv("OPENCLAW_AGENT", "qqbot"),
        openclaw_channel=os.getenv("OPENCLAW_CHANNEL", os.getenv("OPENCLAW_TO", "qqbot")),
        openclaw_cli=os.getenv("OPENCLAW_CLI", "/opt/homebrew/bin/openclaw"),
        bot_qq=os.getenv("BOT_QQ", "").strip(),
        group_require_mention=env_bool("GROUP_REQUIRE_MENTION", "true"),
        allowed_groups=csv_set("ALLOWED_GROUPS"),
        blocked_users=csv_set("BLOCKED_USERS"),
        admins=csv_set("ADMINS"),
        data_dir=data_dir,
        db_path=db_path,
        rag_context_dir=Path(os.getenv("RAG_CONTEXT_DIR", str(data_dir / "rag_context"))).expanduser(),
        rag_context_max_chars=env_int("RAG_CONTEXT_MAX_CHARS", "6000"),
        knowledge_topn=env_int("KNOWLEDGE_TOPN", "6"),
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
        max_input_chars=env_int("MAX_INPUT_CHARS", "1500"),
        max_reply_chars_per_msg=env_int("MAX_REPLY_CHARS_PER_MSG", "900"),
        openclaw_timeout=env_int("OPENCLAW_TIMEOUT", "120"),
        user_cooldown_seconds=env_int("USER_COOLDOWN_SECONDS", "8"),
        group_cooldown_seconds=env_int("GROUP_COOLDOWN_SECONDS", "2"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


SETTINGS = load_settings()
logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level, logging.INFO),
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("qqbot-bridge")
SHUTDOWN = asyncio.Event()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=30000;
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                user_id TEXT NOT NULL,
                group_id TEXT,
                text TEXT NOT NULL,
                replied INTEGER NOT NULL DEFAULT 0,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                scope TEXT PRIMARY KEY,
                last_seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rag_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                title TEXT,
                content_hash TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding_ref TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(document_id) REFERENCES rag_documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_document_id ON rag_chunks(document_id);
            CREATE TABLE IF NOT EXISTS knowledge_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                rel_path TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                year INTEGER,
                subject TEXT,
                batch TEXT,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admission_line_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                year INTEGER,
                subject TEXT,
                batch TEXT,
                row_index INTEGER NOT NULL,
                school_name TEXT,
                major_group_code TEXT,
                major_group_name TEXT,
                min_score INTEGER,
                min_rank INTEGER,
                columns_json TEXT NOT NULL,
                row_text TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(file_id) REFERENCES knowledge_files(id)
            );
            CREATE TABLE IF NOT EXISTS score_segment_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                year INTEGER,
                subject TEXT,
                row_index INTEGER NOT NULL,
                score INTEGER,
                same_score_count INTEGER,
                cumulative_count INTEGER,
                rank INTEGER,
                columns_json TEXT NOT NULL,
                row_text TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(file_id) REFERENCES knowledge_files(id)
            );
            CREATE TABLE IF NOT EXISTS control_line_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                year INTEGER,
                subject TEXT,
                batch TEXT,
                score INTEGER,
                columns_json TEXT NOT NULL,
                row_text TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(file_id) REFERENCES knowledge_files(id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_text_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding_model TEXT,
                embedding_json TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                UNIQUE(file_id, chunk_index),
                FOREIGN KEY(file_id) REFERENCES knowledge_files(id)
            );
            """
        )
        self.conn.commit()

    def record_message(
        self,
        message_type: str,
        user_id: str,
        group_id: Optional[str],
        text: str,
        replied: bool,
        reason: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (created_at, message_type, user_id, group_id, text, replied, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(time.time()), message_type, user_id, group_id, text, int(replied), reason),
        )
        self.conn.commit()

    def is_rate_limited(self, scope: str, seconds: int) -> bool:
        now = time.time()
        row = self.conn.execute("SELECT last_seen_at FROM rate_limits WHERE scope = ?", (scope,)).fetchone()
        if row and now - float(row["last_seen_at"]) < seconds:
            return True
        self.conn.execute(
            """
            INSERT INTO rate_limits (scope, last_seen_at)
            VALUES (?, ?)
            ON CONFLICT(scope) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (scope, now),
        )
        self.conn.commit()
        return False


STORE = Store(SETTINGS.db_path)


def normalize_message(raw_msg: Any) -> str:
    if isinstance(raw_msg, str):
        text = re.sub(r"\[CQ:at,qq=\d+\]", "", raw_msg)
        return text.strip()
    if isinstance(raw_msg, list):
        parts: List[str] = []
        for seg in raw_msg:
            if not isinstance(seg, dict):
                continue
            typ = seg.get("type")
            data = seg.get("data") or {}
            if typ == "text":
                parts.append(str(data.get("text", "")))
        return "".join(parts).strip()
    return str(raw_msg).strip()


def is_at_bot(raw_msg: Any) -> bool:
    if not SETTINGS.bot_qq:
        return False
    if isinstance(raw_msg, str):
        return f"[CQ:at,qq={SETTINGS.bot_qq}]" in raw_msg
    if isinstance(raw_msg, list):
        return any(
            isinstance(seg, dict)
            and seg.get("type") == "at"
            and str((seg.get("data") or {}).get("qq")) == SETTINGS.bot_qq
            for seg in raw_msg
        )
    return False


def read_text_if_exists(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        LOG.warning("RAG context file is not UTF-8: %s", path)
        return ""


def load_rag_context(event: Dict[str, Any]) -> str:
    """Load optional precomputed local-RAG context for this conversation.

    A separate local RAG process can write markdown snippets into this directory.
    The bridge stays deliberately simple: it only injects bounded context into
    the OpenClaw prompt and never executes user-controlled commands.
    """
    user_id = str(event.get("user_id", ""))
    message_type = event.get("message_type", "")
    group_id = str(event.get("group_id", "")) if message_type == "group" else ""

    candidates = [SETTINGS.rag_context_dir / "shared.md"]
    if group_id:
        candidates.append(SETTINGS.rag_context_dir / "groups" / f"{group_id}.md")
    if user_id:
        candidates.append(SETTINGS.rag_context_dir / "users" / f"{user_id}.md")

    context_parts = [read_text_if_exists(path) for path in candidates]
    context = "\n\n".join(part for part in context_parts if part)
    if len(context) > SETTINGS.rag_context_max_chars:
        context = context[: SETTINGS.rag_context_max_chars] + "\n[本地资料上下文过长，已截断]"
    return context


def context_block(event: Dict[str, Any]) -> str:
    context = load_rag_context(event)
    if not context:
        return ""
    return f"""
本地资料/RAG 上下文：
{context}

使用要求：
- 可以参考本地资料，但不要把资料当作当年最新招生计划的唯一依据。
- 如果资料与考试院或高校招生章程可能冲突，提醒用户以官方最新信息为准。
"""


GAOKAO_KEYWORDS = {
    "高考",
    "志愿",
    "投档",
    "位次",
    "分数",
    "院校",
    "专业组",
    "冲稳保",
    "录取",
    "本科",
    "物理",
    "历史",
    "选科",
    "招生",
    "省控线",
    "一分一段",
}


def is_gaokao_query(text: str) -> bool:
    return any(keyword in text for keyword in GAOKAO_KEYWORDS)


def infer_query_subject(text: str) -> Optional[str]:
    if any(k in text for k in ("物理", "理科", "理工")):
        return "物理"
    if any(k in text for k in ("历史", "文科", "文史")):
        return "历史"
    return None


def extract_query_numbers(text: str) -> tuple[Optional[int], Optional[int]]:
    nums = []
    for match in re.findall(r"(?<!\d)(\d{2,7})(?!\d)", text.replace(",", "")):
        try:
            nums.append(int(match))
        except ValueError:
            pass
    score = next((n for n in nums if 100 <= n <= 750), None)
    rank = next((n for n in nums if n > 750), None)
    return score, rank


def query_tokens(text: str) -> List[str]:
    tokens = []
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", text):
        if token in GAOKAO_KEYWORDS:
            continue
        if re.fullmatch(r"\d+", token):
            continue
        tokens.append(token)
    return tokens[:6]


def fetch_admission_matches(text: str, limit: int) -> List[str]:
    score, rank = extract_query_numbers(text)
    subject = infer_query_subject(text)
    tokens = query_tokens(text)
    rows: list[str] = []
    seen: set[int] = set()

    def add_results(sql: str, params: tuple[Any, ...]) -> None:
        for row in STORE.conn.execute(sql, params).fetchall():
            row_id = int(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            source = row["rel_path"] or row["source_path"]
            rows.append(
                f"{row['year'] or ''} {row['subject'] or ''} {row['batch'] or ''}：{row['row_text']} 来源：{source}"
            )
            if len(rows) >= limit:
                break

    filters = []
    params: list[Any] = []
    if subject:
        filters.append("(a.subject = ? OR a.subject IS NULL)")
        params.append(subject)
    where = " AND ".join(filters) if filters else "1=1"

    if rank:
        add_results(
            f"""
            SELECT a.*, f.rel_path, f.source_path
            FROM admission_line_rows a JOIN knowledge_files f ON f.id = a.file_id
            WHERE {where} AND a.min_rank IS NOT NULL
            ORDER BY ABS(a.min_rank - ?) ASC, a.year DESC
            LIMIT ?
            """,
            tuple(params + [rank, limit]),
        )
    if score and len(rows) < limit:
        add_results(
            f"""
            SELECT a.*, f.rel_path, f.source_path
            FROM admission_line_rows a JOIN knowledge_files f ON f.id = a.file_id
            WHERE {where} AND a.min_score IS NOT NULL
            ORDER BY ABS(a.min_score - ?) ASC, a.year DESC
            LIMIT ?
            """,
            tuple(params + [score, limit]),
        )
    for token in tokens:
        if len(rows) >= limit:
            break
        add_results(
            f"""
            SELECT a.*, f.rel_path, f.source_path
            FROM admission_line_rows a JOIN knowledge_files f ON f.id = a.file_id
            WHERE {where} AND a.row_text LIKE ?
            ORDER BY a.year DESC
            LIMIT ?
            """,
            tuple(params + [f"%{token}%", limit]),
        )
    return rows[:limit]


def fetch_control_matches(text: str, limit: int) -> List[str]:
    score, _rank = extract_query_numbers(text)
    subject = infer_query_subject(text)
    filters = []
    params: list[Any] = []
    if subject:
        filters.append("(c.subject = ? OR c.subject IS NULL)")
        params.append(subject)
    where = " AND ".join(filters) if filters else "1=1"
    order = "c.year DESC"
    if score:
        order = "ABS(c.score - ?) ASC, c.year DESC"
        params.append(score)
    params.append(limit)
    rows = STORE.conn.execute(
        f"""
        SELECT c.*, f.rel_path, f.source_path
        FROM control_line_rows c JOIN knowledge_files f ON f.id = c.file_id
        WHERE {where}
        ORDER BY {order}
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [
        f"{row['year'] or ''} {row['subject'] or ''} {row['batch'] or ''}省控线：{row['score']} 来源：{row['rel_path'] or row['source_path']}"
        for row in rows
    ]


def ollama_embedding(text: str) -> Optional[List[float]]:
    payload = json.dumps({"model": SETTINGS.embedding_model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{SETTINGS.ollama_url.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [float(x) for x in data["embedding"]]
    except Exception as exc:
        LOG.warning("Embedding query failed: %r", exc)
        return None


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


def fetch_vector_matches(text: str, limit: int) -> List[str]:
    query_vector = ollama_embedding(text)
    if not query_vector:
        return []
    candidates = STORE.conn.execute(
        """
        SELECT c.content, c.embedding_json, f.rel_path, f.source_path
        FROM knowledge_text_chunks c JOIN knowledge_files f ON f.id = c.file_id
        WHERE c.embedding_model = ? AND c.embedding_json IS NOT NULL
        """,
        (SETTINGS.embedding_model,),
    ).fetchall()
    scored = []
    for row in candidates:
        try:
            vector = [float(x) for x in json.loads(row["embedding_json"])]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        score = cosine_similarity(query_vector, vector)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, row in scored[:limit]:
        content = str(row["content"]).strip()
        if len(content) > 450:
            content = content[:450] + "..."
        results.append(f"相似度{score:.3f}：{content} 来源：{row['rel_path'] or row['source_path']}")
    return results


def knowledge_search_block(text: str) -> str:
    if not is_gaokao_query(text):
        return ""
    topn = SETTINGS.knowledge_topn
    admission = fetch_admission_matches(text, topn)
    controls = fetch_control_matches(text, 3)
    chunks = fetch_vector_matches(text, 3)
    if not admission and not controls and not chunks:
        return ""
    parts = ["本地数据库检索结果："]
    if admission:
        parts.append("结构化投档线命中：")
        parts.extend(f"- {item}" for item in admission)
    if controls:
        parts.append("省控线/分段资料命中：")
        parts.extend(f"- {item}" for item in controls)
    if chunks:
        parts.append("向量资料命中：")
        parts.extend(f"- {item}" for item in chunks)
    parts.append("使用要求：回答时给出简短推断过程；不能把历史投档线当作当年录取承诺。")
    return "\n".join(parts)


def build_prompt(event: Dict[str, Any], text: str) -> str:
    message_type = event.get("message_type", "")
    user_id = str(event.get("user_id", ""))
    rag = context_block(event)
    knowledge = knowledge_search_block(text)
    if knowledge:
        rag = f"{rag}\n\n{knowledge}" if rag else knowledge
    if message_type == "group":
        group_id = str(event.get("group_id", ""))
        return f"""你正在 QQ 群聊中回应用户的 @ 提问。
场景信息：
- QQ 群号：{group_id}
- 用户 QQ：{user_id}
{rag}

用户问题：
{text}

要求：
- 如果问题与高考志愿填报相关，按高考志愿咨询规则回答。
- 如果问题与高考志愿无关，可以自然闲聊或正常帮助，但回答要适合 QQ 群聊阅读，尽量简洁。
- 使用 QQ 纯文本格式输出，不要使用 Markdown 标题、加粗、代码块、表格或链接语法。
- 涉及高考志愿时，回答中要包含简短推断过程，说明你如何从分数/位次、年份、科类、院校专业组或专业偏好推到建议。
- 涉及高考志愿时，如果信息不足，先列出需要补充的信息。
- 涉及高考志愿时，不要编造招生计划、专业组代码、投档线，不承诺必录取。
- 涉及高考志愿时，最后提醒以本省教育考试院和高校招生章程为准。
"""
    return f"""你正在 QQ 私聊中回应用户。
用户 QQ：{user_id}
{rag}

用户问题：
{text}

要求：
- 可以比群聊更详细。
- 如果问题与高考志愿填报相关，按高考志愿咨询规则回答。
- 如果问题与高考志愿无关，可以自然闲聊或正常帮助。
- 使用 QQ 纯文本格式输出，不要使用 Markdown 标题、加粗、代码块、表格或链接语法。
- 涉及高考志愿时，回答中要包含简短推断过程，说明你如何从分数/位次、年份、科类、院校专业组或专业偏好推到建议。
- 涉及高考志愿时，如果信息不足，先要求补充省份、年份、选科/科类、分数、位次、城市偏好、专业偏好。
- 涉及高考志愿时，不要编造招生计划、专业组代码、投档线，不承诺必录取。
- 涉及高考志愿时，最后提醒以本省教育考试院和高校招生章程为准。
"""


def call_openclaw(prompt: str, session_key: str) -> str:
    cmd = [
        SETTINGS.openclaw_cli,
        "agent",
        "--agent",
        SETTINGS.openclaw_agent,
        "--session-key",
        session_key,
        "--channel",
        SETTINGS.openclaw_channel,
        "--message",
        prompt,
    ]
    LOG.info("Calling OpenClaw: %s <prompt>", " ".join(shlex.quote(x) for x in cmd[:-1]))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SETTINGS.openclaw_timeout,
            env=os.environ.copy(),
            check=False,
        )
    except FileNotFoundError:
        return "后端问答服务未找到 openclaw 命令，请联系管理员检查配置。"
    except subprocess.TimeoutExpired:
        return "这次分析超时了。请把省份、选科/科类、分数、位次、专业偏好补充完整后再问一次。"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        LOG.error("OpenClaw error: %s", err[:1000])
        return "后端问答服务暂时不可用，请稍后再试。"

    out = (proc.stdout or "").strip()
    return out or "后端没有返回有效内容，请稍后再试。"


def strip_markdown(text: str) -> str:
    text = text.strip()
    if not text:
        return text

    text = re.sub(r"```(?:[\w+-]+)?\n?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "· ", text)
    text = re.sub(r"(?m)^\s*(\d+)\.\s+", r"\1. ", text)
    text = re.sub(r"(?m)^\s*\|?[-:| ]{3,}\|?\s*$", "", text)
    text = re.sub(r"(?m)^\|(.+)\|$", lambda m: m.group(1).replace("|", " / ").strip(), text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_reply(text: str) -> List[str]:
    text = strip_markdown(text)
    max_len = SETTINGS.max_reply_chars_per_msg
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind("。", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


async def onebot_action(ws: Any, action: str, params: Dict[str, Any]) -> None:
    payload = {"action": action, "params": params, "echo": str(uuid.uuid4())}
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def send_private_msg(ws: Any, user_id: str, message: str, message_id: Optional[Any] = None) -> None:
    for index, chunk in enumerate(split_reply(message)):
        payload = reply_message_payload(chunk, message_id, index == 0)
        await onebot_action(ws, "send_private_msg", {"user_id": int(user_id), "message": payload})
        await asyncio.sleep(0.4)


def reply_message_payload(chunk: str, message_id: Optional[Any], include_reply: bool) -> Any:
    if not include_reply or message_id in (None, ""):
        return chunk
    return [
        {"type": "reply", "data": {"id": str(message_id)}},
        {"type": "text", "data": {"text": chunk}},
    ]


async def send_group_msg(ws: Any, group_id: str, message: str, message_id: Optional[Any] = None) -> None:
    for index, chunk in enumerate(split_reply(message)):
        payload = reply_message_payload(chunk, message_id, index == 0)
        await onebot_action(ws, "send_group_msg", {"group_id": int(group_id), "message": payload})
        await asyncio.sleep(0.4)


def should_reply(event: Dict[str, Any], text: str, raw_message: Any) -> tuple[bool, str, str]:
    message_type = event.get("message_type")
    user_id = str(event.get("user_id", ""))

    if not user_id:
        return False, "", "missing_user_id"
    if user_id in SETTINGS.blocked_users:
        return False, "", "blocked_user"
    if STORE.is_rate_limited(f"user:{user_id}", SETTINGS.user_cooldown_seconds):
        return False, "", "user_rate_limited"

    if message_type == "private":
        return True, f"qq-private-{user_id}", "private"

    if message_type != "group":
        return False, "", "unsupported_message_type"

    group_id = str(event.get("group_id", ""))
    if SETTINGS.allowed_groups and group_id not in SETTINGS.allowed_groups:
        return False, "", "group_not_allowed"
    if STORE.is_rate_limited(f"group:{group_id}", SETTINGS.group_cooldown_seconds):
        return False, "", "group_rate_limited"

    at_bot = is_at_bot(raw_message)
    if SETTINGS.group_require_mention and not at_bot:
        return False, "", "no_at_mention"

    return True, f"qq-group-{group_id}-user-{user_id}", "group"


async def handle_message(ws: Any, event: Dict[str, Any]) -> None:
    if event.get("post_type") != "message":
        return

    message_type = str(event.get("message_type", ""))
    raw_message = event.get("message")
    message_id = event.get("message_id")
    user_id = str(event.get("user_id", ""))
    group_id = str(event.get("group_id", "")) if message_type == "group" else None
    text = normalize_message(raw_message)
    if not text:
        STORE.record_message(message_type, user_id, group_id, "", False, "empty_text")
        return
    if len(text) > SETTINGS.max_input_chars:
        text = text[: SETTINGS.max_input_chars] + "\n[用户消息过长，已截断]"

    reply_allowed, session_key, reason = should_reply(event, text, raw_message)
    STORE.record_message(message_type, user_id, group_id, text, reply_allowed, reason)
    if not reply_allowed:
        LOG.info("Ignored message user=%s group=%s reason=%s", user_id, group_id, reason)
        return

    LOG.info("Incoming %s user=%s group=%s text=%r", message_type, user_id, group_id, text[:80])
    prompt = build_prompt(event, text)
    reply = await asyncio.to_thread(call_openclaw, prompt, session_key)

    if message_type == "private":
        await send_private_msg(ws, user_id, reply, message_id)
    elif group_id:
        await send_group_msg(ws, group_id, reply, message_id)


async def main_loop() -> None:
    headers = {}
    if SETTINGS.napcat_access_token:
        headers["Authorization"] = f"Bearer {SETTINGS.napcat_access_token}"

    backoff = 1
    while not SHUTDOWN.is_set():
        try:
            LOG.info("Connecting NapCat WS: %s", SETTINGS.napcat_ws_url)
            async with websockets.connect(
                SETTINGS.napcat_ws_url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            ) as ws:
                LOG.info("Connected to NapCat, database=%s", SETTINGS.db_path)
                backoff = 1
                async for msg in ws:
                    try:
                        event = json.loads(msg)
                    except json.JSONDecodeError:
                        LOG.warning("Non-JSON message: %s", msg[:200])
                        continue
                    if "post_type" not in event:
                        continue
                    asyncio.create_task(handle_message(ws, event))
        except Exception as exc:
            LOG.error("WS error: %r", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def handle_signal(*_args: object) -> None:
    SHUTDOWN.set()


def main() -> int:
    if sys.version_info < (3, 9):
        print("Python 3.9+ required", file=sys.stderr)
        return 1
    LOG.info("Starting qqbot bridge")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(main_loop())
    finally:
        STORE.conn.close()
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
