# Riffle

[![CI](https://github.com/keltzbm/riffle/actions/workflows/ci.yml/badge.svg)](https://github.com/keltzbm/riffle/actions/workflows/ci.yml)

What I own, what I'm building, what the gap costs — in paper and on MTGO —
rendered into my Obsidian vault. Design: [DESIGN.md](DESIGN.md).

## Install

```bash
cd ~/atelier/github/riffle
uv sync                      # creates .venv, installs the package + dev tools
source .venv/bin/activate    # or prefix commands with `uv run`
riffle init
```

## First run

Postgres holds the card catalog, so set it up first ([Database](#database-postgres)).

```bash
riffle db up && riffle db upgrade
riffle ingest scryfall                          # every Scryfall bulk file, ~550 MB each time it publishes
riffle ingest manabox ~/Downloads/collection.csv
riffle sync --offline
```

## Everyday

```bash
riffle sync                        # picks up a new ManaBox export from ~/Downloads, refreshes
                                   # prices, rewrites _generated/, appends _log/
riffle decks                       # deck notes the vault holds
riffle own aesi-lands              # 🟥 what to buy (default); numbers are copies in the deck
riffle own aesi-lands -a           # 🟥 buy, then 🟩 own
riffle own aesi-lands -s own       # just what you have
riffle own aesi-lands --arena      # against your Arena collection, with wildcard counts
riffle price yshtola-spellslinger --budget-tix 500
riffle export aesi-lands --to moxfield -o ~/Downloads/aesi.txt   # owned printings pinned
riffle export all --to manabox -o ~/Downloads/mtg-exports        # every deck, one file each
riffle export izzet-murktide --to mtgo
riffle export aesi-lands --to tcgplayer        # mass-entry list of the shortfall
riffle ingest arena ~/Downloads/mtga_collection.txt              # text list or CSV
riffle legal aesi-lands            # size, copies, bans, commander color identity
riffle legal all                   # every deck; exits 1 if any is illegal
riffle legal my-deck -f modern     # check against a different format
```

## Tab completion

The shell completes by running `riffle` itself, so it has to be on PATH; `uv run
riffle` completes uv's own arguments instead. Once per machine:

```zsh
ln -s ~/atelier/github/riffle/.venv/bin/riffle ~/.local/bin/riffle
riffle --install-completion zsh
exec zsh
```

A deck completes from the vault's deck notes, the ones `riffle decks` lists,
with `all` for `legal` and `export`; anything else, like a path, completes as a
file. `--to`, `--pin`, `--show`, `--board`, `--kind`, and `--format` complete
from their lists.

## Metagame (MTGO)

```bash
riffle schedule trickle                          # fetch mtgo.com's decklists a few pages at a time, all day
riffle mtgo status                               # pace, pauses, recent requests, what's still owed
riffle meta cards -f modern --days 14           # most-played cards: share, avg copies, main/side
riffle meta decks -f modern --card "Psychic Frog"
riffle meta show <event-slug> <player> -o ~/Downloads/list.txt
riffle own ~/Downloads/list.txt                  # what that list costs you
```

mtgo.com builds a page when it's first asked for, and a build that takes more
than about 30 seconds answers with a page without lists, or a redirect, though
the page is ready for the next request. The trickle job runs `riffle mtgo
trickle` every 10 minutes, never more than 5 requests in any 15 minutes: one
page a run to start, one more after each run whose every answer was whole. It
reads this month's index hourly and sweeps back through every earlier month,
and keeps every event an index lists on an owed list until it's stored. A page
that fails is asked again at each of the next two runs, first in line; after
three failures it waits an hour, then waits that double up to a day, and weekly
once it's a month old. Events never asked for come next (newest and oldest in
turn while the sweep goes on), each once it's as old as the youngest of its kind
that ever came back whole; retries after that get the pages left over. The job
pauses only when mtgo.com asks it to (a 429, a 403 or a `Retry-After`). Nothing
is given up; `riffle mtgo forget <slug>` drops an event by hand.

Events are stored once each under `~/.local/share/riffle/mtgo/<year>/<month>/`,
with each page's whole data object gzipped in `mtgo/raw/`. Every request is
logged in `~/.local/share/riffle/mtgo-requests.jsonl`.

A deck is named by its note's slug, or by a path to any `.md` or `.txt` list.

## Price history (every game)

```bash
riffle ingest prices                        # today's prices: tcgcsv and Cardmarket for every game; Scryfall,
                                            # MTGJSON, Card Kingdom, and Mana Pool for Magic; GoatBots for MTGO
```

Riffle keeps its own price history, one snapshot a day, stored as the sources
returned it under `~/.local/share/riffle/`:

- `tcgcsv/daily/<day>/<game>/` — every set's TCGplayer price file from
  tcgcsv.com for everything it carries except comics (card games, miniatures,
  board games, and supplies), fetched one file at a time, once per day (tcgcsv's
  own rule since it took its bulk archive down). Each set is kept as it
  arrives, so a set that fails is the only one the next run asks for; tcgcsv's
  day (it publishes at about 20:05 UTC) spans both daily syncs. A game left
  unfinished when tcgcsv moves on is kept as it stood, with `missing.txt`
  naming the sets it never got. Each set goes under the day its own
  `Last-Modified` says. If tcgcsv stops answering, the rest of its games wait
  for the next run.
- `scryfall/bulk/<type>/<published>.jsonl.gz` — every bulk file Scryfall
  publishes, about twice a day, as served: default cards (each card in English,
  or the one language it was printed in), all cards (every language), oracle
  cards, unique artwork, rulings, and Tagger's oracle and art tags, named by
  Scryfall's publish time in UTC. A file whose contents are the same as the one
  kept before it isn't written again; `scryfall/bulk/checks.jsonl` records every
  publish either way. The catalog is loaded from the newest default-cards file.
- `scryfall/sets/<fetched>.json` — Scryfall's set list each time it changes,
  named by fetch time (Scryfall gives it none); `scryfall/sets.json` is the latest.
- `scryfall/daily/<day>.jsonl.gz` — each Magic printing's prices (USD, EUR,
  MTGO tix) from the day's first default-cards file; the second's are in its
  kept copy.
- `mtgjson/daily/<day>.json.xz` — MTGJSON's prices for every Magic printing,
  several stores in one file: Card Kingdom (what it sells for and what it pays),
  TCGplayer, Mana Pool, Cardmarket (EUR), and Cardhoarder (MTGO tix). Each file
  is checked against MTGJSON's checksum of it before it's kept.
- `mtgjson/90-days/<day>.json.xz` — the same for `<day>` and the 90 days before
  it. The first sync keeps one, so the history starts three months back;
  a day missed after that (the Mac was off) comes back with the next one, kept
  when a day is missing, at most every 30 days.
- `goatbots/daily/<day>.zip` — GoatBots' average sell price in tix for every
  MTGO card it trades; GoatBots is a large MTGO bot chain beside Cardhoarder,
  whose prices Scryfall and MTGJSON carry. `goatbots/card-definitions.zip`
  names each MTGO ID it prices: name, set, rarity, and foil.
- `goatbots/yearly/<year>.zip` — GoatBots' archive of every day in a year, or
  `<year>-partial.zip` for this year, as it stood when first kept. GoatBots
  keeps only the last few years, so the first sync keeps every one it still
  has, and this year's again, whole, once the year is over: only once it runs
  to Dec 31 and holds every day of the partial, which stays beside it. One
  that comes short is asked for again, and kept as `<year>-short-<time>.zip`
  when it has days nothing else kept.
  `<year>.none` marks the year before the oldest it has, with the day GoatBots
  said so; it's asked for again a week later.
- `cardmarket/daily/<day>/<game>.json.gz` — Cardmarket's price guide for every
  game it sells, and its accessories, in euros: each product's low, average, and
  trend prices, and its averages over 1, 7, and 30 days, foils too. A guide under
  20 hours old isn't asked for again, so a rerun costs nothing. `<day>` is the
  guide's own `createdAt` date.
- `cardkingdom/daily/<day>/singles.json.gz` and `sealed.json.gz` — Card
  Kingdom's whole price list, as it stood that day: for every single, what it
  sells it for and how many it has in each condition (NM, EX, VG, G), foil and
  etched too, and what it pays and how many it wants; for every sealed product,
  the same without conditions. `<day>` is the date in the list's own
  `created_at`, which names no zone; Riffle reads it as Pacific time, where Card
  Kingdom is. A day's list is kept once, and not asked for while Card Kingdom's
  today is kept.
- `manapool/daily/<day>/singles.json.gz`, `variants.json.gz`, and
  `sealed.json.gz` — Mana Pool's price lists, the same way: each printing's
  market price (nonfoil and foil) and its cheapest, Near Mint, and Lightly
  Played or better prices in every finish; the lowest price and how many are
  listed for each language, condition, and finish; and every sealed product.
  `<day>` is the UTC date in the list's own `as_of`.
- `cardkingdom/aside/` and `manapool/aside/` — lists that aren't any day's: one
  with no readable stamp, or a second copy of a day already kept. Nothing is
  deleted.

A day is always the source's own, never the Mac's. An answer with nothing in it
(an empty list or guide, or a Cardmarket game with no guide) keeps nothing and
is asked for again every run; `empty-answers.json` notes since when, and after
seven runs in a row the sync warns.

```bash
riffle check                                # every kept file under its own day, made before it was fetched;
                                            # GoatBots' whole years to Dec 31; tcgcsv's unfinished games
```

`riffle sync` does this on its own, so the scheduled job builds the history
day by day. Loading it into a database comes later.

## Keeping the vault current

Every ingest resyncs the vault afterwards (`--no-sync` to skip). Beyond that:

```bash
riffle sync --watch                # resync on every deck-note save or new ManaBox export
riffle watch manapool              # keep any new Mana Pool list (or cardkingdom, cardmarket, tcgcsv, mtgjson, goatbots)
riffle schedule watch              # that, every 5 minutes, a job per store (all but Card Kingdom and Mana Pool asked
                                   # when due); --remove to stop
riffle schedule set 07:00 19:30    # launchd job at these 24-hour times; replaces any old schedule
riffle schedule                    # times, next run, last result, log path
riffle schedule remove
riffle schedule trickle            # the MTGO trickle, every 10 minutes; --remove to stop it
```

The scheduled job runs as the Mac wakes, so `riffle sync` first waits up to two
minutes for the network, then syncs offline if it's still down. No step stops a
sync: each failure is reported, the rest carries on, and the command exits 1
naming what failed. `riffle schedule` shows the last exit; `sync.log` says why.
The log's times are UTC; what Riffle prints to the terminal is the Mac's time,
with the zone. The notes it writes are dated in UTC, and the price log by the
Scryfall day of the prices it logs.

## Database (Postgres)

From v0.4.0 the data moves into Postgres, run locally in Docker (OrbStack on the
Mac). One-time setup from the repo root: a random password for Compose in `.env`
(gitignored) and for every libpq client in `~/.pgpass`, so no config holds it.

```zsh
pw=$(openssl rand -hex 24)
print -r -- "POSTGRES_PASSWORD=$pw" > .env
print -r -- "localhost:5432:*:tcg:$pw" >> ~/.pgpass
chmod 600 ~/.pgpass
unset pw
```

```bash
riffle db up                    # start Postgres (compose.yaml) and wait until it's healthy
riffle db upgrade               # apply pending migrations
riffle db status                # server and schema revision; exits 1 if unreachable or behind
```

`riffle ingest scryfall` and `riffle sync` load the Scryfall download into the
card catalog in Postgres (the "card catalog" step): about 40 seconds the first
time, a skip while the database holds that file already, and about 20 seconds
for each new daily file, which mostly moves `last_seen` on the ID registry. A
failed load keeps the last one, and commands carry on with it.

Every command that needs cards (`own`, `price`, `legal`, `export`, `sync`) reads
them from Postgres in one read-only snapshot. When Postgres is down, its schema
is behind, or it holds no cards yet, the command says what to run and exits 1.

```bash
riffle ingest scryfall --force --no-sync    # reload the catalog even if Postgres holds the file
```

Every card, printing, and set gets Riffle's own ID, a UUIDv5 derived from its
Scryfall ID, so a rebuild from the same files gives the same IDs anywhere.
Scryfall rarely renames an ID; `src/riffle/db/aliases.toml` maps a renamed one
back to the ID it started from.

Tests marked `postgres` use a separate `tcg_test` database, recreated on every
run; without a reachable server they skip. Every test runs with its own config
and data folders and an unreachable database URL, so none can touch real data.

Every `uv run pytest` also measures line and branch coverage and lists the files
with untested code. CI's Linux jobs, which run every test against Postgres, fail
below 90%; `--no-cov` skips the measurement.

Git hooks run the same checks before your commits and pushes. Turn them on once
per clone:

```bash
git config core.hooksPath .githooks
```

`pre-commit` runs ruff's lint and format checks; `pre-push` runs everything CI
does, with the 90% floor, and needs Postgres up (`riffle db up`).

## GitHub Codespaces

For working away from home without a local database: `.devcontainer/` sets up a
codespace with Docker, uv, and SSH. Creating one installs the project and a
database password; every start brings Postgres up and migrates it, so the
database tests run there too. The data is throwaway, like any codespace.

```bash
gh auth refresh -h github.com -s codespace          # once: let gh manage codespaces
gh codespace create -R keltzbm/riffle -b main -m basicLinux32gb
gh codespace ssh                                    # the repo is /workspaces/riffle
gh codespace stop                                   # stops by itself when idle, too
```

## Where things live

| What | Where |
|---|---|
| Config | `~/.config/riffle/config.toml` — the vault, the notes folder in it (`notes`, default `games/tcg`), a folder per game; decks are read from its `mtg/` only, and without one a sync has no decks |
| Card data, collection, sync state, MTGO events, daily prices | `~/.local/share/riffle/` — outside the synced vault |
| Database | Postgres 18 in Docker (`compose.yaml`, volume `tcg_pgdata`); `database_url` in config, password in `~/.pgpass` |
| Output | `mtg/_generated/` and `mtg/_log/` in the notes folder — nothing else |

## Invariants

- Cards are keyed by `card_id`, Riffle's own ID, derived from Scryfall's. Names are resolved once, at the
  edge; unmatched names are reported, never dropped.
- Colors are WUBRG order everywhere.
- The vault is a render target. Authored notes are read, never written.

## License

Copyright (C) 2022–2026 Brandon M. Keltz

Riffle's code is free software under the [GNU Affero General Public License v3.0](LICENSE) or any
later version. Anyone may use, study, change, and share it. Whoever shares a changed version, or runs
one as a service that other people use over a network, must offer those people its source under the
same license.

Card data, prices, and images come from their publishers and from the sources Riffle reads, under
those sources' own terms; this license doesn't cover them.
