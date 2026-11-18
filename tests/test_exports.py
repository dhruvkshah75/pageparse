import errno
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from pageparse import exports
from pageparse.exports import ExportError, ExportStore


def result(status="complete"):
    return {
        "schema_version": 1, "source_name": "report.pdf", "source_type": "pdf",
        "model": "nemotron-parse", "status": status, "selected_pages": [1, 2],
        "pages": [{"page_number": 1, "markdown": "Café\n\n<table><tr><td>A</td></tr></table>",
                   "width_px": 200, "height_px": 200, "coordinate_frame": "content_normalized",
                   "blocks": [], "warnings": []}],
        "failure": None,
    }


@pytest.fixture
def setup(tmp_path):
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    index = tmp_path / "state/exports.sqlite3"
    return ExportStore(index), output


def save(store, dest, status="complete"):
    document = result(status)
    return store.publish(document, store.plan(document, dest))


def test_restart_partial_and_format_specific_deletion(setup):
    store, dest = setup
    (dest / "report.pdf").write_bytes(b"original")
    (dest / "neighbor.md").write_bytes(b"neighbor")
    saved = save(store, dest, "cancelled")
    assert saved.status == "saved"
    markdown, structured = saved.paths
    assert markdown.name == "report.partial.md"
    assert markdown.read_text().startswith("> Partial result")
    assert "<table>" in markdown.read_text()
    assert json.loads(structured.read_text()) == result("cancelled")

    reopened = ExportStore(store.index)
    reopened.recover()
    group, = reopened.list()
    assert group["state"] == "saved"
    assert len(group["files"]) == 2
    with sqlite3.connect(store.index) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("SELECT size FROM export_files WHERE format='md'").fetchone() == (markdown.stat().st_size,)
    assert reopened.delete([]) == []
    assert markdown.exists()
    assert reopened.delete([(group["id"], "md")])[0].status == "deleted"
    assert not markdown.exists()
    assert structured.exists()
    group, = reopened.list()
    assert [file["format"] for file in group["files"]] == ["json"]
    assert group["state"] == "incomplete"

    second = save(reopened, dest)
    assert second.status == "saved"
    newer = reopened.list()[0]
    assert [out.status for out in reopened.delete([(newer["id"], fmt) for fmt in ("md", "json")])] == ["deleted", "deleted"]
    assert (dest / "report.pdf").read_bytes() == b"original"
    assert (dest / "neighbor.md").read_bytes() == b"neighbor"


def test_collision_after_confirmation(setup):
    store, dest = setup
    doc = result("cancelled")
    first = store.plan(doc, dest)
    first.paths[1].write_text("external")
    (dest / "report.partial-2.md").symlink_to("absent")
    collision = store.publish(doc, first)
    assert collision.status == "collision"
    assert [p.name for p in collision.proposal.paths] == ["report.partial-3.md", "report.partial-3.json"]
    assert not first.paths[0].exists()
    assert first.paths[1].read_text() == "external"
    assert store.list() == []
    assert store.publish(doc, collision.proposal).status == "saved"


def test_unsupported_publication_leaves_no_outputs(setup, monkeypatch):
    store, dest = setup

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "private OS detail")

    monkeypatch.setattr(exports.os, "link", unsupported)
    outcome = save(store, dest)
    assert outcome.status == "error" and not outcome.paths and not outcome.staging
    assert "private" not in outcome.message
    assert store.list() == [] and list(dest.iterdir()) == []


