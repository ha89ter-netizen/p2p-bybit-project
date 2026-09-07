"""Загрузка .env.

Файл с секретами — место, где ошибка стоит дорого: либо токен не
подхватится и дайджест молча не уйдёт, либо .env перезатрёт то, что
задано в окружении осознанно.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.dotenv import load


class TestDotenv(unittest.TestCase):
    def _write(self, text: str) -> Path:
        d = tempfile.mkdtemp()
        p = Path(d) / ".env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_simple_pairs(self):
        p = self._write("A=1\nB=two\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load(p), {"A": "1", "B": "two"})
            self.assertEqual(os.environ["B"], "two")

    def test_environment_wins_over_file(self):
        """export задан осознанно — .env не имеет права его перезатирать."""
        p = self._write("TOKEN=from_file\n")
        with mock.patch.dict(os.environ, {"TOKEN": "from_export"}, clear=True):
            load(p)
            self.assertEqual(os.environ["TOKEN"], "from_export")

    def test_ignores_comments_and_blank_lines(self):
        p = self._write("# коммент\n\n  \nA=1\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load(p), {"A": "1"})

    def test_strips_quotes_and_export_prefix(self):
        p = self._write('export A="quoted"\nB=\'single\'\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load(p), {"A": "quoted", "B": "single"})

    def test_empty_value_is_treated_as_unset_token(self):
        """Пустой TELEGRAM_BOT_TOKEN в шаблоне не должен включать отправку."""
        p = self._write("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            load(p)
            from config.settings import TelegramConfig
            self.assertFalse(TelegramConfig().enabled)

    def test_value_with_equals_sign_survives(self):
        p = self._write("A=a=b=c\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load(p), {"A": "a=b=c"})

    def test_config_reads_environment_at_instantiation_not_import(self):
        """Значение по умолчанию в теле dataclass вычисляется один раз при
        импорте. Если читать os.getenv прямо в поле, TelegramConfig()
        навсегда запомнит окружение первого import, и .env, загруженный
        позже, будет молча проигнорирован."""
        from config.settings import TelegramConfig
        with mock.patch.dict(os.environ,
                             {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
                              "TELEGRAM_ROWS": "9"}, clear=True):
            cfg = TelegramConfig()
            self.assertEqual(cfg.bot_token, "t")
            self.assertEqual(cfg.rows_per_bucket, 9)
            self.assertTrue(cfg.enabled)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(TelegramConfig().enabled)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load(Path(tempfile.mkdtemp()) / "nope.env"), {})

    def test_line_without_equals_is_skipped(self):
        p = self._write("мусор\nA=1\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load(p), {"A": "1"})


class TestSecretsNotCommitted(unittest.TestCase):
    def test_gitignore_covers_env_and_database(self):
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text().split()
        for pattern in (".env", "data/"):
            self.assertIn(pattern, ignored)

    def test_example_file_carries_no_real_token(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / ".env.example").read_text()
        for line in text.splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                self.assertEqual(line.strip(), "TELEGRAM_BOT_TOKEN=")


if __name__ == "__main__":
    unittest.main()
