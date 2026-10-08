#!/bin/sh
set -eu

mono_msi="$1"
expected_sha256='6413ff328ebbf7ec7689c648feb3546d8102ded865079d1fbf0331b14b3ab0ec'
actual_sha256="$(sha256sum "$mono_msi" | cut -d' ' -f1)"
test "$actual_sha256" = "$expected_sha256"

install -d -m 0755 /usr/share/wine/mono
install -m 0644 "$mono_msi" /usr/share/wine/mono/wine-mono-7.4.0-x86.msi
test "$(/usr/lib/wine/wine64 --version)" = 'wine-8.0 (Debian 8.0~repack-4)'
