"""One shared background poller, independent of connected browser sessions."""

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from bugalert.catalog import Catalog
from bugalert.config import ConfigError, Settings, load_settings
from bugalert.feed import AristaClient, FeedError, MissingTokenError, load_seed
from bugalert.storage import StateStore, StoredFeed

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        client: AristaClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.client = client or AristaClient()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.store = StateStore(settings.database_file)
        self.stored: StoredFeed | None = None
        self.catalog = Catalog(None, settings, self.clock())
        self.revision = 0
        self.refreshing = False
        self.stopping = False
        self.last_attempt_at: str | None = None
        self.next_refresh_at = self.clock()
        self.error: str | None = None
        self.error_code: str | None = None
        self.config_error: str | None = None
        self.load_warning: str | None = None
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[int | None]] = set()

    def begin_shutdown(self) -> None:
        """Wake every event stream so it can finish its HTTP response normally."""
        if self.stopping:
            return
        self.stopping = True
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize)
        try:
            self.stored = await asyncio.to_thread(self.store.load)
        except FeedError:
            self.load_warning = "The cached feed was invalid; the local seed was used instead."
        if self.stored is None:
            try:
                feed = await asyncio.to_thread(load_seed, self.settings.seed_file)
                self.stored = await asyncio.to_thread(self.store.save, feed, self.clock(), "seed")
            except FeedError as error:
                self.load_warning = str(error)
        self._rebuild()
        self._publish()

    def _rebuild(self) -> None:
        self.catalog = Catalog(self.stored, self.settings, self.clock())

    def _publish(self) -> None:
        if self.stopping:
            return
        self.revision += 1
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self.revision)

    def _expire_new_markers(self) -> None:
        if self.catalog.next_expiry and self.clock() >= self.catalog.next_expiry:
            self._rebuild()
            self._publish()

    def dashboard(self) -> dict:
        self._expire_new_markers()
        stored = self.stored
        return {
            "revision": self.revision,
            "summary": self.catalog.summary,
            "alerts": self.catalog.alerts,
            "releases": self.catalog.releases,
            "feed": {
                "generation_id": stored.feed.generation_id if stored else None,
                "release_date": stored.feed.release_date if stored else None,
                "source": stored.source if stored else None,
                "total_alerts": len(stored.feed.bugs) if stored else 0,
                "total_eos_alerts": self.catalog.total_eos,
                "loaded_at": stored.fetched_at if stored else None,
                "last_success_at": (
                    stored.fetched_at if stored and stored.source == "arista" else None
                ),
                "last_attempt_at": self.last_attempt_at,
                "next_refresh_at": self.next_refresh_at.isoformat(),
                "refreshing": self.refreshing,
                "error": self.error,
                "error_code": self.error_code,
                "token_configured": bool(os.environ.get(self.settings.token_env, "").strip()),
            },
            "config": {
                "path": str(self.settings.config_path),
                "versions": list(self.settings.versions),
                "refresh_seconds": self.settings.refresh_seconds,
                "new_window_hours": self.settings.new_window_hours,
                "token_env": self.settings.token_env,
                "error": self.config_error,
            },
            "warnings": [self.load_warning] if self.load_warning else [],
        }

    async def reload_configuration(self) -> bool:
        previous_error = self.config_error
        try:
            settings = await asyncio.to_thread(load_settings, self.settings.config_path)
            if settings.database_file != self.settings.database_file:
                raise ConfigError("Changing feed.database_file requires a server restart.")
            self.config_error = None
        except ConfigError as error:
            self.config_error = str(error)
            if previous_error != self.config_error:
                self._publish()
            return False
        if settings != self.settings:
            self.settings = settings
            self.next_refresh_at = min(
                self.next_refresh_at, self.clock() + timedelta(seconds=settings.refresh_seconds)
            )
            self._rebuild()
            self._publish()
        elif previous_error:
            self._publish()
        return True

    async def refresh(self) -> bool | None:
        # Coalesce browser requests and the scheduler into one upstream request.
        if self.stopping or self._lock.locked():
            return None
        async with self._lock:
            await self.reload_configuration()
            if self.stopping:
                return None
            self.refreshing = True
            self.last_attempt_at = self.clock().isoformat()
            self._publish()
            success = False
            try:
                feed = await asyncio.to_thread(self.client.fetch, self.settings)
                stored = await asyncio.to_thread(self.store.save, feed, self.clock(), "arista")
                self.stored = stored
                self.error = self.error_code = None
                self.load_warning = None
                success = True
            except MissingTokenError as error:
                self.error, self.error_code = str(error), "missing_token"
            except FeedError as error:
                self.error, self.error_code = str(error), "fetch_error"
                logger.warning("Feed refresh failed: %s", error)
            except OSError, sqlite3.Error:
                self.error = "Cannot persist the downloaded feed. The previous feed is retained."
                self.error_code = "storage_error"
                logger.exception("Cannot persist the feed")
            except Exception:
                self.error = "An unexpected refresh error occurred. The previous feed is retained."
                self.error_code = "refresh_error"
                logger.exception("Unexpected feed refresh failure")
            finally:
                self.refreshing = False
                self.next_refresh_at = self.clock() + timedelta(
                    seconds=self.settings.refresh_seconds
                )
                self._rebuild()
                self._publish()
            return success

    async def run(self) -> None:
        while not self.stopping:
            await self.reload_configuration()
            self._expire_new_markers()
            if self.clock() >= self.next_refresh_at:
                await self.refresh()
            # Also watch TOML changes when the upstream feed is checked infrequently.
            await asyncio.sleep(2)

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
