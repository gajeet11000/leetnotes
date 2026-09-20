import json
import sqlite3

import structlog

from leetnotes_core.leetcode.models import ProblemRecord, QuestionContent

from .db import get_connection

logger = structlog.get_logger(__name__)

_COLUMNS = (
    "slug",
    "id",
    "title",
    "url",
    "difficulty",
    "category",
    "raw_question_html",
    "has_images",
    "imgs_local_paths",
    "content_remote_markdown",
    "content_local_html",
    "content_local_markdown",
    "content_text",
)


class ProblemStorage:
    """SQLite-backed CRUD for ProblemRecord data (the `problems` + `tags` +
    `problem_tags` tables in leetcode.db).

    Community/public problem data only — never touches submission data, so
    these tables are always safe to share or export as-is (e.g. via
    `sqlite3 leetcode.db ".dump problems tags problem_tags"`).
    """

    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    # -------------------------------------------------------------------
    # Row <-> ProblemRecord mapping
    # -------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row, tags: list[dict]) -> ProblemRecord:
        has_images = row["has_images"]
        imgs_raw = row["imgs_local_paths"]
        return ProblemRecord(
            slug=row["slug"],
            id=row["id"],
            title=row["title"],
            url=row["url"],
            difficulty=row["difficulty"],
            category=row["category"],
            tags=tags or None,
            raw_question_html=row["raw_question_html"],
            has_images=bool(has_images) if has_images is not None else None,
            imgs_local_paths=json.loads(imgs_raw) if imgs_raw is not None else None,
            content=QuestionContent(
                remote_markdown=row["content_remote_markdown"],
                local_html=row["content_local_html"],
                local_markdown=row["content_local_markdown"],
                text=row["content_text"],
            ),
        )

    def _get_tags(self, slug: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.slug, t.name FROM tags t "
            "JOIN problem_tags pt ON pt.tag_id = t.id "
            "WHERE pt.problem_slug = ? ORDER BY t.id",
            (slug,),
        ).fetchall()
        return [{"name": r["name"], "slug": r["slug"]} for r in rows]

    def _sync_tags(self, slug: str, tags: list[dict] | None) -> None:
        """Replaces `slug`'s tag associations with `tags`, upserting each tag by its own slug."""
        self.conn.execute("DELETE FROM problem_tags WHERE problem_slug = ?", (slug,))
        for tag in tags or []:
            tag_slug, tag_name = tag.get("slug"), tag.get("name")
            self.conn.execute(
                "INSERT INTO tags (slug, name) VALUES (?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET name = excluded.name",
                (tag_slug, tag_name),
            )
            tag_id = self.conn.execute(
                "SELECT id FROM tags WHERE slug = ?", (tag_slug,)
            ).fetchone()["id"]
            self.conn.execute(
                "INSERT OR IGNORE INTO problem_tags (problem_slug, tag_id) VALUES (?, ?)",
                (slug, tag_id),
            )

    def _upsert_one(self, record: ProblemRecord) -> None:
        values = (
            record.slug,
            record.id,
            record.title,
            record.url,
            record.difficulty,
            record.category,
            record.raw_question_html,
            record.has_images,
            (
                json.dumps(record.imgs_local_paths)
                if record.imgs_local_paths is not None
                else None
            ),
            record.content.remote_markdown,
            record.content.local_html,
            record.content.local_markdown,
            record.content.text,
        )
        placeholders = ", ".join("?" for _ in _COLUMNS)
        update_clause = ", ".join(
            f"{col} = excluded.{col}" for col in _COLUMNS if col != "slug"
        )
        self.conn.execute(
            f"INSERT INTO problems ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(slug) DO UPDATE SET {update_clause}",
            values,
        )
        self._sync_tags(record.slug, record.tags)

    # -------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------

    def add_or_update(self, record: ProblemRecord | dict) -> ProblemRecord:
        """Inserts or updates a problem using the slug as the key."""
        if isinstance(record, dict):
            record = ProblemRecord(**record)

        log = logger.bind(slug=record.slug)
        with self.conn:
            self._upsert_one(record)
        log.info("problem_record_saved", question_id=record.id, title=record.title)
        return record

    def bulk_add_or_update(self, records: list[ProblemRecord | dict]) -> int:
        """Batch inserts multiple problems using slug as keys in a single transaction."""
        count = 0
        with self.conn:
            for item in records:
                record = (
                    item if isinstance(item, ProblemRecord) else ProblemRecord(**item)
                )
                self._upsert_one(record)
                count += 1
        logger.info("problems_bulk_saved", count=count)
        return count

    def get_by_slug(self, slug: str) -> ProblemRecord | None:
        """Fetches a single problem record by slug."""
        log = logger.bind(slug=slug)
        row = self.conn.execute(
            "SELECT * FROM problems WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            log.info("problem_record_not_found")
            return None
        log.info("problem_record_found", question_id=row["id"])
        return self._row_to_record(row, self._get_tags(slug))

    def get_by_id(self, question_id: int) -> ProblemRecord | None:
        """Secondary lookup: fetches a problem record by frontend question ID."""
        log = logger.bind(question_id=question_id)
        row = self.conn.execute(
            "SELECT * FROM problems WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None:
            log.info("problem_record_not_found_by_id")
            return None
        log.info("problem_record_found_by_id", slug=row["slug"])
        return self._row_to_record(row, self._get_tags(row["slug"]))

    def exists(self, identifier: str | int) -> bool:
        """Checks if a problem exists by slug (str) or question ID (int)."""
        if isinstance(identifier, str):
            found = (
                self.conn.execute(
                    "SELECT 1 FROM problems WHERE slug = ?", (identifier,)
                ).fetchone()
                is not None
            )
            logger.bind(slug=identifier).info("problem_exists_check", exists=found)
            return found
        return self.get_by_id(identifier) is not None

    def delete(self, identifier: str | int) -> bool:
        """Deletes a record by slug (str) or question ID (int). Returns True if deleted."""
        log = logger.bind(identifier=identifier)
        if isinstance(identifier, str):
            target_slug = identifier
        else:
            row = self.conn.execute(
                "SELECT slug FROM problems WHERE id = ?", (identifier,)
            ).fetchone()
            target_slug = row["slug"] if row else None

        if target_slug is None:
            log.info("problem_record_delete_skipped", reason="not_found")
            return False

        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM problems WHERE slug = ?", (target_slug,)
            )
        if cursor.rowcount:
            log.info("problem_record_deleted", slug=target_slug)
            return True
        log.info("problem_record_delete_skipped", reason="not_found")
        return False

    def list_all(self) -> list[ProblemRecord]:
        """Returns all stored problem records."""
        rows = self.conn.execute("SELECT * FROM problems").fetchall()
        tag_rows = self.conn.execute(
            "SELECT pt.problem_slug, t.slug, t.name FROM problem_tags pt "
            "JOIN tags t ON t.id = pt.tag_id ORDER BY t.id"
        ).fetchall()
        tags_by_slug: dict[str, list[dict]] = {}
        for r in tag_rows:
            tags_by_slug.setdefault(r["problem_slug"], []).append(
                {"name": r["name"], "slug": r["slug"]}
            )

        records = [
            self._row_to_record(row, tags_by_slug.get(row["slug"], [])) for row in rows
        ]
        logger.info("problems_listed", count=len(records))
        return records

    def count(self) -> int:
        """Returns total number of stored problems."""
        total = self.conn.execute("SELECT COUNT(*) AS c FROM problems").fetchone()["c"]
        logger.info("problems_counted", count=total)
        return total
