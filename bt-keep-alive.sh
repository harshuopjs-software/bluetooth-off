#!/bin/bash

case "$1" in
    pre)
        hciconfig hci0 up 2>/dev/null

        for dev in /sys/class/bluetooth/hci*/device/power/wakeup; do
            [ -f "$dev" ] && echo enabled > "$dev" 2>/dev/null
        done

        for dev in /sys/bus/usb/devices/*/power/wakeup; do
            if [ -f "$(dirname "$dev")/product" ]; then
                product=$(cat "$(dirname "$dev")/product" 2>/dev/null)
                echo "$product" | grep -qi "bluetooth" && echo enabled > "$dev" 2>/dev/null
            fi
        done
        ;;

    post)
        sleep 1
        hciconfig hci0 up 2>/dev/null
        sleep 1
        bluetoothctl power on 2>/dev/null
        ;;
esac
