"""Recovery utilities: scan the WAL and rebuild durable state.

The recovery algorithm is intentionally simple: full-page-image logging
makes it identical to "redo the last write of each page under a
committed transaction". A checkpoint is the truncation of the WAL; on
open we replay everything in the WAL and then truncate.
"""

from __future__ import annotations

from tinydb.storage.disk import DiskManager
from tinydb.txn.manager import TransactionManager
from tinydb.txn.wal import WalRecordType


def recover(disk: DiskManager, mgr: TransactionManager) -> None:
    """Replay the WAL into ``disk`` and reset the manager.

    Safe to call when the WAL is empty: it is a no-op.
    """
    # The manager's recover() walks the WAL using its own iterator and
    # re-writes the touched pages. We delegate the heavy lifting.
    mgr.recover()
