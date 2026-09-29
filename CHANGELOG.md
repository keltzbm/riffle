# Changelog

All notable changes, newest first. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) (0.x: anything may change at a minor bump).
Work in progress goes under **Unreleased** and moves into a version heading at release time.

## [Unreleased]

### Added
- Every bulk file Scryfall publishes is kept, each time it's published (about twice a day), as served:
  `scryfall/bulk/<type>/<published>.jsonl.gz` for default cards, all cards (every language), oracle cards, unique
  artwork, rulings, and Tagger's oracle and art tags, each on a step of its own (`Scryfall rulings: kept the
  2026-09-29 15:00 MDT file, 5.4 MB`). Each download is read whole before it's kept; one that isn't whole gzip is
  set aside as `<name>.bad`, fails its step, and is asked for again next run. A file whose contents, uncompressed,
  are the newest kept file's isn't written again (`the same as the … file; not kept twice`), and
  `scryfall/bulk/checks.jsonl` records every publish, kept or not.
- Scryfall's set list is kept each time it changes, as `scryfall/sets/<fetched>.json`, with every fetch in
  `scryfall/sets/checks.jsonl`.
- An online sync records the disk's free space in `disk.jsonl` and warns on a `disk` step when it's under 50 GB,
  with the days left at the rate it fell over the last week.
- `riffle check` checks every price file Riffle keeps: that it's filed under the day its own stamp says, and that
  it wasn't made after it was fetched (the time in its gzip header, or the file's own). It reads only, prints a line
  per source, names each file that's wrong, and exits 1 if any is. For Card Kingdom and Mana Pool it also says how
  long before its fetch each list was made, which is how Card Kingdom's time zone gets checked.
- Git hooks in `.githooks/`, turned on with `git config core.hooksPath .githooks`: `pre-commit` runs ruff's lint
  and format checks, `pre-push` everything CI runs with the 90% coverage floor, the database tests required.
- `riffle ingest mtgo --max-events N` fetches at most N event pages, newest first; later runs skip what's stored
  and reach further back, so a long backfill spreads over several runs.
- An old MTGO event that comes back empty on three runs, each time right after a good answer, is skipped from then
  on and listed in `~/.local/share/riffle/mtgo-misses.json`; deleting the file retries them.
- Tab completion for deck names and option values. With `riffle` itself on PATH (`uv run riffle` completes uv's
  arguments instead) and `riffle --install-completion zsh` run once, a deck argument completes from the vault's
  deck notes, plus `all` for `legal` and `export`, and `--to`, `--pin`, `--show`, `--board`, `--kind`, and
  `--format` complete from their lists. A path still completes as a file.
- `riffle sync` waits up to two minutes for the network before its first download, since the scheduled job
  runs as the Mac wakes, before Wi-Fi is back. Without a connection by then, it syncs offline: today's
  Scryfall prices from the last download, and the vault from the catalog Postgres already holds.
- The Magic catalog in Postgres. `riffle ingest scryfall` and `riffle sync` load Scryfall's bulk file into
  new tables (migration `0002`): formats, sets, cards, printings, legalities, Magic's own card and printing
  columns, and `external_ids`, the registry mapping Scryfall's, MTGO's, and Arena's IDs to Riffle's. Riffle's
  IDs are derived from Scryfall's (UUIDv5), so every machine gets the same ones. A load writes only rows that
  changed, marks what Scryfall stops listing as retired instead of deleting it, and is skipped when Postgres
  holds the downloaded file already; `--force` loads it anyway. Commands still read the DuckDB catalog, so
  when Postgres is down or its schema is behind, the step says so and the sync carries on.
- Scryfall's set list (parent sets and release dates, which card data lacks) is downloaded after each new bulk
  file, or when missing, into `~/.local/share/riffle/scryfall/sets.json`. A failed download is reported and
  never stops a sync.
- `src/riffle/db/aliases.toml`, for the rare Scryfall ID rename, so a renamed card keeps its Riffle ID.
- License: the GNU Affero General Public License v3.0 or later (`LICENSE`), declared in the package
  metadata too. Card data and images stay under their sources' terms.
- Test coverage: every `uv run pytest` measures line and branch coverage (pytest-cov) and lists the files with
  untested code. CI fails a run below 75%, a floor to raise as coverage grows.
- Mana Pool's price lists, once a day, kept the same way as Card Kingdom's, in
  `~/.local/share/riffle/manapool/daily/<day>/`: `singles` (each printing's market price, nonfoil and foil; its
  cheapest, Near Mint, and Lightly Played or better prices in every finish, etched included; and how many are
  listed), `variants` (the lowest price and how many are listed for each language, condition, and finish), and
  `sealed`. MTGJSON carries only its cheapest price per finish.
- Card Kingdom's whole price list, once a day: for every Magic single it lists, what it sells it for and how many
  it has in each condition (NM, EX, VG, G), foil and etched too, and what it pays and how many it wants; for every
  sealed product, the same without conditions. MTGJSON carries only its Near Mint sell price and its buy price,
  each only while it has copies or wants them, and nothing about the other conditions or quantities.
  `riffle ingest prices` and `riffle sync` keep `/api/v2/pricelist` and `/api/sealed_pricelist` as returned,
  gzipped, in `~/.local/share/riffle/cardkingdom/daily/<day>/`, `<day>` being the Mac's date, since the list's
  `created_at` has no time zone to go by. A list kept today isn't asked for again; each is checked at both ends to
  look like one whole JSON object with a `data` key first.
- Cardmarket's price guides, Europe's prices in euros for every game Cardmarket sells (Magic, Flesh and Blood,
  One Piece, and 17 more) and its accessories: each product's low, average, and trend prices and its 1-, 7-, and
  30-day averages, foils too, sealed products included. `riffle ingest prices` and `riffle sync` keep each game's
  guide once per day Cardmarket makes one, gzipped as returned, in
  `~/.local/share/riffle/cardmarket/daily/<day>/<game>.json.gz`, `<day>` being Cardmarket's own date for it. A
  guide under 20 hours old isn't asked for again. Each game fetched is a step of its own; Magic, Flesh and Blood,
  and One Piece fail when Cardmarket has no guide for them, and the rest are skipped then. If Cardmarket doesn't
  answer at all, the games after it fail at once instead of each waiting out its own retries.
- GoatBots' MTGO prices, from a large MTGO bot chain beside Cardhoarder (whose prices Scryfall and MTGJSON carry):
  `riffle ingest prices` and `riffle sync` keep its average sell price in tix for every MTGO card it trades, once
  per day it publishes, in `~/.local/share/riffle/goatbots/daily/<day>.zip`, and its card definitions (name, set,
  rarity, and foil for each MTGO ID it prices) in `goatbots/card-definitions.zip`, fetched again while they're
  older than the newest day. The first run also keeps its yearly archives of every day, from this year back to the
  first year it has none for (marked `<year>.none`); GoatBots keeps only the last few years, so what it still has
  is kept now. This year's archive is kept as it stands (`<year>-partial.zip`) and again, whole, once the year is
  over. Zips are kept as returned, after checking they hold what they should; one that doesn't says what it held.
- MTGJSON prices for Magic, several stores in one file: Card Kingdom's retail and buylist prices, TCGplayer,
  Mana Pool, Cardmarket (EUR), and Cardhoarder (MTGO tix). `riffle ingest prices` and `riffle sync` keep MTGJSON's
  `AllPricesToday` once per MTGJSON build, in `~/.local/share/riffle/mtgjson/daily/<day>.json.xz`, and on the first
  run its `AllPrices`, the past 90 days, in `mtgjson/90-days/`, so the history starts three months back. A day
  missed later (the Mac was off) comes back with a new 90-day file, kept when a day is missing, at most every 30
  days. Files are kept as returned, named by the date inside, once each matches MTGJSON's `.sha256` of it and
  decompresses whole. Cardhoarder's prices are tix, though MTGJSON labels them "USD". `Meta.json` says whether there's a new day, so a rerun costs one request.
- Daily price snapshots, Riffle's own price history: `riffle ingest prices` (and `riffle sync`) fetch every set's
  TCGplayer price file from tcgcsv.com for everything it carries except comics, 92 of its 94 categories (card
  games, miniatures, board games, and supplies), one file at a time and once per day, into
  `~/.local/share/riffle/tcgcsv/daily/<day>/<game>/`, and keep each Magic printing's Scryfall prices from the
  bulk file already downloaded, in `scryfall/daily/<day>.jsonl.gz`. Files are stored as the sources returned
  them. Magic, Flesh and Blood, and One Piece are looked up on tcgcsv by name; every other category is named by
  its slug (`yugioh`, `warhammer-box-sets`), and each game is a progress step of its own. A run stays under 9,000
  requests, tcgcsv's limit being 10,000 a day.
- CI on GitHub Actions for every push to `main` and every pull request: `ruff check`, `ruff format --check`,
  and the test suite on Linux and macOS with Python 3.12, 3.13, and 3.14 (the versions `requires-python`
  allows).
- `mypy src` runs in CI; the code type-checks clean.
- Postgres: `riffle db up` starts a local Postgres 18 with Docker Compose (`compose.yaml`) and waits until
  it's healthy; `riffle db upgrade` applies migrations; `riffle db status` shows the server and schema
  revision and exits 1 if the database is unreachable or behind. Migrations (Alembic) ship inside the
  package. The first one creates `games` with Magic, Flesh and Blood, and One Piece.
- `database_url` config key, default `postgresql+psycopg://tcg@localhost:5432/tcg`. The password comes
  from `~/.pgpass`, never from config; `riffle init` shows the URL.
