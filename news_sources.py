# -*- coding: utf-8 -*-
"""What a news source is, and how to read a list of them.

The *shape* is method and stays here: a source has a name, a URL, a
category, a region, a language and an extraction mode, and a list of them is
loaded and validated the same way whoever is running it. The *list* is not.
Which thirty-odd feeds a particular desk watches is an editorial judgement
about that desk's coverage, and it moves with whoever makes it.

``mode`` picks the extraction path per source:

- ``"ml"`` — no LLM (TextRank summary, YAKE topics, spaCy NER, VADER
  sentiment). Cheap, and the NLP stack is English-tuned, so it suits
  English-language sources.
- ``"smart"`` — LLM extraction. Worth its cost for foreign-language sources,
  where the English-tuned pipeline degrades badly and a model that reads the
  language directly avoids needing one model per language.

There is deliberately **no default list** and no search path.
``load_sources`` requires an explicit file. A crawl silently running against
someone else's curation would produce a plausible digest of the wrong world.

YAML because the list is edited by hand and carries comments about why a
feed is in or out — the kind of thing that gets deleted when a format cannot
hold it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The extraction paths a source may ask for.
MODES = ("ml", "smart")

#: Closed vocabularies. Both are used to group a digest, and a free-text
#: value cannot be grouped — it just quietly forms a category of one.
CATEGORIES = ("financial", "central_bank", "geopolitical")
REGIONS = ("global", "us", "europe", "asia", "africa", "latam", "mena")

#: Every field a source must carry, in the order they are written.
FIELDS = ("name", "url", "category", "region", "lang", "mode")


@dataclass(frozen=True)
class NewsSource:
    name: str
    url: str
    category: str  # "financial" | "central_bank" | "geopolitical"
    region: str  # "global" | "us" | "europe" | "asia" | "africa" | "latam" | "mena"
    lang: str  # ISO 639-1
    mode: str  # "ml" | "smart"


class SourcesError(ValueError):
    """The source list is missing, malformed or incoherent."""


def _require(raw: dict, key: str, where: str) -> str:
    if key not in raw:
        raise SourcesError(f"{where}: missing '{key}'")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise SourcesError(f"{where}: '{key}' must be a non-empty string")
    if value != value.strip():
        # Refused rather than trimmed: " CNBC" and "CNBC" would silently
        # become the same source, hiding a typo in a hand-edited list.
        raise SourcesError(f"{where}: '{key}' must not have surrounding whitespace")
    return value


def load_sources(path: str | Path) -> tuple[NewsSource, ...]:
    """Load and validate a source list.

    Order is preserved and is part of the contract: a crawl walks the list in
    order, and a digest groups what it finds, so reordering changes what a
    reader sees first.

    Raises:
        SourcesError: The file is absent or unparseable, an entry is missing
            a field, a value is outside its vocabulary, or two entries share
            a name or a URL. Duplicates are refused rather than
            de-duplicated: in a hand-edited list a repeat is a mistake, and
            silently dropping one hides which.
    """
    import yaml

    p = Path(path)
    if not p.is_file():
        raise SourcesError(f"source list not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SourcesError(f"{p.name}: not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "sources" not in raw:
        raise SourcesError(f"{p.name}: expected a mapping with a 'sources' key")
    entries = raw["sources"]
    if not isinstance(entries, list) or not entries:
        raise SourcesError(f"{p.name}: 'sources' must be a non-empty list")

    sources: list[NewsSource] = []
    for i, entry in enumerate(entries):
        where = f"{p.name}: sources[{i}]"
        if not isinstance(entry, dict):
            raise SourcesError(f"{where}: expected a mapping, got {type(entry).__name__}")
        unknown = set(entry) - set(FIELDS)
        if unknown:
            raise SourcesError(f"{where}: unknown field(s) {sorted(unknown)}")
        values = {field: _require(entry, field, where) for field in FIELDS}
        if values["category"] not in CATEGORIES:
            raise SourcesError(
                f"{where}: category {values['category']!r} is not one of {list(CATEGORIES)}")
        if values["region"] not in REGIONS:
            raise SourcesError(
                f"{where}: region {values['region']!r} is not one of {list(REGIONS)}")
        if values["mode"] not in MODES:
            raise SourcesError(
                f"{where}: mode {values['mode']!r} is not one of {list(MODES)}")
        sources.append(NewsSource(**values))

    for field in ("name", "url"):
        seen: dict[str, int] = {}
        for i, source in enumerate(sources):
            value = getattr(source, field)
            if value in seen:
                raise SourcesError(
                    f"{p.name}: sources[{i}] repeats the {field} of sources[{seen[value]}]: "
                    f"{value!r}")
            seen[value] = i

    return tuple(sources)


__all__ = [
    "CATEGORIES",
    "FIELDS",
    "MODES",
    "REGIONS",
    "NewsSource",
    "SourcesError",
    "load_sources",
]
