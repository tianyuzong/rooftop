"""Free-first Bilibili, X and SEC collection adapters.

Credentials never enter the project JSON or research database. Bilibili may
reuse a local browser session. X is official-API only. SEC needs only a public
contact identity in ARGUS_SEC_IDENTITY.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..persistence import persist_source_documents


def source_access_status() -> list[dict]:
    return [
        {"code": "bilibili", "configured": os.environ.get("ARGUS_BILIBILI_BROWSER", "").lower() in {"chrome", "edge", "firefox"},
         "cost": "免费", "auth": "可选：你本人在本机浏览器登录；无需把账号交给系统",
         "mode": "yt-dlp 公开元数据；可选复用本机浏览器 Cookie", "writes": False},
        {"code": "x_official", "configured": bool(os.environ.get("ARGUS_X_BEARER_TOKEN")),
         "cost": "官方读接口按量付费", "auth": "开发者项目 Bearer Token，仅写本机环境变量",
         "mode": "官方 X API v2；零付费约束下保持禁用", "writes": False},
        {"code": "sec_edgar", "configured": bool(os.environ.get("ARGUS_SEC_IDENTITY")),
         "cost": "免费", "auth": "无需 SEC 账号；只需 联系邮箱/项目名 User-Agent",
         "mode": "SEC data.sec.gov 官方 JSON，内部限速建议 <=2 请求/秒", "writes": False},
    ]


def collect_bilibili(target: str, use_browser: bool = False) -> dict:
    """Collect metadata only; never download audio or video."""
    if not target.startswith(("https://www.bilibili.com/", "https://b23.tv/", "bilisearch")):
        raise ValueError("B站目标必须是 bilibili/b23 视频或频道 URL，或 bilisearchN:关键词")
    command = [sys.executable, "-m", "yt_dlp", "--dump-single-json", "--skip-download",
               "--flat-playlist", "--playlist-end", "50", target]
    browser = os.environ.get("ARGUS_BILIBILI_BROWSER", "chrome").lower()
    if use_browser:
        if browser not in {"chrome", "edge", "firefox"}:
            raise ValueError("ARGUS_BILIBILI_BROWSER 只允许 chrome/edge/firefox")
        command[3:3] = ["--cookies-from-browser", browser]
    completed = subprocess.run(command, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
                               capture_output=True, text=True, encoding="utf-8", timeout=90,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout)[-1200:])
    raw = json.loads(completed.stdout)
    entries = raw.get("entries") or [raw]
    docs = []
    for item in entries:
        if not item:
            continue
        video_id = item.get("id") or item.get("display_id") or "unknown"
        webpage = item.get("webpage_url") or item.get("url") or f"https://www.bilibili.com/video/{video_id}"
        docs.append({"document_type": "social_video_metadata", "title": item.get("title") or video_id,
                     "body": item.get("description") or "公开视频元数据（未下载音视频内容）",
                     "source_url": webpage, "source_name": "哔哩哔哩",
                     "published_at": item.get("timestamp"), "observed_at": datetime.now(timezone.utc).isoformat(),
                     "metadata": {key: item.get(key) for key in ("id", "uploader", "channel", "duration", "view_count", "like_count", "comment_count")}})
    return persist_source_documents("bilibili", "social", target, docs, json.dumps(raw, ensure_ascii=False))


def collect_x_recent(query: str, max_results: int = 10) -> dict:
    token = os.environ.get("ARGUS_X_BEARER_TOKEN")
    if not token:
        raise RuntimeError("未配置 ARGUS_X_BEARER_TOKEN；X 账号本身不等于 API 权限，官方读接口按量付费")
    max_results = max(10, min(int(max_results), 100))
    url = ("https://api.x.com/2/tweets/search/recent?query=" + quote(query) +
           f"&max_results={max_results}&tweet.fields=created_at,lang,public_metrics,author_id")
    request = Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "ArgusLocalResearch/0.1"})
    with urlopen(request, timeout=30) as response:
        raw_bytes = response.read()
    raw = json.loads(raw_bytes)
    docs = [{"document_type": "social_post", "title": f"X Post {item['id']}", "body": item.get("text", ""),
             "source_url": f"https://x.com/i/web/status/{item['id']}", "source_name": "X official API",
             "published_at": item.get("created_at"), "observed_at": datetime.now(timezone.utc).isoformat(),
             "metadata": {"id": item["id"], "author_id": item.get("author_id"), "lang": item.get("lang"),
                          "public_metrics": item.get("public_metrics", {})}} for item in raw.get("data", [])]
    return persist_source_documents("x_official", "social", query, docs, raw_bytes)


def collect_sec_submissions(cik: str) -> dict:
    identity = os.environ.get("ARGUS_SEC_IDENTITY", "").strip()
    if "@" not in identity:
        raise RuntimeError("请在本机设置 ARGUS_SEC_IDENTITY，例如 'ArgusResearch your-email@example.com'")
    digits = "".join(character for character in cik if character.isdigit()).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{digits}.json"
    request = Request(url, headers={"User-Agent": identity, "Host": "data.sec.gov"})
    with urlopen(request, timeout=30) as response:
        raw_bytes = response.read()
    raw = json.loads(raw_bytes)
    recent = raw.get("filings", {}).get("recent", {})
    docs = []
    for index, accession in enumerate(recent.get("accessionNumber", [])):
        form = recent.get("form", [""])[index]
        primary = recent.get("primaryDocument", [""])[index]
        accession_path = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(digits)}/{accession_path}/{primary}"
        docs.append({"document_type": "sec_filing_index", "title": f"{raw.get('name')} {form} {accession}",
                     "body": f"（事实）SEC EDGAR 申报索引；表单 {form}；文件 {primary}", "source_url": filing_url,
                     "source_name": "SEC EDGAR", "published_at": recent.get("filingDate", [None])[index],
                     "observed_at": datetime.now(timezone.utc).isoformat(),
                     "metadata": {"cik": digits, "ticker": raw.get("tickers", []), "form": form,
                                  "accession_number": accession, "report_date": recent.get("reportDate", [None])[index]}})
    return persist_source_documents("sec_edgar", "filings", digits, docs, raw_bytes)
