#!/usr/bin/env bash
# Run explicitly on the Linux host. This script does not run from application startup.
set -euo pipefail
if [[ "$(uname -s)" != Linux ]]; then
  echo 'A Linux host is required.' >&2
  exit 1
fi
if [[ "${EUID}" != 0 ]]; then
  echo 'Run with sudo on the prepared Linux host.' >&2
  exit 1
fi
apt-get update
apt-get install -y "linux-headers-$(uname -r)" v4l2loopback-dkms v4l-utils ffmpeg
if lsmod | awk '{print $1}' | grep -qx v4l2loopback; then
  if [[ ! -c /dev/video10 || ! -c /dev/video11 ]]; then
    echo 'v4l2loopback is already loaded with a different device pool; stop existing users and configure the pool explicitly.' >&2
    exit 1
  fi
else
  modprobe v4l2loopback video_nr=10,11 card_label=QA_CAM_10,QA_CAM_11 exclusive_caps=1,1
fi
cat > /etc/modprobe.d/mba-camera.conf <<'EOF'
options v4l2loopback video_nr=10,11 card_label=QA_CAM_10,QA_CAM_11 exclusive_caps=1,1
EOF
cat > /etc/modules-load.d/mba-camera.conf <<'EOF'
v4l2loopback
EOF
cat > /etc/udev/rules.d/80-mba-camera.rules <<'EOF'
SUBSYSTEM=="video4linux", ATTR{name}=="QA_CAM_*", GROUP="video", MODE="0660"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=video4linux
for device in /dev/video10 /dev/video11; do
  v4l2-ctl --device="$device" --info
done
echo 'Camera pool ready. Run the controller as a non-root user with Docker and video-device access.'
