import unittest
from email.message import Message
from unittest.mock import Mock, patch

from app import server


class LocalConnectionTests(unittest.TestCase):
    def authorize(self, peer="127.0.0.1", host="127.0.0.1:8765", extra=None,
                  local=True, token=None, mutation=False):
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = (peer, 50123)
        handler.server = Mock(server_port=8765)
        handler.command = "POST" if mutation else "GET"
        handler.headers = Message()
        handler.headers["Host"] = host
        if local:
            handler.headers["X-Argus-Local"] = "1"
        if token:
            handler.headers["X-Argus-Token"] = token
        for name, value in (extra or {}).items():
            handler.headers[name] = value
        handler._json = Mock()
        with patch.object(server, "_configured_remote_token", return_value="test-token"), \
                patch.object(server, "_within_rate_limit", return_value=True), \
                patch.object(server, "_audit_api"), \
                patch.dict(server.os.environ, {"ARGUS_ALLOWED_HOSTS": "", "ARGUS_ALLOWED_ORIGINS": ""}):
            result = handler._authorize_api("/api/quant/decision", mutation=mutation)
        return result, handler._json

    def test_direct_local_browser_connects_without_token(self):
        for peer, host in [("127.0.0.1", "127.0.0.1:8765"),
                           ("::1", "[::1]:8765"),
                           ("::ffff:127.0.0.1", "localhost:8765")]:
            with self.subTest(peer=peer):
                allowed, reply = self.authorize(peer, host, {
                    "Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"
                }, mutation=True)
                self.assertTrue(allowed)
                reply.assert_not_called()

    def test_remote_and_forwarded_requests_still_require_token(self):
        cases = [
            {"peer": "192.168.1.8"},
            {"peer": "::ffff:192.168.1.8"},
            {"peer": "2001:db8::1"},
            {"host": "example.com:8765"},
            {"host": "127.0.0.1.attacker.test:8765"},
            {"host": "localhost:80"},
            {"local": False},
            {"extra": {"Forwarded": "for=192.168.1.8"}},
            {"extra": {"X-Forwarded-For": "192.168.1.8"}},
            {"extra": {"X-Forwarded-Host": "example.com"}},
            {"extra": {"X-Real-IP": "192.168.1.8"}},
            {"extra": {"Via": "proxy"}},
            {"extra": {"Origin": "http://attacker.test"}},
            {"extra": {"Origin": "null"}},
            {"extra": {"Sec-Fetch-Site": "cross-site"}},
            {"extra": {"Host": "attacker.test"}},
        ]
        for case in cases:
            with self.subTest(case=case):
                allowed, reply = self.authorize(**case)
                self.assertFalse(allowed)
                self.assertEqual(reply.call_args.args[1], 401)

    def test_remote_valid_token_still_works(self):
        allowed, _ = self.authorize(peer="192.168.1.8", host="example.com:8765",
                                    local=False, token="test-token")
        self.assertTrue(allowed)
        allowed, reply = self.authorize(peer="192.168.1.8", token="wrong")
        self.assertFalse(allowed)
        self.assertEqual(reply.call_args.args[1], 403)

    def test_local_cross_origin_mutation_is_rejected(self):
        allowed, reply = self.authorize(extra={"Origin": "http://attacker.test"}, mutation=True)
        self.assertFalse(allowed)
        self.assertEqual(reply.call_args.args[1], 403)

    def test_local_connection_respects_explicit_host_allowlist(self):
        with patch.dict(server.os.environ, {"ARGUS_ALLOWED_HOSTS": "example.com"}):
            self.assertFalse(server._request_host_allowed("127.0.0.1:8765"))


if __name__ == "__main__":
    unittest.main()