- Database tests against a separate `tcg_test` database, recreated each run, each test rolled back. They
  skip when no Postgres is reachable; CI's Linux jobs run them against a `postgres:18` service.
- Dependencies: SQLAlchemy, psycopg (with its bundled libpq), Alembic.
- A dev container for GitHub Codespaces (`.devcontainer/`): Docker, uv, the GitHub CLI, and an SSH server
  for `gh codespace ssh`. Creating a codespace installs the project and a database password; every start
  brings Postgres up and migrates it, so the database tests run there instead of skipping.

### Removed
- **Breaking:** `riffle ingest mtgo` (`--days`, `--delay`, `--max-events`) and giving up on MTGO events. A run
  that asked for pages as fast as a second apart got them stripped, and nothing an index listed was kept once a
  run ended. The trickle below replaces both; `mtgo-misses.json` moves into its owed list on the first run.
- **Breaking:** the DuckDB card catalog (`~/.local/share/riffle/mtg.duckdb`) and its loader, and DuckDB as a
  dependency. Postgres holds the only catalog; delete the old file by hand.
- **Breaking:** `riffle ingest tcgcsv` and the daily price-archive download. tcgcsv.com took the archive down
  (September 2026) and asks clients to fetch price files individually instead; the snapshots above replace it.
  `riffle sync --offline` still keeps the Scryfall prices, which need no request.

