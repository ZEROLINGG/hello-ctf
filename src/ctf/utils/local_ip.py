import re
import psutil
import socket
import platform
from ipaddress import IPv4Address, IPv4Network

# 获取操作系统类型
IS_WINDOWS = platform.system() == 'Windows'
IS_LINUX = platform.system() == 'Linux'
IS_MACOS = platform.system() == 'Darwin'

# ==================== 接口排除规则 ====================

# 精确匹配排除（Linux/macOS）
EXCLUDE_EXACT_PATTERNS = [
    r'^lo\d*$',  # 回环接口: lo, lo0, lo1
]

# 前缀排除（Linux/macOS）
EXCLUDE_PREFIX_PATTERNS = [
    r'^docker',  # Docker 网桥
    r'^vbox',  # VirtualBox
    r'^vmnet',  # VMware
    r'^veth',  # 虚拟以太网对
    r'^br-',  # Linux 网桥
    r'^tun',  # TUN 设备
    r'^tap',  # TAP 设备
    r'^utun',  # macOS 用户态隧道
    r'^wsl',  # WSL 适配器
    r'^mihomo',  # Mihomo 代理
    r'^virbr',  # libvirt 网桥
    r'^xenbr',  # Xen 网桥
    r'^dummy',  # 虚拟接口
    r'^bond',  # 绑定接口
    r'^team',  # Team 接口
]

# Windows 特定排除（关键词匹配，不区分大小写）
EXCLUDE_WINDOWS_KEYWORDS = [
    'loopback',  # 回环接口
    'isatap',  # ISATAP 隧道
    '6to4',  # IPv6 to IPv4 隧道
    'teredo',  # Teredo 隧道
    'pseudo',  # 伪接口
    'tunnel',  # 隧道接口
    'virtual',  # 虚拟接口（需谨慎，某些真实网卡也包含此词）
    'hyper-v',  # Hyper-V 虚拟交换机
    'vethernet',  # Hyper-V 虚拟以太网
    'nat',  # NAT 适配器
    'tunneling',  # 隧道
    'miniport',  # WAN Miniport
    'wan miniport',  # Windows WAN 微型端口
    'microsoft wi-fi direct',  # Wi-Fi Direct 虚拟适配器
    'bluetooth',  # 蓝牙网络
]

# ==================== 接口类型评分规则 ====================

# Linux/macOS 有线网卡模式 (30分)
IFACE_WIRED_LINUX = [
    r'^eth\d+',  # eth0, eth1
    r'^en[ospx]\d+',  # eno1, ens33, enp0s3, enx...
    r'^enp\d+s\d+',  # enp2s0
]

# Windows 有线网卡关键词 (30分，不区分大小写)
IFACE_WIRED_WINDOWS = [
    'ethernet',  # 以太网
    'local area connection',  # 本地连接
    'gigabit',  # 千兆网卡
    'realtek',  # Realtek 网卡
    'intel.*ethernet',  # Intel 以太网
    'broadcom',  # Broadcom 网卡
    'marvell',  # Marvell 网卡
    'qualcomm.*ethernet',  # Qualcomm 以太网
    'killer.*ethernet',  # Killer 网卡
]

# Linux/macOS 无线网卡模式 (20分)
IFACE_WIRELESS_LINUX = [
    r'^wlan\d+',  # wlan0, wlan1
    r'^wl',  # wl, wlp2s0
    r'^wlp\d+s\d+',  # wlp3s0
]

# Windows 无线网卡关键词 (20分，不区分大小写)
IFACE_WIRELESS_WINDOWS = [
    'wi-fi',  # Wi-Fi
    'wifi',  # WiFi
    'wireless',  # 无线网络连接
    'wlan',  # WLAN
    '802.11',  # 802.11 协议
    'qualcomm',  # Qualcomm 无线网卡
    'intel.*wi-fi',  # Intel Wi-Fi
    'intel.*wireless',  # Intel Wireless
    'killer.*wi-fi',  # Killer Wi-Fi
    'broadcom.*wireless',  # Broadcom 无线
]

# ==================== 网段评分规则 ====================

NETWORK_SCORE: list[tuple[IPv4Network, int]] = [
    (IPv4Network("10.0.0.0/8"), 15),
    (IPv4Network("172.16.0.0/12"), 10),
    (IPv4Network("192.168.0.0/16"), 5),
]
PUBLIC_SCORE = 25  # 公网 IP 的额外加分


# ==================== 编译正则表达式 ====================

def compile_patterns(patterns: list[str], flags=0) -> list[re.Pattern]:
    """编译正则表达式列表"""
    return [re.compile(pattern, flags) for pattern in patterns]


# 编译排除规则
EXCLUDE_EXACT_COMPILED = compile_patterns(EXCLUDE_EXACT_PATTERNS)
EXCLUDE_PREFIX_COMPILED = compile_patterns(EXCLUDE_PREFIX_PATTERNS)

if IS_WINDOWS:
    EXCLUDE_WINDOWS_COMPILED = compile_patterns(
        [f'.*{keyword}.*' for keyword in EXCLUDE_WINDOWS_KEYWORDS],
        re.IGNORECASE
    )
