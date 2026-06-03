"""Minimal DHCPv6 + Prefix Delegation server.

Handles SOLICIT → ADVERTISE → REQUEST → REPLY, including IA_PD delegation.
Also handles RENEW and REBIND for the delegated prefix.

Run standalone: python3 dhcp6.py <iface> <delegated_prefix> <prefix_len>
  e.g.  python3 dhcp6.py eth0 2001:db8:1:: 56
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from typing import Optional

# DHCPv6 message types
_SOLICIT   = 1
_ADVERTISE = 2
_REQUEST   = 3
_REPLY     = 7
_RENEW     = 5
_REBIND    = 6

# Option codes
_OPT_CLIENTID   = 1
_OPT_SERVERID   = 2
_OPT_IA_PD      = 25
_OPT_IAPREFIX   = 26

_ALL_DHCPV6_SERVERS = "ff02::1:2"
_SERVER_DUID = b'\x00\x03\x00\x01' + b'\x52\x00\x00\x00\x00\x00'  # DUID-LL
_VLTIME = 86400
_PLTIME = 14400


def _opt(code: int, data: bytes) -> bytes:
    return struct.pack("!HH", code, len(data)) + data


def _build_reply(msg_type: int, xid: bytes, client_duid: bytes,
                 prefix_network: str, prefix_len: int) -> bytes:
    net = ipaddress.IPv6Network(f"{prefix_network}/{prefix_len}", strict=False)
    prefix_bytes = net.network_address.packed

    iaprefix = _opt(_OPT_IAPREFIX, struct.pack("!II", _PLTIME, _VLTIME)
                    + bytes([prefix_len]) + prefix_bytes)
    iapd_data = struct.pack("!III", 2, 1800, 3000) + iaprefix
    iapd = _opt(_OPT_IA_PD, iapd_data)
    server_id = _opt(_OPT_SERVERID, _SERVER_DUID)
    client_id = _opt(_OPT_CLIENTID, client_duid)

    body = server_id + client_id + iapd
    return bytes([msg_type]) + xid + body


class DHCPv6PDServer:
    """Minimal DHCPv6 PD server for test namespaces."""

    def __init__(self, iface: str, delegated_prefix: str, prefix_len: int = 56) -> None:
        self.iface = iface
        self.delegated_prefix = delegated_prefix
        self.prefix_len = prefix_len
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                               self.iface.encode() + b'\x00')
        try:
            ifindex = socket.if_nametoindex(self.iface)
            mreq = ipaddress.IPv6Address(_ALL_DHCPV6_SERVERS).packed + struct.pack("I", ifindex)
            self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
        except OSError:
            pass
        self._sock.settimeout(1.0)
        self._sock.bind(("", 547))
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=3)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            self._handle(data, addr)

    def _handle(self, data: bytes, addr: tuple) -> None:
        if len(data) < 4:
            return
        msg_type = data[0]
        xid = data[1:4]
        client_duid = self._extract_client_duid(data[4:])
        if client_duid is None:
            client_duid = b'\x00\x03\x00\x01' + b'\x00' * 6

        if msg_type in (_SOLICIT, _REQUEST, _RENEW, _REBIND):
            reply_type = _ADVERTISE if msg_type == _SOLICIT else _REPLY
            pkt = _build_reply(reply_type, xid, client_duid,
                                self.delegated_prefix, self.prefix_len)
            try:
                self._sock.sendto(pkt, addr)
            except OSError:
                pass

    def _extract_client_duid(self, options: bytes) -> Optional[bytes]:
        i = 0
        while i + 4 <= len(options):
            code, length = struct.unpack_from("!HH", options, i)
            i += 4
            if i + length > len(options):
                break
            if code == _OPT_CLIENTID:
                return options[i:i + length]
            i += length
        return None


if __name__ == "__main__":
    import sys
    _, iface, prefix, plen = sys.argv[:4]
    srv = DHCPv6PDServer(iface, prefix, int(plen))
    srv.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        srv.stop()
