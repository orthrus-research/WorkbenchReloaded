#!/bin/bash
# The entry script of the example template (../tours.toml): one game instance prepared and its tours played, on a
# machine bootstrap.sh made ready. The bundle folder holds the input archives app.tar, engine.tar, pins.tar and
# cache.tar (vast.lab-inputs builds them), jars/, the tours, and plan.env, which the template's [steps] table writes:
#   NAME, SEED, SCALE   the instance and its world
#   PREPARE_ARGS        further prepare arguments (a jar as /root/bundle/jars/<file>)
#   TOURS               the tour files in the bundle, played in order
#   LABELS              a label for each tour
#   OPTIONS_<n>         JVM options for tour n (from 1)
#   WINDOW              the client's window, e.g. 1920x1080
#   GPU                 1 to draw through VirtualGL on the GPU, 0 for software OpenGL (a local check)
# Results go to $BUNDLE/results: each tour's play log, the client's and the server's logs, its captures, and the
# GPU's state sampled every second; results.tar packs them. Lines starting "RUN " are what the rental reports.
set -uo pipefail
BUNDLE=${1:?bundle directory}
# shellcheck disable=SC1091
source "$BUNDLE/plan.env"
LAB=/root/lab
log() { echo "RUN $*"; }
mkdir -p "$LAB/app" "$LAB/engine" "$BUNDLE/results"
if [ ! -f "$LAB/.unpacked" ]; then
  tar -xf "$BUNDLE/app.tar" -C "$LAB/app"
  tar -xf "$BUNDLE/engine.tar" -C "$LAB/engine"
  tar -xf "$BUNDLE/pins.tar" -C "$LAB/app"
  mkdir -p "$LAB/app/state/cache" && tar -xf "$BUNDLE/cache.tar" -C "$LAB/app/state/cache"
  printf '{"java": "/opt/jdk/26/bin/java"}\n' > "$LAB/app/state/settings.json"
  touch "$LAB/.unpacked"
fi
cd "$LAB/app"
instance="state/instances/$NAME"
if [ ! -d "$instance" ]; then
  log "prepare $NAME"
  # shellcheck disable=SC2086
  python3 tools/launcher.py prepare --name "$NAME" --seed "$SEED" --scale "$SCALE" $PREPARE_ARGS 2>&1 | tail -5
fi
if ! pgrep -x Xvfb >/dev/null; then
  Xvfb :1 -screen 0 3840x2160x24 >/dev/null 2>&1 &
  sleep 3
fi
export DISPLAY=:1
WRAP=""
if [ "${GPU:-0}" = "1" ]; then
  WRAP="/opt/VirtualGL/bin/vglrun -d egl"
  nvidia-smi --query-gpu=name,driver_version,memory.total,pcie.link.gen.current,pcie.link.width.current --format=csv \
    > "$BUNDLE/results/gpu.txt" 2>&1
fi
read -ra tours <<< "$TOURS"
read -ra labels <<< "$LABELS"
for i in "${!tours[@]}"; do
  n=$((i + 1)); label=${labels[$i]}; var="OPTIONS_$n"
  log "tour $n ${tours[$i]} as $label"
  sampler=""
  if [ "${GPU:-0}" = "1" ]; then
    nvidia-smi --query-gpu=timestamp,pstate,clocks.sm,clocks.mem,power.draw,temperature.gpu,utilization.gpu,memory.used \
      --format=csv -l 1 > "$BUNDLE/results/$label-gpu.csv" 2>&1 &
    sampler=$!
  fi
  # shellcheck disable=SC2086
  JAVA_TOOL_OPTIONS="${!var:-}" $WRAP python3 tools/launcher.py play --name "$NAME" --tour "$BUNDLE/${tours[$i]}" \
    --window "$WINDOW" > "$BUNDLE/results/$label-play.log" 2>&1
  code=$?
  [ -n "$sampler" ] && kill "$sampler" 2>/dev/null
  log "tour $n exit $code"
  cp "$instance/client/logs/latest.log" "$BUNDLE/results/$label-client-latest.log" 2>/dev/null
  server_log=$(find "$instance/server" -name latest.log | head -1)
  [ -n "$server_log" ] && cp "$server_log" "$BUNDLE/results/$label-server-latest.log"
  captures=$(grep -o 'Tour captures: .*' "$BUNDLE/results/$label-play.log" | tail -1 | cut -d' ' -f3-)
  [ -n "$captures" ] && [ -d "$captures" ] && cp -r "$captures" "$BUNDLE/results/$label-captures"
  [ "$code" -ne 0 ] && break
done
tar -cf "$BUNDLE/results.tar" -C "$BUNDLE" results
log "done; results.tar $(du -h "$BUNDLE/results.tar" | cut -f1)"
