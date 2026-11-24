"""Workbench's interim copy of Cubature's standalone base: the fallback for a set whose Cubature line has no
``[launch.standalone]`` yet.

A base is what every instance of a runtime starts from before its members are placed: how the server is installed,
the files the server and client folders get, the JVM profile, the player, and the command lines that start each side.
A launch names one with ``base = '<project>:<name>'``, and lab.resolve reads that project's ``[launch.<name>]`` (with
its ``launch/`` overlay folder) at the commit the set names for it, into launch.json. A launched product's own changes
are never a base's: they are its configure script.

The one base here, ``cubature:standalone``, is what Cubature's standalone world writes (``tools/cubic_standalone.py``,
with the JVM profile and client options of ``tools/lab_profiles.py`` and the command lines of ``tools/lab_process.py``),
which Cubature's project.toml now carries as ``[launch.standalone]``. lab.resolve takes it from here only when the
base project's commit has no such table, and says so as a warning; this table is deleted once every set line in use
carries it. Its keys are a base's (``wb_lab.resolve.BASE_KEYS``), with the files as data where Cubature's are an
overlay folder.

Placeholders, filled by the builder:

- in file texts: ``{port}``, ``{seed}``, ``{player}``, ``{player_uuid}``;
- in ``installer``: ``{java}``, ``{installer}``, ``{target}``, ``{version}``, ``{memory}``;
- in commands: ``{java}``, ``{game}`` (the side's folder), ``{cache}``, ``{version}``, ``{classpath}``,
  ``{main_class}``, ``{player}``, ``{player_uuid}``, ``{assets}``, ``{asset_index}``, ``{port}``, ``{args_file}``,
  and three that stand alone and are replaced by a list: ``{jvm}`` (the launch's JVM arguments for the side),
  ``{profile}`` (the base's JVM profile) and ``{tweakers}`` (``--tweakClass <name>`` per tweaker the runtime names).
"""

from __future__ import annotations

# Cubature's lab configuration both sides share (the standalone world copies the server's config to the client).
_CUBIC_CHUNKS = """general {
    S:forceLoadCubicChunks=NEW_WORLD
    I:defaultMinHeight=-128
    I:defaultMaxHeight=384
    I:verticalCubeLoadDistance=12
    I:maxCubeSendTimeMillis=0
    I:maxCubeSendBytesPerTick=0
    I:maxCubePacketBytes=0
}
"""

_SERVER_PROPERTIES = """server-ip=127.0.0.1
server-port={port}
online-mode=false
level-name=world
level-type=DEFAULT
level-seed={seed}
view-distance=12
gamemode=1
difficulty=0
allow-flight=true
max-players=1
max-tick-time=-1
enable-command-block=false
enable-rcon=false
motd=CubicChunks only - local performance
"""

_OPS = """[
  {
    "uuid": "{player_uuid}",
    "name": "{player}",
    "level": 4,
    "bypassesPlayerLimit": false
  }
]
"""

_OPTIONS = """renderDistance:12
maxFps:120
enableVsync:false
fancyGraphics:false
renderClouds:false
fullscreen:false
pauseOnLostFocus:false
showSubtitles:false
tutorialStep:none
"""

INTERIM_BASES: dict[str, dict] = {
    "cubature:standalone": {
        "summary": "Cubature's standalone world: a local creative server on 127.0.0.1 and one offline client",
        "runtime": "cleanroom",
        "player": "CubicLab",
        "installer": ["{java}", "-jar", "{installer}", "server", "--dir", "{target}", "--version", "{version}",
                      "--no-gui", "--yes", "--no-provision-java", "--java", "{java}", "--memory", "{memory}"],
        "memory": "8G",
        # The lab's zgc-fixed profile: ZGC on a fixed 8 GiB heap, the same on server and client.
        "jvm_profile": {"name": "zgc-fixed", "collector": "ZGC", "initialHeapMiB": 8192, "maxHeapMiB": 8192,
                        "arguments": ["-Xms8192M", "-Xmx8G", "-XX:+UseZGC"]},
        # The installer's argument file is read after the command line, so the profile goes there too.
        "jvm_args_file": "# Owned lab profile: same heap and collector on server and client.\n",
        "files": {
            "both": {"config/forge_early.cfg": "general {\n    B:DISABLE_PATCH_MOD_CHECK=true\n}\n",
                     "config/cubicchunks.cfg": _CUBIC_CHUNKS},
            "server": {"server.properties": _SERVER_PROPERTIES, "ops.json": _OPS},
            "client": {"options.txt": _OPTIONS},
        },
        # A session's view distances (lab.run view=...): each key sets these <side>/<file>:<key> lines.
        "views": {"horizontal": ["server/server.properties:view-distance"],
                  "vertical": ["server/config/cubicchunks.cfg:I:verticalCubeLoadDistance",
                               "client/config/cubicchunks.cfg:I:verticalCubeLoadDistance"],
                  "client_render": ["client/options.txt:renderDistance"]},
        # Written only when the request accepts the Minecraft EULA for the instance (lab.build eula=true).
        "eula": "eula=true\n",
        "server": ["{java}", "{jvm}", "{profile}", "-Djava.io.tmpdir={game}/java-tmp", "-Dfile.encoding=UTF-8",
                   "-Djava.awt.headless=true", "@user_jvm_args.txt",
                   "@{game}/libraries/com/cleanroommc/cleanroom/{version}/{args_file}", "nogui"],
        "client": ["{java}", "{jvm}", "{profile}", "-Djava.io.tmpdir={game}/java-tmp", "-Dfile.encoding=UTF-8",
                   "--enable-native-access=ALL-UNNAMED", "-Djava.library.path={game}/natives", "-cp", "{classpath}",
                   "{main_class}", "{tweakers}", "--username", "{player}", "--uuid", "{player_uuid}",
                   "--accessToken", "0", "--userType", "legacy", "--versionType", "custom",
                   "--version", "Cleanroom-{version}", "--gameDir", "{game}", "--assetsDir", "{assets}",
                   "--assetIndex", "{asset_index}", "--width", "1280", "--height", "720",
                   "--server", "127.0.0.1", "--port", "{port}"],
    },
}
