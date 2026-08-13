"""技能管理子系统（M2-3/4/5，技术方案 4.3.1）。

生命周期：导入（zip / GitHub URL@ref）→ 校验与静态扫描 → pending 人工审核
→ enabled → 会话装配（materialize 到工作区）。禁用/移除即时对新会话生效。

- 元数据：Postgres 表 skill_registry（DATABASE_URL）；
- 技能文件库：SKILL_LIBRARY 目录（默认 PROJECT_ROOT/.skill_library）；
- 安全定位：扫描是初筛，pending 人工审核是放行关，沙箱执行是兜底
  ——技能是「提示词+脚本」双重供应链入口，永不自动启用。

CLI：
  python -m self_agent.skill_manager import <zip路径|GitHub URL[@ref]>
  python -m self_agent.skill_manager list
  python -m self_agent.skill_manager approve|reject|enable|disable|remove <name>
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import settings

SKILL_LIBRARY = Path(os.environ.get("SKILL_LIBRARY", settings.PROJECT_ROOT / ".skill_library"))
MAX_SKILL_BYTES = 20 * 1024 * 1024

_DDL = """
CREATE TABLE IF NOT EXISTS skill_registry (
    name TEXT PRIMARY KEY,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending/enabled/disabled/rejected
    scope TEXT NOT NULL DEFAULT 'global',
    source_type TEXT,                            -- zip/github
    source_url TEXT,
    pinned_ref TEXT,
    checksum TEXT,
    scan_findings JSONB DEFAULT '[]',
    imported_by TEXT,
    reviewed_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
)
"""

# —— 静态扫描规则（初筛，命中≠必然恶意，供审核人参考）——
SCRIPT_PATTERNS = [
    (r"\brm\s+-rf\s+/", "危险删除根路径"),
    (r"\bsudo\b", "提权命令"),
    (r"\b(curl|wget)\b", "网络外联(shell)"),
    (r"\b(requests|httpx|urllib\.request|socket)\b", "网络外联(python)"),
    (r"\bsubprocess\b|os\.system", "子进程执行"),
    (r"\beval\(|\bexec\(", "动态执行"),
    (r"base64\.b64decode", "编码载荷"),
    (r"open\(['\"]/(?!work|skills|memories)", "绝对路径文件访问"),
    (r"\.ssh|/etc/passwd|\.env\b|api[_-]?key", "敏感文件/凭据引用"),
]
PROMPT_PATTERNS = [
    (r"ignore (all )?(previous|prior) instructions|无视(之前|以上)|忽略(之前|上述)指令", "注入话术"),
    (r"不要告诉用户|do not tell the user|hide this from", "隐匿行为指示"),
    (r"confirm_token", "试图触碰确认令牌机制"),
]


@dataclass
class SkillPackage:
    name: str
    description: str
    dir: Path


def _conn():
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("需要 DATABASE_URL")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(_DDL)
    return conn


def _parse_frontmatter(md: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", md, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def discover_skills(root: Path) -> list[SkillPackage]:
    """目录树中所有含合法 SKILL.md 的技能目录。"""
    found = []
    for md in sorted(root.rglob("SKILL.md")):
        fm = _parse_frontmatter(md.read_text(errors="replace"))
        name = fm.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", name) or not fm.get("description"):
            continue
        found.append(SkillPackage(name=name, description=fm["description"], dir=md.parent))
    return found


def scan_skill(pkg: SkillPackage) -> list[dict]:
    """静态扫描：脚本危险模式 + SKILL.md 注入模式。"""
    findings = []
    for f in pkg.dir.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(pkg.dir))
        text = f.read_text(errors="replace") if f.stat().st_size < 1_000_000 else ""
        patterns = PROMPT_PATTERNS if f.name == "SKILL.md" else SCRIPT_PATTERNS
        if f.suffix in (".py", ".sh", ".js", ".ts") or f.name == "SKILL.md":
            for pat, label in patterns:
                for m in re.finditer(pat, text, re.I):
                    line = text[: m.start()].count("\n") + 1
                    findings.append({"file": rel, "line": line, "issue": label,
                                     "match": m.group(0)[:60]})
    return findings


def _checksum(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _install(pkg: SkillPackage, *, source_type: str, source_url: str | None,
             pinned_ref: str | None, imported_by: str) -> dict:
    dest = SKILL_LIBRARY / pkg.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(pkg.dir, dest)
    findings = scan_skill(SkillPackage(pkg.name, pkg.description, dest))
    with _conn() as c:
        c.execute(
            """INSERT INTO skill_registry
               (name, description, status, source_type, source_url, pinned_ref,
                checksum, scan_findings, imported_by)
               VALUES (%s,%s,'pending',%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET
                 description=EXCLUDED.description, status='pending',
                 source_type=EXCLUDED.source_type, source_url=EXCLUDED.source_url,
                 pinned_ref=EXCLUDED.pinned_ref, checksum=EXCLUDED.checksum,
                 scan_findings=EXCLUDED.scan_findings,
                 imported_by=EXCLUDED.imported_by, updated_at=now()""",
            (pkg.name, pkg.description, source_type, source_url, pinned_ref,
             _checksum(dest), json.dumps(findings, ensure_ascii=False), imported_by),
        )
    return {"name": pkg.name, "status": "pending", "findings": len(findings)}


def import_zip(zip_path: str | Path, *, imported_by: str = "cli") -> list[dict]:
    data = Path(zip_path).read_bytes()
    return _import_zip_bytes(data, source_type="zip", source_url=str(zip_path),
                             pinned_ref=None, imported_by=imported_by)


def _import_zip_bytes(data: bytes, **meta) -> list[dict]:
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError(f"技能包超过大小上限 {MAX_SKILL_BYTES} bytes")
    results = []
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():  # zip-slip 防护
                p = (Path(td) / info.filename).resolve()
                if not str(p).startswith(str(Path(td).resolve())):
                    raise ValueError(f"非法路径条目: {info.filename}")
            z.extractall(td)
        pkgs = discover_skills(Path(td))
        if not pkgs:
            raise ValueError("压缩包内未发现合法技能（缺 SKILL.md 或 frontmatter 不规范）")
        for pkg in pkgs:
            results.append(_install(pkg, **meta))
    return results


def import_github(url: str, *, imported_by: str = "cli") -> list[dict]:
    """支持 https://github.com/owner/repo[@ref][/子路径]。"""
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:@([\w./-]+))?/?$", url)
    if not m:
        raise ValueError("GitHub 地址格式：https://github.com/owner/repo[@tag|@sha]")
    owner, repo, ref = m.group(1), m.group(2), m.group(3) or "HEAD"
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    r = httpx.get(zip_url, timeout=60, follow_redirects=True)
    if r.status_code != 200:
        raise ValueError(f"下载失败 {r.status_code}：{zip_url}")
    return _import_zip_bytes(r.content, source_type="github",
                             source_url=f"https://github.com/{owner}/{repo}",
                             pinned_ref=None if ref == "HEAD" else ref,
                             imported_by=imported_by)


