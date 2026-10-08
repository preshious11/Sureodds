"""Technical payload/work bounds, independent of the number of ticket selections."""
from dataclasses import asdict
import json

MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 100_000
PROCESSING_BATCH_BYTES = 28_000


class ResourceLimit(ValueError):
    """Fixed, private-data-free technical reason."""


def validate_tree(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ResourceLimit('ticket_parser_node_limit')
        if depth > MAX_JSON_DEPTH:
            raise ResourceLimit('ticket_parser_depth_limit')
        if isinstance(item, (dict, list)):
            children = item.values() if isinstance(item, dict) else item
            # Count pending work too, before constructing a potentially huge stack.
            if nodes + len(stack) + len(item) > MAX_JSON_NODES:
                raise ResourceLimit('ticket_parser_node_limit')
            stack.extend((child, depth + 1) for child in children)


def validate_payload(value):
    """Strings are bounded before JSON parsing; objects before serialization."""
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_JSON_BYTES:
            raise ResourceLimit('ticket_input_too_large')
        try:
            value = json.loads(value)
        except RecursionError:
            raise ResourceLimit('ticket_parser_depth_limit') from None
    validate_tree(value)
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > MAX_JSON_BYTES:
        raise ResourceLimit('ticket_input_too_large')
    return value


def selection_batches(selections):
    """Order-preserving local processing batches measured by normalized payload bytes.

    A single already-validated selection can be handled locally by itself; model
    batching has separate prompt/output bounds and never sends oversized evidence.
    """
    batch, size, total = [], 0, 0
    for selection in selections:
        weight = len(json.dumps(asdict(selection), ensure_ascii=True).encode())
        total += weight
        if total > MAX_JSON_BYTES:
            raise ResourceLimit('ticket_input_too_large')
        if batch and size + weight > PROCESSING_BATCH_BYTES:
            yield tuple(batch)
            batch, size = [], 0
        batch.append(selection)
        size += weight
    if batch:
        yield tuple(batch)
