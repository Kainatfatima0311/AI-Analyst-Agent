"""Chart colours, validated rather than chosen by eye.

These are the categorical slots the charts use, in **fixed order**. Two rules come with them and
are enforced in ``chart_builder``:

* Hues are assigned in slot order and **never cycled**. A ninth series is not a new colour; it
  folds into "Other", and the fold is reported rather than done silently.
* Colour follows the entity, not its rank, so a filter that changes the series count must not
  repaint the survivors.

Provenance, so nobody has to re-derive it. Run through the data-viz palette validator
(OKLab Delta E x100):

* light surface ``#fcfcfb``, adjacent pairs - lightness band, chroma floor, CVD separation
  (worst 9.1 protan) and normal-vision floor (worst 19.6) all pass. Contrast warns for aqua,
  yellow and magenta below 3:1, which obligates *relief*: a visible label or a table view. Every
  chart this tool produces is returned alongside its data and rendered next to the table in the
  UI, which is that relief.
* dark surface ``#1a1a19``, adjacent pairs - all checks pass including contrast.
* Scatter compares every pair rather than neighbours, and the full eight cannot clear the
  all-pairs floors. The **first three slots do** (CVD 9.2 light / 9.4 dark), so scatter carries a
  three-series cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["light", "dark"]

# Fixed order. Index 0 is series 1.
CATEGORICAL_LIGHT: tuple[str, ...] = (
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
)
CATEGORICAL_DARK: tuple[str, ...] = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
)

MAX_SERIES = 8
# Scatter (and any form where every pair is on screen together) is capped at the three slots
# that clear the all-pairs separation floors.
MAX_SERIES_ALL_PAIRS = 3

OTHER_LABEL = "Other"
OTHER_COLOUR = {"light": "#8a8985", "dark": "#7a7975"}


@dataclass(frozen=True)
class Theme:
    """Surface and ink for one mode. Text never wears a series colour."""

    mode: Mode
    surface: str
    text_primary: str
    text_secondary: str
    text_muted: str
    grid: str
    axis: str
    categorical: tuple[str, ...]

    def colour(self, index: int) -> str:
        """Slot colour by position. Beyond the last slot, the fold colour."""
        if index >= len(self.categorical):
            return OTHER_COLOUR[self.mode]
        return self.categorical[index]


LIGHT = Theme(
    mode="light",
    surface="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#75746f",
    grid="#e6e5e1",
    axis="#c9c8c3",
    categorical=CATEGORICAL_LIGHT,
)

DARK = Theme(
    mode="dark",
    surface="#1a1a19",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    text_muted="#9a998f",
    grid="#2e2e2c",
    axis="#454441",
    categorical=CATEGORICAL_DARK,
)

THEMES: dict[Mode, Theme] = {"light": LIGHT, "dark": DARK}


def theme(mode: Mode = "light") -> Theme:
    return THEMES[mode]
