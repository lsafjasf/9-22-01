"""Causally correct merging, including bounded-memory paths.

``merge`` keeps the old in-memory call shape.  ``merge_streams`` is a k-way
heap merge over already-ordered streams (O(k) memory).  ``external_merge``
spills large inputs to per-stream sorted chunk files and merges them, so the
working set is bounded for arbitrarily large event counts.
"""

import contextlib
import heapq
import json
import os
import tempfile

from eventorder.clock import order_key


def merge(*streams):
    """Merge events from any number of iterables into one causal total order.

    Drop-in compatible with ``eventorder.legacy.merge`` (accepts lists and
    returns a list) but sorts by the HLC total-order key.
    """
    events = []
    for stream in streams:
        events.extend(stream)
    events.sort(key=order_key)
    return events


def merge_streams(*streams):
    """Streaming k-way merge of HLC-ordered iterables.

    Each stream must already be in ascending ``order_key`` order (as produced
    by one clock).  Memory use is O(number of streams), independent of event
    count; heapq.merge pulls only one event ahead per stream.
    """
    for event in heapq.merge(*streams, key=order_key):
        yield event


def _write_chunk(events, directory, index):
    path = os.path.join(directory, "chunk-%05d.ndjson" % index)
    ordered = sorted(events, key=order_key)
    with open(path, "w", encoding="utf-8") as handle:
        for event in ordered:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    return path


def _read_chunk(path):
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


@contextlib.contextmanager
def _chunks_from_iterables(streams, chunk_size, directory):
    """Sort-spill each stream into chunk files; clean up on exit."""
    paths = []
    try:
        index = 0
        for stream in streams:
            buffer = []
            for event in stream:
                buffer.append(event)
                if len(buffer) >= chunk_size:
                    paths.append(_write_chunk(buffer, directory, index))
                    index += 1
                    buffer = []
            if buffer:
                paths.append(_write_chunk(buffer, directory, index))
                index += 1
        yield paths
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def external_merge(*streams, chunk_size=10_000, directory=None):
    """Generator merge with bounded RAM for very large event volumes.

    Each input iterable is spilled to sorted NDJSON chunk files of at most
    ``chunk_size`` events; chunks are then k-way heap-merged.  Peak memory is
    O(chunk_size + number_of_chunks); disk cost is one temporary copy of the
    inputs.  Events must be JSON-serializable plain dicts.
    """
    own_dir = directory is None
    if own_dir:
        directory = tempfile.mkdtemp(prefix="eventorder-")
    try:
        with _chunks_from_iterables(streams, chunk_size, directory) as paths:
            readers = [_read_chunk(path) for path in paths]
            yield from heapq.merge(*readers, key=order_key)
    finally:
        if own_dir:
            with contextlib.suppress(OSError):
                os.rmdir(directory)
