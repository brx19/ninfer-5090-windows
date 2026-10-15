"""The profile gate's documentation-ownership contract, and its regression cases.

Why this gate exists: the 2026-10-15 fast-gates run (brx19/ninfer-5090-windows run 38700892274)
failed with 33 disagreements, all of them the gate demanding the eight-lane benchmark table from
README.md -- a document the automated upstream synchronization owns and rewrites independently of
this fork, and which has never carried the table in this repository's history. The table lives in
RELEASE_NOTES.md, the fork-owned transcription site. The contract is now explicit in
check_profile_consistency.doc_contract_applies: RELEASE_NOTES.md must always transcribe
profiles.PROFILES; any other document is bound by the table contract only while it actually
references the shipped launchers, so a legitimate upstream README update cannot reintroduce this
failure while a shared doc that does name a lane is still held to the table.

Every case here drives the real check functions against a temporary tree, so nothing touches the
repository's documents. The clean case is asserted first, deliberately: a gate that always fails
is a gate that gets muted.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "release"))

import check_profile_consistency as gate  # noqa: E402
from profiles import PROFILES  # noqa: E402

SPEC_LABELS = {"dflash2": "DFlash2", "mtp": "MTP"}
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}


def valid_release_notes_body() -> str:
    """A RELEASE_NOTES body that transcribes PROFILES exactly, as the real one does."""
    rows = []
    for profile in PROFILES:
        spec = f"{SPEC_LABELS[profile['spec']]} ({profile['draft']})"
        rows.append(
            f"| `{profile['file']}` | artifact | {spec} | yes | 262,144 | "
            f"{round(profile['tok'])} tok/s | {profile['acc']} |"
        )
    k8v4 = len([p for p in PROFILES if p.get("kv_dtype") == "k8v4"])
    return "\n".join(rows) + f"\n\n**{WORDS[k8v4]} lanes ship `--kv-dtype k8v4`**\n"


def upstream_style_readme() -> str:
    """A legitimate upstream document: general usage, no fork launcher, no profile content."""
    return "# NInfer\n\nA C++/CUDA inference engine for a single GPU.\n\nSee the executable --help.\n"


def run_doc_checks(root: Path, readme: str, notes: str) -> list[str]:
    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    gate.failures.clear()
    gate.notes.clear()
    gate.check_doc_tables(root)
    gate.check_docs(root)
    return list(gate.failures)


def test_upstream_style_readme_does_not_break_the_gate(tmp_path: Path) -> None:
    failures = run_doc_checks(tmp_path, upstream_style_readme(), valid_release_notes_body())
    assert failures == [], "\n".join(failures)
    # The exemption is visible, not silent: the note names the doc the contract does not bind.
    assert any("README.md" in note for note in gate.notes)


def test_shared_doc_naming_a_lane_must_match_the_table(tmp_path: Path) -> None:
    # A README that does reference a shipped launcher is a transcription site: one wrong
    # figure in it must fail the gate exactly as the same drift would in RELEASE_NOTES.
    first = PROFILES[0]
    good = valid_release_notes_body()
    wrong = good.replace(f"{round(first['tok'])} tok/s", f"{round(first['tok']) + 1} tok/s")
    failures = run_doc_checks(tmp_path, wrong, valid_release_notes_body())
    assert any(first["file"] in failure and "tok/s" in failure for failure in failures)


def test_release_notes_must_carry_the_table(tmp_path: Path) -> None:
    # The fork-owned transcription site is never exempt: dropping a value from it fails.
    first = PROFILES[0]
    notes = valid_release_notes_body().replace(f"| {first['acc']} |", "")
    failures = run_doc_checks(tmp_path, upstream_style_readme(), notes)
    assert any(first["acc"] in failure for failure in failures)


def test_contract_helper_applies_the_ownership_split() -> None:
    assert gate.doc_contract_applies("RELEASE_NOTES.md", "") is True
    assert gate.doc_contract_applies("README.md", upstream_style_readme()) is False
    assert gate.doc_contract_applies("README.md", f"start here: {PROFILES[0]['file']}\n") is True
