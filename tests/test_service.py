import asyncio
import copy
import threading

import pytest

from bugalert.app import event_stream
from bugalert.config import load_settings
from bugalert.feed import FeedError, validate_feed
from bugalert.service import MonitorService


def test_refresh_adds_matching_alerts_and_preserves_last_good_on_failure(
    write_config, feed_payload, bug_factory, client_factory, clock
):
    updated = copy.deepcopy(feed_payload)
    updated["bugs"].append(bug_factory(999, cve="CVE-2026-99999"))
    client = client_factory(updated, FeedError("Synthetic upstream failure"))

    async def scenario():
        service = MonitorService(load_settings(write_config()), client, clock)
        await service.start()
        assert service.dashboard()["summary"]["new"] == 0
        clock.advance(minutes=1)
        assert await service.refresh() is True
        data = service.dashboard()
        assert data["summary"]["total"] == 3
        assert data["summary"]["new"] == 1
        assert data["feed"]["source"] == "arista"
        assert data["feed"]["last_success_at"] == clock().isoformat()
        clock.advance(minutes=1)
        assert await service.refresh() is False
        assert service.dashboard()["alerts"] == data["alerts"]
        assert service.dashboard()["feed"]["error"] == "Synthetic upstream failure"
        assert service.dashboard()["feed"]["last_success_at"] == data["feed"]["last_success_at"]

    asyncio.run(scenario())


def test_missing_token_retains_seed_and_is_explicit(write_config, clock):
    async def scenario():
        service = MonitorService(load_settings(write_config()), clock=clock)
        await service.start()
        assert await service.refresh() is False
        data = service.dashboard()
        assert data["summary"]["total"] == 2
        assert data["feed"]["error_code"] == "missing_token"
        assert data["feed"]["last_success_at"] is None
        assert data["feed"]["source"] == "seed"

    asyncio.run(scenario())


def test_config_changes_reload_without_marking_existing_alerts_new(write_config, clock):
    async def scenario():
        path = write_config(["4.31.1M"])
        service = MonitorService(load_settings(path), clock=clock)
        await service.start()
        assert service.dashboard()["summary"]["total"] == 1
        write_config(["4.32.1F"])
        assert await service.reload_configuration()
        assert service.dashboard()["summary"]["total"] == 2
        assert service.dashboard()["summary"]["new"] == 0
        path.write_text("[eos]\nversions = [", encoding="utf-8")
        assert not await service.reload_configuration()
        assert service.dashboard()["config"]["error"]
        assert service.settings.versions == ("4.32.1F",)
        write_config(["4.31.1M"])
        assert await service.reload_configuration()
        assert service.dashboard()["config"]["error"] is None

    asyncio.run(scenario())


def test_unknown_versions_are_unevaluated_not_zero_count(write_config, clock):
    async def scenario():
        service = MonitorService(load_settings(write_config(["4.99.1M"])), clock=clock)
        await service.start()
        assert service.dashboard()["releases"] == [
            {"version": "4.99.1M", "known": False, "alert_count": None, "security_count": None}
        ]

    asyncio.run(scenario())


def test_new_markers_persist_and_expire_without_changing_first_seen(
    write_config, feed_payload, bug_factory, client_factory, clock
):
    updated = copy.deepcopy(feed_payload)
    updated["bugs"].append(bug_factory(999))
    path = write_config()

    async def scenario():
        service = MonitorService(load_settings(path), client_factory(updated), clock)
        await service.start()
        clock.advance(minutes=1)
        await service.refresh()
        assert service.dashboard()["summary"]["new"] == 1
        first_seen = service.stored.observations[999].first_seen
        restarted = MonitorService(load_settings(path), client_factory(updated), clock)
        await restarted.start()
        assert restarted.dashboard()["summary"]["new"] == 1
        clock.advance(hours=73)
        assert restarted.dashboard()["summary"]["new"] == 0
        assert restarted.stored.observations[999].first_seen == first_seen
        assert restarted.client.calls == 0  # Expiry does not require an upstream check.

    asyncio.run(scenario())


def test_overlapping_refreshes_are_coalesced(write_config, feed_payload, clock):
    entered, release = threading.Event(), threading.Event()

    class BlockingClient:
        calls = 0

        def fetch(self, settings):
            self.calls += 1
            entered.set()
            if not release.wait(timeout=5):
                raise FeedError("Test release timed out")
            return validate_feed(feed_payload)

    async def scenario():
        client = BlockingClient()
        service = MonitorService(load_settings(write_config()), client, clock)
        await service.start()
        task = asyncio.create_task(service.refresh())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await service.refresh() is None
        finally:
            release.set()
        assert await task is True
        assert client.calls == 1

    asyncio.run(scenario())


def test_scheduler_refreshes_without_any_browser(
    write_config, feed_payload, client_factory, clock, monkeypatch
):
    async def scenario():
        client = client_factory(feed_payload)
        service = MonitorService(load_settings(write_config()), client, clock)
        await service.start()

        async def advance_sleep(seconds):
            clock.advance(seconds=seconds)
            if client.calls >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr("bugalert.service.asyncio.sleep", advance_sleep)
        with pytest.raises(asyncio.CancelledError):
            await service.run()
        assert client.calls == 2
        assert service.dashboard()["feed"]["source"] == "arista"

    asyncio.run(scenario())


