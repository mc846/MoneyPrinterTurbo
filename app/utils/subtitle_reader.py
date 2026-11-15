"""Read SRT cue boundaries before adapting them to MoviePy timing tuples."""
import os
import re


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            # Once a cue has started, timestamps belong to its text until the
            # blank-line separator; they must not overwrite the cue's timing.
            if times and current_times is None:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


_CLOSING = "\"'”’»)]"


def restore_end_punctuation(cues, script, marks="?!"):
    """
    Burned-in subtitles: the split at punctuation drops a cue's closing "?" or "!" ("olsaydı?" -> "olsaydı").
    Each cue is found in the script in order (its words, any separators between them) and gets back the
    marks that follow it there (after a closing quote or bracket). A cue not found is left as it is.
    ``cues`` are moviepy cues ((start, end), text); a new list is returned, nothing is written.
    """
    script = str(script or "")
    if not script.strip():
        return list(cues)
    position, restored = 0, []
    for timing, text in cues:
        words = re.findall(r"\w+", str(text or ""))
        match = None
        if words:
            pattern = r"\W*?".join(re.escape(word) for word in words)
            match = re.compile(pattern).search(script, position)
        if match is None:
            restored.append((timing, text))
            continue
        position = match.end()
        tail = re.match(rf"[{re.escape(_CLOSING)}]*([{re.escape(marks)}]+)", script[position:])
        if tail and not str(text).rstrip().endswith(tail.group(1)):
            text = str(text).rstrip() + tail.group(1)
        restored.append((timing, text))
    return restored


def moviepy_subtitles(filename):
    """Convert only cue headers; timestamp-bearing text remains caption text."""
    from moviepy.tools import convert_to_seconds

    cues = []
    for _index, timing, text in file_to_subtitles(filename):
        start, end = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", timing)
        cues.append(((convert_to_seconds(start), convert_to_seconds(end)), text))
    return cues
