#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pdfplumber


DATA_DIR = Path(os.getenv("DATA_DIR", "/Volumes/File/qqbot-data")).expanduser()
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", str(DATA_DIR / "knowledge"))).expanduser()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "qqbot.sqlite3"))).expanduser()
SOFFICE = Path(os.getenv("SOFFICE", "/Users/alanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/soffice"))
PDFTOPPM = Path(os.getenv("PDFTOPPM", "/Users/alanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/pdftoppm"))
TESSERACT = Path(os.getenv("TESSERACT", "/opt/homebrew/bin/tesseract"))
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "chi_sim+eng")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")

TABLE_SUFFIXES = {".xls", ".xlsx", ".csv", ".tsv"}
PDF_SUFFIXES = {".pdf"}
TEXT_SUFFIXES = {".md", ".txt"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compact(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def infer_year(path: Path) -> int | None:
    match = re.search(r"20\d{2}", str(path))
    return int(match.group(0)) if match else None


def infer_subject(path: Path, row_text: str = "") -> str | None:
    hay = f"{path} {row_text}"
    if any(k in hay for k in ("物理", "理科", "理工")):
        return "物理"
    if any(k in hay for k in ("历史", "文科", "文史")):
        return "历史"
    return None


def infer_batch(path: Path, row_text: str = "") -> str | None:
    hay = f"{path} {row_text}"
    mapping = [
        ("乡村教师", "乡村教师"),
        ("公安政法", "公安政法"),
        ("地方专项", "地方专项"),
        ("医学定向", "医学定向"),
        ("军事", "军事"),
        ("军队", "军事"),
        ("航海", "航海"),
        ("提前", "提前批"),
        ("普通批", "普通批"),
        ("本科批", "本科批"),
    ]
    for key, value in mapping:
        if key in hay:
            return value
    return None


def file_kind(path: Path) -> str:
    name = path.name
    if any(k in name for k in ("一分", "逐分", "分段", "省控", "控制线")):
        return "score_segment"
    if any(k in name for k in ("投档线", "本科录取", "平行志愿")):
        return "admission_line"
    return "text"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=30000;
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
        CREATE INDEX IF NOT EXISTS idx_admission_line_rows_year_subject_batch
            ON admission_line_rows(year, subject, batch);
        CREATE INDEX IF NOT EXISTS idx_admission_line_rows_min_rank
            ON admission_line_rows(min_rank);
        CREATE INDEX IF NOT EXISTS idx_score_segment_rows_year_subject_score
            ON score_segment_rows(year, subject, score);
        CREATE INDEX IF NOT EXISTS idx_control_line_rows_year_subject_batch
            ON control_line_rows(year, subject, batch);
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            content,
            source_path UNINDEXED,
            row_table UNINDEXED,
            row_id UNINDEXED
        );
        """
    )
    return conn


def upsert_file(conn: sqlite3.Connection, path: Path, kind: str) -> int:
    stat = path.stat()
    rel = str(path.relative_to(KNOWLEDGE_DIR))
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO knowledge_files
            (source_path, rel_path, title, kind, year, subject, batch, content_hash, size_bytes, mtime, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            rel_path = excluded.rel_path,
            title = excluded.title,
            kind = excluded.kind,
            year = excluded.year,
            subject = excluded.subject,
            batch = excluded.batch,
            content_hash = excluded.content_hash,
            size_bytes = excluded.size_bytes,
            mtime = excluded.mtime,
            updated_at = excluded.updated_at
        """,
        (
            str(path),
            rel,
            path.stem,
            kind,
            infer_year(path),
            infer_subject(path),
            infer_batch(path),
            sha256_file(path),
            stat.st_size,
            int(stat.st_mtime),
            now,
        ),
    )
    row = conn.execute("SELECT id FROM knowledge_files WHERE source_path = ?", (str(path),)).fetchone()
    assert row is not None
    file_id = int(row["id"])
    conn.execute("DELETE FROM admission_line_rows WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM score_segment_rows WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM control_line_rows WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM knowledge_text_chunks WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM knowledge_fts WHERE source_path = ?", (str(path),))
    return file_id


