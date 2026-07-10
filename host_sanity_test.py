#!/usr/bin/env python3
"""
host_sanity_test.py
---------------------
IVI Sanity Test Suite - Laptop/PC edition.

Replaces ADB entirely with:
  - UART (pyserial) over a USB-serial adapter, wired to the QNX hypervisor /
    bootloader console. Auto-detects the COM port so you don't have to hard-
    code it. Used to trigger resets and watch boot logs / panics.
  - Telnet (over the VLAN the target brings up) for Android shell access
    once booted, instead of adb shell.

Why not ADB: adb's connection drops on every reboot and has to be
re-established (`adb wait-for-device` / manual reconnect), which adds
unpredictable slack into any boot-time measurement. UART has no such
handshake - it's a dumb serial link that's live the instant the target
starts putting bytes on the wire, so boot timing measured off UART is not
contaminated by adb's own reconnect delay.

No CLI required for day-to-day use - all config, including per-test repeat
counts, lives in the CONFIG section below. Run:
    python3 host_sanity_test.py
"""

import csv
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

# ============================= CONFIG ======================================

# "AUTO" scans available COM ports and picks the most likely UART-console
# port (see auto_detect_serial_port()). Set an explicit string (e.g.
# "COM5" or "/dev/ttyUSB0") to skip auto-detection entirely.
COM_PORT = "AUTO"
UART_BAUD = 115200
SERIAL_PORT_KEYWORDS = ["USB", "UART", "CP210", "FTDI", "CH340", "Serial", "CDC"]

# How the target is reset.
#   "uart_cmd"   - sends a text command over UART (bootloader/QNX console)
#   "dtr_toggle" - toggles the serial adapter's DTR line, for rigs where
#                  DTR is wired through a reset circuit (common pattern on
#                  dev boards - confirm your hardware supports this before
#                  relying on it)
RESET_METHOD = "uart_cmd"
RESET_UART_CMD = "reboot"
RESET_PULSE_S = 0.25

TARGET_IP = "192.168.7.2"      # Android IP on the VLAN once booted
TELNET_PORT = 23
TELNET_USER = "root"
TELNET_PASSWORD = ""           # leave "" if telnetd has no auth
TELNET_PROMPT = "$"            # or "#" - match your actual shell prompt

QNX_READY_MARKERS = ["QNX ready", "hypervisor started", "vdev: android0 up"]
ANDROID_BOOT_MARKERS = ["Boot Completed", "sys.boot_completed=1", "BOOTANIMATION: exit"]
KERNEL_PANIC_MARKERS = ["Kernel panic", "PANIC", "Watchdog bite", "Fatal error"]
VLAN_UP_MARKERS = ["vlan0: link up", "vlan0: UP"]

APPS = [
    ("com.android.car.media", "Media"),
    ("com.android.car.dialer", "Phone"),
    ("com.android.car.settings", "Settings"),
    ("com.google.android.apps.maps", "Navigation"),
    ("com.android.car.radio", "Radio"),
]

VHAL_PROP_GEAR = ""       # OEM-specific VHAL property id
VHAL_PROP_DOOR_LOCK = ""  # OEM-specific VHAL property id

# ---- Repeat counts, per test, editable here (no CLI flags) ----
REPEAT = {
    "ST_BOOT_TIME": 3,      # explicit ask: run boot timing multiple times
    "ST_UART_LINK": 1,
    "ST_QNX_BOOT_PHASE": 1,
    "ST_VLAN_UP": 1,
    "ST_TELNET_LOGIN": 1,
    "ST_APPS": 1,
    "ST_VOLUME": 1,
    "ST_SETTINGS": 1,
    "ST_STABILITY": 1,
    "ST_FULL_LOG": 1,
    "ST_KERNEL_PANIC_SCAN": 1,
}

BOOT_TIMEOUT_S = 180

# ============================================================================

CRASH_PATTERN = re.compile(r"FATAL EXCEPTION|ANR in|Process .* has died")


