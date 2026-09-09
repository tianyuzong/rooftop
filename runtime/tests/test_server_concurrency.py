import unittest
import tempfile
import socket
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app import server
from app.db import connect, initialize
from app.server import (ConcurrentHTTPServer, DualStackHTTPServer,
                        MAX_CONCURRENT_COMPARISONS, _server_class_for_host,
                        _client_ip, _remote_auth_status, chart_payload, comparison_analysis_assets,
                        resolve_market_asset)


class ServerConcurrencyTests(unittest.TestCase):
    def test_shared_server_is_configured_for_concurrent_windows(self):
        self.assertTrue(ConcurrentHTTPServer.daemon_threads)
        self.assertTrue(ConcurrentHTTPServer.allow_reuse_address)
        self.assertGreaterEqual(ConcurrentHTTPServer.request_queue_size, 32)
        self.assertGreaterEqual(MAX_CONCURRENT_COMPARISONS, 2)

    def test_ipv6_wildcard_selects_dual_stack_server(self):
        self.assertIs(_server_class_for_host("::"), DualStackHTTPServer)
        self.assertEqual(DualStackHTTPServer.address_family, socket.AF_INET6)
        self.assertIs(_server_class_for_host("0.0.0.0"), ConcurrentHTTPServer)

    def test_dual_stack_logs_normalize_ipv4_mapped_clients(self):
        self.assertEqual(_client_ip(("127.0.0.1", 50000)), "127.0.0.1")
        self.assertEqual(_client_ip(("::ffff:10.207.153.128", 50000)), "10.207.153.128")
        self.assertEqual(
            _client_ip(("2400:dd01:103a:4032:bf4f:3072:d1a7:7b16", 50000)),
            "2400:dd01:103a:4032:bf4f:3072:d1a7:7b16",
        )

    def test_plugin_launcher_enables_report_refresh(self):
        launcher = Path(__file__).resolve().parents[2] / "scripts" / "start_stock_compare.ps1"
        content = launcher.read_text(encoding="utf-8-sig")
        self.assertIn('$env:ARGUS_REPORT_REFRESH_ENABLED = "1"', content)
        self.assertIn('$env:ARGUS_LIVE_REFRESH_ENABLED = "1"', content)
        self.assertIn('$env:ARGUS_CONTINUOUS_LEARNING_ENABLED = "1"', content)
        self.assertIn("health.runtime.virtual_environment", content)
        self.assertIn("health.runtime.plugin_version", content)
        self.assertIn("health.runtime.runtime_revision", content)
        self.assertIn("health.runtime.data_lake", content)
        self.assertIn('$env:ARGUS_DATA_LAKE = $dataLakeRoot', content)
        self.assertIn("Get-RuntimeRevision", content)
        self.assertIn('Join-Path $PSScriptRoot "runtime_revision.py"', content)
        self.assertNotIn("$pythonPath -c", content)
        self.assertIn('Join-Path $venvRoot "python.exe"', content)
        self.assertIn('"--host", $BindAddress', content)
        self.assertIn("public_host = $PublicHost", content)
        self.assertIn('dual_stack = $BindAddress -eq "::"', content)
        self.assertIn('[string]$TokenFile = ""', content)
        self.assertIn('"X-Argus-Token"', content)
        self.assertIn("ARGUS_REMOTE_TOKEN_FILE", content)
        self.assertIn("Format-HttpHost", content)
        self.assertIn('[ValidateSet("auto", "signals", "compare", "harness", "agent")]', content)
        self.assertIn('if ($stockItems.Count) { "compare" } else { "signals" }', content)
        self.assertIn('$resolvedView -eq "compare"', content)
        self.assertIn('"${PublicScheme}://${urlHost}:$selectedPort/?view=$resolvedView"', content)
        self.assertIn('view = $resolvedView', content)
        self.assertIn("ARGUS_API_RATE_LIMIT_PER_MINUTE", content)
        self.assertIn("ARGUS_ALLOWED_HOSTS", content)
        self.assertNotIn('[Parameter(Mandatory = $true)]\n    [string]$Stocks', content)
        task_installer = Path(__file__).resolve().parents[2] / "scripts" / "install_continuous_learning_tasks.ps1"
        task_content = task_installer.read_text(encoding="utf-8-sig")
        self.assertIn("08:45", task_content)
        self.assertIn("18:00", task_content)
        self.assertIn("08:40", task_content)
        self.assertIn("Argus-Ashare-Web-Service", task_content)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", task_content)
        self.assertIn("-DataLakePath", task_content)
        self.assertIn("StartWhenAvailable", task_content)

    def test_remote_api_auth_uses_header_token_and_leaves_static_public(self):
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "remote.token"
            token_file.write_text("expected-token", encoding="utf-8")
            with patch.dict(os.environ, {"ARGUS_REMOTE_TOKEN_FILE": str(token_file)}):
                self.assertIsNone(_remote_auth_status("/styles.css", None))
                self.assertEqual(_remote_auth_status("/api/health", None), 401)
                self.assertEqual(_remote_auth_status("/api/health", "wrong"), 403)
                self.assertIsNone(_remote_auth_status("/api/health", "expected-token"))

    def test_dashboard_does_not_infer_account_money_without_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch.object(server, "connect", side_effect=lambda: connect(path)), \
                    patch.object(server, "strategy_lab_payload", return_value={}), \
                    patch.object(server, "source_access_status", return_value=[]), \
                    patch.object(server, "smtp_status", return_value={}):
                payload = server.dashboard_payload()
        self.assertFalse(payload["portfolio"]["has_positions"])
        self.assertIsNone(payload["portfolio"]["market_value"])
        self.assertIsNone(payload["portfolio"]["unrealized_pnl"])
        self.assertIsNone(payload["meta"]["evidence_coverage"])
        app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("value.textContent = '无持仓'", app_js)
        self.assertIn("系统不会推测账户金额", app_js)

    def test_data_source_summary_uses_user_facing_roles_and_statuses(self):
        now = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
        sources = [
            {"code": "tdx_public", "name": "通达信公开行情协议", "enabled": 1,
             "health_status": "HEALTHY", "last_success_at": "2026-09-01T09:30:00+08:00"},
            {"code": "qmt", "name": "QMT", "enabled": 0,
             "health_status": "UNKNOWN", "last_success_at": None},
            {"code": "local_cache", "name": "本地最后可信快照", "enabled": 1,
             "health_status": "UNKNOWN", "last_success_at": None},
        ]
        with patch.dict(os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}):
            summary = server._source_summary(sources, now=now)
        self.assertEqual(summary["primary"]["code"], "tdx_public")
        self.assertEqual(summary["primary"]["status_label"], "正常")
        self.assertEqual(summary["backup"]["status_label"], "已启用")
        self.assertEqual(summary["alternatives"][0]["status_label"], "未接入")
        self.assertEqual(summary["timezone_label"], "北京时间")

    def test_data_source_summary_marks_old_success_as_stale(self):
        source = {"code": "tdx_public", "name": "通达信公开行情协议", "enabled": 1,
                  "health_status": "HEALTHY", "last_success_at": "2026-08-27T09:30:00+08:00"}
        presented = server._source_state(
            source, now=datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(presented["status_code"], "STALE")
        self.assertEqual(presented["status_label"], "数据可能陈旧")

    def test_library_hides_raw_source_enums_in_normal_view(self):
        app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("当前在线行情", app_js)
        self.assertIn("专业数据源与原始状态", app_js)
        self.assertIn("timeZone:'Asia/Shanghai'", app_js)
        self.assertNotIn("safe(s.health_status)", app_js)
        self.assertNotIn("s.enabled ? '启用' : '待配置'", app_js)

    def test_real_portfolio_import_is_previewed_confirmed_and_kept_read_only(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        index_html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        server_py = (Path(__file__).resolve().parents[1] / "app" / "server.py").read_text(
            encoding="utf-8"
        )
        market_py = (Path(__file__).resolve().parents[1] / "app" / "data_sources" / "market.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="portfolioImportDialog"', index_html)
        self.assertNotIn('id="marketTape"', index_html)
        self.assertNotIn("function renderMarkets", app_js)
        self.assertIn('data-portfolio-mode="manual"', index_html)
        self.assertIn('data-portfolio-mode="csv"', index_html)
        self.assertIn("确认后将以本次快照替换当前真实持仓", index_html)
        self.assertIn("/api/portfolio/imports/preview", app_js)
        self.assertIn("/api/portfolio/imports/confirm", app_js)
        self.assertIn("/api/portfolio/clear", app_js)
        self.assertIn("不会用成本价代替当前价格", app_js)
        self.assertIn('path == "/api/portfolio/imports/preview"', server_py)
        self.assertIn('path == "/api/portfolio/imports/confirm"', server_py)
        self.assertIn('path == "/api/portfolio/clear"', server_py)
        self.assertIn("AND verification_status='USER_CONFIRMED'", market_py)

    def test_windows_task_runner_is_windows_powershell_compatible(self):
        runner = Path(__file__).resolve().parents[2] / "scripts" / "run_continuous_learning.ps1"
        content = runner.read_bytes()
        self.assertTrue(all(byte < 128 for byte in content))
        self.assertIn(b'$env:PYTHONUTF8 = "1"', content)
        self.assertIn(b'$env:ARGUS_DATA_LAKE = $dataLakeRoot', content)

    def test_quick_decision_uses_published_strategy_endpoints_only(self):
        app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/quant/decision", app_js)
        self.assertIn("$('#quickQuantRun').onclick=viewQuickQuantRecommendation", app_js)
        handler = app_js.split("async function viewQuickQuantRecommendation(){", 1)[1].split(
            "function scheduleHarnessPoll", 1
        )[0]
        self.assertIn("/api/quant/mandates", handler)
        self.assertNotIn("/api/harness/runs", handler)
        self.assertNotIn("refresh_data", handler)
        self.assertNotIn("collect_sentiment", handler)
        self.assertIn("quantDecisionOutputHtml(decision)", handler)
        self.assertIn("output.scrollIntoView", handler)
        self.assertIn("output.focus", handler)
        self.assertIn("quantAllocationState(decision.result).blocked", handler)
        self.assertIn("即时多因子与K线预测", app_js)
        self.assertIn("stockForecastCurveSvg", app_js)
        self.assertIn("样本外校准区间", app_js)
        self.assertNotIn("若已持有：未触发技术卖出复核", app_js)
        self.assertNotIn("投资条件已保存，盘后会自动补齐数据", handler)
        self.assertIn("LATEST_TRADING_DAY_SNAPSHOT", app_js)
        self.assertIn("最近交易日模型快照", app_js)
        self.assertNotIn("等待首次盘后策略解算", app_js)

    def test_active_recommendation_poll_is_hourly_and_keeps_open_stock_profiles(self):
        app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("const HARNESS_ACTIVE_POLL_MS=60*60*1000;", app_js)
        self.assertIn("window.setTimeout(loadHarness,HARNESS_ACTIVE_POLL_MS)", app_js)
        self.assertNotIn("window.setTimeout(loadHarness,5000)", app_js)
        self.assertIn("details.stock-profile[open]", app_js)
        self.assertIn("details.open=Boolean(symbol&&openStockProfiles.has(symbol))", app_js)

    def test_quant_chart_combines_history_with_probabilistic_forecast(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("function quantCurveSvg(curve,forecastCurve,targetReturn,simulations)", app_js)
        self.assertIn("function portfolioFutureCurveSvg(forecast,targetReturnPct)", app_js)
        self.assertIn("建议资金分配（研究）", app_js)
        self.assertIn("展开查看每只股票校准区间", app_js)
        result_renderer = app_js.split("function quantResultHtml", 1)[1].split(
            "function harnessResultHtml", 1
        )[0]
        self.assertIn("rec.forecast_allocations", result_renderer)
        self.assertIn("portfolioFutureCurveSvg(portfolioForecast", result_renderer)
        self.assertIn("forecastCandidates.slice", result_renderer)
        self.assertIn("${portfolioPrediction}${stockPredictions}${forecastEvidence}", result_renderer)
        self.assertNotIn(
            '<div class="professional-only"><div class="subsection-head"><span>历史资金曲线',
            result_renderer,
        )
        self.assertNotIn("exp.forecast_curve", result_renderer)
        self.assertIn("不回退到旧的长期模板线", result_renderer)
        self.assertIn("forecast-band-wide", app_js)
        self.assertIn("forecast-band-likely", app_js)
        self.assertIn("forecast-median", app_js)
        self.assertIn("forecast-target", app_js)
        self.assertIn("组合相关情景区间", app_js)
        self.assertIn("未通过门禁的期限仍提供明确标注的历史基准情景", app_js)
        self.assertIn("包含未证明预测优势的历史基准情景", app_js)
        self.assertIn("scenarioForecastDays", result_renderer)
        self.assertIn("validated_horizon_trading_days", result_renderer)
        self.assertIn("共同严格校准 ${validatedForecastDays} 日", result_renderer)
        self.assertIn('portfolio-endpoint-grid extended', app_js)
        self.assertIn('.portfolio-endpoint-grid.extended', styles)
        self.assertIn(".quant-equity-chart .forecast-band-wide", styles)
        self.assertIn(".quant-equity-chart .forecast-median", styles)

    def test_stock_profile_materials_have_traceable_deep_links(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn("function enhanceStockProfileLinks", app_js)
        self.assertIn("/?view=library&query=", app_js)
        self.assertIn("/?view=overview&asset=", app_js)
        self.assertIn("/?view=logic&symbol=", app_js)
        self.assertIn("/PC_HSF10/NewFinanceAnalysis/Index", app_js)
        self.assertIn("/PC_HSF10/IndustryAnalysis/Index", app_js)
        self.assertIn("linkedQuery&&$('#librarySearchInput')", app_js)
        self.assertIn("requestedAsset", app_js)
        self.assertIn("stock-evidence-link", styles)

    def test_market_refresh_runs_outside_web_interpreter(self):
        refresher = server.MarketDataRefresher()
        process = Mock()
        process.poll.return_value = 0
        with patch.object(server.subprocess, "Popen", return_value=process) as popen:
            self.assertTrue(refresher._launch_market_refresh(True, True, ["600519"]))
        arguments = popen.call_args.args[0]
        self.assertIn("app.data_sources.market", arguments)
        self.assertIn("--no-history", arguments)
        self.assertNotIn("--no-minute", arguments)

    def test_quote_and_daily_refreshes_use_independent_processes(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(server, "DATA_LAKE", Path(folder)):
            refresher = server.MarketDataRefresher()
            quote_process = Mock()
            quote_process.poll.return_value = 0
            daily_process = Mock()
            daily_process.poll.return_value = 0
            with patch.object(
                server.subprocess, "Popen", side_effect=[quote_process, daily_process]
            ) as popen:
                self.assertTrue(refresher._launch_market_refresh(
                    False, False, ["600519", "000858"]
                ))
                self.assertTrue(refresher._launch_daily_refresh(["600519", "000858"]))
        quote_arguments = popen.call_args_list[0].args[0]
        daily_arguments = popen.call_args_list[1].args[0]
        self.assertIn("--no-minute", quote_arguments)
        self.assertIn("--no-daily", quote_arguments)
        self.assertIn("--no-minute", daily_arguments)
        self.assertNotIn("--no-daily", daily_arguments)
        self.assertIs(refresher.refresh_process, quote_process)
        self.assertIs(refresher.daily_process, daily_process)

    def test_selected_asset_refresh_fetches_minutes_without_slow_daily_work(self):
        symbol = "601988"
        process = Mock()
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(server, "DATA_LAKE", Path(folder)), \
                patch.object(server.subprocess, "Popen", return_value=process) as popen:
            server._asset_refresh_processes.pop(symbol, None)
            self.assertTrue(server._refresh_market_asset_in_background(symbol))
            server._asset_refresh_processes.pop(symbol, None)
        arguments = popen.call_args.args[0]
        self.assertIn("--no-history", arguments)
        self.assertIn("--no-daily", arguments)
        self.assertNotIn("--no-minute", arguments)

    def test_selected_asset_refresh_is_due_only_during_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch.object(server, "connect", lambda: connect(path)), \
                    patch.object(server, "_a_share_session_open", return_value=True):
                self.assertTrue(server._asset_live_refresh_due("601988"))
            with patch.object(server, "_a_share_session_open", return_value=False):
                self.assertFalse(server._asset_live_refresh_due("601988"))

    def test_market_refresh_tracks_overview_symbols_and_uses_one_minute_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO comparison_watchlist
                       (symbol,name,first_compared_at,last_compared_at,compare_count)
                       VALUES('601988','中国银行','2026-09-01','2026-09-01',1)"""
                )
                conn.commit()
            with (patch.object(server, "connect", lambda: connect(path)),
                  patch.dict(os.environ, {
                      "ARGUS_MARKET_SYMBOLS": "000001.SH",
                      "ARGUS_MINUTE_REFRESH_SECONDS": "60",
                  })):
                refresher = server.MarketDataRefresher()
                symbols = refresher._tracked_symbols()
            self.assertEqual(refresher.minute_seconds, 60)
            self.assertIn("000001.SH", symbols)
            self.assertIn("601988", symbols)

    def test_a_share_minute_refresh_only_runs_in_trading_sessions(self):
        with patch.object(server, "_is_cn_trading_day", return_value=True):
            self.assertTrue(server._a_share_session_open(
                datetime(2026, 9, 1, 10, 0, tzinfo=server.SHANGHAI)
            ))
            self.assertFalse(server._a_share_session_open(
                datetime(2026, 9, 1, 12, 0, tzinfo=server.SHANGHAI)
            ))
            self.assertTrue(server._a_share_session_open(
                datetime(2026, 9, 1, 14, 30, tzinfo=server.SHANGHAI)
            ))
        with patch.object(server, "_is_cn_trading_day", return_value=False):
            self.assertFalse(server._a_share_session_open(
                datetime(2026, 9, 2, 10, 0, tzinfo=server.SHANGHAI)
            ))

    def test_overview_refreshes_dashboard_and_chart_every_minute(self):
        app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function renderMarketSyncState(meta)", app_js)
        self.assertIn("window.setInterval(refreshRealtimeOverview, 60_000);", app_js)
        self.assertIn("normalizeDashboard(await request('/api/dashboard'))", app_js)
        self.assertIn("盘中每分钟同步", app_js)

    def test_serve_script_accepts_a_configurable_host(self):
        serve_script = Path(__file__).resolve().parents[2] / "scripts" / "serve.py"
        content = serve_script.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--host"', content)
        self.assertIn("run(host=args.host, port=args.port)", content)

    def test_comparison_watchlist_drives_overview_assets_and_empty_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO comparison_watchlist
                       (symbol,name,first_compared_at,last_compared_at,compare_count)
                       VALUES('601988','中国银行','2026-08-26','2026-08-26',1)"""
                )
                conn.commit()
                assets = comparison_analysis_assets(conn)
            self.assertEqual([(item["symbol"], item["name"]) for item in assets], [("601988", "中国银行")])
            with patch.object(server, "connect", lambda: connect(path)):
                payload = chart_payload("601988", "1d")
            self.assertEqual(payload["asset"]["name"], "中国银行")
            self.assertEqual(payload["series"], [])

    def test_latest_quote_uses_capture_time_instead_of_future_stamped_old_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                source_id = conn.execute(
                    "SELECT id FROM data_sources WHERE code='tdx_public'"
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO comparison_watchlist
                       (symbol,name,first_compared_at,last_compared_at,compare_count)
                       VALUES('000858','五粮液','2026-09-01','2026-09-01',1)"""
                )
                conn.executemany(
                    """INSERT INTO quote_snapshots
                       (asset_symbol,asset_name,observed_at,price,previous_close,
                        source_id,captured_at,raw_path)
                       VALUES('000858','五粮液',?,?,?,?,?,?)""",
                    [
                        ("2026-09-01T15:29:28+08:00", 71.27, 71.51, source_id,
                         "2026-09-01T01:08:19+08:00", "old.json"),
                        ("2026-09-01T14:53:43+08:00", 71.82, 71.27, source_id,
                         "2026-09-01T06:54:24+00:00", "new.json"),
                    ],
                )
                conn.commit()
            with patch.object(server, "connect", lambda: connect(path)):
                chart = chart_payload("000858", "1d")
                quotes = server.market_quotes_payload(["000858"])
                with closing(connect(path)) as conn:
                    assets = comparison_analysis_assets(conn)
            self.assertEqual(chart["quote"]["price"], 71.82)
            self.assertEqual(quotes["quotes"][0]["price"], 71.82)
            self.assertEqual(assets[0]["price"], 71.82)

    def test_overview_resolves_any_stock_and_populates_chart_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with (patch.object(server, "connect", lambda: connect(path)),
                  patch.object(server, "resolve_stock", return_value={
                      "input": "中国平安", "symbol": "601318", "name": "中国平安",
                  }),
                  patch.object(server, "refresh_market_data", return_value={
                      "provider": "tdx", "status": "SUCCESS", "errors": [],
                  }) as refresh):
                payload = resolve_market_asset("中国平安")
            refresh.assert_called_once_with(
                ["601318"], include_minutes=True, include_daily=True, include_history=True
            )
            self.assertEqual(payload["stock"]["symbol"], "601318")
            self.assertEqual(payload["chart"]["asset"]["name"], "中国平安")

    def test_recommendation_methodology_uses_live_profile_contract(self):
        payload = server.recommendation_methodology_payload()
        self.assertFalse(payload["order_execution"])
        self.assertEqual(payload["profiles"]["aggressive"]["fundamental_weight"], 0.25)
        self.assertEqual(payload["profiles"]["balanced"]["fundamental_weight"], 0.40)
        self.assertEqual(payload["profiles"]["conservative"]["fundamental_weight"], 0.55)
        self.assertIn("综合分", payload["ranking_formula"]["display"])

    def test_navigation_has_unified_library_and_plain_methodology_page(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="library"', html)
        self.assertIn('data-view="logic"', html)
        self.assertIn('data-view="signals"', html)
        self.assertIn('id="notificationBadge"', html)
        self.assertIn('id="assetInput"', html)
        self.assertNotIn('id="methodButton"', html)
        self.assertNotIn('data-view="strategy"', html)
        self.assertNotIn('data-view="reports"', html)
        self.assertIn("/api/quant/methodology", app_js)
        self.assertIn("/api/assets/resolve", app_js)
        self.assertIn("/api/notifications", app_js)
        self.assertIn("/api/models", app_js)
        self.assertIn("X-Argus-Token", app_js)

    def test_signal_subscription_layout_stays_reachable(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="subscription-actions"', app_js)
        self.assertIn("main{overflow-x:clip;overflow-y:visible}", styles)
        self.assertIn(".signal-layout{align-items:start}", styles)
        self.assertIn(".notification-band{position:sticky;top:14px", styles)
        self.assertIn(
            "@media(max-width:1250px){.notification-band{position:static;order:-1",
            styles,
        )
        self.assertIn(
            "@media(max-width:1250px){.signal-summary{align-items:flex-start;flex-direction:column}.signal-layout{grid-template-columns:1fr}",
            styles,
        )
        self.assertIn(".subscription-actions{grid-column:1/-1", styles)


if __name__ == "__main__":
    unittest.main()
