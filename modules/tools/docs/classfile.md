# classfile

`orthrus_tools.classfile.parse(data)` reads a JVM class file's constant pool and method bodies
without loading or running it. It is the reader behind `mod-survey`, and the place to start for
any other check over compiled classes or JARs.

```python
from orthrus_tools.classfile import parse

parsed = parse(class_bytes)
parsed.class_names  # every class the constant pool names
parsed.methods  # (owner, name) of every field or method referenced
parsed.strings  # every UTF-8 constant
parsed.complete  # False when the input was truncated or malformed
```

Only what the survey needs is decoded: UTF-8 strings, class names, member references, integer
constants and each method's `Code` attribute. Malformed input never raises: `parse` returns what
it read before the damage and sets `complete` to `False`, so one bad class cannot stop a survey
of thousands.
