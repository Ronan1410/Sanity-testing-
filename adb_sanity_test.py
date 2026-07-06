#!/usr/bin/env python3
"""
adb_sanity_test.py
--------------------
IVI Sanity Test Suite - ADB Automation (Python port of adb_sanity_test.sh)

Runs ST_001..ST_021 sequentially against a connected Android Automotive
(AAOS) or Android-based IVI target over ADB.

Some checks are FULL-AUTO (pass/fail decided by script from adb output),
some are SEMI-AUTO (script captures state, tester confirms pass/fail
because the result is physical/visual/audible), and a few require
vendor-specific HAL/tools that vary by platform (flagged as MANUAL).

Usage:
    python3 adb_sanity_test.py [device_serial]

Requires: adb in PATH, target already connected (USB or adb connect ip:port)
"""

import csv
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---- EDIT THESE FOR YOUR TARGET ----------------------------------------
# Package names to test in ST_004. Update to match your IVI's actual apps.
APPS = [
    ("com.android.car.media", "Media"),
    ("com.android.car.dialer", "Phone"),
    ("com.android.car.settings", "Settings"),
    ("com.google.android.apps.maps", "Navigation"),  # or your OEM nav app
    ("com.android.car.radio", "Radio"),
]
# VHAL property IDs are OEM/HAL specific. Replace with your platform's
# actual property IDs (see hardware/interfaces/automotive/vehicle or
# your vendor's VHAL definitions). Left blank here as placeholders.
VHAL_PROP_GEAR = ""       # e.g. "289408000"  (GEAR_SELECTION)
VHAL_PROP_DOOR_LOCK = ""  # e.g. "315274270"  (DOOR_LOCK)
# -------------------------------------------------------------------------

CRASH_PATTERN = re.compile(r"FATAL EXCEPTION|ANR in|Process .* has died")


