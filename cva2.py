"""
CVa 2 v2 — Python Controller for RP2040DongleV2
Capture Engine: mss (Siêu nhẹ, không dùng BitBlt)
Scanning Algorithm: Chuẩn 100% 3 Tầng Ưu Tiên Của Dự Án Cũ CVa
  - Tầng 1: Tâm ngắm 3x3 px (Ưu tiên số 1)
  - Tầng 2: Cột đầu dọc phía trên 24px (Upper Vertical Comb)
  - Tầng 3: 16 tia Starburst tỏa tròn 360 độ (bảng tra Sin/Cos)
Color Math: Đại số số nguyên không phép chia (Zero-Division Integer) nguyên bản CVa
Zero-Jitter: Không flush buffer, không log nghẽn console, chuột lướt mượt 1000Hz tuyệt đối
"""

import sys
import time
import math
import ctypes
from ctypes import wintypes
import serial
import serial.tools.list_ports
import winsound
import mss

# Khắc phục Windows DPI Scaling
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# =========================================================================
# Protocol Constants (Khớp 100% với rp2040donglev2.ino)
# =========================================================================
PROTO_MAGIC = 0xAA
PROTO_KEY   = 0x5C
OP_CLICK    = 0x01
OP_ACCEL_ON = 0x04
OP_ACCEL_OFF= 0x05

CLICK_PERIOD_SEC = 0.060  # Hồi 60ms giữa 2 lần click
SCAN_RADIUS = 25
RECT_SIZE = SCAN_RADIUS * 2  # 50x50 pixel

# Win32 Virtual Key Codes
VK_RBUTTON = 0x02
VK_A       = 0x41
VK_D       = 0x44
VK_OEM_3   = 0xC0  # `~ Backtick
VK_END     = 0x23
VK_PAGEUP  = 0x21
VK_PAGEDOWN= 0x22

user32 = ctypes.windll.user32

# =========================================================================
# Bảng Tra 16 Tia Starburst (Chuẩn 100% Dự Án Cũ CVa)
# =========================================================================
STAR_BURST_RAYS = [
    ( 0.000, -1.000),
    ( 0.383, -0.924),
    ( 0.707, -0.707),
    ( 0.924, -0.383),
    ( 1.000,  0.000),
    ( 0.924,  0.383),
    ( 0.707,  0.707),
    ( 0.383,  0.924),
    ( 0.000,  1.000),
    (-0.383,  0.924),
    (-0.707,  0.707),
    (-0.924,  0.383),
    (-1.000,  0.000),
    (-0.924, -0.383),
    (-0.707, -0.707),
    (-0.383, -0.924),
]

class RayOffsets:
    def __init__(self, radius=SCAN_RADIUS):
        self.offsets = []
        for i in range(16):
            ray_list = []
            dx_f, dy_f = STAR_BURST_RAYS[i]
            for r in range(radius):
                rx = int(round(r * dx_f))
                ry = int(round(r * dy_f))
                ray_list.append((rx, ry))
            self.offsets.append(ray_list)

# =========================================================================
# Thuật Toán Nhận Diện Màu Sắc Thuần Số Nguyên (Chuẩn 100% CVa Cũ)
# =========================================================================
def is_yellow(r, g, b):
    if g < 120 or r < 110 or b > 125:
        return False
    gb = g - b
    rb = r - b
    if gb < 25 or rb < 15:
        return False
    if (g * 100) < (r * 82) or (r * 100) < (g * 70):
        return False
    if g >= r:
        return (6 * rb <= 7 * gb) and (5 * rb >= 4 * gb)
    else:
        return (6 * gb >= 5 * rb) and (5 * gb <= 6 * rb)

def is_purple(r, g, b):
    if r < 75 or b < 75:
        return False
    rg = r - g
    bg = b - g
    if rg < 20 or bg < 12:
        return False
    if r >= b:
        return (25 * rg <= 60 * bg) and (60 * bg <= 90 * rg)
    else:
        return (30 * bg <= 60 * rg) and (60 * bg <= 95 * bg)

def is_red(r, g, b):
    if r < 130 or g > 110 or b > 110:
        return False
    return (r - g >= 40) and (r - b >= 40)

def is_target_pixel(raw_bytes, idx, color_mode):
    # mss trả về buffer BGRA 4 kênh
    b = raw_bytes[idx]
    g = raw_bytes[idx + 1]
    r = raw_bytes[idx + 2]
    if color_mode == 0:
        return is_yellow(r, g, b)
    elif color_mode == 1:
        return is_purple(r, g, b)
    elif color_mode == 2:
        return is_red(r, g, b)
    return False

