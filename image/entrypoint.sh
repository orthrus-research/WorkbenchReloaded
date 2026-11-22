#!/bin/sh
# Runs Workbench as the host's user (spec 13; platform-neutral). On a native Linux engine, bind-mounted output
# roots belong to whoever provisioned Workbench, so `wb up` passes that user's uid and gid (WB_UID, WB_GID).
# Docker Desktop maps ownership itself and keeps the default, 1000. The container starts as root only to:
#   - give the state volume to that user (only the files that are not theirs yet),
#   - make sure the uid has a passwd entry (OpenSSH refuses to run for a uid without one),
# and then drops every privilege before Workbench starts.
set -eu
uid="${WB_UID:-1000}"
gid="${WB_GID:-1000}"
if [ "$(id -u)" = "0" ]; then
  getent group "$gid" >/dev/null 2>&1 || echo "wbhost:x:${gid}:" >> /etc/group
  getent passwd "$uid" >/dev/null 2>&1 || echo "wbhost:x:${uid}:${gid}:Workbench:/home/wb:/bin/sh" >> /etc/passwd  # scrub: allow (container path)
  mkdir -p /var/lib/wb
  find /var/lib/wb \( ! -uid "$uid" -o ! -gid "$gid" \) -exec chown -h "${uid}:${gid}" {} + 2>/dev/null || true
  chown "${uid}:${gid}" /home/wb 2>/dev/null || true  # scrub: allow
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --inh-caps=-all env HOME=/home/wb "$@"  # scrub: allow
fi
exec "$@"
