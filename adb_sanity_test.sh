#!/usr/bin/env bash
#
# IVI Sanity Test Suite - ADB Automation
# ----------------------------------------
# Runs ST_001..ST_021 sequentially against a connected Android
# Automotive (AAOS) or Android-based IVI target over ADB.
#
# Some checks are FULL-AUTO (pass/fail decided by script from adb output),
# some are SEMI-AUTO (script captures state, tester confirms pass/fail
# because the result is physical/visual/audible), and a few require
# vendor-specific HAL/tools that vary by platform (flagged as MANUAL).
#
# Usage:
#   ./adb_sanity_test.sh [device_serial]
#
# Requires: adb in PATH, target already connected (USB or adb connect ip:port)
#
set -uo pipefail

SERIAL="${1:-}"
ADB="adb"
if [[ -n "$SERIAL" ]]; then
    ADB="adb -s $SERIAL"
fi

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RESULTS_DIR="./sanity_results_${TIMESTAMP}"
LOG_DIR="${RESULTS_DIR}/logs"
RESULTS_FILE="${RESULTS_DIR}/results.csv"

mkdir -p "$LOG_DIR"
echo "test_id,description,status,detail,logfile" > "$RESULTS_FILE"

# ---- EDIT THESE FOR YOUR TARGET ----------------------------------------
# Package names to test in ST_004. Update to match your IVI's actual apps.
APPS=(
    "com.android.car.media:Media"
    "com.android.car.dialer:Phone"
    "com.android.car.settings:Settings"
    "com.google.android.apps.maps:Navigation"   # or your OEM nav app
    "com.android.car.radio:Radio"
)
# VHAL property IDs are OEM/HAL specific. Replace with your platform's
# actual property IDs (see hardware/interfaces/automotive/vehicle or
# your vendor's VHAL definitions). Left blank here as placeholders.
VHAL_PROP_GEAR=""       # e.g. 289408000  (GEAR_SELECTION)
VHAL_PROP_DOOR_LOCK=""  # e.g. 315274270  (DOOR_LOCK)
# -------------------------------------------------------------------------

pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; }
manual() { echo "  [MANUAL/CONFIRM] $1"; }

record() {
    # record <id> <desc> <status> <detail> <logfile>
    echo "\"$1\",\"$2\",\"$3\",\"$4\",\"$5\"" >> "$RESULTS_FILE"
}

capture_logcat_snippet() {
    # capture_logcat_snippet <test_id> <seconds>
    local id="$1" secs="${2:-5}"
    local f="${LOG_DIR}/${id}_logcat.txt"
    timeout "$secs" $ADB logcat -d > "$f" 2>&1
    echo "$f"
}

check_no_crash() {
    # returns 0 if no FATAL/ANR found in file
    local f="$1"
    if grep -qE "FATAL EXCEPTION|ANR in|Process .* has died" "$f"; then
        return 1
    fi
    return 0
}

wait_for_device() {
    echo "Waiting for device..."
    $ADB wait-for-device
}

echo "=================================================================="
echo " IVI Sanity Test Suite - Run started: $TIMESTAMP"
echo " Results directory: $RESULTS_DIR"
echo "=================================================================="

# ---------------------------------------------------------------------
echo -e "\n[ST_001] Flash latest build & power ON - boot check"
$ADB logcat -c
wait_for_device
BOOT_WAIT=0
BOOTED=""
while [[ $BOOT_WAIT -lt 120 ]]; do
    STATE=$($ADB shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [[ "$STATE" == "1" ]]; then
        BOOTED="yes"
        break
    fi
    sleep 2
    BOOT_WAIT=$((BOOT_WAIT+2))
done
LOGF=$(capture_logcat_snippet "ST_001" 3)
if [[ "$BOOTED" == "yes" ]] && check_no_crash "$LOGF"; then
    pass "Device booted in ~${BOOT_WAIT}s with no fatal errors in log"
    record "ST_001" "Boot after flash" "PASS" "boot_time_s=${BOOT_WAIT}" "$LOGF"
else
    fail "Boot did not complete cleanly within timeout"
    record "ST_001" "Boot after flash" "FAIL" "boot_time_s=${BOOT_WAIT}" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_002] Boot animation / launcher timing"
START=$(date +%s)
TOP=""
for i in $(seq 1 60); do
    TOP=$($ADB shell dumpsys activity activities 2>/dev/null | grep -m1 "mResumedActivity" )
    if [[ -n "$TOP" ]]; then break; fi
    sleep 1
