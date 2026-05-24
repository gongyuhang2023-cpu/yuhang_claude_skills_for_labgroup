"""Parse `vocabulary.txt` into a category→terms dict and format for ASR.

The vocabulary file is a flat text file with three line types:

  - ``# CategoryName``  — category title (hash, single space, then a name).
                          Terms following this line are bucketed under it.
  - ``## anything``     — comment, always skipped.
  - ``term``            — a single vocabulary entry (one per line).
  - blank lines         — skipped.

Bare terms appearing before any category title are placed in a default
``General`` bucket. Re-declaring a category appends to the existing list
rather than silently overriding.

This module is intentionally pure: ``load`` only reads from disk,
``to_context_string`` is a string transform. No ASR or transcribe-side
imports — that integration lives in P2.2's transcribe module.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_CATEGORY = "General"


def load(path: str | Path) -> dict[str, list[str]]:
    """Parse ``path`` into ``{category: [terms]}`` preserving file order.

    Raises ``FileNotFoundError`` if the path does not exist — callers that
    want "no vocabulary configured" semantics should check up-front rather
    than rely on a silent empty return, so a missing-but-expected file
    surfaces as a typo immediately.

    Returns an empty dict for an empty file or a file containing only
    blank lines and ``##`` comments.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Vocabulary file not found: {path} / 找不到热词文件：{path}"
        )

    categories: dict[str, list[str]] = {}
    current = DEFAULT_CATEGORY  # only materialized in the dict on first term

    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            # `##` (double hash) → comment, always skip.
            if line.startswith("##"):
                continue
            # `# Name` (hash + space + name) → category title.
            if line.startswith("# "):
                current = line[2:].strip()
                categories.setdefault(current, [])
                continue
            # `#anything` without the trailing space is treated as a comment
            # (avoids misreading the `#nocomment` style as a category).
            if line.startswith("#"):
                continue
            # Plain term — bucket under current category, creating it lazily.
            categories.setdefault(current, []).append(line)

    return categories


def resolve_vocab_path(
    explicit: str | Path | None, meeting_dir: str | Path
) -> Path | None:
    """Find the vocabulary file to use, in convention order.

    Search order (first hit wins):
      1. ``explicit`` — if given, must be a real file; missing path raises
         FileNotFoundError. Users who pass ``--vocab path/to/typo.txt`` need
         a hard error, not a silent fallback.
      2. ``<meeting_dir>/vocabulary.txt`` — per-meeting override.
      3. ``<cwd>/vocabulary.txt`` — project-root convention.
      4. None — no vocabulary file found.

    Used by both the ``transcribe`` and ``postprocess`` CLI subcommands and
    by ``postprocess.run()`` when called programmatically. Lives here so
    both the parsing API and the discovery policy share a single home.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(
                f"Vocabulary file not found: {path}"
                f" / 找不到指定的热词文件:{path}"
            )
        return path
    meeting_dir = Path(meeting_dir)
    for cand in (meeting_dir / "vocabulary.txt", Path.cwd() / "vocabulary.txt"):
        if cand.is_file():
            return cand
    return None


def to_context_string(parsed: dict[str, list[str]]) -> str:
    """Render ``parsed`` into the ASR prompt format used by Qwen3-ASR.

    Output shape: ``"Cat1: term1, term2; Cat2: term3"`` — categories joined
    by ``; ``, terms within a category joined by ``, ``. Empty categories
    (header with no terms) are skipped so the model never sees ``"Empty: "``.

    Returns an empty string for ``{}`` or for a dict whose categories are
    all empty; callers should test the result with ``if context:`` before
    injecting into the ASR prompt.
    """
    parts = [
        f"{category}: {', '.join(terms)}"
        for category, terms in parsed.items()
        if terms
    ]
    return "; ".join(parts)
