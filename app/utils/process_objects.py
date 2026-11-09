"""Objects that must stay the same for the whole process.

Streamlit's source watcher removes the application's modules from ``sys.modules``
when it reports a changed source file, and the next page run imports them again.
Every module-level object is then created a second time, while background threads
keep using the first one. For the WebUI that meant an empty in-memory task store and
an empty record of running generations: a finished video was reported as "task
record unavailable". On Windows the watcher also reported files that were only read
(see ``app/controllers/__init__.py``).

The registry lives on the ``sys`` module, which is never re-imported, so every copy
of a module gets the same object back.
"""

import sys
import threading
from typing import Callable, TypeVar

T = TypeVar("T")

_REGISTRY = "_moneyprinterturbo_process_objects"


def _registry() -> dict:
    # dict.setdefault is atomic: two threads importing at once still share one registry.
    return sys.__dict__.setdefault(
        _REGISTRY, {"lock": threading.RLock(), "objects": {}}
    )


def process_object(name: str, factory: Callable[[], T]) -> T:
    """Return the process-wide object called ``name``; ``factory`` creates it once."""
    registry = _registry()
    with registry["lock"]:
        objects = registry["objects"]
        if name not in objects:
            objects[name] = factory()
        return objects[name]
