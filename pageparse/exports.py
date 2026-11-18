"""Journaled, no-clobber publication and indexed cleanup of export files."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from time import monotonic, sleep, time
from uuid import uuid4

from .jobs import markdown_result, pretty_json

FORMATS = ("md", "json")
SERIALIZERS = {"md": markdown_result, "json": pretty_json}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
SCHEMA = """
BEGIN;
CREATE TABLE export_groups (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    source_name TEXT NOT NULL,
    parent TEXT NOT NULL,
    parent_dev INTEGER NOT NULL,
    parent_ino INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','saved','incomplete','attention required')),
    expected_files INTEGER NOT NULL DEFAULT 2 CHECK(expected_files IN (1, 2))
);
CREATE TABLE export_files (
    group_id TEXT NOT NULL REFERENCES export_groups(id) ON DELETE CASCADE,
    format TEXT NOT NULL CHECK(format IN ('md','json')),
    staging TEXT NOT NULL,
    final TEXT NOT NULL,
    absolute_path TEXT NOT NULL UNIQUE,
    stage_dev INTEGER,
    stage_ino INTEGER,
    final_dev INTEGER,
    final_ino INTEGER,
    digest TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    stage_state TEXT NOT NULL DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (group_id, format)
);
PRAGMA user_version = 2;
COMMIT;
"""

MIGRATE_V1 = """
BEGIN;
ALTER TABLE export_groups ADD COLUMN expected_files INTEGER NOT NULL DEFAULT 2
    CHECK(expected_files IN (1, 2));
