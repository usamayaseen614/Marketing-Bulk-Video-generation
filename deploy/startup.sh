#!/bin/bash
# Installs Docker on a fresh Debian VM. Passed to `gcloud compute instances
# create` via --metadata-from-file=startup-script=startup.sh.
#
# Use --metadata-from-file rather than inlining this: gcloud splits --metadata
# on commas, so a single comma anywhere in the script silently truncates it and
# you get a VM with no Docker and no error.
#
# Runs once, at first boot, as root. Takes 1-3 minutes. It finishes by touching
# /var/log/startup-done, which is what you wait for before SSHing in and
# expecting `docker` to exist.
#
# Debug a failed run with:
#   sudo journalctl -u google-startup-scripts.service -n 200 --no-pager

set -eux
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl git

# Docker's own apt repository. Debian's `docker.io` package is older and the
# standalone `docker-compose` (v1) is dead — this project needs the v2
# compose plugin, invoked as `docker compose` with a space.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# deb822 (.sources) format. Docker switched to this; the older one-line
# "deb [signed-by=...]" entry in docker.list is what most stale copy-paste
# snippets still use and it is no longer what Docker documents.
tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

# Marker the deploy steps poll for.
touch /var/log/startup-done
