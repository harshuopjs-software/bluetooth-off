#!/usr/bin/env python3

import subprocess
import time
import sys
import re
import logging
import signal
import configparser
import dbus
from pathlib import Path

CONFIG_PATH = Path("/etc/bt-proximity/config.ini")
LOG_DIR = Path("/var/log/bt-proximity")

DEFAULT_CONFIG = {
    "phone_mac": "XX:XX:XX:XX:XX:XX",
    "rssi_threshold": "-6",
    "check_interval": "5",
    "away_count_trigger": "3",
    "wake_check_interval": "30",
    "scan_interval": "3",
    "log_level": "INFO",
}


def setup_logging(level_str="INFO"):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "monitor.log"
    level = getattr(logging, level_str.upper(), logging.INFO)
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )
    return logging.getLogger("bt-proximity")


def load_config(logger):
    config = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config["proximity"] = DEFAULT_CONFIG
        with open(CONFIG_PATH, "w") as f:
            config.write(f)
        logger.info(f"Created default config at {CONFIG_PATH}")
        logger.warning("Please edit the config and set your phone's MAC address!")
        sys.exit(1)
    config.read(CONFIG_PATH)
    if "proximity" not in config:
        logger.error(f"Config file {CONFIG_PATH} is missing [proximity] section!")
        sys.exit(1)
    return config["proximity"]


def ensure_bluetooth_on(logger):
    try:
        result = subprocess.run(
            ["bluetoothctl", "power", "on"],
            timeout=10,
            capture_output=True,
            text=True,
        )
        if "succeeded" in result.stdout.lower() or result.returncode == 0:
            logger.debug("Bluetooth adapter is ON")
            return True
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to power on Bluetooth: {e}")
    return False


