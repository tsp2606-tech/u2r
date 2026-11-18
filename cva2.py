"""
CVa 2 v2 — Pure Custom HID Controller for RP2040DongleV2
Architecture: Single-Process, Zero-IPC, Pure USB HID (Usage Page 0xFF00, Usage 0x01)
Hardware Target: Waveshare RP2040-Zero (VXE R1 SE+ Wireless Mouse Dongle)
Features:
  - 100% Pure USB HID: Hoàn toàn không mở cổng COM Serial, không hiện trong Ports (COM & LPT)
  - Windows nhận diện thiết bị như chuột gaming thương mại (VXE, Razer, Logitech có Custom Vendor Interface)
  - Đơn tiến trình (Single-Process): Xóa bỏ hoàn toàn Shared Memory, Named Event, không có tiến trình con
  - Chụp màn hình qua mss (< 0.1ms, FOV 50x50), không dùng BitBlt hay DXGI hook
  - Quét 3 tầng ưu tiên chuẩn CVa nguyên bản: Tâm 3x3 -> Cột đầu 24px -> 16 tia Starburst
  - Đại số số nguyên không phép chia (Zero-Division Integer Algebra) trên kênh RGB
  - Tự động thích nghi độ phân giải màn hình (16:9, 4:3 Stretched)
  - Cáp USB hoàn toàn im lặng khi ngắm (0 byte/s), chỉ bắn 65-byte Output Report khi thấy mục tiêu
"""

import sys
import time
import math
import ctypes
from ctypes import wintypes
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
VK_LCONTROL= 0xA2  # Left Control (Phím kích hoạt)
VK_A       = 0x41
VK_D       = 0x44
VK_OEM_3   = 0xC0  # `~ Backtick
VK_END     = 0x23
VK_PAGEUP  = 0x21
VK_PAGEDOWN= 0x22

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
hid_dll  = ctypes.windll.hid
setupapi = ctypes.windll.setupapi

# =========================================================================
# Win32 HID Native Setup
# =========================================================================
class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', wintypes.BYTE * 8)
    ]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('InterfaceClassGuid', GUID),
        ('Flags', wintypes.DWORD),
        ('Reserved', ctypes.c_size_t)
    ]

setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
hid_dll.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]

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
# Giao Tiếp Độc Quyền Qua Custom HID (Zero-COM, Chuẩn Macro Chuột Thật)
# =========================================================================
def find_rp2040_hid_device():
    guid = GUID()
    hid_dll.HidD_GetHidGuid(ctypes.byref(guid))
    dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    if_data = SP_DEVICE_INTERFACE_DATA()
    if_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
    idx = 0
    found_handle = None

    while setupapi.SetupDiEnumDeviceInterfaces(dev_info, None, ctypes.byref(guid), idx, ctypes.byref(if_data)):
        idx += 1
        req_size = wintypes.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(dev_info, ctypes.byref(if_data), None, 0, ctypes.byref(req_size), None)
        if req_size.value == 0: continue
        buf = (ctypes.c_byte * req_size.value)()
        ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if setupapi.SetupDiGetDeviceInterfaceDetailW(dev_info, ctypes.byref(if_data), buf, req_size, None, None):
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            path_lower = path.lower()
            # Khớp định danh VXE R1 SE+ (VID: 0x248A, PID: 0x8367) hoặc EAGET (0x3936:0xE025)
            if any(vid in path_lower for vid in ['vid_248a&pid_8367', 'vid_3936&pid_e025']):
                for acc in [0xC0000000, 0x40000000, 0]: # Thử tuần tự quyền truy cập
                    h = kernel32.CreateFileW(path, acc, 3, None, 3, 0, None)
                    if h and h != 0xFFFFFFFFFFFFFFFF and h != -1:
                        report = (ctypes.c_byte * 65)()
                        report[1] = 0xAA
                        if hid_dll.HidD_SetOutputReport(h, report, 65):
                            found_handle = h
                            break
                        kernel32.CloseHandle(h)
                if found_handle:
                    break

    setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    return found_handle

def send_hid_packet(handle, opcode, nonce):
    enc_op = opcode ^ PROTO_KEY ^ nonce
    checksum = (PROTO_MAGIC + enc_op + nonce) & 0xFF
    report = (ctypes.c_byte * 65)()
    report[0] = 0x00        # Report ID 0
    report[1] = PROTO_MAGIC # 0xAA
    report[2] = enc_op
    report[3] = nonce
    report[4] = checksum
    return hid_dll.HidD_SetOutputReport(handle, report, 65) != 0

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
# Vòng Lặp Thực Thi Đơn Tiến Trình (Single-Process Loop, Zero-IPC)
# =========================================================================
def main():
    print("=" * 65)
    print("  CVa 2 v2 — Pure Custom HID Controller (Single-Process, Zero-IPC)")
    print("  Hardware Target: VXE R1 SE+ Wireless Mouse (Pure USB HID 1000Hz)")
    print("=" * 65)

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    center_x = screen_w // 2
    center_y = screen_h // 2
    print(f"[SCREEN] Do phan giai: {screen_w}x{screen_h} (Tam ngam: {center_x}, {center_y})")

    scanner = CVa3TierScanner(rect_size=RECT_SIZE)
    region = scanner.get_grab_region(center_x, center_y)
    print(f"[CAPTURE] Khoi tao mss + CVa 3-Tier Scan (Vung FOV: {RECT_SIZE}x{RECT_SIZE} quanh tam, khong dung BitBlt)")

    hid_handle = find_rp2040_hid_device()
    if hid_handle:
        print("[HARDWARE] Da ket noi truc tiep qua Custom HID (Zero-COM Mode - San sang)")
        play_sound(True)
    else:
        print("[WARNING] Chua tim thay thiet bi Custom HID. Tu dong do tim ngam...")

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
    print("  - Giu [Left Control]: Quet mau & Tu dong ban bang phan cung")
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

            # Tu dong ket noi lai Custom HID neu mach bi rut ra cam lai
            if hid_handle is None and now >= next_reconnect_time:
                next_reconnect_time = now + 1.5
                hid_handle = find_rp2040_hid_device()
                if hid_handle:
                    print("[HARDWARE] Da ket noi lai qua Custom HID (San sang)")
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
                if hid_handle:
                    try:
                        op = OP_ACCEL_ON if raw_accel else OP_ACCEL_OFF
                        send_hid_packet(hid_handle, op, nonce)
                        nonce = (nonce + 1) & 0xFF
                    except Exception:
                        hid_handle = None
            page_up_was_held = pu_held

            # Kiem tra giu Left Control (VK_LCONTROL)
            if not is_key_held(VK_LCONTROL):
                time.sleep(0.001)
                continue

            # Chan ban neu dang di chuyen A hoac D
            if block_ad and (is_key_held(VK_A) or is_key_held(VK_D)):
                time.sleep(0.001)
                continue

            # Quet mau bang mss + 3 Tang Uu Tien Chuan Du An Cu CVa
            target_found = scanner.scan_for_color(region, color_mode)

            if target_found and now >= next_click_time:
                if hid_handle:
                    try:
                        ok = send_hid_packet(hid_handle, OP_CLICK, nonce)
                        if not ok:
                            kernel32.CloseHandle(hid_handle)
                            hid_handle = None
                        nonce = (nonce + 1) & 0xFF
                        next_click_time = now + CLICK_PERIOD_SEC
                    except Exception:
                        hid_handle = None
                else:
                    next_click_time = now + CLICK_PERIOD_SEC

    finally:
        scanner.close()
        if hid_handle:
            kernel32.CloseHandle(hid_handle)

if __name__ == "__main__":
    main()
