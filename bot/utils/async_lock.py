import asyncio
import fasteners
from random import uniform
from os import path, makedirs

from bot.utils import logger


class AsyncInterProcessLock:
    def __init__(self, lock_file: str):
        lock_dir = path.dirname(lock_file)
        makedirs(lock_dir, exist_ok=True)
        
        self._lock = fasteners.InterProcessLock(lock_file)
        self._file_name, _ = path.splitext(path.basename(lock_file))

    async def __aenter__(self) -> 'AsyncInterProcessLock':
        while True:
            try:
                lock_acquired = await asyncio.to_thread(self._lock.acquire, timeout=uniform(5, 10))
                if lock_acquired:
                    return self
                sleep_time = uniform(30, 150)
                logger.info(
                    f"⌛ {self._file_name} | Failed to acquire lock for "
                    f"{'accounts_config' if 'accounts_config' in self._file_name else 'session'}. "
                    f"Retrying in {int(sleep_time)} seconds"
                )
                await asyncio.sleep(sleep_time)
            except Exception as e:
                logger.error(f"❌ Error acquiring lock: {str(e)}")
                await asyncio.sleep(5)

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            await asyncio.to_thread(self._lock.release)
        except Exception as e:
            logger.error(f"❌ Error releasing lock: {str(e)}")
