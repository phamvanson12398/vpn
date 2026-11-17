# HMA Proxy Manager

Biến tài khoản VPN **HMA** thành nhiều proxy (HTTP và/hoặc SOCKS5). Mỗi proxy đi qua một kết nối VPN HMA riêng nên có **IP ra ngoài riêng**. Quản lý qua **giao diện web** trên Windows.

> Nguyên tắc: 1 kết nối VPN = 1 IP = 1 proxy. Số proxy tối đa bằng số thiết bị gói HMA cho phép (Standard: 5, Family: 10).
>
> ⚠️ HMA có thể cấm chia sẻ proxy cho người khác. Chỉ dùng cho nhu cầu cá nhân và đọc điều khoản của HMA trước.

```
[Trình duyệt / tool] ──> 127.0.0.1:8881 ──> container gluetun #1 (OpenVPN HMA Japan)     ──> Internet (IP Nhật)
                    ──> 127.0.0.1:8882 ──> container gluetun #2 (OpenVPN HMA Singapore) ──> Internet (IP Singapore)
                    ──> 127.0.0.1:888N ──> container gluetun #N ...

[HMA Proxy Manager (Python, web UI http://127.0.0.1:8800)] ── điều khiển container qua Docker Desktop
```

Mỗi proxy là một container [gluetun](https://github.com/qdm12/gluetun) chạy file `.ovpn` của HMA, bật HTTP proxy và **kill switch** (VPN rớt thì không có traffic nào đi thẳng ra mạng). SOCKS5 (tùy chọn) là container `gost` dùng chung mạng với container VPN đó. Manager tự kiểm tra IP định kỳ và tự phục hồi khi proxy hỏng.

## Yêu cầu

| Hạng mục | Yêu cầu |
|---|---|
| Hệ điều hành | Windows 10 22H2 / Windows 11 (64-bit) |
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) dùng **WSL 2** (mặc định) |
| Python | 3.10 trở lên, từ [python.org](https://www.python.org/downloads/) (nhớ tick **Add python.exe to PATH**) |
| HMA | Tài khoản còn hạn, có **username/password VPN** và tải được file **OpenVPN (.ovpn)** |

Tại sao cần Docker: Windows không cho chạy nhiều kết nối VPN song song và tách từng ứng dụng ra từng VPN. Docker Desktop chạy mỗi VPN trong một container Linux riêng (trong WSL 2), nhờ vậy mới có nhiều IP cùng lúc.

## Cài đặt và chạy

1. Cài **Docker Desktop**, mở lên, đợi biểu tượng cá voi báo *Engine running*.
2. Cài **Python 3.10+**.
3. Tải mã nguồn này về (Code › Download ZIP rồi giải nén, hoặc `git clone`).
4. Nhấp đúp **`start.bat`**. Lần đầu sẽ tạo môi trường Python và cài thư viện, sau đó mở trình duyệt ở **http://127.0.0.1:8800**.

Trên giao diện web, làm theo khung **Bắt đầu**:

1. Tab **Cài đặt › Tài khoản VPN HMA**: nhập username/password **VPN** (trong tài khoản HMA, phần cấu hình thủ công/OpenVPN; không phải email đăng nhập web). Mật khẩu lưu vào file `.env`, không bao giờ hiển thị lại.
2. **File .ovpn**: tải các file OpenVPN từ HMA, chọn upload (được nhiều file cùng lúc) hoặc chép vào thư mục `ovpn\`.
3. **Thêm proxy**: đặt id (vd `jp`) và chọn server. Cổng tự cấp: 8881, 8882, …
4. Tab **Proxy** › **Tạo proxy**. Lần đầu Docker cần tải image (khoảng 1 phút). Sau 20–60 giây, cột *Sức khỏe* chuyển sang **Khỏe** và hiện IP ra ngoài.

Thử proxy (PowerShell hoặc cmd):

```bash
curl -x http://127.0.0.1:8881 https://api.ipify.org
```

### Tự chạy khi bật máy

* Docker Desktop › Settings › General › bật **Start Docker Desktop when you sign in**.
* Chuột phải **`install-autostart.ps1`** › *Run with PowerShell*. Lệnh này tạo Scheduled Task chạy Manager ẩn khi đăng nhập (chờ 1 phút cho Docker khởi động) và tự chạy lại nếu bị tắt. Giao diện vẫn ở http://127.0.0.1:8800.
* Gỡ: `Unregister-ScheduledTask -TaskName "HMA Proxy Manager" -Confirm:$false`

Container proxy có `restart: unless-stopped`, nên khởi động lại máy thì Docker tự bật lại proxy, kể cả khi Manager chưa chạy.

## Giao diện web

| Tab | Chức năng |
|---|---|
| **Proxy** | Bảng id, quốc gia, cổng, trạng thái container, sức khỏe, IP ra ngoài, thời gian chạy. Bật/tắt/restart/xóa/kiểm tra từng proxy hoặc tất cả |
| **Xuất danh sách** | `http://ip:port`, `socks5://ip:port`, `ip:port`, JSON; sao chép hoặc tải file; lọc proxy khỏe; kèm user:pass |
| **Cài đặt** | Tài khoản VPN, quản lý file .ovpn, thêm proxy, sửa `config.yaml` (có kiểm tra lỗi trước khi áp dụng, tự sao lưu `config.yaml.bak`) |
| **Nhật ký** | Log của Manager và log từng container VPN/SOCKS5 (mật khẩu đã được che) |

## Cấu hình (`config.yaml`)

File được tạo tự động ở lần chạy đầu (mẫu đầy đủ: [`config.example.yaml`](config.example.yaml)). Các mục chính:

```yaml
max_connections: 5          # theo gói HMA (5 hoặc 10); cấu hình vượt số này bị từ chối
bind_address: 127.0.0.1     # 0.0.0.0 = cho máy khác trong mạng LAN dùng (nên bật proxy_auth)
base_port: 8881
health_check: { interval_seconds: 60, failure_threshold: 3, ip_check_url: https://api.ipify.org }
recovery: { max_restarts: 3, backoff_base_seconds: 30, backoff_max_seconds: 900, switch_server: true }
proxy_auth: { enabled: false, user: "", password: "" }
socks5: { enabled: false, base_port: 1081 }
gateway: { enabled: false, http_port: 8880, socks_port: 1080, mode: roundrobin }
web: { bind: 127.0.0.1, port: 8800, password: "" }
proxies:
  - id: jp
    ovpn: Japan.Tokyo.TCP.ovpn
  - id: sg
    ovpn: Singapore.TCP.ovpn
    port: 8890              # tùy chọn: cổng riêng
```

Chọn server ngẫu nhiên, mỗi proxy một quốc gia khác nhau (thay cho `proxies`):

```yaml
selection:
  mode: random
  count: 3
  distinct_countries: true
  countries: []             # vd [Japan, Singapore, USA]; để trống = mọi quốc gia
```

Danh sách server được chọn được lưu lại, các lần sau dùng đúng danh sách đó. Muốn chọn lại thì dùng `hmaproxy create --reshuffle`.

**Lưu ý:** đổi `bind_address`, `socks5`, `proxy_auth` chỉ có tác dụng với proxy tạo mới, nên phải **Xóa** rồi **Tạo** lại proxy. Đổi `web` hoặc `gateway` thì phải khởi động lại chương trình.

### Truy cập từ máy khác trong mạng LAN

1. Đặt `bind_address: 0.0.0.0` và bật `proxy_auth` (user/password).
2. Xóa rồi tạo lại proxy.
3. Mở cổng trong Windows Firewall nếu bị chặn, ví dụ (PowerShell, quyền Admin):
   `New-NetFirewallRule -DisplayName "HMA proxies" -Direction Inbound -Protocol TCP -LocalPort 8881-8890 -Action Allow`
4. Tab **Xuất danh sách** tự điền IP LAN của máy.

Giao diện web vẫn chỉ nghe trên 127.0.0.1. Nếu muốn mở web cho máy khác: `web.bind: 0.0.0.0` **và** `web.password` (đăng nhập user `admin`).

### Cổng xoay vòng (gateway)

`gateway.enabled: true` mở thêm một cổng (mặc định 8880, SOCKS5 1080). Mỗi kết nối mới được chuyển tới một proxy **đang khỏe** khác nhau (lần lượt hoặc ngẫu nhiên). Trình duyệt hoặc tool chỉ cần trỏ vào một cổng duy nhất.

## Tự kiểm tra và tự phục hồi

Mỗi `interval_seconds` (mặc định 60 giây), Manager làm các việc sau:

1. Gọi `ip_check_url` **qua từng proxy** (có URL dự phòng) để lấy IP ra ngoài, rồi so với IP thật của máy và IP của các proxy khác.
2. Container bị dừng ngoài ý muốn: bật lại ngay ở lần kiểm tra kế tiếp (≤ 1 chu kỳ).
3. Proxy lỗi ≥ `failure_threshold` lần liên tiếp:
   * restart container, tối đa `max_restarts` lần; giữa các lần chờ 30s → 60s → 120s … (tối đa `backoff_max_seconds`) để HMA không khóa vì kết nối dồn dập;
   * vẫn hỏng: đổi sang file `.ovpn` khác **cùng quốc gia** (giữ nguyên id và cổng);
   * hết cách: đánh dấu **FAILED**, ghi log ERROR, rồi vẫn thử lại chậm (mỗi `backoff_max_seconds`) để tự lành khi HMA ổn định trở lại.
4. Nếu proxy trả về **IP thật của máy** (rò IP; với kill switch của gluetun thì không nên xảy ra), proxy đó bị **tắt ngay** và đánh dấu FAILED, chờ người dùng xử lý.
5. Proxy vừa khởi động có `startup_grace_seconds` (mặc định 90 giây) để kết nối VPN trước khi bị tính lỗi.

Proxy bạn tự **Tắt** sẽ không bị tự bật lại.

## Bảo mật

* Mật khẩu VPN nằm trong `.env`. Khi tạo container, mật khẩu được ghi ra `.hmaproxy/secrets/` và mount vào container dưới dạng **secret file**, không nằm trong biến môi trường, nên `docker inspect` không thấy.
* Mọi log (file, console, log container hiển thị trên web) đều đi qua bộ lọc che mật khẩu (`***`). Không có API nào trả về mật khẩu VPN.
* Proxy và web mặc định chỉ nghe trên `127.0.0.1`. Nếu mở `0.0.0.0` mà không bật `proxy_auth`, Manager sẽ cảnh báo.
* Web API chặn request từ trang web khác (bắt buộc header riêng và kiểm tra Host, chống CSRF/DNS-rebinding).
* DNS do gluetun phân giải qua VPN (DNS-over-TLS). Proxy HTTP và SOCKS5 (`socks5h`) tự phân giải tên miền, không dùng DNS của máy.
* Mật khẩu `proxy_auth` có trong cấu hình container (env của gluetun, tham số của gost). Đây là mật khẩu proxy của bạn, không phải mật khẩu HMA.

## Dòng lệnh (tùy chọn)

Mọi chức năng đều có thêm bản dòng lệnh (`.venv\Scripts\python -m hmaproxy …`), tiện cho script:

```text
hmaproxy web [--port 8800] [--no-browser]
hmaproxy init | validate
hmaproxy create [id ...] [--wait] [--reshuffle]
hmaproxy start|stop|restart|remove [id|all]
hmaproxy list
hmaproxy check [id|all]
hmaproxy export --format plain|http|socks5|json [--host IP] [--no-auth] [--all] [-o file]
hmaproxy logs <id> [--tail N] [--follow] [--socks]
hmaproxy monitor                 # giám sát + tự phục hồi, không có web
hmaproxy gateway [--type http|socks5]
```

Ngôn ngữ thông báo: `language: vi|en` trong config, `--lang en` hoặc biến `HMAPROXY_LANG`. Mã thoát: `0` thành công, `1` lỗi chung, `2` cấu hình, `3` Docker, `4` thiếu thông tin đăng nhập, `5` không tìm thấy proxy, `6` kiểm tra sức khỏe thất bại.

Chương trình cũng chạy được trên Linux (Docker Engine): dùng `python -m hmaproxy web` và tham khảo [`deploy/hmaproxy.service`](deploy/hmaproxy.service) để chạy bằng systemd. [`examples/docker-compose.yml`](examples/docker-compose.yml) là bản chạy tay 2 proxy, không cần Manager (giai đoạn 1–2 của lộ trình).

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| “Không kết nối được Docker” | Mở Docker Desktop, đợi *Engine running*, bấm làm mới trang |
| Proxy mãi “Đang kết nối” | Tab Nhật ký › VPN `<id>`. `AUTH_FAILED`: sai username/password VPN (phải là tài khoản VPN, không phải email). `TLS handshake failed`/timeout: thử file `.ovpn` TCP khác |
| Container bị dừng ngay | Xem log container. Kiểm tra Docker Desktop đang dùng WSL 2 (Settings › General) |
| Cổng đã bị dùng | Đổi `base_port` hoặc `port` của proxy |
| Hai proxy cùng IP | HMA đôi khi đưa hai server về cùng một IP ra; chọn server khác |
| Một số website chặn | Nhiều trang chặn dải IP VPN; đây là giới hạn của VPN, không phải lỗi của tool |

## Phát triển

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Test dùng Docker giả lập nên chạy được trên mọi máy, kể cả máy không có Docker. Cấu trúc mã:

```
hmaproxy/
  web.py, static/index.html   giao diện web + JSON API
  cli.py                      dòng lệnh
  service.py                  thao tác chung (create/start/stop/list/check/export)
  config.py                   đọc và kiểm tra config.yaml, .env
  providers/                  lớp nhà cung cấp VPN (hma.py); thêm hãng khác = thêm 1 file
  docker_ctl.py               tạo và điều khiển container (Docker SDK)
  health.py                   kiểm tra IP qua proxy
  monitor.py, recover.py      giám sát, tự phục hồi, backoff
  gateway.py                  cổng xoay vòng
  export.py, state.py, logging_setup.py, i18n.py
```
