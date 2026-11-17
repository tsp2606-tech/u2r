"""
CVa 2 v2 — Python Controller for RP2040DongleV2
Capture Engine: mss + OpenCV HSV (Tối ưu hóa thị giác máy tính)
Hardware Target: Waveshare RP2040-Zero (VXE R1 SE+ Dongle)
Features:
  - Vùng quét FOV xung quanh tâm ngắm (Crosshair ROI)
  - Không gian màu HSV lọc chuẩn xác (Yellow / Purple / Red)
  - Mặt nạ vùng kích hoạt (Trigger Zone: Vùng đầu + Tâm ngắm)
  - Cáp USB hoàn toàn im lặng khi ngắm (0 byte/s), chỉ bắn 4 byte khi thấy mục tiêu
  - Giao thức nhị phân mã hóa XOR 4-byte tương thích 100% với RP2040DongleV2
"""

import sys
import time
import json
import ctypes
from ctypes import wintypes
import serial
import serial.tools.list_ports
import winsound
import numpy as np
import cv2
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
# Bảng Không Gian Màu HSV (Hue - Saturation - Value)
# =========================================================================
# Hue: 0-180, Saturation: 0-255, Value: 0-255 trong OpenCV
HSV_PRESETS = {
    0: {  # Yellow (Vàng)
        "name": "YELLOW (Default)",
        "lower": np.array([25, 80, 80], dtype=np.uint8),
        "upper": np.array([35, 255, 255], dtype=np.uint8),
    },
    1: {  # Purple / Magenta (Tím)
        "name": "PURPLE",
        "lower": np.array([130, 60, 70], dtype=np.uint8),
        "upper": np.array([165, 255, 255], dtype=np.uint8),
    },
    2: {  # Red (Đỏ)
        "name": "RED",
        "lower1": np.array([0, 90, 90], dtype=np.uint8),
        "upper1": np.array([10, 255, 255], dtype=np.uint8),
        "lower2": np.array([170, 90, 90], dtype=np.uint8),
        "upper2": np.array([180, 255, 255], dtype=np.uint8),
    }
}

# =========================================================================
# Bộ Chụp Màn Hình & Lọc Màu: mss + OpenCV HSV
# =========================================================================
class MSSColorDetector:
    def __init__(self, fov_size=50):
        self.fov_size = fov_size
        self.sct = mss.MSS()
        self.kernel = np.ones((3, 3), np.uint8)

        # Tạo mặt nạ vùng ngắm bắn (Trigger Zone):
        # 1. Hình tròn bán kính 10px quanh tâm
        # 2. Cột thẳng đứng phía trên (Upper Head) để bắt đầu địch khi kê góc
        cx = fov_size // 2
        cy = fov_size // 2
        Y, X = np.ogrid[:fov_size, :fov_size]
        circle = (X - cx)**2 + (Y - cy)**2 <= 11**2
        head_col = (np.abs(X - cx) <= 4) & (Y - cy >= -22) & (Y - cy <= 0)
        self.trigger_mask = circle | head_col

    def get_grab_region(self, center_x, center_y):
        half = self.fov_size // 2
        return {
            "top": int(center_y - half),
            "left": int(center_x - half),
            "width": int(self.fov_size),
            "height": int(self.fov_size)
        }

    def scan_for_color(self, region, color_mode):
        # 1. Chụp ảnh qua mss
        shot = self.sct.grab(region)
        arr = np.frombuffer(shot.raw, dtype=np.uint8).reshape((self.fov_size, self.fov_size, 4))
        bgr = arr[:, :, :3]

        # 2. Chuyển sang không gian màu HSV
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

        # 3. Lọc theo ngưỡng dải màu
        if color_mode == 2:  # Chế độ Đỏ (2 dải)
            p = HSV_PRESETS[2]
            mask1 = cv2.inRange(hsv, p["lower1"], p["upper1"])
            mask2 = cv2.inRange(hsv, p["lower2"], p["upper2"])
            color_mask = cv2.bitwise_or(mask1, mask2)
        else:
            p = HSV_PRESETS.get(color_mode, HSV_PRESETS[0])
            color_mask = cv2.inRange(hsv, p["lower"], p["upper"])

        # 4. Xử lý hình thái học (Morphology) để làm mịn và lọc nhiễu
        color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_CLOSE, self.kernel)

        # 5. Áp dụng mặt nạ vùng ngắm (Chỉ kích hoạt nếu màu nằm trong vùng tâm/đầu ngắm)
        target_in_zone = color_mask & self.trigger_mask

        # 6. Đếm số điểm ảnh hợp lệ (Ngưỡng tối thiểu 2 pixel để loại bỏ hạt nhiễu)
        hits = np.count_nonzero(target_in_zone)
        return hits >= 2

    def close(self):
        self.sct.close()

