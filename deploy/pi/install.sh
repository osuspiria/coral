#!/usr/bin/env bash
# CORAL player installer (spec 8.1). Run as root on a Raspberry Pi with Raspberry Pi
# OS Lite (Debian 12/13). Idempotent: running it again only fixes what is missing.
#
# First installation:
#   sudo ./install.sh --release coral-player-0.3.4.tar.gz --pubkey release-pubkey.pub \
#        --server https://coral.example.com [--token-file token]
# Emergency access (after `signagectl player wg-add` on the VPS):
#   sudo ./install.sh --wireguard-only --wg-address 10.77.0.2 \
#        --wg-endpoint coral.example.com:51820 --wg-server-pubkey <key>
# New Pis only: data partition + read-only root (spec 4.1):
#   ... --data-partition /dev/nvme0n1p3 --readonly-root
set -euo pipefail

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'
Options:
  --release FILE          signed release tarball (FILE.minisig next to it)
  --pubkey FILE           release public key (installed as /etc/signage/release-pubkey.pub)
  --server URL            dashboard URL (https://...), written to config.toml on first install
  --token-file FILE       player token (asked interactively when missing and not installed)
  --wg-address IP         WireGuard tunnel address given by `signagectl player wg-add`
  --wg-endpoint HOST:PORT WireGuard endpoint of the VPS
  --wg-server-pubkey KEY  public key of the VPS
  --wireguard-only        only (re)configure WireGuard
  --ssh-only-wireguard    accept SSH only on the tunnel, and only with keys
  --data-partition DEV    empty partition for /srv/signage (new Pis only)
  --readonly-root         enable the overlay root (requires --data-partition)
  --force                 allow formatting a partition that already has a filesystem
  --dry-run               print the actions without changing anything
EOF
}

RELEASE="" PUBKEY="" SERVER="" TOKEN_FILE=""
WG_ADDRESS="" WG_ENDPOINT="" WG_SERVER_PUBKEY="" WIREGUARD_ONLY=0 SSH_ONLY_WG=0
DATA_PARTITION="" READONLY_ROOT=0 FORCE=0 DRY_RUN=0
OS_RELEASE=${CORAL_OS_RELEASE:-/etc/os-release}   # overridable for tests
PYTHON=${CORAL_INSTALL_PYTHON:-python3}
WATCHDOG_MAX_FILE=${CORAL_WATCHDOG_MAX_FILE:-/sys/class/watchdog/watchdog0/max_timeout}

while [ $# -gt 0 ]; do
    case "$1" in
        --release) RELEASE=$2; shift 2 ;;
        --pubkey) PUBKEY=$2; shift 2 ;;
        --server) SERVER=$2; shift 2 ;;
        --token-file) TOKEN_FILE=$2; shift 2 ;;
        --wg-address) WG_ADDRESS=$2; shift 2 ;;
        --wg-endpoint) WG_ENDPOINT=$2; shift 2 ;;
        --wg-server-pubkey) WG_SERVER_PUBKEY=$2; shift 2 ;;
        --wireguard-only) WIREGUARD_ONLY=1; shift ;;
        --ssh-only-wireguard) SSH_ONLY_WG=1; shift ;;
        --data-partition) DATA_PARTITION=$2; shift 2 ;;
        --readonly-root) READONLY_ROOT=1; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
run() {
    echo "+ $*"
    if [ "$DRY_RUN" = 0 ]; then "$@"; fi
}
write_file() {  # write_file PATH MODE  (content on stdin)
    local path=$1 mode=$2 content
    content=$(cat)
    echo "+ write $path ($mode)"
    if [ "$DRY_RUN" = 0 ]; then
        install -d "$(dirname "$path")"
        printf '%s\n' "$content" > "$path.tmp"
        chmod "$mode" "$path.tmp"
        mv "$path.tmp" "$path"
    fi
}

# ------------------------------------------------------------------ checks

