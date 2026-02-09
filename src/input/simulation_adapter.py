"""Simulation adapter — replays saved signals from files for testing."""

import asyncio
import logging
from pathlib import Path

from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class SimulationAdapter(BaseAdapter):
    """Loads signal files from a directory and replays them onto the queue.

    Supports two modes controlled by ``delay_sec``:
    - **Timed replay** (delay_sec > 0): pushes each signal with a delay,
      simulating a live feed. Runs until all files are replayed.
    - **On-demand / burst** (delay_sec == 0): pushes all signals at once
      with no delay — useful for unit tests or batch processing.

    Files are sorted alphabetically so replay order is deterministic.
    Only ``.txt`` files are loaded; hidden files and non-txt are skipped.
    """

    def __init__(
        self,
        signals_dir: str | Path,
        delay_sec: float = 5.0,
        loop: bool = False,
        queue: asyncio.Queue | None = None,
    ):
        """
        Args:
            signals_dir: Directory containing ``.txt`` signal files.
            delay_sec: Seconds to wait between each signal. 0 = burst mode.
            loop: If True, restart from the beginning after the last file.
            queue: Optional shared queue; one is created if not provided.
        """
        super().__init__(queue)
        self._signals_dir = Path(signals_dir)
        self._delay_sec = delay_sec
        self._loop = loop
        self._running = False

    def _load_files(self) -> list[Path]:
        """Return sorted list of .txt files in the signals directory."""
        if not self._signals_dir.is_dir():
            logger.error("Signals directory does not exist: %s", self._signals_dir)
            return []

        files = sorted(self._signals_dir.glob("*.txt"))
        if not files:
            logger.warning("No .txt files found in %s", self._signals_dir)
        return files

    async def start(self) -> None:
        """Begin replaying signals from the configured directory."""
        self._running = True
        files = self._load_files()

        if not files:
            logger.warning("SimulationAdapter: nothing to replay.")
            return

        logger.info(
            "SimulationAdapter: loaded %d signal files from %s (delay=%.1fs, loop=%s)",
            len(files),
            self._signals_dir,
            self._delay_sec,
            self._loop,
        )

        while self._running:
            for i, filepath in enumerate(files):
                if not self._running:
                    break

                text = filepath.read_text().strip()
                if not text:
                    logger.debug("Skipping empty file: %s", filepath.name)
                    continue

                logger.info(
                    "SimulationAdapter: replaying [%d/%d] %s",
                    i + 1,
                    len(files),
                    filepath.name,
                )
                await self._queue.put(text)

                if self._delay_sec > 0 and self._running:
                    await asyncio.sleep(self._delay_sec)

            if not self._loop:
                break

        logger.info("SimulationAdapter: replay finished.")

    async def stop(self) -> None:
        """Stop the replay loop."""
        self._running = False