def test_failed_second_link_and_rollback_tracks_survivor(setup, monkeypatch):
    store, dest = setup
    link, unlink = os.link, os.unlink

    def broken_link(stage, final, **kwargs):
        if final.endswith(".json"):
            raise PermissionError
        return link(stage, final, **kwargs)

    def broken_unlink(name, **kwargs):
        if name == "report.md":
            raise PermissionError
        return unlink(name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(exports.os, "link", broken_link)
        patch.setattr(exports.os, "unlink", broken_unlink)
        outcome = save(store, dest)
    assert outcome.status == "incomplete"
    assert outcome.paths == (dest / "report.md",)
    assert outcome.paths[0].exists()
    group, = store.list()
    assert group["state"] == "incomplete"
    assert store.delete([(group["id"], "md")])[0].status == "deleted"
    assert store.list() == []


def test_untrusted_destination_preserves_stages_until_trusted(setup):
    store, dest = setup
    dest.chmod(0o770)
    proposal = store.plan(result(), dest)
    assert not proposal.trusted
    publication = store.publish(result(), proposal)
    assert publication.status == "attention required"
    assert all(path.exists() for path in (*publication.paths, *publication.staging))
    group, = store.list()
    assert store.delete([(group["id"], "md")])[0].status == "untrusted directory"
    dest.chmod(0o700)
    store.recover()
    assert not any(path.exists() for path in publication.staging)
    assert store.delete([(group["id"], "md")])[0].status == "deleted"


def test_absent_target_and_replaced_or_symlink_parent(setup):
    store, dest = setup
    md, structured = save(store, dest).paths
    group, = store.list()
    md.unlink()
    assert store.delete([(group["id"], "md")])[0].status == "missing"
    moved = dest.with_name("moved")
    dest.rename(moved)
    assert store.delete([(group["id"], "json")])[0].status == "original directory unavailable"
    dest.mkdir()
    structured.write_text("new directory")
    assert store.delete([(group["id"], "json")])[0].status == "original directory unavailable"
    structured.unlink()
    dest.rmdir()
    dest.symlink_to(moved)
    with pytest.raises(ExportError, match="symlink components"):
        store.plan(result(), dest)
    assert store.delete([(group["id"], "json")])[0].status == "original directory unavailable"
    assert (moved / structured.name).exists()
    dest.unlink()
    moved.rename(dest)
    assert store.delete([(group["id"], "json")])[0].status == "deleted"


def test_modified_or_symlink_target_is_preserved(setup):
    store, dest = setup
    md, structured = save(store, dest).paths
    neighbor = dest / "neighbor"
    neighbor.write_text("unrelated")
    info = md.stat()
    data = md.read_bytes()
    md.write_bytes(b"x" + data[1:])
    os.utime(md, ns=(info.st_atime_ns, info.st_mtime_ns))
    structured.unlink()
    structured.symlink_to(neighbor)
    group, = store.list()
    outcomes = store.delete([(group["id"], fmt) for fmt in ("md", "json")])
    assert [outcome.status for outcome in outcomes] == ["changed", "changed"]
    assert md.read_bytes().startswith(b"x")
    assert structured.is_symlink()
    assert neighbor.read_text() == "unrelated"
    assert len(store.list()[0]["files"]) == 2


def test_inaccessible_file_retry_and_final_recheck(setup, monkeypatch):
    store, dest = setup
    md, _ = save(store, dest).paths
    group, = store.list()
    original = exports.digest_file

    def inaccessible(file):
        raise PermissionError

    with monkeypatch.context() as patch:
        patch.setattr(exports, "digest_file", inaccessible)
        assert store.delete([(group["id"], "md")])[0].status == "inaccessible"

    def replaced(file):
        result = original(file)
        md.rename(md.with_suffix(".old"))
        md.write_text("replacement")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(exports, "digest_file", replaced)
        assert store.delete([(group["id"], "md")])[0].status == "changed"
    assert md.read_text() == "replacement"
    md.with_suffix(".old").replace(md)
    assert store.delete([(group["id"], "md")])[0].status == "deleted"


def interrupted_publish(index, dest, phase, started=None, release=None):
    store = ExportStore(index)
    link = os.link
    write_stage = store.write_stage
    if phase == "before identity":
        fdopen = os.fdopen

        def exit_before_record(*args, **kwargs):
            os._exit(23)

        os.fdopen = exit_before_record
    elif phase == "before content":
        def exit_before_content(db, fd, group, fmt, name, serialize, document):
            def interrupted(_):
                os._exit(23)
            write_stage(db, fd, group, fmt, name, interrupted, document)

        store.write_stage = exit_before_content
    else:
        def interrupted_link(stage, final, **kwargs):
            link(stage, final, **kwargs)
            if (phase == "first final" and final.endswith(".md")) or (
                phase == "second final" and final.endswith(".json")
            ):
                os._exit(23)
            if phase == "pause" and final.endswith(".md"):
                started.set()
                release.wait(5)

        os.link = interrupted_link
    save(store, dest)


@pytest.mark.parametrize("phase,state,count", [
    ("before identity", "attention required", 0), ("before content", None, 0),
    ("first final", "incomplete", 1), ("second final", "saved", 2),
])
def test_recovery_after_process_exit(setup, phase, state, count):
    store, dest = setup
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=interrupted_publish, args=(store.index, dest, phase))
    child.start()
    child.join(5)
    assert child.exitcode == 23
    reopened = ExportStore(store.index)
    reopened.recover()
    groups = reopened.list()
    assert len(list(dest.glob("report.*"))) == count
    assert len(list(dest.glob(".*.stage"))) == (phase == "before identity")
    if state:
        group, = groups
        assert group["state"] == state
        assert sum(file["status"] == "verified" for file in group["files"]) == count
    else:
        assert groups == []


def test_second_app_cannot_recover_mid_publish(setup):
    store, dest = setup
    context = multiprocessing.get_context("spawn")
    started, release = context.Event(), context.Event()
    child = context.Process(target=interrupted_publish, args=(store.index, dest, "pause", started, release))
    child.start()
    try:
        assert started.wait(5)
        second = ExportStore(store.index, lock_timeout=.1)
        with pytest.raises(ExportError, match="busy"):
            second.recover()
        with sqlite3.connect(store.index) as db:
            assert db.execute("SELECT state FROM export_groups").fetchone() == ("pending",)
        assert (dest / "report.md").exists() and not (dest / "report.json").exists()
    finally:
        release.set()
        child.join(5)
    assert child.exitcode == 0
    second.recover()
    assert second.list()[0]["state"] == "saved"


def test_index_unavailable(tmp_path):
    parent = tmp_path / "file"
    parent.touch()
    with pytest.raises(ExportError, match="index is unavailable"):
        ExportStore(parent / "exports.sqlite3").recover()