done
END=$(date +%s)
ELAPSED=$((END-START))
LOGF="${LOG_DIR}/ST_002_top_activity.txt"
echo "$TOP" > "$LOGF"
if [[ -n "$TOP" ]]; then
    pass "Home/launcher activity detected after ${ELAPSED}s: $TOP"
    record "ST_002" "Boot animation/launcher" "PASS" "elapsed_s=${ELAPSED}" "$LOGF"
else
    fail "No resumed activity detected"
    record "ST_002" "Boot animation/launcher" "FAIL" "elapsed_s=${ELAPSED}" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_003] Touch functionality (semi-auto)"
# Injects a grid of taps + a swipe; tester must visually confirm response.
$ADB shell input tap 100 100
sleep 0.3
$ADB shell input tap 500 500
sleep 0.3
$ADB shell input swipe 100 800 800 800 300
LOGF=$(capture_logcat_snippet "ST_003" 3)
manual "Taps/swipe injected. Confirm on-screen response was correct."
record "ST_003" "Touch functionality" "MANUAL_CONFIRM" "taps+swipe injected" "$LOGF"

# ---------------------------------------------------------------------
echo -e "\n[ST_004] Launch major apps, check for crash/ANR"
for entry in "${APPS[@]}"; do
    PKG="${entry%%:*}"
    NAME="${entry##*:}"
    $ADB logcat -c
    $ADB shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 > /tmp/monkey_out.txt 2>&1
    sleep 3
    LOGF=$(capture_logcat_snippet "ST_004_${NAME}" 3)
    if grep -q "No activities found" /tmp/monkey_out.txt; then
        fail "$NAME ($PKG): package/launcher not found on device"
        record "ST_004_${NAME}" "Launch $NAME" "FAIL" "package not found" "$LOGF"
    elif check_no_crash "$LOGF"; then
        pass "$NAME ($PKG) launched cleanly"
        record "ST_004_${NAME}" "Launch $NAME" "PASS" "no crash/ANR" "$LOGF"
    else
        fail "$NAME ($PKG) crashed or ANR'd"
        record "ST_004_${NAME}" "Launch $NAME" "FAIL" "crash/ANR detected" "$LOGF"
    fi
    $ADB shell input keyevent KEYCODE_HOME
done

# ---------------------------------------------------------------------
echo -e "\n[ST_005] Volume up/down via keyevent + verify stream level"
BEFORE=$($ADB shell dumpsys audio 2>/dev/null | grep -A2 "STREAM_MUSIC" | head -3)
$ADB shell input keyevent KEYCODE_VOLUME_UP
sleep 0.5
$ADB shell input keyevent KEYCODE_VOLUME_UP
sleep 0.5
AFTER=$($ADB shell dumpsys audio 2>/dev/null | grep -A2 "STREAM_MUSIC" | head -3)
LOGF="${LOG_DIR}/ST_005_audio.txt"
{ echo "BEFORE:"; echo "$BEFORE"; echo "AFTER:"; echo "$AFTER"; } > "$LOGF"
if [[ "$BEFORE" != "$AFTER" ]]; then
    pass "STREAM_MUSIC level changed after volume key events"
    record "ST_005" "Volume up/down" "PASS" "stream level changed" "$LOGF"
else
    manual "Could not confirm level change from dumpsys; verify audibly"
    record "ST_005" "Volume up/down" "MANUAL_CONFIRM" "no diff detected in dumpsys" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_006] FM/AM radio tune (manual - vendor HAL specific)"
manual "Radio tuning is vendor-HAL specific; no generic ADB command exists."
manual "Tune manually via UI/steering control and confirm audio plays."
record "ST_006" "Radio tune" "MANUAL" "vendor radio HAL - no generic adb path" ""

# ---------------------------------------------------------------------
echo -e "\n[ST_007] Bluetooth pairing / reconnect state"
LOGF="${LOG_DIR}/ST_007_bt.txt"
$ADB shell dumpsys bluetooth_manager > "$LOGF" 2>&1
if grep -qi "Bonded devices" "$LOGF"; then
    manual "Bonded device list captured. Confirm pairing + auto-reconnect manually."
    record "ST_007" "BT pairing/reconnect" "MANUAL_CONFIRM" "see log for bonded devices" "$LOGF"
else
    fail "Could not read bluetooth_manager state"
    record "ST_007" "BT pairing/reconnect" "FAIL" "dumpsys bluetooth_manager unreadable" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_008] A2DP playback state"
