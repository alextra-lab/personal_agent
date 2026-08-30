"""Unit tests for the SearXNG engine-parity check (FRE-1331 AC-4).

These pin the two behaviours that decide whether the check is a real guard or a
decoration. Both were found by hand during the ticket, and one of them was a live
defect in the first revision of the script:

* the remove-list must actually be subtracted, or every deliberately-removed engine
  reports as missing and the check is dismissed as noisy;
* an unrecognised ``use_default_settings.engines`` shape must be REFUSED, not
  defaulted to "nothing removed". The first revision defaulted, and printed OK on a
  settings file SearXNG could not honour — fail-open on a guard, which is exactly the
  pathology the check exists to detect elsewhere.

No running SearXNG is needed: the parsing half is pure, and it is the half that can
silently lie.
"""

from __future__ import annotations

import pytest
import yaml
from scripts.monitors.searxng_engine_parity import declared_engines, settings_path


def test_declared_engines_subtracts_the_remove_list() -> None:
    """Engines named in `use_default_settings.engines.remove` are not expected to load.

    They are an intentional absence — a module missing from the image, or deleted
    upstream — so reporting them as missing would make every run noisy and train the
    reader to ignore it.
    """
    config = yaml.safe_load(
        """
        use_default_settings:
          engines:
            remove:
              - reddit
              - wikidata
        engines:
          - name: brave
          - name: reddit
          - name: arxiv
        """
    )
    assert declared_engines(config) == {"brave", "arxiv"}


def test_declared_engines_ignores_entries_without_a_name() -> None:
    """A malformed engine entry is skipped rather than raising.

    SearXNG itself tolerates these; the parity check should not be stricter than the
    thing it is checking, or it fails for a reason unrelated to engine parity.
    """
    config = yaml.safe_load(
        """
        engines:
          - name: brave
          - shortcut: nameless
          - name: arxiv
        """
    )
    assert declared_engines(config) == {"brave", "arxiv"}


def test_declared_engines_handles_a_file_with_no_remove_list() -> None:
    """`use_default_settings` is optional; its absence means nothing is removed."""
    config = yaml.safe_load("engines:\n  - name: brave\n")
    assert declared_engines(config) == {"brave"}


def test_declared_engines_refuses_a_non_mapping_remove_section() -> None:
    """A wrong-shaped `use_default_settings.engines` must REFUSE, never default to empty.

    This is the regression that matters. The first revision of this script treated an
    unrecognised shape as "nothing removed" and went on to print OK, exit 0, on a
    settings file SearXNG could not honour. A guard that reports green on input it does
    not understand is worse than no guard, because it is trusted.
    """
    config = yaml.safe_load(
        """
        use_default_settings:
          engines:
            - name: this-is-a-list-not-a-mapping
        engines:
          - name: brave
        """
    )
    with pytest.raises(ValueError, match="not a mapping"):
        declared_engines(config)


def test_declared_engines_refuses_a_non_list_engines_section() -> None:
    """A top-level `engines:` that is not a list is not a SearXNG settings file."""
    config = yaml.safe_load("engines:\n  brave: true\n")
    with pytest.raises(ValueError, match="not a list"):
        declared_engines(config)


def test_settings_path_prefers_the_real_file_when_it_exists() -> None:
    """The real settings.yml is what the container mounts; the template is the fallback.

    The real file is gitignored (FRE-1310) because it carries API keys, so a fresh
    clone and CI have only the template — but on a deployed host the real file is the
    one whose parity actually matters.
    """
    resolved = settings_path(None)
    assert resolved.name in {"settings.yml", "settings.yml.example"}
    assert resolved.parent.name == "searxng"


def test_settings_path_honours_an_explicit_override() -> None:
    """An explicit --settings path wins, which is what makes the seeded negative possible."""
    assert settings_path("/tmp/seeded.yml").as_posix() == "/tmp/seeded.yml"
