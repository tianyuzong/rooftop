"""Dated market/company digests built only from available, attributed local data."""
from __future__ import annotations

import html
import json
import re
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .alerts import queue_alert
from .db import connect

SHANGHAI = ZoneInfo('Asia/Shanghai')
DIGEST_KINDS = {'MARKET', 'COMPANIES'}
INDEXES = [('000001.SH', '上证指数'), ('399001.SZ', '深证成指'),
           ('399006.SZ', '创业板指'), ('000300.SH', '沪深300')]


def normalize_selection(payload, conn_factory=connect):
    kinds = payload.get('digest_kinds') or []
    if not isinstance(kinds, list) or not set(kinds).issubset(DIGEST_KINDS):
        raise ValueError('日报内容请选择大盘总结或关注公司')
    raw = payload.get('watch_stocks', payload.get('watch_symbols', [])) or []
    if isinstance(raw, str):
        raw = [v for v in re.split(r'[\s,，;；、]+', raw.strip()) if v]
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError('关注股票最多选择 20 只')
    stocks = []
    with closing(conn_factory()) as conn:
        for token in raw:
            token = str(token).strip()
            row = conn.execute('SELECT symbol,name FROM a_share_universe_assets WHERE symbol=? OR name=? LIMIT 1', (token, token)).fetchone()
            if row is None:
                row = conn.execute('SELECT symbol,name FROM assets WHERE symbol=? OR name=? LIMIT 1', (token, token)).fetchone()
            if row is None and not re.fullmatch(r'\d{6}', token):
                raise ValueError(f'无法识别股票“{token}”，请填写六位股票代码')
            symbol = row['symbol'] if row else token
            if not re.fullmatch(r'\d{6}', symbol):
                raise ValueError('关注公司请填写 A 股公司名称或六位股票代码')
            if symbol not in stocks:
                stocks.append(symbol)
    if 'COMPANIES' in kinds and not stocks:
        raise ValueError('请选择至少一家关注公司')
    send_time = str(payload.get('send_time') or '08:30')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', send_time):
        raise ValueError('发送时间格式为 HH:MM（北京时间）')
    return sorted(set(kinds)), stocks, send_time


def _quote(conn, symbol, day):
    row = conn.execute('''SELECT q.price,q.change_pct,q.observed_at,q.volume,q.amount,
        d.name source FROM quote_snapshots q JOIN data_sources d ON d.id=q.source_id
        WHERE q.asset_symbol=? AND date(q.observed_at,'+8 hours')=?
        ORDER BY julianday(q.observed_at) DESC,q.captured_at DESC LIMIT 1''', (symbol, day)).fetchone()
    if row:
        return dict(row)
    bar = conn.execute('''SELECT b.close price,b.trade_date observed_at,b.volume,b.amount,
        d.name source FROM market_daily_bars b JOIN data_sources d ON d.id=b.source_id
        WHERE b.asset_symbol=? AND b.trade_date=? ORDER BY b.captured_at DESC LIMIT 1''', (symbol, day)).fetchone()
    if not bar:
        return None
    item = dict(bar)
    previous = conn.execute('''SELECT close FROM market_daily_bars WHERE asset_symbol=?
        AND trade_date<? ORDER BY trade_date DESC,captured_at DESC LIMIT 1''', (symbol, day)).fetchone()
    item['change_pct'] = (item['price'] / previous[0] - 1) * 100 if previous and previous[0] else None
    return item


def _news(conn, day, terms, limit=6):
    condition = ' OR '.join('(title LIKE ? OR body LIKE ?)' for _ in terms)
    params = [v for term in terms for v in (f'%{term}%', f'%{term}%')]
    return [dict(r) for r in conn.execute(f'''SELECT title,body,source_name,source_url,published_at
        FROM source_documents WHERE substr(published_at,1,10)=? AND ({condition})
        ORDER BY published_at DESC LIMIT ?''', (day, *params, limit))]


def _price_line(label, quote):
    if not quote:
        return f'{label}：当天行情缺失，未使用其他日期或演示行情替代。'
    change = f"{quote['change_pct']:+.2f}%" if quote.get('change_pct') is not None else '涨跌幅缺失'
    return f"{label}：{quote['price']:,.2f}，{change}；截至 {quote['observed_at']}；来源：{quote['source']}。"


