"""Local-first relational persistence.

SQLite is the zero-configuration system of record.
No table in this schema can place or represent an order.
"""

import json
import math
import os
import sqlite3
import threading
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_LAKE = Path(os.environ.get("ARGUS_DATA_LAKE", ROOT / "data_lake"))
DB_PATH = DATA_LAKE / "db" / "market_intelligence.db"
_INITIALIZE_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

DATA_DIRS = (
    "db", "raw/market", "raw/documents", "raw/news", "normalized",
    "documents", "cache", "models", "research/factors", "research/backtests",
    "private", "backups", "logs", "dead_letter", "outbox",
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, exchange_symbol TEXT,
  name TEXT NOT NULL, market TEXT NOT NULL, asset_type TEXT NOT NULL,
  currency TEXT NOT NULL, data_status TEXT NOT NULL DEFAULT 'DEMO'
);
CREATE TABLE IF NOT EXISTS data_sources (
  id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  source_kind TEXT NOT NULL, access_mode TEXT NOT NULL, priority INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, license_note TEXT NOT NULL,
  homepage TEXT NOT NULL, health_status TEXT NOT NULL DEFAULT 'UNKNOWN',
  last_success_at TEXT, last_error_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES data_sources(id),
  dataset TEXT NOT NULL, asset_symbol TEXT, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
  raw_path TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS data_quality_checks (
  id INTEGER PRIMARY KEY, asset_symbol TEXT NOT NULL, check_name TEXT NOT NULL,
  source_a TEXT NOT NULL, source_b TEXT NOT NULL, status TEXT NOT NULL,
  metric REAL, details_json TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
  asset_id INTEGER NOT NULL REFERENCES assets(id), trade_date TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL DEFAULT 0, amount REAL,
  source_id INTEGER REFERENCES data_sources(id), captured_at TEXT,
  raw_path TEXT, is_demo INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(asset_id, trade_date)
);
CREATE TABLE IF NOT EXISTS quote_snapshots (
  asset_symbol TEXT NOT NULL, asset_name TEXT NOT NULL,
  observed_at TEXT NOT NULL, price REAL NOT NULL, previous_close REAL,
  open REAL, high REAL, low REAL, change_value REAL, change_pct REAL,
  volume REAL, amount REAL, turnover_rate REAL,
  source_id INTEGER NOT NULL REFERENCES data_sources(id),
  captured_at TEXT NOT NULL, raw_path TEXT NOT NULL,
  PRIMARY KEY(asset_symbol, observed_at, source_id)
);
CREATE TABLE IF NOT EXISTS minute_bars (
  asset_symbol TEXT NOT NULL, bar_time TEXT NOT NULL,
  interval_minutes INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume REAL, amount REAL,
  bar_kind TEXT NOT NULL DEFAULT 'OHLC',
  source_id INTEGER NOT NULL REFERENCES data_sources(id),
  captured_at TEXT NOT NULL, raw_path TEXT NOT NULL,
  PRIMARY KEY(asset_symbol, bar_time, interval_minutes, source_id)
);
CREATE TABLE IF NOT EXISTS market_daily_bars (
  asset_symbol TEXT NOT NULL, trade_date TEXT NOT NULL, adjust_mode TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL, amount REAL, source_id INTEGER NOT NULL REFERENCES data_sources(id),
  captured_at TEXT NOT NULL, raw_path TEXT NOT NULL,
  PRIMARY KEY(asset_symbol, trade_date, adjust_mode, source_id)
);
CREATE TABLE IF NOT EXISTS portfolios (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, as_of TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'UNSET',
  source_name TEXT NOT NULL DEFAULT '尚未导入', imported_at TEXT,
  verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED', last_import_id INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  asset_id INTEGER NOT NULL REFERENCES assets(id), quantity REAL NOT NULL,
  cost_price REAL NOT NULL, current_price REAL NOT NULL, highest_since_entry REAL NOT NULL,
  import_id INTEGER, as_of TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'LEGACY_LOCAL',
  source_name TEXT NOT NULL DEFAULT '历史本地记录',
  verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  valuation_status TEXT NOT NULL DEFAULT 'UNAVAILABLE',
  price_observed_at TEXT, price_source TEXT, tracking_started_at TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_imports (
  id INTEGER PRIMARY KEY, import_key TEXT NOT NULL UNIQUE,
  portfolio_id INTEGER REFERENCES portfolios(id), account_name TEXT NOT NULL,
  source_type TEXT NOT NULL, source_name TEXT NOT NULL, source_file_name TEXT,
  as_of TEXT NOT NULL, status TEXT NOT NULL, replace_existing INTEGER NOT NULL DEFAULT 1,
  position_count INTEGER NOT NULL, content_hash TEXT NOT NULL,
  normalized_json TEXT NOT NULL, reconciliation_json TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
  source_type TEXT NOT NULL, reliability REAL NOT NULL,
  verification_status TEXT NOT NULL, checked_at TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, claim TEXT NOT NULL, label TEXT NOT NULL,
  status TEXT NOT NULL, source_id INTEGER REFERENCES sources(id),
  observed_at TEXT, captured_at TEXT NOT NULL, independent_check TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, statement TEXT NOT NULL,
  status TEXT NOT NULL, falsification_rule TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, report_type TEXT NOT NULL,
  body TEXT NOT NULL, created_at TEXT NOT NULL, evidence_coverage REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_documents (
  id INTEGER PRIMARY KEY, doc_key TEXT NOT NULL UNIQUE,
  document_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  source_url TEXT, source_name TEXT, published_at TEXT, observed_at TEXT,
  captured_at TEXT NOT NULL, raw_path TEXT NOT NULL,
  content_hash TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS source_document_versions (
  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES source_documents(id),
  captured_at TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  content_hash TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
  raw_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_watchlist (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
  last_requested_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS comparison_watchlist (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
  first_compared_at TEXT NOT NULL, last_compared_at TEXT NOT NULL,
  compare_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS app_migrations (
  migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_bad_cases (
  id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
  case_type TEXT NOT NULL, source TEXT NOT NULL, page TEXT,
  input_json TEXT NOT NULL, expected_json TEXT NOT NULL DEFAULT '{}',
  observed_json TEXT NOT NULL DEFAULT '{}', severity TEXT NOT NULL DEFAULT 'MEDIUM',
  status TEXT NOT NULL DEFAULT 'OBSERVED', occurrences INTEGER NOT NULL DEFAULT 1,
  notes TEXT NOT NULL DEFAULT '', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_candidates (
  id INTEGER PRIMARY KEY, bad_case_id INTEGER REFERENCES harness_bad_cases(id),
  candidate_type TEXT NOT NULL, title TEXT NOT NULL, rationale TEXT NOT NULL,
  config_json TEXT NOT NULL, baseline_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DRAFT', activatable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, evaluated_at TEXT, approved_at TEXT, approved_by TEXT
);
CREATE TABLE IF NOT EXISTS harness_evaluations (
  id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES harness_candidates(id),
  trigger_kind TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, total_cases INTEGER NOT NULL DEFAULT 0,
  passed_cases INTEGER NOT NULL DEFAULT 0, failed_cases INTEGER NOT NULL DEFAULT 0,
  skipped_cases INTEGER NOT NULL DEFAULT 0, regression_count INTEGER NOT NULL DEFAULT 0,
  pass_rate REAL NOT NULL DEFAULT 0, details_json TEXT NOT NULL DEFAULT '[]', error TEXT
);
CREATE TABLE IF NOT EXISTS harness_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE, parent_version TEXT,
  status TEXT NOT NULL, config_json TEXT NOT NULL, reason TEXT NOT NULL,
  candidate_id INTEGER REFERENCES harness_candidates(id), created_at TEXT NOT NULL,
  activated_at TEXT
);
CREATE TABLE IF NOT EXISTS harness_events (
  id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, entity_type TEXT NOT NULL,
  entity_id INTEGER, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_threads (
  id INTEGER PRIMARY KEY, thread_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', context_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_runs (
  id INTEGER PRIMARY KEY, run_key TEXT NOT NULL UNIQUE,
  thread_id INTEGER NOT NULL REFERENCES harness_threads(id),
  workflow TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL,
  input_json TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
  plan_json TEXT NOT NULL DEFAULT '[]', result_json TEXT NOT NULL DEFAULT '{}',
  current_step INTEGER NOT NULL DEFAULT 0, error TEXT, retry_of TEXT,
  requested_by TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL,
  started_at TEXT, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS harness_run_events (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES harness_runs(id),
  sequence INTEGER NOT NULL, event_type TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'INFO',
  message TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
);
CREATE TABLE IF NOT EXISTS harness_tool_calls (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES harness_runs(id),
  step_key TEXT NOT NULL, tool_name TEXT NOT NULL, risk_level TEXT NOT NULL,
  status TEXT NOT NULL, arguments_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}', error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  UNIQUE(run_id,step_key)
);
CREATE TABLE IF NOT EXISTS harness_approvals (
  id INTEGER PRIMARY KEY, approval_key TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL REFERENCES harness_runs(id),
  tool_call_id INTEGER NOT NULL UNIQUE REFERENCES harness_tool_calls(id),
  action TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
  request_json TEXT NOT NULL DEFAULT '{}', resolution_json TEXT NOT NULL DEFAULT '{}',
  requested_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT
);
CREATE TABLE IF NOT EXISTS report_sync_jobs (
  job_key TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'IDLE',
  begin_date TEXT, end_date TEXT, next_page INTEGER NOT NULL DEFAULT 1,
  total_pages INTEGER NOT NULL DEFAULT 0, total_hits INTEGER NOT NULL DEFAULT 0,
  processed_pages INTEGER NOT NULL DEFAULT 0, fetched_rows INTEGER NOT NULL DEFAULT 0,
  stored_rows INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0,
  stop_requested INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT,
  updated_at TEXT NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, asset_id INTEGER REFERENCES assets(id),
  signal_type TEXT NOT NULL, score REAL NOT NULL, label TEXT NOT NULL,
  rationale TEXT NOT NULL, model_version TEXT NOT NULL, generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY, strategy_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  category TEXT NOT NULL, description TEXT NOT NULL, implementation TEXT NOT NULL,
  source_framework TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_risk_policies (
  id INTEGER PRIMARY KEY, strategy_id INTEGER NOT NULL UNIQUE REFERENCES strategies(id),
  stop_loss_pct REAL NOT NULL, take_profit_pct REAL NOT NULL,
  trailing_stop_pct REAL NOT NULL, max_position_pct REAL NOT NULL,
  max_drawdown_pct REAL NOT NULL, max_daily_loss_pct REAL NOT NULL,
  turnover_limit REAL, liquidity_rule TEXT NOT NULL, stress_rules_json TEXT NOT NULL,
  version TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factors (
  id INTEGER PRIMARY KEY, factor_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  family TEXT NOT NULL, expression TEXT NOT NULL, direction INTEGER NOT NULL,
  description TEXT NOT NULL, source_framework TEXT NOT NULL,
  version TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_factors (
  strategy_id INTEGER NOT NULL REFERENCES strategies(id),
  factor_id INTEGER NOT NULL REFERENCES factors(id), weight REAL NOT NULL,
  role TEXT NOT NULL, PRIMARY KEY(strategy_id,factor_id)
);
CREATE TABLE IF NOT EXISTS factor_runs (
  id INTEGER PRIMARY KEY, factor_id INTEGER NOT NULL REFERENCES factors(id),
  universe TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
  ic REAL, rank_ic REAL, coverage REAL, params_json TEXT NOT NULL,
  metrics_json TEXT, raw_path TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS backtest_runs (
  id INTEGER PRIMARY KEY, strategy_id INTEGER NOT NULL REFERENCES strategies(id),
  asset_symbol TEXT NOT NULL, framework TEXT NOT NULL,
  data_start TEXT, data_end TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, config_json TEXT NOT NULL, metrics_json TEXT,
  equity_json TEXT, raw_path TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS investment_mandates (
  id INTEGER PRIMARY KEY, mandate_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  capital REAL NOT NULL, horizon_months INTEGER NOT NULL,
  target_return_pct REAL NOT NULL, max_drawdown_pct REAL NOT NULL,
  stop_loss_pct REAL NOT NULL DEFAULT 8,
  take_profit_pct REAL NOT NULL DEFAULT 20,
  trailing_stop_pct REAL NOT NULL DEFAULT 8,
  sectors_json TEXT NOT NULL DEFAULT '[]', universe_json TEXT NOT NULL,
  max_positions INTEGER NOT NULL, take_profit_mode TEXT NOT NULL,
  max_iterations INTEGER NOT NULL, execution_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_evolution_runs (
  id INTEGER PRIMARY KEY, experiment_key TEXT NOT NULL UNIQUE,
  mandate_id INTEGER NOT NULL REFERENCES investment_mandates(id),
  status TEXT NOT NULL, data_snapshot_at TEXT, iteration_count INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS strategy_evolution_candidates (
  id INTEGER PRIMARY KEY,
  experiment_id INTEGER NOT NULL REFERENCES strategy_evolution_runs(id),
  profile TEXT NOT NULL, iteration INTEGER NOT NULL, params_json TEXT NOT NULL,
  validation_json TEXT NOT NULL DEFAULT '{}', feasible INTEGER NOT NULL DEFAULT 0,
  score REAL, selected INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(experiment_id,profile,iteration)
);
CREATE TABLE IF NOT EXISTS strategy_simulations (
  id INTEGER PRIMARY KEY,
  experiment_id INTEGER NOT NULL REFERENCES strategy_evolution_runs(id),
  candidate_id INTEGER REFERENCES strategy_evolution_candidates(id),
  phase TEXT NOT NULL, profile TEXT NOT NULL, data_start TEXT NOT NULL,
  data_end TEXT NOT NULL, metrics_json TEXT NOT NULL, curve_json TEXT NOT NULL,
  trades_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_evolution_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  mandate_id INTEGER NOT NULL REFERENCES investment_mandates(id),
  experiment_id INTEGER NOT NULL REFERENCES strategy_evolution_runs(id),
  status TEXT NOT NULL, mandate_json TEXT NOT NULL, strategies_json TEXT NOT NULL,
  approved_by TEXT, created_at TEXT NOT NULL, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS strategy_evolution_retry_jobs (
  id INTEGER PRIMARY KEY, retry_key TEXT NOT NULL UNIQUE,
  mandate_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_candidate_offset INTEGER NOT NULL DEFAULT 0,
  last_data_asof TEXT, last_experiment_key TEXT, active_version_key TEXT,
  last_gate_json TEXT NOT NULL DEFAULT '{}', last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS trading_calendar (
  trade_date TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'CN',
  is_open INTEGER NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(trade_date,market)
);
CREATE TABLE IF NOT EXISTS harness_learning_cycles (
  id INTEGER PRIMARY KEY, cycle_key TEXT NOT NULL UNIQUE,
  cycle_date TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
  trigger_kind TEXT NOT NULL, universe_json TEXT NOT NULL,
  data_asof TEXT, metrics_json TEXT NOT NULL DEFAULT '{}',
  errors_json TEXT NOT NULL DEFAULT '[]', progress_json TEXT NOT NULL DEFAULT '{}',
  heartbeat_at TEXT, worker_token TEXT, started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sentiment_daily (
  symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
  score REAL NOT NULL, confidence REAL NOT NULL,
  document_count INTEGER NOT NULL DEFAULT 0,
  positive_count INTEGER NOT NULL DEFAULT 0,
  negative_count INTEGER NOT NULL DEFAULT 0,
  source_breakdown_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(symbol,trade_date)
);
CREATE TABLE IF NOT EXISTS prediction_model_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  parent_version TEXT, status TEXT NOT NULL,
  feature_schema_json TEXT NOT NULL, coefficients_json TEXT NOT NULL,
  training_start TEXT, training_end TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}', gate_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_predictions (
  id INTEGER PRIMARY KEY, prediction_key TEXT NOT NULL UNIQUE,
  cycle_id INTEGER REFERENCES harness_learning_cycles(id),
  model_version TEXT NOT NULL, symbol TEXT NOT NULL,
  signal_date TEXT NOT NULL, target_date TEXT NOT NULL, phase TEXT NOT NULL,
  probability_up REAL NOT NULL, predicted_return REAL NOT NULL,
  confidence REAL NOT NULL, features_json TEXT NOT NULL,
  rationale_json TEXT NOT NULL DEFAULT '{}', actual_return REAL,
  actual_direction INTEGER, direction_correct INTEGER, brier_score REAL,
  absolute_error REAL, status TEXT NOT NULL DEFAULT 'PENDING',
  created_at TEXT NOT NULL, scored_at TEXT
);
CREATE TABLE IF NOT EXISTS prediction_evaluations (
  id INTEGER PRIMARY KEY, cycle_id INTEGER REFERENCES harness_learning_cycles(id),
  baseline_version TEXT NOT NULL, candidate_version TEXT NOT NULL,
  eval_start TEXT NOT NULL, eval_end TEXT NOT NULL, sample_count INTEGER NOT NULL,
  baseline_metrics_json TEXT NOT NULL, candidate_metrics_json TEXT NOT NULL,
  gate_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_backtest_points (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES prediction_evaluations(id),
  model_role TEXT NOT NULL, symbol TEXT NOT NULL,
  signal_date TEXT NOT NULL, target_date TEXT NOT NULL,
  probability_up REAL NOT NULL, predicted_return REAL NOT NULL,
  actual_return REAL NOT NULL, direction_correct INTEGER NOT NULL,
  brier_score REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intraday_strategy_runs (
  id INTEGER PRIMARY KEY, run_key TEXT NOT NULL UNIQUE,
  cycle_id INTEGER REFERENCES harness_learning_cycles(id),
  status TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
  symbols_json TEXT NOT NULL, config_json TEXT NOT NULL,
  data_start TEXT, data_end TEXT, metrics_json TEXT NOT NULL DEFAULT '{}',
  error TEXT, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS intraday_strategy_candidates (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES intraday_strategy_runs(id),
  strategy_key TEXT NOT NULL, params_json TEXT NOT NULL,
  training_json TEXT NOT NULL DEFAULT '{}',
  validation_json TEXT NOT NULL DEFAULT '{}',
  holdout_json TEXT NOT NULL DEFAULT '{}',
  feasible INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0,
  selected INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(run_id,strategy_key)
);
CREATE TABLE IF NOT EXISTS intraday_strategy_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  parent_version TEXT, run_id INTEGER NOT NULL REFERENCES intraday_strategy_runs(id),
  status TEXT NOT NULL, strategy_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
  gate_json TEXT NOT NULL, artifact_hash TEXT NOT NULL,
  reason TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS intraday_signals (
  id INTEGER PRIMARY KEY, signal_key TEXT NOT NULL UNIQUE,
  version_key TEXT NOT NULL, symbol TEXT NOT NULL, bar_time TEXT NOT NULL,
  action TEXT NOT NULL, score REAL NOT NULL, reference_price REAL NOT NULL,
  features_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deep_model_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  parent_version TEXT, status TEXT NOT NULL, architecture TEXT NOT NULL,
  feature_schema_json TEXT NOT NULL, sequence_length INTEGER NOT NULL,
  training_start TEXT, training_end TEXT, artifact_path TEXT NOT NULL,
  artifact_hash TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}',
  gate_json TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL,
  created_at TEXT NOT NULL, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS deep_model_predictions (
  id INTEGER PRIMARY KEY, prediction_key TEXT NOT NULL UNIQUE,
  cycle_id INTEGER REFERENCES harness_learning_cycles(id),
  model_version TEXT NOT NULL, symbol TEXT NOT NULL,
  signal_date TEXT NOT NULL, target_date TEXT NOT NULL,
  probability_up REAL NOT NULL, predicted_return REAL NOT NULL,
  confidence REAL NOT NULL, features_json TEXT NOT NULL,
  actual_return REAL, direction_correct INTEGER, brier_score REAL,
  status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL, scored_at TEXT
);
CREATE TABLE IF NOT EXISTS sentiment_source_health (
  source_code TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
  status TEXT NOT NULL, last_attempt_at TEXT, last_success_at TEXT,
  document_count INTEGER NOT NULL DEFAULT 0, latency_ms REAL,
  error TEXT, coverage_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS code_evolution_candidates (
  id INTEGER PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE,
  parent_version TEXT, status TEXT NOT NULL, title TEXT NOT NULL,
  rationale TEXT NOT NULL, patch_text TEXT NOT NULL,
  allowed_paths_json TEXT NOT NULL, workspace_path TEXT,
  patch_hash TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at TEXT NOT NULL, evaluated_at TEXT, promoted_at TEXT
);
CREATE TABLE IF NOT EXISTS code_evolution_evaluations (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES code_evolution_candidates(id),
  status TEXT NOT NULL, checks_json TEXT NOT NULL DEFAULT '[]',
  tests_passed INTEGER NOT NULL DEFAULT 0, tests_failed INTEGER NOT NULL DEFAULT 0,
  regression_count INTEGER NOT NULL DEFAULT 0,
  baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
  candidate_metrics_json TEXT NOT NULL DEFAULT '{}',
  gate_json TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL,
  finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS code_evolution_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  parent_version TEXT, candidate_id INTEGER REFERENCES code_evolution_candidates(id),
  status TEXT NOT NULL, manifest_json TEXT NOT NULL,
  backup_path TEXT, reason TEXT NOT NULL,
  created_at TEXT NOT NULL, activated_at TEXT, rolled_back_at TEXT
);
CREATE TABLE IF NOT EXISTS a_share_universe_assets (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL, exchange TEXT NOT NULL,
  industry_code TEXT, source TEXT NOT NULL, source_asof TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS a_share_sector_memberships (
  symbol TEXT NOT NULL, sector_code TEXT NOT NULL, sector_name TEXT NOT NULL,
  sector_level INTEGER NOT NULL, source TEXT NOT NULL, source_asof TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(symbol,sector_code,source)
);
CREATE TABLE IF NOT EXISTS fundamental_reports (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, report_date TEXT NOT NULL,
  notice_date TEXT NOT NULL, report_type TEXT, source_code TEXT NOT NULL,
  observed_at TEXT NOT NULL, revenue REAL, net_profit REAL,
  deduct_net_profit REAL, revenue_yoy_pct REAL, net_profit_yoy_pct REAL,
  deduct_net_profit_yoy_pct REAL, roe_pct REAL, roic_pct REAL,
  gross_margin_pct REAL, net_margin_pct REAL, current_ratio REAL,
  quick_ratio REAL, cash_ratio REAL, debt_ratio_pct REAL,
  interest_debt_ratio_pct REAL, cashflow_to_profit REAL, fcff REAL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(symbol,report_date,source_code)
);
CREATE TABLE IF NOT EXISTS fundamental_valuations (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, asof_date TEXT NOT NULL,
  source_code TEXT NOT NULL, observed_at TEXT NOT NULL,
  market_cap REAL, pe_ttm REAL, pe_dynamic REAL, pb REAL, roe_pct REAL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(symbol,asof_date,source_code)
);
CREATE TABLE IF NOT EXISTS quant_mandates (
  id INTEGER PRIMARY KEY, mandate_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', input_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quant_portfolio_runs (
  id INTEGER PRIMARY KEY, run_key TEXT NOT NULL UNIQUE,
  mandate_id INTEGER NOT NULL REFERENCES quant_mandates(id),
  learning_cycle_id INTEGER REFERENCES harness_learning_cycles(id),
  trigger_kind TEXT NOT NULL, status TEXT NOT NULL, data_asof TEXT,
  prediction_model_version TEXT, strategy_experiment_key TEXT,
  result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS quant_portfolio_candidates (
  run_id INTEGER NOT NULL REFERENCES quant_portfolio_runs(id),
  symbol TEXT NOT NULL, name TEXT NOT NULL, sector_names_json TEXT NOT NULL,
  liquidity_amount REAL, liquidity_rank INTEGER,
  PRIMARY KEY(run_id,symbol)
);
CREATE TABLE IF NOT EXISTS quant_portfolio_positions (
  run_id INTEGER NOT NULL REFERENCES quant_portfolio_runs(id),
  symbol TEXT NOT NULL, name TEXT NOT NULL, weight REAL NOT NULL,
  shares INTEGER NOT NULL, reference_price REAL NOT NULL,
  probability_up REAL, predicted_return REAL, composite_score REAL NOT NULL,
  PRIMARY KEY(run_id,symbol)
);
CREATE TABLE IF NOT EXISTS quant_portfolio_versions (
  id INTEGER PRIMARY KEY, version_key TEXT NOT NULL UNIQUE,
  mandate_id INTEGER NOT NULL REFERENCES quant_mandates(id),
  run_id INTEGER NOT NULL REFERENCES quant_portfolio_runs(id),
  parent_version TEXT, status TEXT NOT NULL, score REAL NOT NULL,
  gate_json TEXT NOT NULL, result_json TEXT NOT NULL,
  created_at TEXT NOT NULL, activated_at TEXT
);
CREATE TABLE IF NOT EXISTS sector_cache_jobs (
  id INTEGER PRIMARY KEY, job_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  sectors_json TEXT NOT NULL, sector_codes_json TEXT NOT NULL,
  target_asof TEXT NOT NULL, retention_start TEXT NOT NULL,
  total_symbols INTEGER NOT NULL DEFAULT 0,
  market_processed INTEGER NOT NULL DEFAULT 0,
  market_cached INTEGER NOT NULL DEFAULT 0,
  model_ready INTEGER NOT NULL DEFAULT 0,
  fundamentals_processed INTEGER NOT NULL DEFAULT 0,
  fundamentals_cached INTEGER NOT NULL DEFAULT 0,
  current_symbol TEXT, error_count INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
  updated_at TEXT NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS sector_cache_items (
  job_id INTEGER NOT NULL REFERENCES sector_cache_jobs(id),
  symbol TEXT NOT NULL, name TEXT NOT NULL,
  market_status TEXT NOT NULL DEFAULT 'PENDING',
  fundamental_status TEXT NOT NULL DEFAULT 'PENDING',
  row_count INTEGER NOT NULL DEFAULT 0,
  data_start TEXT, data_end TEXT,
  report_count INTEGER NOT NULL DEFAULT 0,
  valuation_date TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(job_id,symbol)
);
CREATE TABLE IF NOT EXISTS alert_outbox (
  id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
  target TEXT, signal_id INTEGER, subscription_id INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, created_at TEXT NOT NULL, sent_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS research_model_definitions (
  id INTEGER PRIMARY KEY, model_key TEXT NOT NULL, model_kind TEXT NOT NULL,
  name TEXT NOT NULL, description TEXT NOT NULL, profile TEXT,
  version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT',
  specification_json TEXT NOT NULL, checksum TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT,
  UNIQUE(model_key,version)
);
CREATE TABLE IF NOT EXISTS research_model_assignments (
  id INTEGER PRIMARY KEY,
  model_id INTEGER NOT NULL REFERENCES research_model_definitions(id),
  model_kind TEXT NOT NULL, scope_type TEXT NOT NULL, scope_value TEXT NOT NULL,
  profile TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
  approved_by TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_signals (
  id INTEGER PRIMARY KEY, signal_key TEXT NOT NULL UNIQUE,
  mandate_id INTEGER NOT NULL REFERENCES quant_mandates(id),
  version_key TEXT NOT NULL, validation_status TEXT NOT NULL,
  action TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NEW', data_asof TEXT NOT NULL,
  reference_price REAL, shares INTEGER, weight REAL,
  stop_price REAL, take_profit_price REAL,
  score REAL, confidence REAL, rationale_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}', invalidation TEXT NOT NULL,
  previous_signal_key TEXT, research_only INTEGER NOT NULL DEFAULT 1,
  order_execution INTEGER NOT NULL DEFAULT 0,
  generated_at TEXT NOT NULL, acknowledged_at TEXT, dismissed_at TEXT
);
CREATE TABLE IF NOT EXISTS signal_subscriptions (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  mandate_id INTEGER REFERENCES quant_mandates(id),
  channel TEXT NOT NULL, target TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  event_kinds_json TEXT NOT NULL DEFAULT '["BUY","SELL","REBALANCE"]',
  minimum_confidence REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS api_audit_log (
  id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
  method TEXT NOT NULL, path TEXT NOT NULL, client_ip TEXT NOT NULL,
  auth_status TEXT NOT NULL, outcome TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS semantic_documents (
  id INTEGER PRIMARY KEY, doc_key TEXT NOT NULL UNIQUE, page TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, source_ref TEXT,
  content_hash TEXT NOT NULL, embedding BLOB, embedding_dim INTEGER,
  model_name TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_asset_date ON prices(asset_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_quote_symbol_time ON quote_snapshots(asset_symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_quote_symbol_capture ON quote_snapshots(asset_symbol, julianday(captured_at) DESC);
CREATE INDEX IF NOT EXISTS idx_minute_symbol_time ON minute_bars(asset_symbol, interval_minutes, bar_time DESC);
CREATE INDEX IF NOT EXISTS idx_market_daily_symbol_date ON market_daily_bars(asset_symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_sector_cache_jobs_status ON sector_cache_jobs(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sector_cache_items_status ON sector_cache_items(job_id,market_status,fundamental_status);
CREATE INDEX IF NOT EXISTS idx_semantic_page ON semantic_documents(page);
CREATE INDEX IF NOT EXISTS idx_model_assignments_lookup
  ON research_model_assignments(model_kind,profile,scope_type,scope_value,status);
CREATE INDEX IF NOT EXISTS idx_research_signals_recent
  ON research_signals(mandate_id,status,generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_signals_symbol
  ON research_signals(symbol,generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_subscriptions_mandate
  ON signal_subscriptions(mandate_id,enabled);
CREATE INDEX IF NOT EXISTS idx_api_audit_time ON api_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_documents_type_time ON source_documents(document_type,published_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_document_versions_doc_time ON source_document_versions(document_id,captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_watchlist_requested ON report_watchlist(enabled,last_requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_portfolio_imports_status ON portfolio_imports(status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_portfolio ON positions(portfolio_id,id);
CREATE INDEX IF NOT EXISTS idx_comparison_watchlist_recent ON comparison_watchlist(last_compared_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_sync_status ON report_sync_jobs(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_factor_runs_factor_time ON factor_runs(factor_id,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_strategy_time ON backtest_runs(strategy_id,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_evolution_runs_time ON strategy_evolution_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_run_profile ON strategy_evolution_candidates(experiment_id,profile,iteration);
CREATE INDEX IF NOT EXISTS idx_strategy_simulations_run_phase ON strategy_simulations(experiment_id,phase,profile);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_one_active_version_per_mandate
  ON strategy_evolution_versions(mandate_id) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_strategy_retry_jobs_status
  ON strategy_evolution_retry_jobs(status,updated_at);
CREATE INDEX IF NOT EXISTS idx_learning_cycles_date ON harness_learning_cycles(cycle_date DESC,phase,status);
CREATE INDEX IF NOT EXISTS idx_sentiment_symbol_date ON sentiment_daily(symbol,trade_date DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_single_active_model
  ON prediction_model_versions(status) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_predictions_target_status ON daily_predictions(target_date,status,symbol);
CREATE INDEX IF NOT EXISTS idx_prediction_evaluations_time ON prediction_evaluations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_points_eval ON prediction_backtest_points(evaluation_id,model_role,target_date);
CREATE INDEX IF NOT EXISTS idx_intraday_runs_time ON intraday_strategy_runs(id DESC);
CREATE INDEX IF NOT EXISTS idx_intraday_candidates_run ON intraday_strategy_candidates(run_id,selected,score DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intraday_single_active_version
  ON intraday_strategy_versions(status) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_intraday_signals_symbol ON intraday_signals(symbol,bar_time DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deep_single_active_model
  ON deep_model_versions(status) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_deep_predictions_target ON deep_model_predictions(target_date,status,symbol);
CREATE INDEX IF NOT EXISTS idx_code_evolution_candidates_status ON code_evolution_candidates(status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_evolution_evaluations_time ON code_evolution_evaluations(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_code_evolution_single_active
  ON code_evolution_versions(status) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_a_share_sector_name ON a_share_sector_memberships(sector_name,symbol);
CREATE INDEX IF NOT EXISTS idx_fundamental_reports_available
  ON fundamental_reports(symbol,notice_date DESC,report_date DESC);
CREATE INDEX IF NOT EXISTS idx_fundamental_valuations_asof
  ON fundamental_valuations(symbol,asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_quant_runs_mandate_time ON quant_portfolio_runs(mandate_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_quant_candidates_symbol ON quant_portfolio_candidates(symbol,run_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quant_one_active_version_per_mandate
  ON quant_portfolio_versions(mandate_id) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_harness_cases_status ON harness_bad_cases(status,last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_candidates_status ON harness_candidates(status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_evaluations_time ON harness_evaluations(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_harness_single_active_version ON harness_versions(status) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_harness_threads_updated ON harness_threads(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_runs_thread_time ON harness_runs(thread_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_runs_status ON harness_runs(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_run_events_run ON harness_run_events(run_id,sequence);
CREATE INDEX IF NOT EXISTS idx_harness_approvals_status ON harness_approvals(status,requested_at DESC);
"""

ASSETS = [
    ("512400", "SH.512400", "有色ETF南方", "CN", "ETF", "CNY", 1.922, -0.10),
    ("562500", "SH.562500", "机器人ETF华夏", "CN", "ETF", "CNY", 1.021, 0.08),
    ("000001.SH", "SH.000001", "上证指数", "CN", "INDEX", "CNY", 3634.20, 0.03),
    ("HSI", "HK.HSI", "恒生指数", "HK", "INDEX", "HKD", 25180.00, 0.06),
    ("SPX", "US.SPX", "标普500", "US", "INDEX", "USD", 6365.00, 0.08),
    ("NDX", "US.NDX", "纳斯达克100", "US", "INDEX", "USD", 23210.00, 0.10),
    ("N225", "JP.N225", "日经225", "JP", "INDEX", "JPY", 41820.00, -0.02),
    ("DAX", "DE.DAX", "德国DAX", "DE", "INDEX", "EUR", 24220.00, 0.04),
    ("XAU", "GLOBAL.XAU", "黄金现货", "GLOBAL", "COMMODITY", "USD", 3388.00, 0.07),
    ("HG", "GLOBAL.HG", "COMEX铜", "GLOBAL", "COMMODITY", "USD", 4.42, 0.02),
]

DATA_SOURCES = [
    ("tdx_local", "通达信本地行情", "desktop_cache", "local_files", 2, 1, "通过客户端盘后下载；本地文件为空时不冒充可用", "https://www.tdx.com.cn/"),
    ("tdx_public", "通达信公开行情协议", "public_quote", "public_tcp", 3, 1, "图表行情唯一在线采集源；使用 MIT tdxrs 客户端，历史深度取决于公开节点", "https://github.com/jiangtaovan/tdxrs"),
    ("akshare", "AKShare", "aggregator", "public_http", 10, 1, "MIT；上游网站接口可能变更", "https://github.com/akfamily/akshare"),
    ("qmt", "QMT/miniQMT (xtquant)", "broker_desktop", "local_client", 20, 0, "软件可免费安装，但 API/行情权限由开户券商决定", "https://dict.thinktrader.net"),
    ("futu", "Futu OpenD", "broker_desktop", "local_gateway", 30, 0, "登录免费；不同市场行情权限和额度不同", "https://openapi.futunn.com/futu-api-doc/"),
    ("tencent", "腾讯公开行情", "public_quote", "public_http", 50, 0, "旧数据仅保留审计；通达信模式不再在线调用", "https://gu.qq.com/"),
    ("eastmoney", "东方财富公开行情", "public_quote", "public_http", 51, 0, "旧数据仅保留审计；通达信模式不再在线调用", "https://quote.eastmoney.com/"),
    ("baostock", "BaoStock", "public_api", "public_http", 52, 0, "旧数据仅保留审计；通达信模式不再在线调用", "https://www.baostock.com/"),
    ("bilibili", "哔哩哔哩公开内容", "social_media", "local_yt_dlp", 60, 0, "公开元数据优先；账号只通过本机浏览器会话使用，不保存密码；遵守平台规则和个人信息最小化", "https://www.bilibili.com/"),
    ("x_official", "X API", "social_media", "official_api", 61, 0, "官方读接口按量付费；零付费模式默认禁用，不使用非官方爬虫绕过限制", "https://docs.x.com/x-api/"),
    ("sec_edgar", "SEC EDGAR", "filing", "official_json_api", 62, 0, "免费免 Key；必须提供合规 User-Agent 身份，内部限速低于 SEC 的 10 请求/秒上限", "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"),
    ("local_cache", "本地最后可信快照", "local_cache", "filesystem", 99, 1, "仅保可用性；必须显式标记陈旧，不能伪装实时", "local://data_lake"),
]


def ensure_data_lake() -> Path:
    for item in DATA_DIRS:
        (DATA_LAKE / item).mkdir(parents=True, exist_ok=True)
    return DATA_LAKE


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _series(final_price: float, trend: float, phase: float, days: int = 90):
    raw = []
    for i in range(days):
        progress = i / (days - 1)
        cycle = math.sin(i * 0.31 + phase) * 0.025 + math.sin(i * 0.09) * 0.018
        raw.append((1 + trend * (progress - 1)) * (1 + cycle))
    scale = final_price / raw[-1]
    return [round(v * scale, 4) for v in raw]


_LEGACY_PORTFOLIO_SOURCE_URL = "local://user-input/2026-08-11"
_LEGACY_PORTFOLIO_CLAIM = "截至 2026-08-11 晚持有 512400 与 562500 两只 A 股 ETF"
_LEGACY_BOOTSTRAP_BODY = json.dumps(
    {"fact_opinion_separation": True, "order_execution": False}, ensure_ascii=False
)


def _remove_legacy_synthetic_portfolio(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT id FROM sources WHERE url=? AND source_type='PRIMARY_USER_DATA'",
        (_LEGACY_PORTFOLIO_SOURCE_URL,),
    ).fetchone()
    if marker:
        expected = {
            "512400": (1000.0, 1.922 / .969, 2.06),
            "562500": (4900.0, 1.021 / 1.0339, 1.065),
        }
        for portfolio in conn.execute(
            "SELECT id FROM portfolios WHERE name='当前持仓' AND as_of='2026-08-11'"
        ).fetchall():
            rows = conn.execute(
                """SELECT p.id,a.symbol,p.quantity,p.cost_price,p.highest_since_entry
                   FROM positions p JOIN assets a ON a.id=p.asset_id
                   WHERE p.portfolio_id=?""",
                (portfolio["id"],),
            ).fetchall()
            matched = {}
            for row in rows:
                values = expected.get(row["symbol"])
                if values and all(
                    math.isclose(float(actual), wanted, rel_tol=0, abs_tol=1e-9)
                    for actual, wanted in zip(
                        (row["quantity"], row["cost_price"], row["highest_since_entry"]),
                        values,
                    )
                ):
                    matched[row["symbol"]] = row["id"]
            if set(matched) == set(expected):
                conn.executemany(
                    "DELETE FROM positions WHERE id=?",
                    [(position_id,) for position_id in matched.values()],
                )
                conn.execute(
                    "DELETE FROM portfolios WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM positions WHERE portfolio_id=?)",
                    (portfolio["id"], portfolio["id"]),
                )
        conn.execute(
            "DELETE FROM evidence WHERE source_id=? AND claim=?",
            (marker["id"], _LEGACY_PORTFOLIO_CLAIM),
        )
        conn.execute(
            "DELETE FROM sources WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM evidence WHERE source_id=?)",
            (marker["id"], marker["id"]),
        )
    conn.execute(
        """DELETE FROM reports
           WHERE title='初始投资纪律与待验证假设'
             AND report_type='SYSTEM_BOOTSTRAP'
             AND created_at='2026-08-11'
             AND ABS(evidence_coverage - 0.18) < 0.0000001
             AND body=?""",
        (_LEGACY_BOOTSTRAP_BODY,),
    )


def _seed(conn: sqlite3.Connection) -> None:
    for row in DATA_SOURCES:
        conn.execute(
            """INSERT OR IGNORE INTO data_sources
               (code,name,source_kind,access_mode,priority,enabled,license_note,homepage)
               VALUES(?,?,?,?,?,?,?,?)""", row,
        )
    demo_source = conn.execute("SELECT id FROM data_sources WHERE code='local_cache'").fetchone()[0]
    for index, (symbol, exchange_symbol, name, market, kind, currency, final, trend) in enumerate(ASSETS):
        cursor = conn.execute(
            """INSERT INTO assets(symbol,exchange_symbol,name,market,asset_type,currency,data_status)
               VALUES(?,?,?,?,?,?,'DEMO')""",
            (symbol, exchange_symbol, name, market, kind, currency),
        )
        asset_id = cursor.lastrowid
        closes = _series(final, trend, index * 0.7)
        start = date(2026, 5, 14)
        for day, close in enumerate(closes):
            d = start + timedelta(days=day)
            conn.execute(
                """INSERT INTO prices(asset_id,trade_date,open,high,low,close,volume,source_id,captured_at,is_demo)
                   VALUES(?,?,?,?,?,?,?,?,?,1)""",
                (asset_id, d.isoformat(), close * .997, close * 1.012, close * .988,
                 close, 1_000_000 + day * 173, demo_source, "2026-08-11T21:00:00+08:00"),
            )
    conn.execute(
        """INSERT INTO sources(name,url,source_type,reliability,verification_status,checked_at,notes)
           VALUES(?,?,?,?,?,?,?)""",
        ("上海证券交易所 ETF 公告", "https://www.sse.com.cn/", "PRIMARY_OFFICIAL", .95,
         "VERIFIED", "2026-08-12", "用于核验基金代码和公告"),
    )



def initialize(path: Optional[Path] = None) -> Path:
    db_path = path or DB_PATH
    cache_key = str(db_path.resolve())
    with _INITIALIZE_LOCK:
        if cache_key in _INITIALIZED_PATHS and db_path.exists():
            return db_path
        if path is None:
            ensure_data_lake()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(db_path)) as conn:
            # Journal mode persists with the database and only belongs in the
            # one-time initialization path, never in ordinary read connects.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(quote_snapshots)")}
            if "turnover_rate" not in quote_columns:
                conn.execute("ALTER TABLE quote_snapshots ADD COLUMN turnover_rate REAL")
            portfolio_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(portfolios)")
            }
            for name, declaration in (
                ("source_type", "TEXT NOT NULL DEFAULT 'UNSET'"),
                ("source_name", "TEXT NOT NULL DEFAULT '尚未导入'"),
                ("imported_at", "TEXT"),
                ("verification_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
                ("last_import_id", "INTEGER"),
            ):
                if name not in portfolio_columns:
                    conn.execute(f"ALTER TABLE portfolios ADD COLUMN {name} {declaration}")
            position_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(positions)")
            }
            for name, declaration in (
                ("import_id", "INTEGER"),
                ("as_of", "TEXT NOT NULL DEFAULT ''"),
                ("source_type", "TEXT NOT NULL DEFAULT 'LEGACY_LOCAL'"),
                ("source_name", "TEXT NOT NULL DEFAULT '历史本地记录'"),
                ("verification_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
                ("valuation_status", "TEXT NOT NULL DEFAULT 'UNAVAILABLE'"),
                ("price_observed_at", "TEXT"),
                ("price_source", "TEXT"),
                ("tracking_started_at", "TEXT"),
            ):
                if name not in position_columns:
                    conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {declaration}")
            mandate_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(investment_mandates)")
            }
            for name, default in (
                ("stop_loss_pct", 8), ("take_profit_pct", 20),
                ("trailing_stop_pct", 8),
            ):
                if name not in mandate_columns:
                    conn.execute(
                        f"ALTER TABLE investment_mandates ADD COLUMN {name} "
                        f"REAL NOT NULL DEFAULT {default}"
                    )
            alert_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(alert_outbox)")
            }
            for name, declaration in (
                ("target", "TEXT"),
                ("signal_id", "INTEGER"),
                ("subscription_id", "INTEGER"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("next_attempt_at", "TEXT"),
            ):
                if name not in alert_columns:
                    conn.execute(
                        f"ALTER TABLE alert_outbox ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_alert_outbox_delivery
                   ON alert_outbox(status,next_attempt_at,created_at)"""
            )
            for table, additions in (
                ('signal_subscriptions', [('digest_kinds_json', "TEXT NOT NULL DEFAULT '[]'"),
                                          ('watch_symbols_json', "TEXT NOT NULL DEFAULT '[]'"),
                                          ('send_time', "TEXT NOT NULL DEFAULT '08:30'")]),
                ('alert_outbox', [('received_at', 'TEXT')]),
            ):
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                for name, declaration in additions:
                    if name not in columns:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
            # Archive the original demonstration hypotheses, never user research.
            conn.execute("""UPDATE hypotheses SET status='ARCHIVED'
                WHERE created_at='2026-08-11' AND status='UNVERIFIED'
                AND title IN ('AI 商业模式挤压','美日汇率干预','降息与有色长牛')""")
            learning_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(harness_learning_cycles)")
            }
            for name, declaration in (
                ("progress_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("heartbeat_at", "TEXT"),
                ("worker_token", "TEXT"),
            ):
                if name not in learning_columns:
                    conn.execute(
                        f"ALTER TABLE harness_learning_cycles ADD COLUMN {name} {declaration}"
                    )
            # Reference sources are synchronized once per process start so
            # schema/data-source additions also reach an existing database.
            for row in DATA_SOURCES:
                conn.execute(
                    """INSERT INTO data_sources
                       (code,name,source_kind,access_mode,priority,enabled,license_note,homepage)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET name=excluded.name,source_kind=excluded.source_kind,
                       access_mode=excluded.access_mode,priority=excluded.priority,enabled=excluded.enabled,
                       license_note=excluded.license_note,homepage=excluded.homepage""", row,
                )
            if not conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]:
                _seed(conn)
            synthetic_portfolio_key = "20260901_remove_synthetic_portfolio_snapshot"
            if not conn.execute(
                "SELECT 1 FROM app_migrations WHERE migration_key=?",
                (synthetic_portfolio_key,),
            ).fetchone():
                _remove_legacy_synthetic_portfolio(conn)
                conn.execute(
                    "INSERT INTO app_migrations(migration_key,applied_at) VALUES(?,datetime('now'))",
                    (synthetic_portfolio_key,),
                )
            migration_key = "20260826_report_watchlist_to_comparison_watchlist"
            if not conn.execute(
                "SELECT 1 FROM app_migrations WHERE migration_key=?", (migration_key,)
            ).fetchone():
                conn.execute(
                    """INSERT OR IGNORE INTO comparison_watchlist
                       (symbol,name,first_compared_at,last_compared_at,compare_count)
                       SELECT symbol,name,last_requested_at,last_requested_at,1
                       FROM report_watchlist WHERE enabled=1"""
                )
                conn.execute(
                    "INSERT INTO app_migrations(migration_key,applied_at) VALUES(?,datetime('now'))",
                    (migration_key,),
                )
            # Once a real series exists, synthetic points for that asset must
            # not survive (especially synthetic weekend dates).
            conn.execute(
                """DELETE FROM prices WHERE is_demo=1 AND asset_id IN
                   (SELECT DISTINCT asset_id FROM prices WHERE is_demo=0)"""
            )
            conn.commit()
        _INITIALIZED_PATHS.add(cache_key)
    return db_path


if __name__ == "__main__":
    print(initialize())
