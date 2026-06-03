"""Minimal ICMPv6 Router Advertisement sender.

Sends periodic RA messages from a given interface, advertising a prefix
and a default route with the specified router lifetime.

Run standalone: python3 ra.py <iface> <prefix> <prefix_len> [interval_secs]
  e.g.  python3 ra.py eth0 2001:db8:1:: 64 10
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from typing import Optional

_ALL_NODES = "ff02::1"
_ICMPV6_RA = 134
_ROUTER_LIFETIME = 1800
_OPT_PREFIX = 3
_OPT_LLADDR = 1


def _build_ra(src_mac: bytes, prefix: str, prefix_len: int,
              router_lifetime: int = _ROUTER_LIFETIME) -> bytes:
    prefix_addr = ipaddress.IPv6Network(
        f"{prefix}/{prefix_len}", strict=False
    ).network_address.packed

    ra_hdr = struct.pack("!BBHBBHI",
        _ICMPV6_RA, 0, 0,
        64, 0,
        router_lifetime,
        0,
    ) + struct.pack("!I", 0)

    prefix_opt = struct.pack("!BBBBIII16s",
        _OPT_PREFIX, 4,
        prefix_len,
        0xC0,
        86400, 14400, 0,
        prefix_addr,
    )

    lladdr_opt = struct.pack("!BB", _OPT_LLADDR, 1) + src_mac

    return ra_hdr + prefix_opt + lladdr_opt


class RASender:
    """Sends periodic ICMPv6 Router Advertisement messages."""

    def __init__(self, iface: str, prefix: str, prefix_len: int,
                 interval: float = 5.0,
                 router_lifetime: int = _ROUTER_LIFETIME) -> None:
        self.iface = iface
        self.prefix = prefix
        self.prefix_len = prefix_len
        self.interval = interval
        self.router_lifetime = router_lifetime
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW,
                                  socket.IPPROTO_ICMPV6)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                             self.iface.encode() + b'\x00')
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, 255)
        except OSError:
            return

        src_mac = _get_mac(self.iface)
        pkt = _build_ra(src_mac, self.prefix, self.prefix_len, self.router_lifetime)
        dest = (_ALL_NODES, 0, 0, socket.if_nametoindex(self.iface))

        while not self._stop.is_set():
            try:
                sock.sendto(pkt, dest)
            except OSError:
                pass
            self._stop.wait(self.interval)

        sock.close()


def _get_mac(iface: str) -> bytes:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            parts = f.read().strip().split(":")
            return bytes(int(p, 16) for p in parts)
    except OSError:
        return b'\x00' * 6


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    iface, prefix, plen = args[0], args[1], int(args[2])
    interval = float(args[3]) if len(args) > 3 else 5.0
    sender = RASender(iface, prefix, plen, interval)
    sender.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sender.stop()