### Changed
- Riffle writes UTC and prints local time. The job logs (`sync.log`, `mtgo-trickle.log`) give each run's date and
  time in UTC, and their step times are UTC; they were the Mac's time. On a terminal, `riffle mtgo trickle` and
  `riffle mtgo status` show a pause in the Mac's time with its zone, `riffle schedule` says the zone its times are
  in, and `riffle watch` shows the zone. The generated notes and version logs are dated by the UTC date.
- A step can end in a warning: a yellow `!` on the terminal, a `warning:` line in the log. It doesn't make the
  command exit 1.
- A Cardmarket game not played that has no guide is noted on its step and asked for again every run, instead of
  being skipped without a word; after seven runs in a row it's a warning. Every game's guide is kept, played or not.
- CI's coverage floor goes from 75% to 90%, held by the Linux jobs, which run every test against Postgres; the
  suite is at 94%. The macOS jobs skip the database tests, so they run without a floor.
- MTGO decklists come in as a trickle: a launchd job (`riffle schedule trickle`, beside the daily sync job) runs
  `riffle mtgo trickle` every 10 minutes, all day. Each run asks for 1 to 3 pages, 5 seconds apart, and never more
  than 5 in any 15 minutes; about 430 pages a day at full pace. It reads this month's index hourly and sweeps back
  through every earlier month's for every format, and every event an index lists that isn't stored goes on an
  owed list (`mtgo-owed.json`), tried on every run while under 30 days old, then weekly, and never dropped;
  `riffle mtgo forget <slug>` drops one by hand. An empty answer is believed only after a stored event, asked for
  again, comes back whole; stripped too, or a 429, means throttled: nothing counts against the event, the job
  pauses 3 hours (6, then 12, if it happens again within a day) and comes back a page slower, then speeds up a
  page after 144 whole answers in a row. A 404 or a redirect is an ordinary miss. Every request is logged
  (`mtgo-requests.jsonl`: UTC time, URL, status, size, time taken, how it was read), and each fetched event keeps
  the page's whole data object, gzipped, in `mtgo/raw/`. `riffle mtgo status` shows the pace, any pause, recent
  requests, what's owed by month, and how far back the indexes have been read. Lists that won't parse exit 1;
  a throttle doesn't.
