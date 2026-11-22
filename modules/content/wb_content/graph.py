"""Strongly connected components of a directed graph (iterative Tarjan), and one cycle through a component,
for import cycles between scripts and cycles between items."""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Iterable, Mapping


def components(nodes: Iterable[Hashable], edges: Mapping[Hashable, Iterable[Hashable]]) -> list[list]:
    """Every strongly connected component of two or more nodes, each sorted, the largest first."""
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    found: list[list] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()), key=str)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ()), key=str))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component, key=str))
    return sorted(found, key=lambda c: (-len(c), [str(m) for m in c]))


def cycle_in(component: list, edges: Mapping[Hashable, Iterable[Hashable]]) -> list:
    """A shortest cycle through the component's first node, as nodes from it back to it."""
    members = set(component)
    start = component[0]
    previous = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for child in sorted(edges.get(node, ()), key=str):
            if child == start:
                path = [node]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                return list(reversed(path)) + [start]
            if child in members and child not in previous:
                previous[child] = node
                queue.append(child)
    return [start]
