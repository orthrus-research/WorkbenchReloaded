# jar-audit

Check every class in JARs for its class file version, names and forbidden references. Use it
when a JAR must target one Java release and must not refer to certain packages.

```sh
orthrus-tools jar-audit build/libs/core.jar --release 26 --deny net.minecraft --deny org.lwjgl
orthrus-tools jar-audit                    # the JARs listed in orthrus.toml
```

Classes are read with [classfile](classfile.md); nothing is loaded or run. Each finding has
an `entry`, a `kind` and a `detail`:

| Kind | Meaning |
| --- | --- |
| `version` | the class file version is not the release's (`--release N` means N + 44); an entry under `META-INF/versions/N/` must be N's |
| `preview` | compiled with preview features (minor 65535), unless `--allow-preview` |
| `malformed` | not a class file, or truncated |
| `name` | the entry's path is not the class it declares |
| `duplicate` | the archive holds two entries with one name |
| `reference` | refers to a `--deny` package, in its constant pool classes or in a descriptor or generic signature (field, method, record, annotation and local variable types alike) |
| `unreadable`, `limit` | not a ZIP, or past the size limits (100,000 entries, 16 MiB per class, 1 GiB of classes) |

Classes inside a denied package may refer to that package. A string literal is not a
reference: one that names a denied class is listed under `stringMentions` as a note, for a
separate look at reflection.

Without `--release` the version is not checked. With `--json`, `result.jars` lists each JAR's
`classes`, `versions` (count per `major.minor`), `findings` and `stringMentions`, and
`result.findings` is the total. Exit 1 when there is any finding.

```toml
[jar-audit]
jars = ["build/libs/*.jar"]
release = 21
deny = ["net.minecraft", "org.lwjgl"]
allow_preview = false
```

The audit reads structure, not behaviour: it does not verify bytecode, resolve the class
hierarchy, or find references made by reflection or resources.
