# syntax=docker/dockerfile:1.7
# Workbench Reloaded: wb-core (spec 13). Built from the checkout root: `wb build` or
#   docker build -f image/Dockerfile -t wb-core:dev .
# Every input is pinned: the base by digest, apt by a snapshot.ubuntu.com instant, each JDK by SHA-256
# (image/pins.toml, compiled into image/jdks.lock, image/apt-image.txt and image/tools.lock by
# `python image/pins.py lock`).

ARG BASE=ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db
ARG APT_SNAPSHOT=20261120T000000Z

FROM ${BASE} AS jdks
ARG APT_SNAPSHOT
# snapshot.ubuntu.com is HTTPS only, so the trust store comes first, from the live archive; every package
# after it is read from the pinned snapshot.
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null \
 && echo "APT::Snapshot \"${APT_SNAPSHOT}\";" > /etc/apt/apt.conf.d/50wb-snapshot \
 && apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends wget >/dev/null \
 && rm -rf /var/lib/apt/lists/*
COPY image/jdks.lock /tmp/jdks.lock
RUN set -eu; mkdir -p /opt/jdk; \
    grep -v '^#' /tmp/jdks.lock | while read -r key sha url; do \
      [ -n "$key" ] || continue; \
      wget -q -O "/tmp/jdk-$key.tar.gz" "$url"; \
      echo "$sha  /tmp/jdk-$key.tar.gz" | sha256sum -c - >/dev/null; \
      mkdir -p "/opt/jdk/$key"; \
      tar -xzf "/tmp/jdk-$key.tar.gz" -C "/opt/jdk/$key" --strip-components=1; \
      rm "/tmp/jdk-$key.tar.gz"; \
      rm -rf "/opt/jdk/$key/lib/src.zip" "/opt/jdk/$key/src.zip" "/opt/jdk/$key/demo" "/opt/jdk/$key/sample"; \
      echo "jdk $key: $("/opt/jdk/$key/bin/java" -version 2>&1 | head -1)"; \
    done

# Optional tools (image/pins.toml [tool.*], compiled into image/tools.lock): only those TOOLS names, each checked by
# its SHA-256 and its programs linked into /opt/tools/bin. `wb build` fills TOOLS from the machine's
# [core.image] tools; without it, /opt/tools/bin is empty.
FROM ${BASE} AS tools
ARG APT_SNAPSHOT
ARG TOOLS=""
COPY image/tools.lock /tmp/tools.lock
RUN set -eu; mkdir -p /opt/tools/bin; \
    if [ -z "$TOOLS" ]; then exit 0; fi; \
    apt-get update -qq; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null; \
    echo "APT::Snapshot \"${APT_SNAPSHOT}\";" > /etc/apt/apt.conf.d/50wb-snapshot; \
    apt-get update -qq; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends wget xz-utils >/dev/null; \
    for name in $(echo "$TOOLS" | tr ',' ' '); do \
      line=$(grep -v '^#' /tmp/tools.lock | awk -v n="$name" '$1 == n'); \
      if [ -z "$line" ]; then echo "no tool $name in image/tools.lock" >&2; exit 1; fi; \
      set -- $line; \
      wget -q -O /tmp/tool.archive "$3"; \
      echo "$2  /tmp/tool.archive" | sha256sum -c - >/dev/null; \
      mkdir -p "/opt/tools/$name"; \
      tar -xf /tmp/tool.archive -C "/opt/tools/$name" --strip-components=1; \
      rm /tmp/tool.archive; \
      for folder in $(echo "$4" | tr ',' ' '); do \
        for program in "/opt/tools/$name/$folder"/*; do \
          if [ -f "$program" ] && [ -x "$program" ]; then ln -sf "$program" /opt/tools/bin/; fi; \
        done; \
      done; \
      echo "tool $name: $(ls /opt/tools/bin | tr '\n' ' ')"; \
    done; \
    rm -rf /var/lib/apt/lists/*

FROM ${BASE}
ARG APT_SNAPSHOT
LABEL org.opencontainers.image.title="wb-core" \
      org.opencontainers.image.description="Workbench Reloaded daemon"
COPY image/apt-image.txt /tmp/apt-image.txt
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null \
 && dpkg-query -W ca-certificates \
 && echo "APT::Snapshot \"${APT_SNAPSHOT}\";" > /etc/apt/apt.conf.d/50wb-snapshot \
 && apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      $(grep -v '^#' /tmp/apt-image.txt | tr '\n' ' ') >/dev/null \
 && rm -rf /var/lib/apt/lists/* /tmp/apt-image.txt
COPY --from=jdks /opt/jdk /opt/jdk
COPY --from=tools /opt/tools /opt/tools
RUN useradd --create-home --uid 1000 --shell /bin/bash wb 2>/dev/null || useradd --create-home --shell /bin/bash wb; \
    mkdir -p /var/lib/wb /etc/wb && chown wb:wb /var/lib/wb
COPY --chown=wb:wb sdk/python /opt/wb/src/sdk/python
COPY --chown=wb:wb core /opt/wb/src/core
COPY --chown=wb:wb host /opt/wb/src/host
COPY --chown=wb:wb modules /opt/wb/src/modules
COPY --chown=wb:wb schema /opt/wb/src/schema
COPY --chown=wb:wb image /opt/wb/src/image
COPY --chown=wb:wb config.example /opt/wb/src/config.example
COPY image/entrypoint.sh /opt/wb/entrypoint.sh
# Mounted checkouts belong to the host's user; Workbench only reads them (ci clones them first).
RUN git config --system --add safe.directory '*'
RUN printf '#!/bin/sh\nexec python3 -m wb.cli "$@"\n' > /usr/local/bin/wb && chmod +x /usr/local/bin/wb /opt/wb/entrypoint.sh \
 && find /opt/wb/src -name '__pycache__' -prune -exec rm -rf {} + \
 && PYTHONPATH=/opt/wb/src/sdk/python:/opt/wb/src/core python3 -c "import wb, wb_core.service" \
 && du -sh /opt/jdk | sed 's/^/jdks: /'
ENV PYTHONPATH=/opt/wb/src/sdk/python:/opt/wb/src/core:/opt/wb/src/host \
    PATH=/opt/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    WB_CONFIG=/etc/wb \
    WB_IN_CONTAINER=1 \
    WB_STATE_DIR=/var/lib/wb \
    JAVA_HOME=/opt/jdk/26 \
    JAVA_HOME_8_X64=/opt/jdk/8 \
    JAVA_HOME_17_X64=/opt/jdk/17 \
    JAVA_HOME_25_X64=/opt/jdk/25 \
    JAVA_HOME_26_X64=/opt/jdk/26 \
    JAVA_HOME_27_X64=/opt/jdk/27
# No USER: the entrypoint drops to the host's user (WB_UID, WB_GID; default 1000) before Workbench starts.
WORKDIR /var/lib/wb
VOLUME ["/var/lib/wb"]
EXPOSE 7377
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD \
  python3 -c "import urllib.request as u;t=open('/run/secrets/wb_token').read().strip();r=u.Request('http://127.0.0.1:7377/v1/health',headers={'Authorization':'Bearer '+t});u.urlopen(r,timeout=4)" || exit 1
ENTRYPOINT ["/opt/wb/entrypoint.sh", "python3", "-m", "wb_core"]