# =========================================================================
# Giao Tiếp Phần Cứng Với RP2040 (Cổng COM Tự Động Kết Nối)
# =========================================================================
def send_4byte_packet(ser, opcode, nonce):
    enc_op = opcode ^ PROTO_KEY ^ nonce
    checksum = (PROTO_MAGIC + enc_op + nonce) & 0xFF
    packet = bytes([PROTO_MAGIC, enc_op, nonce, checksum])
    ser.write(packet)
    ser.flush()

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

# =========================================================================
# Vòng Lặp Thực Thi Chính
# =========================================================================
def main():
    print("=" * 65)
    print("  CVa 2 v2 — Python Controller (mss + OpenCV HSV Engine)")
    print("  Hardware Target: Waveshare RP2040-Zero (VXE R1 SE+ Dongle)")
    print("=" * 65)

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    center_x = screen_w // 2
    center_y = screen_h // 2
    print(f"[SCREEN] Do phan giai: {screen_w}x{screen_h} (Tam ngam: {center_x}, {center_y})")

    detector = MSSColorDetector(fov_size=50)
    region = detector.get_grab_region(center_x, center_y)
    print(f"[CAPTURE] Khoi tao mss + OpenCV HSV (Vung FOV: 50x50 quanh tam)")

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
    print(f"[READY] Mau dang chon: {HSV_PRESETS[color_mode]['name']}")

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
                    region = detector.get_grab_region(center_x, center_y)
                    print(f"[SCREEN] Cap nhat do phan giai: {screen_w}x{screen_h} (Tam: {center_x}, {center_y})")

            # Tu dong ket noi lai RP2040 o che do nen
            if ser is None and now >= next_reconnect_time:
                next_reconnect_time = now + 1.5
                ser = find_and_connect_rp2040()
                if ser:
                    play_sound(True)

            # Hotkey: PageDown -> Chuyen doi Vang (0) / Tim (1) / Do (2)
            pd_held = is_key_held(VK_PAGEDOWN)
            if pd_held and not page_down_was_held:
                color_mode = (color_mode + 1) % 3
                play_sound(True)
                print(f"[COLOR] Chuyen sang mau: {HSV_PRESETS[color_mode]['name']}")
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

            # Quet mau bang mss + OpenCV HSV
            target_found = detector.scan_for_color(region, color_mode)

            if target_found and now >= next_click_time:
                if ser:
                    try:
                        send_4byte_packet(ser, OP_CLICK, nonce)
                        nonce = (nonce + 1) & 0xFF
                        next_click_time = now + CLICK_PERIOD_SEC
                        print(f"[CLICK] Muc tieu {HSV_PRESETS[color_mode]['name']} lọt vao tam -> Ban click phan cung tai {now:.3f}s")
                    except Exception as e:
                        print(f"[ERROR] Loi gui Serial: {e}")
                        ser = None
                else:
                    print(f"[CLICK] Phat hien muc tieu (RP2040 chua ket noi)")
                    next_click_time = now + CLICK_PERIOD_SEC

    finally:
        detector.close()
        if ser:
            ser.close()

if __name__ == "__main__":
    main()