- `riffle ingest mtgo` tells mtgo.com's throttling apart from missing data. Throttled, the site answers with
  stripped pages instead of errors, which read as empty months and unpublished lists, so a long backfill silently
  lost whole months. Now an index listing no events is a failure unless its month began under two days ago, and an
  empty event page is "not published yet" only for an event under three days old; older, it's reported as empty,
  fails its step, and is retried next run. Three bad answers in a row pause the run for 30 s, then 60 s and 120 s
  after later streaks, and a bad streak after that stops it with exit 1. Months and events go newest first, event
  pages get 60 s to answer instead of 20, and the summary prints even when a step failed.
- `riffle decks` sizes each column to its longest value, so a long slug no longer pushes its row out of line.
  Widths count terminal cells, so accented and wide characters line up too, and a format or status left empty
  or written as a list no longer stops the command.
- **Breaking:** a failed step no longer stops `sync`, `ingest scryfall`, `ingest prices`, or `ingest mtgo`:
  the command finishes what it can, then names the steps that failed and exits 1. A failed Scryfall download
  is reported instead of ending the sync with a traceback, and the sync carries on with the last download.
  The scheduled job's last exit (`riffle schedule`) now shows whether anything went wrong.
- `riffle watch` reports a failed resync and keeps watching.
- **Breaking:** `own`, `price`, `legal`, `export`, and `sync` read cards from Postgres, which must be running,
  migrated, and loaded (`riffle db up`, `riffle db upgrade`, `riffle ingest scryfall`). Each command reads one
  read-only snapshot; when the catalog can't be read, it says what to run and exits 1. Names load once; a
  collection's or deck's printings, prices, and rules load in one query each, through indexes. Cards are keyed
  by Riffle's `card_id` instead of Scryfall's oracle ID. Cards and printings Scryfall stopped listing are left
  out. Where cards share a name, a real card beats a token, emblem, or Art Series card, then the one with
  more printings wins. `riffle sync --offline` gives the same notes as before, apart from the fix below.
- The Postgres load's step is now "card catalog", its progress total the printing count of the last load.
  A failed load keeps the last one; commands carry on with it.
- `riffle ingest scryfall` records when the bulk file was downloaded (`downloaded_at` in `bulk-meta.json`);
  the first run after this change downloads the file again.
- `riffle ingest scryfall --force` reloads the Postgres catalog as well as downloading.
- Tests run with their own config and data folders and a database URL nothing listens on, so no test can
  touch real files or the real database.
- Long-running commands (`sync`, `ingest scryfall`, `ingest prices`, `ingest mtgo`) show their steps as they
  happen. On a terminal, a running step has a spinner; a purple bar of braille dots that fills one dot at a
  time, glides toward its count, and has a spinner sweeping through it, with its percentage; its count (or bytes
  and speed); and the time so far, then about how long is left, or how long it has been waiting once it goes 10
  seconds without progress. A step with no total shows a braille snake crawling along the track. A finished step
  becomes a line with a green ✔ (red ✘ if it failed), its result, and how long it took. Anywhere else, as in the
  scheduled job's `sync.log`, nothing animates: a dated line starts the run and a timestamped line ends each step.
- `riffle ingest mtgo` reads every month's index before fetching events, so each format's progress has a
  total, and an event linked from two months is fetched once. One line per format replaces one per event.
- Rich, already installed with Typer, is a declared dependency.
- CI ends with one job, `CI passed`, that succeeds only when every other job did. `main` requires it, so a
  pull request set to auto-merge merges itself once CI is green, and nothing reaches `main` without it.
- CI's test job is split in two: Linux with a Postgres service, macOS without (its runners have no Docker).
- `.env`, which holds the local database password for Compose, is ignored by git.
- **Breaking:** the project is now Riffle (github.com/keltzbm/riffle). The package and command are
  `riffle` instead of `mtg`, config lives in `~/.config/riffle/`, data in `~/.local/share/riffle/`, and the
  launchd job's label is `com.keltzbm.riffle-sync`. Nothing is migrated automatically: move the old
  config and data directories, remove the old job, and set the schedule again. Behavior is otherwise
  unchanged; generated notes name `riffle sync`, and requests identify as `riffle/<version>`.