# =========================================================================
# Cỗ Máy Quét 3 Tầng Ưu Tiên Chuẩn CVa (Dùng mss, Không Dùng BitBlt)
# =========================================================================
class CVa3TierScanner:
    def __init__(self, rect_size=RECT_SIZE):
        self.rect_size = rect_size
        self.sct = mss.MSS()
        self.ray_offsets = RayOffsets(SCAN_RADIUS)

    def get_grab_region(self, center_x, center_y):
        half = self.rect_size // 2
        return {
            "top": int(center_y - half),
            "left": int(center_x - half),
            "width": int(self.rect_size),
            "height": int(self.rect_size)
        }

    def scan_for_color(self, region, color_mode):
        # Chụp vùng FOV 50x50 bằng mss siêu nhẹ
        shot = self.sct.grab(region)
        raw_bytes = shot.raw
        rect_size = self.rect_size

        cx = rect_size // 2
        cy = rect_size // 2

        # -------------------------------------------------------------
        # TẦNG 1: Tâm Ngắm (Center 3x3 px) — Ưu tiên số 1
        # -------------------------------------------------------------
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                idx = ((cy + dy) * rect_size + (cx + dx)) * 4
                if is_target_pixel(raw_bytes, idx, color_mode):
                    return True

        # -------------------------------------------------------------
        # TẦNG 2: Cột Đầu Địch Kê Góc (Upper Vertical Comb) — Ưu tiên số 2
        # dx ∈ [-2..2], dy ∈ [-24..-1] quét ngược từ trên xuống dưới
        # -------------------------------------------------------------
        for dy in range(-SCAN_RADIUS + 1, 0):  # -24 đến -1
            for dx in (-2, -1, 0, 1, 2):
                idx = ((cy + dy) * rect_size + (cx + dx)) * 4
                if is_target_pixel(raw_bytes, idx, color_mode):
                    return True

        # -------------------------------------------------------------
        # TẦNG 3: 16 Tia Phóng Nan Hoa (16 Star-burst Rays) — Ưu tiên số 3
        # -------------------------------------------------------------
        hit_count = 0
        min_hit_dist = SCAN_RADIUS
        closest_ray = -1

        for i in range(16):
            ray = self.ray_offsets.offsets[i]
            for r in range(1, SCAN_RADIUS):
                rx, ry = ray[r]
                px = cx + rx
                py = cy + ry
                if 0 <= px < rect_size and 0 <= py < rect_size:
                    idx = (py * rect_size + px) * 4
                    if is_target_pixel(raw_bytes, idx, color_mode):
                        hit_count += 1
                        if r < min_hit_dist:
                            min_hit_dist = r
                            closest_ray = i
                        break

        # Nếu có từ 2 tia chạm màu và mục tiêu nằm sát tâm (khoảng cách <= 2px)
        if hit_count >= 2 and closest_ray != -1:
            if min_hit_dist <= 2:
                return True

        return False

    def close(self):
        self.sct.close()

# =========================================================================
# Giao Tiếp Phần Cứng Với RP2040 (Non-blocking, Không Flush, Không Khựng)
# =========================================================================
def send_4byte_packet(ser, opcode, nonce):
    enc_op = opcode ^ PROTO_KEY ^ nonce
    checksum = (PROTO_MAGIC + enc_op + nonce) & 0xFF
    packet = bytes([PROTO_MAGIC, enc_op, nonce, checksum])
    ser.write(packet)
    # KHÔNG gọi ser.flush() để tránh block luồng CPU gây giật khựng chuột

def find_and_connect_rp2040():
    ports = sorted(
        serial.tools.list_ports.comports(),
        key=lambda p: int(p.device[3:]) if p.device.startswith("COM") and p.device[3:].isdigit() else 0,
        reverse=True
    )
    for p in ports:
        try:
            s = serial.Serial(p.device, 115200, timeout=0.1, write_timeout=0.1)
            time.sleep(0.03)
            s.reset_input_buffer()
            s.write(b"GET_STATUS\n")
            time.sleep(0.04)
            resp = s.read(128).decode("utf-8", errors="ignore")
            if "STATUS_OK" in resp:
                print(f"[HARDWARE] Da ket noi voi RP2040 tren {p.device} (Xac thuc thanh cong)")
                return s
            s.close()
        except Exception:
            pass
    return None

def is_key_held(v_key):
    return (user32.GetAsyncKeyState(v_key) & 0x8000) != 0

def play_sound(enabled):
    freq = 1000 if enabled else 400
    winsound.Beep(freq, 70)

COLOR_NAMES = {
    0: "YELLOW (Default)",
    1: "PURPLE",
    2: "RED"
}

