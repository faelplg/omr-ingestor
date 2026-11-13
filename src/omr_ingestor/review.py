"""Record source-backed hand corrections without changing score files."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

from omr_ingestor.musicxml import has_safe_part_ids, part_ids, read_musicxml
from omr_ingestor.part_identity import raw_part_identities
from omr_ingestor.status import ItemStatus, StatusError, read_status, write_status


def record_review(
    item_dir: Path,
    measures: list[str],
    *,
    part_id: str | None = None,
    replace_existing: bool = False,
    dry_run: bool = False,
) -> ItemStatus:
    """Add corrections, or explicitly replace a review, using raw score numbering."""
    path = item_dir / "status.json"
    status = read_status(path)
    files = sorted((item_dir / "audiveris").rglob("*.musicxml"))
    if len(files) != 1:
        raise StatusError(f"{item_dir}: expected one raw Audiveris MusicXML, found {len(files)}")
    root = read_musicxml(files[0])
    ids = tuple(part_ids(root))
    if not ids or not has_safe_part_ids(root):
        raise StatusError(
            f"{files[0]}: unsafe part identity: IDs must be unique and match declarations"
        )
    if not set(status.part_ids or ()).issubset(ids):
        raise StatusError(
            "recorded part IDs are missing from the raw score; archive status.json and rerun "
            "before recording a fresh review"
        )
    if part_id is None:
        if len(ids) != 1:
            raise StatusError(f"multipart item requires --part; known parts: {list(ids)}")
        part_id = ids[0]
    if part_id not in ids:
        raise StatusError(f"unknown part {part_id!r}; known parts: {list(ids)}")
    if not measures and not replace_existing:
        raise StatusError("no measures given; use --replace to record a review with no corrections")
    duplicates = [number for number, count in Counter(measures).items() if count > 1]
    if duplicates:
        raise StatusError(f"duplicate measure numbers in review: {duplicates}")
    part = next(p for p in root.findall("part") if p.get("id") == part_id)
    numbers = [m.get("number", "") for m in part.findall("measure")]
    for number in measures:
        if not number.strip() or number not in numbers:
            raise StatusError(f"measure {number!r} not found in part {part_id} of {files[0]}")
        if numbers.count(number) != 1:
            raise StatusError(f"part {part_id}: ambiguous duplicate measure {number!r} in score")
    identities = raw_part_identities(root)
    prior = status.hand_corrected
    if len(ids) > 1 and isinstance(prior, tuple):
        if not replace_existing:
            raise StatusError("legacy item-wide review requires --replace to start per-part review")
        prior = None
    existing = prior.get(part_id, ()) if isinstance(prior, dict) else prior or ()
    identity_changed = (
        status.part_identities is not None
        and part_id in status.part_identities
        and status.part_identities[part_id] != identities[part_id]
    ) or any(
        s.code == "part_identity_changed" and s.part_id == part_id for s in status.review_signals
    )
    if identity_changed and not replace_existing:
        raise StatusError(f"part {part_id}: identity changed; review again and use --replace")
    duplicates = sorted(set(existing).intersection(measures))
    if not replace_existing and duplicates:
        raise StatusError(
            f"duplicate measure numbers already recorded: {duplicates}; use --replace"
        )
    corrected = tuple(measures) if replace_existing else (*existing, *measures)
    for number in corrected:
        if numbers.count(number) != 1:
            raise StatusError(f"part {part_id}: prior measure {number!r} is absent or ambiguous")
    hand = {**(prior if isinstance(prior, dict) else {}), part_id: corrected}
    # Preserve legacy ID-only provenance for reviews that have not been replaced.
    if status.part_identities is None and (
        any(pid != part_id for pid in hand) or (prior is not None and not replace_existing)
    ):
        recorded_identities = None
    else:
        recorded_identities = {**identities, **(status.part_identities or {})}
        recorded_identities[part_id] = identities[part_id]
    updated = replace(
        status,
        part_ids=ids,
        part_identities=recorded_identities,
        hand_corrected=hand if len(ids) > 1 else corrected,
        review_signals=tuple(
            s
            for s in status.review_signals
            if not (s.code == "part_identity_changed" and s.part_id == part_id)
        ),
    )
    if not dry_run:
        write_status(updated, path)
    return updated
