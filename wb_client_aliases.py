"""Client-facing model-name and reasoning-effort aliases.

Two gaps that every Claude-shaped client and every Codex-shaped client hit on
this gateway, and that used to be filled by a local forwarding layer:

1. **``claude-*`` model ids.** Claude Desktop's "third-party inference" panel
   only accepts model ids that look like ``claude-*``. The upstream knows none
   of them (``claude-sonnet-5`` -> ``400 / 11102 model ... service info not
   found``), so the client could not be pointed at this gateway at all.
   :func:`resolve_claude_model` maps such a name onto a real upstream id by
   longest prefix. The reply keeps naming the *real* id: a client that reads
   the echoed model otherwise believes it is talking to Claude.

2. **Effort levels the upstream does not have.** Codex's picker tops out at
   ``xhigh`` (and some clients send ``ultra``); the DeepSeek builds on the CN
   exit publish ``low / high / max``. Left alone, ``xhigh`` is either rejected
   or silently downgraded to the model default (:func:`wb_proxy.map_reasoning_effort`
   rewrites an unsupported level to the catalog default, which for
   ``deepseek-v4.1-flash`` is ``high``). :func:`normalize_effort` rewrites the
   value to the level this build actually runs at. A model that *does* declare
   the level keeps it - ``deepseek-v4-pro`` publishes ``high / xhigh``, so
   mapping it to ``max`` there would be a downgrade.

Both tables are configuration, not code: environment variables for a one-off
deployment, the panel's ``settings.json`` for a persistent one, and a built-in
default that matches what this deployment serves.

The module deliberately imports nothing from ``wb_proxy`` / ``wb_settings`` so
it can be dropped next to a running installation and called from anywhere
(``wb_proxy`` wraps it; the settings reader lives in ``wb_settings``).
"""
import json
import os

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
# The CN exit renamed deepseek-flash to deepseek-v4.1-flash (see
# wb_proxy.INTL_MODEL_ALIASES for the same rename on the intl side), so the
# default targets are the ids /v1/models actually advertises here.
DEFAULT_CLAUDE_ALIASES = {
    "claude-opus": "deepseek-v4-pro",
    "claude-sonnet": "deepseek-v4.1-flash",
    "claude-haiku": "deepseek-v4.1-flash",
}

# Levels clients invent that no WorkBuddy model declares. Mapping them to the
# top level the gateway can run is the whole point: "the client asked for more
# thinking than the picker allows" must not turn into "the model default".
DEFAULT_EFFORT_ALIASES = {
    "xhigh": "max",
    "ultra": "max",
}

# ``off`` / ``0`` / ``false`` ... disable a table through its env var without
# removing it from the environment of a service manager.
_OFF_VALUES = ("0", "false", "no", "off", "none", "disabled", "disable")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def env_table_disabled(name):
    """True when ``<name>`` is set to an explicit off value."""
    return _text(os.environ.get(name)).lower() in _OFF_VALUES


