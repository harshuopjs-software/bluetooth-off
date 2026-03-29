#!/usr/bin/env python3

import subprocess
import time
import sys
import os
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
        else:
            logger.warning(f"Bluetooth power on may have failed: {result.stdout}")
            return False
    except subprocess.SubprocessError as e:
        logger.error(f"Failed to power on Bluetooth: {e}")
        return False


def get_rssi_dbus(mac_address, logger):
    """
    Read RSSI from BlueZ DBus API.
    The RSSI property on org.bluez.Device1 is populated during discovery.
    We trigger a short scan, then read the property.
    """
    try:
        bus = dbus.SystemBus()
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )

        device_path = "/org/bluez/hci0/dev_" + mac_address.replace(":", "_")

        try:
            device_obj = bus.get_object("org.bluez", device_path)
            props = dbus.Interface(device_obj, "org.freedesktop.DBus.Properties")

            try:
                rssi = props.Get("org.bluez.Device1", "RSSI")
                return int(rssi)
            except dbus.exceptions.DBusException:
                logger.debug("RSSI property not available (device not in discovery)")
                return None

        except dbus.exceptions.DBusException:
            logger.debug(f"Device {mac_address} not found in BlueZ object tree")
            return None

    except Exception as e:
        logger.debug(f"DBus RSSI read failed: {e}")
        return None


def trigger_discovery(logger):
    """
    Start a short Bluetooth discovery scan to refresh RSSI values.
    bluetoothctl scan on for a few seconds, then off.
    """
    try:
        scan_proc = subprocess.Popen(
            ["bluetoothctl", "scan", "on"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        subprocess.run(
            ["bluetoothctl", "scan", "off"],
            timeout=5,
            capture_output=True,
        )
        scan_proc.terminate()
        scan_proc.wait(timeout=3)
    except Exception as e:
        logger.debug(f"Discovery scan error: {e}")


def is_phone_connected(mac_address, logger):
    """
    Check if the phone is currently connected via bluetoothctl.
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            timeout=5,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "Connected: yes" in result.stdout:
            return True
        return False
    except subprocess.SubprocessError:
        return False


def is_phone_reachable(mac_address, logger):
    """
    Fallback: Bluetooth L2CAP ping to check if phone is in range.
    """
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


def enter_suspend_cycle(mac_address, threshold, wake_interval, running_ref, logger):
    """
    Suspend-wake loop: sleep, wake briefly to check phone, repeat.
    """
    while running_ref[0]:
        if not schedule_rtc_wake(wake_interval, logger):
            logger.error("Cannot set RTC alarm; aborting suspend")
            break

        suspend_system(logger)
        logger.info("Woke up — checking for phone...")
        time.sleep(3)
        ensure_bluetooth_on(logger)
        time.sleep(2)

        trigger_discovery(logger)
        wake_rssi = get_rssi_dbus(mac_address, logger)

        if wake_rssi is not None and wake_rssi >= threshold:
            logger.info(f"Phone is back! RSSI: {wake_rssi} dBm — staying awake")
            return True

        if is_phone_connected(mac_address, logger):
            logger.info("Phone is connected — staying awake")
            return True

        if is_phone_reachable(mac_address, logger):
            logger.info("Phone detected via Bluetooth ping — staying awake")
            return True

        logger.info("Phone still away — going back to sleep...")

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
    running = [True]
    scan_counter = 0

    def handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name} — shutting down...")
        running[0] = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running[0]:
        # Trigger a discovery scan every 3 cycles to refresh RSSI
        scan_counter += 1
        if scan_counter % 3 == 1:
            trigger_discovery(logger)

        rssi = get_rssi_dbus(mac, logger)

        if rssi is not None:
            logger.info(f"Phone RSSI: {rssi} dBm (threshold: {threshold} dBm)")

            if rssi < threshold:
                away_count += 1
                logger.warning(f"Phone is far! Away count: {away_count}/{away_trigger}")

                if away_count >= away_trigger:
                    logger.warning("Phone out of range — entering suspend cycle")
                    if enter_suspend_cycle(mac, threshold, wake_interval, running, logger):
                        away_count = 0
            else:
                away_count = 0
        else:
            # RSSI not available — try connection check and l2ping
            if is_phone_connected(mac, logger):
                logger.info("Phone is connected (RSSI unavailable)")
                away_count = 0
            elif is_phone_reachable(mac, logger):
                logger.info("Phone reachable via Bluetooth ping (RSSI unavailable)")
                away_count = 0
            else:
                away_count += 1
                logger.warning(f"Phone unreachable! Away count: {away_count}/{away_trigger}")

                if away_count >= away_trigger:
                    logger.warning("Phone disappeared — entering suspend cycle")
                    if enter_suspend_cycle(mac, threshold, wake_interval, running, logger):
                        away_count = 0

        time.sleep(interval)

    logger.info("Proximity monitor stopped.")


if __name__ == "__main__":
    main()
