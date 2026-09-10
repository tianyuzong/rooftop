"""Public TDX quote connection, without the broker-specific login packet.

pytdx's default third setup packet identifies a brokerage client. Public TDX
nodes reject that identity while still accepting the TCP connection. The two
public greeting packets establish the read-only quote session by themselves.
"""
from pytdx.hq import TdxHq_API
from pytdx.parser.setup_commands import SetupCmd1, SetupCmd2


class PublicQuoteClient(TdxHq_API):
    def __init__(self):
        super().__init__(raise_exception=True, auto_retry=False, heartbeat=False)

    def setup(self):
        SetupCmd1(self.client).call_api()
        SetupCmd2(self.client).call_api()
