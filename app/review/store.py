import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from app.config import get_settings
from app.domain.departments import validate_department_email
from app.review.models import PreviewRecord, Suggestion


class ReviewConflict(RuntimeError):
    pass


class ReviewNotFound(RuntimeError):
    pass


class ReviewStoreBusy(RuntimeError):
    pass


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def payload_digest(email: str, message: str, version: int = 1) -> str:
    raw = json.dumps(
        {"email": email, "message": message, "version": version},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _database_process_lock(path: Path) -> Iterator[BinaryIO]:
    with path.open("a+b") as handle:
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReviewStoreBusy("Another backend owns this review database.") from exc
        yield handle


class ReviewStore:
    """One live process owns the DB; short SQLite transactions arbitrate its threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._lock_file: BinaryIO | None = None
        self._lock_stack: ExitStack | None = None
        self._lifecycle_lock = threading.Lock()
        self.suggestion_slots = threading.BoundedSemaphore(
            get_settings().MAX_CONCURRENT_ROUTE_REQUESTS
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._lock_file is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stack = ExitStack()
            handle = stack.enter_context(_database_process_lock(Path(str(self.path) + ".lock")))
            self._lock_file = handle
            self._lock_stack = stack
            try:
                with closing(self._connect()) as db:
                    db.executescript("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_key TEXT PRIMARY KEY, csrf_token TEXT NOT NULL,
                        created_at REAL NOT NULL, expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS previews (
                        id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                        version INTEGER NOT NULL, payload_digest TEXT NOT NULL,
                        email TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                        created_at REAL NOT NULL, expires_at REAL NOT NULL,
                        recipient TEXT, suggestion_json TEXT NOT NULL,
                        suggestion_token TEXT, message_id TEXT, error TEXT,
                        FOREIGN KEY(session_key) REFERENCES sessions(session_key)
                    );
                    CREATE TABLE IF NOT EXISTS approvals (
                        preview_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                        version INTEGER NOT NULL, payload_digest TEXT NOT NULL,
                        recipient TEXT NOT NULL, created_at REAL NOT NULL,
                        FOREIGN KEY(preview_id) REFERENCES previews(id)
                    );
                    CREATE TABLE IF NOT EXISTS attempts (
                        id TEXT PRIMARY KEY, preview_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL, state TEXT NOT NULL,
                        message_id TEXT NOT NULL, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, error TEXT,
                        UNIQUE(preview_id, sequence),
                        FOREIGN KEY(preview_id) REFERENCES previews(id)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        preview_id TEXT NOT NULL, event TEXT NOT NULL,
                        created_at REAL NOT NULL, details_json TEXT NOT NULL
                    );
                    """)
                with self._transaction() as db:
                    now = time.time()
                    orphaned = db.execute(
                        "SELECT id FROM previews WHERE state='sending'"
                    ).fetchall()
                    for row in orphaned:
                        error = (
                            "Przerwano proces podczas wysyłki. Sprawdź Mailpit przed nową decyzją."
                        )
                        db.execute(
                            "UPDATE previews SET state='delivery_unknown', error=? WHERE id=?",
                            (error, row["id"]),
                        )
                        db.execute(
                            "UPDATE attempts SET state='delivery_unknown', er"
                            "ror=?, updated_at=? WHERE preview_id=? AND state"
                            "='sending'",
                            (error, now, row["id"]),
                        )
                        self._event(db, row["id"], "recovered_delivery_unknown", {})
                    for row in db.execute("SELECT id,suggestion_json FROM previews"):
                        suggestion = Suggestion.model_validate_json(row["suggestion_json"])
                        if suggestion.status == "pending":
                            suggestion = Suggestion(
                                status="error",
                                error="Przerwano pobieranie sugestii. Możesz wybrać dział ręcznie.",
                            )
                            db.execute(
                                "UPDATE previews SET suggestion_json=?, sugge"
                                "stion_token=NULL WHERE id=?",
                                (suggestion.model_dump_json(), row["id"]),
                            )
                if os.name != "nt":
                    self.path.chmod(0o600)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        stack, self._lock_stack = self._lock_stack, None
        self._lock_file = None
        if stack is not None:
            # Closing the descriptor releases the OS lock, also after process death.
            stack.close()

    def _connect(self) -> sqlite3.Connection:
        if self._lock_file is None:
            raise RuntimeError("ReviewStore.start() must run before database access.")
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(
        db: sqlite3.Connection, preview_id: str, event: str, details: dict[str, Any]
    ) -> None:
        db.execute(
            "INSERT INTO events(preview_id,event,created_at,details_json) VALUES(?,?,?,?)",
            (preview_id, event, time.time(), json.dumps(details, sort_keys=True)),
        )

    def create_session(self, key: str, csrf_token: str, ttl_seconds: int) -> None:
        now = time.time()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)", (key, csrf_token, now, now + ttl_seconds)
            )

    def get_session(self, key: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM sessions WHERE session_key=? AND expires_at>?", (key, time.time())
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            db.close()

    @staticmethod
    def _row(db: sqlite3.Connection, preview_id: str, session_key: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM previews WHERE id=? AND session_key=?", (preview_id, session_key)
        ).fetchone()
        if row is None:
            raise ReviewNotFound("Podgląd nie istnieje w tej sesji.")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _check_version(row: sqlite3.Row, version: int, digest: str | None = None) -> None:
        if row["version"] != version or (digest is not None and row["payload_digest"] != digest):
            raise ReviewConflict("Wersja lub treść podglądu uległa zmianie.")
        if row["payload_digest"] != payload_digest(row["email"], row["message"], row["version"]):
            raise ReviewConflict("Naruszono integralność zapisanego podglądu.")

    @staticmethod
    def _check_not_expired(row: sqlite3.Row) -> None:
        if time.time() >= row["expires_at"]:
            raise ReviewConflict("Podgląd wygasł. Utwórz nowy i zatwierdź go ponownie.")

    def _record(self, db: sqlite3.Connection, row: sqlite3.Row) -> PreviewRecord:
        attempts = [
            dict(a)
            for a in db.execute(
                "SELECT id,sequence,state,message_id,created_at,updated_at,er"
                "ror FROM attempts WHERE preview_id=? ORDER BY sequence",
                (row["id"],),
            )
        ]
        for attempt in attempts:
            attempt["created_at"] = timestamp(attempt["created_at"])
            attempt["updated_at"] = timestamp(attempt["updated_at"])
        state = row["state"]
        if state in {"pending_review", "failed_before_submit"} and time.time() >= row["expires_at"]:
            state = "expired"
        suggestion = Suggestion.model_validate_json(row["suggestion_json"])
        if state == "expired" and suggestion.status == "pending":
            suggestion = Suggestion(
                status="error", error="Podgląd wygasł przed zakończeniem sugestii."
            )
        return PreviewRecord.model_validate(
            {
                "id": row["id"],
                "version": row["version"],
                "payload_digest": row["payload_digest"],
                "email": row["email"],
                "message": row["message"],
                "state": state,
                "created_at": timestamp(row["created_at"]),
                "expires_at": timestamp(row["expires_at"]),
                "recipient": row["recipient"],
                "suggestion": suggestion,
                "message_id": row["message_id"],
                "attempts": attempts,
                "retry_allowed": state == "failed_before_submit",
                "error": row["error"],
            }
        )

    def create_preview(
        self, session_key: str, email: str, message: str, ttl_seconds: int
    ) -> PreviewRecord:
        preview_id, now = uuid.uuid4().hex, time.time()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO previews(id,session_key,version,payload_digest,e"
                "mail,message,state,created_at,expires_at,suggestion_json) VA"
                "LUES(?,?,?,?,?,?,'pending_review',?,?,?)",
                (
                    preview_id,
                    session_key,
                    1,
                    payload_digest(email, message),
                    email,
                    message,
                    now,
                    now + ttl_seconds,
                    Suggestion().model_dump_json(),
                ),
            )
            self._event(db, preview_id, "preview_created", {})
            return self._record(db, self._row(db, preview_id, session_key))

    def get_preview(self, preview_id: str, session_key: str) -> PreviewRecord:
        db = self._connect()
        try:
            # The preview row and its attempts form one recovery decision.
            # A deferred read transaction keeps both SELECTs in one snapshot
            # without acquiring the writer reservation used by mutations.
            with db:
                db.execute("BEGIN")
                return self._record(db, self._row(db, preview_id, session_key))
        finally:
            db.close()

    def begin_suggestion(
        self, preview_id: str, session_key: str, version: int
    ) -> tuple[PreviewRecord, str | None]:
        with self._transaction() as db:
            row = self._row(db, preview_id, session_key)
            self._check_version(row, version)
            self._check_not_expired(row)
            if row["state"] != "pending_review":
                raise ReviewConflict("Sugestia jest dostępna tylko przed zatwierdzeniem.")
            if Suggestion.model_validate_json(row["suggestion_json"]).status == "pending":
                return self._record(db, row), None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE previews SET suggestion_json=?,suggestion_token=? WHERE id=?",
                (Suggestion(status="pending").model_dump_json(), token, preview_id),
            )
            return self._record(db, self._row(db, preview_id, session_key)), token

    def finish_suggestion(
        self, preview_id: str, session_key: str, version: int, token: str, suggestion: Suggestion
    ) -> PreviewRecord:
        with self._transaction() as db:
            db.execute(
                "UPDATE previews SET suggestion_json=CASE WHEN expires_at>? "
                "THEN ? ELSE ? END,suggestion_token=NULL "
                "WHERE id=? AND session_key=? AND version=? AND state='pendin"
                "g_review' AND suggestion_token=?",
                (
                    time.time(),
                    suggestion.model_dump_json(),
                    Suggestion(
                        status="error", error="Podgląd wygasł przed zakończeniem sugestii."
                    ).model_dump_json(),
                    preview_id,
                    session_key,
                    version,
                    token,
                ),
            )
            return self._record(db, self._row(db, preview_id, session_key))

    def _start_attempt(self, db: sqlite3.Connection, row: sqlite3.Row) -> str:
        attempt_id, now = uuid.uuid4().hex, time.time()
        sequence = (
            db.execute("SELECT COUNT(*) FROM attempts WHERE preview_id=?", (row["id"],)).fetchone()[
                0
            ]
            + 1
        )
        message_id = row["message_id"] or f"<review-{row['id']}@local-message-routing-agent.test>"
        db.execute(
            "UPDATE previews SET state='sending',message_id=?,error=NULL WHERE id=?",
            (message_id, row["id"]),
        )
        db.execute(
            "INSERT INTO attempts VALUES(?,?,?,'sending',?,?,?,NULL)",
            (attempt_id, row["id"], sequence, message_id, now, now),
        )
        self._event(db, row["id"], "sending", {"attempt_id": attempt_id, "sequence": sequence})
        return attempt_id

    def begin_approval(
        self, preview_id: str, session_key: str, version: int, digest: str, recipient: str
    ) -> tuple[PreviewRecord, str | None]:
        recipient = validate_department_email(recipient).value
        with self._transaction() as db:
            row = self._row(db, preview_id, session_key)
            self._check_version(row, version, digest)
            approval = db.execute(
                "SELECT * FROM approvals WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if approval is not None:
                if (
                    approval["session_key"] != session_key
                    or approval["version"] != version
                    or approval["payload_digest"] != digest
                    or approval["recipient"] != recipient
                ):
                    raise ReviewConflict("Ta operacja ma inne zatwierdzenie.")
                return self._record(db, row), None
            self._check_not_expired(row)
            if row["state"] != "pending_review":
                raise ReviewConflict("Podgląd nie oczekuje na zatwierdzenie.")
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?)",
                (preview_id, session_key, version, digest, recipient, time.time()),
            )
            suggestion = Suggestion.model_validate_json(row["suggestion_json"])
            if suggestion.status == "pending":
                suggestion = Suggestion(
                    status="error", error="Podgląd zatwierdzono przed zakończeniem sugestii."
                )
            db.execute(
                "UPDATE previews SET recipient=?,suggestion_json=?,suggestion"
                "_token=NULL WHERE id=?",
                (recipient, suggestion.model_dump_json(), preview_id),
            )
            self._event(
                db,
                preview_id,
                "approved",
                {
                    "version": version,
                    "payload_digest": digest,
                    "recipient": recipient,
                    "session_key": session_key,
                },
            )
            attempt_id = self._start_attempt(db, self._row(db, preview_id, session_key))
            return self._record(db, self._row(db, preview_id, session_key)), attempt_id

    def begin_retry(
        self,
        preview_id: str,
        session_key: str,
        version: int,
        digest: str,
        expected_attempt_id: str,
    ) -> tuple[PreviewRecord, str | None]:
        with self._transaction() as db:
            row = self._row(db, preview_id, session_key)
            self._check_version(row, version, digest)
            approval = db.execute(
                "SELECT * FROM approvals WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if (
                approval is None
                or approval["session_key"] != session_key
                or approval["version"] != version
                or approval["payload_digest"] != digest
                or approval["recipient"] != row["recipient"]
            ):
                raise ReviewConflict("Brak zgodnego zatwierdzenia.")
            expected_attempt = db.execute(
                "SELECT sequence,state FROM attempts WHERE id=? AND preview_id=?",
                (expected_attempt_id, preview_id),
            ).fetchone()
            if expected_attempt is None or expected_attempt["state"] != "failed_before_submit":
                raise ReviewConflict("Ponowienie wymaga wskazania nieprzekazanej próby.")
            # Attempts are append-only, consecutive and unique per preview. The
            # initial approval is sequence 1; every later sequence is the single
            # retry of its predecessor. This durable link also covers old rows,
            # so an HTTP replay cannot create another successor after a fast
            # pre-DATA failure, a later retry, or a process restart.
            successor = db.execute(
                "SELECT id FROM attempts WHERE preview_id=? AND sequence=?",
                (preview_id, expected_attempt["sequence"] + 1),
            ).fetchone()
            if successor is not None:
                return self._record(db, row), None
            latest = db.execute(
                "SELECT id FROM attempts WHERE preview_id=? ORDER BY sequence DESC LIMIT 1",
                (preview_id,),
            ).fetchone()
            if (
                row["state"] != "failed_before_submit"
                or latest is None
                or latest["id"] != expected_attempt_id
            ):
                raise ReviewConflict(
                    "Ponowienie jest dostępne tylko dla ostatniej nieprzekazanej próby."
                )
            self._check_not_expired(row)
            attempt_id = self._start_attempt(db, row)
            return self._record(db, self._row(db, preview_id, session_key)), attempt_id

    def complete_attempt(
        self,
        preview_id: str,
        session_key: str,
        attempt_id: str,
        state: str,
        error: str | None = None,
    ) -> PreviewRecord:
        if state not in {"sent", "failed_before_submit", "delivery_unknown"}:
            raise ValueError("Invalid delivery state")
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE attempts SET state=?,error=?,updated_at=? WHERE id=? "
                "AND preview_id=? AND state='sending'",
                (state, error, time.time(), attempt_id, preview_id),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE previews SET state=?,error=? WHERE id=? AND sessi"
                    "on_key=? AND state='sending'",
                    (state, error, preview_id, session_key),
                )
                self._event(db, preview_id, state, {"attempt_id": attempt_id})
            return self._record(db, self._row(db, preview_id, session_key))

    def reject(self, preview_id: str, session_key: str, version: int) -> PreviewRecord:
        with self._transaction() as db:
            row = self._row(db, preview_id, session_key)
            self._check_version(row, version)
            if row["state"] == "rejected":
                return self._record(db, row)
            # This write is a fence against delayed approval/retry requests, not
            # merely a UI dismissal. It remains available after virtual expiry
            # and after a known pre-DATA failure; BEGIN IMMEDIATE serializes it
            # with either delivery transition. Keep approval and attempt history.
            if row["state"] not in {"pending_review", "failed_before_submit"}:
                raise ReviewConflict("Nie można odrzucić rozpoczętej lub zamkniętej operacji.")
            suggestion = Suggestion.model_validate_json(row["suggestion_json"])
            if suggestion.status == "pending":
                suggestion = Suggestion(
                    status="error", error="Podgląd odrzucono przed zakończeniem sugestii."
                )
            db.execute(
                "UPDATE previews SET state='rejected',suggestion_token=NULL,suggestion_json=? "
                "WHERE id=?",
                (suggestion.model_dump_json(), preview_id),
            )
            self._event(db, preview_id, "rejected", {})
            return self._record(db, self._row(db, preview_id, session_key))
