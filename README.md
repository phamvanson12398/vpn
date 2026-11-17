# HMA Proxy Manager (Windows)

Biến tài khoản VPN **HMA** thành nhiều proxy HTTP/SOCKS5 chạy **trực tiếp trên máy Windows của bạn**, không cần Docker, không cần máy chủ. Mỗi proxy đi qua một kết nối VPN HMA riêng nên có **IP ra ngoài riêng**. Mọi thao tác làm trên **giao diện web**.

> Nguyên tắc: 1 kết nối VPN = 1 IP = 1 proxy. Số proxy tối đa bằng số thiết bị gói HMA cho phép (Standard: 5, Family: 10).
>
> ⚠️ HMA có thể cấm chia sẻ proxy cho người khác. Chỉ dùng cho nhu cầu cá nhân và đọc điều khoản của HMA trước.

## Cách hoạt động

```
Trình duyệt / tool
   │  127.0.0.1:8881            127.0.0.1:8882            127.0.0.1:888N
   ▼                           ▼                         ▼
┌──────────────── HMA Proxy Manager (Python, giao diện http://127.0.0.1:8800) ────────────────┐
│ proxy jp ──bind 10.x.x.a──┐  proxy sg ──bind 10.x.x.b──┐ ...  DNS cũng đi qua từng tunnel   │
└───────────────────────────┼────────────────────────────┼─────────────────────────────────────┘
                 openvpn.exe #1 (HMA Japan)   openvpn.exe #2 (HMA Singapore)    ← mỗi cái 1 card mạng ảo
                            ▼                            ▼
                    Internet (IP Nhật)          Internet (IP Singapore)

Trình duyệt / ứng dụng bình thường của bạn ──> mạng thật của máy (không bị ảnh hưởng)
```

* Mỗi proxy có một tiến trình **OpenVPN** riêng, dùng file `.ovpn` của HMA và một **card mạng ảo** riêng (`HMA-Proxy-<id>`, tạo tự động).
* Chương trình chỉnh file `.ovpn` để VPN **không chiếm đường mạng mặc định** của máy (bỏ `redirect-gateway`, không đổi DNS, không bật `block-outside-dns`). Nhờ vậy Internet của máy vẫn chạy bình thường và nhiều VPN chạy song song được.
* Proxy HTTP/SOCKS5 được tích hợp sẵn trong chương trình. Mọi kết nối của proxy được **gắn vào IP của tunnel VPN** tương ứng, nên Windows đưa nó đi qua đúng VPN đó. Tên miền cũng được phân giải qua tunnel (DNS 1.1.1.1/8.8.8.8 qua VPN), không dùng DNS của máy.
* **Kill switch:** khi VPN rớt, proxy trả lỗi 503 (SOCKS5: *network unreachable*) chứ **không bao giờ** đi đường mạng thật. Chương trình còn kiểm tra định kỳ: nếu phát hiện proxy ra bằng IP thật thì tắt proxy đó ngay.
* Mật khẩu HMA **không ghi ra đĩa** cho OpenVPN. Chương trình đưa mật khẩu qua cổng quản lý nội bộ của OpenVPN, ngay trong bộ nhớ.

## Yêu cầu

