"""Scene durations of a revision: how a task's scenes are fitted to a new voiceover.

A revision keeps the task's materials. When its voiceover is longer or shorter, the
scenes are fitted to it here and only here:

1. Order and term matching stay. A scene added for a term goes right after that
   term's last scene; a returning scene goes back to its old place.
2. No clip is longer than the clip duration (``video_clip_duration``), as in the
   pipeline. A video scene also stays inside its source, from its recorded start
   forward to the end of the file. An image scene (NASA or local image) is rendered
   anew for its duration, so only the clip duration limits it.
3. As many scenes as the pipeline would use: the fewest that cover the voiceover with
   full clips. Extra scenes are dropped with the even spread the pipeline uses when
   there are fewer slots than terms (``material._even_spread_indices``: first and last
   stay, the rest evenly spaced), not from the end.
4. The voiceover is spread evenly over the scenes; a scene that cannot take its share
   (a short video source) keeps what it can and the others share the rest.
5. Too little material: first the scenes an earlier revision dropped come back, then
   unused candidates of the scenes' terms that passed the filters, one per term per
   round in term order, like the pipeline's rounds. If that is still not enough, the
   last image scene runs past the clip duration; without an image scene it is an error.

The module only plans. Getting an added candidate (download, filters) is the caller's
``fetch`` callback, called only when a candidate is needed.
"""

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Sequence

IMAGE = "image"
VIDEO = "video"
EVEN_SPREAD = "even spread"
NO_ROOM = "no room left in its source"
# A video scene with less of its source left than this is not used (a few frames).
MIN_SCENE = 0.2
_EPSILON = 1e-6
# Added scenes sit between their term's last scene and the next one in the timeline.
_ADDED_STEP = 0.001


class RevisionTimingError(ValueError):
    """The materials cannot cover the voiceover."""


@dataclass(frozen=True)
class Scene:
    id: str
    material: str
    kind: str
    term_index: int | None = None
    order: float = 0.0
    start: float = 0.0
    source_duration: float | None = None

    def capacity(self, max_clip: float) -> float:
        if self.kind == VIDEO:
            return max(0.0, min(max_clip, (self.source_duration or 0.0) - self.start))
        return max_clip


@dataclass(frozen=True)
class Candidate:
    key: str
    term_index: int


@dataclass
class PlannedScene:
    scene: Scene
    duration: float
    # kept | returned (an earlier revision dropped it) | added (a candidate) | extended
    status: str


@dataclass
class Plan:
    scenes: list[PlannedScene]
    dropped: list[Scene] = field(default_factory=list)
    # Why each dropped scene went: EVEN_SPREAD or NO_ROOM.
    dropped_reasons: dict[str, str] = field(default_factory=dict)
    returned: list[Scene] = field(default_factory=list)
    added: list[tuple[Candidate, Scene]] = field(default_factory=list)
    extended: str | None = None
    skipped_candidates: list[Candidate] = field(default_factory=list)


def even_spread_indices(count: int, needed: int) -> list[int]:
    """The pipeline's even spread: the same indices as material._even_spread_indices."""
    from app.services import material

    return material._even_spread_indices(count, needed)


def spread(total: float, caps: Sequence[float]) -> list[float]:
    """Share ``total`` evenly; a scene capped below its share keeps its cap."""
    durations = [0.0] * len(caps)
    remaining, open_scenes = total, list(range(len(caps)))
    while open_scenes:
        share = remaining / len(open_scenes)
        capped = [index for index in open_scenes if caps[index] < share - _EPSILON]
        if not capped:
            for index in open_scenes:
                durations[index] = share
            break
        for index in capped:
            durations[index] = caps[index]
            remaining -= caps[index]
        open_scenes = [index for index in open_scenes if index not in capped]
    if remaining > _EPSILON and not open_scenes:
        raise RevisionTimingError(f"{total:.2f} s do not fit into {sum(caps):.2f} s of scenes")
    rounded = [round(duration, 3) for duration in durations]
    if rounded:
        rounded[-1] = round(rounded[-1] + total - sum(rounded), 3)
    return rounded


