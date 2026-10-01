"""Subtitle localization quality-control and channel delivery service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

# 交付渠道及其打包规则：
# - cinema 影院：DCP 风格 XML，只含字幕
# - streaming 流媒体：WebVTT 字幕 + 术语表
# - tv 电视台：SRT 字幕 + 术语表
# artifacts 声明包内容依赖，内容指纹据此判断改动影响哪些渠道。
CHANNELS = ("cinema", "streaming", "tv")
CHANNEL_LABELS = {"cinema": "影院", "streaming": "流媒体", "tv": "电视台"}
PACKAGE_STATUSES = ("pending", "packed", "confirmed", "failed", "invalidated")
DUE_STATUSES = ("pending", "invalidated", "failed")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    hour, rem = divmod(ms, 3_600_000)
    minute, rem = divmod(rem, 60_000)
    second, milli = divmod(rem, 1_000)
    return f"{hour:02d}:{minute:02d}:{second:02d}{sep}{milli:03d}"


def _pack_cinema(cues: list[dict[str, Any]], glossary: list[dict[str, Any]]) -> dict[str, str]:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Subtitles>"]
    for cue in cues:
        parts.append(f'  <Cue index="{cue["cue_index"]}" start="{cue["start_ms"]}" end="{cue["end_ms"]}">')
        parts.append(f"    <Text>{xml_escape(cue['text'])}</Text>")
        parts.append("  </Cue>")
    parts.append("</Subtitles>")
    return {"subtitles.xml": "\n".join(parts) + "\n"}


def _pack_vtt(cues: list[dict[str, Any]], glossary: list[dict[str, Any]]) -> dict[str, str]:
    out = ["WEBVTT", ""]
    for pos, cue in enumerate(cues, 1):
        out.extend([
            str(pos),
            f"{_ts(cue['start_ms'], '.')} --> {_ts(cue['end_ms'], '.')}",
            cue["text"],
            "",
        ])
    return {"subtitles.vtt": "\n".join(out).rstrip("\n") + "\n", "glossary.json": _glossary_json(glossary)}


def _pack_srt(cues: list[dict[str, Any]], glossary: list[dict[str, Any]]) -> dict[str, str]:
    out: list[str] = []
    for pos, cue in enumerate(cues, 1):
        out.extend([
            str(pos),
            f"{_ts(cue['start_ms'], ',')} --> {_ts(cue['end_ms'], ',')}",
            cue["text"],
            "",
        ])
    return {"subtitles.srt": "\n".join(out), "glossary.json": _glossary_json(glossary)}


def _glossary_json(glossary: list[dict[str, Any]]) -> str:
    return json.dumps(glossary, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


CHANNEL_RULES: dict[str, dict[str, Any]] = {
    "cinema": {"label": "影院", "artifacts": ("cues",), "pack": _pack_cinema},
    "streaming": {"label": "流媒体", "artifacts": ("cues", "glossary"), "pack": _pack_vtt},
    "tv": {"label": "电视台", "artifacts": ("cues", "glossary"), "pack": _pack_srt},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        # 渠道打包器可替换，便于模拟某个渠道打包失败/中断后重试。
        self.packers: dict[str, Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, str]]] = {
            channel: CHANNEL_RULES[channel]["pack"] for channel in CHANNELS
        }
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS channel_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL CHECK(channel IN ('cinema','streaming','tv')),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','packed','confirmed','failed','invalidated')),
                    fingerprint TEXT,
                    package_hash TEXT,
                    manifest TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    packed_at TEXT,
                    UNIQUE(delivery_id,channel)
                );
                CREATE TABLE IF NOT EXISTS channel_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL REFERENCES channel_packages(id) ON DELETE CASCADE,
                    receipt_no TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('matched','mismatched')),
                    reported_files TEXT NOT NULL DEFAULT '[]',
                    missing_files TEXT NOT NULL DEFAULT '[]',
                    mismatched_files TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'channel',
                    created_at TEXT NOT NULL,
                    UNIQUE(package_id,receipt_no)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(deliveries)")}
            if "completed_at" not in columns:
                conn.execute("ALTER TABLE deliveries ADD COLUMN completed_at TEXT")
        # 旧的单快照交付没有渠道记录，按现有清单补建。
        self.backfill_legacy_deliveries()

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ------------------------------------------------------------------ content

    @staticmethod
    def _canonical_hash(payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _norm_glossary(rows: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            forbidden = row["forbidden_terms"]
            if isinstance(forbidden, str):
                try:
                    forbidden = json.loads(forbidden)
                except json.JSONDecodeError:
                    forbidden = []
            items.append({
                "source_term": row["source_term"],
                "required_translation": row["required_translation"],
                "forbidden_terms": forbidden,
            })
        return sorted(items, key=lambda item: item["source_term"])

    def _load_cues(self, conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index",
            (version_id,),
        )]

    def _load_glossary(self, conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
        return self._norm_glossary(conn.execute(
            "SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term",
            (project_id,),
        ).fetchall())

    def _snapshot(self, conn: sqlite3.Connection, version: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_id": version["project_id"],
            "version_id": version["id"],
            "language": version["language"],
            "version_no": version["version_no"],
            "cues": self._load_cues(conn, version["id"]),
            "glossary": self._load_glossary(conn, version["project_id"]),
        }

    @staticmethod
    def _channel_fingerprint(channel: str, cues: list[dict[str, Any]], glossary: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        digest.update(channel.encode())
        digest.update(b"|cues:")
        digest.update(json.dumps(cues, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        if "glossary" in CHANNEL_RULES[channel]["artifacts"]:
            digest.update(b"|glossary:")
            digest.update(json.dumps(glossary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        return digest.hexdigest()

    @staticmethod
    def _build_files(packer_output: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        if not isinstance(packer_output, dict) or not packer_output or not all(
            isinstance(name, str) and name and isinstance(content, str) for name, content in packer_output.items()
        ):
            raise DomainError("渠道打包结果不合法")
        files: list[dict[str, Any]] = []
        for name in sorted(packer_output):
            data = packer_output[name].encode("utf-8")
            files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        package_hash = Database._canonical_hash(files)
        return files, package_hash

    def _invalidate_packages(self, conn: sqlite3.Connection, *, artifact: str,
                             project_id: int | None = None, version_id: int | None = None) -> list[int]:
        """内容改动后只把真正依赖该内容、且尚未终结的渠道包置为 invalidated。"""
        sql = (
            "SELECT cp.id,cp.channel,cp.status FROM channel_packages cp "
            "JOIN deliveries d ON d.id=cp.delivery_id "
            "JOIN versions v ON v.id=d.version_id "
            "WHERE v.status NOT IN ('delivered','superseded')"
        )
        params: list[Any] = []
        if version_id is not None:
            sql += " AND v.id=?"
            params.append(version_id)
        if project_id is not None:
            sql += " AND v.project_id=?"
            params.append(project_id)
        affected: list[int] = []
        for row in conn.execute(sql, params):
            if artifact not in CHANNEL_RULES[row["channel"]]["artifacts"]:
                continue
            if row["status"] in ("packed", "confirmed", "failed"):
                conn.execute(
                    "UPDATE channel_packages SET status='invalidated',updated_at=? WHERE id=?",
                    (utcnow(), row["id"]),
                )
                self._audit(conn, "system", "package.invalidated", "channel_package", row["id"],
                            {"channel": row["channel"], "reason": artifact})
                affected.append(int(row["id"]))
        return affected

    # ------------------------------------------------------------------ project

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_sha, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            # 术语改动只让带术语表的渠道包（流媒体、电视台）失效。
            invalidated = self._invalidate_packages(conn, artifact="glossary", project_id=project_id)
            self._audit(conn, actor, "glossary.saved", "project", project_id,
                        {"source_term": source_term, "invalidated_packages": invalidated})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (int(parent_id), project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
            )
            self._audit(conn, actor, "version.created", "version", cur.lastrowid, {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            # 字幕改动让该版本所有依赖字幕的渠道包失效。
            invalidated = self._invalidate_packages(conn, artifact="cues", version_id=version_id)
            self._audit(conn, actor, "cue.saved", "version", version_id,
                        {"cue_id": saved_id, "revision": revision, "invalidated_packages": invalidated})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {"version_revision": revision}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    # ------------------------------------------------------------------ workflow

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'", (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """锁定版本并按渠道规则生成字幕包与清单；重复请求不产生第二份包。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有已批准版本可以锁定", 409)
            delivery = conn.execute("SELECT * FROM deliveries WHERE version_id=?", (version_id,)).fetchone()
            if delivery is None:
                snapshot = self._snapshot(conn, version)
                snapshot_hash = self._canonical_hash(snapshot)
                previous = conn.execute(
                    "SELECT d.id,v.status FROM deliveries d JOIN versions v ON v.id=d.version_id "
                    "WHERE v.project_id=? AND v.language=? AND d.version_id<>? ORDER BY d.id DESC LIMIT 1",
                    (version["project_id"], version["language"], version_id),
                ).fetchone()
                supersedes_id = None
                if previous is not None and previous["status"] == "delivered":
                    supersedes_id = previous["id"]
                    conn.execute(
                        "UPDATE versions SET status='superseded',updated_at=? WHERE id="
                        "(SELECT version_id FROM deliveries WHERE id=?)",
                        (utcnow(), previous["id"]),
                    )
                cur = conn.execute(
                    "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (version_id, supersedes_id, snapshot_hash,
                     json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, utcnow()),
                )
                delivery_id = int(cur.lastrowid)
                for channel in CHANNELS:
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_packages(delivery_id,channel,status,created_at,updated_at)"
                        " VALUES(?,?,?,?,?)",
                        (delivery_id, channel, "pending", utcnow(), utcnow()),
                    )
                conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
                self._audit(conn, actor, "version.locked", "version", version_id, {"delivery_id": delivery_id})
            else:
                delivery_id = int(delivery["id"])
                # 重复锁定：确保三个渠道任务都在，不生成第二份包。
                for channel in CHANNELS:
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_packages(delivery_id,channel,status,created_at,updated_at)"
                        " VALUES(?,?,?,?,?)",
                        (delivery_id, channel, "pending", utcnow(), utcnow()),
                    )
        failures = self._pack_due(delivery_id)
        if failures:
            names = "、".join(CHANNEL_LABELS[c] for c in CHANNELS if c in failures)
            raise DomainError(f"渠道打包未完成（{names}），已完成渠道保留，可重试", 502)
        return self.get_delivery(delivery_id)

    def unlock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """交付完成前负责人可解锁返修；已打出的包按内容指纹决定是否失效。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以解锁版本", 403)
            if version["status"] != "locked":
                raise DomainError("只有锁定但尚未完成交付的版本可以解锁", 409)
            conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.unlocked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """继续未完成的渠道打包（断点续打，幂等）；版本是否已交付由回执对账决定。"""
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以重试交付", 403)
            delivery = conn.execute("SELECT * FROM deliveries WHERE version_id=?", (version_id,)).fetchone()
            if delivery is None:
                if version["status"] == "locked":
                    raise DomainError("交付任务缺失，请重新锁定版本", 409)
                raise DomainError("请先锁定版本再执行交付", 409)
            delivery_id = int(delivery["id"])
            if version["status"] == "delivered":
                return self.get_delivery(delivery_id)
            if version["status"] != "locked":
                raise DomainError("版本未锁定，无法继续渠道打包", 409)
        failures = self._pack_due(delivery_id)
        if failures:
            names = "、".join(CHANNEL_LABELS[c] for c in CHANNELS if c in failures)
            raise DomainError(f"渠道打包仍未完成（{names}），已完成渠道保留，可继续重试", 502)
        return self.get_delivery(delivery_id)

    def _pack_due(self, delivery_id: int) -> dict[str, str]:
        """逐个渠道独立提交：中断后已完成渠道保留，只续打 pending/invalidated/failed。

        某渠道失败即中断本轮，排在后面的渠道保持 pending，等待下一次重试。
        """
        failures: dict[str, str] = {}
        for channel in CHANNELS:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                package = conn.execute(
                    "SELECT cp.*,v.id AS version_id,v.project_id FROM channel_packages cp "
                    "JOIN deliveries d ON d.id=cp.delivery_id JOIN versions v ON v.id=d.version_id "
                    "WHERE d.id=? AND cp.channel=?",
                    (delivery_id, channel),
                ).fetchone()
                if package is None or package["status"] not in DUE_STATUSES:
                    continue
                cues = self._load_cues(conn, int(package["version_id"]))
                glossary = self._load_glossary(conn, int(package["project_id"]))
                fingerprint = self._channel_fingerprint(channel, cues, glossary)
                try:
                    files, package_hash = self._build_files(self.packers[channel](cues, glossary))
                except Exception as exc:  # 打包器失败：本渠道置 failed，本轮中断，其它渠道保留现状
                    conn.execute(
                        "UPDATE channel_packages SET status='failed',attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",
                        (str(exc), utcnow(), package["id"]),
                    )
                    self._audit(conn, "system", "package.failed", "channel_package", int(package["id"]),
                                {"channel": channel, "error": str(exc)})
                    failures[channel] = str(exc)
                    break
                conn.execute(
                    "UPDATE channel_packages SET status='packed',fingerprint=?,package_hash=?,manifest=?,"
                    "last_error='',updated_at=?,packed_at=? WHERE id=?",
                    (fingerprint, package_hash,
                     json.dumps({"channel": channel, "package_hash": package_hash, "files": files},
                                ensure_ascii=False, sort_keys=True),
                     utcnow(), utcnow(), package["id"]),
                )
                self._audit(conn, "system", "package.packed", "channel_package", int(package["id"]),
                            {"channel": channel, "package_hash": package_hash, "files": [f["path"] for f in files]})
        self._refresh_snapshot(delivery_id)
        return failures

    def _refresh_snapshot(self, delivery_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT d.id AS delivery_id,v.id AS version_id,v.project_id,v.language,v.version_no "
                "FROM deliveries d JOIN versions v ON v.id=d.version_id WHERE d.id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            snapshot = {
                "project_id": row["project_id"],
                "version_id": row["version_id"],
                "language": row["language"],
                "version_no": row["version_no"],
                "cues": self._load_cues(conn, int(row["version_id"])),
                "glossary": self._load_glossary(conn, int(row["project_id"])),
            }
            conn.execute(
                "UPDATE deliveries SET snapshot_hash=?,manifest=? WHERE id=?",
                (self._canonical_hash(snapshot), json.dumps(snapshot, ensure_ascii=False, sort_keys=True), delivery_id),
            )

    # ------------------------------------------------------------------ receipts

    def record_receipt(self, delivery_id: int, channel: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        """渠道回执按文件校验值对账；重复回执只记一次；全部渠道对得上才置 delivered。"""
        if channel not in CHANNEL_RULES:
            raise DomainError("未知交付渠道")
        receipt_no = str(payload.get("receipt_no", "")).strip()
        reported = payload.get("files", [])
        if not receipt_no or not isinstance(reported, list):
            raise DomainError("回执号和文件清单不完整")
        reported_map: dict[str, str] = {}
        for item in reported:
            if not isinstance(item, str) and isinstance(item, dict):
                path = str(item.get("path", "")).strip()
                sha = str(item.get("sha256", "")).strip().lower()
            else:
                raise DomainError("回执文件项必须包含 path 和 sha256")
            if not path or len(sha) != 64:
                raise DomainError("回执文件路径或校验值不合法")
            reported_map[path] = sha
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            package = conn.execute(
                "SELECT cp.*,v.status AS version_status FROM channel_packages cp "
                "JOIN deliveries d ON d.id=cp.delivery_id JOIN versions v ON v.id=d.version_id "
                "WHERE cp.delivery_id=? AND cp.channel=?",
                (delivery_id, channel),
            ).fetchone()
            if package is None:
                raise DomainError("渠道包不存在", 404)
            duplicate = conn.execute(
                "SELECT * FROM channel_receipts WHERE package_id=? AND receipt_no=?",
                (package["id"], receipt_no),
            ).fetchone()
            if duplicate is not None:
                # 重复回执只记一次，原样返回首次结果，不改变任何状态。
                return self._receipt_result(duplicate, delivery_id, channel, package["version_status"], duplicated=True)
            if package["version_status"] != "locked":
                raise DomainError("版本不在交付中，暂不接收渠道回执", 409)
            if package["status"] not in {"packed", "confirmed"}:
                raise DomainError("该渠道包尚未完成打包，无法对账", 409)
            expected = {f["path"]: f["sha256"] for f in json.loads(package["manifest"]).get("files", [])}
            missing = sorted(path for path in expected if path not in reported_map)
            mismatched = [
                {"path": path, "expected": expected[path], "reported": reported_map[path]}
                for path in sorted(expected)
                if path in reported_map and reported_map[path] != expected[path]
            ]
            reconciled = not missing and not mismatched
            status = "matched" if reconciled else "mismatched"
            cur = conn.execute(
                "INSERT INTO channel_receipts(package_id,receipt_no,status,reported_files,missing_files,"
                "mismatched_files,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (package["id"], receipt_no, status, json.dumps(reported, ensure_ascii=False),
                 json.dumps(missing, ensure_ascii=False), json.dumps(mismatched, ensure_ascii=False),
                 "channel", utcnow()),
            )
            if reconciled:
                conn.execute("UPDATE channel_packages SET status='confirmed',updated_at=? WHERE id=?",
                             (utcnow(), package["id"]))
            elif package["status"] == "confirmed":
                # 该渠道此前已确认，但新回执对账不符：退回待确认，避免误判交付完成。
                conn.execute("UPDATE channel_packages SET status='packed',updated_at=? WHERE id=?",
                             (utcnow(), package["id"]))
            self._audit(conn, actor, f"receipt.{status}", "channel_package", int(package["id"]),
                        {"delivery_id": delivery_id, "channel": channel, "receipt_no": receipt_no})
            version_status = package["version_status"]
            if reconciled:
                version_status = self._maybe_complete(conn, delivery_id)
            receipt = conn.execute("SELECT * FROM channel_receipts WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._receipt_result(receipt, delivery_id, channel, version_status, duplicated=False)

    def _maybe_complete(self, conn: sqlite3.Connection, delivery_id: int) -> str:
        counts = conn.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) confirmed "
            "FROM channel_packages WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        version_row = conn.execute(
            "SELECT v.id,v.status FROM versions v JOIN deliveries d ON d.version_id=v.id WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
        if int(counts["total"]) == len(CHANNELS) and int(counts["confirmed"]) == len(CHANNELS) \
                and version_row["status"] == "locked":
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?",
                         (utcnow(), version_row["id"]))
            conn.execute("UPDATE deliveries SET completed_at=? WHERE id=?", (utcnow(), delivery_id))
            self._audit(conn, "system", "version.delivered", "version", int(version_row["id"]),
                        {"delivery_id": delivery_id})
            return "delivered"
        return str(version_row["status"])

    @staticmethod
    def _receipt_result(receipt: sqlite3.Row, delivery_id: int, channel: str,
                        version_status: str, *, duplicated: bool) -> dict[str, Any]:
        return {
            "id": int(receipt["id"]),
            "delivery_id": delivery_id,
            "channel": channel,
            "receipt_no": receipt["receipt_no"],
            "reconciled": receipt["status"] == "matched",
            "status": receipt["status"],
            "missing_files": json.loads(receipt["missing_files"]),
            "mismatched_files": json.loads(receipt["mismatched_files"]),
            "duplicated": duplicated,
            "version_status": version_status,
        }

    # ------------------------------------------------------------------ backfill

    def backfill_legacy_deliveries(self) -> int:
        """旧交付只有单快照清单：按各渠道规则补建渠道记录，并补记 legacy 回执。"""
        backfilled = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM deliveries ORDER BY id").fetchall()
            for delivery in rows:
                exists = conn.execute(
                    "SELECT COUNT(*) n FROM channel_packages WHERE delivery_id=?", (delivery["id"],)
                ).fetchone()["n"]
                if exists:
                    continue
                try:
                    manifest = json.loads(delivery["manifest"])
                except json.JSONDecodeError:
                    manifest = {}
                cues = manifest.get("cues", [])
                glossary = self._norm_glossary([
                    {"source_term": g.get("source_term", ""),
                     "required_translation": g.get("required_translation", ""),
                     "forbidden_terms": g.get("forbidden_terms", "[]")}
                    for g in manifest.get("glossary", [])
                ])
                for channel in CHANNELS:
                    files, package_hash = self._build_files(self.packers[channel](cues, glossary))
                    fingerprint = self._channel_fingerprint(channel, cues, glossary)
                    cur = conn.execute(
                        "INSERT INTO channel_packages(delivery_id,channel,status,fingerprint,package_hash,manifest,"
                        "created_at,updated_at,packed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (delivery["id"], channel, "confirmed", fingerprint, package_hash,
                         json.dumps({"channel": channel, "package_hash": package_hash, "files": files},
                                    ensure_ascii=False, sort_keys=True),
                         delivery["created_at"], delivery["created_at"], delivery["created_at"]),
                    )
                    receipt_no = f"legacy:{delivery['id']}:{channel}"
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_receipts(package_id,receipt_no,status,reported_files,"
                        "missing_files,mismatched_files,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (cur.lastrowid, receipt_no, "matched",
                         json.dumps([{"path": f["path"], "sha256": f["sha256"]} for f in files], ensure_ascii=False),
                         "[]", "[]", "legacy", delivery["created_at"]),
                    )
                if delivery["completed_at"] is None:
                    conn.execute("UPDATE deliveries SET completed_at=? WHERE id=?",
                                 (delivery["created_at"], delivery["id"]))
                self._audit(conn, "system", "delivery.backfilled", "delivery", int(delivery["id"]),
                            {"channels": list(CHANNELS)})
                backfilled += 1
        return backfilled

    # ------------------------------------------------------------------ queries

    def _delivery_detail(self, conn: sqlite3.Connection, delivery_id: int) -> dict[str, Any]:
        delivery = conn.execute(
            "SELECT d.*,v.status AS version_status,v.language AS language FROM deliveries d "
            "JOIN versions v ON v.id=d.version_id WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
        if delivery is None:
            raise DomainError("交付不存在", 404)
        result = dict(delivery)
        channels: list[dict[str, Any]] = []
        for package in conn.execute(
            "SELECT * FROM channel_packages WHERE delivery_id=? ORDER BY id", (delivery_id,)
        ):
            channel = dict(package)
            channel["label"] = CHANNEL_LABELS[package["channel"]]
            channel["manifest_data"] = json.loads(package["manifest"]) if package["manifest"] else {}
            channel["receipts"] = [dict(r) for r in conn.execute(
                "SELECT id,receipt_no,status,missing_files,mismatched_files,source,created_at "
                "FROM channel_receipts WHERE package_id=? ORDER BY id", (package["id"],)
            )]
            channels.append(channel)
        result["channels"] = channels
        return result

    def get_delivery(self, delivery_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            return self._delivery_detail(conn, delivery_id)

    def get_delivery_for_version(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            delivery = conn.execute("SELECT id FROM deliveries WHERE version_id=?", (version_id,)).fetchone()
            if delivery is None:
                raise DomainError("该版本还没有交付任务", 404)
            return self._delivery_detail(conn, int(delivery["id"]))

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            deliveries = []
            rows = conn.execute(
                "SELECT d.*,v.status AS version_status,v.language AS language FROM deliveries d "
                "JOIN versions v ON v.id=d.version_id ORDER BY d.id DESC"
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["channels"] = []
                for package in conn.execute(
                    "SELECT cp.*,("
                    "SELECT status FROM channel_receipts WHERE package_id=cp.id ORDER BY id DESC LIMIT 1"
                    ") AS last_receipt_status FROM channel_packages cp WHERE cp.delivery_id=? ORDER BY cp.id",
                    (row["id"],),
                ):
                    summary = {
                        "id": package["id"],
                        "channel": package["channel"],
                        "label": CHANNEL_LABELS[package["channel"]],
                        "status": package["status"],
                        "package_hash": package["package_hash"],
                        "attempts": package["attempts"],
                        "last_error": package["last_error"],
                        "last_receipt_status": package["last_receipt_status"],
                    }
                    item["channels"].append(summary)
                deliveries.append(item)
            return deliveries

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "deliveries"]:
                return self._send(self.db.get_delivery(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "delivery":
                return self._send(self.db.get_delivery_for_version(int(parts[2])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.db.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.db.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.db.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.db.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.db.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "unlock", "deliver"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                if parts[3] == "unlock":
                    return self._send(self.db.unlock(version_id, actor, role))
                return self._send(self.db.deliver(version_id, actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.db.review(int(parts[2]), actor, body, role))
            if len(parts) == 6 and parts[:2] == ["api", "deliveries"] and parts[3] == "channels" and parts[5] == "receipt":
                return self._send(self.db.record_receipt(int(parts[2]), parts[4], actor, body))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与渠道交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
