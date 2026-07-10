"""E2E test fixtures and skip guards.

Tests in this directory require:
  - Root / CAP_NET_ADMIN (to create network namespaces)
  - dhcpcd, ip installed on the host
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.helpers import netns as ns

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.geteuid() != 0, reason="e2e tests require root"),
    pytest.mark.skipif(
        not all(shutil.which(t) for t in ["dhcpcd", "ip"]),
        reason="dhcpcd or ip not found",
    ),
]

_HAVE_RADVD = bool(shutil.which("radvd"))
_HOOK_PATH = str(Path(__file__).parent.parent.parent / "hooks" / "dhcpcd-hook")


@dataclass
class Topology:
    ns_router: str
    ns_isp1: str
    ns_isp2: str
    ns_client: str
    wan1: str
    wan1_isp: str
    wan2: str
    wan2_isp: str
    lan: str
    lan_client: str
    isp1_gw: str
    isp1_wan_ip: str
    isp2_gw: str
    isp2_wan_ip: str
    lan_router_ip: str
    lan_client_ip: str
    isp1_v6_gw: str
    isp1_delegated_prefix: str = "2001:db8:1::"
    isp1_prefix_len: int = 56
    state_dir: str = "/run/uplinkmgr"

    @property
    def mv0(self) -> str:
        return f"{self.lan}-u0"

    @property
    def mv1(self) -> str:
        return f"{self.lan}-u1"


@pytest.fixture
def topology(tmp_path):
    uid = f"{os.getpid() % 9999}"
    r   = f"um-r{uid}"
    i1  = f"um-i1{uid}"
    i2  = f"um-i2{uid}"
    cli = f"um-cl{uid}"

    topo = Topology(
        ns_router=r, ns_isp1=i1, ns_isp2=i2, ns_client=cli,
        wan1="wan1", wan1_isp="wan1-isp",
        wan2="wan2", wan2_isp="wan2-isp",
        lan="lan", lan_client="lan-cli",
        isp1_gw="172.16.1.1", isp1_wan_ip="172.16.1.2",
        isp2_gw="172.16.2.1", isp2_wan_ip="172.16.2.2",
        lan_router_ip="192.168.0.1", lan_client_ip="192.168.0.10",
        isp1_v6_gw="fe80::1",
        state_dir=str(tmp_path / "run"),
    )
    os.makedirs(topo.state_dir, exist_ok=True)

    for name in (r, i1, i2, cli):
        ns.create_ns(name)

    try:
        ns.add_veth(topo.wan1, topo.wan1_isp, ns=r, peer_ns=i1)
        ns.add_veth(topo.wan2, topo.wan2_isp, ns=r, peer_ns=i2)
        ns.add_veth(topo.lan, topo.lan_client, ns=r, peer_ns=cli)

        ns.link_up(i1, "lo"); ns.link_up(i1, topo.wan1_isp)
        ns.add_addr(i1, topo.wan1_isp, f"{topo.isp1_gw}/24")

        ns.link_up(i2, "lo"); ns.link_up(i2, topo.wan2_isp)
        ns.add_addr(i2, topo.wan2_isp, f"{topo.isp2_gw}/24")

        ns.link_up(r, "lo"); ns.link_up(r, topo.lan)
        ns.add_addr(r, topo.lan, f"{topo.lan_router_ip}/24")
        ns.link_up(r, topo.wan1); ns.link_up(r, topo.wan2)
        ns.add_macvlan(r, topo.mv0, topo.lan)
        ns.link_up(r, topo.mv0)

        ns.link_up(cli, "lo"); ns.link_up(cli, topo.lan_client)
        ns.add_addr(cli, topo.lan_client, f"{topo.lan_client_ip}/24")
        ns.add_route(cli, "default", topo.lan_router_ip)
        ns.set_log_dir(tmp_path)

        yield topo

    finally:
        for name in (r, i1, i2, cli):
            ns.delete_ns(name)


def write_env_files(tmp_path: Path, topo: Topology) -> Path:
    env_dir = tmp_path / "env"
    env_dir.mkdir(exist_ok=True)
    (env_dir / f"{topo.wan1}.env").write_text(
        f"UPLINKMGR_UPLINK_NAME=isp1\n"
        f"UPLINKMGR_WAN_IFACE={topo.wan1}\n"
        f"UPLINKMGR_IPV6_PD=true\n"
        f"UPLINKMGR_IPV6_IA_NA=true\n"
    )
    (env_dir / f"{topo.wan2}.env").write_text(
        f"UPLINKMGR_UPLINK_NAME=isp2\n"
        f"UPLINKMGR_WAN_IFACE={topo.wan2}\n"
        f"UPLINKMGR_IPV6_PD=false\n"
        f"UPLINKMGR_IPV6_IA_NA=false\n"
    )
    return env_dir


def write_hook_runner(tmp_path: Path, env_dir: Path, state_dir: str) -> Path:
    runner = tmp_path / "run-hooks"
    runner.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        export UPLINKMGR_ENV_DIR="{env_dir}"
        export UPLINKMGR_STATE_DIR="{state_dir}"
        UPLINKMGR_PID_FILE="${{UPLINKMGR_STATE_DIR}}/uplinkmgr.pid"
        export UPLINKMGR_PID_FILE
        export UPLINKMGR_HOOK_LOG="{state_dir}/hook.log"
        . "{_HOOK_PATH}"
    """))
    runner.chmod(0o755)
    return runner


def write_dhcpcd_conf(tmp_path: Path, topo: Topology) -> Path:
    env_dir = write_env_files(tmp_path, topo)
    runner = write_hook_runner(tmp_path, env_dir, topo.state_dir)
    conf = tmp_path / "dhcpcd.conf"
    conf.write_text(textwrap.dedent(f"""\
        script {runner}

        allowinterfaces {topo.wan1} {topo.mv0} {topo.wan2}

        interface {topo.wan1}
            metric 100
            ipv6rs
            ia_na 1
            ia_pd 2/::/{topo.isp1_prefix_len} {topo.mv0}/0/64
            duid

        interface {topo.wan2}
            metric 200
    """))
    return conf


def write_uplinkmgr_yaml(tmp_path: Path, topo: Topology) -> Path:
    cfg = tmp_path / "uplinkmgr.yaml"
    cfg.write_text(textwrap.dedent(f"""\
        uplinkmgr:
          routing_table_start: 160
          rule_priority_start: 29000
          radvd_min_restart_interval: 5
          monitor:
            interval: 3
            failure_threshold: 2
            recovery_threshold: 2
            v4_hosts:
              - {topo.isp1_gw}
              - {topo.isp2_gw}
            ping_count: 1

          networks:
            - name: lan
              interface: {topo.lan}

          uplinks:
            - name: isp1
              interface: {topo.wan1}
              ipv6_pd: true
              ipv6_pd_hint: {topo.isp1_prefix_len}
              ia_na: true
            - name: isp2
              interface: {topo.wan2}
              ipv6_pd: false
    """))
    return cfg
