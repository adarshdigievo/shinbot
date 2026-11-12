"""Official Telegram channel entrypoint, executed in its dedicated runner."""

import asyncio

from shinbot.services.telegram import run

if __name__ == "__main__":
    asyncio.run(run())
