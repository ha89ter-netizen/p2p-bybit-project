"""Ретраи и бэкофф HTTP-клиента.

Логика ретраев — классический кандидат на тихую поломку: она не
срабатывает в обычный день и выясняется в момент, когда сбор уже идёт
третьи сутки.
"""
import io
import unittest
import urllib.error
from unittest import mock

from collector.bybit_public import (THROTTLE_BACKOFF_SEC, BybitPublicClient,
                                    FetchError)
from config.settings import CollectorConfig

CFG = CollectorConfig(max_retries=3, backoff_base_sec=1.5,
                      min_gap_between_requests_sec=0)


class _Resp(io.BytesIO):
    """urlopen возвращает контекстный менеджер, BytesIO сам им не является."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def responder(body: bytes):
    """Свежий ответ на каждый вызов — один BytesIO читается только раз."""
    return lambda *a, **k: _Resp(body)


OK_BODY = b'{"ret_code":0,"ret_msg":"SUCCESS","result":{"count":0,"items":[]}}'
AUTH_FAIL_BODY = (b'{"ret_code":10007,"ret_msg":"User authentication failed.",'
                  b'"result":{}}')


def raises_http(code: int):
    """side_effect-функция, ВОЗВРАЩАЮЩАЯ исключение, его не бросает —
    mock поднимает только то, что присвоено side_effect напрямую или
    брошено изнутри. Возвращённый HTTPError уходит дальше как file-like
    объект и маскируется под пустой ответ."""
    def _raise(*a, **k):
        raise urllib.error.HTTPError("https://h/p", code, "err", {}, None)
    return _raise


class TestRetry(unittest.TestCase):
    def test_succeeds_without_retry(self):
        client = BybitPublicClient(CFG)
        with mock.patch("urllib.request.urlopen", side_effect=responder(OK_BODY)) as u:
            page = client.fetch_page("h", "1", 1)
        self.assertEqual(u.call_count, 1)
        self.assertEqual(page.items, [])

    def test_retries_transient_error_then_succeeds(self):
        client = BybitPublicClient(CFG)
        attempts = {"n": 0}

        def flaky(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.URLError("сеть моргнула")
            return _Resp(OK_BODY)

        with mock.patch("time.sleep") as sleep, \
             mock.patch("urllib.request.urlopen", side_effect=flaky):
            page = client.fetch_page("h", "1", 1)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(page.items, [])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.5])

    def test_gives_up_after_max_retries(self):
        client = BybitPublicClient(CFG)
        with mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")) as u:
            with self.assertRaises(FetchError):
                client.fetch_page("h", "1", 1)
        self.assertEqual(u.call_count, CFG.max_retries)

    def test_nonzero_ret_code_is_an_error(self):
        """HTTP 200 с ret_code != 0 — это провал, а не успешный ответ."""
        client = BybitPublicClient(CFG)
        with mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=responder(AUTH_FAIL_BODY)):
            with self.assertRaises(FetchError) as ctx:
                client.fetch_page("h", "1", 1)
        self.assertIn("10007", str(ctx.exception))

    def test_rate_limit_backs_off_far_harder_than_normal_error(self):
        """429 с бэкоффом в полторы секунды — это способ получить бан по IP
        на третьи сутки сбора, а не защита от него."""
        client = BybitPublicClient(CFG)
        with mock.patch("time.sleep") as sleep, \
             mock.patch("urllib.request.urlopen", side_effect=raises_http(429)):
            with self.assertRaises(FetchError):
                client.fetch_page("h", "1", 1)
        waits = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(waits, [THROTTLE_BACKOFF_SEC, THROTTLE_BACKOFF_SEC * 2])
        self.assertGreater(min(waits), CFG.backoff_base_sec ** CFG.max_retries)

    def test_server_error_500_uses_normal_backoff(self):
        client = BybitPublicClient(CFG)
        with mock.patch("time.sleep") as sleep, \
             mock.patch("urllib.request.urlopen", side_effect=raises_http(500)):
            with self.assertRaises(FetchError):
                client.fetch_page("h", "1", 1)
        waits = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(waits, [1.5, 1.5 ** 2])


class TestPagination(unittest.TestCase):
    def _client_returning(self, page_sizes):
        client = BybitPublicClient(CollectorConfig(
            max_pages=len(page_sizes) if page_sizes else 1,
            page_size=50, min_gap_between_requests_sec=0))
        pages = iter(page_sizes)

        def fake(host, side, page):
            from collector.bybit_public import Page
            n = next(pages)
            return Page(host, side, page, [{"id": f"{page}-{i}"} for i in range(n)],
                        0, b"{}", 1)
        client.fetch_page = fake
        return client

    def test_short_page_ends_pagination_as_complete(self):
        client = self._client_returning([50, 12])
        pages, complete = client.fetch_book("h", "1")
        self.assertTrue(complete)
        self.assertEqual(len(pages), 2)

    def test_hitting_page_cap_is_reported_incomplete(self):
        """Книга больше, чем мы можем прочитать. Если посчитать такой обход
        полным, хвост книги будет объявлен исчезнувшим — и статистика
        времени жизни превратится в мусор."""
        client = self._client_returning([50, 50])
        pages, complete = client.fetch_book("h", "1")
        self.assertFalse(complete)
        self.assertEqual(len(pages), 2)

    def test_failure_midway_is_incomplete_but_keeps_partial_pages(self):
        client = BybitPublicClient(CollectorConfig(
            max_pages=5, page_size=50, min_gap_between_requests_sec=0))
        from collector.bybit_public import Page
        calls = {"n": 0}

        def fake(host, side, page):
            calls["n"] += 1
            if calls["n"] == 2:
                raise FetchError("network died")
            return Page(host, side, page, [{"id": str(i)} for i in range(50)],
                        0, b"{}", 1)
        client.fetch_page = fake
        pages, complete = client.fetch_book("h", "1")
        self.assertFalse(complete)
        self.assertEqual(len(pages), 1)


if __name__ == "__main__":
    unittest.main()
