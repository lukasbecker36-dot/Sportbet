"""Team-name normalisation for matching FotMob records to Betfair markets."""

import re
import unicodedata

try:
    import config
except ImportError:  # pragma: no cover
    config = None

_TEAM_NAME_MAP = getattr(config, "TEAM_NAME_MAP", {}) if config else {}

# Common club-name suffixes/prefixes that differ between sources.
_NOISE_TOKENS = {"fc", "afc", "cf", "sc", "ac", "1899", "1. "}
_NOISE_RE = re.compile(r"\b(fc|afc|cf|sc|ac|ssc|ssd|us|az)\b", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalise(name: str) -> str:
    """Lowercase, drop accents, punctuation, and common club suffixes."""
    if not name:
        return ""
    text = _strip_accents(name).lower()
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonical(name: str) -> str:
    """Apply the configured FotMob->Betfair mapping, then normalise."""
    mapped = _TEAM_NAME_MAP.get(name, name)
    return normalise(mapped)
