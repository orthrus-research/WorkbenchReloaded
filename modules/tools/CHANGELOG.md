# Changelog

Vendored from the former standalone tools at 0.6.0; their earlier history is not part of this
repository. Trimmed to the
tools Workbench runs: the project set-up commands were left out.

## Workbench changes

- The tools are commands of the taxonomy's modules now (`toolchain.jdk`, `jar.audit`, `ci.actions`, ...); the
  former `tools.*` names are aliases. The standalone tools' `frames` is not exposed: `frames.*` covers it.
- `jdk` also searches `/opt/jdk`, where the image keeps its JDKs.
