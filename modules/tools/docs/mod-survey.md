# orthrus-mod-survey

Lists, for every Minecraft 1.12.2 mod JAR in a folder, which OpenGL calls and which of the
game's rendering classes it refers to. Use it to see which mods in a pack can disturb a
renderer's state before testing with them.

## Use

```sh
orthrus-mod-survey path/to/mods --out survey.json   # full per-mod result in a file
orthrus-mod-survey path/to/mods --json              # the summary as one JSON object
orthrus-mod-survey path/to/mods --mod celeritas --mod optifine
orthrus-mod-survey one-mod.jar
```

The report has three parts:

- **Calls:** for each category (depth function, blend function, own shader programs, own
  framebuffers, immediate mode, ...), how many mods have a class that calls it directly or
  through the game's `GlStateManager` and `OpenGlHelper`, by SRG and MCP names.
- **Patches:** which rendering classes (`EntityRenderer`, `RenderGlobal`, `BufferBuilder`, ...)
  a Mixin or a class transformer names.
- **Depth leaving the depth buffer:** classes with a method that both makes a depth read, copy
  or attachment call and names a depth constant.

JARs nested inside a mod (for example under `META-INF/jarjar`) count toward that mod, up to
three levels deep.

## Limits

Nothing is executed and no source is read: the survey sees only the names in each class's
constant pool. A reference is not proof of use at runtime, and call arguments are not visible.
The depth heuristic looks for instruction bytes in one method body without decoding the
bytecode, so it can pair an unrelated call and constant. Classes that fail to parse are counted
under `unreadable`.
