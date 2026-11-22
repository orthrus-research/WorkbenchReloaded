"""What a Groovy file says, read off its tokens: its package, imports and declared types, every call with its
arguments (in parentheses, as a trailing closure, or as a command expression such as ``crafting.remove 'x'``),
the fluent chains calls make, simple aliases (``def te = mods.te``), and the blocks a mod-loaded check guards.

Nothing is evaluated: an argument is a range of tokens, and only a literal one has a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import KEYWORDS, NAME, NUMBER, OP, STRING, Token

DOTS = (".", "?.", "*.")
_DECLARES = frozenset({"def", "void", "static", "final", "private", "public", "protected", "synchronized",
                       "abstract", "native", "boolean", "int", "long", "short", "byte", "char", "float", "double"})
_VALUES = frozenset({"true", "false", "null", "new", "this", "super"})
_CONTINUES = frozenset({",", "(", "[", "{", "=", ".", "?.", "*.", "?", ":", "->", "+", "-", "*", "/", "%", "&&",
                        "||", "&", "|", "^", "<", ">", "<=", ">=", "==", "!=", "<<", ">>", "+=", "-=", "*=", "/=",
                        "?:", "!", "~", "**", "..", "..<", "=~", "==~", "in", "instanceof", "as"})
_TYPES = frozenset({"class", "interface", "enum", "trait"})
GUARDS = frozenset({"isModLoaded", "isLoaded"})


@dataclass
class Call:
    name: tuple[str, ...]  # the callee as written: ("mods", "te", "pulverizer", "add"); ("add",) for a fluent step
    start: int  # the token of its first name
    end: int  # its last token
    args: list[tuple[int, int]]  # each argument's tokens, [a, b)
    closure: tuple[int, int] | None  # a trailing closure's braces
    chained: bool  # it follows a dot after another expression: a fluent step
    line: int
    constructs: bool = False  # new Name(...)


@dataclass
class Import:
    name: str  # a.b.C, or a.b for a.b.*
    star: bool
    static: bool
    alias: str | None
    line: int


@dataclass
class Guard:
    start: int  # the tokens it covers, [start, end)
    end: int
    mods: frozenset[str]  # mod ids as written: a literal or the name after mods.


@dataclass
class Source:
    tokens: list[Token]
    match: dict[int, int] = field(default_factory=dict)
    problems: list[tuple[int, str]] = field(default_factory=list)
    package: str = ""
    imports: list[Import] = field(default_factory=list)
    types: list[tuple[str, int]] = field(default_factory=list)  # top-level types and their lines
    calls: list[Call] = field(default_factory=list)
    by_start: dict[int, Call] = field(default_factory=dict)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    statics: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)  # Class -> its aliased fields
    guards: list[Guard] = field(default_factory=list)

    def chain(self, head: Call) -> list[Call]:
        """The head call and every fluent step after it: a.b().c().d()."""
        steps, current, tokens = [head], head, self.tokens
        while current.end + 2 < len(tokens) and tokens[current.end + 1].text in DOTS:
            step = self.by_start.get(current.end + 2)
            if step is None or not step.chained:
                break
            steps.append(step)
            current = step
        return steps

    def resolve(self, name: tuple[str, ...]) -> tuple[str, ...]:
        """A callee with aliases expanded: te.pulverizer.add, after def te = mods.te, is mods.te.pulverizer.add;
        Maps.MIXER.recipeBuilder, where class Maps holds MIXER = mods.gregtech.mixer, is mods.gregtech.mixer..."""
        seen: set = set()
        while name:
            head = name[0]
            if head in self.aliases and head not in seen:
                seen.add(head)
                name = self.aliases[head] + name[1:]
            elif len(name) > 1 and name[1] in self.statics.get(head, {}) and (head, name[1]) not in seen:
                seen.add((head, name[1]))
                name = self.statics[head][name[1]] + name[2:]
            else:
                break
        return name

    def literal(self, span: tuple[int, int]) -> str | None:
        """An argument's value when it is one string literal without interpolation."""
        a, b = span
        if b - a == 1 and self.tokens[a].kind == STRING:
            return self.tokens[a].value
        return None

    def integer(self, span: tuple[int, int]) -> int | None:
        a, b = span
        sign = 1
        if b - a == 2 and self.tokens[a].text == "-":
            sign, a = -1, a + 1
        if b - a != 1 or self.tokens[a].kind != NUMBER:
            return None
        text = self.tokens[a].text.replace("_", "").rstrip("lLgGiI")
        try:
            return sign * int(text, 0)
        except ValueError:
            return None

    def inner(self, span: tuple[int, int]) -> Call | None:
        """The call an argument starts with: item('a:b') in item('a:b') * 2."""
        call = self.by_start.get(span[0])
        return call if call is not None and call.end < span[1] else None

    def guarded(self, index: int) -> set[str]:
        return {mod for guard in self.guards if guard.start <= index < guard.end for mod in guard.mods}

    def starts_statement(self, index: int) -> bool:
        """Whether the expression at ``index`` begins a statement (its value is not used)."""
        if index == 0:
            return True
        before, here = self.tokens[index - 1], self.tokens[index]
        if before.text in (";", "{", "}") or before.text in ("else", "do"):
            return True
        if before.text == ")":
            opening = self.match.get(index - 1)
            if opening is not None and opening > 0 and self.tokens[opening - 1].text in ("if", "for", "while"):
                return True
        return before.line < here.line and before.text not in _CONTINUES