LOGF="${LOG_DIR}/ST_008_a2dp.txt"
$ADB shell dumpsys bluetooth_manager > "$LOGF" 2>&1
if grep -qi "A2DP" "$LOGF"; then
    manual "A2DP profile info captured. Confirm audio quality manually."
    record "ST_008" "A2DP playback" "MANUAL_CONFIRM" "A2DP profile present in dumpsys" "$LOGF"
else
    manual "A2DP not found in dumpsys - confirm connection manually first"
    record "ST_008" "A2DP playback" "MANUAL" "A2DP not detected" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_009] Bluetooth call audio (manual)"
LOGF="${LOG_DIR}/ST_009_telecom.txt"
$ADB shell dumpsys telecom > "$LOGF" 2>&1
manual "Place/receive a test BT call now; confirm audio routes to car speakers/mic."
record "ST_009" "BT call audio" "MANUAL" "telecom dumpsys captured for reference" "$LOGF"

# ---------------------------------------------------------------------
echo -e "\n[ST_010] USB media detection"
LOGF="${LOG_DIR}/ST_010_usb.txt"
$ADB shell dumpsys mount > "$LOGF" 2>&1
$ADB shell sm list-volumes >> "$LOGF" 2>&1
if grep -qiE "usb|public" "$LOGF"; then
    pass "USB/public volume detected"
    record "ST_010" "USB detection" "PASS" "volume found - confirm playback manually" "$LOGF"
else
    fail "No USB volume detected - is drive inserted?"
    record "ST_010" "USB detection" "FAIL" "no usb volume found" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_011] Wi-Fi connectivity"
LOGF="${LOG_DIR}/ST_011_wifi.txt"
$ADB shell dumpsys wifi > "$LOGF" 2>&1
if grep -qi "mNetworkInfo.*CONNECTED\|Wifi is connected" "$LOGF"; then
    pass "Wi-Fi reports CONNECTED"
    record "ST_011" "Wi-Fi connect" "PASS" "connected state found" "$LOGF"
else
    fail "Wi-Fi not connected"
    record "ST_011" "Wi-Fi connect" "FAIL" "connected state not found" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_012] GPS / Navigation location fix"
LOGF="${LOG_DIR}/ST_012_location.txt"
$ADB shell dumpsys location > "$LOGF" 2>&1
if grep -qiE "last location|fused" "$LOGF"; then
    pass "Location provider data present"
    record "ST_012" "GPS/Navigation" "PASS" "location data present - confirm map visually" "$LOGF"
else
    fail "No location fix data found"
    record "ST_012" "GPS/Navigation" "FAIL" "no location data" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_013] Vehicle info via car service (AAOS)"
LOGF="${LOG_DIR}/ST_013_car_service.txt"
$ADB shell dumpsys car_service > "$LOGF" 2>&1
if [[ -s "$LOGF" ]]; then
    manual "car_service dump captured. Cross-check speed/gear/fuel/door values against expected."
    record "ST_013" "Vehicle info (CAN)" "MANUAL_CONFIRM" "see car_service dump" "$LOGF"
else
    fail "car_service dumpsys empty/unavailable - is this AAOS?"
    record "ST_013" "Vehicle info (CAN)" "FAIL" "car_service unavailable" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_014] HVAC control"
LOGF="${LOG_DIR}/ST_014_hvac.txt"
$ADB shell dumpsys car_service --services CarPropertyService > "$LOGF" 2>&1
manual "Adjust temperature/fan/AC via UI, then compare HVAC property values in log."
record "ST_014" "HVAC control" "MANUAL_CONFIRM" "see car_service HVAC property dump" "$LOGF"

# ---------------------------------------------------------------------
echo -e "\n[ST_015] Reverse gear -> rear camera"
LOGF="${LOG_DIR}/ST_015_reverse.txt"
if [[ -n "$VHAL_PROP_GEAR" ]]; then
    $ADB shell cmd car_service inject-vhal-event "$VHAL_PROP_GEAR" "REVERSE_VALUE_HERE" > "$LOGF" 2>&1
    sleep 2
    $ADB shell dumpsys activity activities | grep -i camera >> "$LOGF"
    manual "Reverse gear injected via VHAL. Confirm rear camera view is live/clear."
    record "ST_015" "Reverse camera" "MANUAL_CONFIRM" "gear injection attempted" "$LOGF"
else
    manual "VHAL_PROP_GEAR not configured. Engage reverse physically and confirm camera."
    record "ST_015" "Reverse camera" "MANUAL" "VHAL prop id not set in script" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_016] Steering wheel buttons (volume/track/voice)"
LOGF=$(capture_logcat_snippet "ST_016" 2)
manual "Press steering wheel Volume/Track/Voice buttons now; confirm each triggers expected action."
record "ST_016" "Steering wheel buttons" "MANUAL" "physical button press required" "$LOGF"

