import asyncio
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from bistro.services import autotest
from bistro import config


class AutotestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_startup_starts_one_sender_and_shutdown_waits_for_cancellation(self):
        spec = importlib.util.spec_from_file_location(
            "bistro_autotest_application", Path(__file__).resolve().parents[1] / "bistro" / "bot.py",
        )
        application = importlib.util.module_from_spec(spec)
        with patch.object(config, "TOKEN", "123456:offline-test-token"):
            spec.loader.exec_module(application)
        self.addAsyncCleanup(application.bot.session.close)
        self.addAsyncCleanup(autotest.stop_autotests)
        started = asyncio.Event()
        sending = asyncio.Event()

        async def send_due(bot):
            self.assertIs(bot, application.bot)
            started.set()
            await sending.wait()

        with patch.object(autotest, "send_due_autotests", AsyncMock(side_effect=send_due)) as sender:
            await application.dp.emit_startup(bot=application.bot)
            await asyncio.wait_for(started.wait(), timeout=2)
            task = autotest._sender_task
            await application.dp.emit_startup(bot=application.bot)
            self.assertIs(autotest._sender_task, task)
            sender.assert_awaited_once_with(application.bot)

            await application.dp.emit_shutdown(bot=application.bot)

        self.assertTrue(task.cancelled())
        self.assertIsNone(autotest._sender_task)


if __name__ == "__main__":
    unittest.main()