class SanityRunner:
    def __init__(self, serial: str | None = None):
        self.adb_base = ["adb"] if not serial else ["adb", "-s", serial]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir = Path(f"sanity_results_{timestamp}")
        self.log_dir = self.results_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.results_file = self.results_dir / "results.csv"
        self.rows = []
        self.timestamp = timestamp

    # ---------------- adb helpers ----------------

    def adb(self, *args, timeout=30, check=False):
        """Run an adb command, return CompletedProcess (stdout/stderr as text)."""
        cmd = self.adb_base + list(args)
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=check
            )
        except subprocess.TimeoutExpired as e:
            class Fake:
                stdout = e.stdout or ""
                stderr = e.stderr or ""
                returncode = -1
            return Fake()

    def adb_shell(self, cmd_str, timeout=30):
        return self.adb("shell", cmd_str, timeout=timeout)

    def logcat_clear(self):
        self.adb("logcat", "-c")

    def logcat_dump(self, test_id, seconds=None):
        """Capture current logcat buffer (or stream for N seconds) to a file."""
        logfile = self.log_dir / f"{test_id}_logcat.txt"
        if seconds:
            try:
                proc = subprocess.run(
                    self.adb_base + ["logcat"],
                    capture_output=True, text=True, timeout=seconds
                )
                content = proc.stdout
            except subprocess.TimeoutExpired as e:
                content = e.stdout or ""
        else:
            proc = self.adb("logcat", "-d")
            content = proc.stdout
        logfile.write_text(content or "", errors="replace")
        return logfile

    @staticmethod
    def check_no_crash(logfile: Path) -> bool:
        try:
            text = logfile.read_text(errors="replace")
        except FileNotFoundError:
            return True
        return not CRASH_PATTERN.search(text)

    # ---------------- output helpers ----------------

    def pass_(self, msg):
        print(f"  [PASS] {msg}")

    def fail(self, msg):
        print(f"  [FAIL] {msg}")

    def manual(self, msg):
        print(f"  [MANUAL/CONFIRM] {msg}")

    def record(self, test_id, description, status, detail, logfile=""):
        self.rows.append({
            "test_id": test_id,
            "description": description,
            "status": status,
            "detail": detail,
            "logfile": str(logfile) if logfile else "",
        })

    def write_results_csv(self):
        with open(self.results_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["test_id", "description", "status", "detail", "logfile"]
            )
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row)

    def wait_for_device(self):
        print("Waiting for device...")
        self.adb("wait-for-device", timeout=120)

    # ---------------- test steps ----------------

    def st_001_boot(self):
        print("\n[ST_001] Flash latest build & power ON - boot check")
        self.logcat_clear()
        self.wait_for_device()
        boot_wait = 0
        booted = False
        while boot_wait < 120:
            r = self.adb_shell("getprop sys.boot_completed", timeout=10)
            state = (r.stdout or "").strip()
            if state == "1":
                booted = True
                break
            time.sleep(2)
            boot_wait += 2
        logfile = self.logcat_dump("ST_001", seconds=3)
        if booted and self.check_no_crash(logfile):
            self.pass_(f"Device booted in ~{boot_wait}s with no fatal errors in log")
            self.record("ST_001", "Boot after flash", "PASS", f"boot_time_s={boot_wait}", logfile)
        else:
            self.fail("Boot did not complete cleanly within timeout")
            self.record("ST_001", "Boot after flash", "FAIL", f"boot_time_s={boot_wait}", logfile)

    def st_002_launcher(self):
        print("\n[ST_002] Boot animation / launcher timing")
        start = time.time()
        top = ""
        for _ in range(60):
            r = self.adb_shell("dumpsys activity activities", timeout=10)
            for line in (r.stdout or "").splitlines():
                if "mResumedActivity" in line:
                    top = line.strip()
                    break
            if top:
                break
            time.sleep(1)
        elapsed = int(time.time() - start)
        logfile = self.log_dir / "ST_002_top_activity.txt"
        logfile.write_text(top or "")
        if top:
            self.pass_(f"Home/launcher activity detected after {elapsed}s: {top}")
            self.record("ST_002", "Boot animation/launcher", "PASS", f"elapsed_s={elapsed}", logfile)
        else:
            self.fail("No resumed activity detected")
            self.record("ST_002", "Boot animation/launcher", "FAIL", f"elapsed_s={elapsed}", logfile)

    def st_003_touch(self):
        print("\n[ST_003] Touch functionality (semi-auto)")
        self.adb_shell("input tap 100 100")
        time.sleep(0.3)
        self.adb_shell("input tap 500 500")
        time.sleep(0.3)
        self.adb_shell("input swipe 100 800 800 800 300")
        logfile = self.logcat_dump("ST_003", seconds=3)
        self.manual("Taps/swipe injected. Confirm on-screen response was correct.")
        self.record("ST_003", "Touch functionality", "MANUAL_CONFIRM", "taps+swipe injected", logfile)

    def st_004_apps(self):
        print("\n[ST_004] Launch major apps, check for crash/ANR")
        for pkg, name in APPS:
            self.logcat_clear()
            monkey = self.adb_shell(
                f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", timeout=15
            )
            time.sleep(3)
            logfile = self.logcat_dump(f"ST_004_{name}", seconds=3)
            monkey_out = (monkey.stdout or "") + (monkey.stderr or "")
            test_id = f"ST_004_{name}"
            if "No activities found" in monkey_out:
                self.fail(f"{name} ({pkg}): package/launcher not found on device")
                self.record(test_id, f"Launch {name}", "FAIL", "package not found", logfile)
            elif self.check_no_crash(logfile):
                self.pass_(f"{name} ({pkg}) launched cleanly")
                self.record(test_id, f"Launch {name}", "PASS", "no crash/ANR", logfile)
            else:
                self.fail(f"{name} ({pkg}) crashed or ANR'd")
                self.record(test_id, f"Launch {name}", "FAIL", "crash/ANR detected", logfile)
            self.adb_shell("input keyevent KEYCODE_HOME")

    def st_005_volume(self):
        print("\n[ST_005] Volume up/down via keyevent + verify stream level")
        before = self._stream_music_snippet()
        self.adb_shell("input keyevent KEYCODE_VOLUME_UP")
        time.sleep(0.5)
        self.adb_shell("input keyevent KEYCODE_VOLUME_UP")
        time.sleep(0.5)
        after = self._stream_music_snippet()
        logfile = self.log_dir / "ST_005_audio.txt"
        logfile.write_text(f"BEFORE:\n{before}\nAFTER:\n{after}\n")
        if before != after:
            self.pass_("STREAM_MUSIC level changed after volume key events")
            self.record("ST_005", "Volume up/down", "PASS", "stream level changed", logfile)
        else:
            self.manual("Could not confirm level change from dumpsys; verify audibly")
            self.record("ST_005", "Volume up/down", "MANUAL_CONFIRM", "no diff detected in dumpsys", logfile)

    def _stream_music_snippet(self):
        r = self.adb_shell("dumpsys audio", timeout=15)
        lines = (r.stdout or "").splitlines()
        for i, line in enumerate(lines):
            if "STREAM_MUSIC" in line:
                return "\n".join(lines[i:i + 3])
        return ""

    def st_006_radio(self):
        print("\n[ST_006] FM/AM radio tune (manual - vendor HAL specific)")
        self.manual("Radio tuning is vendor-HAL specific; no generic ADB command exists.")
        self.manual("Tune manually via UI/steering control and confirm audio plays.")
        self.record("ST_006", "Radio tune", "MANUAL", "vendor radio HAL - no generic adb path")

    def st_007_bt_pairing(self):
        print("\n[ST_007] Bluetooth pairing / reconnect state")
        r = self.adb_shell("dumpsys bluetooth_manager", timeout=15)
        logfile = self.log_dir / "ST_007_bt.txt"
        logfile.write_text(r.stdout or "")
        if "Bonded devices" in (r.stdout or ""):
            self.manual("Bonded device list captured. Confirm pairing + auto-reconnect manually.")
            self.record("ST_007", "BT pairing/reconnect", "MANUAL_CONFIRM", "see log for bonded devices", logfile)
        else:
            self.fail("Could not read bluetooth_manager state")
            self.record("ST_007", "BT pairing/reconnect", "FAIL", "dumpsys bluetooth_manager unreadable", logfile)

    def st_008_a2dp(self):
        print("\n[ST_008] A2DP playback state")
        r = self.adb_shell("dumpsys bluetooth_manager", timeout=15)
        logfile = self.log_dir / "ST_008_a2dp.txt"
        logfile.write_text(r.stdout or "")
        if "A2DP" in (r.stdout or ""):
            self.manual("A2DP profile info captured. Confirm audio quality manually.")
            self.record("ST_008", "A2DP playback", "MANUAL_CONFIRM", "A2DP profile present in dumpsys", logfile)
        else:
            self.manual("A2DP not found in dumpsys - confirm connection manually first")
            self.record("ST_008", "A2DP playback", "MANUAL", "A2DP not detected", logfile)

    def st_009_bt_call(self):
        print("\n[ST_009] Bluetooth call audio (manual)")
        r = self.adb_shell("dumpsys telecom", timeout=15)
        logfile = self.log_dir / "ST_009_telecom.txt"
        logfile.write_text(r.stdout or "")
        self.manual("Place/receive a test BT call now; confirm audio routes to car speakers/mic.")
        self.record("ST_009", "BT call audio", "MANUAL", "telecom dumpsys captured for reference", logfile)

    def st_010_usb(self):
        print("\n[ST_010] USB media detection")
        r1 = self.adb_shell("dumpsys mount", timeout=15)
        r2 = self.adb_shell("sm list-volumes", timeout=15)
        logfile = self.log_dir / "ST_010_usb.txt"
        combined = (r1.stdout or "") + "\n" + (r2.stdout or "")
        logfile.write_text(combined)
        if re.search(r"usb|public", combined, re.IGNORECASE):
            self.pass_("USB/public volume detected")
            self.record("ST_010", "USB detection", "PASS", "volume found - confirm playback manually", logfile)
        else:
            self.fail("No USB volume detected - is drive inserted?")
            self.record("ST_010", "USB detection", "FAIL", "no usb volume found", logfile)

    def st_011_wifi(self):
        print("\n[ST_011] Wi-Fi connectivity")
        r = self.adb_shell("dumpsys wifi", timeout=15)
        logfile = self.log_dir / "ST_011_wifi.txt"
        logfile.write_text(r.stdout or "")
        if re.search(r"mNetworkInfo.*CONNECTED|Wifi is connected", r.stdout or "", re.IGNORECASE):
            self.pass_("Wi-Fi reports CONNECTED")
            self.record("ST_011", "Wi-Fi connect", "PASS", "connected state found", logfile)
        else:
            self.fail("Wi-Fi not connected")
            self.record("ST_011", "Wi-Fi connect", "FAIL", "connected state not found", logfile)

    def st_012_gps(self):
        print("\n[ST_012] GPS / Navigation location fix")
        r = self.adb_shell("dumpsys location", timeout=15)
        logfile = self.log_dir / "ST_012_location.txt"
        logfile.write_text(r.stdout or "")
        if re.search(r"last location|fused", r.stdout or "", re.IGNORECASE):
            self.pass_("Location provider data present")
            self.record("ST_012", "GPS/Navigation", "PASS", "location data present - confirm map visually", logfile)
        else:
            self.fail("No location fix data found")
            self.record("ST_012", "GPS/Navigation", "FAIL", "no location data", logfile)

    def st_013_vehicle_info(self):
        print("\n[ST_013] Vehicle info via car service (AAOS)")
        r = self.adb_shell("dumpsys car_service", timeout=20)
        logfile = self.log_dir / "ST_013_car_service.txt"
        logfile.write_text(r.stdout or "")
        if (r.stdout or "").strip():
            self.manual("car_service dump captured. Cross-check speed/gear/fuel/door values against expected.")
            self.record("ST_013", "Vehicle info (CAN)", "MANUAL_CONFIRM", "see car_service dump", logfile)
        else:
            self.fail("car_service dumpsys empty/unavailable - is this AAOS?")
            self.record("ST_013", "Vehicle info (CAN)", "FAIL", "car_service unavailable", logfile)

    def st_014_hvac(self):
        print("\n[ST_014] HVAC control")
        r = self.adb_shell("dumpsys car_service --services CarPropertyService", timeout=20)
        logfile = self.log_dir / "ST_014_hvac.txt"
        logfile.write_text(r.stdout or "")
        self.manual("Adjust temperature/fan/AC via UI, then compare HVAC property values in log.")
        self.record("ST_014", "HVAC control", "MANUAL_CONFIRM", "see car_service HVAC property dump", logfile)

    def st_015_reverse_camera(self):
        print("\n[ST_015] Reverse gear -> rear camera")
        logfile = self.log_dir / "ST_015_reverse.txt"
        if VHAL_PROP_GEAR:
            r = self.adb_shell(
                f"cmd car_service inject-vhal-event {VHAL_PROP_GEAR} REVERSE_VALUE_HERE", timeout=15
            )
            time.sleep(2)
            r2 = self.adb_shell("dumpsys activity activities", timeout=15)
            camera_lines = [l for l in (r2.stdout or "").splitlines() if "camera" in l.lower()]
            logfile.write_text((r.stdout or "") + "\n" + "\n".join(camera_lines))
            self.manual("Reverse gear injected via VHAL. Confirm rear camera view is live/clear.")
            self.record("ST_015", "Reverse camera", "MANUAL_CONFIRM", "gear injection attempted", logfile)
        else:
            logfile.write_text("VHAL_PROP_GEAR not configured in script.")
            self.manual("VHAL_PROP_GEAR not configured. Engage reverse physically and confirm camera.")
            self.record("ST_015", "Reverse camera", "MANUAL", "VHAL prop id not set in script", logfile)

    def st_016_steering_buttons(self):
        print("\n[ST_016] Steering wheel buttons (volume/track/voice)")
        logfile = self.logcat_dump("ST_016", seconds=2)
        self.manual("Press steering wheel Volume/Track/Voice buttons now; confirm each triggers expected action.")
        self.record("ST_016", "Steering wheel buttons", "MANUAL", "physical button press required", logfile)

    def st_017_lock_unlock(self):
        print("\n[ST_017] Lock/unlock vehicle")
        logfile = self.log_dir / "ST_017_lock.txt"
        if VHAL_PROP_DOOR_LOCK:
            r = self.adb_shell(
                f"cmd car_service inject-vhal-event {VHAL_PROP_DOOR_LOCK} LOCK_VALUE_HERE", timeout=15
            )
            logfile.write_text(r.stdout or "")
            self.manual("Door lock event injected. Confirm IVI status icon updates correctly.")
            self.record("ST_017", "Lock/unlock", "MANUAL_CONFIRM", "lock event injected", logfile)
        else:
            logfile.write_text("VHAL_PROP_DOOR_LOCK not configured in script.")
            self.manual("VHAL_PROP_DOOR_LOCK not configured. Lock/unlock physically and confirm IVI status.")
            self.record("ST_017", "Lock/unlock", "MANUAL", "VHAL prop id not set in script", logfile)

    def st_018_ignition_cycle(self):
        print("\n[ST_018] Ignition OFF -> sleep -> ON resume")
        self.adb_shell("input keyevent KEYCODE_SLEEP")
        time.sleep(5)
        self.adb_shell("input keyevent KEYCODE_WAKEUP")
        time.sleep(3)
        logfile = self.logcat_dump("ST_018", seconds=5)
        if self.check_no_crash(logfile):
            self.pass_("Resumed from sleep with no fatal errors logged")
            self.record("ST_018", "Ignition off/on resume", "PASS", "no crash on resume", logfile)
        else:
            self.fail("Crash/ANR detected on resume")
            self.record("ST_018", "Ignition off/on resume", "FAIL", "crash detected", logfile)

    def st_019_settings_persistence(self):
        print("\n[ST_019] Settings persistence (brightness/language/sound)")
        r0 = self.adb_shell("settings get system screen_brightness", timeout=10)
        orig = (r0.stdout or "").strip()
        self.adb_shell("settings put system screen_brightness 150")
        self.adb_shell("input keyevent KEYCODE_SLEEP")
        time.sleep(3)
        self.adb_shell("input keyevent KEYCODE_WAKEUP")
        time.sleep(2)
        r1 = self.adb_shell("settings get system screen_brightness", timeout=10)
        new_val = (r1.stdout or "").strip()
        logfile = self.log_dir / "ST_019_settings.txt"
        logfile.write_text(f"orig={orig}\nafter_cycle={new_val}\n")
        if new_val == "150":
            self.pass_("Brightness setting persisted (150) after sleep/wake cycle")
            self.record("ST_019", "Settings persistence", "PASS", "brightness persisted", logfile)
        else:
            self.fail("Brightness setting did not persist as expected")
            self.record("ST_019", "Settings persistence", "FAIL", f"expected 150 got {new_val}", logfile)
        if orig:
            self.adb_shell(f"settings put system screen_brightness {orig}")

    def st_020_stability(self):
        print("\n[ST_020] System stability monitor (short window)")
        self.logcat_clear()
        monitor_secs = 60
        print(f"Monitoring logcat for {monitor_secs}s for crashes/ANRs/reboots...")
        logfile = self.logcat_dump("ST_020", seconds=monitor_secs)
        if self.check_no_crash(logfile):
            self.pass_(f"No crashes/ANRs during {monitor_secs}s monitor window")
            self.record("ST_020", "Stability monitor", "PASS", f"clean {monitor_secs}s window", logfile)
        else:
            self.fail("Crash/ANR detected during monitor window")
            self.record("ST_020", "Stability monitor", "FAIL", "see log", logfile)

    def st_021_full_log_review(self):
        print("\n[ST_021] Full logcat review for critical errors")
        r = self.adb("logcat", "-d", timeout=30)
        logfile = self.log_dir / "ST_021_full_logcat.txt"
        content = r.stdout or ""
        logfile.write_text(content, errors="replace")
        crit_count = len(CRASH_PATTERN.findall(content))
        if crit_count == 0:
            self.pass_("No FATAL/ANR/process-death entries found in full logcat")
            self.record("ST_021", "Full log review", "PASS", "0 critical entries", logfile)
        else:
            self.fail(f"{crit_count} critical entries found - see {logfile}")
            self.record("ST_021", "Full log review", "FAIL", f"{crit_count} critical entries", logfile)

    # ---------------- orchestration ----------------

    def run_all(self):
        print("=" * 66)
        print(f" IVI Sanity Test Suite - Run started: {self.timestamp}")
        print(f" Results directory: {self.results_dir}")
        print("=" * 66)

        steps = [
            self.st_001_boot,
            self.st_002_launcher,
            self.st_003_touch,
            self.st_004_apps,
            self.st_005_volume,
            self.st_006_radio,
            self.st_007_bt_pairing,
            self.st_008_a2dp,
            self.st_009_bt_call,
            self.st_010_usb,
            self.st_011_wifi,
            self.st_012_gps,
            self.st_013_vehicle_info,
            self.st_014_hvac,
            self.st_015_reverse_camera,
            self.st_016_steering_buttons,
            self.st_017_lock_unlock,
            self.st_018_ignition_cycle,
            self.st_019_settings_persistence,
            self.st_020_stability,
            self.st_021_full_log_review,
        ]

        for step in steps:
            try:
                step()
            except Exception as e:
                print(f"  [ERROR] Step {step.__name__} raised an exception: {e}")
                self.record(step.__name__, step.__name__, "FAIL", f"exception: {e}")

        self.write_results_csv()

        print("\n" + "=" * 66)
        print(f" Sanity run complete. Results: {self.results_file}")
        print(f" Logs: {self.log_dir}")
        print("=" * 66)
        print(f'Next: python3 generate_report.py "{self.results_dir}"')


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else None
    runner = SanityRunner(serial)
    runner.run_all()


if __name__ == "__main__":
    main()