def parse_pairs(raw, normalize_keys=True):
    """Normalise a table written as an object, JSON, or ``[{"from","to"}]``.

    The object spelling is what ``settings.json`` and the panel send; the list
    spelling is what a hand-written config or a client-side editor tends to
    produce; a JSON string is what a service manager's env var carries.
    Malformed entries are dropped instead of raising: a broken alias table must
    never turn one request into a 500.
    """
    if isinstance(raw, str) and raw.strip()[:1] in ("{", "["):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    pairs = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = item.get("from") or item.get("alias") or item.get("name") \
                or item.get("prefix")
            target = item.get("to") or item.get("target") or item.get("model")
            pairs.append((source, target))
    elif isinstance(raw, str):
        for chunk in raw.replace(";", ",").split(","):
            if not chunk.strip():
                continue
            if "=" in chunk:
                source, _, target = chunk.partition("=")
            elif ":" in chunk:
                source, _, target = chunk.partition(":")
            else:
                continue
            pairs.append((source, target))
    out = {}
    for source, target in pairs:
        key = _text(source)
        value = _text(target)
        if not key or not value:
            continue
        if normalize_keys:
            key = key.lower()
        if key.lower() == value.lower():
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Claude -> real model id
# ---------------------------------------------------------------------------
def claude_table(settings=None):
    """The prefix table to honour: settings > env > built-in default.

    Order matters per key, not as a whole: an operator who only overrides
    ``claude-opus`` keeps the shipped sonnet/haiku mapping.

    Env spellings, all optional::

        CLAUDE_ALIAS_OPUS=deepseek-v4-pro
        CLAUDE_ALIAS_SONNET=deepseek-v4.1-flash
        CLAUDE_ALIAS_HAIKU=deepseek-v4.1-flash
        WB_CLAUDE_ALIASES="claude-opus=deepseek-v4-pro,claude-sonnet=other"  # replaces
        WB_CLAUDE_ALIASES=off                                                # disables
    """
    if env_table_disabled("WB_CLAUDE_ALIASES"):
        return {}
    table = dict(DEFAULT_CLAUDE_ALIASES)
    raw = os.environ.get("WB_CLAUDE_ALIASES")
    if _text(raw):
        table = parse_pairs(raw)
    for family in ("OPUS", "SONNET", "HAIKU"):
        value = _text(os.environ.get("CLAUDE_ALIAS_" + family))
        if value:
            table["claude-" + family.lower()] = value
    overrides = parse_pairs(settings)
    if overrides:
        table.update(overrides)
    return table


def resolve_claude_model(name, table=None):
    """The real upstream id for a ``claude-*`` name, or ``""``.

    Two match rules, in order:

      * **explicit prefix** - ``claude-sonnet-4-5`` matches
        ``claude-sonnet-4-5-20250929``; longest prefix wins, so a version-
        specific entry beats the family one;
      * **family token** - Claude 3.x ids put the version *first*
        (``claude-3-5-sonnet-20241022``, ``claude-3-opus-20240229``), so a
        family entry (``claude-sonnet`` / ``claude-opus``) also matches when
        the family appears as a hyphen-delimited token after ``claude``.

    Matching is case-insensitive: Claude clients have shipped both
    ``claude-Sonnet-5`` and ``claude-sonnet-5``. A name that maps to itself is
    left alone, and any name not starting with ``claude`` never matches.
    """
    text = _text(name)
    if not text:
        return ""
    table = DEFAULT_CLAUDE_ALIASES if table is None else table
    low = text.lower()
    tokens = low.split("-")
    best_prefix = ""
    best_target = ""
    families = {}
    for prefix, target in (table or {}).items():
        key = _text(prefix).lower()
        value = _text(target)
        if not key or not value or value.lower() == low:
            continue
        parts = key.split("-")
        if len(parts) == 2 and parts[0] == "claude" and parts[1]:
            families[parts[1]] = value
        if low.startswith(key) and len(key) > len(best_prefix):
            best_prefix = key
            best_target = value
    if best_target:
        return best_target
    if low.startswith("claude") and len(tokens) > 1:
        best_family = ""
        best_value = ""
        for family, value in families.items():
            if family in tokens[1:] and len(family) > len(best_family):
                best_family = family
                best_value = value
        if best_value:
            return best_value
    return ""


def apply_claude_alias(payload, table=None):
    """Rewrite ``payload["model"]`` in place. Returns ``(from, to)`` or ``("", "")``.

    Only the request is rewritten; the response builders decide what to echo
    (see ``wb_proxy.client_facing_model``: a claude alias echoes the real id).
    """
    if not isinstance(payload, dict):
        return ("", "")
    name = payload.get("model")
    if not isinstance(name, str):
        return ("", "")
    target = resolve_claude_model(name, table)
    if not target:
        return ("", "")
    payload["model"] = target
    return (_text(name), target)


