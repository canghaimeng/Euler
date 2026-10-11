"""Read-only Euler import adapters.

These modules snapshot existing Fact Graph, Global Memory, and round receipts.
They must never become a second Fact admission or EventStore write path.
"""

from .project import (
    ProjectAdapterError,
    FactSnapshot,
    MemoryRecord,
    ProjectSnapshot,
    ReceiptSnapshot,
    snapshot_project,
)

__all__ = [
    "ProjectAdapterError",
    "FactSnapshot",
    "MemoryRecord",
    "ReceiptSnapshot",
    "ProjectSnapshot",
    "snapshot_project",
]