def read(tokens: list[Token]) -> Source:
    source = Source(tokens)
    source.match = _brackets(tokens, source.problems)
    _heading(source)
    source.calls = _calls(source)
    source.by_start = {call.start: call for call in source.calls}
    source.aliases = _aliases(source)
    source.guards = _guards(source)
    return source


def _brackets(tokens: list[Token], problems: list) -> dict[int, int]:
    pairs, stack = {}, []
    closing = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if token.kind != OP:
            continue
        if token.text in ("(", "[", "{"):
            stack.append(index)
        elif token.text in closing:
            while stack and tokens[stack[-1]].text != closing[token.text]:
                problems.append((tokens[stack[-1]].line, f"{tokens[stack[-1]].text!r} is not closed"))
                stack.pop()
            if not stack:
                problems.append((token.line, f"{token.text!r} closes nothing"))
                continue
            opening = stack.pop()
            pairs[opening], pairs[index] = index, opening
    for index in stack:
        problems.append((tokens[index].line, f"{tokens[index].text!r} is not closed"))
    return pairs


def _dotted(tokens: list[Token], index: int) -> tuple[str, int]:
    """The dotted name starting at ``index`` (a.b.C) and the index after it."""
    parts = [tokens[index].text]
    index += 1
    while index + 1 < len(tokens) and tokens[index].text == "." and tokens[index + 1].kind == NAME:
        parts.append(tokens[index + 1].text)
        index += 2
    return ".".join(parts), index


def _heading(source: Source) -> None:
    tokens = source.tokens
    depth = 0
    for index, token in enumerate(tokens):
        if token.text == "{" and token.kind == OP:
            depth += 1
        elif token.text == "}" and token.kind == OP:
            depth = max(0, depth - 1)
        if token.kind != NAME or depth:
            continue
        if token.text == "package" and index + 1 < len(tokens) and tokens[index + 1].kind == NAME:
            if not source.package:
                source.package = _dotted(tokens, index + 1)[0]
        elif token.text == "import" and source.starts_statement(index) and index + 1 < len(tokens):
            at = index + 1
            static = tokens[at].text == "static"
            at += static
            if at >= len(tokens) or tokens[at].kind != NAME:
                continue
            name, at = _dotted(tokens, at)
            star = at + 1 < len(tokens) and tokens[at].text == "." and tokens[at + 1].text == "*"
            at += 2 if star else 0
            alias = tokens[at + 1].text if at + 1 < len(tokens) and tokens[at].text == "as" else None
            source.imports.append(Import(name, star, static, alias, token.line))
        elif token.text in _TYPES and index + 1 < len(tokens) and tokens[index + 1].kind == NAME and (
                index == 0 or tokens[index - 1].text not in DOTS + ("::",)):
            source.types.append((tokens[index + 1].text, token.line))


def _callee(tokens: list[Token], index: int) -> tuple[tuple[str, ...], int, bool]:
    parts, start = [tokens[index].text], index
    while start >= 2 and tokens[start - 1].text in DOTS and tokens[start - 2].kind == NAME and (
            tokens[start - 2].text not in KEYWORDS or tokens[start - 2].text in ("this", "super")):
        parts.insert(0, tokens[start - 2].text)
        start -= 2
    chained = start >= 1 and tokens[start - 1].text in DOTS
    return tuple(parts), start, chained


def _split(source: Source, a: int, b: int) -> list[tuple[int, int]]:
    """The arguments between token ``a`` and ``b`` (exclusive), split at top-level commas."""
    if a >= b:
        return []
    out, start, index = [], a, a
    while index < b:
        token = source.tokens[index]
        if token.text in ("(", "[", "{") and index in source.match:
            index = source.match[index] + 1
            continue
        if token.text == ",":
            out.append((start, index))
            start = index + 1
        index += 1
    out.append((start, b))
    return [span for span in out if span[0] < span[1]]


