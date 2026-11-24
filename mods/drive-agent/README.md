# The drive agent

A small client mod for Minecraft 1.12.2 on Cleanroom that a session puts beside the game's own mods. It changes
nothing in the world and does nothing unless a system property turns a part of it on.

**Capture** (`-Dwb.capture=true`): a screenshot once the player has held still for `wb.capture.settleTicks` (400);
with `-Dwb.capture.tour=true`, one at every new viewpoint after the settle, so a session steps through views by
teleporting. Each capture logs marker lines (`WB VIEW n fps=.. avgFps=.. heap=..MB`, `WB CAPTURE n <path>`,
`WB HEAP AFTER GC ..MB`). Shots go to `screenshots/` of `wb.capture.dir`, by default the game folder.

**Driver** (`-Dwb.driver=true`): requests a session writes to `wb.driver.inbox` (by default `<game>/wb-inbox`) are
answered on the game's thread in `wb.driver.outbox` (`<game>/wb-outbox`): `status`, `screenshot`, `chat`, `key`
(through the game's key bindings), `click` (on the open screen), `close` and `quit`. `wb run drive.send inbox=...
action=status` sends one and waits for its answer.

Build it with its wrapper (JDK 26 runs Gradle; the bytecode is Java 21, the oldest Java Cleanroom runs on):

```
cd mods/drive-agent
./gradlew build          # build/libs/wbdrive-<version>.jar, remapped for the game
```

Give it to a session as a member of the launch it plays (once its JAR is in the Store, a third entry or a
project's artifact), with the parts it should run as the session's JVM options:

```
wb play <project>:<launch> --jvm client:-Dwb.driver=true --jvm client:-Dwb.capture=true
```
