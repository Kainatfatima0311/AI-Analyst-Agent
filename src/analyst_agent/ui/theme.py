"""Visual tokens and the stylesheet for the analyst interface.

The colours are not chosen here — they come from ``tools/palette.py``, which was run through the
data-viz validator. That matters for more than tidiness: a chart and the card it sits in have to
read as one system, and the alternative is a UI whose accents quietly disagree with its own
charts.

Status colour is reserved and never carries meaning alone. Every state ships with an icon and a
label, so a supported hypothesis is legible to someone who cannot distinguish the green from the
red, in a screenshot, or in print.
"""

from __future__ import annotations

from dataclasses import dataclass

from analyst_agent.tools.palette import DARK, LIGHT

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


# --- stylesheet ---------------------------------------------------------------


def stylesheet() -> str:
    """One block of CSS, written against tokens so light and dark swap in one place."""
    return f"""
<style>
:root {{
  --surface:        {LIGHT.surface};
  --surface-raised: #ffffff;
  --surface-sunken: #f4f3f0;
  --border:         #e3e2dd;
  --border-strong:  #cfcec8;
  --text:           {LIGHT.text_primary};
  --text-secondary: {LIGHT.text_secondary};
  --text-muted:     {LIGHT.text_muted};
  --accent:         {LIGHT.categorical[0]};
  --accent-soft:    #e8f0fb;
  --radius:         10px;
  --radius-sm:      6px;
}}

@media (prefers-color-scheme: dark) {{
  :root {{
    --surface:        {DARK.surface};
    --surface-raised: #232322;
    --surface-sunken: #131312;
    --border:         #33322f;
    --border-strong:  #454441;
    --text:           {DARK.text_primary};
    --text-secondary: {DARK.text_secondary};
    --text-muted:     {DARK.text_muted};
    --accent:         {DARK.categorical[0]};
    --accent-soft:    #1c2a3d;
  }}
}}

html, body, [class*="css"] {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif;
  font-feature-settings: "cv11", "ss01";
}}

.stApp {{ background: var(--surface); }}
.block-container {{ padding-top: 2.2rem; max-width: 1180px; }}

/* Streamlit's default heading weights are heavier than this page needs. */
h1, h2, h3 {{ color: var(--text); letter-spacing: -0.015em; }}
h1 {{ font-size: 1.72rem; font-weight: 660; }}
h2 {{ font-size: 1.16rem; font-weight: 620; margin-top: 1.6rem; }}
h3 {{ font-size: 0.97rem; font-weight: 600; }}
p, li, label {{ color: var(--text-secondary); }}

/* --- masthead ------------------------------------------------------------ */
.masthead {{
  display: flex; align-items: baseline; gap: 0.7rem;
  padding-bottom: 0.9rem; margin-bottom: 1.4rem;
  border-bottom: 1px solid var(--border);
}}
.masthead .title {{ font-size: 1.28rem; font-weight: 650; color: var(--text); }}
.masthead .tagline {{ font-size: 0.85rem; color: var(--text-muted); }}

/* --- cards --------------------------------------------------------------- */
.card {{
  background: var(--surface-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.05rem 1.15rem;
  margin-bottom: 0.85rem;
}}
.card.flush {{ padding: 0.8rem 0.95rem; }}
.card-title {{
  font-size: 0.72rem; font-weight: 640; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.55rem;
}}

/* --- chips --------------------------------------------------------------- */
.chip {{
  display: inline-flex; align-items: center; gap: 0.34rem;
  padding: 0.16rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 560; line-height: 1.5;
  border: 1px solid currentColor; white-space: nowrap;
}}
.chip .icon {{ font-size: 0.78rem; }}
.chip-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.35rem 0 0.6rem; }}

/* --- conclusion ---------------------------------------------------------- */
.conclusion {{
  background: var(--surface-raised);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  padding: 1.15rem 1.3rem;
}}
.conclusion .text {{ font-size: 1.02rem; line-height: 1.62; color: var(--text); }}

/* --- findings ------------------------------------------------------------ */
.finding {{
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 0.85rem 1rem; margin-bottom: 0.7rem; background: var(--surface-raised);
}}
.finding.material {{ border-left: 3px solid var(--accent); }}
.finding .statement {{ color: var(--text); font-size: 0.95rem; line-height: 1.55; }}
.hypothesis {{
  border-top: 1px dashed var(--border);
  padding: 0.55rem 0 0.2rem; margin-top: 0.6rem;
}}
.hypothesis .claim {{ color: var(--text); font-size: 0.9rem; }}
.hypothesis .why {{ color: var(--text-muted); font-size: 0.82rem; margin-top: 0.2rem; }}

/* --- timeline ------------------------------------------------------------ */
.timeline {{ position: relative; padding-left: 1.05rem; }}
.timeline::before {{
  content: ""; position: absolute; left: 4px; top: 6px; bottom: 6px;
  width: 1px; background: var(--border);
}}
.tl-row {{ position: relative; padding: 0.28rem 0; }}
.tl-row::before {{
  content: ""; position: absolute; left: -1.05rem; top: 0.62rem;
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--border-strong); box-shadow: 0 0 0 3px var(--surface);
}}
.tl-row.active::before {{ background: var(--accent); }}
.tl-row.error::before  {{ background: #b23c3c; }}
.tl-node {{ font-size: 0.85rem; font-weight: 560; color: var(--text); }}
.tl-meta {{ font-size: 0.78rem; color: var(--text-muted); }}

/* --- evidence ------------------------------------------------------------ */
.evidence {{
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.6rem 0.75rem; margin-bottom: 0.55rem;
}}
.evidence .purpose {{ font-size: 0.85rem; color: var(--text); margin-bottom: 0.3rem; }}
.evidence .meta {{ font-size: 0.76rem; color: var(--text-muted); }}

/* --- approval banner ----------------------------------------------------- */
.approval {{
  background: var(--surface-raised);
  border: 1px solid #d9b866; border-left: 3px solid #9a6b00;
  border-radius: var(--radius); padding: 1rem 1.15rem; margin-bottom: 0.9rem;
}}
.approval .kind {{ font-weight: 640; color: var(--text); font-size: 0.95rem; }}
.approval .why {{ color: var(--text-secondary); font-size: 0.87rem; margin-top: 0.25rem; }}

/* --- stats --------------------------------------------------------------- */
.stat-row {{ display: flex; gap: 1.6rem; flex-wrap: wrap; }}
.stat .value {{ font-size: 1.28rem; font-weight: 650; color: var(--text); line-height: 1.2; }}
.stat .label {{
  font-size: 0.71rem; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--text-muted); margin-top: 0.1rem;
}}

/* --- code ---------------------------------------------------------------- */
code, pre, .stCode {{ font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }}
.stCode > div {{ border-radius: var(--radius-sm) !important; font-size: 0.82rem !important; }}

/* --- streamlit chrome ---------------------------------------------------- */
div[data-testid="stTextArea"] textarea,
div[data-testid="stTextInput"] input {{
  border-radius: var(--radius-sm); border-color: var(--border-strong);
  font-size: 0.95rem;
}}
.stButton > button {{
  border-radius: var(--radius-sm); font-weight: 560; font-size: 0.88rem;
  border-color: var(--border-strong);
}}
div[data-testid="stExpander"] {{
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-raised);
}}
div[data-testid="stExpander"] summary {{ font-size: 0.86rem; font-weight: 560; }}
section[data-testid="stSidebar"] {{
  background: var(--surface-sunken); border-right: 1px solid var(--border);
}}
#MainMenu, footer, header {{ visibility: hidden; }}
hr {{ border-color: var(--border); }}
</style>
"""


def chip(label: str, icon: str, colour: str) -> str:
    """A status chip: colour, icon and text together, never colour alone."""
    return (
        f'<span class="chip" style="color:{colour}">'
        f'<span class="icon">{icon}</span>{label}</span>'
    )


def status_chip(key: str | None) -> str:
    status = status_of(key)
    return chip(status.label, status.icon, status.colour)


def confidence_chip(level: str | None) -> str:
    status = CONFIDENCE.get(level or "", Status(level or "unknown", "○○○", "#6b6a66", "neutral"))
    return chip(status.label, status.icon, status.colour)
