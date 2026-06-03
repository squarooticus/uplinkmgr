"""Minimal DHCPv4 server — DISCOVER→OFFER, REQUEST→ACK.

Handles a single client address. Designed for network-namespace e2e tests.
Run standalone with: python3 dhcp4.py <iface> <server_ip> <client_ip> <gateway>
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
from typing import Optional


# DHCP message types
_DISCOVER = 1
_OFFER    = 2
_REQUEST  = 3
_ACK      = 5

# Options
_OPT_SUBNET      = 1
_OPT_ROUTER      = 3
_OPT_LEASE_TIME  = 51
_OPT_MSG_TYPE    = 53
_OPT_SERVER_ID   = 54
_OPT_END         = 255

_MAGIC = b'\x63\x82\x53\x63'
_LEASE_TIME = 3600


def _pack_opts(*opt_pairs) -> bytes:
    buf = b""
    for code, data in opt_pairs:
        buf += bytes([code, len(data)]) + data
    return buf + bytes([_OPT_END])


def _ip(s: str) -> bytes:
    return ipaddress.IPv4Address(s).packed


class DHCPv4Server:
    """Simple single-client DHCPv4 server for test namespaces."""

    def __init__(self, iface: str, server_ip: str, client_ip: str,
                 gateway: str, prefix_len: int = 24) -> None:
        self.iface = iface
        self.server_ip = server_ip
        self.client_ip = client_ip
        self.gateway = gateway
        self.prefix_len = prefix_len
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                               self.iface.encode() + b'\x00')
        self._sock.settimeout(1.0)
        self._sock.bind(("", 67))
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
                data, addr = self._sock.recvfrom(1024)
            except (socket.timeout, OSError):
                continue
            self._handle(data)

    def _handle(self, data: bytes) -> None:
        if len(data) < 236:
            return
        if data[0] != 1 or data[236:240] != _MAGIC:
            return
        msg_type = self._get_msg_type(data[240:])
        if msg_type == _DISCOVER:
            self._send(_OFFER, data)
        elif msg_type == _REQUEST:
            self._send(_ACK, data)

    def _get_msg_type(self, options: bytes) -> int:
        i = 0
        while i < len(options):
            code = options[i]
            if code == _OPT_END:
                break
            if code == 0:
                i += 1
                continue
            if i + 1 >= len(options):
                break
            length = options[i + 1]
            if code == _OPT_MSG_TYPE and length >= 1:
                return options[i + 2]
            i += 2 + length
        return 0

    def _send(self, msg_type: int, request: bytes) -> None:
        xid = request[4:8]
        chaddr = request[28:44]

        subnet_mask = ipaddress.IPv4Network(
            f"0.0.0.0/{self.prefix_len}", strict=False
        ).netmask.packed

        opts = _pack_opts(
            (_OPT_MSG_TYPE, bytes([msg_type])),
            (_OPT_SERVER_ID, _ip(self.server_ip)),
            (_OPT_LEASE_TIME, struct.pack(">I", _LEASE_TIME)),
            (_OPT_SUBNET, subnet_mask),
            (_OPT_ROUTER, _ip(self.gateway)),
        )

        pkt = struct.pack(
            "!BBBB4sHH4s4s4s4s16s64s128s",
            2, 1, 6, 0,
            xid,
            0, 0x8000,
            b'\x00'*4,
            _ip(self.client_ip),
            _ip(self.server_ip),
            b'\x00'*4,
            chaddr,
            b'\x00'*64,
            b'\x00'*128,
        ) + _MAGIC + opts

        try:
            self._sock.sendto(pkt, ("255.255.255.255", 68))
        except OSError:
            pass


if __name__ == "__main__":
    import sys, time
    _, iface, server_ip, client_ip, gateway = sys.argv[:5]
    srv = DHCPv4Server(iface, server_ip, client_ip, gateway)
    srv.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        srv.stop()