- `tests/test_sync.py` formatted with `ruff format`; the whole repo now passes the format check.
- Development Python pinned to 3.14 in `.python-version`: `uv run` and the CI lint job use it; the test
  matrix still covers 3.12 through 3.14.
- Type fixes found by mypy: code that uses a card's oracle id now receives it as a definite string instead
  of re-reading an optional field, and `riffle sync` no longer reuses one variable for the price archive and
  the sync result.

### Fixed
- Scryfall's second bulk file of a day is downloaded. Scryfall publishes about twice a day (09:05 and 21:05 UTC on
  2026-09-28), and a refresh skipped any file for a day it already had; the step is now `Scryfall default cards`,
  current only when that very file is kept. Each download no longer deletes the file before it, or one set aside
  as unreadable: the file kept before, `default-cards.jsonl.gz`, moves into `scryfall/bulk/default_cards/` on the
  first refresh, named by its publish time from `bulk-meta.json`.
- tcgcsv keeps each set's price file as it arrives. One set that failed voided its whole game, and the next run
  asked for every set again, against tcgcsv's rule of never fetching a file twice; now the game's step fails
  naming how many it lacks (`451 of 452 sets kept; set 23 failed (HTTP 500), asked again next run`), and the next
  run of the same tcgcsv day asks only for those. tcgcsv publishes about 20:05 UTC, so its day spans both daily
  syncs. A game with every set is gzipped, sorted by set; one tcgcsv's day passed before it was whole is gzipped
  as it stood, with `missing.txt` naming the sets it never got. Each line gains the set's fetch time and its
  file's `Last-Modified` (UTC). A set whose file is from tcgcsv's next refresh goes under that day, and the run
  carries on under it, warning what the old day lacks; one with no `Last-Modified` goes under the run's day, and
  the step says so. Once tcgcsv gives no answer, the rest of its games fail at once (`not asked: tcgcsv gave no
  answer`) instead of each waiting out its retries, up to about 4¾ hours. A game that fails any other way, a
  disk error included, no longer skips the games after it.
- GoatBots' whole archive for a year that's over is kept as the year's only if it runs to Dec 31 and holds every
  day of the partial archive kept during the year. One short of that says what it lacks and is asked for again
  next run, a warning after seven runs in a row; it's kept too, as `<year>-short-<UTC time>.zip`, when it has a
  day no archive of the year kept has, so nothing is lost if the whole one never comes. The partial archive is
  kept beside the whole one, not deleted. A year GoatBots had no archive for is asked for again a week after it
  said so, not never; `<year>.none` holds the day.
- `riffle check` also names tcgcsv sets kept under a day from a later refresh, notes the games not finished, and
  names GoatBots whole-year archives short of Dec 31.
- The MTGO trickle waits up to 60 seconds for a month's index, as it does for an event page, not 20. An old
  month's index runs to about 300 KB and took up to 15 seconds to come back; 16 of the first 58 reads ran past
  20, and each one ended its run with nothing else asked.
- An MTGO event that keeps missing no longer holds up the trickle. Owed events went newest first, and one under
  30 days old was asked again on every run, so a new event that redirected took one of each run's two pages; with
  the index sweep taking the other, the trickle stored nothing. Events never asked for now come first, newest
  first, and retries get only the pages left over. A retry waits an hour after its try, doubling with each try up
  to a day, and a week once it's 30 days old; nothing is dropped. `riffle mtgo status` counts the events never
  asked for and those to retry, and lists the newest 10 retries with what each last came to, how many times it
  was asked, and when it's next due.
- Card Kingdom's and Mana Pool's lists are dated by their own stamps, Card Kingdom's `created_at` (read as Pacific
  time) and Mana Pool's `as_of` (UTC), not by the Mac's date. A run after midnight kept the day before's list under
  the new day, and the new day's own list was never asked for. A list for a day already kept isn't kept again, and
  one Riffle filed under the wrong day before is moved to its own day, or set aside in `<store>/aside/` when that
  day has its list. A list with no readable stamp is set aside there too, never under a day, and its step fails.
  Each list's step says when the store made it.
- The price log is dated by the day of the Scryfall prices the catalog holds, not the Mac's date. An offline
  `riffle watch`, `ingest manabox` or `sync --offline` after midnight logged the old prices under the new day, and
  that day's real sync then logged nothing. The sync's summary says the day.
