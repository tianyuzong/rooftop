"""Local SMTP settings; Windows DPAPI protects the saved authorization code."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import ssl
import threading
from pathlib import Path

from .db import DATA_LAKE

SETTINGS_PATH = DATA_LAKE / 'secrets' / 'email_settings.json'
_lock = threading.RLock()


def _protect(value: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise ValueError('此配置入口需要 Windows 凭据加密；其他系统请配置 SMTP 环境变量')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, result = Blob(len(value), buffer), Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError('无法读取本机加密的邮箱授权码，请重新填写')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


def get_settings() -> dict:
    with _lock:
        saved = json.loads(SETTINGS_PATH.read_text(encoding='utf-8')) if SETTINGS_PATH.exists() else {}
    password = ''
    if saved.get('password_protected'):
        password = _protect(base64.b64decode(saved['password_protected']), True).decode('utf-8')
    return {
        'host': saved.get('host', os.environ.get('ARGUS_SMTP_HOST', '')),
        'port': int(saved.get('port', os.environ.get('ARGUS_SMTP_PORT', '465'))),
        'user': saved.get('user', os.environ.get('ARGUS_SMTP_USER', '')),
        'password': password or os.environ.get('ARGUS_SMTP_PASSWORD', ''),
        'security': saved.get('security', os.environ.get('ARGUS_SMTP_SECURITY', 'ssl')),
        'enabled': bool(saved.get('enabled', os.environ.get('ARGUS_EMAIL_SEND_ENABLED') == '1')),
        'default_target': saved.get('default_target', os.environ.get('ARGUS_ALERT_TO', '')),
    }


def public_settings() -> dict:
    try:
        settings = get_settings()
    except ValueError as exc:
        return {'configured': False, 'send_enabled': False, 'error': str(exc), 'password_configured': False}
    return {'configured': all(settings.get(k) for k in ('host', 'user', 'password')),
            'send_enabled': settings['enabled'], 'host': settings['host'], 'port': settings['port'],
            'user': settings['user'], 'security': settings['security'],
            'default_target': settings['default_target'],
            'default_target_configured': bool(settings['default_target']),
            'password_configured': bool(settings['password']),
            'provider': 'SMTP', 'default': 'dry_run', 'order_execution': False}


def save_settings(payload: dict) -> dict:
    previous = get_settings()
    host = str(payload.get('host') or '').strip()
    user = str(payload.get('user') or '').strip()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host):
        raise ValueError('SMTP 主机格式无效')
    if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', user):
        raise ValueError('请填写有效的发件邮箱')
    security = str(payload.get('security') or 'ssl')
    port = int(payload.get('port', 465))
    if security not in {'ssl', 'starttls'} or not 1 <= port <= 65535:
        raise ValueError('请选择 SSL 或 STARTTLS 及有效端口')
    password = str(payload.get('password') or '')
    if not password and (host, user) == (previous['host'], previous['user']):
        password = previous['password']
    if not password:
        raise ValueError('请填写邮箱 SMTP 授权码')
    target = str(payload.get('default_target') or user).strip()
    if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', target):
        raise ValueError('默认收件邮箱格式无效')
    saved = {'host': host, 'port': port, 'user': user, 'security': security,
             'enabled': payload.get('enabled') is True,
             'default_target': target,
             'password_protected': base64.b64encode(_protect(password.encode('utf-8'))).decode('ascii')}
    with _lock:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        temporary.replace(SETTINGS_PATH)
    return public_settings()


def smtp_client(smtplib_module, settings: dict):
    context = ssl.create_default_context()
    if settings['security'] == 'starttls':
        client = smtplib_module.SMTP(settings['host'], settings['port'], timeout=20)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        except Exception:
            client.close()
            raise
        return client
    return smtplib_module.SMTP_SSL(settings['host'], settings['port'], timeout=20, context=context)


def verify_connection() -> dict:
    import smtplib
    settings = get_settings()
    if not all(settings.get(k) for k in ('host', 'user', 'password')):
        raise ValueError('请先保存发件邮箱和 SMTP 授权码')
    try:
        with smtp_client(smtplib, settings) as client:
            client.login(settings['user'], settings['password'])
            client.noop()
        return {'status': 'CONNECTED', 'detail': 'SMTP 登录成功；请发送日报并确认收件箱实际收到'}
    except Exception as exc:
        return {'status': 'FAILED', 'detail': str(exc).replace(settings['password'], '[已隐藏]')[:500]}