def plan(
    scenes: Iterable[Scene],
    target: float,
    max_clip: float,
    *,
    reserve: Iterable[Scene] = (),
    candidates: Iterable[Candidate] = (),
    fetch: Callable[[Candidate], Scene | None] | None = None,
) -> Plan:
    """Fit ``scenes`` to ``target`` seconds (see the module docstring for the rules)."""
    if target <= 0:
        raise RevisionTimingError("the voiceover has no duration")
    if max_clip <= 0:
        raise RevisionTimingError("the clip duration must be positive")
    timeline = sorted(scenes, key=lambda scene: scene.order)
    if not timeline:
        raise RevisionTimingError("the task has no scenes")
    result = Plan(scenes=[])
    for scene in [s for s in timeline if s.capacity(max_clip) < MIN_SCENE]:
        timeline.remove(scene)
        result.dropped.append(scene)
        result.dropped_reasons[scene.id] = NO_ROOM
    reserve = [scene for scene in reserve if scene.capacity(max_clip) >= MIN_SCENE]
    status = {scene.id: "kept" for scene in timeline}
    caps = {scene.id: scene.capacity(max_clip) for scene in timeline}

    if sum(caps.values()) >= target - _EPSILON:
        # The fewest evenly spread scenes that cover the voiceover.
        count = len(timeline)
        chosen = timeline
        for needed in range(max(1, math.ceil(target / max_clip - _EPSILON)), count + 1):
            picked = [timeline[index] for index in even_spread_indices(count, needed)]
            if sum(caps[scene.id] for scene in picked) >= target - _EPSILON:
                chosen = picked
                break
        for scene in timeline:
            if scene not in chosen:
                result.dropped.append(scene)
                result.dropped_reasons[scene.id] = EVEN_SPREAD
        timeline = chosen
    else:
        capacity = sum(caps.values())
        for scene in sorted(reserve, key=lambda scene: scene.order):
            if capacity >= target - _EPSILON:
                break
            if scene.id in status:
                continue
            timeline.append(scene)
            status[scene.id] = "returned"
            caps[scene.id] = scene.capacity(max_clip)
            capacity += caps[scene.id]
            result.returned.append(scene)
        timeline.sort(key=lambda scene: scene.order)

        if capacity < target - _EPSILON and fetch is not None:
            terms = sorted({scene.term_index for scene in timeline if scene.term_index is not None})
            queues = {term: [c for c in candidates if c.term_index == term] for term in terms}
            while capacity < target - _EPSILON and any(queues.values()):
                for term in terms:
                    if capacity >= target - _EPSILON:
                        break
                    while queues[term]:
                        candidate = queues[term].pop(0)
                        scene = fetch(candidate)
                        if scene is None:
                            result.skipped_candidates.append(candidate)
                            continue
                        last = max(s.order for s in timeline if s.term_index == term)
                        scene = replace(scene, term_index=term, order=round(last + _ADDED_STEP, 6))
                        timeline.append(scene)
                        timeline.sort(key=lambda s: s.order)
                        status[scene.id] = "added"
                        caps[scene.id] = scene.capacity(max_clip)
                        capacity += caps[scene.id]
                        result.added.append((candidate, scene))
                        break

        if capacity < target - _EPSILON:
            images = [scene for scene in timeline if scene.kind == IMAGE]
            if not images:
                raise RevisionTimingError(
                    f"the scenes cover {capacity:.2f} s of the {target:.2f} s voiceover, no candidate "
                    "is left and there is no image scene to lengthen"
                )
            last_image = images[-1]
            caps[last_image.id] += target - capacity
            status[last_image.id] = "extended"
            result.extended = last_image.id

    durations = spread(target, [caps[scene.id] for scene in timeline])
    result.scenes = [
        PlannedScene(scene=scene, duration=duration, status=status[scene.id])
        for scene, duration in zip(timeline, durations)
    ]
    return result


def term_matching_warning(script_changed: bool, match_materials_to_script: bool) -> str:
    """A changed text keeps the scenes in place; in script order the terms may no longer
    follow the sentences (terms are not matched again in this step)."""
    if script_changed and match_materials_to_script:
        return (
            "The text changed while materials follow the script order: the scenes keep their "
            "terms and order, so a scene may no longer match its sentence."
        )
    return ""
