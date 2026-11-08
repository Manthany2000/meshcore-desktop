#!/usr/bin/env bash
# Builds dist/meshcore-desktop-linux-x86_64.tar.gz inside an Ubuntu 22.04 container
# (glibc 2.35), so it runs on Ubuntu 22.04+, Debian 12+, Fedora 36+ and the like, not just this one.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
engine="$(command -v podman || command -v docker)"
# Rootless podman already maps the container's root to you; docker needs a chown.
owner=()
[[ "$engine" == *docker ]] && owner=(-e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)")
"$engine" run --rm "${owner[@]}" -v "$root:/src:Z" -w /src docker.io/library/ubuntu:22.04 bash -euc '
  apt-get update -qq
  # Libraries Qt (and WebEngine) link against, so PyInstaller can bundle them.
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates curl binutils libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
    libfontconfig1 libdbus-1-3 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxshmfence1 \
    libasound2 libxkbfile1 libgbm1 libdrm2 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libwayland-client0 \
    libwayland-cursor0 libwayland-egl1 libglib2.0-0 libxrender1 libxi6 libx11-xcb1 >/dev/null
  # A standalone Python 3.12 (22.04 ships 3.10).
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
  uv venv -q -p 3.12 /tmp/v && VIRTUAL_ENV=/tmp/v uv pip install -q -r requirements.txt pyinstaller
  /tmp/v/bin/pyinstaller --noconfirm --log-level WARN --clean --distpath /tmp/dist --workpath /tmp/build packaging/meshcore-desktop.spec
  cp packaging/meshcore-desktop.desktop /tmp/dist/meshcore-desktop/
  cp meshcore_desktop/icons/app.png /tmp/dist/meshcore-desktop/meshcore-desktop.png
  cp packaging/PORTABLE.txt /tmp/dist/meshcore-desktop/
  mkdir -p dist && tar -C /tmp/dist -czf dist/meshcore-desktop-linux-x86_64.tar.gz meshcore-desktop
  [ -n "${HOST_UID:-}" ] && chown -R "$HOST_UID:$HOST_GID" dist || true
'
echo "built dist/meshcore-desktop-linux-x86_64.tar.gz"