- An empty answer is no longer kept as a day: a Card Kingdom or Mana Pool list with no rows, a Cardmarket guide
  with none, a GoatBots price file of `{}`. Empty GoatBots card definitions no longer replace the kept ones. Each is
  noted in `empty-answers.json` and asked for again every run; after seven empty runs in a row the sync warns,
  "empty since <day>", without failing.
- Cardmarket's download server answers 403, not 404, for a guide it doesn't have; that now counts as no guide, so
  a game Cardmarket stops publishing no longer fails every sync.
- Two runs no longer overlap. `riffle sync`, `riffle ingest prices`, `riffle ingest scryfall`, and the resyncs of
  `riffle watch` and `riffle ingest manabox` take turns: a second run waits for the first, saying which run it's
  waiting for and since when. They shared download names, the price log and `sync-state.json`, so one run could
  delete the other's download or leave a price list mixed from two. The ManaBox copy and `sync-state.json` are now
  written whole or not at all.
- One bad note no longer stops the vault half of every sync. A note that isn't UTF-8 is skipped and named on a
  failed step, and its deck's generated note is left as it was; an unreadable `sync-state.json` is set aside and
  each deck's version log starts again from a baseline; `riffle watch` keeps watching after any error, not only a
  failed step. A vault path with no `tcg/mtg` folder fails the sync saying where to set it, instead of building an
  empty vault there and reporting success.
- One price source's trouble no longer ends the sync or sends it offline. The network wait goes on as soon as any
  source answers, so Scryfall alone being unreachable fails only Scryfall's steps. An error no source expects
  fails only that source's steps, with the traceback in `~/.local/share/riffle/errors.log`, and the rest still run.
  These used to end the sync: a tcgcsv group list without whole-number IDs or a tcgcsv file that isn't UTF-8 (the
  game's step fails), a negative or non-numeric Retry-After (the usual backoff is used), a `database_url` that
  can't be used (the catalog step fails naming the config file, and the prices are still kept), a Scryfall bulk
  file cut short (set aside, and downloaded again by the next online sync), and an unreadable `bulk-meta.json`
  (counted as missing, so the bulk file is downloaded again).
- Lines a command prints while its steps run, such as `riffle ingest mtgo`'s report, the tcgcsv request count,
  and `riffle sync`'s summary and warnings, print above the progress display instead of onto its last line.
  typer.echo wrote to the terminal underneath the display rather than through it, and the line printed for a
  finished step brought back that step's last bar, so a stale progress line was left behind with the next line
  joined to it. Clearing the display when a command ends (below) wasn't the cause.
- `riffle sync` and `riffle ingest scryfall` download Scryfall's bulk file whenever Scryfall has published one for
  a newer day than the one kept. They used to skip a download under 24 hours old, and a job run at the same time
  each day starts a few seconds short of that, so every other day's file was skipped, and with it that day's
  Scryfall prices, which can't be fetched again, and that day's catalog load. Each run now asks Scryfall's bulk
  index, one request, what it last published.