check_system() {
    step "Checking the system"
    [ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE"
    # shellcheck disable=SC1090
    . "$OS_RELEASE"
    case "${ID:-}:${VERSION_CODENAME:-}" in
        debian:bookworm|debian:trixie|raspbian:bookworm|raspbian:trixie) ;;
        *) die "unsupported system ${PRETTY_NAME:-unknown} (expected Raspberry Pi OS / Debian 12 or 13)" ;;
    esac
    echo "system: ${PRETTY_NAME}"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
        || die "python3 >= 3.11 is required (the agent uses tomllib)"
    echo "python: $("$PYTHON" --version 2>&1)"
    if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
        die "run as root (sudo)"
    fi
    if [ "$READONLY_ROOT" = 1 ] && [ -z "$DATA_PARTITION" ]; then
        die "--readonly-root needs --data-partition: the agent and the updater must persist"
    fi
    if [ "$WIREGUARD_ONLY" = 0 ] && [ ! -e /opt/signage/current ]; then
        [ -n "$RELEASE" ] || die "--release is required on a first installation"
        [ -n "$PUBKEY" ] || [ -f /etc/signage/release-pubkey.pub ] \
            || die "--pubkey is required on a first installation"
        [ -n "$SERVER" ] || [ -f /etc/signage/config.toml ] \
            || die "--server is required on a first installation"
    fi
    if [ -n "$SERVER" ]; then
        case "$SERVER" in https://*) ;; *) die "--server must be https://" ;; esac
    fi
}

# ------------------------------------------------------------------ steps

install_packages() {
    step "Packages"
    run apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        mpv polkitd minisign chrony wireguard-tools unattended-upgrades nftables
}

create_user() {
    step "User and directories"
    if id signage >/dev/null 2>&1; then
        echo "user signage exists"
    else
        run useradd --system --home-dir /var/lib/signage --no-create-home \
            --shell /usr/sbin/nologin --groups video,render,audio signage
    fi
    run install -d -m 755 /opt/signage /opt/signage/releases /etc/signage
}