def set_status(name: str, status: str, *, reviewed_by: str = "cli") -> None:
    assert status in ("enabled", "disabled", "rejected", "pending")
    with _conn() as c:
        n = c.execute(
            "UPDATE skill_registry SET status=%s, reviewed_by=%s, updated_at=now() WHERE name=%s",
            (status, reviewed_by, name)).rowcount
    if not n:
        raise ValueError(f"技能不存在: {name}")


def remove(name: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM skill_registry WHERE name=%s", (name,))
    shutil.rmtree(SKILL_LIBRARY / name, ignore_errors=True)


def list_skills() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT name, description, status, source_type, source_url, pinned_ref,"
            " jsonb_array_length(scan_findings), reviewed_by FROM skill_registry ORDER BY name"
        ).fetchall()
    keys = ["name", "description", "status", "source", "url", "ref", "findings", "reviewer"]
    return [dict(zip(keys, r)) for r in rows]


def materialize(workspace: Path) -> list[str]:
    """把 enabled 的受管技能装配进工作区（会话装配，M2-5）。"""
    dest_root = workspace / "skills" / "managed"
    shutil.rmtree(dest_root, ignore_errors=True)
    names = []
    try:
        enabled = [s["name"] for s in list_skills() if s["status"] == "enabled"]
    except Exception:  # noqa: BLE001 —— 无 DB 时静默降级为无受管技能
        return []
    for name in enabled:
        src = SKILL_LIBRARY / name
        if src.exists():
            shutil.copytree(src, dest_root / name)
            names.append(name)
    return names


