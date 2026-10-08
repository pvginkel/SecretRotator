"""The wizard's screens from a plan's steps (design §7.4): every operator step is a screen of its
own; consecutive non-silent tool steps are one screen; a silent step rides on the screen of the
step before it, or of the step after it when it leads the plan."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from secret_rotator.model import Actor, Step


@dataclass
class Screen:
    index: int
    actor: Actor
    steps: list[Step] = field(default_factory=list)


def collate(steps: Sequence[Step]) -> list[Screen]:
    """steps: a plan with an operator step, as every plan the UI lists has (R31), so a silent step
    that leads it always has a screen after it to ride on."""
    screens: list[Screen] = []
    leading: list[Step] = []
    for step in steps:
        if step.silent:
            if screens:
                screens[-1].steps.append(step)
            else:
                leading.append(step)
            continue
        if step.actor is Actor.TOOL and screens and screens[-1].actor is Actor.TOOL:
            screens[-1].steps.append(step)
        else:
            screens.append(Screen(len(screens), step.actor, [*leading, step]))
        leading = []
    return screens


def screen_of(screens: Sequence[Screen], step: Step) -> Screen:
    return next(screen for screen in screens if step in screen.steps)