| Hạng mục | Yêu cầu |
|---|---|
| Windows | 10 / 11, 64-bit |
| OpenVPN | [OpenVPN Community](https://openvpn.net/community-downloads/) bản Windows 64-bit (miễn phí). Cài với tùy chọn mặc định, **giữ TAP-Windows6** |
| Python | 3.10 trở lên, từ [python.org](https://www.python.org/downloads/) (tick **Add python.exe to PATH**) |
| Quyền | **Administrator**, để tạo card mạng ảo và route VPN. `start.bat` tự hỏi quyền |
| HMA | Tài khoản còn hạn, có **username/password VPN** và tải được file **OpenVPN (.ovpn)** |

**Tắt ứng dụng HMA chính thức** khi dùng chương trình này: nó cũng chiếm một suất kết nối và có thể đổi đường mạng của máy.

## Dùng với ExpressVPN

Tool hỗ trợ cả **HMA** và **ExpressVPN**. Chọn nhà cung cấp ở tab **Cài đặt › Tài khoản VPN**, hoặc đặt `provider: expressvpn` trong `config.yaml`.

Cách lấy file cấu hình ExpressVPN:

1. Đăng nhập **expressvpn.com** › **Set up other devices** › **Manual Configuration** › tab **OpenVPN**.
2. Chép **Username** và **Password** hiện ở đó vào tab **Cài đặt › Tài khoản VPN** của tool. Đây là thông tin riêng cho OpenVPN, **khác** email/mật khẩu tài khoản.
3. Ngay bên dưới là danh sách địa điểm: tải các file `.ovpn` cần dùng (vd `my_expressvpn_japan_-_tokyo_udp.ovpn`), rồi upload ở **Cài đặt › File .ovpn**.
4. Đặt `max_connections` bằng số thiết bị gói ExpressVPN của bạn cho phép (xem trong tài khoản).

File của ExpressVPN dùng một số tùy chọn cũ (`keysize`, `ns-cert-type`, mã hóa AES-256-CBC) mà OpenVPN 2.6 không còn nhận. Tool tự sửa các dòng đó khi chạy, bạn không cần sửa file.

## Cài đặt và chạy

1. Cài **OpenVPN Community** và **Python**.
2. Tải mã nguồn này về (Code › Download ZIP rồi giải nén, hoặc `git clone`).
3. Nhấp đúp **`start.bat`** và chọn **Yes** khi Windows hỏi quyền Administrator. File này tự kiểm tra Python và OpenVPN, đề nghị cài tự động bằng `winget` nếu thiếu, cài thư viện Python ở lần đầu, rồi mở trình duyệt ở **http://127.0.0.1:8800**.
4. Làm theo khung **Bắt đầu** trên trang:
   1. Tab **Cài đặt › Tài khoản VPN HMA**: nhập username/password **VPN** (trong tài khoản HMA, phần cấu hình thủ công/OpenVPN; không phải email đăng nhập web).
   2. **File .ovpn**: tải các file OpenVPN từ HMA rồi upload (chọn được nhiều file), hoặc chép vào thư mục `ovpn\`.
   3. **Thêm proxy**: đặt id (vd `jp`) và chọn server. Cổng tự cấp: 8881, 8882, …
   4. Tab **Proxy** › **Tạo proxy**. Sau 10–30 giây, cột *VPN* báo **đã kết nối**. Lần kiểm tra kế tiếp (hoặc khi bấm *Kiểm tra IP*) sẽ hiện IP ra ngoài.

Thử proxy trong PowerShell hoặc cmd:

```bash
curl -x http://127.0.0.1:8881 https://api.ipify.org
```

> **Cửa sổ đen của `start.bat` chính là chương trình.** Đóng cửa sổ này sẽ ngắt mọi VPN và proxy. Lần chạy sau, các proxy đang bật sẽ tự kết nối lại.

### Chạy như server: bật máy là tự chạy

1. Chạy `start.bat` một lần để cài Python/thư viện và cấu hình xong (tài khoản, file .ovpn, proxy). Sau đó đóng cửa sổ của nó.
2. Nhấp đúp **`cai-tu-chay-khi-bat-may.bat`** và chọn Yes khi Windows hỏi quyền.

Từ đó tool chạy **ngầm**, không có cửa sổ, bằng tài khoản SYSTEM. Mỗi lần bật máy nó **tự chạy, không cần đăng nhập Windows**, và tự chạy lại nếu bị tắt. Các proxy đang bật sẽ tự kết nối lại.

* Mở giao diện: lối tắt **HMA Proxy Manager** trên Desktop, hoặc http://127.0.0.1:8800. Nhấp `start.bat` lúc này cũng chỉ mở giao diện.
* Nhật ký: `.hmaproxy\logs\hmaproxy.log`, hoặc tab Nhật ký trên giao diện.
* Gỡ (dừng hẳn và thôi tự chạy): nhấp đúp **`go-tu-chay.bat`**.

## Giao diện web

| Tab | Chức năng |
|---|---|
| **Proxy** | Bảng id, quốc gia/server, cổng, trạng thái VPN (+ IP tunnel), sức khỏe, IP ra ngoài, thời gian chạy. Bật/tắt/restart/xóa/kiểm tra từng proxy hoặc tất cả |
| **Xuất danh sách** | `http://ip:port`, `socks5://ip:port`, `ip:port`, JSON; sao chép hoặc tải file; lọc proxy khỏe; kèm user:pass |
| **Cài đặt** | Tài khoản VPN, quản lý file .ovpn, thêm proxy, sửa `config.yaml` (kiểm tra lỗi trước khi áp dụng, tự sao lưu `config.yaml.bak`; tải lại cấu hình không làm rớt các VPN đang chạy) |
| **Nhật ký** | Log của chương trình và log OpenVPN của từng proxy (mật khẩu đã được che) |

## Cấu hình (`config.yaml`)

File được tạo tự động ở lần chạy đầu (mẫu đầy đủ: [`config.example.yaml`](config.example.yaml)):

```yaml
max_connections: 5          # theo gói HMA (5 hoặc 10); cấu hình vượt số này bị từ chối
bind_address: 127.0.0.1     # 0.0.0.0 = cho máy khác trong mạng LAN dùng (nên bật proxy_auth)
base_port: 8881
health_check: { interval_seconds: 60, failure_threshold: 3, ip_check_url: https://api.ipify.org }
recovery: { max_restarts: 3, backoff_base_seconds: 30, backoff_max_seconds: 900, switch_server: true }
proxy_auth: { enabled: false, user: "", password: "" }
socks5: { enabled: false, base_port: 1081 }
gateway: { enabled: false, http_port: 8880, socks_port: 1080, mode: roundrobin }
openvpn: { path: "", driver: tap-windows6, adapter_prefix: HMA-Proxy }
dns: { servers: [1.1.1.1, 8.8.8.8, 9.9.9.9] }
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

* `openvpn.driver`: mặc định `tap-windows6` vì tương thích mọi kiểu mã hóa của file HMA. `ovpn-dco` nhanh hơn nhưng chỉ chạy với cấu hình dùng AES-GCM/ChaCha20.
* Đổi `proxy_auth` hoặc `bind_address` xong, bấm **Restart** proxy để áp dụng. Đổi cổng/server hoặc bật SOCKS5 cho proxy đã tạo: **Xóa** rồi **Tạo** lại. Đổi `web` hoặc `gateway` phải khởi động lại chương trình.

### Cho máy khác trong mạng LAN dùng proxy

1. Đặt `bind_address: 0.0.0.0` và bật `proxy_auth` (user/password), rồi Restart các proxy.
2. Mở cổng trong Windows Firewall (PowerShell, quyền Admin):
   `New-NetFirewallRule -DisplayName "HMA proxies" -Direction Inbound -Protocol TCP -LocalPort 8881-8890 -Action Allow`
3. Tab **Xuất danh sách** tự điền IP LAN của máy.

Giao diện web vẫn chỉ nghe trên 127.0.0.1. Nếu muốn mở web cho máy khác: `web.bind: 0.0.0.0` **và** `web.password` (đăng nhập user `admin`).

### Cổng xoay vòng (gateway)

`gateway.enabled: true` mở thêm một cổng (mặc định 8880, SOCKS5 1080). Mỗi kết nối mới được chuyển tới một proxy **đang khỏe** khác nhau (lần lượt hoặc ngẫu nhiên).

## Tự kiểm tra và tự phục hồi

Mỗi `interval_seconds` (mặc định 60 giây), chương trình làm các việc sau:

1. Gọi `ip_check_url` **qua từng proxy** để lấy IP ra ngoài, rồi so với IP thật của máy và IP của các proxy khác.
2. `openvpn.exe` bị thoát (rớt mạng, HMA ngắt…): kết nối lại ngay ở lần kiểm tra kế tiếp.
3. Proxy lỗi ≥ `failure_threshold` lần liên tiếp:
   * kết nối lại, tối đa `max_restarts` lần; giữa các lần chờ 30s → 60s → 120s … (tối đa `backoff_max_seconds`) để HMA không khóa vì kết nối dồn dập;
   * vẫn hỏng: đổi sang file `.ovpn` khác **cùng quốc gia** (giữ nguyên id và cổng);
   * hết cách: đánh dấu **FAILED**, ghi log ERROR, rồi vẫn thử lại chậm (mỗi `backoff_max_seconds`).
4. Proxy trả về **IP thật của máy**: proxy đó bị **tắt ngay** và đánh dấu FAILED.
5. Hai server HMA cấp trùng IP tunnel: tunnel kết nối sau sẽ được kết nối lại để nhận IP khác.

Proxy bạn tự **Tắt** sẽ không bị tự bật lại. Thoát chương trình sẽ ngắt mọi VPN. Nếu chương trình bị kill hoặc crash, Windows cũng tự đóng các `openvpn.exe` con, không để lại VPN mồ côi.

## Bảo mật

* Mật khẩu VPN chỉ nằm trong `.env`. OpenVPN nhận mật khẩu qua cổng quản lý nội bộ (127.0.0.1, có token ngẫu nhiên), không qua file.
* Mọi log (file, cửa sổ, log OpenVPN hiển thị trên web) đều che mật khẩu thành `***`. Không có API nào trả về mật khẩu VPN.
* Proxy và web mặc định chỉ nghe trên `127.0.0.1`. Nếu mở `0.0.0.0` mà không bật `proxy_auth`, chương trình sẽ cảnh báo.
* Proxy từ chối kết nối tới chính máy này (127.x, link-local), để không bị dùng làm cầu vào máy.
* Web API chặn request từ trang web khác (bắt buộc header riêng và kiểm tra Host).
* Chỉ hỗ trợ IPv4 qua tunnel: đích IPv6 bị từ chối thay vì đi đường mạng thật.

## Dòng lệnh (tùy chọn)

Khi chương trình đang chạy, có thể điều khiển nó bằng lệnh trong thư mục dự án:

```text
.venv\Scripts\python -m hmaproxy list
.venv\Scripts\python -m hmaproxy create [id ...] [--reshuffle]
.venv\Scripts\python -m hmaproxy start|stop|restart|remove [id|all]
.venv\Scripts\python -m hmaproxy check [id|all]
.venv\Scripts\python -m hmaproxy export --format plain|http|socks5|json [--host IP] [--no-auth] [--all] [-o file]
.venv\Scripts\python -m hmaproxy logs <id|manager> [--tail N]
.venv\Scripts\python -m hmaproxy validate | init
```

Mã thoát: `0` thành công, `1` lỗi, `2` cấu hình, `3` hệ thống (thiếu OpenVPN/quyền Admin), `4` thiếu thông tin đăng nhập, `5` không tìm thấy proxy, `6` kiểm tra sức khỏe thất bại, `7` chương trình chưa chạy. Ngôn ngữ: `language: vi|en` trong config hoặc `--lang en`.

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| “Chưa cài OpenVPN” | Cài OpenVPN Community; nếu cài ở thư mục khác, điền `openvpn.path` |
| “Cần chạy bằng quyền Administrator” | Mở bằng `start.bat` và chọn Yes |
| “Không tạo được card mạng ảo” | Cài lại OpenVPN và giữ tùy chọn **TAP-Windows6**; hoặc đổi `openvpn.driver` |
| VPN báo *AUTH_FAILED* | Sai username/password VPN (phải là tài khoản VPN, không phải email), hoặc đã vượt số thiết bị (tắt ứng dụng HMA, thiết bị khác) |
| VPN cứ *đang kết nối* | Tab Nhật ký › OpenVPN `<id>`. Thử file `.ovpn` TCP khác; kiểm tra tường lửa/antivirus có chặn `openvpn.exe` không |
| VPN đã kết nối nhưng proxy lỗi | Bấm *Kiểm tra IP*, xem cột lỗi. Thử đổi `dns.servers`, hoặc tắt phần mềm VPN/firewall khác đang can thiệp route |
| Cổng đã bị dùng | Đổi `base_port` hoặc `port` của proxy |
| Một số website chặn | Nhiều trang chặn dải IP VPN; đây là giới hạn của VPN, không phải lỗi của tool |

## Phát triển

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Test chạy được cả trên Linux/macOS. Phần OpenVPN được thử với một tiến trình giả lập nói đúng giao thức quản lý của OpenVPN. Proxy HTTP/SOCKS5 được thử đầu-cuối thật, kể cả 60 kết nối đồng thời.

```
hmaproxy/
  web.py, static/index.html   giao diện web + JSON API (tiến trình chính)
  vpn_ctl.py                  quản lý các tunnel: tạo/bật/tắt/xóa, khôi phục khi khởi động
  openvpn.py                  một openvpn.exe: chỉnh .ovpn (split tunnel), giao thức quản lý
  proxy_server.py             proxy HTTP + SOCKS5 gắn vào IP tunnel, kill switch
  dns_resolve.py              phân giải DNS qua tunnel
  winutil.py                  quyền Admin, tìm OpenVPN, card mạng ảo (tapctl), job object
  service.py, monitor.py, recover.py, health.py   thao tác chung, giám sát, tự phục hồi
  config.py, providers/       cấu hình; lớp nhà cung cấp VPN (thêm hãng khác = thêm 1 file)
  gateway.py, export.py, cli.py, state.py, logging_setup.py, i18n.py
```