def check_update(name: str) -> dict:
    """github 来源技能：对比远端（pinned_ref 或 HEAD）与本地校验和。"""
    with _conn() as c:
        row = c.execute("SELECT source_type, source_url, pinned_ref, checksum"
                        " FROM skill_registry WHERE name=%s", (name,)).fetchone()
    if not row:
        raise ValueError(f"技能不存在: {name}")
    source_type, source_url, ref, checksum = row
    if source_type != "github":
        raise ValueError("仅 github 来源支持检查更新")
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)", source_url)
    owner, repo = m.group(1), m.group(2)
    r = httpx.get(f"https://codeload.github.com/{owner}/{repo}/zip/{ref or 'HEAD'}",
                  timeout=60, follow_redirects=True)
    r.raise_for_status()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extractall(td)
        pkg = next((p for p in discover_skills(Path(td)) if p.name == name), None)
        if pkg is None:
            return {"name": name, "remote": "已移除", "changed": True}
        remote_sum = _checksum(pkg.dir)
    return {"name": name, "local": checksum, "remote": remote_sum,
            "changed": remote_sum != checksum}


def update(name: str, *, by: str = "cli") -> dict:
    """备份当前版本后从来源重导入；更新后回到 pending 重新过审（安全策略）。"""
    with _conn() as c:
        row = c.execute("SELECT source_type, source_url, pinned_ref"
                        " FROM skill_registry WHERE name=%s", (name,)).fetchone()
    if not row:
        raise ValueError(f"技能不存在: {name}")
    source_type, source_url, ref = row
    if source_type != "github":
        raise ValueError("仅 github 来源支持更新（zip 请重新上传）")
    _backup(name)
    url = source_url + (f"@{ref}" if ref else "")
    results = import_github(url, imported_by=by)
    mine = next((r for r in results if r["name"] == name), None)
    if mine is None:
        raise ValueError("远端仓库中已找不到该技能")
    return mine


def _backup(name: str) -> None:
    import time as _time

    src = SKILL_LIBRARY / name
    if src.exists():
        dest = SKILL_LIBRARY / ".backup" / f"{name}-{int(_time.time())}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)


def rollback(name: str) -> dict:
    """恢复最近一次备份；恢复后置 pending 重新过审。"""
    backups = sorted((SKILL_LIBRARY / ".backup").glob(f"{name}-*"),
                     key=lambda p: p.name) if (SKILL_LIBRARY / ".backup").exists() else []
    if not backups:
        raise ValueError(f"无可用备份: {name}")
    latest = backups[-1]
    dest = SKILL_LIBRARY / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(latest, dest)
    findings = scan_skill(SkillPackage(name, "", dest))
    with _conn() as c:
        c.execute("UPDATE skill_registry SET status='pending', checksum=%s,"
                  " scan_findings=%s, updated_at=now() WHERE name=%s",
                  (_checksum(dest), json.dumps(findings, ensure_ascii=False), name))
    return {"name": name, "restored_from": latest.name, "status": "pending"}


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "import":
        target = args[1]
        results = (import_github(target) if target.startswith("https://github.com/")
                   else import_zip(target))
        for r in results:
            print(f"已导入 {r['name']} → pending（扫描发现 {r['findings']} 项，需审核后 enable）")
    elif cmd == "list":
        for s in list_skills():
            print(f"{s['status']:9} {s['name']:24} findings={s['findings']} "
                  f"src={s['source'] or '-'} ref={s['ref'] or '-'} | {s['description'][:40]}")
    elif cmd in ("approve", "enable"):
        set_status(args[1], "enabled")
        print(f"{args[1]} → enabled")
    elif cmd == "disable":
        set_status(args[1], "disabled")
        print(f"{args[1]} → disabled")
    elif cmd == "reject":
        set_status(args[1], "rejected")
        print(f"{args[1]} → rejected")
    elif cmd == "remove":
        remove(args[1])
        print(f"{args[1]} 已移除")
    elif cmd == "check-update":
        print(check_update(args[1]))
    elif cmd == "update":
        print(update(args[1]))
    elif cmd == "rollback":
        print(rollback(args[1]))
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
