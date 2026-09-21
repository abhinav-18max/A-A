`seccomp.json` is the Playwright v1.51.0 Docker seccomp profile:
https://github.com/microsoft/playwright/blob/v1.51.0/utils/docker/seccomp_profile.json

Playwright is Copyright Microsoft Corporation and contributors, licensed under
Apache License 2.0: https://github.com/microsoft/playwright/blob/v1.51.0/LICENSE

The profile retains Docker's deny-by-default syscall policy and the upstream
user-namespace allowances. It is not a seccomp-unconfined configuration.

Local modification: `clone3` returns ENOSYS (38) so modern glibc can fall back to
the allowed `clone` syscall. This avoids treating a denied new syscall as a
thread-creation failure. The Apache 2.0 license is included in `LICENSE.playwright`.

Local modification: allow the `chroot` syscall even when the container drops all
capabilities. Chromium uses it inside its own user namespace to establish its
sandbox. Linux still checks the caller's namespace capabilities; this does not
grant `CAP_SYS_CHROOT` or `CAP_SYS_ADMIN` to the container.
