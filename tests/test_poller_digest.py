"""Периодическая работа не должна влиять на сбор данных.

Сеть Telegram может лежать, токен протухнуть, диск переполниться. Ни одно
из этих событий не имеет права остановить коллектор: данные невосполнимы,
а отчёт и уведомление воспроизводимы в любой момент.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import poller
from config.settings import TelegramConfig
from storage.db import Store

# TELEGRAM — frozen dataclass, отдельный атрибут в нём не подменить:
# нужно заменять объект целиком.
ON = TelegramConfig(bot_token="123:TEST", chat_id="42")
OFF = TelegramConfig(bot_token="", chat_id="")


class TestPeriodicIsolation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "t.sqlite"))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_telegram_failure_does_not_propagate(self):
        with mock.patch.object(poller, "send_digest",
                               side_effect=RuntimeError("telegram лёг")), \
             mock.patch("scripts.export_findings.main"), \
             mock.patch.object(poller, "TELEGRAM", ON), \
             mock.patch.object(poller, "log") as log:
            poller.run_periodic(self.store)          # не должно бросить
        self.assertTrue(any("не отправлено" in str(c) for c in log.call_args_list))

    def test_findings_failure_does_not_propagate(self):
        with mock.patch("scripts.export_findings.main",
                        side_effect=OSError("диск полон")), \
             mock.patch.object(poller, "log") as log:
            poller.run_periodic(self.store)
        self.assertTrue(any("FINDINGS" in str(c) for c in log.call_args_list))

    def test_findings_written_even_when_telegram_disabled(self):
        """Отчёт нужен для анализа независимо от уведомлений."""
        with mock.patch("scripts.export_findings.main") as export, \
             mock.patch.object(poller, "send_digest") as send, \
             mock.patch.object(poller, "TELEGRAM", OFF):
            poller.run_periodic(self.store)
        export.assert_called_once()
        send.assert_not_called()

    def test_telegram_failure_does_not_prevent_findings(self):
        """Части независимы: упавший Telegram не лишает нас отчёта."""
        with mock.patch("scripts.export_findings.main") as export, \
             mock.patch.object(poller, "send_digest",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "TELEGRAM", ON), \
             mock.patch.object(poller, "log"):
            poller.run_periodic(self.store)
        export.assert_called_once()

    def test_digest_period_is_derived_from_hours(self):
        from config.settings import COLLECTOR, TELEGRAM
        expected = max(1, TELEGRAM.digest_hours * 3600
                       // COLLECTOR.poll_interval_sec)
        self.assertEqual(expected, TELEGRAM.digest_hours * 180)

    def test_send_digest_builds_and_sends(self):
        with mock.patch("notify.telegram.send") as sent, \
             mock.patch("analysis.digest.build") as built, \
             mock.patch("notify.telegram.render", return_value="text"):
            built.return_value = mock.MagicMock()
            poller.send_digest(self.store)
        sent.assert_called_once_with("text")


if __name__ == "__main__":
    unittest.main()
