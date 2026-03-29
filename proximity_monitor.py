#!/usr/bin/env python3

import subprocess
import time
import sys
import os
import logging
import signal
import configparser
from pathlib import Path

CONFIG_PATH = Path("/etc/bt-proximity/config.ini")
LOG_DIR = Path("/var/log/bt-proximity")

DEFAULT_CONFIG = {
    "phone_mac": "XX:XX:XX:XX:XX:XX",
    "rssi_threshold": "-6",
    "check_interval": "5",
    "away_count_trigger": "3",
    "wake_check_interval": "30",
    "log_level": "INFO",
}


def setup_logging(level_str="INFO"):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "monitor.log"
    level = getattr(logging, level_str.upper(), logging.INFO)
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
        logger.warning(f"Run: sudo nano {CONFIG_PATH}")
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
        else:
            logger.warning(f"Bluetooth power on may have failed: {result.stdout}")
            return False
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to power on Bluetooth: {e}")
        return False


def get_rssi(mac_address, logger):
    try:
        subprocess.run(
            ["hcitool", "cc", mac_address],
            timeout=10,
            capture_output=True,
        )
        result = subprocess.run(
            ["hcitool", "rssi", mac_address],
            timeout=5,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "RSSI return value" in result.stdout:
            rssi_str = result.stdout.strip().split(":")[1].strip()
            rssi = int(rssi_str)
            return rssi
        logger.debug(f"hcitool rssi returned: {result.stdout.strip()}")
        return None
    except subprocess.TimeoutExpired:
        logger.debug("RSSI read timed out")
        return None
    except (subprocess.SubprocessError, ValueError, IndexError) as e:
        logger.debug(f"RSSI read failed: {e}")
        return None


def is_phone_reachable(mac_address, logger):
    try:
        result = subprocess.run(
            ["l2ping", "-c", "1", "-t", "5", mac_address],
            timeout=10,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


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


def main():
    logger = setup_logging()
    logger.info("Bluetooth Proximity Monitor — Starting Up")

    config = load_config(logger)
    mac = config.get("phone_mac", DEFAULT_CONFIG["phone_mac"])
    threshold = int(config.get("rssi_threshold", DEFAULT_CONFIG["rssi_threshold"]))
    interval = int(config.get("check_interval", DEFAULT_CONFIG["check_interval"]))
    away_trigger = int(config.get("away_count_trigger", DEFAULT_CONFIG["away_count_trigger"]))
    wake_interval = int(config.get("wake_check_interval", DEFAULT_CONFIG["wake_check_interval"]))
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

    ensure_bluetooth_on(logger)

    away_count = 0
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name} — shutting down...")
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running:
        rssi = get_rssi(mac, logger)

        if rssi is not None:
            logger.info(f"Phone RSSI: {rssi} dBm (threshold: {threshold} dBm)")

            if rssi < threshold:
                away_count += 1
                logger.warning(f"Phone is far! Away count: {away_count}/{away_trigger}")

                if away_count >= away_trigger:
                    logger.warning("Phone out of range — entering suspend cycle")

                    while running:
                        if not schedule_rtc_wake(wake_interval, logger):
                            logger.error("Cannot set RTC alarm; aborting suspend")
                            break

                        suspend_system(logger)
                        logger.info("Woke up — checking for phone...")
                        time.sleep(3)
                        ensure_bluetooth_on(logger)
                        time.sleep(1)

                        wake_rssi = get_rssi(mac, logger)
                        if wake_rssi is not None and wake_rssi >= threshold:
                            logger.info(f"Phone is back! RSSI: {wake_rssi} dBm — staying awake")
                            away_count = 0
                            break
                        elif is_phone_reachable(mac, logger):
                            logger.info("Phone detected via Bluetooth ping — staying awake")
                            away_count = 0
                            break
                        else:
                            logger.info("Phone still away — going back to sleep...")
                            continue
            else:
                away_count = 0
        else:
            if is_phone_reachable(mac, logger):
                logger.info("Phone reachable (via Bluetooth ping) but RSSI unavailable")
                away_count = 0
            else:
                away_count += 1
                logger.warning(f"Phone unreachable! Away count: {away_count}/{away_trigger}")

                if away_count >= away_trigger:
                    logger.warning("Phone disappeared — entering suspend cycle")

                    while running:
                        if not schedule_rtc_wake(wake_interval, logger):
                            logger.error("Cannot set RTC alarm; aborting suspend")
                            break

                        suspend_system(logger)
                        logger.info("Woke up — checking for phone...")
                        time.sleep(3)
                        ensure_bluetooth_on(logger)
                        time.sleep(1)

                        if is_phone_reachable(mac, logger):
                            logger.info("Phone is back — staying awake")
                            away_count = 0
                            break
                        else:
                            logger.info("Phone still gone — going back to sleep...")
                            continue

        time.sleep(interval)

    logger.info("Proximity monitor stopped.")


if __name__ == "__main__":
    main()
