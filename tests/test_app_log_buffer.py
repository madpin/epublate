"""Tests for the in-memory ring-buffer log handler."""

from __future__ import annotations

import logging

import pytest

from epublate.app.log_buffer import (
    DEFAULT_CAPACITY,
    RingBufferHandler,
    filter_records,
    install_ring_buffer,
    uninstall_ring_buffer,
)


def test_handler_default_capacity() -> None:
    handler = RingBufferHandler()
    assert handler.capacity == DEFAULT_CAPACITY


def test_handler_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError):
        RingBufferHandler(capacity=0)


def test_handler_records_basic_emit() -> None:
    handler = RingBufferHandler(capacity=5)
    record = logging.LogRecord(
        name="epublate.test",
        level=logging.INFO,
        pathname="t.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    handler.emit(record)
    snap = handler.records()
    assert len(snap) == 1
    assert snap[0] is record
    assert snap[0].message == "hello world"


def test_handler_wraparound_drops_oldest() -> None:
    handler = RingBufferHandler(capacity=3)
    for i in range(5):
        rec = logging.LogRecord(
            name="epublate.test",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg=f"event-{i}",
            args=(),
            exc_info=None,
        )
        handler.emit(rec)
    snap = handler.records()
    assert [r.getMessage() for r in snap] == ["event-2", "event-3", "event-4"]


def test_handler_records_returns_snapshot_copy() -> None:
    """Mutating the snapshot must not affect the handler's buffer."""

    handler = RingBufferHandler(capacity=5)
    handler.emit(_make_record("one"))
    snap = handler.records()
    snap.clear()
    assert len(handler.records()) == 1


def test_handler_resilient_to_args_interpolation_failure() -> None:
    """A %-format mismatch shouldn't drop the record."""

    handler = RingBufferHandler(capacity=5)
    rec = logging.LogRecord(
        name="epublate.test",
        level=logging.INFO,
        pathname="t.py",
        lineno=1,
        msg="bad %s %s",
        args=("only-one",),
        exc_info=None,
    )
    handler.emit(rec)
    snap = handler.records()
    assert len(snap) == 1
    # The pre-rendered ``message`` falls back to the raw msg when
    # args interpolation explodes, so the record is still inspectable.
    assert "bad" in snap[0].message


def test_handler_clear() -> None:
    handler = RingBufferHandler(capacity=5)
    handler.emit(_make_record("one"))
    handler.emit(_make_record("two"))
    handler.clear()
    assert handler.records() == []


def test_install_and_uninstall_round_trip() -> None:
    """Install attaches to the package logger; uninstall detaches."""

    logger = logging.getLogger("epublate.test_install")
    handler = install_ring_buffer(logger, capacity=10, level=logging.DEBUG)
    try:
        assert handler in logger.handlers
        logger.info("hi")
        assert any("hi" in r.getMessage() for r in handler.records())
    finally:
        uninstall_ring_buffer(handler, logger)
        assert handler not in logger.handlers


def test_install_lowers_logger_level_when_handler_more_verbose() -> None:
    """If the logger's level would suppress the records the handler
    wants, ``install_ring_buffer`` lowers it."""

    logger = logging.getLogger("epublate.test_level")
    logger.setLevel(logging.WARNING)
    try:
        handler = install_ring_buffer(logger, level=logging.DEBUG)
        try:
            assert logger.level == logging.DEBUG
        finally:
            uninstall_ring_buffer(handler, logger)
    finally:
        # restore for the next test invocation
        logger.setLevel(logging.NOTSET)


def test_filter_records_min_level() -> None:
    records = [
        _make_record("debug-1", level=logging.DEBUG),
        _make_record("info-1", level=logging.INFO),
        _make_record("warn-1", level=logging.WARNING),
        _make_record("err-1", level=logging.ERROR),
    ]
    out = filter_records(records, min_level=logging.WARNING)
    assert [r.getMessage() for r in out] == ["warn-1", "err-1"]


def test_filter_records_substring_search_is_case_insensitive() -> None:
    records = [
        _make_record("Provider returned 429"),
        _make_record("Translated segment 12 OK"),
        _make_record("provider failed twice"),
    ]
    out = filter_records(records, needle="PROVIDER")
    assert {r.getMessage() for r in out} == {
        "Provider returned 429",
        "provider failed twice",
    }


def test_filter_records_logger_prefix() -> None:
    records = [
        _make_record("a", logger_name="epublate.batch"),
        _make_record("b", logger_name="epublate.llm.openai_compat"),
        _make_record("c", logger_name="epublate.batch.sub"),
    ]
    out = filter_records(records, logger_prefix="epublate.batch")
    assert [r.getMessage() for r in out] == ["a", "c"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    msg: str,
    *,
    level: int = logging.INFO,
    logger_name: str = "epublate.test",
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname="t.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    # Ensure ``message`` is populated (matches what the handler does).
    rec.message = rec.getMessage()
    return rec