PRAGMA user_version = 2;
COMMIT;
"""


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class Proposal:
    parent: Path
    identity: tuple[int, int]
    paths: tuple[Path, ...]
    trusted: bool

    @property
    def formats(self):
        return tuple(path.suffix[1:] for path in self.paths)


@dataclass(frozen=True)
class Outcome:
    path: Path
    status: str


@dataclass(frozen=True)
class Publication:
    status: str
    paths: tuple[Path, ...] = ()
    staging: tuple[Path, ...] = ()
    proposal: Proposal | None = None
    message: str = ""


def identity(info):
    return info.st_dev, info.st_ino


def metadata(info):
    return (*identity(info), info.st_size, info.st_mtime_ns)


def trusted_directory(info):
    return info.st_uid == os.geteuid() and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


@contextmanager
def directory(path):
    fd = os.open("/", DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def digest_file(file):
    return hashlib.file_digest(file, "sha256").hexdigest()


def verify(fd, name, expected, row, *, remove=False, stage=False):
    """Return the observed outcome after identity, metadata, and digest checks."""
    try:
        initial = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "inaccessible"
    if expected[0] is None:
        return "unowned"
    if not stat.S_ISREG(initial.st_mode) or identity(initial) != expected:
        return "changed"
    # Before the digest is journaled, the exclusively created stage's recorded
    # identity establishes ownership of a partially written file.
    if row["digest"] is None and not stage:
        return "unowned"
    try:
        with os.fdopen(os.open(name, FILE_FLAGS, dir_fd=fd), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or metadata(before) != metadata(initial):
                return "changed"
            if row["digest"] is not None:
                if (before.st_size, before.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                    return "changed"
                if digest_file(handle) != row["digest"]:
                    return "changed"
            if metadata(os.fstat(handle.fileno())) != metadata(before):
                return "changed"
            if remove and not trusted_directory(os.fstat(fd)):
                return "untrusted directory"
            if metadata(os.stat(name, dir_fd=fd, follow_symlinks=False)) != metadata(before):
                return "changed"
            if remove:
                os.unlink(name, dir_fd=fd)
                return "deleted"
        return "verified"
    except FileNotFoundError:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "inaccessible"
        return "changed"
    except OSError:
        return "inaccessible"


class ExportStore:
    def __init__(self, index: Path, *, lock_timeout=2):
        self.index = index
        self.lock_timeout = lock_timeout

    @contextmanager
    def locked(self):
        lock = None
        try:
            try:
                self.index.parent.mkdir(parents=True, exist_ok=True)
                lock = os.open(self.index.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise ExportError("The export index is unavailable. Check the state directory and its permissions.") from None
            deadline = monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if monotonic() >= deadline:
                        raise ExportError("The export index is busy. Retry in a moment.") from None
                    sleep(min(.05, max(0, deadline - monotonic())))
                except OSError:
                    raise ExportError("Could not lock the export index. Check its filesystem and permissions.") from None
            db = sqlite3.connect(self.index, timeout=0)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if not version:
                    db.executescript(SCHEMA)
                elif version == 1:
                    db.executescript(MIGRATE_V1)
                elif version != 2:
                    raise ExportError("This export index needs a compatible PageParse version.")
                yield db
            finally:
                db.close()
        except sqlite3.Error:
            raise ExportError("The export index is unavailable. Check its permissions, disk space, and integrity.") from None
        finally:
            if lock is not None:
                os.close(lock)

    def plan(self, result, destination: Path, formats=FORMATS):
        if not result["pages"] or result["status"] not in ("complete", "failed", "cancelled"):
            raise ExportError("Generate at least one page before exporting.")
        requested = set(formats)
        if not requested or not requested.issubset(FORMATS):
            raise ExportError("Choose Markdown, JSON, or both.")
        formats = tuple(fmt for fmt in FORMATS if fmt in requested)
        parent = Path(os.path.abspath(destination))
        try:
            with self.locked() as db, directory(parent) as fd:
                return self.candidate(db, fd, parent, result, formats)
        except OSError:
            raise ExportError("Choose an accessible output directory without symlink components.") from None

    def candidate(self, db, fd, parent, result, formats):
        stem = Path(result["source_name"]).stem
        if result["status"] != "complete":
            stem += ".partial"
        suffix = 1
        while True:
            name = stem if suffix == 1 else f"{stem}-{suffix}"
            paths = tuple(parent / f"{name}.{fmt}" for fmt in formats)
            if all(self.available(db, fd, path) for path in paths):
                info = os.fstat(fd)
                return Proposal(parent, identity(info), paths, trusted_directory(info))
            suffix += 1

    @staticmethod
    def available(db, fd, path):
        if db.execute("SELECT 1 FROM export_files WHERE absolute_path=?", (str(path),)).fetchone():
            return False
        try:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False

    def publish(self, result, proposal):
        if not result["pages"] or result["status"] not in ("complete", "failed", "cancelled"):
            raise ExportError("Generate at least one page before exporting.")
        try:
            with self.locked() as db, directory(proposal.parent) as fd:
                current = os.fstat(fd)
                if identity(current) != proposal.identity or trusted_directory(current) != proposal.trusted:
                    return Publication("collision", proposal=self.candidate(db, fd, proposal.parent, result, proposal.formats),
                                       message="The destination changed. Review the proposed paths again.")
                if not all(self.available(db, fd, path) for path in proposal.paths):
                    return Publication("collision", proposal=self.candidate(db, fd, proposal.parent, result, proposal.formats),
                                       message="An output path became occupied. Review the new paths.")
                return self.write_files(db, fd, result, proposal)
        except OSError:
            raise ExportError("The output directory is unavailable. Check its path and permissions.") from None

    def write_files(self, db, fd, result, proposal):
        group_id = uuid4().hex
        formats = proposal.formats
        group = (group_id, time(), result["source_name"], str(proposal.parent), *proposal.identity, len(formats))
        stages = [f".pageparse-{uuid4().hex}.{fmt}.stage" for fmt in formats]
        with db:
            db.execute(
                "INSERT INTO export_groups(id,created,source_name,parent,parent_dev,parent_ino,state,expected_files) "
                "VALUES(?,?,?,?,?,?,'pending',?)", group,
            )
            db.executemany(
                "INSERT INTO export_files(group_id,format,staging,final,absolute_path) VALUES(?,?,?,?,?)",
                [(group_id, fmt, stage, path.name, str(path))
                 for fmt, stage, path in zip(formats, stages, proposal.paths)],
            )

        try:
            for fmt, stage in zip(formats, stages):
                self.write_stage(db, fd, group_id, fmt, stage, SERIALIZERS[fmt], result)
            for row in self.files(db, group_id):
                if verify(fd, row["staging"], (row["stage_dev"], row["stage_ino"]), row, stage=True) != "verified":
                    raise OSError
                os.link(row["staging"], row["final"], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                with db:
                    db.execute(
                        "UPDATE export_files SET final_dev=stage_dev,final_ino=stage_ino,status='verified' "
                        "WHERE group_id=? AND format=?", (group_id, row["format"]),
                    )
        except OSError as error:
            # If a final appeared after the preflight, preserve it and propose new paths.
            collision = isinstance(error, FileExistsError)
            for row in self.files(db, group_id):
                if row["status"] == "verified":
                    verify(fd, row["final"], (row["stage_dev"], row["stage_ino"]), row, remove=True)
            state, paths, staging = self.resolve(db, fd, group_id)
            if collision and not paths and not staging:
                return Publication("collision", proposal=self.candidate(db, fd, proposal.parent, result, proposal.formats),
                                   message="An output path became occupied. Review the new paths.")
            return Publication("incomplete" if paths or staging else "error", paths, staging,
                               message="Could not publish the selected files safely. Check the destination and retry.")

        state, paths, staging = self.resolve(db, fd, group_id)
        if state == "saved":
            return Publication("saved", paths)
        message = "Check the recorded output paths and any remaining staging files."
        return Publication(state or "error", paths, staging, message=message)

    @staticmethod
    def write_stage(db, fd, group_id, fmt, name, serialize, result):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(name, flags, 0o600, dir_fd=fd), "wb") as handle:
            stage_id = identity(os.fstat(handle.fileno()))
            with db:
                db.execute("UPDATE export_files SET stage_dev=?,stage_ino=? WHERE group_id=? AND format=?",
                           (*stage_id, group_id, fmt))
            data = serialize(result).encode("utf-8")
            handle.write(data)
            handle.flush()
            info = os.fstat(handle.fileno())
            with db:
                db.execute(
                    "UPDATE export_files SET digest=?,size=?,mtime_ns=? WHERE group_id=? AND format=?",
                    (hashlib.sha256(data).hexdigest(), info.st_size, info.st_mtime_ns, group_id, fmt),
                )

    @staticmethod
    def files(db, group_id):
        return db.execute("SELECT * FROM export_files WHERE group_id=? ORDER BY format DESC", (group_id,)).fetchall()

    @staticmethod
    def group_state(db, group_id):
        rows = ExportStore.files(db, group_id)
        if not rows:
            db.execute("DELETE FROM export_groups WHERE id=?", (group_id,))
            return None
        if any(row["status"] != "verified" or row["stage_state"] != "resolved" for row in rows):
            state = "attention required"
        else:
            expected = db.execute("SELECT expected_files FROM export_groups WHERE id=?", (group_id,)).fetchone()[0]
            state = "saved" if len(rows) == expected else "incomplete"
        db.execute("UPDATE export_groups SET state=? WHERE id=?", (state, group_id))
        return state

    def resolve(self, db, fd, group_id):
        paths, staging = [], []
        with db:
            for row in self.files(db, group_id):
                key = group_id, row["format"]
                stage_id = row["stage_dev"], row["stage_ino"]
                final_id = (row["final_dev"], row["final_ino"]) if row["final_dev"] is not None else stage_id
                final = verify(fd, row["final"], final_id, row)
                stage = "missing" if row["stage_state"] == "resolved" else verify(
                    fd, row["staging"], stage_id, row, remove=True, stage=True,
                )
                stage_state = "resolved" if stage in ("deleted", "missing") else stage
                if final == "missing" and stage_state == "resolved":
                    db.execute("DELETE FROM export_files WHERE group_id=? AND format=?", key)
                    continue
                if final == "verified":
                    paths.append(Path(row["absolute_path"]))
                if stage_state != "resolved":
                    staging.append(Path(row["absolute_path"]).with_name(row["staging"]))
                db.execute(
                    "UPDATE export_files SET status=?,stage_state=?,final_dev=?,final_ino=? "
                    "WHERE group_id=? AND format=?",
                    (final, stage_state, *(final_id if final == "verified" else (row["final_dev"], row["final_ino"])), *key),
                )
            state = self.group_state(db, group_id)
        return state, tuple(paths), tuple(staging)

    def recover(self):
        with self.locked() as db:
            groups = db.execute(
                "SELECT * FROM export_groups WHERE state IN ('pending','attention required')",
            ).fetchall()
            for group in groups:
                try:
                    with directory(Path(group["parent"])) as fd:
                        if identity(os.fstat(fd)) != (group["parent_dev"], group["parent_ino"]):
                            raise OSError
                        self.resolve(db, fd, group["id"])
                except OSError:
                    with db:
                        db.execute("UPDATE export_groups SET state='attention required' WHERE id=?", (group["id"],))

    def list(self):
        with self.locked() as db:
            groups = db.execute("SELECT * FROM export_groups ORDER BY created DESC").fetchall()
            for group in groups:
                if group["state"] != "pending":
                    self.forget_missing(db, group)
            groups = db.execute("SELECT * FROM export_groups ORDER BY created DESC").fetchall()
            return [dict(group, files=[dict(row) for row in self.files(db, group["id"])]) for group in groups]

    def forget_missing(self, db, group):
        """Drop absent paths only after opening their original directory."""
        missing = []
        try:
            with directory(Path(group["parent"])) as fd:
                if identity(os.fstat(fd)) != (group["parent_dev"], group["parent_ino"]):
                    return
                for row in self.files(db, group["id"]):
                    if row["stage_state"] != "resolved":
                        continue
                    try:
                        os.stat(row["final"], dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        missing.append((group["id"], row["format"]))
                    except OSError:
                        continue
        except OSError:
            return
        if missing:
            with db:
                db.executemany("DELETE FROM export_files WHERE group_id=? AND format=?", missing)
                self.group_state(db, group["id"])

    def delete(self, requests):
        outcomes = []
        with self.locked() as db:
            for group_id, fmt in requests:
                row = db.execute(
                    "SELECT f.*,g.parent,g.parent_dev,g.parent_ino FROM export_files f "
                    "JOIN export_groups g ON g.id=f.group_id WHERE f.group_id=? AND f.format=?",
                    (group_id, fmt),
                ).fetchone()
                if row is None:
                    continue
                try:
                    with directory(Path(row["parent"])) as fd:
                        if identity(os.fstat(fd)) != (row["parent_dev"], row["parent_ino"]):
                            raise OSError
                        final_id = row["final_dev"], row["final_ino"]
                        status = verify(fd, row["final"], final_id, row, remove=True)
                except OSError:
                    status = "original directory unavailable"
                with db:
                    if status in ("missing", "deleted") and row["stage_state"] == "resolved":
                        db.execute("DELETE FROM export_files WHERE group_id=? AND format=?", (group_id, fmt))
                    else:
                        db.execute("UPDATE export_files SET status=? WHERE group_id=? AND format=?",
                                   ("missing" if status == "deleted" else status, group_id, fmt))
                    self.group_state(db, group_id)
                outcomes.append(Outcome(Path(row["absolute_path"]), status))
        return outcomes
