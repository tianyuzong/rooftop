import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from app.db import connect, initialize
from app import alerts, mail_settings
from app.email_digest import build_digest, normalize_selection, queue_due_digests, queue_digest
from app.signal_service import create_subscription, subscription_payload, set_subscription_enabled


class EmailDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'test.db'
        initialize(self.path)
        self.factory=lambda:connect(self.path)
        self.settings_patch=patch.object(mail_settings,'SETTINGS_PATH',Path(self.temp.name)/'email.json')
        self.settings_patch.start()
        with closing(self.factory()) as conn:
            source=conn.execute("SELECT id FROM data_sources WHERE code='local_cache'").fetchone()[0]
            for day,price in [('2026-09-07',100),('2026-09-08',102),('2026-09-09',999)]:
                conn.execute('''INSERT INTO quote_snapshots(asset_symbol,asset_name,observed_at,price,previous_close,
                    change_pct,source_id,captured_at,raw_path) VALUES('000001.SH','上证指数',?,?,100,2,?,?,?)''',
                    (day+'T15:00:00+08:00',price,source,day,'fixture'))
            conn.execute("INSERT INTO a_share_universe_assets(symbol,name,exchange,source,source_asof,updated_at) VALUES('300750','宁德时代','SZ','fixture','2026-09-08','now')")
            for day,title in [('2026-09-08','宁德时代 当日公告'),('2026-09-09','宁德时代 未来公告')]:
                conn.execute('''INSERT INTO source_documents(doc_key,document_type,title,body,source_url,source_name,published_at,captured_at,raw_path,content_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',(day,'NEWS',title,'内容','https://example.invalid/source','fixture',day,day,'fixture',day))
            conn.commit()

    def tearDown(self):
        self.settings_patch.stop();self.temp.cleanup()

    def subscription(self, **kwargs):
        return create_subscription({'name':'日报','target':'reader@example.invalid','event_kinds':[],
          'digest_kinds':['MARKET'],'send_time':'08:30',**kwargs}, self.factory)

    def test_dated_summary_never_uses_future_quotes_or_news(self):
        report=build_digest({'digest_kinds':['MARKET','COMPANIES'],'watch_stocks':'宁德时代','report_date':'2026-09-08'},self.factory)
        self.assertIn('102.00',report['body']);self.assertNotIn('999',report['body'])
        self.assertIn('当日公告',report['body']);self.assertNotIn('未来公告',report['body'])
        self.assertIn('深证成指',report['missing_data']);self.assertIn('当天行情缺失',report['body'])
        self.assertNotIn('<script',report['html'])

    def test_schedule_is_shanghai_time_and_idempotent(self):
        self.subscription()
        self.assertEqual(queue_due_digests(datetime(2026,9,9,8,29,tzinfo=ZoneInfo('Asia/Shanghai')),self.factory)['queued'],0)
        self.assertEqual(queue_due_digests(datetime(2026,9,9,8,30,tzinfo=ZoneInfo('Asia/Shanghai')),self.factory)['queued'],1)
        self.assertEqual(queue_due_digests(datetime(2026,9,9,9,0,tzinfo=ZoneInfo('Asia/Shanghai')),self.factory)['queued'],0)
        with closing(self.factory()) as conn:
            row=conn.execute('SELECT * FROM alert_outbox').fetchone()
        self.assertIn('2026-09-08',row['subject'])

    def test_edit_cancels_old_content_and_does_not_duplicate_subscription(self):
        sub=self.subscription();queue_digest(sub['id'],'2026-09-08',self.factory)
        updated=self.subscription(id=sub['id'],digest_kinds=['COMPANIES'],watch_stocks='宁德时代')
        self.assertEqual(updated['watch_symbols'],['300750'])
        self.assertEqual(len(subscription_payload(self.factory)['subscriptions']),1)
        with closing(self.factory()) as conn:
            self.assertEqual(conn.execute('SELECT status FROM alert_outbox').fetchone()[0],'CANCELLED')
        queue_digest(sub['id'],'2026-09-08',self.factory)
        with closing(self.factory()) as conn:
            row=conn.execute('SELECT * FROM alert_outbox').fetchone()
        self.assertEqual(row['status'],'PENDING');self.assertIn('关注公司',row['subject'])
        set_subscription_enabled(sub['id'],False,self.factory)
        with closing(self.factory()) as conn:
            self.assertEqual(conn.execute('SELECT status FROM alert_outbox').fetchone()[0],'CANCELLED')

    def test_selection_and_email_validation(self):
        with self.assertRaises(ValueError):
            normalize_selection({'digest_kinds':['COMPANIES']},self.factory)
        with self.assertRaises(ValueError):
            self.subscription(target='reader@example.invalid\nBcc:someone@example.invalid')
        with self.assertRaises(ValueError):
            normalize_selection({'send_time':'25:99'},self.factory)

    def test_dpapi_settings_never_return_or_store_plaintext_password(self):
        result=mail_settings.save_settings({'host':'smtp.163.com','port':465,'user':'test@163.com','password':'fixture-only-authorization-code','enabled':True})
        self.assertNotIn('password',result)
        self.assertNotIn('fixture-only-authorization-code',mail_settings.SETTINGS_PATH.read_text())
        self.assertEqual(mail_settings.get_settings()['password'],'fixture-only-authorization-code')
        mail_settings.save_settings({'host':'smtp.163.com','port':465,'user':'test@163.com','password':'','enabled':True})
        self.assertEqual(mail_settings.get_settings()['password'],'fixture-only-authorization-code')

    def test_server_acceptance_and_user_receipt_are_separate(self):
        sub=self.subscription();queued=queue_digest(sub['id'],'2026-09-08',self.factory)
        with self.assertRaises(ValueError):alerts.confirm_received(queued['message']['id'],self.factory)
        settings={'host':'smtp.example.invalid','port':465,'user':'sender@example.invalid','password':'fixture','security':'ssl','enabled':True,'default_target':''}
        client=Mock();transport=Mock();transport.__enter__=Mock(return_value=client);transport.__exit__=Mock(return_value=False)
        with patch.object(alerts,'smtp_status',return_value={'configured':True,'send_enabled':True}),patch.object(alerts,'get_settings',return_value=settings),patch.object(alerts,'smtp_client',return_value=transport):
            result=alerts.send_pending(conn_factory=self.factory)
        self.assertEqual(result['sent'],1)
        with closing(self.factory()) as conn:
            row=conn.execute('SELECT * FROM alert_outbox').fetchone()
            self.assertIsNone(row['received_at']);self.assertEqual(row['status'],'SENT')
            self.assertIsNotNone(conn.execute('SELECT last_sent_at FROM signal_subscriptions').fetchone()[0])
        alerts.confirm_received(row['id'],self.factory)
        with closing(self.factory()) as conn:self.assertIsNotNone(conn.execute('SELECT received_at FROM alert_outbox').fetchone()[0])

    def test_fresh_database_contains_no_preset_hypotheses_or_positions(self):
        with closing(self.factory()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM positions').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0],0)


if __name__=='__main__':unittest.main()
