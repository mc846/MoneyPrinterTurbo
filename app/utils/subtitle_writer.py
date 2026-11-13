"""Stage subtitle artifacts beside their destination before publication."""
from contextlib import contextmanager
import os
import re
import tempfile
import time

from loguru import logger

# Windows: a freshly written file can be held for a moment by another process (an
# antivirus or indexer scan), and renaming it then fails with "Access is denied"
# (WinError 5). The subtitle was lost that way in a real render (14.11), so the
# publication is retried for a short while before it gives up.
REPLACE_ATTEMPTS = 50
REPLACE_DELAY_SECONDS = 0.1


def publish_staged(staged, destination) -> None:
    """``os.replace`` that retries a PermissionError for up to ~5 seconds."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(staged, destination)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_SECONDS)


@contextmanager
def staged_subtitle_file(destination):
    descriptor, staged = tempfile.mkstemp(
        prefix=".subtitle-", suffix=".srt",
        dir=os.path.dirname(os.path.abspath(destination)),
    )
    os.close(descriptor)
    try:
        yield staged
    finally:
        try:
            os.remove(staged)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            logger.warning(f"failed to remove temporary subtitle: {cleanup_error}")


def has_subtitle_cue(content) -> bool:
    """Return True when the body carries at least one timed SRT cue.

    An SRT body without a complete timestamp timing line holds no caption any player
    can display, so a publication built from it is a failed write rather than a
    valid empty result.
    """
    timestamp = r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9],[0-9]{3}"
    return any(
        re.fullmatch(rf"\s*{timestamp}\s+-->\s+{timestamp}(?:\s+.*)?\s*", line)
        for line in str(content or "").splitlines()
    )


def write_subtitle_file(destination, content) -> bool:
    """Publish a complete subtitle file without destroying existing captions.

    Every other caption publication site validates the artifact it is about to
    replace: ``voice._write_subtitle_items`` re-parses the staged file and
    ``task.generate_subtitle`` requires parseable cues before ``os.replace``.
    Publishing an empty body would silently delete the captions already stored
    at ``destination``, which is the exact outcome those checks exist to
    prevent, so the same rule is enforced once here for every caller.

    Returns ``True`` after a successful replacement, and ``False`` when nothing
    was published because the body held no cue.
    """
    if not has_subtitle_cue(content):
        logger.warning(
            f"refusing to publish a subtitle file without cues: path={destination}"
        )
        return False

    with staged_subtitle_file(destination) as staged:
        with open(staged, "w", encoding="utf-8") as output:
            output.write(content)
        publish_staged(staged, destination)
    return True
