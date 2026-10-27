"""Riffle's IDs: derived from the creating source's IDs, the same on every machine, forever."""

import uuid

import pytest

from riffle.db import ids

SOL_RING = "6ad8011d-3471-4369-9d68-b264cc027487"  # oracle id
SOL_RING_ALPHA = "c4300d24-1cae-4dd5-be7e-38cc677cf5bd"  # Scryfall id of the Alpha printing
ALPHA = "288bd996-960e-448b-a187-9504c1930c2c"  # Scryfall set id


@pytest.mark.parametrize(
    ("source", "external_id", "expected"),
    [
        ("scryfall_oracle", SOL_RING, "268a79bb-a878-5926-8b86-3af97d552149"),
        ("scryfall", SOL_RING_ALPHA, "be90543e-dbd2-5a2f-aa0d-6bfabf0eb6c5"),
        ("scryfall_set", ALPHA, "3312c949-9a17-5299-b809-6eea1f1a49fb"),
    ],
    ids=["card", "printing", "set"],
)
def test_ids_never_change(source, external_id, expected):
    """Pinned values: a change to the namespace or the seed format fails here, before it
    could give every row in every database a new ID."""
    assert ids.derive(source, external_id) == uuid.UUID(expected)


def test_the_namespace_is_derived_from_the_repository_url():
    assert uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/keltzbm/riffle") == ids.NAMESPACE


def test_one_id_from_two_sources_gives_two_uuids():
    assert ids.derive("scryfall", SOL_RING) != ids.derive("scryfall_oracle", SOL_RING)


def test_an_alias_derives_the_renamed_id_from_the_first():
    aliases = {"scryfall_oracle": {"new-oracle-id": SOL_RING}}
    assert ids.seed("scryfall_oracle", "new-oracle-id", aliases) == f"scryfall_oracle:{SOL_RING}"
    assert ids.derive("scryfall_oracle", "new-oracle-id", aliases) == ids.derive("scryfall_oracle", SOL_RING)
    assert ids.derive("scryfall", "new-oracle-id", aliases) == ids.derive("scryfall", "new-oracle-id")


def test_magic_is_created_by_scryfall_alone():
    assert ids.CREATORS["mtg"] == ids.Creators(
        card="scryfall_oracle", printing="scryfall", set="scryfall_set"
    )
    assert ids.creating_sources() == {"scryfall_oracle", "scryfall", "scryfall_set"}


def test_the_shipped_aliases_file_loads():
    assert ids.load_aliases() == {}


def test_aliases_load_from_toml(tmp_path):
    path = tmp_path / "aliases.toml"
    path.write_text(f'[scryfall_oracle]\n"new-oracle-id" = "{SOL_RING}"\n')
    assert ids.load_aliases(path) == {"scryfall_oracle": {"new-oracle-id": SOL_RING}}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[scryfal_oracle]\n"a" = "b"\n', r"\[scryfal_oracle\] isn't a creating source"),
        ("[scryfall]\n'a' = 1\n", "must map IDs to IDs"),
        ('scryfall = "a"\n', "must map IDs to IDs"),
        ('[scryfall]\n"c" = "b"\n"b" = "a"\n', "c point at other aliases"),
    ],
    ids=["unknown source", "a number", "not a table", "a chain"],
)
def test_bad_aliases_are_refused(tmp_path, text, message):
    path = tmp_path / "aliases.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        ids.load_aliases(path)
