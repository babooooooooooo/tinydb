"""Volcano-style operator base class.

Each operator has three lifecycle methods:

    open()    : initialize state (e.g. pin the first page).
    next()    : produce the next ``Row`` or ``None`` to signal end of stream.
    close()   : release any pinned pages / cursors.

Operators are typically composed into trees, e.g.::

    Project(Filter(SeqScan(table)))

The top-level driver repeatedly calls ``next()`` until exhaustion, then
``close()`` on the root.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tinydb.executor.row import Row


class Operator(ABC):
    def open(self) -> None:
        """Prepare the operator to start producing rows. Default: no-op."""

    @abstractmethod
    def next(self) -> Row | None:
        """Return the next row or None if the stream is exhausted."""

    def close(self) -> None:
        """Release any resources. Default: no-op."""