def get_rssi_from_scan(mac_address, scan_duration, logger):
    """
    Run a timed bluetoothctl scan and parse RSSI for the target device.
    This is the only reliable way to get RSSI in modern BlueZ.
    """
    try:
        proc = subprocess.Popen(
            ["bluetoothctl", "scan", "on"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(scan_duration)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

        # After the scan, the RSSI property should be updated in DBus
        try:
            bus = dbus.SystemBus()
            device_path = "/org/bluez/hci0/dev_" + mac_address.replace(":", "_")
            manager = dbus.Interface(
                bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager",
            )
            objects = manager.GetManagedObjects()
            if device_path in objects:
                dev_props = objects[device_path].get("org.bluez.Device1", {})
                rssi = dev_props.get("RSSI", None)
                if rssi is not None:
                    return int(rssi)
        except Exception as e:
            logger.debug(f"DBus RSSI read failed: {e}")

        # Fallback: stop the scan cleanly
        subprocess.run(
            ["bluetoothctl", "scan", "off"],
            timeout=3,
            capture_output=True,
        )
        return None

    except Exception as e:
        logger.debug(f"Scan-based RSSI failed: {e}")
        return None


def l2ping_check(mac_address, logger):
    """
    Bluetooth ping — returns (reachable, avg_latency_ms).
    This is the most reliable proximity check.
    When phone moves out of BT range, this fails entirely.
    """
    try:
        result = subprocess.run(
            ["l2ping", "-c", "2", "-t", "3", mac_address],
            timeout=12,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            latencies = re.findall(r"time\s+([\d.]+)ms", result.stdout)
            if latencies:
                avg = sum(float(x) for x in latencies) / len(latencies)
                return True, avg
            return True, 0.0
        return False, 0.0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False, 0.0


def schedule_rtc_wake(seconds, logger):
    try:
        subprocess.run(
            ["rtcwake", "-m", "no", "-s", str(seconds)],
            timeout=5,
            check=True,
            capture_output=True,
        )
        logger.debug(f"RTC wake alarm set: will wake in {seconds}s")
        return True
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to set RTC wake alarm: {e}")
        return False


def suspend_system(logger):
    logger.info("Suspending system...")
    try:
        subprocess.run(["systemctl", "suspend"], check=True, timeout=10)
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to suspend: {e}")


def phone_is_nearby(mac_address, threshold, scan_duration, logger):
    """
    Combined check: try RSSI first, then fall back to l2ping reachability.
    Returns (nearby: bool, detail: str)
    """
    # Method 1: Try to get RSSI via scan
    rssi = get_rssi_from_scan(mac_address, scan_duration, logger)
    if rssi is not None:
        if rssi >= threshold:
            return True, f"RSSI={rssi} dBm (>= {threshold})"
        else:
            return False, f"RSSI={rssi} dBm (< {threshold})"

    # Method 2: l2ping reachability
    reachable, latency = l2ping_check(mac_address, logger)
    if reachable:
        return True, f"l2ping OK (latency={latency:.1f}ms, no RSSI)"
    else:
        return False, "l2ping FAILED — phone unreachable"


def enter_suspend_cycle(mac, threshold, wake_interval, scan_dur, running_ref, logger):
    while running_ref[0]:
        if not schedule_rtc_wake(wake_interval, logger):
            logger.error("Cannot set RTC alarm; aborting suspend")
            break

        suspend_system(logger)
        logger.info("Woke up — checking for phone...")
        time.sleep(3)
        ensure_bluetooth_on(logger)
        time.sleep(2)

        nearby, detail = phone_is_nearby(mac, threshold, scan_dur, logger)
        if nearby:
            logger.info(f"Phone is back! {detail} — staying awake")
            return True
        else:
            logger.info(f"Phone still away: {detail} — going back to sleep...")

    return False


def main():
    logger = setup_logging()
    logger.info("Bluetooth Proximity Monitor — Starting Up")

    config = load_config(logger)
    mac = config.get("phone_mac", DEFAULT_CONFIG["phone_mac"])
    threshold = int(config.get("rssi_threshold", DEFAULT_CONFIG["rssi_threshold"]))
    interval = int(config.get("check_interval", DEFAULT_CONFIG["check_interval"]))
    away_trigger = int(config.get("away_count_trigger", DEFAULT_CONFIG["away_count_trigger"]))
    wake_interval = int(config.get("wake_check_interval", DEFAULT_CONFIG["wake_check_interval"]))
    scan_dur = int(config.get("scan_interval", DEFAULT_CONFIG["scan_interval"]))
    log_level = config.get("log_level", DEFAULT_CONFIG["log_level"])

    logger = setup_logging(log_level)

    if mac == "XX:XX:XX:XX:XX:XX":
        logger.error("Phone MAC address not configured!")
        logger.error(f"Edit {CONFIG_PATH} and set your phone's MAC.")
        sys.exit(1)

    logger.info(f"Phone MAC:        {mac}")
    logger.info(f"RSSI Threshold:   {threshold} dBm")
    logger.info(f"Check Interval:   {interval}s")
    logger.info(f"Away Trigger:     {away_trigger} consecutive readings")
    logger.info(f"Wake Interval:    {wake_interval}s")
    logger.info(f"Scan Duration:    {scan_dur}s")

    ensure_bluetooth_on(logger)

    away_count = 0
    running = [True]

    def handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name} — shutting down...")
        running[0] = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running[0]:
        nearby, detail = phone_is_nearby(mac, threshold, scan_dur, logger)

        if nearby:
            logger.info(f"Phone nearby: {detail}")
            away_count = 0
        else:
            away_count += 1
            logger.warning(f"Phone away: {detail} [{away_count}/{away_trigger}]")

            if away_count >= away_trigger:
                logger.warning("Phone out of range — entering suspend cycle")
                if enter_suspend_cycle(mac, threshold, wake_interval, scan_dur, running, logger):
                    away_count = 0

        time.sleep(interval)

    logger.info("Proximity monitor stopped.")


if __name__ == "__main__":
    main()
