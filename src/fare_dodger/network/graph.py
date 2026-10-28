"""Static graph helpers."""

from __future__ import annotations


def strongly_connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """Iterative Tarjan; components are returned with sorted members."""
    adjacency: list[list[int]] = [[] for _ in range(n)]
    for a, b in edges:
        adjacency[a].append(b)

    index = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0

    for root in range(n):
        if index[root] != -1:
            continue
        work: list[tuple[int, int]] = [(root, 0)]
        while work:
            node, child_i = work.pop()
            if child_i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            recurse = False
            for i in range(child_i, len(adjacency[node])):
                nxt = adjacency[node][i]
                if index[nxt] == -1:
                    work.append((node, i + 1))
                    work.append((nxt, 0))
                    recurse = True
                    break
                if on_stack[nxt]:
                    low[node] = min(low[node], index[nxt])
            if recurse:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components
