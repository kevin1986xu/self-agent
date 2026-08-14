"""知识库 MVP（M2-6 / R16，技术方案 4.10）。

文档（PDF/Word/Markdown/txt）→ 解析 → 分块 → Postgres 入库 → search_knowledge
工具供 agent 检索引用。

检索双模式（可插拔升级，代码不动）：
- 配置了 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL →
  pgvector 余弦检索（入库时向量化）；
- 未配置（当前 token plan 无 embedding 模型）→ pg_trgm 相似度 + 关键词
  兜底检索，对中文三元组可用；配好 embedding 后 `reindex` 一次即升级。

CLI：python -m self_agent.knowledge add <文件> [scope] | list | remove <id>
     | search <query> | reindex
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

EMB_BASE = os.environ.get("EMBEDDING_BASE_URL", "")
EMB_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMB_MODEL = os.environ.get("EMBEDDING_MODEL", "")
EMB_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
CHUNK_SIZE, CHUNK_OVERLAP = 500, 80

_DDL = f"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS knowledge_doc (
    id BIGSERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    chunks INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT REFERENCES knowledge_doc(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector({EMB_DIM})
);
CREATE INDEX IF NOT EXISTS knowledge_chunk_trgm
    ON knowledge_chunk USING gin (content gin_trgm_ops);
"""


def _conn():
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("需要 DATABASE_URL")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(_DDL)
    return conn


def _embed(texts: list[str]) -> list[list[float]] | None:
    if not (EMB_BASE and EMB_KEY and EMB_MODEL):
        return None
    r = httpx.post(f"{EMB_BASE}/embeddings", headers={"Authorization": f"Bearer {EMB_KEY}"},
                   json={"model": EMB_MODEL, "input": texts}, timeout=60)
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    if suffix in (".md", ".txt", ".markdown"):
        return path.read_text(errors="replace")
    raise ValueError(f"不支持的格式: {suffix}（支持 pdf/docx/md/txt）")


