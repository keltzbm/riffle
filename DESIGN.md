# Riffle — Design

A rewrite of my original MTG scripts, which are tagged `v0.1.0`.

## Purpose

One source of truth for what cards I own, what decks I'm building, and what
the gap costs — paper and MTGO. Everything else follows from that.

Two problems motivated the rewrite, both hit in practice:

1. **Wrong printings on import** — decklists matched by name, so a Moxfield
   import picked arbitrary printings.
2. **Ownership checks returning false** — a precon's contents weren't in the
   collection export, so 30 owned cards read as "need to buy." Fixed by
   scanning precons into ManaBox: the export is the one record of what you own.

Both are the same bug: **name is not a key.**

## Decisions

| Decision | Why |
|---|---|
| Key cards by Riffle's `card_id` | Names collide and differ across printings. Resolve once, at the edge. The ID is derived from Scryfall's `oracle_id` and outlives it. |
| Scryfall bulk JSON, not scraping (MTGO decklists are the one scrape — no API exists) | Daily, authoritative, includes legalities and prices — **paper USD and MTGO tix** (Scryfall sources tix from Cardhoarder). |
| Price history is our own daily snapshot, stored raw | tcgcsv took its bulk archive down in September 2026, so its history can't be backfilled. Each day Riffle fetches every set's price file for the games it covers (one file at a time, once a day, as tcgcsv asks), keeps Scryfall's prices for Magic, keeps MTGJSON's, several stores in one file, whose 90-day file starts Magic's history three months back, keeps GoatBots' MTGO prices, with its yearly archives for the years before, keeps Cardmarket's price guide for every game it sells and its accessories, and keeps Card Kingdom's and Mana Pool's whole price lists, every condition and quantity. Files are stored as returned so any later loader can re-read them. |
| Postgres for the system of record (v0.4.0) | Constraints, transactions, and many writers; the catalog and events move there patch by patch. Migrations (Alembic) ship inside the package, and a test checks they build exactly what the ORM models define. |
| Riffle's own IDs, derived from the source's | Cards, printings, and sets get a UUIDv5 of a fixed namespace and the creating source's ID (`db/ids.py`): the same on every machine, so the catalog can be rebuilt from its sources. One source per game creates rows (Scryfall for Magic); others only map their IDs onto them in `external_ids`. |
| Loads stage, then merge | COPY into temporary tables, ANALYZE them, then one join-filtered upsert per table (`db/catalog.py`): only new and changed rows are written, so a repeat load writes nothing. Rows the source stops listing are retired, never deleted. |
| Commands read cards from Postgres | One read-only, repeatable-read snapshot per command (`store/postgres.py`). Names load once, for every card; prices, printings, and rules are fetched in one query per collection or deck, never one per card. The collection is re-read from the ManaBox CSV each run — 2,500 rows don't need a table yet. |
| Dataclasses, stdlib where possible | Validation happens at ingest. |
| WUBRG color ordering | The old `sorted()` produced `BGU`; every external source says `UBG`. |
| Vault is a render target | Moxfield owns decklists, ManaBox owns the collection, deck notes own reasoning. |
| Deck notes are an input | The fenced list under "Moxfield import" in each note is what the tool reads. |

## Vault write contract

```
~/atelier/library/games/tcg/mtg/    the vault, the notes folder (notes), mtg/
├── _generated/     rewritten every sync — only when content changes
│   ├── <deck>.data.md          owned/buy counts, paper and MTGO totals, buy table
│   └── collection-summary.md
├── _log/           append-only
│   ├── prices.md               closed in 0.4.0: `riffle prices log` reads the store's days
│   └── <deck>.versions.md      a +/- diff each time a list changes
└── commander/ …    authored — read, never written
```

Authored notes embed generated ones with `![[aesi-lands.data]]`.
One writer per file: scripts never write authored notes; you never edit logs.

## Where things live

`~/atelier/github/riffle` sits **beside** the vault, not in it. Card data, the
collection CSV, and sync state live in `~/.local/share/riffle`, outside anything
pCloud syncs. Config is `~/.config/riffle/config.toml`: `vault` names the vault,
`notes` the folder in it that holds `mtg/`.

## Milestones

| | | Status |
|---|---|---|
| 1 | Collection truth: Scryfall + ManaBox, `riffle own` | done |
| 2 | Obsidian export: `_generated/`, `riffle sync` | done |
| 3 | Analysis: mana demand/supply, curve, legality, playset eligibility | legality + playsets done; mana not started |
| 4 | Prices and logging: paper + MTGO, price log, list versions | done — log rollup still to do |
| 5 | Metagame ingest: MTGO decklists only | ingest + card stats done |
| — | Export: Moxfield, ManaBox, MTGO .txt, TCGplayer mass entry, owned-printing pins | done |

## Layout

```
src/riffle/
├── config.py       XDG paths, config.toml
├── net.py          HTTP for every source: User-Agent, retries, 429s, streamed downloads
├── vault.py        read deck notes, frontmatter, buy lines — read-only
├── sync.py         the work behind `riffle sync`, CLI- and DB-free
├── models/         Printing, Prices, Deck, DeckEntry, Holding
├── ingest/         scryfall, scryfall_catalog (bulk file → Postgres), manabox, decklist, arena, mtgo, tcgcsv,
│                   mtgjson, goatbots, cardmarket, pricelists (whole store price lists)
├── store/          Catalog protocol + Postgres implementation
├── db/             Postgres: engine, ORM models, migrations, `db up`, ids (derived IDs, aliases.toml),
│                   catalog (the stage-and-merge loader any game's source uses)
├── analysis/       resolve, ownership, pricing, colors, legality, metagame
├── export/         formats (moxfield/manabox/mtgo/tcgplayer), obsidian
└── cli.py          typer app — the only entry point
tests/              run against an in-memory Catalog; no download needed
```
