from __future__ import annotations

import copy

import pytest
from coral_player.manifest import Action, EmptyManifest, InvalidManifest, parse_manifest
from player_helpers import blob, item_for, make_manifest


def test_valid_manifest_is_parsed_and_sorted():
    raw = make_manifest([item_for(blob(2), 2), item_for(blob(1), 1, start="2026-10-01")])
    manifest = parse_manifest(raw)
    assert [i.position for i in manifest.items] == [1, 2]
    assert manifest.items[0].start_date.isoformat() == "2026-10-01"
    assert manifest.action is Action.APPLY


def test_empty_manifest_without_flag_is_refused():
    with pytest.raises(EmptyManifest):
        parse_manifest(make_manifest([]))


def test_cleared_manifest_is_accepted():
    manifest = parse_manifest(make_manifest([], cleared=True))
    assert manifest.action is Action.CLEAR


def test_unassigned_manifest_means_keep():
    manifest = parse_manifest(make_manifest([], assigned=False))
    assert manifest.action is Action.KEEP


def test_cleared_with_items_is_invalid():
    with pytest.raises(InvalidManifest):
        parse_manifest(make_manifest([item_for(blob(1), 1)], cleared=True))


def test_tampered_manifest_fails_hash_check():
    raw = make_manifest([item_for(blob(1), 1)])
    tampered = copy.deepcopy(raw)
    tampered["items"][0]["size"] += 1
    with pytest.raises(InvalidManifest, match="playlist_hash"):
        parse_manifest(tampered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "../../etc/passwd"),
        ("size", 0),
        ("size", True),
        ("url", "https://evil.example/x"),
        ("kind", "audio"),
        ("ext", "sh"),
        ("start_date", "2026-13-01"),
        ("duration", -1),
    ],
)
def test_invalid_items_are_rejected(field, value):
    item = item_for(blob(1), 1)
    item[field] = value
    with pytest.raises(InvalidManifest):
        parse_manifest(make_manifest([item]))


@pytest.mark.parametrize("raw", [None, [], {"version": 2}, "text"])
def test_garbage_is_rejected(raw):
    with pytest.raises(InvalidManifest):
        parse_manifest(raw)


def test_unknown_timezone_is_rejected():
    with pytest.raises(InvalidManifest, match="timezone"):
        parse_manifest(make_manifest([item_for(blob(1), 1)], timezone="Mars/Olympus"))


def test_dashboard_manifest_is_accepted_by_player():
    """The two sides must agree on the canonical form of the hash."""
    from coral_dashboard.manifest import ManifestContext, build_manifest

    rows = [
        {
            "kind": "image",
            "image_duration_s": None,
            "duration_s": None,
            "stored_name": "a" * 64 + ".png",
            "sha256": "a" * 64,
            "size": 10,
            "position": 1,
            "start_date": "2026-10-01",
            "end_date": None,
        },
        {
            "kind": "video",
            "image_duration_s": None,
            "duration_s": 12.345,
            "stored_name": "b" * 64 + ".mp4",
            "sha256": "b" * 64,
            "size": 99,
            "position": 2,
            "start_date": None,
            "end_date": "2026-12-31",
        },
    ]
    ctx = ManifestContext("Europe/Lisbon", 10)
    for kwargs in (
        {"assigned": True, "cleared": False},
        {"assigned": True, "cleared": True},
        {"assigned": False, "cleared": False},
    ):
        parse_manifest(build_manifest(rows, ctx, **kwargs))
