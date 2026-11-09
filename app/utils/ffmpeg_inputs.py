"""
Protocol whitelist for every input of every ffmpeg command the application runs.

ffmpeg picks a demuxer by probing a file's content, not its name. A downloaded
"clip.mp4" that is really an HLS or DASH playlist makes ffmpeg fetch the URLs
listed inside it (server-side request forgery), and the fetched segments reach
decoders the file itself never named. With ``-protocol_whitelist`` an input
read from disk may only open local files and a pipe input may only read its
pipe, so such a file fails with "Protocol 'http' not on whitelist" instead.

The protocol lists live here and nowhere else:
- commands the application builds use ffmpeg_input() / input_options();
- commands MoviePy and pydub build are rewritten by restrict_ffmpeg_inputs(),
  installed by guard_libraries() on the subprocess module those libraries use.
"""

import importlib
import os
import subprocess
import threading
import types

# Inputs read from disk (including files a concat list or an image sequence names).
FILE_PROTOCOLS = "file"
# Inputs fed through standard input or another pipe ("-", "pipe:N"). Current ffmpeg
# opens "-" with its fd protocol, older releases and "pipe:N" with pipe.
PIPE_PROTOCOLS = "fd,pipe"
# pydub reads in-memory audio through "cache:pipe:0" when its converter is plain "ffmpeg".
CACHED_PIPE_PROTOCOLS = "cache,pipe"


def input_protocols(source) -> str:
    """Return the protocols an ffmpeg input named ``source`` may use."""
    source = os.fspath(source)
    if source == "-" or source.startswith("pipe:"):
        return PIPE_PROTOCOLS
    if source.startswith("cache:pipe:"):
        return CACHED_PIPE_PROTOCOLS
    return FILE_PROTOCOLS


def input_options(source) -> list[str]:
    """Input options restricting ``source``: place them right before the input (``-i`` or ffprobe's path)."""
    return ["-protocol_whitelist", input_protocols(source)]


def ffmpeg_input(source) -> list[str]:
    """``-protocol_whitelist <protocols> -i <source>`` for an ffmpeg command."""
    source = os.fspath(source)
    return [*input_options(source), "-i", source]


def restrict_ffmpeg_inputs(command) -> list[str]:
    """
    Return a copy of an ffmpeg argument list in which every ``-i`` input carries
    a protocol whitelist. Inputs that already set ``-protocol_whitelist`` keep it.
    """
    arguments = [os.fspath(argument) if isinstance(argument, os.PathLike) else argument for argument in command]
    restricted = []
    has_whitelist = False
    for index, argument in enumerate(arguments):
        if argument == "-protocol_whitelist":
            has_whitelist = True
        elif argument == "-i" and index + 1 < len(arguments):
            if not has_whitelist:
                restricted.extend(input_options(arguments[index + 1]))
            has_whitelist = False
        restricted.append(argument)
    return restricted


def is_ffmpeg_command(args) -> bool:
    """True for an argument list whose program is ffmpeg (not ffprobe or ffplay)."""
    if isinstance(args, (str, bytes)) or not isinstance(args, (list, tuple)) or not args:
        return False
    program = os.path.basename(os.fspath(args[0])).lower()
    return program.startswith("ffmpeg")


class _RestrictedPopen(subprocess.Popen):
    """subprocess.Popen that restricts the inputs of ffmpeg commands."""

    def __init__(self, args, *rest, **kwargs):
        if is_ffmpeg_command(args):
            args = restrict_ffmpeg_inputs(args)
        super().__init__(args, *rest, **kwargs)


class _GuardedSubprocess(types.ModuleType):
    """The subprocess module as MoviePy and pydub see it once guard_libraries() ran."""

    Popen = _RestrictedPopen

    def __init__(self):
        super().__init__("subprocess", "subprocess with ffmpeg inputs restricted to local protocols")

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, *popenargs, **kwargs):
        if popenargs and is_ffmpeg_command(popenargs[0]):
            popenargs = (restrict_ffmpeg_inputs(popenargs[0]), *popenargs[1:])
        elif is_ffmpeg_command(kwargs.get("args")):
            kwargs["args"] = restrict_ffmpeg_inputs(kwargs["args"])
        return subprocess.run(*popenargs, **kwargs)


# Library modules that start ffmpeg, and the name under which each imported subprocess.
# MoviePy has no option for input arguments of its readers, writers and
# ffmpeg_parse_infos; they all look this name up when they start a process.
LIBRARY_MODULES = (
    ("moviepy.video.io.ffmpeg_reader", "sp"),
    ("moviepy.video.io.ffmpeg_writer", "sp"),
    ("moviepy.audio.io.readers", "sp"),
    ("moviepy.audio.io.ffmpeg_audiowriter", "sp"),
    ("moviepy.tools", "sp"),
    ("moviepy.video.io.ffmpeg_tools", "subprocess"),
    ("pydub.audio_segment", "subprocess"),
)

_guarded_subprocess = _GuardedSubprocess()
_guard_lock = threading.Lock()


def guard_libraries() -> list[str]:
    """
    Make the ffmpeg commands MoviePy and pydub start carry the protocol whitelist.

    Idempotent; modules that import MoviePy or pydub call it at import time.
    Returns the modules that are guarded. A library module that no longer
    imports subprocess under the expected name is left alone (and the tests
    that check this fail), so a library upgrade cannot break video rendering.
    """
    guarded = []
    with _guard_lock:
        for module_name, attribute in LIBRARY_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            current = getattr(module, attribute, None)
            if current is subprocess:
                setattr(module, attribute, _guarded_subprocess)
                current = _guarded_subprocess
            if current is _guarded_subprocess:
                guarded.append(module_name)
    return guarded
