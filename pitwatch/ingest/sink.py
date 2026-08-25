"""Where readings go once something has read them.

Two jobs, kept apart from the device code so that neither reader has to know
anything about Postgres:

* Batch the clamp samples. The Shelly pushes about once a second per channel,
  which is not much, but one INSERT per reading is a round trip per reading
  forever and there is no reason to pay it. Samples are queued and written in
  groups.
* Keep the latest reading in memory. The dashboard, the run detector and the
  alert rules all want "what is happening right now" and none of them should be
  querying a hypertable to find out.

The queue is bounded. If the database is unreachable, the choice is between
dropping readings and growing until the container is killed, and a monitor that
dies during the incident it was bought for is the worse of the two. Drops are
counted and logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg

from pitwatch.ingest.shelly import EmSample

log = logging.getLogger(__name__)

QUEUE_LIMIT = 10_000
FLUSH_INTERVAL_S = 1.0
FLUSH_BATCH = 500


@dataclass
class LiveState:
    """The most recent reading from each clamp, and when it arrived.

    Held in memory on purpose. It is rebuilt from the database at startup so a
    restart does not blank the dashboard, and after that it is only ever
    written by the ingest tasks.
    """

    samples: dict[int, EmSample] = field(default_factory=dict)
    updated_at: datetime | None = None

    def update(self, sample: EmSample) -> None:
        previous = self.samples.get(sample.channel)
        # Delta notifications carry only what changed, so a frame with a
        # voltage and no current would otherwise wipe out the current. Carry
        # forward the fields this frame did not mention.
        if previous is not None:
            sample = EmSample(
                ts=sample.ts,
                channel=sample.channel,
                current=sample.current if sample.current is not None else previous.current,
                voltage=sample.voltage if sample.voltage is not None else previous.voltage,
                act_power=sample.act_power if sample.act_power is not None else previous.act_power,
                aprt_power=sample.aprt_power
                if sample.aprt_power is not None
                else previous.aprt_power,
                pf=sample.pf if sample.pf is not None else previous.pf,
                freq=sample.freq if sample.freq is not None else previous.freq,
            )
        self.samples[sample.channel] = sample
        self.updated_at = datetime.now(UTC)

    def current_for(self, channel: int) -> float | None:
        sample = self.samples.get(channel)
        return sample.current if sample else None


class SampleSink:
    def __init__(self, pool: asyncpg.Pool, live: LiveState) -> None:
        self._pool = pool
        self._live = live
        self._queue: asyncio.Queue[EmSample] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._dropped = 0
        self._written = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def written(self) -> int:
        return self._written

    async def submit(self, samples: list[EmSample]) -> None:
        for sample in samples:
            self._live.update(sample)
            try:
                self._queue.put_nowait(sample)
            except asyncio.QueueFull:
                self._dropped += 1
                if self._dropped % 100 == 1:
                    log.error(
                        "Sample queue is full, dropping readings (%d so far). "
                        "The database is not keeping up or is unreachable.",
                        self._dropped,
                    )

    async def run(self, stop: asyncio.Event) -> None:
        """Drain the queue into the database until stop is set."""
        while not stop.is_set():
            batch = await self._collect(stop)
            if batch:
                await self._write(batch)
        # Whatever is still queued at shutdown is worth one last attempt.
        remaining = self._drain()
        if remaining:
            await self._write(remaining)

    async def _collect(self, stop: asyncio.Event) -> list[EmSample]:
        """Wait for something, then take whatever else has piled up behind it."""
        waiter = asyncio.ensure_future(self._queue.get())
        stopper = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, stopper}, timeout=FLUSH_INTERVAL_S, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (waiter, stopper):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        if waiter not in done:
            return []
        batch = [waiter.result()]
        batch.extend(self._drain(limit=FLUSH_BATCH - 1))
        return batch

    def _drain(self, limit: int = QUEUE_LIMIT) -> list[EmSample]:
        batch = []
        while len(batch) < limit:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _write(self, batch: list[EmSample]) -> None:
        rows = [
            (s.ts, s.channel, s.current, s.voltage, s.act_power, s.aprt_power, s.pf, s.freq)
            for s in batch
        ]
        try:
            await self._pool.executemany(
                """
                INSERT INTO em_sample (ts, channel, current, voltage, act_power, aprt_power, pf, freq)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                rows,
            )
        except (asyncpg.PostgresError, OSError) as error:
            # Not requeued. The batch is already the oldest data in the system,
            # and putting it back would push out the readings arriving now,
            # which are the ones somebody is looking at.
            log.error("Could not write %d sample(s): %s", len(rows), error)
            return
        self._written += len(rows)

    async def prime(self) -> None:
        """Fill the live state from the database so a restart is not a blank page."""
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (channel) ts, channel, current, voltage, act_power, aprt_power, pf, freq
            FROM em_sample
            WHERE ts > now() - interval '1 hour'
            ORDER BY channel, ts DESC
            """
        )
        for row in rows:
            self._live.update(
                EmSample(
                    ts=row["ts"],
                    channel=row["channel"],
                    current=row["current"],
                    voltage=row["voltage"],
                    act_power=row["act_power"],
                    aprt_power=row["aprt_power"],
                    pf=row["pf"],
                    freq=row["freq"],
                )
            )
        if rows:
            log.info("Primed the live state from %d recent reading(s)", len(rows))


async def record_device_status(
    pool: asyncpg.Pool, device: str, online: bool, error: str | None
) -> None:
    await pool.execute(
        """
        INSERT INTO device_status (device, online, last_seen, last_error, updated_at)
        VALUES ($1, $2, CASE WHEN $2 THEN now() END, $3, now())
        ON CONFLICT (device) DO UPDATE SET
            online     = excluded.online,
            last_seen  = COALESCE(excluded.last_seen, device_status.last_seen),
            last_error = excluded.last_error,
            updated_at = now()
        """,
        device,
        online,
        error,
    )