def test_sse_delivers_revisions_heartbeats_and_cleans_up(
    write_config, feed_payload, client_factory, clock
):
    async def scenario():
        service = MonitorService(load_settings(write_config()), client_factory(feed_payload), clock)
        await service.start()
        stream = event_stream(service, heartbeat_seconds=0.001)
        first = await anext(stream)
        assert f'"revision": {service.revision}' in first
        assert "event: update" in first
        assert await anext(stream) == ": keepalive\n\n"
        await service.refresh()
        # Multiple status updates are coalesced to the most recent revision.
        assert f'"revision": {service.revision}' in await anext(stream)
        await stream.aclose()
        assert not service._subscribers

    asyncio.run(scenario())


def test_shutdown_wakes_all_streams_and_overrides_pending_updates(
    write_config, feed_payload, client_factory, clock
):
    async def scenario():
        service = MonitorService(load_settings(write_config()), client_factory(feed_payload), clock)
        await service.start()
        streams = [event_stream(service, heartbeat_seconds=3600) for _ in range(3)]
        for stream in streams:
            await anext(stream)
        await service.refresh()
        assert all(queue.full() for queue in service._subscribers)

        service.begin_shutdown()
        service.begin_shutdown()  # Cleanup is safe to call again during lifespan.
        service._publish()  # A finishing refresh must not overwrite shutdown.
        for stream in streams:
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=1)
        assert not service._subscribers

        late_stream = event_stream(service)
        with pytest.raises(StopAsyncIteration):
            await anext(late_stream)
        assert not service._subscribers

    asyncio.run(scenario())


def test_shutdown_wakes_an_idle_stream_without_waiting_for_a_heartbeat(write_config, clock):
    async def scenario():
        service = MonitorService(load_settings(write_config()), clock=clock)
        await service.start()
        stream = event_stream(service, heartbeat_seconds=3600)
        await anext(stream)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)  # Let the stream block on its empty subscription queue.
        service.begin_shutdown()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, timeout=1)
        assert not service._subscribers

    asyncio.run(scenario())


def test_stream_cancellation_still_propagates_and_cleans_up(write_config, clock):
    async def scenario():
        service = MonitorService(load_settings(write_config()), clock=clock)
        await service.start()
        stream = event_stream(service)
        await anext(stream)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not service._subscribers

    asyncio.run(scenario())


def test_shutdown_does_not_start_a_new_feed_request(write_config, client_factory, feed_payload):
    async def scenario():
        client = client_factory(feed_payload)
        service = MonitorService(load_settings(write_config()), client)
        await service.start()
        service.begin_shutdown()
        assert await service.refresh() is None
        assert client.calls == 0

    asyncio.run(scenario())


def test_missing_seed_allows_first_live_download(write_config, feed_payload, client_factory, clock):
    async def scenario():
        service = MonitorService(
            load_settings(write_config(seed_file="missing.json")),
            client_factory(feed_payload),
            clock,
        )
        await service.start()
        assert service.dashboard()["summary"]["total"] == 0
        assert service.dashboard()["warnings"]
        assert await service.refresh()
        assert service.dashboard()["summary"]["total"] == 2
        assert service.dashboard()["summary"]["new"] == 0
        assert not service.dashboard()["warnings"]

    asyncio.run(scenario())


def test_cache_is_used_when_seed_has_been_removed(
    write_config, feed_payload, client_factory, clock
):
    async def scenario():
        path = write_config()
        service = MonitorService(load_settings(path), client_factory(feed_payload), clock)
        await service.start()
        await service.refresh()
        (path.parent / "seed.json").unlink()
        restarted = MonitorService(load_settings(path), clock=clock)
        await restarted.start()
        assert restarted.dashboard()["summary"]["total"] == 2
        assert restarted.dashboard()["feed"]["source"] == "arista"
        assert await restarted.refresh() is False  # Missing credential does not discard the cache.
        assert restarted.dashboard()["feed"]["last_success_at"] == clock().isoformat()

    asyncio.run(scenario())


def test_database_path_change_requires_restart(write_config, clock):
    async def scenario():
        service = MonitorService(load_settings(write_config()), clock=clock)
        await service.start()
        write_config(database_file="different.sqlite3")
        assert not await service.reload_configuration()
        assert "restart" in service.dashboard()["config"]["error"]
        assert service.settings.database_file.name == "state.sqlite3"

    asyncio.run(scenario())


def test_storage_failure_keeps_the_last_displayed_feed(
    write_config, feed_payload, bug_factory, client_factory, clock, monkeypatch
):
    updated = copy.deepcopy(feed_payload)
    updated["bugs"].append(bug_factory(999))

    async def scenario():
        service = MonitorService(load_settings(write_config()), client_factory(updated), clock)
        await service.start()
        before = service.dashboard()["alerts"]

        def failed_save(*args):
            raise OSError("Synthetic read-only database")

        monkeypatch.setattr(service.store, "save", failed_save)
        assert await service.refresh() is False
        assert service.dashboard()["alerts"] == before
        assert service.dashboard()["feed"]["error_code"] == "storage_error"
        assert not service.dashboard()["feed"]["refreshing"]

    asyncio.run(scenario())