def _probe_port(device, baud, probe_timeout=1.5):
    """Open a candidate port briefly and see if the target talks back."""
    try:
        with serial.Serial(device, baud, timeout=probe_timeout) as s:
            s.reset_input_buffer()
            s.write(b"\r\n")
            time.sleep(0.3)
            data = s.read(s.in_waiting or 64)
            return len(data) > 0
    except Exception:
        return False


def auto_detect_serial_port():
    """
    Returns a COM port device string. Strategy:
      1. List all ports; filter to ones matching common USB-UART chip
         descriptions/manufacturers (CP210x, FTDI, CH340, generic USB CDC).
      2. If exactly one candidate, use it.
      3. If multiple candidates (or none matched and there are multiple
         ports total), probe each by opening it and checking for any
         response - pick the first one that talks back.
      4. If still ambiguous, default to the first candidate and warn.
    """
    ports = list(list_ports.comports())
    if not ports:
        sys.exit("No serial ports found. Plug in the UART adapter and try again, "
                  "or set COM_PORT explicitly in the CONFIG section.")

    def desc_of(p):
        return f"{p.description or ''} {p.manufacturer or ''}"

    candidates = [p for p in ports if any(k.lower() in desc_of(p).lower()
                                           for k in SERIAL_PORT_KEYWORDS)]

    print("Available serial ports:")
    for p in ports:
        flag = " <-- candidate" if p in candidates else ""
        print(f"  {p.device}: {p.description}{flag}")

    if len(candidates) == 1:
        print(f"Auto-selected: {candidates[0].device}")
        return candidates[0].device

    pool = candidates if candidates else ports
    if len(pool) > 1:
        print("Multiple possible ports - probing for a responsive console...")
        for p in pool:
            if _probe_port(p.device, UART_BAUD):
                print(f"Auto-selected (responsive): {p.device}")
                return p.device
        print(f"[WARN] No port responded to a probe; defaulting to {pool[0].device}. "
              f"Set COM_PORT explicitly if this is wrong.")
        return pool[0].device

    print(f"Auto-selected: {pool[0].device}")
    return pool[0].device


class HostUart:
    def __init__(self, port=None, baud=UART_BAUD):
        self.port = port or auto_detect_serial_port()
        self.ser = serial.Serial(self.port, baud, timeout=0.1)
        print(f"UART opened on {self.port} @ {baud} baud")

    def send(self, cmd):
        self.ser.write((cmd + "\r\n").encode())

    def read_available(self):
        n = self.ser.in_waiting
        if n:
            try:
                return self.ser.read(n).decode(errors="ignore")
            except Exception:
                return ""
        return ""

    def wait_for(self, patterns, timeout_s, also_fail_on=None):
        """
        Poll UART until one of `patterns` appears in the accumulated buffer,
        or timeout_s elapses. also_fail_on patterns (e.g. kernel panic
        strings) short-circuit the wait if seen first.
        Returns (matched_pattern_or_None, elapsed_s, full_buffer_text).
        """
        also_fail_on = also_fail_on or []
        start = time.monotonic()
        buf = ""
        while time.monotonic() - start < timeout_s:
            buf += self.read_available()
            for p in also_fail_on:
                if p in buf:
                    return ("FAILMATCH:" + p, time.monotonic() - start, buf)
            for p in patterns:
                if p in buf:
                    return (p, time.monotonic() - start, buf)
            time.sleep(0.05)
        return (None, timeout_s, buf)

    def toggle_dtr_reset(self, pulse_s=RESET_PULSE_S):
        self.ser.dtr = False
        time.sleep(pulse_s)
        self.ser.dtr = True

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class SimpleTelnet:
    """Minimal telnet client (avoids the deprecated/removed stdlib telnetlib)."""

    def __init__(self, host, port=23, timeout_s=10):
        self.sock = socket.create_connection((host, port), timeout=timeout_s)
        self.sock.settimeout(0.2)

    @staticmethod
    def _strip_iac(data):
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == 0xFF and i + 2 < len(data):
                i += 3
                continue
            out.append(data[i])
            i += 1
        return bytes(out)

    def read_until(self, marker, timeout_s=5):
        start = time.monotonic()
        buf = b""
        while time.monotonic() - start < timeout_s:
            try:
                chunk = self.sock.recv(4096)
                if chunk:
                    buf += self._strip_iac(chunk)
                    if marker.encode() in buf:
                        break
            except (socket.timeout, BlockingIOError):
                pass
            except OSError:
                break
            time.sleep(0.05)
        return buf.decode(errors="ignore")

    def send(self, line):
        self.sock.send((line + "\r\n").encode())

    def run_cmd(self, cmd, timeout_s=5, prompt=TELNET_PROMPT):
        self.send(cmd)
        return self.read_until(prompt, timeout_s)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def telnet_shell(timeout_s=10):
    try:
        t = SimpleTelnet(TARGET_IP, TELNET_PORT, timeout_s)
        t.read_until("login:", timeout_s=3)
        t.send(TELNET_USER)
        if TELNET_PASSWORD:
            t.read_until("assword:", timeout_s=3)
            t.send(TELNET_PASSWORD)
        t.read_until(TELNET_PROMPT, timeout_s=5)
        return t
    except Exception as e:
        print(f"  [FAIL] Telnet connect/login failed: {e}")
        return None


