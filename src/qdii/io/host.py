"""macOS 主机事实与防睡眠（ADD-0 §8.2）。

- 防睡眠：进程自己持有 `caffeinate -i -m -s -w <pid>` 电源断言，进程退出即释放；
  不修改系统 pmset 设置。合盖仍会睡眠。
- 电源来源、NTP 偏移、磁盘余量：只读查询，写入状态页。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostFacts:
    power_source: str | None  # "AC Power" / "Battery Power"
    ntp_offset_ms: float | None
    disk_free_gb: float
    sleep_assertion_alive: bool
    firewall_blocks_lan: bool | None = None


def hold_sleep_assertion() -> subprocess.Popen[bytes] | None:
    exe = shutil.which("caffeinate")
    if exe is None:
        return None
    return subprocess.Popen([exe, "-i", "-m", "-s", "-w", str(os.getpid())])


def _run(cmd: list[str], timeout: float = 10) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def power_source() -> str | None:
    out = _run(["pmset", "-g", "batt"])
    m = re.search(r"Now drawing from '([^']+)'", out or "")
    return m.group(1) if m else None


def ntp_offset_ms(server: str = "time.apple.com") -> float | None:
    """只查询偏移（sntp 不带 -S 不会修改系统时间）。"""
    out = _run(["sntp", "-t", "5", server], timeout=15)
    m = re.search(r"^([+-]\d+\.\d+)\s+\+/-", out or "", re.MULTILINE)
    return float(m.group(1)) * 1000 if m else None


def disk_free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1e9


def firewall_blocks_all_incoming() -> bool | None:
    """macOS 应用防火墙是否处于“阻止所有传入连接”模式；该模式下局域网访问不可达，回环不受影响。"""
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getblockall"])
    if out is None:
        return None
    return "blocking all" in out.lower()


def lan_ipv4(interface: str) -> tuple[str, str] | None:
    """返回 (IPv4 地址, CIDR 子网)，例如 ("192.168.1.23", "192.168.1.0/24")。"""
    import ipaddress

    out = _run(["ifconfig", interface])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", out or "")
    if not m:
        return None
    ip, mask = m.group(1), int(m.group(2), 16)
    prefix = mask.bit_count()
    net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    if not net.is_private:
        return None
    return ip, str(net)
