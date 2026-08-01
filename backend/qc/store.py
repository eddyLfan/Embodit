"""SQLite-backed, one-file-per-scan QC report store."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path, *, writable: bool = True) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30.0)
    else:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def report_connection(path: Path, *, writable: bool = True) -> Iterator[sqlite3.Connection]:
    connection = connect(path, writable=writable)
    try:
        yield connection
        if writable:
            connection.commit()
    except Exception:
        if writable:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_report(path: Path, scan: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    with report_connection(path) as db:
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS scan (
                scan_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                dataset_path TEXT NOT NULL,
                dataset_format TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_fingerprint TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                total_episodes INTEGER NOT NULL DEFAULT 0,
                processed_episodes INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS detector_runs (
                detector_id TEXT PRIMARY KEY,
                detector_version TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                applicable INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                skip_reason TEXT,
                processed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                coverage_weight REAL NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS episodes (
                episode_index INTEGER PRIMARY KEY,
                task_text TEXT NOT NULL DEFAULT '',
                duration_s REAL NOT NULL DEFAULT 0,
                frame_count INTEGER NOT NULL DEFAULT 0,
                integrity_status TEXT NOT NULL,
                auto_decision TEXT NOT NULL,
                manual_decision TEXT,
                usable_ratio REAL NOT NULL,
                quality_score REAL NOT NULL,
                coverage REAL NOT NULL,
                finding_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                scan_status TEXT NOT NULL DEFAULT 'completed',
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS episode_detectors (
                episode_index INTEGER NOT NULL,
                detector_id TEXT NOT NULL,
                detector_version TEXT NOT NULL,
                status TEXT NOT NULL,
                skip_reason TEXT,
                coverage_weight REAL NOT NULL DEFAULT 1,
                PRIMARY KEY (episode_index, detector_id),
                FOREIGN KEY (episode_index) REFERENCES episodes(episode_index) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS findings (
                finding_id TEXT PRIMARY KEY,
                stable_signature TEXT NOT NULL,
                episode_index INTEGER NOT NULL,
                issue_code TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                confidence REAL NOT NULL,
                detector_id TEXT NOT NULL,
                detector_version TEXT NOT NULL,
                start_s REAL,
                end_s REAL,
                camera_key TEXT,
                signal_key TEXT,
                dimension_indices_json TEXT NOT NULL DEFAULT '[]',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                threshold_json TEXT NOT NULL DEFAULT '{}',
                explanation TEXT NOT NULL DEFAULT '',
                suggested_decision TEXT NOT NULL,
                hard_invalid INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (episode_index) REFERENCES episodes(episode_index) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS finding_reviews (
                finding_id TEXT PRIMARY KEY,
                review_status TEXT NOT NULL,
                reviewed_by TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                adjusted_start_s REAL,
                adjusted_end_s REAL,
                adjusted_severity TEXT,
                adjusted_issue_code TEXT,
                note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS saved_filters (
                filter_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                expression_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exports (
                export_id TEXT PRIMARY KEY,
                output_path TEXT NOT NULL,
                expression_json TEXT NOT NULL,
                included_count INTEGER NOT NULL,
                forced_invalid_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_findings_episode ON findings(episode_index);
            CREATE INDEX IF NOT EXISTS idx_findings_issue ON findings(issue_code);
            CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
            CREATE INDEX IF NOT EXISTS idx_findings_camera ON findings(camera_key);
            CREATE INDEX IF NOT EXISTS idx_episodes_decision ON episodes(auto_decision);
            CREATE INDEX IF NOT EXISTS idx_episodes_integrity ON episodes(integrity_status);
            """
        )
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("DELETE FROM scan")
        db.execute(
            """
            INSERT INTO scan (
                scan_id, schema_version, dataset_path, dataset_format, dataset_id,
                dataset_fingerprint, config_json, config_hash, status, phase,
                started_at, total_episodes, processed_episodes, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan["scanId"], SCHEMA_VERSION, scan["datasetPath"], scan["datasetFormat"],
                scan["datasetId"], scan["datasetFingerprint"],
                json.dumps(scan["config"], ensure_ascii=False, sort_keys=True),
                scan["configHash"], scan.get("status", "running"), scan.get("phase", "preflight"),
                scan.get("startedAt") or now_iso(), int(scan.get("totalEpisodes") or 0),
                int(scan.get("processedEpisodes") or 0), scan.get("message", ""),
            ),
        )
    return path


def update_scan(path: Path, **fields: Any) -> None:
    columns = {
        "status": "status", "phase": "phase", "completedAt": "completed_at",
        "processedEpisodes": "processed_episodes", "message": "message", "error": "error",
    }
    assignments = []
    values: list[Any] = []
    for key, value in fields.items():
        column = columns.get(key)
        if column:
            assignments.append(f"{column}=?")
            values.append(value)
    if not assignments:
        return
    with report_connection(path) as db:
        db.execute(f"UPDATE scan SET {', '.join(assignments)}", values)


def upsert_detector_run(path: Path, row: dict[str, Any]) -> None:
    with report_connection(path) as db:
        db.execute(
            """
            INSERT INTO detector_runs (
                detector_id, detector_version, enabled, applicable, status, skip_reason,
                processed_count, failed_count, coverage_weight, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(detector_id) DO UPDATE SET
                detector_version=excluded.detector_version,
                enabled=excluded.enabled, applicable=excluded.applicable,
                status=excluded.status, skip_reason=excluded.skip_reason,
                processed_count=excluded.processed_count, failed_count=excluded.failed_count,
                coverage_weight=excluded.coverage_weight, config_json=excluded.config_json
            """,
            (
                row["detectorId"], row.get("version", "1"), int(row.get("enabled", True)),
                int(row.get("applicable", True)), row.get("status", "pending"),
                row.get("skipReason"), int(row.get("processedCount", 0)),
                int(row.get("failedCount", 0)), float(row.get("coverageWeight", 1.0)),
                json.dumps(row.get("config") or {}, ensure_ascii=False, sort_keys=True),
            ),
        )


def write_episode(
    path: Path,
    episode: dict[str, Any],
    findings: list[dict[str, Any]],
    detector_statuses: list[dict[str, Any]],
) -> None:
    errors = sum(item.get("severity") in {"error", "fatal"} for item in findings)
    warnings = sum(item.get("severity") == "warning" for item in findings)
    with report_connection(path) as db:
        db.execute(
            """
            INSERT INTO episodes (
                episode_index, task_text, duration_s, frame_count, integrity_status,
                auto_decision, manual_decision, usable_ratio, quality_score, coverage,
                finding_count, error_count, warning_count, scan_status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(episode_index) DO UPDATE SET
                task_text=excluded.task_text, duration_s=excluded.duration_s,
                frame_count=excluded.frame_count, integrity_status=excluded.integrity_status,
                auto_decision=excluded.auto_decision, usable_ratio=excluded.usable_ratio,
                quality_score=excluded.quality_score, coverage=excluded.coverage,
                finding_count=excluded.finding_count, error_count=excluded.error_count,
                warning_count=excluded.warning_count, scan_status=excluded.scan_status
            """,
            (
                int(episode["episodeIndex"]), episode.get("taskText", ""),
                float(episode.get("duration", 0)), int(episode.get("frameCount", 0)),
                episode["integrityStatus"], episode["autoDecision"], episode.get("manualDecision"),
                float(episode["usableRatio"]), float(episode["qualityScore"]),
                float(episode["coverage"]), len(findings), errors, warnings,
                episode.get("scanStatus", "completed"), episode.get("note", ""),
            ),
        )
        db.execute("DELETE FROM episode_detectors WHERE episode_index=?", (int(episode["episodeIndex"]),))
        for status in detector_statuses:
            db.execute(
                """
                INSERT INTO episode_detectors (
                    episode_index, detector_id, detector_version, status, skip_reason, coverage_weight
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(episode["episodeIndex"]), status["detectorId"], status.get("version", "1"),
                    status.get("status", "completed"), status.get("skipReason"),
                    float(status.get("coverageWeight", 1.0)),
                ),
            )
        db.execute("DELETE FROM findings WHERE episode_index=?", (int(episode["episodeIndex"]),))
        for item in findings:
            db.execute(
                """
                INSERT INTO findings (
                    finding_id, stable_signature, episode_index, issue_code, category, severity,
                    confidence, detector_id, detector_version, start_s, end_s, camera_key,
                    signal_key, dimension_indices_json, metrics_json, threshold_json,
                    explanation, suggested_decision, hard_invalid, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["finding_id"], item["stable_signature"], int(item["episode_index"]),
                    item["issue_code"], item["category"], item["severity"],
                    float(item.get("confidence", 1.0)), item["detector_id"],
                    item.get("detector_version", "1"), item.get("start_s"), item.get("end_s"),
                    item.get("camera_key"), item.get("signal_key"),
                    json.dumps(item.get("dimension_indices") or []),
                    json.dumps(item.get("metrics") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(item.get("threshold") or {}, ensure_ascii=False, sort_keys=True),
                    item.get("explanation", ""), item.get("suggested_decision", "review"),
                    int(bool(item.get("hard_invalid"))), now_iso(),
                ),
            )


def _decode_finding(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for source, target in (
        ("dimension_indices_json", "dimensionIndices"),
        ("metrics_json", "metrics"),
        ("threshold_json", "threshold"),
    ):
        item[target] = json.loads(item.pop(source) or "[]" if "indices" in source else item.pop(source) or "{}")
    return _camelize(item)


def _camelize(row: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "episode_index": "episodeIndex", "task_text": "taskText",
        "duration_s": "duration", "frame_count": "frameCount",
        "integrity_status": "integrityStatus", "auto_decision": "autoDecision",
        "manual_decision": "manualDecision", "usable_ratio": "usableRatio",
        "quality_score": "qualityScore", "finding_count": "findingCount",
        "error_count": "errorCount", "warning_count": "warningCount",
        "scan_status": "scanStatus", "finding_id": "findingId",
        "stable_signature": "stableSignature", "issue_code": "issueCode",
        "detector_id": "detectorId", "detector_version": "detectorVersion",
        "start_s": "startS", "end_s": "endS", "camera_key": "cameraKey",
        "signal_key": "signalKey", "suggested_decision": "suggestedDecision",
        "hard_invalid": "hardInvalid", "created_at": "createdAt",
        "review_status": "reviewStatus", "adjusted_start_s": "adjustedStartS",
        "adjusted_end_s": "adjustedEndS", "adjusted_severity": "adjustedSeverity",
        "adjusted_issue_code": "adjustedIssueCode", "reviewed_by": "reviewedBy",
        "reviewed_at": "reviewedAt",
        "skip_reason": "skipReason", "coverage_weight": "coverageWeight",
        "processed_count": "processedCount", "failed_count": "failedCount",
        "config_hash": "configHash", "dataset_fingerprint": "datasetFingerprint",
    }
    return {mapping.get(key, key): value for key, value in row.items()}


def scan_info(path: Path) -> dict[str, Any]:
    with report_connection(path, writable=False) as db:
        row = db.execute("SELECT * FROM scan").fetchone()
        if row is None:
            raise ValueError("QC 报告缺少 scan 元数据")
        result = _camelize(dict(row))
        result["config"] = json.loads(result.pop("config_json"))
        return result


def summary(path: Path) -> dict[str, Any]:
    with report_connection(path, writable=False) as db:
        scan_row = db.execute("SELECT * FROM scan").fetchone()
        totals = db.execute(
            """
            SELECT COUNT(*) AS episodes,
                   SUM(integrity_status='invalid') AS invalid,
                   SUM(auto_decision='pass') AS passed,
                   SUM(auto_decision='review') AS review,
                   SUM(auto_decision='quarantine') AS quarantine,
                   AVG(quality_score) AS average_quality,
                   AVG(usable_ratio) AS average_usable,
                   AVG(coverage) AS average_coverage
            FROM episodes
            """
        ).fetchone()
        issues = [
            dict(row)
            for row in db.execute(
                """
                SELECT issue_code AS issueCode, category, severity, COUNT(*) AS count,
                       COUNT(DISTINCT episode_index) AS episodes
                FROM findings GROUP BY issue_code, category, severity
                ORDER BY count DESC, issue_code
                """
            )
        ]
        detectors = []
        for row in db.execute("SELECT * FROM detector_runs ORDER BY detector_id"):
            raw = dict(row)
            detector_config = json.loads(raw.pop("config_json") or "{}")
            item = _camelize(raw)
            stats = db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(status='completed') AS completed,
                       SUM(status='skipped') AS skipped,
                       SUM(status='failed') AS failed
                FROM episode_detectors WHERE detector_id=?
                """,
                (item["detectorId"],),
            ).fetchone()
            total = int(stats["total"] or 0)
            completed = int(stats["completed"] or 0)
            skipped = int(stats["skipped"] or 0)
            failed = int(stats["failed"] or 0)
            item.update(
                {
                    "config": detector_config,
                    "completedCount": completed,
                    "skippedCount": skipped,
                    "failedCount": failed,
                    "coverage": round(100.0 * completed / total, 3) if total else None,
                }
            )
            if total and skipped == total:
                item["status"] = "skipped"
                item["skipReason"] = "所有 episode 均不具备该检测器需要的字段"
            detectors.append(item)
    scan = _camelize(dict(scan_row)) if scan_row else {}
    scan.pop("config_json", None)
    return {
        "scan": scan,
        "totals": {
            "episodes": int(totals["episodes"] or 0),
            "invalid": int(totals["invalid"] or 0),
            "pass": int(totals["passed"] or 0),
            "review": int(totals["review"] or 0),
            "quarantine": int(totals["quarantine"] or 0),
            "averageQuality": round(float(totals["average_quality"] or 0), 3),
            "averageUsable": round(float(totals["average_usable"] or 0), 3),
            "averageCoverage": round(float(totals["average_coverage"] or 0), 3),
        },
        "issues": issues,
        "detectors": detectors,
    }


def query_episodes(path: Path, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    where: list[str] = []
    values: list[Any] = []
    if filters.get("integrityStatus") in {"valid", "invalid", "unknown"}:
        where.append("e.integrity_status=?")
        values.append(filters["integrityStatus"])
    if filters.get("decision") in {"pass", "review", "quarantine"}:
        where.append("COALESCE(e.manual_decision, e.auto_decision)=?")
        values.append(filters["decision"])
    for key, column in (
        ("minimumQuality", "e.quality_score"),
        ("minimumUsable", "e.usable_ratio"),
        ("minimumCoverage", "e.coverage"),
    ):
        if filters.get(key) is not None:
            where.append(f"{column}>=?")
            values.append(float(filters[key]))
    issues = [str(item) for item in filters.get("issueCodes") or [] if str(item)]
    if issues:
        marks = ",".join("?" for _ in issues)
        where.append(f"EXISTS (SELECT 1 FROM findings f WHERE f.episode_index=e.episode_index AND f.issue_code IN ({marks}))")
        values.extend(issues)
    search = str(filters.get("search") or "").strip()
    if search:
        where.append("(CAST(e.episode_index AS TEXT) LIKE ? OR e.task_text LIKE ?)")
        values.extend([f"%{search}%", f"%{search}%"])
    clause = " WHERE " + " AND ".join(where) if where else ""
    allowed_sort = {
        "episodeIndex": "e.episode_index", "qualityScore": "e.quality_score",
        "usableRatio": "e.usable_ratio", "coverage": "e.coverage",
        "findingCount": "e.finding_count",
    }
    sort = allowed_sort.get(str(filters.get("sort")), "e.episode_index")
    direction = "DESC" if str(filters.get("direction")).lower() == "desc" else "ASC"
    limit = min(500, max(1, int(filters.get("limit") or 100)))
    offset = max(0, int(filters.get("offset") or 0))
    with report_connection(path, writable=False) as db:
        total = int(db.execute(f"SELECT COUNT(*) FROM episodes e{clause}", values).fetchone()[0])
        rows = [
            _camelize(dict(row))
            for row in db.execute(
                f"SELECT e.* FROM episodes e{clause} ORDER BY {sort} {direction} LIMIT ? OFFSET ?",
                [*values, limit, offset],
            )
        ]
        for row in rows:
            issue_rows = db.execute(
                """
                SELECT issue_code, severity, COUNT(*) AS count FROM findings
                WHERE episode_index=? GROUP BY issue_code, severity ORDER BY count DESC
                """,
                (row["episodeIndex"],),
            ).fetchall()
            row["issues"] = [
                {"issueCode": item["issue_code"], "severity": item["severity"], "count": item["count"]}
                for item in issue_rows
            ]
    return {"total": total, "offset": offset, "limit": limit, "episodes": rows}


def selected_episode_indices(path: Path, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an unpaginated selection for export, with invalid rows separated."""
    filters = dict(filters or {})
    page_size = 500
    offset = 0
    selected: list[int] = []
    invalid: list[int] = []
    while True:
        page = query_episodes(path, {**filters, "limit": page_size, "offset": offset})
        rows = page["episodes"]
        for row in rows:
            index = int(row["episodeIndex"])
            (invalid if row["integrityStatus"] == "invalid" else selected).append(index)
        offset += len(rows)
        if not rows or offset >= int(page["total"]):
            break
    return {
        "episodes": selected,
        "invalidEpisodes": invalid,
        "total": len(selected) + len(invalid),
    }


def episode_detail(path: Path, episode_index: int) -> dict[str, Any]:
    with report_connection(path, writable=False) as db:
        episode = db.execute("SELECT * FROM episodes WHERE episode_index=?", (int(episode_index),)).fetchone()
        if episode is None:
            raise KeyError(episode_index)
        rows = db.execute(
            """
            SELECT f.*, r.review_status, r.reviewed_by, r.reviewed_at,
                   r.adjusted_start_s, r.adjusted_end_s, r.adjusted_severity,
                   r.adjusted_issue_code, r.note AS review_note
            FROM findings f LEFT JOIN finding_reviews r ON r.finding_id=f.finding_id
            WHERE f.episode_index=? ORDER BY COALESCE(f.start_s, -1), f.severity DESC
            """,
            (int(episode_index),),
        ).fetchall()
        detectors = [
            _camelize(dict(row))
            for row in db.execute(
                "SELECT * FROM episode_detectors WHERE episode_index=? ORDER BY detector_id",
                (int(episode_index),),
            )
        ]
    return {
        "episode": _camelize(dict(episode)),
        "findings": [_decode_finding(row) for row in rows],
        "detectors": detectors,
    }


def review_finding(path: Path, finding_id: str, review: dict[str, Any], actor: str) -> dict[str, Any]:
    status = str(review.get("reviewStatus") or "unreviewed")
    if status not in {"unreviewed", "confirmed", "rejected", "modified"}:
        raise ValueError("非法 reviewStatus")
    with report_connection(path) as db:
        if db.execute("SELECT 1 FROM findings WHERE finding_id=?", (finding_id,)).fetchone() is None:
            raise KeyError(finding_id)
        db.execute(
            """
            INSERT INTO finding_reviews (
                finding_id, review_status, reviewed_by, reviewed_at,
                adjusted_start_s, adjusted_end_s, adjusted_severity,
                adjusted_issue_code, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_id) DO UPDATE SET
                review_status=excluded.review_status, reviewed_by=excluded.reviewed_by,
                reviewed_at=excluded.reviewed_at, adjusted_start_s=excluded.adjusted_start_s,
                adjusted_end_s=excluded.adjusted_end_s,
                adjusted_severity=excluded.adjusted_severity,
                adjusted_issue_code=excluded.adjusted_issue_code, note=excluded.note
            """,
            (
                finding_id, status, actor, now_iso(), review.get("startS"), review.get("endS"),
                review.get("severity"), review.get("issueCode"), str(review.get("note") or ""),
            ),
        )
        db.execute(
            """
            INSERT INTO audit_log (action, target_type, target_id, actor, payload_json, created_at)
            VALUES ('review', 'finding', ?, ?, ?, ?)
            """,
            (finding_id, actor, json.dumps(review, ensure_ascii=False), now_iso()),
        )
    return {"findingId": finding_id, "reviewStatus": status}


def review_episode(path: Path, episode_index: int, decision: str | None, note: str, actor: str) -> dict[str, Any]:
    if decision is not None and decision not in {"pass", "review", "quarantine"}:
        raise ValueError("非法人工决定")
    with report_connection(path) as db:
        cursor = db.execute(
            "UPDATE episodes SET manual_decision=?, note=? WHERE episode_index=?",
            (decision, note, int(episode_index)),
        )
        if cursor.rowcount == 0:
            raise KeyError(episode_index)
        db.execute(
            """
            INSERT INTO audit_log (action, target_type, target_id, actor, payload_json, created_at)
            VALUES ('decision', 'episode', ?, ?, ?, ?)
            """,
            (str(episode_index), actor, json.dumps({"decision": decision, "note": note}, ensure_ascii=False), now_iso()),
        )
    return {"episodeIndex": int(episode_index), "manualDecision": decision, "note": note}


def report_csv(path: Path, kind: str) -> str:
    output = io.StringIO()
    with report_connection(path, writable=False) as db:
        if kind == "findings":
            rows = db.execute("SELECT * FROM findings ORDER BY episode_index, start_s").fetchall()
        else:
            rows = db.execute("SELECT * FROM episodes ORDER BY episode_index").fetchall()
        if not rows:
            return ""
        writer = csv.writer(output)
        writer.writerow(rows[0].keys())
        writer.writerows([tuple(row) for row in rows])
    return output.getvalue()
