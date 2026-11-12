"""Record source-backed hand corrections without changing score files."""

from dataclasses import replace
from pathlib import Path

from omr_ingestor.musicxml import part_ids, read_musicxml
from omr_ingestor.status import ItemStatus, PartIdentity, StatusError, read_status, write_status


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
    declarations = root.findall("part-list/score-part")
    declared_ids = [part.get("id", "") for part in declarations]
    if (
        not ids
        or any(not pid.strip() for pid in ids)
        or len(set(ids)) != len(ids)
        or len(set(declared_ids)) != len(declared_ids)
        or set(declared_ids) != set(ids)
    ):
        raise StatusError("unsafe part identity: IDs must be unique and match declarations")
    if status.part_ids is not None and status.part_ids != ids:
        raise StatusError("recorded part IDs differ from the raw score; rerun before reviewing")
    if part_id is None:
        if len(ids) != 1:
            raise StatusError(f"multipart item requires --part; known parts: {list(ids)}")
        part_id = ids[0]
    if part_id not in ids:
        raise StatusError(f"unknown part {part_id!r}; known parts: {list(ids)}")
    if len(set(measures)) != len(measures):
        raise StatusError("duplicate measure numbers in review")
    part = next(p for p in root.findall("part") if p.get("id") == part_id)
    numbers = [m.get("number", "") for m in part.findall("measure")]
    for number in measures:
        if not number.strip() or number not in numbers:
            raise StatusError(f"part {part_id}: invalid measure {number!r}")
        if numbers.count(number) != 1:
            raise StatusError(f"part {part_id}: ambiguous duplicate measure {number!r} in score")
    identities = {
        p.get("id", ""): PartIdentity(
            part_name=p.findtext("part-name") or "",
            instruments=tuple(
                i.findtext("instrument-name") or "" for i in p.findall("score-instrument")
            ),
        )
        for p in declarations
    }
    prior = status.hand_corrected
    if len(ids) > 1 and isinstance(prior, tuple):
        if not replace_existing:
            raise StatusError("legacy item-wide review requires --replace to start per-part review")
        prior = None
    existing = prior.get(part_id, ()) if isinstance(prior, dict) else prior or ()
    identity_changed = (
        status.part_identities is not None
        and status.part_identities[part_id] != identities[part_id]
    ) or any(
        s.code == "part_identity_changed" and s.part_id == part_id for s in status.review_signals
    )
    if identity_changed and not replace_existing:
        raise StatusError(f"part {part_id}: identity changed; review again and use --replace")
    if not replace_existing and set(existing).intersection(measures):
        raise StatusError("duplicate measure numbers already recorded; use --replace")
    corrected = tuple(measures) if replace_existing else (*existing, *measures)
    for number in corrected:
        if numbers.count(number) != 1:
            raise StatusError(f"part {part_id}: prior measure {number!r} is absent or ambiguous")
    hand = {**(prior if isinstance(prior, dict) else {}), part_id: corrected}
    # Preserve legacy ID-only provenance for reviews of other parts.
    if status.part_identities is None and (
        any(pid != part_id for pid in hand) or (prior is not None and not replace_existing)
    ):
        recorded_identities = None
    else:
        recorded_identities = dict(status.part_identities or identities)
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
