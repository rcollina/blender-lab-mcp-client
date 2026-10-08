import pytest

from blender_lab_mcp_client import server as srv
from blender_lab_mcp_client.config import Settings
from blender_lab_mcp_client.server import BlenderConnection


@pytest.fixture(autouse=True)
def _clean_jobs():
    srv._FAKE_JOBS.clear()
    yield
    srv._FAKE_JOBS.clear()


@pytest.fixture
def conn(fake_addon, stub_bpy):
    fake_addon.run_locally = True
    return BlenderConnection(fake_addon.host, fake_addon.port)


async def test_bridge_async_is_synchronous_and_already_done(conn):
    job = await conn.send_command("python.execute_async", {"code": "__result__ = 7"})
    status = await conn.send_command("job.status", {"job_id": job["job_id"]})
    assert status["status"] == "succeeded"
    assert status["result"]["result"] == 7


async def test_bridge_async_failure_is_reported_failed(conn):
    job = await conn.send_command("python.execute_async", {"code": "raise RuntimeError('x')"})
    status = await conn.send_command("job.status", {"job_id": job["job_id"]})
    assert status["status"] == "failed"


async def test_cancel_does_not_rewrite_finished_job(conn):
    job = await conn.send_command("python.execute_async", {"code": "pass"})
    out = await conn.send_command("job.cancel", {"job_id": job["job_id"]})
    assert out["status"] == "succeeded"


async def test_fake_jobs_are_bounded_to_last_100(conn):
    ids = []
    for i in range(srv._MAX_FAKE_JOBS + 5):
        ids.append((await conn.send_command("python.execute_async", {"code": f"__result__ = {i}"}))["job_id"])
    assert len(srv._FAKE_JOBS) == 100
    assert list(srv._FAKE_JOBS) == ids[-100:]
    with pytest.raises(RuntimeError, match="Unknown job_id"):
        await conn.send_command("job.status", {"job_id": ids[0]})


async def test_job_list_tolerates_unreachable_blender(mcp_session, fake_addon):
    settings = Settings(host=fake_addon.host, port=fake_addon.port)
    fake_addon.drop = True  # bridge unreachable -> ConnectionError -> swallowed
    async with mcp_session(settings) as (_, call):
        is_err, out = await call("blender_job_list")
    assert not is_err and out == {"jobs": []}


async def test_unknown_transport_is_rejected(mcp_session, fake_addon):
    settings = Settings(host=fake_addon.host, port=fake_addon.port)
    async with mcp_session(settings) as (_, call):
        is_err, text = await call("blender_python_exec", code="x = 1", transport="Headless")
    assert is_err and "Unknown transport" in text
    assert fake_addon.requests == []
