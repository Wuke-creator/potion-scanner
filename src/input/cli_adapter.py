import asyncio
import sys

from .base_adapter import BaseAdapter


class CLIAdapter(BaseAdapter):
    """Reads trading signals from stdin.

    Prompts the user to paste a signal. Collects multi-line input
    until a blank line (two consecutive Enters) is entered, then
    pushes the collected text onto the queue as a single string.
    """

    def __init__(self, queue: asyncio.Queue | None = None):
        super().__init__(queue)
        self._running = False

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                print("\n--- Paste signal (press Enter twice to submit, Ctrl+C to quit) ---")
                lines: list[str] = []

                while True:
                    line = await loop.run_in_executor(None, sys.stdin.readline)

                    if line == "":  # EOF
                        self._running = False
                        break

                    line = line.rstrip("\n")

                    if line == "" and lines and lines[-1] == "":
                        lines.pop()  # remove trailing blank
                        break

                    lines.append(line)

                text = "\n".join(lines).strip()
                if text:
                    await self._queue.put(text)

            except (KeyboardInterrupt, EOFError):
                self._running = False

    async def stop(self) -> None:
        self._running = False
