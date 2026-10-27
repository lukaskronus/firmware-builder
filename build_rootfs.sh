#!/bin/bash
set -e

# Cleanup trap - unmount everything if script exits for any reason
ROOTFS_DIR="$(pwd)/pure_rootfs"
cleanup() {
    echo "--- Cleaning up mounts ---"
    sudo umount -lf "$ROOTFS_DIR/dev/pts" 2>/dev/null || true
    sudo umount -lf "$ROOTFS_DIR/dev"     2>/dev/null || true
    sudo umount -lf "$ROOTFS_DIR/sys"     2>/dev/null || true
    sudo umount -lf "$ROOTFS_DIR/proc"    2>/dev/null || true
}
trap cleanup EXIT

echo "=== Building Debian 13 (Trixie) rootfs for Hi3798MV100 ==="

# ── 1. Prepare working directory ────────────────────────────────────────────
sudo rm -rf "$ROOTFS_DIR"
mkdir -p "$ROOTFS_DIR"

# ── 2. First stage debootstrap ───────────────────────────────────────────────
# Only include packages needed for first boot here.
# SSH, network and Docker prerequisites are added inside the chroot.
echo "--- Stage 1: debootstrap ---"
sudo debootstrap \
    --arch=armhf \
    --foreign \
    --variant=minbase \
    --include=systemd,systemd-sysv,dbus \
    trixie "$ROOTFS_DIR" http://deb.debian.org/debian/

# ── 3. Prepare chroot environment ────────────────────────────────────────────
echo "--- Preparing chroot environment ---"
sudo cp /usr/bin/qemu-arm-static "$ROOTFS_DIR/usr/bin/"
sudo cp /etc/resolv.conf "$ROOTFS_DIR/etc/resolv.conf"

sudo mount -t proc  /proc        "$ROOTFS_DIR/proc"
sudo mount -t sysfs /sys         "$ROOTFS_DIR/sys"
sudo mount -o bind  /dev         "$ROOTFS_DIR/dev"
sudo mount -o bind  /dev/pts     "$ROOTFS_DIR/dev/pts"

# ── 4. Second stage + configuration inside chroot ────────────────────────────
echo "--- Stage 2: debootstrap + configuration ---"
sudo chroot "$ROOTFS_DIR" /bin/bash << 'CHROOT_EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

# Complete debootstrap second stage
/debootstrap/debootstrap --second-stage

# ── Apt sources ──────────────────────────────────────────────────────────────
cat > /etc/apt/sources.list << 'SOURCES'
deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb http://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware
deb http://deb.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
SOURCES

apt-get update -qq
apt-get upgrade -y -q -o APT::Install-Recommends=false

# ── Packages: minimal headless Docker host ──────────────────────────────────
# Only boot, SSH, Docker prerequisites, time sync, and rootfs resize.
apt-get install -y -q --no-install-recommends \
    sudo \
    nano \
    kmod \
    curl \
    gnupg \
    ca-certificates \
    openssh-server \
    iproute2 \
    iptables \
    e2fsprogs \
    chrony \
    systemd-resolved \
    usbutils \
    firmware-realtek \
    wpasupplicant

# ── Hostname and hosts ───────────────────────────────────────────────────────
echo "debian" > /etc/hostname
cat > /etc/hosts << 'HOSTS'
127.0.0.1   localhost
127.0.1.1   debian
HOSTS

# ── Timezone ─────────────────────────────────────────────────────────────────
ln -sf /usr/share/zoneinfo/Asia/Ho_Chi_Minh /etc/localtime
echo "Asia/Ho_Chi_Minh" > /etc/timezone

# ── Root password ────────────────────────────────────────────────────────────
echo "root:1234" | chpasswd

# ── SSH ──────────────────────────────────────────────────────────────────────
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
systemctl enable ssh

# Generate host keys on first boot so cloned images do not share them.
rm -f /etc/ssh/ssh_host_*
cat > /etc/systemd/system/ssh-keygen.service << 'SERVICE'
[Unit]
Description=Generate SSH host keys
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
Before=ssh.service
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
SERVICE
systemctl enable ssh-keygen.service

# ── fstab ────────────────────────────────────────────────────────────────────
cat > /etc/fstab << 'FSTAB'
# /etc/fstab
/dev/mmcblk0p9  /       ext4    defaults,noatime,errors=remount-ro  0 1
proc            /proc   proc    defaults                             0 0
sysfs           /sys    sysfs   defaults                             0 0
tmpfs           /tmp    tmpfs   defaults,noatime,nosuid,size=100M    0 0
tmpfs           /var/tmp tmpfs  defaults,noatime,nosuid,size=50M     0 0
FSTAB

# ── Network: systemd-networkd + per-board MAC ───────────────────────────────
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/10-lo.network << 'NET'
[Match]
Name=lo

[Network]
Address=127.0.0.1/8
NET

cat > /etc/systemd/network/10-eth0.network << 'NET'
[Match]
Name=eth0

[Network]
DHCP=yes
NET