def chunk_text(text: str) -> list[str]:
    """按段落聚合到 CHUNK_SIZE，超长段落硬切并保留 overlap。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= CHUNK_SIZE:
            buf = f"{buf}\n{p}".strip()
            continue
        if buf:
            chunks.append(buf)
        while len(p) > CHUNK_SIZE:
            chunks.append(p[:CHUNK_SIZE])
            p = p[CHUNK_SIZE - CHUNK_OVERLAP:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks


def add_document(path: str | Path, *, scope: str = "global", uploaded_by: str = "cli") -> dict:
    path = Path(path)
    chunks = chunk_text(parse_file(path))
    if not chunks:
        raise ValueError("文档解析后无内容")
    vectors = _embed(chunks)
    with _conn() as c:
        doc_id = c.execute(
            "INSERT INTO knowledge_doc (filename, scope, chunks, uploaded_by)"
            " VALUES (%s,%s,%s,%s) RETURNING id",
            (path.name, scope, len(chunks), uploaded_by)).fetchone()[0]
        for i, content in enumerate(chunks):
            c.execute(
                "INSERT INTO knowledge_chunk (doc_id, seq, content, embedding) VALUES (%s,%s,%s,%s)",
                (doc_id, i, content, vectors[i] if vectors else None))
    return {"doc_id": doc_id, "filename": path.name, "chunks": len(chunks),
            "mode": "vector" if vectors else "trgm"}


def _query_terms(query: str, cap: int = 10) -> list[str]:
    """中文连续段切 2-gram + 非中文词，去重限量，作词项覆盖打分用。"""
    terms: list[str] = []
    for seg in re.findall(r"[一-鿿]+|[A-Za-z0-9.]+", query):
        if re.match(r"[一-鿿]", seg):
            terms += [seg[i:i + 2] for i in range(len(seg) - 1)] if len(seg) > 1 else [seg]
        else:
            terms.append(seg)
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen[:cap]


def search(query: str, *, k: int = 5, scope: str | None = None) -> list[dict]:
    """scope: 项目作用域；命中该 scope 与 global 的文档。None=不过滤。"""
    with _conn() as c:
        vec = _embed([query]) if (EMB_BASE and EMB_KEY and EMB_MODEL) else None
        if vec:
            rows = c.execute(
                """SELECT d.filename, ch.seq, ch.content, 1 - (ch.embedding <=> %s::vector) AS score
                   FROM knowledge_chunk ch JOIN knowledge_doc d ON d.id = ch.doc_id
                   WHERE ch.embedding IS NOT NULL
                     AND (%s::text IS NULL OR d.scope IN (%s, 'global'))
                   ORDER BY ch.embedding <=> %s::vector LIMIT %s""",
                (vec[0], scope, scope, vec[0], k)).fetchall()
        else:
            # trgm 模式：整句 word_similarity（similarity 会被块长度稀释）
            # + 中文 2-gram 词项覆盖率（语义改写的 query 靠词项部分命中兜底）
            terms = _query_terms(query)
            rows = c.execute(
                """SELECT d.filename, ch.seq, ch.content,
                          GREATEST(
                            word_similarity(%s, ch.content),
                            CASE WHEN ch.content ILIKE '%%' || %s || '%%' THEN 0.9 ELSE 0 END,
                            (SELECT COALESCE(AVG(CASE WHEN ch.content ILIKE '%%' || t || '%%'
                                                      THEN 1.0 ELSE 0 END), 0) * 0.6
                             FROM unnest(%s::text[]) AS t)
                          ) AS score
                   FROM knowledge_chunk ch JOIN knowledge_doc d ON d.id = ch.doc_id
                   WHERE (%s::text IS NULL OR d.scope IN (%s, 'global'))
                   ORDER BY score DESC LIMIT %s""",
                (query, query, terms, scope, scope, k)).fetchall()
    return [{"file": r[0], "seq": r[1], "content": r[2], "score": round(float(r[3]), 3)}
            for r in rows if float(r[3]) > 0.05]


def list_docs() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, filename, scope, chunks, created_at::date FROM knowledge_doc ORDER BY id").fetchall()
    return [dict(zip(["id", "filename", "scope", "chunks", "date"], r)) for r in rows]


def remove(doc_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM knowledge_doc WHERE id=%s", (doc_id,))


def reindex() -> int:
    """embedding 配置就绪后，为所有缺向量的块补向量。"""
    if not (EMB_BASE and EMB_KEY and EMB_MODEL):
        raise RuntimeError("未配置 EMBEDDING_*，无法向量化")
    n = 0
    with _conn() as c:
        rows = c.execute("SELECT id, content FROM knowledge_chunk WHERE embedding IS NULL").fetchall()
        for batch_start in range(0, len(rows), 16):
            batch = rows[batch_start:batch_start + 16]
            vecs = _embed([r[1] for r in batch])
            for (cid, _), v in zip(batch, vecs):
                c.execute("UPDATE knowledge_chunk SET embedding=%s WHERE id=%s", (v, cid))
                n += 1
    return n


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "add":
        info = add_document(args[1], scope=args[2] if len(args) > 2 else "global")
        print(f"已入库 doc#{info['doc_id']} {info['filename']}：{info['chunks']} 块（{info['mode']} 模式）")
    elif cmd == "list":
        for d in list_docs():
            print(f"#{d['id']:<4} {d['filename']:32} scope={d['scope']} chunks={d['chunks']} {d['date']}")
    elif cmd == "remove":
        remove(int(args[1]))
        print("已删除")
    elif cmd == "search":
        for hit in search(" ".join(args[1:])):
            print(f"[{hit['score']}] {hit['file']}#{hit['seq']}: {hit['content'][:80]}")
    elif cmd == "reindex":
        print(f"已补向量 {reindex()} 块")
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