# =========================================================================
# Vòng Lặp Thực Thi Chính
# =========================================================================
def main():
    print("=" * 65)
    print("  CVa 2 v2 — Python Controller (mss + Exact CVa 3-Tier Scan)")
    print("  Hardware Target: Waveshare RP2040-Zero (VXE R1 SE+ Dongle)")
    print("=" * 65)

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    center_x = screen_w // 2
    center_y = screen_h // 2
    print(f"[SCREEN] Do phan giai: {screen_w}x{screen_h} (Tam ngam: {center_x}, {center_y})")

    scanner = CVa3TierScanner(rect_size=RECT_SIZE)
    region = scanner.get_grab_region(center_x, center_y)
    print(f"[CAPTURE] Khoi tao mss + CVa 3-Tier Scan (Vung FOV: {RECT_SIZE}x{RECT_SIZE} quanh tam, khong dung BitBlt)")

    ser = find_and_connect_rp2040()
    if ser:
        play_sound(True)
    else:
        print("[WARNING] Chua tim thay mach RP2040. Tu dong do tim ngam...")

    color_mode = 0  # 0 = Yellow, 1 = Purple, 2 = Red
    block_ad = False
    raw_accel = False
    nonce = 1
    next_click_time = 0.0
    next_reconnect_time = 0.0
    next_center_refresh = 0.0

    page_down_was_held = False
    backtick_was_held = False
    page_up_was_held = False

    print("\n[PHIM TAT]")
    print("  - Giu [Chuot Phai]: Quet mau & Tu dong ban bang phan cung")
    print("  - [PageDown]: Chuyen doi mau (YELLOW -> PURPLE -> RED)")
    print("  - [`~] (Backtick): Bat/tat chan ban khi di chuyen A/D (Counter-strafe)")
    print("  - [PageUp]: Bat/tat gia toc chuot phan cung (Raw Accel)")
    print("  - [End]: Tat chuong trinh\n")
    print(f"[READY] Mau dang chon: {COLOR_NAMES[color_mode]}")

    try:
        while True:
            if is_key_held(VK_END):
                print("[EXIT] Phim End duoc nhan. Dang tat chuong trinh...")
                play_sound(False)
                break

            now = time.perf_counter()

            # Tu dong cap nhat do phan giai & tam ngam moi 1 giay (Tu thich nghi 4:3 / 16:9)
            if now >= next_center_refresh:
                next_center_refresh = now + 1.0
                cur_w = user32.GetSystemMetrics(0)
                cur_h = user32.GetSystemMetrics(1)
                if cur_w != screen_w or cur_h != screen_h:
                    screen_w, screen_h = cur_w, cur_h
                    center_x = screen_w // 2
                    center_y = screen_h // 2
                    region = scanner.get_grab_region(center_x, center_y)
                    print(f"[SCREEN] Cap nhat do phan giai: {screen_w}x{screen_h} (Tam: {center_x}, {center_y})")

            # Tu dong ket noi lai RP2040 o che do nen
            if ser is None and now >= next_reconnect_time:
                next_reconnect_time = now + 1.5
                ser = find_and_connect_rp2040()
                if ser:
                    play_sound(True)

            # Hotkey: PageDown -> Chuyen doi Vang (0) -> Tim (1) -> Do (2)
            pd_held = is_key_held(VK_PAGEDOWN)
            if pd_held and not page_down_was_held:
                color_mode = (color_mode + 1) % 3
                play_sound(True)
                print(f"[COLOR] Chuyen sang mau: {COLOR_NAMES[color_mode]}")
            page_down_was_held = pd_held

            # Hotkey: `~ -> Bat/tat chan di chuyen A/D
            bt_held = is_key_held(VK_OEM_3)
            if bt_held and not backtick_was_held:
                block_ad = not block_ad
                play_sound(block_ad)
                print(f"[CONFIG] Chan ban khi nhan A/D: {'BAT' if block_ad else 'TAT'}")
            backtick_was_held = bt_held

            # Hotkey: PageUp -> Bat/tat Raw Accel
            pu_held = is_key_held(VK_PAGEUP)
            if pu_held and not page_up_was_held:
                raw_accel = not raw_accel
                play_sound(raw_accel)
                print(f"[CONFIG] Gia toc chuot phan cung: {'BAT' if raw_accel else 'TAT'}")
                if ser:
                    try:
                        op = OP_ACCEL_ON if raw_accel else OP_ACCEL_OFF
                        send_4byte_packet(ser, op, nonce)
                        nonce = (nonce + 1) & 0xFF
                    except Exception:
                        ser = None
            page_up_was_held = pu_held

            # Kiem tra giu Chuot Phai (VK_RBUTTON)
            if not is_key_held(VK_RBUTTON):
                time.sleep(0.001)
                continue

            # Chan ban neu dang di chuyen A hoac D
            if block_ad and (is_key_held(VK_A) or is_key_held(VK_D)):
                time.sleep(0.001)
                continue

            # Quet mau bang mss + 3 Tang Uu Tien Chuan Du An Cu CVa
            target_found = scanner.scan_for_color(region, color_mode)

            if target_found and now >= next_click_time:
                if ser:
                    try:
                        send_4byte_packet(ser, OP_CLICK, nonce)
                        nonce = (nonce + 1) & 0xFF
                        next_click_time = now + CLICK_PERIOD_SEC
                    except Exception:
                        ser = None
                else:
                    next_click_time = now + CLICK_PERIOD_SEC

    finally:
        scanner.close()
        if ser:
            ser.close()

if __name__ == "__main__":
    main()