# RTL8188ETV USB dongle (0bda:0179, rtl8xxxu). The credentials are left as
# placeholders on purpose so no PSK is committed to git; edit them on the board
# and run "systemctl restart systemd-networkd". systemd-networkd drives
# wpa_supplicant itself, so no separate wifi service is needed.
cat > /etc/systemd/network/20-wlan0.network << 'NET'
[Match]
Name=wlan*

[Network]
DHCP=yes

[WPA]
SSID=CHANGE-ME
PSK=CHANGE-ME
NET

# Derive a stable locally administered MAC from the eMMC CID. The DTS stays
# generic; the generated .link is applied by udev on every later boot.
cat > /usr/local/sbin/set-eth0-mac << 'SCRIPT'
#!/bin/sh
set -eu

LINK=/etc/systemd/network/10-eth0.link
[ -e "$LINK" ] && exit 0

CID=$(cat /sys/block/mmcblk0/device/cid 2>/dev/null || true)
[ -n "$CID" ] || CID=$(cat /etc/machine-id 2>/dev/null || true)
[ -n "$CID" ] || exit 0

HASH=$(printf '%s' "$CID" | sha256sum | cut -c1-12)
B1=$(printf '%s' "$HASH" | cut -c1-2)
B2=$(printf '%s' "$HASH" | cut -c3-4)
B3=$(printf '%s' "$HASH" | cut -c5-6)
B4=$(printf '%s' "$HASH" | cut -c7-8)
B5=$(printf '%s' "$HASH" | cut -c9-10)
B6=$(printf '%s' "$HASH" | cut -c11-12)
FIRST=$(( (0x$B1 & 0xFC) | 0x02 ))
MAC=$(printf '%02x:%s:%s:%s:%s:%s' "$FIRST" "$B2" "$B3" "$B4" "$B5" "$B6")
[ "${#MAC}" -eq 17 ] || exit 1

mkdir -p /etc/systemd/network
cat > "$LINK" << EOF
[Match]
OriginalName=eth0

[Link]
MACAddressPolicy=none
MACAddress=$MAC
EOF
chmod 0644 "$LINK"

# Apply immediately; on later boots udev applies the .link before networkd.
udevadm settle --timeout=5 2>/dev/null || true
ip link set dev eth0 address "$MAC" 2>/dev/null || true
SCRIPT
chmod 0755 /usr/local/sbin/set-eth0-mac

cat > /etc/systemd/system/set-eth0-mac.service << 'SERVICE'
[Unit]
Description=Set a stable per-board MAC address for eth0
ConditionPathExists=!/etc/systemd/network/10-eth0.link
Before=network-pre.target
After=local-fs.target systemd-udevd.service
DefaultDependencies=no

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/set-eth0-mac
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
SERVICE

systemctl enable systemd-networkd.service systemd-resolved.service dbus.service
systemctl enable set-eth0-mac.service

# Point glibc at resolved's upstream file so Docker containers do not inherit
# the 127.0.0.53 stub address.
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf

# ── Serial console (systemd, NOT inittab) ────────────────────────────────────
# Use a full unit file instead of drop-in to avoid dev-ttyAMA0.device dependency
cat > /etc/systemd/system/serial-getty@ttyAMA0.service << 'SERVICE'
[Unit]
Description=Serial Getty on ttyAMA0
Documentation=man:agetty(8) man:systemd-getty-generator(8)
After=sysinit.target
DefaultDependencies=no

[Service]
Type=idle
ExecStart=-/sbin/agetty --keep-baud 115200 ttyAMA0 vt100
Restart=always
RestartSec=0
UtmpIdentifier=ttyAMA0
TTYPath=/dev/ttyAMA0
TTYReset=yes
TTYVHangup=yes
KillMode=process

[Install]
WantedBy=getty.target
SERVICE

systemctl enable serial-getty@ttyAMA0.service

# ── Locale ───────────────────────────────────────────────────────────────────
# C.UTF-8 is built into glibc, so the locales package is unnecessary.
cat > /etc/default/locale << 'LOCALE'
LANG=C.UTF-8
LOCALE

# ── First boot resize service ─────────────────────────────────────────────────
# Automatically expands the ext4 filesystem to fill mmcblk0p9 on first boot
cat > /etc/systemd/system/resize-rootfs.service << 'SERVICE'
[Unit]
Description=Resize root filesystem to fill partition
After=local-fs.target
ConditionPathExists=/etc/resize-rootfs-pending

[Service]
Type=oneshot
ExecStart=/sbin/resize2fs /dev/mmcblk0p9
ExecStartPost=/bin/rm -f /etc/resize-rootfs-pending
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE

touch /etc/resize-rootfs-pending
systemctl enable resize-rootfs.service

# ── Cleanup ──────────────────────────────────────────────────────────────────
apt-get autoremove -y -q
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/* /usr/share/locale/*
rm -f /usr/bin/qemu-arm-static

# Give cloned images a fresh machine-id and SSH host keys on first boot.
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
mkdir -p /var/lib/dbus
ln -s /etc/machine-id /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*

echo "✅ Chroot configuration complete"
CHROOT_EOF

echo "=== Rootfs build complete ==="
echo "Location : $ROOTFS_DIR"
echo "Root password : 1234  (change immediately after first boot)"  
