# orthrus-strict-math

Keeps deterministic Java code on `StrictMath`. `java.lang.Math` may use CPU- and JDK-specific
intrinsics for transcendental functions (`sin`, `cos`, `exp`, `pow`, `atan2`, `tanh`, `cbrt`,
`log`, ...), which the specification lets differ from `StrictMath` by an ulp. One JVM stays
consistent with itself, so a determinism check on one machine cannot see the difference, but
another CPU or JDK build can generate different output. Projects whose output must be a pure
function of their inputs, such as world generators, need `StrictMath`.

## Use

```sh
orthrus-strict-math --init     # first run in a project: record existing calls
orthrus-strict-math            # in CI and before commits: no new inexact calls
orthrus-strict-math --update   # after converting calls: shrink the baseline
```

The baseline is a ratchet: a file may keep the calls it had, but never add more, and `--update`
only shrinks it. Converting a recorded call to `StrictMath` can change output, so do it in a
commit that also updates the project's golden data.

`Math` methods whose results the specification fixes exactly (`min`, `max`, `floor`, `sqrt`,
`abs`, `fma`, the `*Exact` family, ...) and constants such as `Math.PI` are always allowed.
Mentions inside comments, strings and text blocks are ignored. Static imports from
`java.lang.Math` are refused because they hide which methods are called.

## Settings

```toml
# orthrus.toml
[strict-math]
sources = ["src/main/java", "core/src/main/java"]   # default: ["src/main/java"]
baseline = "tools/strict-math-baseline.json"        # default: strict-math-baseline.json
```

Exit status: 0 clean, 1 new inexact calls or static imports, 2 usage error. With `--json` the
`result` lists `added` and `spare` calls and any `staticImports`.
