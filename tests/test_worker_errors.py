import asyncio

import httpx
from arq.worker import Retry

from app.worker.errors import serializable_job_errors


async def test_third_party_exception_is_rewrapped_as_runtime_error():
    # An httpx error pickled into Redis would blow up any consumer without
    # httpx installed (e.g. the arq-ui container) — it must come out as a
    # builtin carrying the original type name.
    @serializable_job_errors
    async def job(ctx):
        raise httpx.ConnectError("connection refused")

    try:
        await job({})
    except RuntimeError as exc:
        assert "ConnectError" in str(exc)
        assert "connection refused" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


async def test_builtin_exception_passes_through_unwrapped():
    @serializable_job_errors
    async def job(ctx):
        raise ValueError("plain builtin")

    try:
        await job({})
    except ValueError as exc:
        assert str(exc) == "plain builtin"
    else:
        raise AssertionError("expected ValueError")


async def test_arq_retry_signal_passes_through():
    # Retry is arq's control-flow signal — wrapping it would silently disable
    # the queue-level retry it requests.
    @serializable_job_errors
    async def job(ctx):
        raise Retry(defer=5)

    try:
        await job({})
    except Retry:
        pass
    else:
        raise AssertionError("expected Retry")


async def test_cancellation_passes_through():
    @serializable_job_errors
    async def job(ctx):
        raise asyncio.CancelledError()

    try:
        await job({})
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("expected CancelledError")


async def test_successful_result_is_returned():
    @serializable_job_errors
    async def job(ctx):
        return 42

    assert await job({}) == 42
