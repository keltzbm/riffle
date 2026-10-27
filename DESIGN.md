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
<vault>/<notes>/mtg/               the vault, the notes folder in it (notes), mtg/
├── _generated/     rewritten every sync — only when content changes
│   ├── <deck>-data.md          owned/buy counts, paper and MTGO totals, buy table
│   └── collection-summary.md
├── _log/           append-only
│   ├── prices.md               closed in 0.4.0: `riffle prices log` reads the store's days
│   └── <deck>-versions.md      a +/- diff each time a list changes
├── .data/          hidden_folder: decks out of Obsidian's view, their notes beside them
│   ├── _generated/<deck>-data.md
│   ├── _log/<deck>-versions.md   moved here from _log/ when the deck moves in, and back out
│   └── commander/ …            authored — read, never written
└── commander/ …    authored — read, never written
```

Authored notes embed generated ones with `![[aesi-lands.data]]`.
One writer per file: scripts never write authored notes; you never edit logs.

## Where things live

The riffle folder sits **beside** the vault, not in it. Card data, the
collection CSV, and sync state live in `~/.local/share/riffle`, outside anything
a sync service copies. Config is `~/.config/riffle/config.toml`: `vault` names the vault,
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
├── times.py        shared time handling
├── locks.py        process locks
├── files.py        shared JSON-object reads and temporary-file replacement
├── net.py          HTTP for every source: retries, 429s, streamed downloads
├── runs.py         retained snapshots, differences, and verified writes
├── progress.py     step tracking, terminal progress, and plain logs
├── trickle.py      source-independent pacing, request logs, and owed items
├── links.py        shared card names, note links, and filenames
├── vault.py        read deck notes, frontmatter, and buy lines
├── sync.py         deck analysis and generated-note synchronization
├── models/         card, deck, collection, and metagame records; no database imports
├── watch/          watching, cadence, and lateness shared by source collectors
├── ingest/
│   ├── local/      manabox, arena, decklist, precons: read local files
│   ├── remote/     scryfall, scryfall_catalog, mtgjson, tcgcsv, goatbots,
│   │               cardmarket, pricelists, and shared empty-answer handling
│   └── mtgo/       pages (parsing), storage, and MTGO-specific trickle collection
├── ops/
│   ├── schedule/   jobs (shared definitions), dispatch, launchd, and systemd
│   ├── sync.py     full-sync workflow, independent of terminal handling
│   ├── ingest.py   source refreshes, price snapshots, and ManaBox pickup
│   ├── database.py tracked backups and database operation failures
│   ├── status.py   collection and job status
│   ├── joblogs.py  scheduled-run log readers
│   ├── disk.py     disk space and growth checks
│   └── checks.py   retained-file integrity and collection-gap checks
├── store/          Catalog protocol and Postgres reader
├── db/             engine, ORM tables, migrations, IDs, catalog loader, backups, Compose
├── analysis/       resolve, ownership, pricing, colors, legality, metagame
├── export/         card output, deck formats, and Obsidian output
└── cli/            __init__ registers the app; command modules and _common share its handling
tests/              grouped by these responsibilities; repo/ checks docs and repository policy
```

Start with a command in `cli/`, then follow its calls into operations, analysis,
ingestion, or the catalog. `cli/sync.py` handles options, terminal summaries,
exit codes, and watch polling. `ops/sync.py` coordinates a full sync: network
readiness, source refreshes, prices, collection pickup, notes, and the final
backup. It reports results through a small output protocol and progress steps,
without importing the CLI or Typer. `sync.py` handles deck analysis and generated
notes against the catalog protocol.

The ingest, prices, and database commands call the same operations as a sync.
Each price source can fail independently. Offline resyncs skip downloads and
backups; an online invocation still attempts its backup after a network failure
or a vault validation failure. An unavailable catalog ends the run before backup.

Metagame analysis imports `Event` and `MtgoDeck` from `models/metagame.py`, so it
does not load the MTGO collector. Scheduler dispatch selects a backend; both
backends depend on `ops/schedule/jobs.py`, which never imports them. Shared link
handling lives at the root because catalog readers and parsers also use it.

