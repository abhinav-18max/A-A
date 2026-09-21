#!/bin/sh
set -eu
mkdir -p "${HOME:-/tmp/mba-home}"
# PulseAudio requires a passwd entry even when the worker uses an arbitrary host UID.
if ! getent passwd "$(id -u)" >/dev/null; then
    cp /etc/passwd /tmp/mba-passwd
    cp /etc/group /tmp/mba-group
    printf 'mba:x:%s:%s:Adapter:%s:/bin/sh\n' "$(id -u)" "$(id -g)" "${HOME:-/tmp/mba-home}" >> /tmp/mba-passwd
    export NSS_WRAPPER_PASSWD=/tmp/mba-passwd NSS_WRAPPER_GROUP=/tmp/mba-group
    NSS_LIBRARY=$(dpkg -L libnss-wrapper | sed -n '/\/libnss_wrapper.so$/p')
    test -f "$NSS_LIBRARY"
    export LD_PRELOAD="$NSS_LIBRARY"
fi
exec python -m mba.cli worker "$@"