class SanityRunner:
    def __init__(self, port=None):
        if port is None and COM_PORT != "AUTO":
            port = COM_PORT
        self.uart = HostUart(port=port)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir = Path(f"sanity_results_{timestamp}")
        self.log_dir = self.results_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.results_file = self.results_dir / "results.csv"
        self.rows = []

    # ---------------- logging / results ----------------

    def log_write(self, name, text):
        path = self.log_dir / f"{name}.txt"
        path.write_text(text or "", errors="replace")
        return path

    def record(self, test_id, description, status, detail, logfile=""):
        self.rows.append({
            "test_id": test_id, "description": description,
            "status": status, "detail": detail,
            "logfile": str(logfile) if logfile else "",
        })
        print(f"  [{status}] {test_id}: {detail}")

    def write_results_csv(self):
        with open(self.results_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["test_id", "description", "status", "detail", "logfile"]
            )
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row)

    # ---------------- reset helper ----------------

    def reset_module(self):
        if RESET_METHOD == "dtr_toggle":
            self.uart.toggle_dtr_reset()
        else:
            self.uart.send(RESET_UART_CMD)

    def run_repeated(self, test_id, description, times, fn):
        results = []
        for i in range(1, times + 1):
            iter_id = f"{test_id}_iter{i}" if times > 1 else test_id
            print(f"\n[{iter_id}] {description} (run {i}/{times})")
            status, detail, logfile = fn(i)
            self.record(iter_id, description, status, detail, logfile)
            results.append((status, detail))

        if times > 1:
            statuses = [r[0] for r in results]
            overall = "PASS" if all(s == "PASS" for s in statuses) else "FAIL"
            nums = []
            for _, d in results:
                try:
                    nums.append(float(d.split("=")[-1].rstrip("s")))
                except Exception:
                    pass
            if nums:
                summary = (f"n={len(nums)} min={min(nums):.1f}s "
                           f"max={max(nums):.1f}s avg={sum(nums)/len(nums):.1f}s")
            else:
                summary = f"{statuses.count('PASS')}/{times} passed"
            self.record(f"{test_id}_SUMMARY", f"{description} (summary)", overall, summary)

    # ---------------- individual test bodies ----------------

    def _boot_time_once(self, iteration):
        self.reset_module()
        marker, elapsed, buf = self.uart.wait_for(
            ANDROID_BOOT_MARKERS, BOOT_TIMEOUT_S, also_fail_on=KERNEL_PANIC_MARKERS
        )
        logfile = self.log_write(f"ST_BOOT_TIME_iter{iteration}", buf)
        if marker is None:
            return ("FAIL", f"no boot marker within {BOOT_TIMEOUT_S}s", logfile)
        if marker.startswith("FAILMATCH:"):
            return ("FAIL", f"kernel panic detected: {marker[10:]} at {elapsed:.1f}s", logfile)
        return ("PASS", f"boot_time_s={elapsed:.1f}", logfile)

    def st_boot_time(self):
        # No adb reconnect involved - UART is live continuously through the
        # reset, so this timing isn't padded by any reconnection handshake.
        self.run_repeated("ST_BOOT_TIME", "Full boot time (reset to Android ready)",
                           REPEAT.get("ST_BOOT_TIME", 1), self._boot_time_once)

    def _qnx_boot_phase_once(self, iteration):
        self.reset_module()
        marker, elapsed, buf = self.uart.wait_for(
            QNX_READY_MARKERS, BOOT_TIMEOUT_S, also_fail_on=KERNEL_PANIC_MARKERS
        )
        logfile = self.log_write(f"ST_QNX_BOOT_PHASE_iter{iteration}", buf)
        if marker is None:
            return ("FAIL", f"QNX ready marker not seen within {BOOT_TIMEOUT_S}s", logfile)
        if marker.startswith("FAILMATCH:"):
            return ("FAIL", f"kernel panic during QNX phase: {marker[10:]}", logfile)
        return ("PASS", f"qnx_ready_s={elapsed:.1f}", logfile)

    def st_qnx_boot_phase(self):
        self.run_repeated("ST_QNX_BOOT_PHASE", "QNX hypervisor ready time",
                           REPEAT.get("ST_QNX_BOOT_PHASE", 1), self._qnx_boot_phase_once)

    def _uart_link_once(self, iteration):
        test_str = f"HOST_UART_PING_{iteration}"
        self.uart.send(test_str)
        marker, elapsed, buf = self.uart.wait_for([test_str], 3)
        logfile = self.log_write(f"ST_UART_LINK_iter{iteration}", buf)
        if marker == test_str:
            return ("PASS", "echo matched, link clean", logfile)
        elif buf:
            return ("FAIL", "response received but did not match - possible corruption", logfile)
        return ("FAIL", "no response - check wiring/baud rate/COM port", logfile)

    def st_uart_link_check(self):
        self.run_repeated("ST_UART_LINK", "UART link integrity check",
                           REPEAT.get("ST_UART_LINK", 1), self._uart_link_once)

    def _vlan_up_once(self, iteration):
        marker, elapsed, buf = self.uart.wait_for(VLAN_UP_MARKERS, 30)
        logfile = self.log_write(f"ST_VLAN_UP_iter{iteration}", buf)
        if marker:
            return ("PASS", f"vlan_up_s={elapsed:.1f}", logfile)
        return ("FAIL", "vlan interface up marker not seen on UART", logfile)

    def st_vlan_network_up(self):
        self.run_repeated("ST_VLAN_UP", "VLAN interface up", REPEAT.get("ST_VLAN_UP", 1),
                           self._vlan_up_once)

    def _telnet_login_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "telnet connect/login failed", "")
        out = t.run_cmd("echo telnet_ok")
        logfile = self.log_write(f"ST_TELNET_LOGIN_iter{iteration}", out)
        t.close()
        if "telnet_ok" in out:
            return ("PASS", "shell reachable and responsive", logfile)
        return ("FAIL", "logged in but shell did not echo test command", logfile)

    def st_telnet_login(self):
        self.run_repeated("ST_TELNET_LOGIN", "Telnet reachability/login",
                           REPEAT.get("ST_TELNET_LOGIN", 1), self._telnet_login_once)

    def _apps_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "no telnet session", "")
        all_out = ""
        any_fail = False
        for pkg, name in APPS:
            t.run_cmd(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
            time.sleep(3)
            out = t.run_cmd(f"logcat -d -t 50 | grep -E 'FATAL EXCEPTION|ANR in' | grep {pkg}")
            all_out += f"--- {name} ({pkg}) ---\n{out}\n"
            if CRASH_PATTERN.search(out):
                any_fail = True
            t.run_cmd("input keyevent KEYCODE_HOME")
        t.close()
        logfile = self.log_write(f"ST_APPS_iter{iteration}", all_out)
        if any_fail:
            return ("FAIL", "one or more apps crashed/ANR'd - see log", logfile)
        return ("PASS", f"{len(APPS)} apps launched cleanly", logfile)

    def st_apps_launch(self):
        self.run_repeated("ST_APPS", "Launch major apps (crash/ANR check)",
                           REPEAT.get("ST_APPS", 1), self._apps_once)

    def _volume_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "no telnet session", "")
        before = t.run_cmd("dumpsys audio | grep -A2 STREAM_MUSIC")
        t.run_cmd("input keyevent KEYCODE_VOLUME_UP")
        t.run_cmd("input keyevent KEYCODE_VOLUME_UP")
        after = t.run_cmd("dumpsys audio | grep -A2 STREAM_MUSIC")
        t.close()
        logfile = self.log_write(f"ST_VOLUME_iter{iteration}", f"BEFORE:\n{before}\nAFTER:\n{after}")
        if before != after:
            return ("PASS", "stream level changed", logfile)
        return ("MANUAL_CONFIRM", "no diff detected via telnet - verify audibly", logfile)

    def st_volume(self):
        self.run_repeated("ST_VOLUME", "Volume up/down", REPEAT.get("ST_VOLUME", 1),
                           self._volume_once)

    def _settings_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "no telnet session", "")
        orig = t.run_cmd("settings get system screen_brightness").strip()
        t.run_cmd("settings put system screen_brightness 150")
        t.run_cmd("input keyevent KEYCODE_SLEEP")
        time.sleep(3)
        t.run_cmd("input keyevent KEYCODE_WAKEUP")
        time.sleep(2)
        new_val = t.run_cmd("settings get system screen_brightness").strip()
        t.run_cmd(f"settings put system screen_brightness {orig}")
        t.close()
        logfile = self.log_write(f"ST_SETTINGS_iter{iteration}", f"orig={orig}\nafter_cycle={new_val}")
        if "150" in new_val:
            return ("PASS", "brightness persisted through sleep/wake", logfile)
        return ("FAIL", f"expected 150, got {new_val}", logfile)

    def st_settings_persistence(self):
        self.run_repeated("ST_SETTINGS", "Settings persistence", REPEAT.get("ST_SETTINGS", 1),
                           self._settings_once)

    def _stability_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "no telnet session", "")
        t.run_cmd("logcat -c")
        window_s = 60
        print(f"  Monitoring logcat over telnet for {window_s}s...")
        out = t.run_cmd(f"sleep {window_s} && logcat -d", timeout_s=window_s + 10)
        t.close()
        logfile = self.log_write(f"ST_STABILITY_iter{iteration}", out)
        if CRASH_PATTERN.search(out):
            return ("FAIL", "crash/ANR detected during monitor window", logfile)
        return ("PASS", f"clean {window_s}s window", logfile)

    def st_stability_monitor(self):
        self.run_repeated("ST_STABILITY", "System stability monitor",
                           REPEAT.get("ST_STABILITY", 1), self._stability_once)

    def _full_log_once(self, iteration):
        t = telnet_shell()
        if t is None:
            return ("FAIL", "no telnet session", "")
        out = t.run_cmd("logcat -d", timeout_s=15)
        t.close()
        logfile = self.log_write(f"ST_FULL_LOG_iter{iteration}", out)
        crit = len(CRASH_PATTERN.findall(out))
        if crit == 0:
            return ("PASS", "0 critical entries", logfile)
        return ("FAIL", f"{crit} critical entries found", logfile)

    def st_full_log_review(self):
        self.run_repeated("ST_FULL_LOG", "Full logcat review", REPEAT.get("ST_FULL_LOG", 1),
                           self._full_log_once)

    def _kernel_panic_scan_once(self, iteration):
        window_s = 20
        buf = ""
        start = time.monotonic()
        while time.monotonic() - start < window_s:
            buf += self.uart.read_available()
            time.sleep(0.1)
        logfile = self.log_write(f"ST_KERNEL_PANIC_SCAN_iter{iteration}", buf)
        for p in KERNEL_PANIC_MARKERS:
            if p in buf:
                return ("FAIL", f"panic marker found: {p}", logfile)
        return ("PASS", f"clean {window_s}s UART capture", logfile)

    def st_kernel_panic_scan(self):
        self.run_repeated("ST_KERNEL_PANIC_SCAN", "QNX/kernel panic scan (UART)",
                           REPEAT.get("ST_KERNEL_PANIC_SCAN", 1), self._kernel_panic_scan_once)

    # ---------------- manual-only items ----------------

    def manual_only_items(self):
        items = [
            ("ST_TOUCH", "Touch functionality", "physical touch/gesture confirmation required"),
            ("ST_RADIO", "FM/AM radio tune", "vendor radio HAL - no generic UART/telnet path"),
            ("ST_BT_PAIR", "Bluetooth pairing/reconnect", "confirm manually or via telnet dumpsys bluetooth_manager"),
            ("ST_BT_A2DP", "A2DP playback", "confirm audio quality manually"),
            ("ST_BT_CALL", "Bluetooth call audio", "confirm manually - place/receive test call"),
            ("ST_USB", "USB media detection/playback", "confirm via telnet dumpsys mount + manual playback check"),
            ("ST_GPS", "GPS/Navigation fix", "confirm via telnet dumpsys location + visual map check"),
            ("ST_VEHICLE_INFO", "Vehicle info (speed/gear/fuel/door)", "cross-check car_service dump via telnet against expected"),
            ("ST_HVAC", "HVAC control", "adjust via UI, compare CarPropertyService values via telnet"),
            ("ST_REVERSE_CAM", "Reverse camera", "inject VHAL gear event if configured, else engage physically"),
            ("ST_STEERING_BTN", "Steering wheel buttons", "physical button press required"),
            ("ST_LOCK_UNLOCK", "Lock/unlock vehicle", "inject VHAL door lock event if configured, else physical test"),
        ]
        for test_id, desc, detail in items:
            self.record(test_id, desc, "MANUAL", detail)

    # ---------------- orchestration ----------------

    def run_all(self):
        print("=" * 66)
        print(" IVI Sanity Test Suite - Laptop/PC (UART + Telnet, no ADB)")
        print(f" Results directory: {self.results_dir}")
        print("=" * 66)

        steps = [
            self.st_uart_link_check,
            self.st_boot_time,
            self.st_qnx_boot_phase,
            self.st_kernel_panic_scan,
            self.st_vlan_network_up,
            self.st_telnet_login,
            self.st_apps_launch,
            self.st_volume,
            self.st_settings_persistence,
            self.st_stability_monitor,
            self.st_full_log_review,
        ]
        for step in steps:
            try:
                step()
            except Exception as e:
                print(f"  [ERROR] {step.__name__} raised: {e}")
                self.record(step.__name__, step.__name__, "FAIL", f"exception: {e}")

        self.manual_only_items()
        self.write_results_csv()
        self.uart.close()

        print("\n" + "=" * 66)
        print(f" Run complete. Results: {self.results_file}")
        print(f" Logs: {self.log_dir}")
        print("=" * 66)
        print(f'Next: python3 generate_report.py "{self.results_dir}"')


def main():
    runner = SanityRunner()
    runner.run_all()


if __name__ == "__main__":
    main()
