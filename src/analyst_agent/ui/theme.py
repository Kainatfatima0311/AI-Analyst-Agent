"""Visual tokens and the stylesheet for the analyst interface.

Two palettes live here and they do different jobs, which is the distinction worth keeping
straight:

* **Chrome** — the sidebar, the buttons, the card borders, the active nav row. Indigo, and it
  never appears inside a plot. Chrome carries *hierarchy*: what is clickable, what is current,
  what is primary.
* **Series** — the colours a chart uses, which come from ``tools/palette.py`` and were run
  through the data-viz validator. Chrome must never borrow one of those, because a reader who
  learns that a hue means "premium category" should not then meet it on a button.

The one overlap to be aware of: series slot 7 is a violet close to the chrome indigo. Slot 7 is
only reached by a chart with seven series, where the legend and direct labels carry identity
anyway — but it is the reason chrome sits at a lighter, more saturated step rather than sharing
one.

Status colour is reserved and never carries meaning alone. Every state ships with an icon and a
label, so a supported hypothesis is legible to someone who cannot distinguish the green from the
red, in a screenshot, or in print.

Dark mode is **selected, not flipped**: the content surfaces and the series steps are separate
values validated against the dark surface, because an inverted light palette produces muddy
mid-tones and fails contrast exactly where the data is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from analyst_agent.tools.palette import DARK, LIGHT

Mode = Literal["light", "dark"]

# --- status ------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    label: str
    icon: str
    colour: str
    tone: str


# Reserved, and distinct from the categorical series colours so a status can never be mistaken
# for "series 4".
STATUS: dict[str, Status] = {
    "supported": Status("Supported", "✓", "#1f7a4d", "good"),
    "refuted": Status("Refuted", "✕", "#b23c3c", "critical"),
    "inconclusive": Status("Inconclusive", "~", "#9a6b00", "warning"),
    "proposed": Status("Proposed", "·", "#6b6a66", "neutral"),
    "testing": Status("Testing", "◐", "#2a78d6", "neutral"),
    # run states
    "completed": Status("Completed", "✓", "#1f7a4d", "good"),
    "failed": Status("Failed", "✕", "#b23c3c", "critical"),
    "truncated": Status("Truncated", "!", "#9a6b00", "warning"),
    "awaiting_approval": Status("Awaiting approval", "⏸", "#9a6b00", "warning"),
    "clarifying": Status("Waiting on you", "?", "#2a78d6", "neutral"),
    "investigating": Status("Investigating", "◐", "#2a78d6", "neutral"),
    "received": Status("Queued", "·", "#6b6a66", "neutral"),
    # guard verdicts
    "allowed": Status("Allowed", "✓", "#1f7a4d", "good"),
    "approved": Status("Approved by a human", "✓", "#2a78d6", "neutral"),
    "escalated": Status("Escalated", "⏸", "#9a6b00", "warning"),
    "rejected": Status("Blocked", "✕", "#b23c3c", "critical"),
}

CONFIDENCE: dict[str, Status] = {
    "high": Status("High confidence", "●●●", "#1f7a4d", "good"),
    "medium": Status("Medium confidence", "●●○", "#9a6b00", "warning"),
    "low": Status("Low confidence", "●○○", "#b23c3c", "critical"),
}


def status_of(key: str | None) -> Status:
    return STATUS.get(key or "", Status(key or "unknown", "·", "#6b6a66", "neutral"))


# --- chrome ------------------------------------------------------------------


@dataclass(frozen=True)
class Chrome:
    """Surfaces and ink for one mode. The sidebar is dark in both."""

    surface: str
    raised: str
    sunken: str
    border: str
    border_strong: str
    text: str
    text_secondary: str
    text_muted: str
    shadow: str
    accent_soft: str


CHROME: dict[Mode, Chrome] = {
    "light": Chrome(
        surface="#f5f6fb",
        raised="#ffffff",
        sunken="#f0f1f8",
        border="#e6e8f2",
        border_strong="#d3d6e6",
        text="#14162b",
        text_secondary="#5b5f7a",
        text_muted="#868aa3",
        shadow="0 1px 2px rgba(20, 22, 43, .04), 0 8px 24px rgba(20, 22, 43, .05)",
        accent_soft="#eeecfe",
    ),
    "dark": Chrome(
        surface="#0f1017",
        raised="#181a25",
        sunken="#13141d",
        border="#272a3a",
        border_strong="#343850",
        text="#f2f3f8",
        text_secondary="#a9adc4",
        text_muted="#7c8099",
        shadow="0 1px 2px rgba(0, 0, 0, .3), 0 8px 24px rgba(0, 0, 0, .35)",
        accent_soft="#211f3d",
    ),
}

# Chrome accent. One hue, two steps: the flat one for fills and borders, the pair for the
# gradient on a primary action. Deliberately not a series colour — see the module docstring.
ACCENT = "#6c5ce7"
ACCENT_STRONG = "#5a49dd"
ACCENT_GRADIENT = "linear-gradient(135deg, #7a68f0 0%, #6c5ce7 55%, #5f4fe0 100%)"

# The sidebar stays dark in both modes: it is the frame, and a frame that changes with the
# content makes the content harder to find.
SIDEBAR = "#0d0f1e"
SIDEBAR_RAISED = "#171a30"
SIDEBAR_BORDER = "#22263f"
SIDEBAR_TEXT = "#e8e9f2"
SIDEBAR_MUTED = "#8d92ad"


def chrome(mode: Mode = "light") -> Chrome:
    return CHROME[mode]


# --- charts ------------------------------------------------------------------


def chart_layout(mode: Mode = "light") -> dict[str, Any]:
    """Layout overrides so a generated figure sits in the current surface.

    The agent's charts are built by ``chart_builder`` against the light theme. Rather than
    regenerate them, the surface and ink are overridden here — the *series* colours are left
    exactly as they were chosen, because those are the validated ones and repainting them by
    theme would break the rule that colour follows the entity.
    """
    theme = LIGHT if mode == "light" else DARK
    return {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"color": theme.text_secondary, "size": 12},
        "title": {"font": {"color": theme.text_primary, "size": 14}},
        "xaxis": {"gridcolor": theme.grid, "linecolor": theme.axis, "zerolinecolor": theme.grid},
        "yaxis": {"gridcolor": theme.grid, "linecolor": theme.axis, "zerolinecolor": theme.grid},
        "legend": {"font": {"color": theme.text_secondary}},
        "margin": {"l": 48, "r": 16, "t": 32, "b": 36},
        "hoverlabel": {"bgcolor": theme.surface, "font": {"color": theme.text_primary}},
    }


# --- stylesheet ---------------------------------------------------------------


def stylesheet(mode: Mode = "light") -> str:
    """One block of CSS, written against tokens so the two modes swap in one place."""
    c = chrome(mode)
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {{
  --surface: {c.surface};
  --raised: {c.raised};
  --sunken: {c.sunken};
  --border: {c.border};
  --border-strong: {c.border_strong};
  --text: {c.text};
  --text-secondary: {c.text_secondary};
  --text-muted: {c.text_muted};
  --shadow: {c.shadow};
  --accent: {ACCENT};
  --accent-strong: {ACCENT_STRONG};
  --accent-soft: {c.accent_soft};
  --accent-gradient: {ACCENT_GRADIENT};
  --radius: 14px;
  --radius-sm: 10px;
}}

html, body, [class*="css"], .stApp {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.stApp {{ background: var(--surface); color: var(--text); }}
#MainMenu, footer, header[data-testid="stHeader"] {{ visibility: hidden; height: 0; }}
.block-container {{ padding: 1.6rem 2.2rem 4rem; max-width: 1500px; }}

h1, h2, h3, h4 {{ color: var(--text); font-weight: 600; letter-spacing: -.015em; }}
p, li, span, label {{ color: var(--text-secondary); }}
a {{ color: var(--accent); }}
hr {{ border-color: var(--border); }}

/* --- sidebar ------------------------------------------------------------- */

[data-testid="stSidebar"] {{
  background: {SIDEBAR};
  border-right: 1px solid {SIDEBAR_BORDER};
  width: 268px !important;
}}
[data-testid="stSidebar"] > div:first-child {{ padding: 1.3rem 1rem 1rem; }}
[data-testid="stSidebar"] * {{ color: {SIDEBAR_TEXT}; }}
[data-testid="stSidebarCollapseButton"] svg {{ fill: {SIDEBAR_MUTED}; }}

.brand {{ display: flex; align-items: center; gap: .7rem; padding: .1rem .3rem 1.3rem; }}
.brand .mark {{
  width: 38px; height: 38px; border-radius: 11px;
  background: var(--accent-gradient);
  display: grid; place-items: center;
  font-size: 1.05rem; color: #fff; font-weight: 700;
  box-shadow: 0 4px 14px rgba(108, 92, 231, .4);
}}
.brand .name {{ font-size: 1.06rem; font-weight: 700; line-height: 1.1; letter-spacing: -.02em; }}
.brand .kind {{ font-size: .72rem; color: {SIDEBAR_MUTED}; letter-spacing: .01em; }}

/* Nav rows. The current page is a styled div; the rest are buttons wearing the same shape, so
   the only visual difference between them is state rather than kind. */
.nav-current {{
  display: flex; align-items: center; gap: .65rem;
  padding: .62rem .8rem; margin-bottom: .3rem;
  border-radius: var(--radius-sm);
  background: rgba(108, 92, 231, .22);
  border: 1px solid rgba(122, 104, 240, .45);
  font-size: .89rem; font-weight: 600;
}}
.nav-current .ico {{ opacity: .95; width: 1.15rem; text-align: center; }}

[data-testid="stSidebar"] .stButton > button {{
  width: 100%; justify-content: flex-start; text-align: left;
  background: transparent; border: 1px solid transparent;
  color: {SIDEBAR_MUTED} !important;
  padding: .6rem .8rem; margin-bottom: .12rem;
  border-radius: var(--radius-sm);
  font-size: .89rem; font-weight: 500;
  box-shadow: none; transition: background .12s, color .12s;
}}
[data-testid="stSidebar"] .stButton > button:hover {{
  background: rgba(255, 255, 255, .05);
  color: {SIDEBAR_TEXT} !important;
  border-color: transparent;
}}
[data-testid="stSidebar"] .stButton > button p {{ color: inherit !important; font-size: .89rem; }}

.side-status {{
  margin: 1.1rem .1rem .3rem; padding: .8rem .85rem;
  background: {SIDEBAR_RAISED}; border: 1px solid {SIDEBAR_BORDER};
  border-radius: var(--radius-sm);
}}
.side-status .row {{ display: flex; align-items: center; gap: .5rem; font-size: .84rem; font-weight: 600; }}
.side-status .dot {{ width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 0 3px rgba(31, 122, 77, .18); }}
.side-status .sub {{ font-size: .74rem; color: {SIDEBAR_MUTED}; margin-top: .3rem; }}

.side-user {{
  margin: .5rem .1rem 0; padding: .7rem .8rem;
  background: {SIDEBAR_RAISED}; border: 1px solid {SIDEBAR_BORDER};
  border-radius: var(--radius-sm);
  display: flex; align-items: center; gap: .6rem;
}}
.side-user .avatar {{
  width: 30px; height: 30px; border-radius: 9px;
  background: var(--accent-gradient); color: #fff;
  display: grid; place-items: center; font-size: .8rem; font-weight: 700;
}}
.side-user .who {{ font-size: .8rem; font-weight: 600; line-height: 1.2; }}
.side-user .role {{ font-size: .72rem; color: {SIDEBAR_MUTED}; }}

[data-testid="stSidebar"] .stTextInput input {{
  background: {SIDEBAR_RAISED}; border: 1px solid {SIDEBAR_BORDER};
  color: {SIDEBAR_TEXT}; border-radius: var(--radius-sm); font-size: .82rem;
}}
[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div {{
  background: {SIDEBAR_RAISED}; border-color: {SIDEBAR_BORDER}; color: {SIDEBAR_TEXT};
  border-radius: var(--radius-sm);
}}
[data-testid="stSidebar"] label, [data-testid="stSidebar"] .stCaption {{
  color: {SIDEBAR_MUTED} !important; font-size: .76rem;
}}

/* --- page header --------------------------------------------------------- */

.greeting {{ font-size: 1.65rem; font-weight: 700; letter-spacing: -.03em; color: var(--text); }}
.greeting-sub {{ font-size: .92rem; color: var(--text-muted); margin-top: .15rem; }}

/* --- cards -------------------------------------------------------------- */

/* A Streamlit bordered container *is* the card. Markup cannot wrap widgets — an unclosed div in
   st.markdown is closed immediately — so the container carries the surface instead. */
[data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--raised); border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important; box-shadow: var(--shadow);
  padding: 1.05rem 1.15rem; margin-bottom: 1rem;
}}
/* The ask card is the one thing every visit starts with, so it gets the only accent hairline. */
[data-testid="stVerticalBlockBorderWrapper"]:has(.ask-head) {{
  border-color: rgba(108, 92, 231, .5) !important;
}}
[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] {{
  background: {SIDEBAR_RAISED}; border-color: {SIDEBAR_BORDER} !important; box-shadow: none;
}}

.card {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1.1rem 1.2rem;
  box-shadow: var(--shadow); margin-bottom: 1rem;
}}
.card.accent {{ border-color: rgba(108, 92, 231, .45); }}
.card.flush {{ padding: .8rem 1rem; }}
.card-title {{
  font-size: .74rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
  color: var(--text-muted); margin: 1.3rem 0 .6rem;
}}
.section-head {{ display: flex; align-items: baseline; justify-content: space-between; margin: 1.5rem 0 .7rem; }}
.section-head .h {{ font-size: 1.06rem; font-weight: 600; color: var(--text); letter-spacing: -.02em; }}

/* The ask card: a purple hairline is the only thing on the page that gets one, because it is
   the one thing every visit starts with. */
.ask-head {{ display: flex; align-items: center; gap: .55rem; margin-bottom: .1rem; }}
.ask-head .spark {{ font-size: 1.05rem; }}
.ask-head .t {{ font-size: 1.02rem; font-weight: 600; color: var(--text); }}
.ask-foot {{ display: flex; align-items: center; justify-content: space-between; margin-top: -.2rem; }}
.counter {{ font-size: .78rem; color: var(--text-muted); font-variant-numeric: tabular-nums; }}

.suggest {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius); padding: .85rem .9rem;
  box-shadow: var(--shadow); height: 100%;
  display: flex; gap: .65rem; align-items: flex-start;
}}
.suggest .ico {{
  width: 30px; height: 30px; border-radius: 9px; flex: 0 0 30px;
  display: grid; place-items: center; font-size: .92rem;
}}
.suggest .q {{ font-size: .86rem; font-weight: 500; color: var(--text); line-height: 1.35; }}

.recent {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius); padding: .9rem 1rem;
  box-shadow: var(--shadow);
}}
.recent.current {{ border-color: rgba(108, 92, 231, .5); background: var(--accent-soft); }}
.recent .q {{ font-size: .9rem; font-weight: 600; color: var(--text); line-height: 1.35; min-height: 2.4em; }}
.recent .meta {{ display: flex; align-items: center; justify-content: space-between; margin-top: .7rem; }}
.recent .when {{ font-size: .76rem; color: var(--text-muted); }}

/* --- result ------------------------------------------------------------- */

.result-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }}
.result-head .q {{ font-size: 1.2rem; font-weight: 650; color: var(--text); letter-spacing: -.02em; }}

.takeaway {{
  display: flex; gap: .6rem; align-items: flex-start;
  padding: .7rem .8rem; margin-bottom: .5rem;
  background: var(--sunken); border-radius: var(--radius-sm);
  border-left: 3px solid var(--border-strong);
}}
.takeaway.material {{ border-left-color: #9a6b00; }}
.takeaway.good {{ border-left-color: #1f7a4d; }}
.takeaway .ico {{ font-size: .85rem; opacity: .8; }}
.takeaway .t {{ font-size: .83rem; color: var(--text); line-height: 1.4; }}

.panel-title {{
  display: flex; align-items: center; gap: .4rem;
  font-size: .82rem; font-weight: 600; color: var(--text); margin-bottom: .6rem;
}}
.panel-title .hint {{ font-size: .72rem; color: var(--text-muted); font-weight: 400; }}

.evidence-foot {{
  display: flex; align-items: center; justify-content: space-between;
  border-top: 1px solid var(--border); margin-top: .4rem; padding-top: .9rem;
}}
.evidence-foot .l {{ font-size: .86rem; font-weight: 600; color: var(--text); }}
.evidence-foot .n {{ font-size: .86rem; color: var(--accent); font-weight: 500; }}

.conclusion {{
  background: var(--raised); border: 1px solid var(--border); border-left: 3px solid var(--accent);
  border-radius: var(--radius); padding: 1rem 1.15rem; margin-bottom: .8rem;
}}
.conclusion .text {{ font-size: .96rem; line-height: 1.6; color: var(--text); }}

.stat-row {{ display: flex; gap: 1.6rem; flex-wrap: wrap; }}
.stat .value {{ font-size: 1.25rem; font-weight: 650; color: var(--text); font-variant-numeric: tabular-nums; }}
.stat .label {{ font-size: .74rem; color: var(--text-muted); letter-spacing: .02em; }}

/* --- findings, hypotheses, timeline, audit ------------------------------ */

.finding {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius); padding: .95rem 1.1rem; margin-bottom: .8rem;
}}
.finding.material {{ border-left: 3px solid #9a6b00; }}
.finding .statement {{ font-size: .95rem; font-weight: 550; color: var(--text); line-height: 1.5; }}
.hypothesis {{
  margin-top: .7rem; padding: .7rem .85rem;
  background: var(--sunken); border-radius: var(--radius-sm);
}}
.hypothesis .claim {{ font-size: .88rem; color: var(--text); }}
.hypothesis .why {{ font-size: .8rem; color: var(--text-muted); margin-top: .3rem; line-height: 1.45; }}

.timeline {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius); padding: .5rem .3rem; overflow: hidden;
}}
.tl-row {{
  display: grid; grid-template-columns: 190px 1fr; gap: .8rem; align-items: baseline;
  padding: .42rem .9rem; border-left: 2px solid transparent;
}}
.tl-row.active {{ border-left-color: var(--accent); background: var(--accent-soft); }}
.tl-row.error {{ border-left-color: #b23c3c; }}
.tl-node {{ font-size: .8rem; font-weight: 600; color: var(--text); font-family: ui-monospace, monospace; }}
.tl-meta {{ font-size: .78rem; color: var(--text-muted); line-height: 1.4; }}

.evidence {{ margin-bottom: .3rem; }}
.evidence .purpose {{ font-size: .87rem; color: var(--text); font-weight: 500; }}
.evidence .meta {{ font-size: .74rem; color: var(--text-muted); font-family: ui-monospace, monospace; }}

.approval {{
  background: var(--raised); border: 1px solid rgba(154, 107, 0, .45);
  border-left: 3px solid #9a6b00; border-radius: var(--radius);
  padding: .95rem 1.1rem; margin-bottom: .7rem;
}}
.approval .kind {{ font-size: .95rem; font-weight: 650; color: var(--text); }}
.approval .why {{ font-size: .86rem; color: var(--text-secondary); margin-top: .25rem; line-height: 1.5; }}

/* --- chips -------------------------------------------------------------- */

.chip-row {{ display: flex; gap: .4rem; flex-wrap: wrap; margin: .35rem 0 .6rem; }}
.chip {{
  display: inline-flex; align-items: center; gap: .32rem;
  padding: .2rem .55rem; border-radius: 999px;
  font-size: .74rem; font-weight: 550; letter-spacing: .01em;
  border: 1px solid var(--border-strong); background: var(--raised);
}}
.chip .ico {{ font-size: .72rem; }}

/* --- controls ----------------------------------------------------------- */

.stButton > button {{
  border-radius: var(--radius-sm); font-weight: 550; font-size: .85rem;
  border: 1px solid var(--border-strong); background: var(--raised); color: var(--text);
  box-shadow: none; transition: border-color .12s, background .12s, transform .06s;
}}
.stButton > button:hover {{ border-color: var(--accent); color: var(--accent); }}
.stButton > button:active {{ transform: translateY(1px); }}
.stButton > button[kind="primary"] {{
  background: var(--accent-gradient); border: none; color: #fff !important;
  box-shadow: 0 4px 14px rgba(108, 92, 231, .32);
}}
.stButton > button[kind="primary"]:hover {{ filter: brightness(1.06); color: #fff !important; }}
.stButton > button[kind="primary"] p {{ color: #fff !important; }}

/* Suggestion, recent and saved rows are buttons wearing a card: the whole surface is the hit
   target, which is what the design implies and what a separate "use this" control would break.
   Keyed selectors, so the ordinary buttons — Approve, Reject, Send — keep their own shape. If a
   Streamlit version stops emitting the key class these degrade to normal buttons rather than
   breaking. */
[class*="st-key-eg-"] .stButton > button,
[class*="st-key-recent-"] .stButton > button,
[class*="st-key-saved-"] .stButton > button {{
  justify-content: flex-start; text-align: left;
  min-height: 4.1rem; padding: .85rem .95rem;
  border-radius: var(--radius); border: 1px solid var(--border);
  background: var(--raised); box-shadow: var(--shadow);
  font-size: .87rem; font-weight: 550; line-height: 1.35; white-space: normal;
}}
[class*="st-key-eg-"] .stButton > button:hover,
[class*="st-key-recent-"] .stButton > button:hover,
[class*="st-key-saved-"] .stButton > button:hover {{
  border-color: var(--accent); background: var(--accent-soft); color: var(--text);
}}
[class*="st-key-saved-"] .stButton > button {{ min-height: 3rem; }}

/* The coloured glyph tile on a suggestion card. It cannot live in the label — a Streamlit button
   label is text — so it is a ::before on the button, coloured by column position. Position, not
   question: the tile is decoration, and tying a hue to a *question* would start to read as the
   identity a chart's colours carry. */
[class*="st-key-eg-"] .stButton > button::before {{
  content: "▤";
  flex: 0 0 30px; width: 30px; height: 30px; margin-right: .7rem;
  display: grid; place-items: center;
  border-radius: 9px; font-size: .95rem; line-height: 1;
  background: var(--accent-soft); color: var(--accent);
}}
[data-testid="stHorizontalBlock"] > div:nth-child(1) [class*="st-key-eg-"] button::before {{
  content: "▤"; background: rgba(31, 122, 77, .13); color: #1f7a4d;
}}
[data-testid="stHorizontalBlock"] > div:nth-child(2) [class*="st-key-eg-"] button::before {{
  content: "◔"; background: rgba(108, 92, 231, .13); color: #6c5ce7;
}}
[data-testid="stHorizontalBlock"] > div:nth-child(3) [class*="st-key-eg-"] button::before {{
  content: "◈"; background: rgba(192, 112, 0, .13); color: #c07000;
}}
[data-testid="stHorizontalBlock"] > div:nth-child(4) [class*="st-key-eg-"] button::before {{
  content: "⌁"; background: rgba(42, 120, 214, .13); color: #2a78d6;
}}
[class*="st-key-suggest-next"] .stButton > button {{
  min-height: 4.1rem; border-radius: var(--radius); padding: 0;
  font-size: 1.1rem; color: var(--text-muted);
}}

/* A recent card is one surface: the button holds the question and the meta line is pulled up
   into it, so the whole thing reads and clicks as a single card rather than a button with a
   caption stuck underneath. */
[class*="st-key-recent-"] .stButton > button {{
  min-height: 4.4rem; align-items: flex-start; padding-bottom: 1.9rem;
}}
.recent-meta {{
  display: flex; align-items: center; justify-content: space-between;
  margin: -2.5rem .95rem 1rem; position: relative; pointer-events: none;
}}
.recent-meta .when {{ font-size: .76rem; color: var(--text-muted); }}
.recent-current-anchor + div .stButton > button {{
  border-color: rgba(108, 92, 231, .5); background: var(--accent-soft);
}}

/* Chart panels: a quieter surface inside the result card, so three panels read as three panels
   rather than as three cards stacked on a card. */
[data-testid="stVerticalBlockBorderWrapper"]
  [data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--sunken); box-shadow: none; padding: .85rem .9rem; margin-bottom: 0;
}}
.blank {{
  font-size: .82rem; color: var(--text-muted); line-height: 1.45;
  padding: 1.6rem .2rem; text-align: center;
}}

.side-art {{ margin-top: 1.4rem; opacity: .95; filter: drop-shadow(0 8px 24px rgba(108, 92, 231, .35)); }}
[data-testid="stSidebar"] .stExpander {{
  background: transparent; border: none !important; margin-top: -.35rem;
}}
[data-testid="stSidebar"] .stExpander summary {{ font-size: .74rem; color: {SIDEBAR_MUTED}; }}

.stTextArea textarea, .stTextInput input {{
  background: var(--raised); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text); font-size: .92rem;
}}
.stTextArea textarea:focus, .stTextInput input:focus {{
  border-color: var(--accent); box-shadow: 0 0 0 3px rgba(108, 92, 231, .14);
}}
.stTextArea textarea::placeholder {{ color: var(--text-muted); }}

div[data-baseweb="select"] > div {{
  background: var(--raised); border-color: var(--border);
  border-radius: var(--radius-sm); font-size: .84rem;
}}

.stExpander {{
  border: 1px solid var(--border) !important; border-radius: var(--radius) !important;
  background: var(--raised);
}}
.stExpander summary {{ font-size: .86rem; font-weight: 550; color: var(--text); }}
.stTabs [data-baseweb="tab-list"] {{ gap: .3rem; border-bottom: 1px solid var(--border); }}
.stTabs [data-baseweb="tab"] {{ font-size: .88rem; font-weight: 550; }}
.stTabs [aria-selected="true"] {{ color: var(--accent); }}
[data-testid="stCodeBlock"] {{ border-radius: var(--radius-sm); font-size: .8rem; }}
</style>
"""


# --- chips --------------------------------------------------------------------


def chip(label: str, icon: str, colour: str) -> str:
    return (
        f'<span class="chip" style="color:{colour};border-color:{colour}44;'
        f'background:{colour}12">'
        f'<span class="ico">{icon}</span>{label}</span>'
    )


def status_chip(key: str | None) -> str:
    status = status_of(key)
    return chip(status.label, status.icon, status.colour)


def confidence_chip(level: str | None) -> str:
    status = CONFIDENCE.get(level or "", Status(level or "unstated", "○○○", "#6b6a66", "neutral"))
    return chip(status.label, status.icon, status.colour)