# ---------------------------------------------------------------------------
# Effort level normalisation
# ---------------------------------------------------------------------------
def effort_table(settings=None):
    """The effort table to honour: settings > env > built-in default.

    ``WB_EFFORT_ALIASES`` accepts ``"xhigh:max,ultra:max"``, a JSON object, or
    ``off`` to keep the client's value byte-for-byte.
    """
    if env_table_disabled("WB_EFFORT_ALIASES"):
        return {}
    raw = _text(os.environ.get("WB_EFFORT_ALIASES"))
    table = parse_pairs(raw) if raw else dict(DEFAULT_EFFORT_ALIASES)
    overrides = parse_pairs(settings)
    if overrides:
        table.update(overrides)
    return table


def effort_slot(body):
    """Where the effort lives in a request body: ``(holder, key)`` or ``(None, "")``.

    Three spellings reach this gateway:

      * ``reasoning_effort`` - Chat Completions (snake case, the official one);
      * ``reasoningEffort``  - Chat Completions (camel case, some SDKs);
      * ``reasoning.effort`` - Responses API, which ``responses_to_chat``
        translates into ``reasoning_effort`` when it builds the chat body.
    """
    if not isinstance(body, dict):
        return (None, "")
    for key in ("reasoning_effort", "reasoningEffort"):
        if key in body:
            return (body, key)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        return (reasoning, "effort")
    return (None, "")


def normalize_effort(body, model=None, table=None, supported=None):
    """Rewrite one request's effort alias in place. Returns ``(from, to)``.

    ``supported`` is the model's own level list from the catalog. When the
    model really declares the level the client sent, the request is left
    alone: ``deepseek-v4-pro`` publishes ``high / xhigh``, and rewriting its
    ``xhigh`` to ``max`` would hand the upstream a level it does not have
    (``map_reasoning_effort`` would then fall back to ``high``, i.e. *less*
    thinking than the client asked for).

    Nothing is rewritten when ``from`` and ``to`` are the same level, when the
    body carries no effort at all, or when the table is empty.
    """
    if not table or not isinstance(body, dict):
        return ("", "")
    holder, key = effort_slot(body)
    if holder is None:
        return ("", "")
    raw = holder.get(key)
    if not isinstance(raw, str):
        return ("", "")
    current = raw.strip()
    if not current:
        return ("", "")
    target = table.get(current.lower())
    if not target:
        return ("", "")
    target = _text(target)
    if not target or target.lower() == current.lower():
        return ("", "")
    levels = set()
    for level in supported or []:
        text = _text(level).lower()
        if text:
            levels.add(text)
    if current.lower() in levels:
        return ("", "")
    if holder is body:
        body[key] = target
    else:
        # Copy before writing: build_upstream_body() works on a shallow copy of
        # the client payload, and mutating a nested dict would leak the rewrite
        # back into the caller's request object.
        nested = dict(holder)
        nested[key] = target
        body["reasoning"] = nested
    return (current, target)


# ---------------------------------------------------------------------------
# Anthropic thinking budget -> effort (documentation + logging)
# ---------------------------------------------------------------------------
# Claude Desktop expresses "how hard to think" with ``thinking.budget_tokens``,
# not with an effort level. wb_proxy.anthropic_effort_from_messages() already
# turns a budget into a level; this table is the same conversion written down,
# for operators reading a log:
#
#   budget < 8000          -> low
#   8000 <= budget < 16000 -> medium   (no WorkBuddy model declares medium;
#                                       map_reasoning_effort falls back to the
#                                       model default, i.e. high)
#   16000 <= budget < 32000 -> high
#   budget >= 32000        -> xhigh    (-> max through the effort table above)
#
# Deliberate trade-off: the gateway does not try to reverse-engineer Claude's
# exact budget for "medium"; it maps the four bands the client actually sends
# and lets the effort table decide the top band. Rewriting the budget itself is
# not needed - the level derived from it travels into the chat body.
THINKING_BUDGET_BANDS = (
    (32000, "xhigh"),
    (16000, "high"),
    (8000, "medium"),
    (0, "low"),
)


def thinking_budget_effort(budget):
    """The level Anthropic's ``budget_tokens`` corresponds to, or ``""``."""
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value >= 32000:
        return "xhigh"
    if value >= 16000:
        return "high"
    if value >= 8000:
        return "medium"
    return "low"