def build_digest(payload, conn_factory=connect):
    kinds, symbols, _ = normalize_selection(payload, conn_factory)
    if not kinds:
        raise ValueError('请选择日报内容')
    today = datetime.now(SHANGHAI).date()
    day = date.fromisoformat(str(payload.get('report_date') or today - timedelta(days=1)))
    if day > today:
        raise ValueError('不能生成未来日期的总结')
    day_text = day.isoformat()
    sections, missing = [], []
    with closing(conn_factory()) as conn:
        if 'MARKET' in kinds:
            quotes = [(name, _quote(conn, symbol, day_text)) for symbol, name in INDEXES]
            lines = [_price_line(name, quote) for name, quote in quotes]
            available = [(name, q) for name, q in quotes if q and q.get('change_pct') is not None]
            if available:
                lines.insert(0, '已取得的指数行情：' + '；'.join(f"{name}{q['change_pct']:+.2f}%" for name, q in available) + '。')
            else:
                lines.insert(0, '本机缺少当日指数行情，下列公开报道仅作为有来源的补充。')
            missing.extend(name for name, quote in quotes if not quote)
            sections.append({'title': '大盘总结', 'lines': lines,
                             'news': _news(conn, day_text, ['收评', '沪指', 'A股收盘'])})
        if 'COMPANIES' in kinds:
            for symbol in symbols:
                row = conn.execute('SELECT name FROM a_share_universe_assets WHERE symbol=?', (symbol,)).fetchone()
                row = row or conn.execute('SELECT name FROM assets WHERE symbol=?', (symbol,)).fetchone()
                name = row[0] if row else symbol
                quote = _quote(conn, symbol, day_text)
                lines = [_price_line(name, quote)]
                if not quote:
                    missing.append(name)
                financial = conn.execute('''SELECT report_date,notice_date,revenue,net_profit,
                    revenue_yoy_pct,net_profit_yoy_pct,roe_pct,source_code FROM fundamental_reports
                    WHERE symbol=? AND notice_date<=? ORDER BY report_date DESC,notice_date DESC LIMIT 1''', (symbol, day_text)).fetchone()
                if financial:
                    lines.append(f"截至当日已公告财报：{financial['report_date']}，公告日 {financial['notice_date']}；来源：{financial['source_code']}。")
                    for field, label, ratio in [('revenue','营业收入',False), ('net_profit','净利润',False), ('revenue_yoy_pct','营收同比',True), ('net_profit_yoy_pct','净利润同比',True), ('roe_pct','净资产收益率',True)]:
                        value = financial[field]
                        lines.append(f'{label}：' + ('缺失' if value is None else f'{value:,.2f}%' if ratio else f'{value / 1e8:,.2f} 亿元'))
                else:
                    lines.append('截至当日已公告财报：本机暂无可核验数据。')
                sections.append({'title': f'{name} · {symbol}', 'lines': lines,
                                 'news': _news(conn, day_text, [name, symbol], 5)})
    subject = f'[Rooftop] {day_text} ' + '与'.join({'MARKET':'大盘总结','COMPANIES':'关注公司简报'}[kind] for kind in kinds)
    lines = [subject, f'报告日期：{day_text}（北京时间）', '以下使用本机已保存的公开资料；缺失信息单独标注。', '']
    for section in sections:
        lines.extend([section['title'], *section['lines'], '当日公告与新闻：'])
        if not section['news']:
            lines.append('本机未检索到当日报道；这不代表公司当天没有发生事件。')
        for news in section['news']:
            lines.extend([f"• {news['title']}（{news['source_name']}，{news['published_at']}）",
                          news['source_url'] or '原文链接缺失'])
        lines.append('')
    lines.append('仅用于研究与信息跟踪。关注名单与真实持仓分开管理。')
    body = '\n'.join(lines)
    return {'subject': subject, 'body': body, 'report_date': day_text, 'sections': sections,
            'missing_data': missing, 'digest_kinds': kinds, 'watch_symbols': symbols,
            'html': '<div style="font:15px/1.8 sans-serif;max-width:760px;margin:auto"><pre style="white-space:pre-wrap;font:inherit">' + html.escape(body) + '</pre></div>'}


def queue_digest(subscription_id, report_date=None, conn_factory=connect):
    with closing(conn_factory()) as conn:
        sub = conn.execute('SELECT * FROM signal_subscriptions WHERE id=? AND enabled=1', (int(subscription_id),)).fetchone()
    if not sub:
        raise ValueError('请先保存并启用订阅')
    digest = build_digest({'digest_kinds': json.loads(sub['digest_kinds_json']),
                           'watch_symbols': json.loads(sub['watch_symbols_json']), 'report_date': report_date}, conn_factory)
    dedupe = f"digest:{sub['id']}:{digest['report_date']}"
    queued = queue_alert(dedupe, digest['subject'], digest['body'], target=sub['target'], subscription_id=sub['id'],
                         metadata={'kind':'DAILY_DIGEST', 'report_date':digest['report_date'], 'html':digest['html']}, conn_factory=conn_factory)
    with closing(conn_factory()) as conn:
        if not queued:
            conn.execute("""UPDATE alert_outbox SET subject=?,body=?,metadata_json=?,status='PENDING',
                attempts=0,next_attempt_at=NULL,error=NULL,created_at=? WHERE dedupe_key=? AND status='CANCELLED'""",
                (digest['subject'],digest['body'],json.dumps({'kind':'DAILY_DIGEST','report_date':digest['report_date'],'html':digest['html']},ensure_ascii=False),
                 datetime.now(SHANGHAI).isoformat(),dedupe))
            conn.commit()
        row = conn.execute('SELECT id,status,error,sent_at FROM alert_outbox WHERE dedupe_key=?', (dedupe,)).fetchone()
    return {'queued':queued, 'message':dict(row), 'digest':digest}


def queue_due_digests(now=None, conn_factory=connect):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    day = (now.date() - timedelta(days=1)).isoformat()
    with closing(conn_factory()) as conn:
        rows = conn.execute("SELECT id,send_time FROM signal_subscriptions WHERE enabled=1 AND digest_kinds_json!='[]'").fetchall()
    queued = 0
    for row in rows:
        if now.strftime('%H:%M') < row['send_time']:
            continue
        with closing(conn_factory()) as conn:
            exists = conn.execute('SELECT 1 FROM alert_outbox WHERE dedupe_key=?', (f"digest:{row['id']}:{day}",)).fetchone()
        if not exists:
            queued += int(queue_digest(row['id'], day, conn_factory)['queued'])
    return {'queued':queued}