def _calls(source: Source) -> list[Call]:
    tokens, match = source.tokens, source.match
    out = []
    for index, token in enumerate(tokens):
        if token.kind != NAME or token.text in KEYWORDS or index + 1 >= len(tokens):
            continue
        following = tokens[index + 1]
        name, start, chained = _callee(tokens, index)
        before = tokens[start - 1] if start > 0 else None
        if before is not None and before.text in ("@", "class", "interface", "enum", "trait"):
            continue
        constructs = before is not None and before.text == "new"
        if following.text == "(" and index + 1 in match:
            close = match[index + 1]
            braced = close + 1 < len(tokens) and tokens[close + 1].text == "{" and close + 1 in match and \
                tokens[close + 1].line == tokens[close].line
            if braced and before is not None and not chained and not constructs and (
                    before.text in _DECLARES or (before.kind == NAME and before.text not in KEYWORDS)
                    or before.text in (">", "]")):
                continue  # a method declaration: def name(...) { ... }
            closure = (close + 1, match[close + 1]) if braced and not constructs else None
            end = closure[1] if closure else close
            out.append(Call(name, start, end, _split(source, index + 2, close), closure, chained, tokens[start].line,
                            constructs))
        elif constructs:
            continue
        elif following.text == "{" and index + 1 in match and (chained or len(name) > 1) and \
                following.line == token.line:
            closure = (index + 1, match[index + 1])
            out.append(Call(name, start, closure[1], [], closure, chained, tokens[start].line))
        elif len(name) > 1 and not chained and following.line == token.line and _argument_start(tokens, index + 1) \
                and source.starts_statement(start):
            end = _command_end(source, index + 1)
            out.append(Call(name, start, end - 1, _split(source, index + 1, end), None, False, tokens[start].line))
    return out


def _argument_start(tokens: list[Token], index: int) -> bool:
    token = tokens[index]
    if token.kind in (STRING, NUMBER):
        return index + 1 >= len(tokens) or tokens[index + 1].text != "="
    if token.kind == NAME:
        if token.text in KEYWORDS and token.text not in _VALUES:
            return False
        return index + 1 < len(tokens) and tokens[index + 1].text in ("(", ",", ".") and tokens[index + 1].line == \
            token.line
    return token.text == "["


def _command_end(source: Source, index: int) -> int:
    """Where a command expression's arguments end: the end of its line (or of a line ending in a comma)."""
    tokens = source.tokens
    while index < len(tokens):
        token = tokens[index]
        if token.text in (";", "}", ")", "]") and token.kind == OP:
            return index
        if token.text in ("(", "[", "{") and index in source.match:
            index = source.match[index] + 1
        else:
            index += 1
        if index < len(tokens) and tokens[index].line != tokens[index - 1].line and tokens[index - 1].text != ",":
            return index
    return index


def _aliases(source: Source) -> dict[str, tuple[str, ...]]:
    """Names bound to a dotted path that is not called: def te = mods.thermalexpansion."""
    tokens, out = source.tokens, {}
    for index in range(len(tokens) - 2):
        if tokens[index].kind != NAME or tokens[index + 1].text != "=" or tokens[index + 2].kind != NAME:
            continue
        if tokens[index].text in KEYWORDS or (index and tokens[index - 1].text in DOTS):
            continue
        path, end = _dotted(tokens, index + 2)
        if "." not in path:
            continue
        if end < len(tokens) and tokens[end].line == tokens[end - 1].line and tokens[end].text not in (";", "}"):
            continue  # more follows on the line: a call or an expression, not a plain path
        out[tokens[index].text] = tuple(path.split("."))
    return out


def _statement(source: Source, index: int) -> tuple[int, int]:
    """The tokens of the statement starting at ``index``: a braced block or one line."""
    tokens = source.tokens
    if index < len(tokens) and tokens[index].text == "{" and index in source.match:
        return index, source.match[index] + 1
    end = index
    while end < len(tokens) and tokens[end].text not in (";", "}"):
        last = source.match[end] if tokens[end].text in ("(", "[", "{") and end in source.match else end
        end = last + 1
        if end < len(tokens) and tokens[end].line != tokens[last].line and tokens[last].text not in _CONTINUES:
            break
    return index, end


def _enclosing_end(source: Source, index: int) -> int:
    """The end of the block that holds token ``index`` (the file's end at the top level)."""
    depth = 0
    for at in range(index, len(source.tokens)):
        text = source.tokens[at].text
        if text == "{":
            depth += 1
        elif text == "}":
            if depth == 0:
                return at
            depth -= 1
    return len(source.tokens)


def _guards(source: Source) -> list[Guard]:
    tokens, out = source.tokens, []
    for index, token in enumerate(tokens):
        if token.text != "if" or token.kind != NAME or index + 1 >= len(tokens) or tokens[index + 1].text != "(":
            continue
        close = source.match.get(index + 1)
        if close is None:
            continue
        held, negated = set(), set()
        for call in source.calls:
            if index + 1 < call.start < close and call.name[-1] in GUARDS:
                mod = source.literal(call.args[0]) if call.args else None
                if mod is None and len(call.name) == 3 and call.name[0] == "mods":
                    mod = call.name[1]
                if mod:
                    (negated if tokens[call.start - 1].text == "!" else held).add(mod)
        if not held and not negated:
            continue
        body = _statement(source, close + 1)
        if held:
            out.append(Guard(body[0], body[1], frozenset(held)))
        if negated:
            after = body[1]
            if after < len(tokens) and tokens[after].text == "else":
                otherwise = _statement(source, after + 1)
                out.append(Guard(otherwise[0], otherwise[1], frozenset(negated)))
            first = body[0] + (tokens[body[0]].text == "{")
            if first < len(tokens) and tokens[first].text == "return":
                out.append(Guard(body[1], _enclosing_end(source, index), frozenset(negated)))
    return out
