"""Tiny message catalogue so CLI output can be Vietnamese (default) or English.

Language is chosen (highest priority first) by ``--lang``, the ``HMAPROXY_LANG``
environment variable, then ``language:`` in config.yaml.
"""

from __future__ import annotations

import os

MESSAGES: dict[str, dict[str, str]] = {
    # --- config ---
    "config_not_found": {
        "vi": "Không tìm thấy file cấu hình: {path}",
        "en": "Config file not found: {path}",
    },
    "config_invalid_yaml": {
        "vi": "File cấu hình không phải YAML hợp lệ: {error}",
        "en": "Config file is not valid YAML: {error}",
    },
    "config_field_invalid": {
        "vi": "Giá trị cấu hình không hợp lệ ở '{field}': {reason}",
        "en": "Invalid config value at '{field}': {reason}",
    },
    "ovpn_dir_missing": {
        "vi": "Thư mục ovpn không tồn tại: {path}",
        "en": "ovpn directory does not exist: {path}",
    },
    "ovpn_dir_empty": {
        "vi": "Chưa có file .ovpn nào (thư mục {path}). Tải file OpenVPN từ tài khoản VPN của bạn rồi thêm ở tab Cài đặt › File .ovpn.",
        "en": "No .ovpn files found in {path}. Download the OpenVPN files from your VPN account and add them in Settings › .ovpn files.",
    },
    "ovpn_missing": {
        "vi": "Proxy '{id}': không tìm thấy file .ovpn '{ovpn}' trong {dir}",
        "en": "Proxy '{id}': .ovpn file '{ovpn}' not found in {dir}",
    },
    "too_many_proxies": {
        "vi": "Cấu hình có {count} proxy nhưng max_connections = {max}. Gói VPN của bạn chỉ cho phép {max} kết nối cùng lúc; hãy bớt proxy hoặc tăng max_connections nếu gói cho phép.",
        "en": "Config defines {count} proxies but max_connections = {max}. Your VPN plan only allows {max} simultaneous connections; remove proxies or raise max_connections if your plan allows it.",
    },
    "duplicate_id": {
        "vi": "Trùng id proxy: '{id}'",
        "en": "Duplicate proxy id: '{id}'",
    },
    "bad_id": {
        "vi": "id proxy '{id}' không hợp lệ (chỉ dùng a-z, 0-9, '-', '_', tối đa 32 ký tự)",
        "en": "Invalid proxy id '{id}' (use a-z, 0-9, '-', '_', max 32 chars)",
    },
    "duplicate_port": {
        "vi": "Trùng cổng {port} ({a} và {b})",
        "en": "Port {port} is used twice ({a} and {b})",
    },
    "port_range": {
        "vi": "Cổng {port} của '{id}' nằm ngoài khoảng 1-65535",
        "en": "Port {port} of '{id}' is outside 1-65535",
    },
    "no_proxies": {
        "vi": "Chưa có proxy nào trong cấu hình. Thêm ở tab Cài đặt › Thêm proxy (hoặc mục 'proxies' trong config.yaml).",
        "en": "No proxies configured yet. Add some in Settings › Add proxy (or 'proxies' in config.yaml).",
    },
    "not_enough_servers": {
        "vi": "Chỉ có {available} server phù hợp nhưng cần {count}",
        "en": "Only {available} matching servers available but {count} requested",
    },
    "unknown_provider": {
        "vi": "Nhà cung cấp VPN không được hỗ trợ: '{name}'. Hỗ trợ: {supported}",
        "en": "Unsupported VPN provider: '{name}'. Supported: {supported}",
    },
    "auth_incomplete": {
        "vi": "proxy_auth.enabled = true nhưng thiếu user hoặc password",
        "en": "proxy_auth.enabled is true but user or password is empty",
    },
    "public_bind_no_auth": {
        "vi": "Proxy đang mở trên {bind} mà KHÔNG có mật khẩu. Bất kỳ ai truy cập được máy này đều dùng được proxy. Nên bật proxy_auth.",
        "en": "Proxies listen on {bind} WITHOUT a password. Anyone who can reach this host can use them. Enable proxy_auth.",
    },
    "env_perms": {
        "vi": "File {path} đang có quyền {mode}; nên đặt 600: chmod 600 {path}",
        "en": "File {path} has mode {mode}; it should be 600: chmod 600 {path}",
    },
    "missing_credentials": {
        "vi": "Thiếu thông tin đăng nhập VPN: cần biến {names} (trong môi trường hoặc file .env)",
        "en": "Missing VPN credentials: set {names} (in the environment or the .env file)",
    },
    "config_ok": {
        "vi": "Cấu hình hợp lệ: {count} proxy, max_connections = {max}",
        "en": "Config is valid: {count} proxies, max_connections = {max}",
    },
    # --- openvpn / system ---
    "openvpn_missing": {
        "vi": "Chưa cài OpenVPN. Tải OpenVPN Community (bản Windows 64-bit, miễn phí) tại https://openvpn.net/community-downloads/ rồi cài với các tùy chọn mặc định (có TAP-Windows6).",
        "en": "OpenVPN is not installed. Get OpenVPN Community for Windows (64-bit, free) from https://openvpn.net/community-downloads/ and install it with default options (including TAP-Windows6).",
    },
    "need_admin": {
        "vi": "Cần chạy chương trình bằng quyền Administrator (để tạo card mạng ảo và route cho VPN). Hãy mở bằng start.bat, chọn Yes khi Windows hỏi.",
        "en": "Administrator rights are required (to create virtual adapters and VPN routes). Start via start.bat and accept the UAC prompt.",
    },
    "tapctl_missing": {
        "vi": "Không tìm thấy tapctl.exe trong thư mục OpenVPN; hãy cài lại OpenVPN Community",
        "en": "tapctl.exe not found next to openvpn.exe; reinstall OpenVPN Community",
    },
    "tapctl_failed": {
        "vi": "Không tạo được card mạng ảo: {error}. Kiểm tra driver (openvpn.driver trong config) đã được cài cùng OpenVPN.",
        "en": "Could not create the virtual adapter: {error}. Check that the driver (openvpn.driver) was installed with OpenVPN.",
    },
    "vpn_start_failed": {"vi": "Không chạy được openvpn.exe: {error}", "en": "Could not start openvpn.exe: {error}"},
    "vpn_auth_failed": {
        "vi": "Nhà cung cấp VPN từ chối đăng nhập (AUTH_FAILED): kiểm tra username/password VPN (loại dành cho cấu hình thủ công/OpenVPN), hoặc đã vượt số thiết bị cho phép",
        "en": "The VPN provider rejected the login (AUTH_FAILED): check the VPN username/password, or the device limit was exceeded",
    },
    "vpn_exited": {"vi": "openvpn.exe đã thoát (mã {code})", "en": "openvpn.exe exited (code {code})"},
    "vpn_connected": {"vi": "VPN đã kết nối: IP tunnel {ip}, server {server}", "en": "VPN connected: tunnel IP {ip}, server {server}"},
    "vpn_reconnecting": {"vi": "VPN đang kết nối lại ({reason})", "en": "VPN reconnecting ({reason})"},
    "tunnel_ip_duplicate": {
        "vi": "Tunnel IP {ip} trùng với proxy {other}; kết nối lại để nhận IP khác",
        "en": "Tunnel IP {ip} clashes with proxy {other}; reconnecting to get another one",
    },
    "port_in_use": {
        "vi": "Cổng {port} đang bị chương trình khác dùng ({error}). Đổi base_port hoặc port của proxy.",
        "en": "Port {port} is already in use ({error}). Change base_port or the proxy's port.",
    },
    "removed": {"vi": "Đã xóa proxy '{id}'", "en": "Removed proxy '{id}'"},
    "container_exists": {
        "vi": "Proxy '{id}' đã được tạo trước đó; bỏ qua.",
        "en": "Proxy '{id}' already exists; skipping.",
    },
    "created": {
        "vi": "Đã tạo proxy '{id}' ({country}, {ovpn}) -> {bind}:{port}",
        "en": "Created proxy '{id}' ({country}, {ovpn}) -> {bind}:{port}",
    },
    "web_not_running": {
        "vi": "Chương trình chưa chạy. Mở start.bat trước (lệnh này điều khiển chương trình đang chạy qua {url}).",
        "en": "The program is not running. Start it with start.bat first (this command talks to it at {url}).",
    },
    "proxy_not_found": {
        "vi": "Không có proxy nào tên '{id}'",
        "en": "No proxy named '{id}'",
    },
    "no_managed": {
        "vi": "Chưa có proxy nào được tạo. Chạy: hmaproxy create",
        "en": "No proxies have been created yet. Run: hmaproxy create",
    },
    "action_done": {"vi": "{action}: {id} OK", "en": "{action}: {id} OK"},
    "action_failed": {"vi": "{action}: {id} lỗi: {error}", "en": "{action}: {id} failed: {error}"},
    "over_limit_running": {
        "vi": "Đã có {existing} proxy, tạo thêm {new} sẽ vượt max_connections = {max}",
        "en": "{existing} proxies already exist; adding {new} would exceed max_connections = {max}",
    },
    "waiting_ready": {
        "vi": "Đang chờ các proxy kết nối VPN (tối đa {timeout}s) ...",
        "en": "Waiting for proxies to connect (up to {timeout}s) ...",
    },
    "ready": {"vi": "Proxy '{id}' sẵn sàng, IP ra ngoài: {ip}", "en": "Proxy '{id}' is ready, exit IP: {ip}"},
    "not_ready": {
        "vi": "Proxy '{id}' chưa sẵn sàng sau {timeout}s: {error}",
        "en": "Proxy '{id}' not ready after {timeout}s: {error}",
    },
    # --- health ---
    "real_ip": {"vi": "IP thật của máy: {ip}", "en": "Real IP of this host: {ip}"},
    "real_ip_unknown": {
        "vi": "Không lấy được IP thật của máy ({error}); bỏ qua kiểm tra rò IP",
        "en": "Could not get this host's real IP ({error}); leak comparison skipped",
    },
    "check_ok": {"vi": "{id}: OK, IP {ip} ({ms} ms)", "en": "{id}: OK, IP {ip} ({ms} ms)"},
    "check_fail": {"vi": "{id}: LỖI - {error}", "en": "{id}: FAILED - {error}"},
    "ip_leak": {
        "vi": "RÒ IP: proxy trả về IP thật của máy ({ip})",
        "en": "IP LEAK: proxy returned this host's real IP ({ip})",
    },
    "ip_duplicate": {
        "vi": "{id}: IP {ip} trùng với proxy {other}",
        "en": "{id}: IP {ip} is the same as proxy {other}",
    },
    "socks_fail": {"vi": "SOCKS5 lỗi: {error}", "en": "SOCKS5 failed: {error}"},
    # --- recovery / monitor ---
    "container_down": {
        "vi": "VPN không chạy (trạng thái {status}); khởi động lại",
        "en": "VPN is not running (status {status}); restarting",
    },
    "unhealthy": {
        "vi": "Hỏng sau {count} lần kiểm tra thất bại liên tiếp",
        "en": "Unhealthy after {count} consecutive failed checks",
    },
    "recover_restart": {
        "vi": "Tự phục hồi: restart container (lần {attempt}/{max})",
        "en": "Recovery: restarting container (attempt {attempt}/{max})",
    },
    "recover_switch": {
        "vi": "Tự phục hồi: đổi server {old} -> {new}",
        "en": "Recovery: switching server {old} -> {new}",
    },
    "recover_failed": {
        "vi": "Tự phục hồi thất bại, đánh dấu FAILED: {reason}",
        "en": "Recovery gave up, marked FAILED: {reason}",
    },
    "recover_backoff": {
        "vi": "Chờ backoff thêm {seconds}s trước lần thử tiếp theo",
        "en": "Backing off {seconds}s before the next attempt",
    },
    "recovered": {"vi": "Đã phục hồi, IP {ip}", "en": "Recovered, IP {ip}"},
    "socks_restart": {
        "vi": "HTTP proxy chạy nhưng SOCKS5 lỗi; mở lại cổng SOCKS5",
        "en": "HTTP proxy works but SOCKS5 failed; reopening the SOCKS5 listener",
    },
    "monitor_start": {
        "vi": "Bắt đầu giám sát {count} proxy, chu kỳ {interval}s",
        "en": "Monitoring {count} proxies every {interval}s",
    },
    "monitor_stop": {"vi": "Dừng giám sát", "en": "Monitor stopped"},
    # --- export / gateway ---
    "export_empty": {
        "vi": "Không có proxy nào để xuất",
        "en": "Nothing to export",
    },
    "socks_disabled": {
        "vi": "SOCKS5 chưa bật (socks5.enabled = false trong config)",
        "en": "SOCKS5 is not enabled (socks5.enabled = false in config)",
    },
    "gateway_listening": {
        "vi": "Gateway xoay vòng ({mode}) đang nghe tại {bind}:{port}, {count} proxy phía sau",
        "en": "Rotating gateway ({mode}) listening on {bind}:{port} with {count} upstream proxies",
    },
    "gateway_no_backends": {
        "vi": "Gateway: không có proxy khỏe nào để chuyển tiếp",
        "en": "Gateway: no healthy proxy to forward to",
    },
    # --- init ---
    "init_created": {"vi": "Đã tạo {path}", "en": "Created {path}"},
    "init_exists": {"vi": "Đã có {path}, giữ nguyên", "en": "{path} already exists, left unchanged"},
    "interrupted": {"vi": "Đã hủy", "en": "Interrupted"},
}

SUPPORTED = ("vi", "en")
_lang = os.environ.get("HMAPROXY_LANG", "vi")


def set_language(lang: str | None) -> None:
    global _lang
    if lang and lang.lower() in SUPPORTED:
        _lang = lang.lower()


def get_language() -> str:
    return _lang


def t(key: str, **kwargs) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        template = key
    else:
        template = entry.get(_lang) or entry["en"]
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
