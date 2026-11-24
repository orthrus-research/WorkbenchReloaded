# Which Workbench code the container runs; sourced by the entrypoint and by the image's own wb command.
#
# The image carries no Workbench code, only what needs an image (OS packages, Python, the JDKs, tools). `wb up` and
# `wb deploy` mount the code they start read-only at /opt/wb/release and name the release in WB_RELEASE. The first
# start of a release copies it into the state volume (/var/lib/wb/releases/<release>), whole or not at all, and the
# daemon imports that copy: a Linux filesystem, and nothing done on the host later reaches a running daemon. So a
# Python-only deploy needs no image build, only a restart.
#
# wb_pick_code copy|read chooses, in order:
#   1. the state volume's copy of WB_RELEASE ("copy" first makes it from the mount when it is missing);
#   2. the mount itself, read in place: a checkout that `wb up` started on a machine with no release yet
#      (WB_RELEASE empty, WB_SOURCE_COMMIT its commit), or a release the volume could not take;
#   3. the release this volume ran last (a compose file from before the mount);
#   4. code an older image carries in /opt/wb/src.
# It sets and exports WB_CODE, PYTHONPATH and WB_COMMIT, or says what is missing and returns 1.
# The folders are variables only so the tests can run this outside a container.

wb_state="${WB_STATE_DIR:-/var/lib/wb}"
wb_mount="${WB_RELEASE_MOUNT:-/opt/wb/release}"
wb_baked="${WB_BAKED_CODE:-/opt/wb/src}"
wb_python="${WB_PYTHON:-python3}"

wb_is_code() {
  [ -f "$1/core/wb_core/__init__.py" ] && [ -f "$1/sdk/python/wb/__init__.py" ]
}

wb_is_release_name() {
  case "$1" in
    "" | *[!0-9a-f]*) return 1 ;;
  esac
  return 0
}

wb_copy_release() {
  # $1: the release name. Copies the mount into the volume unless the volume has it; never a half copy.
  wb_dest="$wb_state/releases/$1"
  [ -f "$wb_dest/RELEASE.json" ] && return 0
  [ -f "$wb_mount/RELEASE.json" ] && wb_is_code "$wb_mount" || return 1
  mkdir -p "$wb_state/releases" || return 1
  rm -rf "$wb_dest.part" "$wb_dest" || return 1
  mkdir "$wb_dest.part" && cp -R "$wb_mount/." "$wb_dest.part/" && mv "$wb_dest.part" "$wb_dest" || {
    rm -rf "$wb_dest.part"
    return 1
  }
  echo "wb: release $1 copied into the state volume" >&2
}

wb_pick_code() {
  WB_CODE=""
  wb_release="${WB_RELEASE:-}"
  if [ -n "$wb_release" ]; then
    if ! wb_is_release_name "$wb_release"; then
      echo "wb: WB_RELEASE=$wb_release is not a release name (the commit's first 12 hex digits)" >&2
      wb_release=""
    elif [ "$1" = copy ] && ! wb_copy_release "$wb_release"; then
      echo "wb: could not copy release $wb_release into the state volume; running it from the mount" >&2
    fi
    if [ -n "$wb_release" ] && [ -f "$wb_state/releases/$wb_release/RELEASE.json" ] \
       && wb_is_code "$wb_state/releases/$wb_release"; then
      WB_CODE="$wb_state/releases/$wb_release"
    fi
  fi
  if [ -z "$WB_CODE" ] && wb_is_code "$wb_mount"; then
    WB_CODE="$wb_mount"
  fi
  if [ -z "$WB_CODE" ] && [ -f "$wb_state/releases/last" ]; then
    wb_last="$(cat "$wb_state/releases/last")"
    if wb_is_release_name "$wb_last" && wb_is_code "$wb_state/releases/$wb_last"; then
      WB_CODE="$wb_state/releases/$wb_last"
    fi
  fi
  if [ -z "$WB_CODE" ] && wb_is_code "$wb_baked"; then
    WB_CODE="$wb_baked"
  fi
  if [ -z "$WB_CODE" ]; then
    echo "wb: no Workbench code to run: no release in the state volume, nothing mounted at $wb_mount and none in" \
         "the image; start it with wb up or wb deploy from a Workbench checkout" >&2
    return 1
  fi
  if [ "$1" = copy ] && [ -n "$wb_release" ] && [ "$WB_CODE" = "$wb_state/releases/$wb_release" ]; then
    printf '%s\n' "$wb_release" > "$wb_state/releases/last.part" && mv "$wb_state/releases/last.part" \
      "$wb_state/releases/last" || true
  fi
  PYTHONPATH="$WB_CODE/sdk/python:$WB_CODE/core:$WB_CODE/host"
  if [ -f "$WB_CODE/RELEASE.json" ]; then
    WB_COMMIT="$("$wb_python" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("commit") or "")' \
                 "$WB_CODE/RELEASE.json" 2>/dev/null)" || WB_COMMIT=""
  elif [ "$WB_CODE" = "$wb_mount" ]; then
    WB_COMMIT="${WB_SOURCE_COMMIT:-}"
  fi  # code in an older image: the WB_COMMIT it was built with
  export WB_CODE PYTHONPATH WB_COMMIT
}