else:
    EXCLUDE_WINDOWS_COMPILED = []

# 编译接口评分规则
IFACE_WIRED_LINUX_COMPILED = compile_patterns(IFACE_WIRED_LINUX)
IFACE_WIRELESS_LINUX_COMPILED = compile_patterns(IFACE_WIRELESS_LINUX)

if IS_WINDOWS:
    IFACE_WIRED_WINDOWS_COMPILED = compile_patterns(
        [f'.*{keyword}.*' for keyword in IFACE_WIRED_WINDOWS],
        re.IGNORECASE
    )
    IFACE_WIRELESS_WINDOWS_COMPILED = compile_patterns(
        [f'.*{keyword}.*' for keyword in IFACE_WIRELESS_WINDOWS],
        re.IGNORECASE
    )
else:
    IFACE_WIRED_WINDOWS_COMPILED = []
    IFACE_WIRELESS_WINDOWS_COMPILED = []


# ==================== 核心函数 ====================

def get_network_score(ip_obj: IPv4Address) -> int:
    """计算IP网段评分"""
    if ip_obj.is_private:
        for network, score in NETWORK_SCORE:
            if ip_obj in network:
                return score
        return 0
    return PUBLIC_SCORE


def is_valid_ipv4(ip: str) -> bool:
    """验证IPv4地址有效性"""
    try:
        ip_obj = IPv4Address(ip)
        return not (
                ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_unspecified
        )
    except ValueError:
        return False


def match_any_pattern(text: str, patterns: list[re.Pattern]) -> bool:
    """检查文本是否匹配任何一个正则模式"""
    return any(pattern.match(text) for pattern in patterns)


def should_exclude_interface(iface: str, iface_lower: str) -> bool:
    """判断接口是否应该被排除"""
    # 精确匹配排除
    if match_any_pattern(iface_lower, EXCLUDE_EXACT_COMPILED):
        return True

    # 前缀匹配排除
    if match_any_pattern(iface_lower, EXCLUDE_PREFIX_COMPILED):
        return True

    # Windows 特定排除
    if IS_WINDOWS and match_any_pattern(iface, EXCLUDE_WINDOWS_COMPILED):
        return True

    return False


def get_iface_score(iface: str, iface_lower: str) -> int:
    """计算接口类型评分"""
    if IS_WINDOWS:
        # Windows 使用原始大小写匹配（已设置 re.IGNORECASE）
        if match_any_pattern(iface, IFACE_WIRED_WINDOWS_COMPILED):
            return 30
        if match_any_pattern(iface, IFACE_WIRELESS_WINDOWS_COMPILED):
            return 20
    else:
        # Linux/macOS 使用小写匹配
        if match_any_pattern(iface_lower, IFACE_WIRED_LINUX_COMPILED):
            return 30
        if match_any_pattern(iface_lower, IFACE_WIRELESS_LINUX_COMPILED):
            return 20

    return 0


def normalize_interface_name(iface: str) -> str:
    """规范化接口名称"""
    return iface.strip()


def get_ip(verbose: bool = False) -> str | None:
    """
    获取最佳IPv4地址

    Args:
        verbose: 是否打印详细信息

    Returns:
        最佳IPv4地址，如果没有找到则返回 None
    """
    candidates: list[tuple[int, str, str]] = []  # (score, ip, iface_name)
    all_stats = psutil.net_if_stats()

    for iface, addrs in psutil.net_if_addrs().items():
        iface = normalize_interface_name(iface)
        iface_lower = iface.lower()

        # 排除不需要的接口
        if should_exclude_interface(iface, iface_lower):
            if verbose:
                print(f"[跳过] {iface} - 已排除")
            continue

        # 检查接口状态
        stats = all_stats.get(iface)
        if not stats or not stats.isup:
            if verbose:
                print(f"[跳过] {iface} - 接口未启用")
            continue

        # 提取有效的 IPv4 地址
        ipv4 = next(
            (addr.address for addr in addrs
             if addr.family == socket.AF_INET and is_valid_ipv4(addr.address)),
            None
        )
        if not ipv4:
            if verbose:
                print(f"[跳过] {iface} - 无有效IPv4地址")
            continue

        # 计算评分
        ip_obj = IPv4Address(ipv4)
        iface_score = get_iface_score(iface, iface_lower)
        network_score = get_network_score(ip_obj)
        total_score = iface_score + network_score

        candidates.append((total_score, ipv4, iface))

        if verbose:
            ip_type = "公网IP" if not ip_obj.is_private else "私有IP"
            print(f"[候选] {iface:25s} {ipv4:15s} - "
                  f"接口分:{iface_score:2d} + 网段分:{network_score:2d} = 总分:{total_score:2d} ({ip_type})")

    # 按分数降序排列
    candidates.sort(key=lambda x: x[0], reverse=True)

    if verbose and candidates:
        print(f"\n[选中] {candidates[0][2]} - {candidates[0][1]} (得分: {candidates[0][0]})")

    return candidates[0][1] if candidates else None


if __name__ == '__main__':
    print(get_ip(verbose=True))