setup_data_partition() {
    [ -n "$DATA_PARTITION" ] || return 0
    step "Data partition $DATA_PARTITION -> /srv/signage"
    [ -b "$DATA_PARTITION" ] || [ "$DRY_RUN" = 1 ] || die "$DATA_PARTITION is not a block device"
    local fstype=""
    if [ -b "$DATA_PARTITION" ]; then fstype=$(blkid -o value -s TYPE "$DATA_PARTITION" || true); fi
    if [ -n "$fstype" ] && [ "$fstype" != ext4 ]; then
        die "$DATA_PARTITION has a $fstype filesystem; refusing (use an empty partition)"
    fi
    if [ -z "$fstype" ]; then
        run mkfs.ext4 -q -L signage-data "$DATA_PARTITION"
    elif ! grep -q "/srv/signage " /etc/fstab 2>/dev/null && [ "$FORCE" = 0 ]; then
        die "$DATA_PARTITION already has an ext4 filesystem that is not ours; use --force to reuse it"
    fi
    run install -d /srv/signage
    if ! grep -q " /srv/signage " /etc/fstab 2>/dev/null; then
        local uuid="UUID-OF-$DATA_PARTITION"
        if [ "$DRY_RUN" = 0 ]; then uuid=$(blkid -o value -s UUID "$DATA_PARTITION"); fi
        echo "+ append fstab entries"
        if [ "$DRY_RUN" = 0 ]; then
            {
                echo "UUID=$uuid /srv/signage ext4 defaults,noatime,commit=60 0 2"
                echo "/srv/signage/opt /opt/signage none bind,x-systemd.requires-mounts-for=/srv/signage 0 0"
                echo "/srv/signage/lib /var/lib/signage none bind,x-systemd.requires-mounts-for=/srv/signage 0 0"
                echo "/srv/signage/updater /var/lib/signage-updater none bind,x-systemd.requires-mounts-for=/srv/signage 0 0"
            } >> /etc/fstab
        fi
    fi
    run mount /srv/signage || true
    run install -d /srv/signage/opt /srv/signage/lib /srv/signage/updater
    # Keep whatever is already installed (e.g. re-running on an existing Pi).
    for pair in /opt/signage:opt /var/lib/signage:lib /var/lib/signage-updater:updater; do
        local src=${pair%%:*} dst=/srv/signage/${pair##*:}
        if [ -d "$src" ] && ! mountpoint -q "$src" && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
            run cp -a "$src/." "$dst/"
        fi
        run install -d "$src"
    done
    run mount -a
}

install_release() {
    [ -n "$RELEASE" ] || return 0
    step "Release $RELEASE"
    local name version dir
    name=$(basename "$RELEASE")
    version=${name#coral-player-}; version=${version%.tar.gz}
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected release file name $name"
    if [ -n "$PUBKEY" ]; then run install -m 644 "$PUBKEY" /etc/signage/release-pubkey.pub; fi
    # Verify BEFORE extracting anything (spec 6.1).
    run minisign -V -p /etc/signage/release-pubkey.pub -m "$RELEASE" -x "$RELEASE.minisig"
    dir=/opt/signage/releases/$version
    if [ -d "$dir" ]; then
        echo "release $version already installed"
    else
        run install -d "$dir.tmp"
        run tar xzf "$RELEASE" -C "$dir.tmp"
        run mv "$dir.tmp" "$dir"
    fi
    if [ -e /opt/signage/current ]; then
        echo "current -> $(readlink /opt/signage/current) (left as is; the updater manages it)"
    else
        run ln -sfn "$dir" /opt/signage/current
    fi
}

write_config() {
    step "Configuration"
    if [ -f /etc/signage/config.toml ]; then
        echo "/etc/signage/config.toml exists (left as is)"
    else
        write_file /etc/signage/config.toml 640 <<EOF
server_url = "$SERVER"
token_file = "/etc/signage/token"
data_dir = "/var/lib/signage"
mpv_ipc = "unix:/run/signage/mpv.sock"
EOF
    fi
    if [ -f /etc/signage/token ]; then
        echo "/etc/signage/token exists (left as is)"
    else
        local token
        if [ -n "$TOKEN_FILE" ]; then
            token=$(tr -d '[:space:]' < "$TOKEN_FILE")
        elif [ "$DRY_RUN" = 1 ]; then
            token="(asked interactively)"
        else
            read -r -s -p "Player token (from signagectl player add): " token; echo
        fi
        [ -n "$token" ] || die "empty token"
        printf '%s\n' "$token" | write_file /etc/signage/token 640
    fi
    run chown root:signage /etc/signage/config.toml /etc/signage/token
}

install_units() {
    step "Services, updater, logs, polkit"
    local d=/opt/signage/current/deploy/pi
    run install -m 644 "$d/mpv.service" "$d/signage-agent.service" \
        "$d/signage-updater.service" "$d/signage-updater.timer" /etc/systemd/system/
    run install -m 644 "$d/tmpfiles-signage.conf" /etc/tmpfiles.d/signage.conf
    run install -d /etc/systemd/journald.conf.d
    run install -m 644 "$d/journald-signage.conf" /etc/systemd/journald.conf.d/signage.conf
    run install -d /etc/polkit-1/rules.d
    run install -m 644 "$d/polkit-signage.rules" /etc/polkit-1/rules.d/50-signage.rules
    run rm -rf /opt/signage/updater.new
    run install -d /opt/signage/updater.new
    run cp -r /opt/signage/current/player/updater/coral_updater /opt/signage/updater.new/
    run rm -rf /opt/signage/updater
    run mv /opt/signage/updater.new /opt/signage/updater
    run systemd-tmpfiles --create /etc/tmpfiles.d/signage.conf
}

setup_time_and_watchdog() {
    step "Clock (chrony) and hardware watchdog"
    # No RTC: allow stepping the clock at any time, not only at boot (spec 4.5).
    write_file /etc/chrony/conf.d/signage.conf 644 <<'EOF'
# CORAL: the Pi has no RTC; step the clock whenever it is far off.
makestep 1 -1
EOF
    run systemctl enable --now chrony
    # Some kernels report max_timeout 0 (unknown): never write 0s, that disables it.
    local max="" timeout=15
    if [ -r "$WATCHDOG_MAX_FILE" ]; then
        max=$(tr -d '[:space:]' < "$WATCHDOG_MAX_FILE")
    fi
    if [[ "$max" =~ ^[0-9]+$ ]] && [ "$max" -gt 0 ]; then
        timeout=$(( max < 15 ? max : 15 ))
        echo "hardware watchdog: max ${max}s, using ${timeout}s"
    else
        echo "hardware watchdog: max unknown (${max:-not readable}), using ${timeout}s"
    fi
    write_file /etc/systemd/system.conf.d/signage-watchdog.conf 644 <<EOF
# CORAL: systemd feeds the hardware watchdog; a total hang reboots the Pi (spec 4.1).
[Manager]
RuntimeWatchdogSec=${timeout}s
RebootWatchdogSec=2min
EOF
}

setup_security_updates() {
    step "Automatic security updates"
    if [ "$READONLY_ROOT" = 1 ]; then
        echo "read-only root: automatic OS updates OFF (use maintenance mode, see docs)"
        write_file /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
    else
        # Debian's default unattended-upgrades origins are the security archive only.
        write_file /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    fi
}

setup_wireguard() {
    step "Emergency access (WireGuard)"
    run install -d -m 700 /etc/wireguard
    if [ ! -f /etc/wireguard/signage.key ]; then
        echo "+ generate the Pi's WireGuard key (it never leaves the Pi)"
        if [ "$DRY_RUN" = 0 ]; then (umask 077; wg genkey > /etc/wireguard/signage.key); fi
    fi
    if [ "$DRY_RUN" = 0 ]; then
        echo "WireGuard public key of this Pi: $(wg pubkey < /etc/wireguard/signage.key)"
    fi
    if [ -z "$WG_ADDRESS" ]; then
        echo "Register it on the VPS with: signagectl player wg-add <id> --pubkey <key>"
        return 0
    fi
    [ -n "$WG_ENDPOINT" ] && [ -n "$WG_SERVER_PUBKEY" ] \
        || die "--wg-address needs --wg-endpoint and --wg-server-pubkey"
    local private="(private key)"
    if [ "$DRY_RUN" = 0 ]; then private=$(cat /etc/wireguard/signage.key); fi
    write_file /etc/wireguard/wg0.conf 600 <<EOF
# CORAL emergency access (spec 6.4). The Pi only talks to the VPS through it.
[Interface]
PrivateKey = $private
Address = $WG_ADDRESS/32
# Mobile networks often have a smaller path MTU: avoid fragmentation.
MTU = 1280

[Peer]
PublicKey = $WG_SERVER_PUBKEY
Endpoint = $WG_ENDPOINT
AllowedIPs = 10.77.0.1/32
PersistentKeepalive = 25
EOF
    write_file /etc/systemd/system/wg-quick@wg0.service.d/10-signage.conf 644 <<'EOF'
# CORAL: bring the emergency tunnel up after the network, retry if it fails (spec 6.4).
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
Restart=on-failure
RestartSec=30
EOF
    run systemctl daemon-reload
    run systemctl enable wg-quick@wg0
    run systemctl restart wg-quick@wg0
}

setup_ssh_only_wireguard() {
    [ "$SSH_ONLY_WG" = 1 ] || return 0
    step "SSH: keys only, tunnel only"
    if [ "$DRY_RUN" = 0 ] && ! ls /home/*/.ssh/authorized_keys /root/.ssh/authorized_keys >/dev/null 2>&1; then
        die "no authorized_keys found: add your SSH key first, or you would be locked out"
    fi
    write_file /etc/ssh/sshd_config.d/10-signage.conf 644 <<'EOF'
# CORAL: SSH with keys only (spec 6.4).
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
    write_file /etc/nftables.d/signage-ssh-wg.nft 644 <<'EOF'
# CORAL: SSH only through the WireGuard tunnel (spec 6.4).
table inet signage_ssh {
    chain input {
        type filter hook input priority 0; policy accept;
        tcp dport 22 iifname != "wg0" drop
    }
}
EOF
    write_file /etc/systemd/system/signage-ssh-wg.service 644 <<'EOF'
[Unit]
Description=CORAL: accept SSH only on the WireGuard tunnel
After=network-pre.target
Before=ssh.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d/signage-ssh-wg.nft
ExecStop=/usr/sbin/nft delete table inet signage_ssh

[Install]
WantedBy=multi-user.target
EOF
    run systemctl daemon-reload
    run systemctl enable --now signage-ssh-wg.service
    run systemctl reload ssh
}

enable_services() {
    step "Starting services"
    run systemctl daemon-reload
    run systemctl restart systemd-journald
    run systemctl enable --now mpv.service signage-agent.service signage-updater.timer
}

enable_readonly_root() {
    [ "$READONLY_ROOT" = 1 ] || return 0
    step "Read-only root (overlay)"
    run raspi-config nonint do_overlayfs 0
    echo "The overlay takes effect after a reboot: sudo reboot"
}

# ------------------------------------------------------------------ main

check_system
if [ "$WIREGUARD_ONLY" = 1 ]; then
    setup_wireguard
    setup_ssh_only_wireguard
    exit 0
fi
install_packages
create_user
setup_data_partition
install_release
write_config
install_units
setup_time_and_watchdog
setup_security_updates
setup_wireguard
setup_ssh_only_wireguard
enable_services
enable_readonly_root
step "Done"
echo "Check: systemctl status mpv signage-agent; journalctl -u signage-agent -f"
