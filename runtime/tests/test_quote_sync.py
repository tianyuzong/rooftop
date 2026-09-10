import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app import quote_sync
from app import server
from app.data_sources import market
from app.data_sources.tdx_public import PublicQuoteClient


class QuoteSyncTests(unittest.TestCase):
    def test_startup_fetches_immediately_even_outside_session(self):
        refresher = server.MarketDataRefresher()
        refresher.stop_event = Mock()
        refresher.stop_event.is_set.side_effect = [False, False, True]
        with patch.object(server, '_a_share_session_open', return_value=False), \
                patch.object(server.time, 'monotonic', side_effect=[1, 11]), \
                patch.object(refresher, '_tracked_symbols', return_value=['600519']), \
                patch.object(refresher, '_launch_market_refresh', return_value=True) as launch:
            refresher.run()
        launch.assert_called_once_with(False, False, ['600519'])

    def test_closed_session_also_updates_after_one_minute(self):
        refresher = server.MarketDataRefresher()
        refresher.stop_event = Mock()
        refresher.stop_event.is_set.side_effect = [False, False, False, True]
        with patch.object(server, '_a_share_session_open', return_value=False), \
                patch.object(server.time, 'monotonic', side_effect=[1, 31, 61]), \
                patch.object(refresher, '_tracked_symbols', return_value=['600519']), \
                patch.object(refresher, '_launch_market_refresh', return_value=True) as launch:
            refresher.run()
        self.assertEqual(launch.call_count, 2)

    def test_public_connection_uses_only_public_greetings(self):
        client = PublicQuoteClient()
        with patch('app.data_sources.tdx_public.SetupCmd1') as first, \
                patch('app.data_sources.tdx_public.SetupCmd2') as second:
            client.setup()
            first.return_value.call_api.assert_called_once()
            second.return_value.call_api.assert_called_once()

    def test_quotes_split_at_60_and_keep_market_identity(self):
        symbols = ['000001.SH'] + [f'{n:06d}' for n in range(1, 122)]
        def fetch(instruments, fund):
            self.assertLessEqual(len(instruments), 60)
            return [dict(market=m, code=c, price=100 if m == 1 else 10,
                         last_close=10, high=100, low=1) for _, m, c in instruments], 'test'
        with patch.object(market, '_tdx_fetch_quote_group', side_effect=fetch) as mocked:
            rows, _ = market.fetch_tdx_quotes(symbols)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(len(rows), len(symbols))
        by_symbol = {r['symbol']: r for r in rows}
        self.assertEqual(by_symbol['000001.SH']['price'], 100)
        self.assertEqual(by_symbol['000001']['price'], 10)

    def test_failed_batch_does_not_discard_other_quotes(self):
        def fetch(instruments, fund):
            if fund:
                raise RuntimeError('fund node unavailable')
            return [dict(market=1, code='600519', price=10, last_close=10)], 'test'
        with patch.object(market, '_tdx_fetch_quote_group', side_effect=fetch):
            rows, raw = market.fetch_tdx_quotes(['600519', '512400'])
        self.assertEqual([r['symbol'] for r in rows], ['600519'])
        self.assertIn(b'fund node unavailable', raw)

    def test_fund_price_scale_and_receipt_timestamp(self):
        client = Mock()
        client.get_security_quotes.return_value = [dict(
            code='512400', market=1, price=18.24, last_close=18.53,
            open=18.45, high=18.46, low=18.23, servertime='101:43:59')]
        with patch('app.data_sources.tdx_public.PublicQuoteClient', return_value=client):
            rows, _ = market._tdx_fetch_quote_group([('512400', 1, '512400')], True)
        item = market._tdx_quote_item('512400', rows[0])
        self.assertEqual(item['price'], 1.824)
        self.assertEqual(item['previous_close'], 1.853)
        self.assertEqual(item['time_basis'], 'RECEIVED_AT')
        self.assertEqual(item['observed_at'], rows[0]['_received_at'])
        client.disconnect.assert_called_once()

    def test_sync_health_is_not_enabled_flag_or_unrelated_ingestion(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder, patch.object(quote_sync, 'DATA_LAKE', Path(folder)):
            self.assertEqual(quote_sync.status_payload(True, True, now)['status'], 'STALE')
            quote_sync.write_status(status='HEALTHY', last_success_at=now.isoformat())
            self.assertEqual(quote_sync.status_payload(True, True, now)['status'], 'HEALTHY')
            self.assertEqual(quote_sync.status_payload(True, True, now + timedelta(seconds=121))['status'], 'STALE')
            quote_sync.write_status(status='FAILED')
            self.assertEqual(quote_sync.status_payload(True, True, now)['status'], 'FAILED')
            quote_sync.write_status(status='RUNNING', last_result_status='FAILED')
            self.assertEqual(quote_sync.status_payload(True, True, now)['status'], 'FAILED')
            self.assertEqual(quote_sync.status_payload(True, False, now)['status'], 'DISABLED')
            quote_sync.write_status(status='HEALTHY')
            self.assertEqual(quote_sync.status_payload(False, True, now)['status'], 'CLOSED')

    def test_minute_bars_keep_native_fund_prices_and_exclude_future_bars(self):
        client = Mock()
        client.get_security_bars.return_value = [
            dict(datetime=stamp, open=1.824, high=1.825, low=1.823,
                 close=1.824, vol=1000, amount=1824)
            for stamp in ['2026-09-09 10:00', '2099-09-09 10:00']]
        with patch('app.data_sources.tdx_public.PublicQuoteClient', return_value=client):
            rows, _ = market.fetch_tdx_recent_minutes('512400', 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['close'], 1.824)
        self.assertEqual(rows[0]['volume'], 1000)
        self.assertEqual(rows[0]['bar_kind'], 'OHLC')
        client.get_security_bars.assert_called_once_with(8, 1, '512400', 0, 240)


if __name__ == '__main__':
    unittest.main()
