"""Default files written on first run (``hmaproxy init`` or first web start)."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_TEMPLATE = """\
# HMA Proxy Manager - cấu hình
provider: hma
language: vi                # vi | en
max_connections: 5          # theo gói HMA của bạn (5 hoặc 10)
ovpn_dir: ./ovpn            # đặt các file .ovpn tải từ HMA vào đây
bind_address: 127.0.0.1     # đổi thành 0.0.0.0 nếu muốn máy khác trong mạng dùng proxy
base_port: 8881             # proxy HTTP: 8881, 8882, ...

health_check:
  interval_seconds: 60
  failure_threshold: 3
  ip_check_url: https://api.ipify.org

recovery:
  max_restarts: 3           # restart tối đa bấy nhiêu lần trước khi đổi server
  backoff_base_seconds: 30  # chờ 30s, 60s, 120s ... giữa các lần thử
  backoff_max_seconds: 900
  switch_server: true       # đổi sang file .ovpn khác cùng quốc gia nếu vẫn hỏng

proxy_auth:
  enabled: false
  user: ""
  password: ""

socks5:
  enabled: false            # true = thêm cổng SOCKS5 cho mỗi proxy
  base_port: 1081           # SOCKS5: 1081, 1082, ...

gateway:
  enabled: false            # một cổng xoay vòng qua tất cả proxy đang khỏe
  http_port: 8880
  socks_port: 1080
  mode: roundrobin          # roundrobin | random

openvpn:
  path: ""                  # để trống = tự tìm C:\\Program Files\\OpenVPN\\bin\\openvpn.exe
  driver: tap-windows6      # tap-windows6 (khuyên dùng) | wintun | ovpn-dco
  adapter_prefix: HMA-Proxy # card mạng ảo sẽ tên HMA-Proxy-<id>

dns:                        # DNS cho proxy, truy vấn đi qua chính tunnel VPN
  servers: [1.1.1.1, 8.8.8.8, 9.9.9.9]

web:
  bind: 127.0.0.1
  port: 8800
  password: ""              # đặt mật khẩu nếu mở giao diện web cho máy khác
  open_browser: true

# Cách 1: liệt kê từng proxy (tên file đúng như trong thư mục ovpn),
# hoặc thêm qua giao diện web: tab Cài đặt > Thêm proxy. Ví dụ:
#   - id: jp
#     ovpn: Japan.Tokyo.TCP.ovpn
#   - id: sg
#     ovpn: Singapore.TCP.ovpn
proxies: []

# Cách 2: chọn ngẫu nhiên N server khác quốc gia (bỏ comment để dùng)
# selection:
#   mode: random
#   count: 3
#   distinct_countries: true
#   countries: []           # để trống = mọi quốc gia
"""

ENV_TEMPLATE = """\
# Thông tin đăng nhập VPN của HMA (không phải email/mật khẩu web).
HMA_USER=
HMA_PASS=
"""


def init_project(base: Path) -> list[tuple[Path, bool]]:
    """Create config.yaml, .env and ovpn/ if missing. Returns (path, created)."""
    out = []
    base.mkdir(parents=True, exist_ok=True)
    cfg = base / "config.yaml"
    if not cfg.exists():
        cfg.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        out.append((cfg, True))
    else:
        out.append((cfg, False))
    env = base / ".env"
    if not env.exists():
        write_private(env, ENV_TEMPLATE)
        out.append((env, True))
    else:
        out.append((env, False))
    ovpn = base / "ovpn"
    if not ovpn.exists():
        ovpn.mkdir()
        out.append((ovpn, True))
    else:
        out.append((ovpn, False))
    return out


def write_private(path: Path, content: str) -> None:
    """Write a file readable only by the owner (mode 600 on Linux)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_env_values(path: Path, values: dict[str, str]) -> None:
    """Update KEY=VALUE lines in a .env file, keeping other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else None
        if key in remaining:
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={_quote(value)}")
    write_private(path, "\n".join(out) + "\n")


def _quote(value: str) -> str:
    if value and any(ch in value for ch in " #'\"=") and "'" not in value:
        return f"'{value}'"
    return value
