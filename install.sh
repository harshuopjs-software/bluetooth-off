#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}    $1"; }
success() { echo -e "${GREEN}[✓]${NC}       $1"; }
warn()    { echo -e "${YELLOW}[!]${NC}       $1"; }
error()   { echo -e "${RED}[✗]${NC}       $1"; }
header()  { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}  $1${NC}"; echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

if [ "$EUID" -ne 0 ]; then
    error "This script must be run as root (sudo)."
    echo "  Usage: sudo ./install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo $USER)}"
REAL_HOME=$(eval echo "~$REAL_USER")

header "Bluetooth Proximity System — Installer"
info "Script directory: $SCRIPT_DIR"
info "Installing for user: $REAL_USER"
info "User home: $REAL_HOME"
echo ""

header "Step 1: Installing Packages"

info "Updating package database..."
pacman -Sy --noconfirm 2>/dev/null

info "Installing Bluetooth packages..."
pacman -S --noconfirm --needed bluez bluez-utils 2>/dev/null
success "Bluetooth packages installed"

info "Installing KDE Connect..."
pacman -S --noconfirm --needed kdeconnect 2>/dev/null
success "KDE Connect installed"

info "Installing optional utilities..."
pacman -S --noconfirm --needed sshfs xdg-desktop-portal 2>/dev/null || true
success "Optional packages installed"

header "Step 2: Enabling Bluetooth"

systemctl enable --now bluetooth.service
success "Bluetooth service enabled and started"

header "Step 3: Installing Proximity Monitor"

INSTALL_DIR="/opt/bt-proximity"
mkdir -p "$INSTALL_DIR"

cp "$SCRIPT_DIR/proximity_monitor.py" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/proximity_monitor.py"
success "proximity_monitor.py → $INSTALL_DIR/"

if [ ! -f /etc/bt-proximity/config.ini ]; then
    mkdir -p /etc/bt-proximity
    cp "$SCRIPT_DIR/config.ini" /etc/bt-proximity/config.ini
    success "config.ini → /etc/bt-proximity/config.ini (EDIT THIS!)"
else
    warn "Config already exists at /etc/bt-proximity/config.ini — skipping"
fi

mkdir -p /var/log/bt-proximity
success "Log directory created: /var/log/bt-proximity/"

header "Step 4: Installing systemd Services"

cp "$SCRIPT_DIR/bt-proximity.service" /etc/systemd/system/
success "bt-proximity.service → /etc/systemd/system/"

USER_SERVICE_DIR="$REAL_HOME/.config/systemd/user"
mkdir -p "$USER_SERVICE_DIR"
cp "$SCRIPT_DIR/kdeconnect-user.service" "$USER_SERVICE_DIR/"
chown -R "$REAL_USER:$REAL_USER" "$USER_SERVICE_DIR"
success "kdeconnect-user.service → $USER_SERVICE_DIR/"

systemctl daemon-reload
success "systemd daemon reloaded"

header "Step 5: Installing udev Rules"

cp "$SCRIPT_DIR/90-bluetooth-wake.rules" /etc/udev/rules.d/
success "90-bluetooth-wake.rules → /etc/udev/rules.d/"

udevadm control --reload-rules
udevadm trigger
success "udev rules reloaded"

header "Step 6: Installing Sleep Hook"

cp "$SCRIPT_DIR/bt-keep-alive.sh" /usr/lib/systemd/system-sleep/
chmod +x /usr/lib/systemd/system-sleep/bt-keep-alive.sh
success "bt-keep-alive.sh → /usr/lib/systemd/system-sleep/"

header "Step 7: Detecting Bluetooth Hardware"

BT_INFO=$(lsusb 2>/dev/null | grep -i bluetooth || echo "Not found via USB")
info "Bluetooth adapter detected:"
echo "  $BT_INFO"
echo ""

if echo "$BT_INFO" | grep -q "Not found"; then
    warn "No USB Bluetooth adapter found. You may have a PCIe/SDIO adapter."
    warn "The udev rules may need adjustment. Check: lspci | grep -i bluetooth"
else
    VENDOR_ID=$(echo "$BT_INFO" | head -1 | grep -oP 'ID \K[0-9a-f]{4}')
    PRODUCT_ID=$(echo "$BT_INFO" | head -1 | grep -oP 'ID [0-9a-f]{4}:\K[0-9a-f]{4}')
    if [ -n "$VENDOR_ID" ] && [ -n "$PRODUCT_ID" ]; then
        info "Your adapter IDs:  Vendor=$VENDOR_ID  Product=$PRODUCT_ID"
        info "Make sure the udev rules use these IDs!"
        info "Edit: /etc/udev/rules.d/90-bluetooth-wake.rules"
    fi
fi

header "Step 8: Checking Paired Devices"

PAIRED=$(bluetoothctl devices Paired 2>/dev/null || bluetoothctl devices 2>/dev/null)
if [ -n "$PAIRED" ]; then
    info "Your paired Bluetooth devices:"
    echo "$PAIRED" | while read -r line; do
        echo "    $line"
    done
    echo ""
    info "Copy your phone's MAC address to /etc/bt-proximity/config.ini"
else
    warn "No paired devices found!"
    warn "Pair your phone first: bluetoothctl → scan on → pair <MAC>"
fi

header "Installation Complete!"

echo -e "${GREEN}All components have been installed successfully.${NC}"
echo ""
echo -e "${YELLOW}━━━ REQUIRED NEXT STEPS ━━━${NC}"
echo ""
echo "  1. Edit the config file with your phone's MAC address:"
echo -e "     ${CYAN}sudo nano /etc/bt-proximity/config.ini${NC}"
echo ""
echo "  2. (If needed) Update the udev rules with your Bluetooth adapter IDs:"
echo -e "     ${CYAN}sudo nano /etc/udev/rules.d/90-bluetooth-wake.rules${NC}"
echo ""
echo "  3. Enable and start the proximity monitor:"
echo -e "     ${CYAN}sudo systemctl enable --now bt-proximity.service${NC}"
echo ""
echo "  4. Enable KDE Connect (if not using KDE Plasma desktop):"
echo -e "     ${CYAN}systemctl --user enable --now kdeconnect-user.service${NC}"
echo ""
echo "  5. Open firewall ports for KDE Connect (if firewall is active):"
echo -e "     ${CYAN}sudo ufw allow 1714:1764/tcp${NC}"
echo -e "     ${CYAN}sudo ufw allow 1714:1764/udp${NC}"
echo ""
echo -e "${YELLOW}━━━ USEFUL COMMANDS ━━━${NC}"
echo ""
echo "  Check proximity monitor status:"
echo -e "     ${CYAN}sudo systemctl status bt-proximity.service${NC}"
echo ""
echo "  View proximity monitor logs:"
echo -e "     ${CYAN}journalctl -u bt-proximity.service -f${NC}"
echo -e "     ${CYAN}cat /var/log/bt-proximity/monitor.log${NC}"
echo ""
echo "  List paired Bluetooth devices:"
echo -e "     ${CYAN}bluetoothctl devices${NC}"
echo ""
echo "  Test RSSI manually:"
echo -e "     ${CYAN}hcitool cc <PHONE_MAC> && hcitool rssi <PHONE_MAC>${NC}"
echo ""
echo "  Pair your phone via KDE Connect:"
echo -e "     ${CYAN}kdeconnect-cli --list-available${NC}"
echo -e "     ${CYAN}kdeconnect-cli --pair --device <DEVICE_ID>${NC}"
echo ""