# ---------------------------------------------------------------------
echo -e "\n[ST_017] Lock/unlock vehicle"
LOGF="${LOG_DIR}/ST_017_lock.txt"
if [[ -n "$VHAL_PROP_DOOR_LOCK" ]]; then
    $ADB shell cmd car_service inject-vhal-event "$VHAL_PROP_DOOR_LOCK" "LOCK_VALUE_HERE" > "$LOGF" 2>&1
    manual "Door lock event injected. Confirm IVI status icon updates correctly."
    record "ST_017" "Lock/unlock" "MANUAL_CONFIRM" "lock event injected" "$LOGF"
else
    manual "VHAL_PROP_DOOR_LOCK not configured. Lock/unlock physically and confirm IVI status."
    record "ST_017" "Lock/unlock" "MANUAL" "VHAL prop id not set in script" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_018] Ignition OFF -> sleep -> ON resume"
$ADB shell input keyevent KEYCODE_SLEEP
sleep 5
$ADB shell input keyevent KEYCODE_WAKEUP
sleep 3
LOGF=$(capture_logcat_snippet "ST_018" 5)
if check_no_crash "$LOGF"; then
    pass "Resumed from sleep with no fatal errors logged"
    record "ST_018" "Ignition off/on resume" "PASS" "no crash on resume" "$LOGF"
else
    fail "Crash/ANR detected on resume"
    record "ST_018" "Ignition off/on resume" "FAIL" "crash detected" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_019] Settings persistence (brightness/language/sound)"
LOGF="${LOG_DIR}/ST_019_settings.txt"
ORIG_BRIGHTNESS=$($ADB shell settings get system screen_brightness | tr -d '\r')
$ADB shell settings put system screen_brightness 150
$ADB shell input keyevent KEYCODE_SLEEP
sleep 3
$ADB shell input keyevent KEYCODE_WAKEUP
sleep 2
NEW_BRIGHTNESS=$($ADB shell settings get system screen_brightness | tr -d '\r')
{ echo "orig=$ORIG_BRIGHTNESS"; echo "after_cycle=$NEW_BRIGHTNESS"; } > "$LOGF"
if [[ "$NEW_BRIGHTNESS" == "150" ]]; then
    pass "Brightness setting persisted (150) after sleep/wake cycle"
    record "ST_019" "Settings persistence" "PASS" "brightness persisted" "$LOGF"
else
    fail "Brightness setting did not persist as expected"
    record "ST_019" "Settings persistence" "FAIL" "expected 150 got $NEW_BRIGHTNESS" "$LOGF"
fi
$ADB shell settings put system screen_brightness "$ORIG_BRIGHTNESS" 2>/dev/null

# ---------------------------------------------------------------------
echo -e "\n[ST_020] System stability monitor (short window)"
LOGF="${LOG_DIR}/ST_020_stability.txt"
$ADB logcat -c
MONITOR_SECS=60
echo "Monitoring logcat for ${MONITOR_SECS}s for crashes/ANRs/reboots..."
timeout "$MONITOR_SECS" $ADB logcat > "$LOGF" 2>&1
if check_no_crash "$LOGF"; then
    pass "No crashes/ANRs during ${MONITOR_SECS}s monitor window"
    record "ST_020" "Stability monitor" "PASS" "clean ${MONITOR_SECS}s window" "$LOGF"
else
    fail "Crash/ANR detected during monitor window"
    record "ST_020" "Stability monitor" "FAIL" "see log" "$LOGF"
fi

# ---------------------------------------------------------------------
echo -e "\n[ST_021] Full logcat review for critical errors"
LOGF="${LOG_DIR}/ST_021_full_logcat.txt"
$ADB logcat -d > "$LOGF" 2>&1
CRIT_COUNT=$(grep -cE "FATAL EXCEPTION|ANR in|Process .* has died" "$LOGF")
if [[ "$CRIT_COUNT" -eq 0 ]]; then
    pass "No FATAL/ANR/process-death entries found in full logcat"
    record "ST_021" "Full log review" "PASS" "0 critical entries" "$LOGF"
else
    fail "$CRIT_COUNT critical entries found - see $LOGF"
    record "ST_021" "Full log review" "FAIL" "${CRIT_COUNT} critical entries" "$LOGF"
fi

echo -e "\n=================================================================="
echo " Sanity run complete. Results: $RESULTS_FILE"
echo " Logs: $LOG_DIR"
echo "=================================================================="
echo "Next: python3 generate_report.py \"$RESULTS_DIR\""
