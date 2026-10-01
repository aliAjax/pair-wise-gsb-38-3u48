"""Subtitle localization quality-control and channel delivery service."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

# Delivery channels and their packaging rules. A locked title must reach all
# three; each channel gets its own subtitle render, glossary slice and manifest.
CHANNELS = ("cinema", "streaming", "tv")
CHANNEL_RULES: dict[str, dict[str, Any]] = {
    "cinema": {
        "label": "影院",
        "format": "dcp-xml",
        "subtitle_file": "subtitle.xml",
        "terms_file": "glossary.json",
        "include_sdh": False,  # DCP interop package excludes SDH cues
    },
    "streaming": {
        "label": "流媒体",
        "format": "webvtt",
        "subtitle_file": "subtitle.vtt",
        "terms_file": "glossary.json",
        "include_sdh": True,
    },
    "tv": {
        "label": "电视台",
        "format": "ebu-stl",
        "subtitle_file": "subtitle.stl",
        "terms_file": "glossary.json",
        "include_sdh": True,
    },
}
MANIFEST_FILE = "manifest.json"
# Package lifecycle: pending -> packaged / failed; packaged -> stale when the
# locked inputs change; stale/failed -> packaged again on retry.
PACKAGE_STATUSES = ("pending", "packaged", "failed", "stale")
DUE_STATUSES = ("pending", "failed", "stale")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fmt_timecode(ms: int) -> str:
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:03d}"


def render_vtt(cues: list[dict[str, Any]]) -> bytes:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{fmt_timecode(cue['start_ms'])} --> {fmt_timecode(cue['end_ms'])}")
        lines.append(str(cue["text"]))
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def render_dcp_xml(cues: list[dict[str, Any]], language: str) -> bytes:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<SubtitleReel language="{html.escape(language, quote=True)}">']
    for cue in cues:
        lines.append(
            '  <Subtitle id="{idx}" start="{start}" end="{end}"><Text>{text}</Text></Subtitle>'.format(
                idx=int(cue["cue_index"]),
                start=fmt_timecode(cue["start_ms"]),
                end=fmt_timecode(cue["end_ms"]),
                text=html.escape(str(cue["text"]), quote=False),
            )
        )
    lines.append("</SubtitleReel>")
    return "\n".join(lines).encode("utf-8")


def render_stl(cues: list[dict[str, Any]], language: str) -> bytes:
    lines = ["# EBU STL compatible subtitle bundle", f"# Language: {language}", ""]
    for cue in cues:
        lines.append(
            f"CN {int(cue['cue_index']):04d} {fmt_timecode(cue['start_ms'])} --> {fmt_timecode(cue['end_ms'])}"
        )
        lines.append(str(cue["text"]))
        lines.append("")
    return "\n".join(lines).encode("utf-8")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        # Test hook: packaging for these channels always fails until removed.
        self.build_failures: set[str] = set()
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
                    sdh INTEGER NOT NULL DEFAULT 0,
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
                    channels TEXT,
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
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL CHECK(channel IN ('cinema','streaming','tv')),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','packaged','failed','stale')),
                    revision INTEGER NOT NULL DEFAULT 0,
                    package_hash TEXT,
                    manifest_sha TEXT,
                    manifest TEXT,
                    error TEXT,
                    legacy INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,channel)
                );
                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL REFERENCES channel_packages(id) ON DELETE CASCADE,
                    receipt_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('matched','mismatched')),
                    reported_files TEXT NOT NULL DEFAULT '[]',
                    missing TEXT NOT NULL DEFAULT '[]',
                    mismatches TEXT NOT NULL DEFAULT '[]',
                    unexpected TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    legacy INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(package_id,receipt_id)
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
            self._migrate_columns(conn)
        self._backfill_legacy_deliveries()

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        def columns(table: str) -> set[str]:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

        if "sdh" not in columns("cues"):
            conn.execute("ALTER TABLE cues ADD COLUMN sdh INTEGER NOT NULL DEFAULT 0")
        if "channels" not in columns("glossaries"):
            conn.execute("ALTER TABLE glossaries ADD COLUMN channels TEXT")

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ------------------------------------------------------------------ projects

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
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
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        channels = self._parse_term_channels(payload.get("channels"))
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
            old = conn.execute(
                "SELECT channels FROM glossaries WHERE project_id=? AND source_term=?",
                (project_id, source_term),
            ).fetchone()
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,channels,notes,created_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,
                   channels=excluded.channels,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False),
                 json.dumps(channels, ensure_ascii=False) if channels is not None else None,
                 str(payload.get("notes", "")), utcnow()),
            )
            affected = set(channels or CHANNELS)
            if old and old["channels"]:
                affected |= set(json.loads(old["channels"]))
            # Moving a term between channels changes every involved package,
            # including the channel the term was removed from.
            stale = self._invalidate_packages(
                conn, project_id=project_id, channels=affected & set(CHANNELS), reason="glossary",
                source_term=source_term,
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id,
                        {"source_term": source_term, "channels": channels, "invalidated": stale})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required,
                "forbidden_terms": forbidden, "channels": channels}

    @staticmethod
    def _parse_term_channels(value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list) or any(c not in CHANNELS for c in value):
            raise DomainError(f"渠道必须是 {list(CHANNELS)} 的子集")
        # Deduplicate while preserving canonical order. None means "all channels".
        return [c for c in CHANNELS if c in value] or None

    # ----------------------------------------------------------------- versions

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
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms,p.media_name,p.media_sha256 FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
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

    def _load_cues(self, conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT cue_index,start_ms,end_ms,text,sdh FROM cues WHERE version_id=? ORDER BY cue_index",
            (version_id,))]

    def _load_terms(self, conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
        terms = []
        for r in conn.execute(
            "SELECT source_term,required_translation,forbidden_terms,channels FROM glossaries WHERE project_id=? ORDER BY source_term",
            (project_id,)):
            channels = json.loads(r["channels"]) if r["channels"] else None
            terms.append({"source_term": r["source_term"], "required_translation": r["required_translation"],
                          "forbidden_terms": json.loads(r["forbidden_terms"]), "channels": channels})
        return terms

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
            sdh = 1 if bool(payload.get("sdh", False)) else 0
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
                conn.execute(
                    "UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,sdh=?,updated_by=?,updated_at=? WHERE id=?",
                    (cue_index, start_ms, end_ms, text, sdh, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,sdh,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, cue_index, start_ms, end_ms, text, sdh, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            # Streaming/TV carry every cue; cinema only sees the cue when it is
            # part of the DCP render either before or after the edit.
            affected = {"streaming", "tv"}
            if sdh == 0 or (existing is not None and existing["sdh"] == 0):
                affected.add("cinema")
            stale = self._invalidate_packages(
                conn, project_id=int(version["project_id"]), channels=affected, reason="cue",
                version_id=version_id, cue_id=saved_id)
            self._audit(conn, actor, "cue.saved", "version", version_id,
                        {"cue_id": saved_id, "revision": revision, "invalidated": stale})
            saved = dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone())
        return saved | {"version_revision": revision}

    def _invalidate_packages(self, conn: sqlite3.Connection, *, project_id: int, channels: set[str],
                             reason: str, version_id: int | None = None, **detail: Any) -> list[str]:
        """Mark already-built packages stale. Only packaged rows are affected:
        pending/failed rows pick up the new inputs on their next build."""
        if not channels:
            return []
        params: list[Any] = list(channels)
        sql = """SELECT cp.id, cp.version_id, cp.channel FROM channel_packages cp
                 JOIN versions v ON v.id=cp.version_id
                 WHERE cp.channel IN (%s) AND cp.status='packaged'
                   AND v.status!='delivered'""" % ",".join("?" for _ in channels)
        if version_id is not None:
            sql += " AND cp.version_id=?"
            params.append(version_id)
        else:
            sql += " AND v.project_id=?"
            params.append(project_id)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        conn.execute(
            "UPDATE channel_packages SET status='stale',error=NULL,updated_at=? WHERE id IN (%s)"
            % ",".join("?" for _ in ids),
            [utcnow(), *ids],
        )
        # Old receipts only prove the previous bytes; retire them so the new
        # render has to be reconciled again before delivery.
        conn.execute(
            "UPDATE delivery_receipts SET active=0,note=? WHERE active=1 AND package_id IN (%s)"
            % ",".join("?" for _ in ids),
            [f"superseded by {reason} change", *ids],
        )
        for r in rows:
            self._audit(conn, "system", "package.invalidated", "channel_package", r["id"],
                        {"reason": reason, "channel": r["channel"], **detail})
        return sorted({r["channel"] for r in rows})

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

    # ----------------------------------------------------------------- locking

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            # One task row per channel; duplicate lock attempts never create a
            # second package for the same (version, channel).
            self._ensure_tasks(conn, version_id)
            self._audit(conn, actor, "version.locked", "version", version_id, {"channels": list(CHANNELS)})
        # Build every channel independently so one channel's packaging failure
        # cannot roll back or half-write the other channels.
        self._build_due(version_id, channels=CHANNELS)
        return self.delivery_status(version_id)

    def unlock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "locked":
                raise DomainError("只有已锁定且未交付的版本可以解锁修改", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以解锁版本", 403)
            conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.unlocked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    @staticmethod
    def _ensure_tasks(conn: sqlite3.Connection, version_id: int) -> None:
        now = utcnow()
        for channel in CHANNELS:
            conn.execute(
                "INSERT OR IGNORE INTO channel_packages(version_id,channel,status,created_at,updated_at) VALUES(?,?, 'pending',?,?)",
                (version_id, channel, now, now),
            )

    # ------------------------------------------------------------- packaging

    def build_packages(self, version_id: int, actor: str, payload: dict[str, Any] | None = None,
                       role: str = "viewer") -> dict[str, Any]:
        """Idempotent (re)try: builds only pending/failed/stale channels and
        leaves every already-packaged channel byte-for-byte untouched."""
        payload = payload or {}
        requested = payload.get("channels")
        if requested is not None:
            if not isinstance(requested, list) or any(c not in CHANNELS for c in requested):
                raise DomainError(f"渠道必须是 {list(CHANNELS)} 的子集")
            channels = tuple(dict.fromkeys(requested))
        else:
            channels = CHANNELS
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以生成或重试渠道包", 403)
            if version["status"] != "locked":
                raise DomainError("只有已锁定版本可以生成渠道包", 409)
            self._ensure_tasks(conn, version_id)
        self._build_due(version_id, channels=channels)
        return self.delivery_status(version_id)

    def _build_due(self, version_id: int, *, channels: tuple[str, ...]) -> None:
        for channel in channels:
            self._try_build(version_id, channel)

    def _try_build(self, version_id: int, channel: str) -> None:
        """Build one channel in its own transaction. Any failure is recorded on
        that row only; sibling channels and earlier successes stay committed."""
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM channel_packages WHERE version_id=? AND channel=?",
                    (version_id, channel),
                ).fetchone()
                if row is None or row["status"] == "packaged":
                    return
                if channel in self.build_failures:
                    raise DomainError(f"{CHANNEL_RULES[channel]['label']}渠道打包失败：打包管线暂时不可用")
                version = self._version(conn, version_id)
                cues = self._load_cues(conn, int(version["project_id"]))
                terms = self._load_terms(conn, int(version["project_id"]))
                pkg = self._compose_package(
                    project={"media_name": version["media_name"], "media_sha256": version["media_sha256"]},
                    version={"id": version_id, "language": version["language"],
                             "version_no": version["version_no"], "revision": version["revision"],
                             "project_id": int(version["project_id"])},
                    cues=cues, terms=terms, channel=channel,
                )
                conn.execute(
                    """UPDATE channel_packages SET status='packaged',revision=?,package_hash=?,
                       manifest_sha=?,manifest=?,error=NULL,updated_at=? WHERE id=?""",
                    (pkg["revision"], pkg["package_hash"], pkg["manifest_sha"],
                     json.dumps(pkg["manifest"], ensure_ascii=False, sort_keys=True),
                     utcnow(), row["id"]),
                )
                self._audit(conn, "system", "package.built", "channel_package", row["id"],
                            {"channel": channel, "package_hash": pkg["package_hash"],
                             "previous_status": row["status"]})
        except Exception as exc:  # packaging pipeline failure -> failed row, not half package
            message = str(exc)
            with self.connect() as conn:
                conn.execute(
                    "UPDATE channel_packages SET status='failed',error=?,updated_at=? WHERE version_id=? AND channel=?",
                    (message, utcnow(), version_id, channel),
                )
                self._audit(conn, "system", "package.failed", "channel_package", None,
                            {"version_id": version_id, "channel": channel, "error": message})

    @staticmethod
    def _terms_for_channel(terms: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
        selected = [
            {"source_term": t["source_term"], "required_translation": t["required_translation"],
             "forbidden_terms": t["forbidden_terms"]}
            for t in terms if not t["channels"] or channel in t["channels"]
        ]
        return sorted(selected, key=lambda t: t["source_term"])

    def _compose_package(self, *, project: dict[str, Any], version: dict[str, Any],
                         cues: list[dict[str, Any]], terms: list[dict[str, Any]],
                         channel: str) -> dict[str, Any]:
        rule = CHANNEL_RULES[channel]
        channel_cues = [c for c in cues if rule["include_sdh"] or not c.get("sdh")]
        channel_terms = self._terms_for_channel(terms, channel)
        language = version["language"]
        if rule["format"] == "webvtt":
            subtitle = render_vtt(channel_cues)
        elif rule["format"] == "dcp-xml":
            subtitle = render_dcp_xml(channel_cues, language)
        else:
            subtitle = render_stl(channel_cues, language)
        terms_blob = canon(channel_terms).encode("utf-8")
        files = [
            {"path": rule["subtitle_file"], "bytes": subtitle},
            {"path": rule["terms_file"], "bytes": terms_blob},
        ]
        entries = [{"path": f["path"], "sha256": sha256_bytes(f["bytes"]), "bytes": len(f["bytes"])}
                   for f in files]
        package_hash = sha256_bytes(canon(entries).encode("utf-8"))
        manifest = {
            "schema": "channel-package/v1",
            "project_id": version.get("project_id"),
            "version_id": version["id"],
            "language": language,
            "version_no": version["version_no"],
            "channel": channel,
            "channel_label": rule["label"],
            "format": rule["format"],
            "revision": version["revision"],
            "media": {"name": project["media_name"], "sha256": project["media_sha256"]},
            "cue_count": len(channel_cues),
            "includes_sdh": bool(rule["include_sdh"]),
            "glossary_sha256": sha256_bytes(terms_blob),
            "files": entries,
            "package_hash": package_hash,
        }
        manifest_bytes = canon(manifest).encode("utf-8")
        return {"revision": version["revision"], "package_hash": package_hash,
                "manifest_sha": sha256_bytes(manifest_bytes), "manifest": manifest}

    # -------------------------------------------------------------- receipts

    def add_receipt(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        channel = str(payload.get("channel", "")).strip()
        receipt_id = str(payload.get("receipt_id", "")).strip()
        if channel not in CHANNELS:
            raise DomainError(f"渠道必须是 {list(CHANNELS)} 之一")
        if not receipt_id:
            raise DomainError("渠道回执编号不能为空")
        reported = payload.get("files", [])
        if not isinstance(reported, list):
            raise DomainError("回执文件清单必须是数组")
        normalized: dict[str, str] = {}
        for item in reported:
            if not isinstance(item, dict) or "path" not in item or "sha256" not in item:
                raise DomainError("每个回执文件需要 path 和 sha256")
            normalized[str(item["path"])] = str(item["sha256"]).strip().lower()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以登记渠道回执", 403)
            pkg = conn.execute(
                "SELECT * FROM channel_packages WHERE version_id=? AND channel=?",
                (version_id, channel),
            ).fetchone()
            if not pkg:
                raise DomainError(f"{CHANNEL_RULES[channel]['label']}渠道包尚未生成", 409)
            if pkg["status"] != "packaged":
                raise DomainError(
                    f"{CHANNEL_RULES[channel]['label']}渠道包状态为 {pkg['status']}，暂不能对账", 409)
            duplicate = conn.execute(
                "SELECT * FROM delivery_receipts WHERE package_id=? AND receipt_id=?",
                (pkg["id"], receipt_id),
            ).fetchone()
            # 重复回执只记一次：相同 (包, 回执号) 直接返回原记录。
            if duplicate:
                return self._receipt_payload(conn, duplicate, deduplicated=True)
            manifest = json.loads(pkg["manifest"])
            required = {f["path"]: f["sha256"] for f in manifest["files"]}
            reported_paths = {p for p in normalized if p != MANIFEST_FILE}
            missing = sorted(set(required) - reported_paths)
            mismatches = sorted(
                p for p in required.keys() & reported_paths if normalized[p] != required[p])
            unexpected = sorted(p for p in reported_paths - set(required))
            status = "matched" if not (missing or mismatches or unexpected) else "mismatched"
            cur = conn.execute(
                """INSERT INTO delivery_receipts(package_id,receipt_id,status,reported_files,missing,
                   mismatches,unexpected,note,active,legacy,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,1,0,?,?)""",
                (pkg["id"], receipt_id, status, json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                 json.dumps(missing), json.dumps(mismatches), json.dumps(unexpected),
                 str(payload.get("note", "")), actor, utcnow()),
            )
            self._audit(conn, actor, f"receipt.{status}", "channel_package", pkg["id"],
                        {"receipt_id": receipt_id, "missing": missing, "mismatches": mismatches,
                         "unexpected": unexpected})
            row = conn.execute("SELECT * FROM delivery_receipts WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._receipt_payload(conn, row)

    @staticmethod
    def _receipt_payload(conn: sqlite3.Connection, row: sqlite3.Row, deduplicated: bool = False) -> dict[str, Any]:
        pkg = conn.execute("SELECT channel,version_id FROM channel_packages WHERE id=?", (row["package_id"],)).fetchone()
        return {
            "id": row["id"], "version_id": pkg["version_id"], "channel": pkg["channel"],
            "receipt_id": row["receipt_id"], "status": row["status"], "active": bool(row["active"]),
            "legacy": bool(row["legacy"]), "deduplicated": deduplicated,
            "reported_files": json.loads(row["reported_files"]),
            "missing": json.loads(row["missing"]), "mismatches": json.loads(row["mismatches"]),
            "unexpected": json.loads(row["unexpected"]), "note": row["note"],
            "created_by": row["created_by"], "created_at": row["created_at"],
        }

    # -------------------------------------------------------------- finalize

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以确认交付", 403)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            if version["status"] != "locked":
                raise DomainError("只有已锁定版本可以确认交付", 409)
            status = self._delivery_status(conn, version_id)
            blockers: list[dict[str, Any]] = []
            for ch in status["channels"]:
                if ch["status"] != "packaged":
                    blockers.append({"channel": ch["channel"], "reason": f"渠道包状态为 {ch['status']}"})
                elif not ch["reconciled"]:
                    latest = ch["latest_receipt"]
                    if latest is None:
                        blockers.append({"channel": ch["channel"], "reason": "缺少渠道回执"})
                    else:
                        blockers.append({"channel": ch["channel"], "reason": "回执对账未通过",
                                         "missing": latest["missing"], "mismatches": latest["mismatches"],
                                         "unexpected": latest["unexpected"]})
            if blockers:
                raise DomainError(json.dumps(
                    {"message": "仍有渠道未完成对账，版本不能标记为已交付", "blockers": blockers},
                    ensure_ascii=False), 409)
            summary = {
                ch["channel"]: {"package_hash": ch["package_hash"], "manifest_sha": ch["manifest_sha"],
                                "files": ch["files"], "receipt_id": ch["latest_receipt"]["receipt_id"]}
                for ch in status["channels"]
            }
            manifest = {"schema": "delivery/v2", "version_id": version_id,
                        "language": version["language"], "version_no": version["version_no"],
                        "channels": summary}
            snapshot_hash = sha256_bytes(canon(summary).encode("utf-8"))
            previous = conn.execute(
                """SELECT d.id FROM deliveries d JOIN versions v ON v.id=d.version_id
                   WHERE v.project_id=? AND v.language=? AND d.version_id<>? ORDER BY d.id DESC LIMIT 1""",
                (version["project_id"], version["language"], version_id),
            ).fetchone()
            if previous:
                prior = conn.execute(
                    "SELECT version_id FROM deliveries WHERE id=?", (previous["id"],)).fetchone()
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=?",
                             (utcnow(), prior["version_id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id,
                        {"snapshot_hash": snapshot_hash, "channels": list(CHANNELS)})
            delivery = dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())
        return delivery | {"channels": status["channels"]}

    def _delivery_status(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        channels: list[dict[str, Any]] = []
        all_packaged = True
        all_reconciled = True
        for channel in CHANNELS:
            pkg = conn.execute(
                "SELECT * FROM channel_packages WHERE version_id=? AND channel=?",
                (version_id, channel),
            ).fetchone()
            if pkg is None:
                all_packaged = False
                all_reconciled = False
                channels.append({"channel": channel, "channel_label": CHANNEL_RULES[channel]["label"],
                                 "status": "missing", "reconciled": False, "latest_receipt": None})
                continue
            receipts = [dict(r) for r in conn.execute(
                "SELECT * FROM delivery_receipts WHERE package_id=? ORDER BY id DESC", (pkg["id"],))]
            latest = self._receipt_summary(receipts[0]) if receipts else None
            reconciled = pkg["status"] == "packaged" and any(
                r["status"] == "matched" and r["active"] for r in receipts)
            all_packaged &= pkg["status"] == "packaged"
            all_reconciled &= reconciled
            manifest = json.loads(pkg["manifest"]) if pkg["manifest"] else None
            channels.append({
                "channel": channel, "channel_label": CHANNEL_RULES[channel]["label"],
                "status": pkg["status"], "reconciled": reconciled, "legacy": bool(pkg["legacy"]),
                "revision": pkg["revision"], "package_hash": pkg["package_hash"],
                "manifest_sha": pkg["manifest_sha"], "error": pkg["error"],
                "cue_count": manifest["cue_count"] if manifest else None,
                "files": manifest["files"] if manifest else [],
                "latest_receipt": latest,
                "receipt_count": len(receipts),
                "created_at": pkg["created_at"], "updated_at": pkg["updated_at"],
            })
        delivered = bool(conn.execute(
            "SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone())
        return {"version_id": version_id, "status": version["status"] if version else None,
                "channels": channels, "all_packaged": all_packaged, "all_reconciled": all_reconciled,
                "delivered": delivered}

    @staticmethod
    def _receipt_summary(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "receipt_id": row["receipt_id"], "status": row["status"],
                "active": bool(row["active"]), "legacy": bool(row["legacy"]),
                "missing": json.loads(row["missing"]), "mismatches": json.loads(row["mismatches"]),
                "unexpected": json.loads(row["unexpected"]), "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def delivery_status(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM versions WHERE id=?", (version_id,)).fetchone():
                raise DomainError("字幕版本不存在", 404)
            return self._delivery_status(conn, version_id)

    # ------------------------------------------------- legacy delivery backfill

    def _backfill_legacy_deliveries(self) -> int:
        """Older deliveries predate channel tasks: rebuild per-channel package
        rows and reconciled receipts from the manifest stored at delivery time."""
        backfilled = 0
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT d.*, v.language,v.version_no,v.project_id,p.media_name,p.media_sha256
                   FROM deliveries d JOIN versions v ON v.id=d.version_id
                   JOIN projects p ON p.id=v.project_id
                   WHERE NOT EXISTS (SELECT 1 FROM channel_packages cp WHERE cp.version_id=d.version_id)"""
            ).fetchall()
            for delivery in rows:
                manifest = json.loads(delivery["manifest"])
                cues = manifest.get("cues")
                if cues is None:  # already a v2 aggregate manifest; nothing to derive
                    continue
                terms = []
                for g in manifest.get("glossary", []):
                    forbidden = g["forbidden_terms"]
                    if isinstance(forbidden, str):
                        forbidden = json.loads(forbidden)
                    terms.append({"source_term": g["source_term"],
                                  "required_translation": g["required_translation"],
                                  "forbidden_terms": forbidden, "channels": None})
                now = utcnow()
                for channel in CHANNELS:
                    pkg = self._compose_package(
                        project={"media_name": delivery["media_name"],
                                 "media_sha256": delivery["media_sha256"]},
                        version={"id": delivery["version_id"], "language": delivery["language"],
                                 "version_no": delivery["version_no"], "revision": 0,
                                 "project_id": delivery["project_id"]},
                        cues=cues, terms=terms, channel=channel)
                    cur = conn.execute(
                        """INSERT INTO channel_packages(version_id,channel,status,revision,package_hash,
                           manifest_sha,manifest,error,legacy,created_at,updated_at)
                           VALUES(?,?,'packaged',?,?,?,?,NULL,1,?,?)""",
                        (delivery["version_id"], channel, pkg["revision"], pkg["package_hash"],
                         pkg["manifest_sha"],
                         json.dumps(pkg["manifest"], ensure_ascii=False, sort_keys=True), now, now))
                    conn.execute(
                        """INSERT INTO delivery_receipts(package_id,receipt_id,status,reported_files,
                           missing,mismatches,unexpected,note,active,legacy,created_by,created_at)
                           VALUES(?,?,'matched',?,'[]','[]','[]','legacy delivery reconciled from manifest',
                                  1,1,?,?)""",
                        (cur.lastrowid, f"legacy:delivery-{delivery['id']}",
                         json.dumps({f["path"]: f["sha256"] for f in pkg["manifest"]["files"]},
                                    ensure_ascii=False, sort_keys=True),
                         delivery["delivered_by"], now))
                    self._audit(conn, "system", "package.backfilled", "channel_package", cur.lastrowid,
                                {"delivery_id": delivery["id"], "channel": channel})
                backfilled += 1
        return backfilled

    # ----------------------------------------------------------------- listing

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
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

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
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "packages":
                return self._send(self.db.delivery_status(int(parts[2])))
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
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {
                    "submit", "review", "lock", "unlock", "packages", "receipts", "deliver"}:
                version_id = int(parts[2])
                action = parts[3]
                if action == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if action == "review":
                    return self._send(self.db.review(version_id, actor, body, role))
                if action == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                if action == "unlock":
                    return self._send(self.db.unlock(version_id, actor, role))
                if action == "packages":
                    return self._send(self.db.build_packages(version_id, actor, body, role))
                if action == "receipts":
                    return self._send(self.db.add_receipt(version_id, actor, body, role), 201)
                return self._send(self.db.deliver(version_id, actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与多渠道交付服务")
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