- A category tcgcsv lists but has no set list for (its groups answer 404, as My Little Pony's does) is skipped
  and noted on its step instead of failing every sync; Magic, Flesh and Blood, and One Piece still fail. The live
  progress display also clears itself when a command ends, so a step that failed last can't leave its spinner line
  behind.
- An answer cut off partway is a failed step, retried the next run, like any other failure. A tcgcsv answer cut
  off partway, chunked or short of its Content-Length, raised an error that stopped `riffle sync` and
  `riffle ingest prices` outright, skipping the rest of tcgcsv's games and everything after them; a timeout or
  dropped connection while reading one stopped the rest of tcgcsv's games; and a Scryfall bulk file that ended
  short of its Content-Length was kept as if whole. Any connection error before an answer, and a garbled status
  line, is retried like a dropped connection.
- Art Series cards ("Sol Ring // Sol Ring", about 2,200 of them) no longer count as the real card. The DuckDB
  catalog folded them onto it along with Secret Lair reversibles, so an art card in a collection counted as
  owning the card, its price could become the card's cheapest, and, as the newest printing, it replaced the
  card's color identity (then empty) and type line: a Commander deck could pass the color-identity check with
  an off-color card. About 5,500 cards were affected.

## [0.3.0] — 2026-09-23

### Added
- `mtg ingest tcgcsv`: daily TCGplayer price archives from tcgcsv.com, every game in one file per day,
  stored as downloaded under `~/.local/share/mtg/tcgcsv/archive/`. `--since 2024-02-08` backfills the whole
  archive; already-stored days are skipped, unpublished days are retried next run. `mtg sync` fetches the
  last few days, so the scheduled job keeps the archive current.
- `mtg schedule` is now a command group: `mtg schedule` / `show` (times, next run, loaded, runs,
  last exit, log), `set 07:00 19:30` (several daily 24-hour times, validated), `remove`.
- `CHANGELOG.md`.
- Tests: scheduling (fake launchctl), DuckDB catalog against an in-memory table, decklist / vault /
  file-ingest edge cases, and many more legality and MTGO cases — table-driven with
  `pytest.mark.parametrize`.

### Changed
- **Breaking:** `mtg schedule --at HH:MM` → `mtg schedule set HH:MM`; `--remove` → `remove`.
- The launchd plist is written with `plistlib` instead of a string template.
- MTGO ingest paces the monthly index requests as well as event pages (long backfills).
- `ruff check src tests` is clean: Typer's `Option`/`Argument` defaults are allowed in config, ambiguous
  single-letter names renamed, long test strings split. No behavior change.
- Resolving a collection loads every printing in one query and looks rows up in memory, instead of one
  query per ManaBox row (twice per row in the collection summary). `mtg sync --offline` went from 6.0 s to
  0.24 s on a three-deck vault.
- Owned counts are computed once per sync instead of once per deck.
- Clearer code in a few places: the deck-price builder, decklist set codes, and pairs that must line up
  one-to-one now fail loudly (`zip(strict=True)`) instead of silently truncating.
- One HTTP layer (`net.py`) for Scryfall, mtgo.com, and tcgcsv: the same User-Agent everywhere, retries on
  stalls and server errors, and a wait on 429 — for `Retry-After` when sent — as Scryfall requires. Client
  errors like 403 fail at once instead of being retried.
- Downloads stream to disk with a live size on a terminal (tcgcsv archives, the Scryfall bulk file); the
  scheduled job's log gets plain lines. A bad Scryfall download no longer replaces the last good file.

### Removed
- `analysis/mana.py`: stubs that were never implemented. Mana analysis is listed as not started in DESIGN.
- **Breaking:** sealed precon lists (`precons/`) and the `precons` config key. A precon counts as owned
  only once it's scanned into ManaBox, so nothing is ever counted twice. `mtg init` flags a leftover
  `precons` line; it's otherwise ignored.

### Fixed
- Maybeboard / Considering / Tokens sections in a decklist were counted as deck cards, so `mtg own`
  listed cards you were only considering. They're skipped now.
- Zero-quantity lines are skipped instead of added as entries.
- A byte-order mark at the start of a Windows-made list no longer corrupts the first card name.
- A heading like "Important notes" no longer passes for the "Moxfield import" heading.
- Arena CSVs with blank or zero counts no longer error or add empty holdings.
- Invalid schedule times (`7pm`, `25:00`) are rejected instead of crashing or writing a broken job.

## [0.2.0] — 2026-09-21

The file-based rewrite.

### Added
- Scryfall bulk ingest (JSON Lines) into DuckDB, keyed by `oracle_id`.
- ManaBox collection ingest (newest export picked up from ~/Downloads), sealed precons, Arena lists.
- `mtg own`, `price` (paper + MTGO tix), `export` (Moxfield, ManaBox, MTGO, Arena, TCGplayer;
  owned printings pinned), `sync`, `watch`, `schedule`.
- Obsidian output: `_generated/<deck>-data.md`, `_log/` prices and list versions.
- `mtg legal`: format legality, copy limits, deck size, sideboard, commander color identity;
  playset eligibility.
- MTGO decklists: `mtg ingest mtgo` (leagues, challenges, showcases; `-f all`), `mtg meta cards /
  decks / show`, deck fingerprints.

### Fixed
- `mtg schedule` wrote the launchd plist but never loaded it.
- Legality came from a card's newest printing, so non-tournament printings (e.g. gold-border
  World Championship decks) made Sylvan Library, Vampiric Tutor, and Blast Zone read "not legal".
  Now merged across all printings.
- MTGO events published before their lists were stored empty and never retried.
- Secret Lair reversible printings counted as separate cards.

## [0.1.0] — 2022

Original scripts: Firestore, Selenium scraping, dict-backed classes. Preserved as a tag.