def convert_xls_to_xlsx(path: Path, out_dir: Path) -> Path:
    if path.suffix.lower() == ".xlsx":
        return path
    subprocess.run(
        [str(SOFFICE), "--headless", "--convert-to", "xlsx", "--outdir", str(out_dir), str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    converted = out_dir / f"{path.stem}.xlsx"
    if not converted.exists():
        matches = list(out_dir.glob("*.xlsx"))
        if not matches:
            raise RuntimeError(f"LibreOffice did not produce xlsx for {path}")
        converted = matches[0]
    return converted


def table_rows_from_spreadsheet(path: Path) -> Iterable[list[str]]:
    with tempfile.TemporaryDirectory(prefix="qqbot-xls-") as tmp:
        source = convert_xls_to_xlsx(path, Path(tmp))
        sheets = pd.read_excel(source, sheet_name=None, header=None, dtype=str, engine="openpyxl")
        for _, df in sheets.items():
            df = df.replace({np.nan: ""})
            for row in df.itertuples(index=False, name=None):
                values = [compact(v) for v in row]
                while values and not values[-1]:
                    values.pop()
                if sum(1 for v in values if v) >= 2:
                    yield values


def text_from_pdf(path: Path) -> tuple[list[list[str]], str]:
    rows: list[list[str]] = []
    text_parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                text_parts.append(text.strip())
            for table in page.extract_tables() or []:
                for row in table:
                    values = [compact(v) for v in row]
                    while values and not values[-1]:
                        values.pop()
                    if sum(1 for v in values if v) >= 2:
                        rows.append(values)
    text = "\n\n".join(text_parts)
    if text.strip() or rows:
        return rows, text

    return rows, ocr_pdf(path)


def tesseract_lang_arg() -> str:
    try:
        proc = subprocess.run(
            [str(TESSERACT), "--list-langs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    available = set(proc.stdout.splitlines()[1:])
    requested = [lang for lang in TESSERACT_LANG.split("+") if lang]
    usable = [lang for lang in requested if lang in available]
    if usable:
        return "+".join(usable)
    return "eng" if "eng" in available else ""


def ocr_pdf(path: Path) -> str:
    """OCR fallback for image-only PDFs.

    The normal path uses pdfplumber for digital PDFs. If no text/tables are
    extractable, render pages with poppler and run the local tesseract binary.
    Chinese OCR requires a local chi_sim traineddata file; when unavailable,
    tesseract falls back to any usable installed language.
    """
    if not PDFTOPPM.exists() or not TESSERACT.exists():
        return ""
    lang = tesseract_lang_arg()
    if not lang:
        return ""
    with tempfile.TemporaryDirectory(prefix="qqbot-ocr-") as tmp:
        tmp_path = Path(tmp)
        prefix = tmp_path / "page"
        render = subprocess.run(
            [str(PDFTOPPM), "-png", "-r", "220", str(path), str(prefix)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if render.returncode != 0:
            return ""
        text_parts: list[str] = []
        for image in sorted(tmp_path.glob("page-*.png")):
            proc = subprocess.run(
                [str(TESSERACT), str(image), "stdout", "-l", lang, "--psm", "6"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                text_parts.append(proc.stdout.strip())
        return "\n\n".join(text_parts)


def numbers_from_text(text: str) -> list[int]:
    out = []
    for match in re.findall(r"(?<!\d)(\d{2,7})(?!\d)", text.replace(",", "")):
        try:
            out.append(int(match))
        except ValueError:
            pass
    return out


def parse_admission_row(path: Path, row_index: int, values: list[str]) -> dict[str, Any] | None:
    row_text = " ".join(v for v in values if v)
    if not row_text or any(k in row_text for k in ("院校代号 院校", "注：", "说明：")):
        return None

    nums = numbers_from_text(row_text)
    school_name = None
    group_code = None
    group_name = None
    min_score = None
    min_rank = None

    # Most Jiangsu投档线 sheets are shaped like:
    # c0=院校/专业组代码, c1=院校专业组名称, c2=投档最低分, later cols=同分排序项/最低位次.
    if len(values) >= 3 and re.fullmatch(r"\d{3,6}", values[0] or "") and re.search(r"[\u4e00-\u9fff]", values[1] or ""):
        group_code = values[0]
        group_name = values[1]
        school_name = re.split(r"\d{2,4}专业组|专业组", group_name, maxsplit=1)[0].strip() or group_name
        try:
            score_value = int(float(values[2]))
            if 100 <= score_value <= 750:
                min_score = score_value
        except ValueError:
            pass
        tail_nums = []
        for value in values[3:]:
            tail_nums.extend(numbers_from_text(value))
        rank_values = [n for n in tail_nums if n > 750]
        if rank_values:
            min_rank = rank_values[0]

    if group_code is None:
        match = re.search(r"(\d{4,6})\s*专业组|专业组\s*(\d{2,6})|(\d{4,6})", row_text)
        if match:
            group_code = next(g for g in match.groups() if g)
    if group_name is None:
        group_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()·\-\s]*专业组[^\s]*)", row_text)
        if group_match:
            group_name = compact(group_match.group(1))
    if school_name is None:
        text_cells = [v for v in values if re.search(r"[\u4e00-\u9fff]", v)]
        for cell in text_cells:
            if not any(k in cell for k in ("院校", "专业组", "投档", "分数", "位次", "科目", "名称")):
                school_name = cell
                break
    if min_score is None:
        score_candidates = [n for n in nums if 100 <= n <= 750]
        min_score = score_candidates[0] if score_candidates else None
    # Do not infer rank from arbitrary large numbers in the row: Jiangsu rows
    # often start with a 4-digit院校代号, which is not a最低位次.

    if not min_score and not min_rank and not school_name:
        return None
    return {
        "year": infer_year(path),
        "subject": infer_subject(path, row_text),
        "batch": infer_batch(path, row_text),
        "row_index": row_index,
        "school_name": school_name,
        "major_group_code": group_code,
        "major_group_name": group_name,
        "min_score": min_score,
        "min_rank": min_rank,
        "columns_json": json.dumps({f"c{i}": v for i, v in enumerate(values)}, ensure_ascii=False),
        "row_text": row_text,
    }


def parse_segment_row(path: Path, row_index: int, values: list[str]) -> dict[str, Any] | None:
    row_text = " ".join(v for v in values if v)
    nums = numbers_from_text(row_text)
    if not nums:
        return None
    score = next((n for n in nums if 100 <= n <= 750), None)
    if score is None:
        return None
    counts = [n for n in nums if n != score and n >= 0]
    return {
        "year": infer_year(path),
        "subject": infer_subject(path, row_text),
        "row_index": row_index,
        "score": score,
        "same_score_count": counts[0] if counts else None,
        "cumulative_count": counts[1] if len(counts) > 1 else None,
        "rank": counts[1] if len(counts) > 1 else (counts[0] if counts else None),
        "columns_json": json.dumps({f"c{i}": v for i, v in enumerate(values)}, ensure_ascii=False),
        "row_text": row_text,
    }


def insert_admission_rows(conn: sqlite3.Connection, file_id: int, path: Path, rows: Iterable[list[str]]) -> int:
    now = int(time.time())
    count = 0
    for idx, values in enumerate(rows):
        parsed = parse_admission_row(path, idx, values)
        if not parsed:
            continue
        conn.execute(
            """
            INSERT INTO admission_line_rows
                (file_id, year, subject, batch, row_index, school_name, major_group_code, major_group_name,
                 min_score, min_rank, columns_json, row_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                parsed["year"],
                parsed["subject"],
                parsed["batch"],
                parsed["row_index"],
                parsed["school_name"],
                parsed["major_group_code"],
                parsed["major_group_name"],
                parsed["min_score"],
                parsed["min_rank"],
                parsed["columns_json"],
                parsed["row_text"],
                now,
            ),
        )
        conn.execute(
            "INSERT INTO knowledge_fts(content, source_path, row_table, row_id) VALUES (?, ?, ?, last_insert_rowid())",
            (parsed["row_text"], str(path), "admission_line_rows"),
        )
        count += 1
    return count


def insert_segment_rows(conn: sqlite3.Connection, file_id: int, path: Path, rows: Iterable[list[str]]) -> int:
    now = int(time.time())
    count = 0
    for idx, values in enumerate(rows):
        parsed = parse_segment_row(path, idx, values)
        if not parsed:
            continue
        conn.execute(
            """
            INSERT INTO score_segment_rows
                (file_id, year, subject, row_index, score, same_score_count, cumulative_count,
                 rank, columns_json, row_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                parsed["year"],
                parsed["subject"],
                parsed["row_index"],
                parsed["score"],
                parsed["same_score_count"],
                parsed["cumulative_count"],
                parsed["rank"],
                parsed["columns_json"],
                parsed["row_text"],
                now,
            ),
        )
        conn.execute(
            "INSERT INTO knowledge_fts(content, source_path, row_table, row_id) VALUES (?, ?, ?, last_insert_rowid())",
            (parsed["row_text"], str(path), "score_segment_rows"),
        )
        count += 1
    return count


def insert_control_lines_from_text(conn: sqlite3.Connection, file_id: int, path: Path, text: str) -> int:
    year = infer_year(path)
    now = int(time.time())
    lines = [compact(line) for line in text.splitlines() if compact(line)]
    count = 0
    seen: set[tuple[int | None, str, str, int]] = set()
    for idx in range(len(lines) - 1):
        header = lines[idx]
        values_line = lines[idx + 1]
        if "本科" not in header and "一本" not in header:
            continue
        batches = [x for x in re.split(r"\s+", header) if x]
        parts = [x for x in re.split(r"\s+", values_line) if x]
        if not parts or parts[0] not in {"理", "文", "物理", "历史"}:
            continue
        subject = {"理": "物理", "文": "历史"}.get(parts[0], parts[0])
        scores = []
        for part in parts[1:]:
            try:
                score = int(part)
            except ValueError:
                continue
            if 100 <= score <= 750:
                scores.append(score)
        for batch, score in zip(batches, scores):
            key = (year, subject, batch, score)
            if key in seen:
                continue
            seen.add(key)
            row_text = f"{year or ''} {subject} {batch} {score}".strip()
            conn.execute(
                """
                INSERT INTO control_line_rows
                    (file_id, year, subject, batch, score, columns_json, row_text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    year,
                    subject,
                    batch,
                    score,
                    json.dumps({"header": header, "line": values_line}, ensure_ascii=False),
                    row_text,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_fts(content, source_path, row_table, row_id) VALUES (?, ?, ?, last_insert_rowid())",
                (row_text, str(path), "control_line_rows"),
            )
            count += 1
    return count


def chunk_text(text: str, max_chars: int = 900, overlap: int = 120) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(0, end - overlap)
    return [c for c in chunks if len(c) >= 40]


def ollama_embedding(text: str) -> list[float]:
    payload = json.dumps({"model": EMBEDDING_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama embedding failed: HTTP {exc.code}: {body}") from exc
    return [float(x) for x in data["embedding"]]


def insert_text_chunks(conn: sqlite3.Connection, file_id: int, path: Path, text: str, embed: bool) -> int:
    now = int(time.time())
    chunks = chunk_text(text)
    for idx, chunk in enumerate(chunks):
        embedding = ollama_embedding(chunk) if embed else None
        conn.execute(
            """
            INSERT INTO knowledge_text_chunks
                (file_id, chunk_index, content, embedding_model, embedding_json, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                idx,
                chunk,
                EMBEDDING_MODEL if embedding else None,
                json.dumps(embedding) if embedding else None,
                json.dumps({"source_path": str(path)}, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "INSERT INTO knowledge_fts(content, source_path, row_table, row_id) VALUES (?, ?, ?, last_insert_rowid())",
            (chunk, str(path), "knowledge_text_chunks"),
        )
    return len(chunks)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030", errors="ignore")


def iter_files() -> list[Path]:
    suffixes = TABLE_SUFFIXES | PDF_SUFFIXES | TEXT_SUFFIXES
    return sorted(
        p
        for p in KNOWLEDGE_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in suffixes
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import qqbot knowledge files into SQLite and local embeddings.")
    parser.add_argument("--no-embed", action="store_true", help="skip Ollama embeddings")
    parser.add_argument("--limit", type=int, default=0, help="only process first N files")
    args = parser.parse_args()

    conn = connect()
    totals = {"files": 0, "admission": 0, "segments": 0, "control": 0, "chunks": 0, "errors": 0}
    files = iter_files()
    if args.limit:
        files = files[: args.limit]

    for path in files:
        kind = file_kind(path)
        try:
            file_id = upsert_file(conn, path, kind)
            rows: list[list[str]] = []
            text = ""
            if path.suffix.lower() in TABLE_SUFFIXES:
                rows = list(table_rows_from_spreadsheet(path))
            elif path.suffix.lower() in PDF_SUFFIXES:
                rows, text = text_from_pdf(path)
            elif path.suffix.lower() in TEXT_SUFFIXES:
                text = read_text_file(path)

            if kind == "score_segment":
                totals["segments"] += insert_segment_rows(conn, file_id, path, rows)
                if text:
                    totals["control"] += insert_control_lines_from_text(conn, file_id, path, text)
                if text:
                    totals["chunks"] += insert_text_chunks(conn, file_id, path, text, not args.no_embed)
            elif kind == "admission_line":
                totals["admission"] += insert_admission_rows(conn, file_id, path, rows)
                if path.suffix.lower() in PDF_SUFFIXES and text and not rows:
                    totals["chunks"] += insert_text_chunks(conn, file_id, path, text, not args.no_embed)
            else:
                if not text and rows:
                    text = "\n".join(" ".join(r) for r in rows)
                totals["chunks"] += insert_text_chunks(conn, file_id, path, text, not args.no_embed)
            conn.commit()
            totals["files"] += 1
            print(f"OK {kind}: {path.relative_to(KNOWLEDGE_DIR)}")
        except Exception as exc:
            conn.rollback()
            totals["errors"] += 1
            print(f"ERROR {path}: {exc}")

    print(json.dumps(totals, ensure_ascii=False))
    conn.close()
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
