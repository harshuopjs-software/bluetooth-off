#!/usr/bin/env python3

import subprocess
import time
import sys
import re
import logging
import signal
import configparser
from pathlib import Path
import os

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
        sys.exit(1)
    config.read(CONFIG_PATH)
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
            return True
    except subprocess.SubprocessError:
        pass
    return False


def get_hcitool_path():
    if os.path.exists("/opt/bt-proximity/hcitool"):
        return "/opt/bt-proximity/hcitool"
    return "hcitool"


def get_rssi(mac_address, logger):
    """
    Get the RSSI (signal strength) of a paired Bluetooth device using hcitool.
    This works perfectly for already connected devices!
    """
    hcitool = get_hcitool_path()
    try:
        subprocess.run([hcitool, "cc", mac_address], timeout=10, capture_output=True)
        result = subprocess.run([hcitool, "rssi", mac_address], timeout=5, capture_output=True, text=True)
        
        if result.returncode == 0 and "RSSI return value" in result.stdout:
            rssi_str = result.stdout.strip().split(":")[1].strip()
            return int(rssi_str)
            
        logger.debug(f"hcitool rssi returned: {result.stdout.strip()}")
        return None
    except Exception as e:
        logger.debug(f"RSSI read failed: {e}")
        return None


def l2ping_check(mac_address, logger):
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
    except Exception:
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
        subprocess.run(["systemctl", "suspend", "-i"], check=True, timeout=10)
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to suspend: {e}")


def phone_is_nearby(mac_address, threshold, logger):
    """
    Using the compiled hcitool to reliably get continuous RSSI for the connection!
    """
    rssi = get_rssi(mac_address, logger)
    if rssi is not None:
        if rssi >= threshold:
            return True, f"RSSI={rssi} dBm (>= {threshold})"
        else:
            return False, f"RSSI={rssi} dBm (< {threshold})"

    # Fallback to l2ping if RSSI is totally unavailable (e.g. connection dropped)
    reachable, latency = l2ping_check(mac_address, logger)
    if reachable:
        # If reachable but no RSSI, we assume it's just out of ~1m range since RSSI drops off.
        # However, to be strict about 1m, if RSSI drops out, we can optionally suspend!
        # But l2ping means it's still < 10m. We will log it as away if we STRICTLY want 1m.
        # But normally l2ping OK + None RSSI means it's connected but dormant.
        # We will count it as OK but warn.
        # Since the user wants STRICT 1m: we only trust RSSI! 
        return False, f"l2ping OK, but no RSSI (Phone might be slightly too far)"
    else:
        return False, "Unreachable"


def enter_suspend_cycle(mac, threshold, wake_interval, running_ref, logger):
    while running_ref[0]:
        if not schedule_rtc_wake(wake_interval, logger):
            logger.error("Cannot set RTC alarm; aborting suspend")
            break

        suspend_system(logger)
        logger.info("Woke up — checking for phone...")
        time.sleep(3)
        ensure_bluetooth_on(logger)
        time.sleep(2)

        nearby, detail = phone_is_nearby(mac, threshold, logger)
        if nearby:
            logger.info(f"Phone is back! {detail} — staying awake")
            return True
        else:
            logger.info(f"Phone still away: {detail} — going back to sleep...")

    return False


def main():
    logger = setup_logging()
    config = load_config(logger)
    mac = config.get("phone_mac", DEFAULT_CONFIG["phone_mac"])
    threshold = int(config.get("rssi_threshold", DEFAULT_CONFIG["rssi_threshold"]))
    interval = int(config.get("check_interval", DEFAULT_CONFIG["check_interval"]))
    away_trigger = int(config.get("away_count_trigger", DEFAULT_CONFIG["away_count_trigger"]))
    wake_interval = int(config.get("wake_check_interval", DEFAULT_CONFIG["wake_check_interval"]))
    log_level = config.get("log_level", DEFAULT_CONFIG["log_level"])

    logger = setup_logging(log_level)
    ensure_bluetooth_on(logger)

    away_count = 0
    running = [True]

    def handle_signal(signum, frame):
        logger.info(f"Shutting down...")
        running[0] = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running[0]:
        nearby, detail = phone_is_nearby(mac, threshold, logger)

        if nearby:
            logger.info(f"Phone nearby: {detail}")
            away_count = 0
        else:
            away_count += 1
            logger.warning(f"Phone away: {detail} [{away_count}/{away_trigger}]")

            if away_count >= away_trigger:
                if enter_suspend_cycle(mac, threshold, wake_interval, running, logger):
                    away_count = 0

        time.sleep(interval)

if __name__ == "__main__":
    main()