The executable remains `riffle.cli:app`. This layout changes no command names,
configuration keys, database revisions, retained-data paths, or installed job labels.

`files.replacing` creates the parent directory and replaces the target only when
the writing block finishes. Callers keep their serialization and cleanup policy:
metadata retains failed partials, notes use hidden temporary names and discard
failed writes, and `runs.writing` flushes and syncs durable data before replacement.
`files.read_json_object` supplies the shared empty-object fallback for disposable
caches. Readers that must report corruption, including sync state, remain strict.

Cardmarket's guide and product-list entry points share `_collect` in their source
module. It handles fetching, publication detection, validation, retention, ETags,
and cleanup. Resource-specific URLs, fields, labels, and missing-list rules remain
explicit there; this helper does not define a cross-source collection framework.

GoatBots uses its own `_collect` for prices and card definitions. ZIP parsing
stays in `_newest`/`_prices` and `_defined`; `_extra_days` handles price archives
containing multiple days. Shared collection checks publication times, compares
already-retained content, updates ETags, and preserves each resource's log fields.

## How sync runs

Start at `cli/sync.py:sync_cmd`, then read `ops/sync.py:run`. The first handles the
command; the second gives the workflow's order. Follow a step into its owning
module when you need its details:

1. **Enter the command.** `cli/sync.py` supplies the output adapter and translates
   an unavailable catalog or inventory into exit 1. `cli/_common.py:_run` opens
   the progress display or plain log and holds the shared run lock until the
   command ends. Its tracker remembers failed steps and sets the final exit code.
2. **Refresh online data.** `ops/sync.py:run` reads config and waits for any source
   host to accept a connection. An online run calls `ops/ingest.py:refresh`,
   retaining Scryfall bulk data and sets, then loading the card catalog into
   Postgres. No connection means a failed network step and an offline fallback.
3. **Keep prices.** `ops/ingest.py:snapshot_prices` builds a Scryfall price day
   from the retained bulk file, including offline. Online, it then asks MTGJSON,
   GoatBots, Cardmarket, Card Kingdom, Mana Pool, and TCGCSV, in that order. Each
   source reports its own failures so the remaining sources can run. The TCGCSV
   result returns to the output adapter for its summary.
4. **Pick up the collection.** After the online disk check,
   `ops/ingest.py:pick_up` scans Downloads newest first. It retains the current
   collection and all valid ManaBox exports before replacing the collection with
   a newer valid candidate. `copy_manabox` preserves the candidate's modification
   time, which determines whether the next download is newer. Manual restores
   use a later timestamp so an old download cannot undo them.
5. **Generate notes.** `ops/sync.py:_notes` validates the vault and collection,
   loads registered precon boxes, and opens one read-only catalog transaction.
   `sync.py:inventory` resolves holdings; `sync.py:run` analyzes decks and writes
   generated notes, version logs, and sync state. The catalog closes before the
   output adapter prints the result. Root `sync.py` uses the `Catalog` protocol
   and does not open Postgres or handle terminal output.
6. **Back up.** `ops/database.py:backup` makes the final database dump for an
   online invocation. This decision uses the original offline option: a network
   fallback or a vault validation failure still reaches backup. An unavailable
   catalog or inventory ends the run before it.

The main write boundaries are:

| Work | Writes |
|---|---|
| Source collection | Retained files and request logs under the XDG data directory; the Scryfall catalog load updates Postgres |
| ManaBox pickup | Retained exports under `manabox/`, then `collection.csv` under the data directory |
| Note generation | The configured vault's `mtg/_generated/` and `mtg/_log/`, with corresponding folders under `hidden_folder` for hidden decks; sync state stays in the data directory |
| Database backup | Verified dumps and hashes under the data directory's `db-backups/` |

A sync commits files as it goes; it is not one transaction across the store,
database, and vault. A later failure leaves earlier retained data in place.
Recorded failures still make the command exit 1, even when later work succeeds.
Warnings alone do not fail the command.

With `--watch`, `cli/sync.py` polls note and input modification times and calls
`_resync` after a change. That follows the same workflow offline: it can pick up
exports and rewrite notes, but makes no downloads or database backup. A failed
resync is reported and the watch keeps running.
