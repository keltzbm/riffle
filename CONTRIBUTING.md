# Contributing to Riffle

Riffle is a personal project by Brandon M. Keltz. Anyone may report a problem or suggest an idea;
code changes come only from people he invites.

## Issues

Open an issue on [GitHub](https://github.com/keltzbm/riffle/issues/new/choose) for:

- **a bug:** a command that fails, crashes, or gives a wrong result;
- **wrong data:** a card, price, decklist, or other fact that doesn't match its source;
- **an idea:** something Riffle could do, or do better.

Each kind has a short form. Search the [open issues](https://github.com/keltzbm/riffle/issues)
first, and put one problem in each issue. For a bug, the most useful things are the exact command, what
you expected, what happened instead, and the full output, pasted as text rather than a screenshot. For
wrong data, name the card or list, the value Riffle shows, the value its source shows, and a link to
the source.

## Security problems

Don't open a public issue for a security problem. Report it privately: on GitHub, open the repository's
**Security** tab and choose **Report a vulnerability**.

## Pull requests

Pull requests are by invitation only: GitHub accepts them only from collaborators on the repository.
If you'd like to work on something, open an issue about it first.

You're welcome to fork Riffle and change your copy. It is licensed under the
[GNU Affero General Public License, version 3](LICENSE): if you run a changed version for other people
over a network, the license requires you to offer them its source code.

## For invited contributors

Set up as the [README](README.md#install) says, then bring up the database the tests use:

```bash
riffle db up && riffle db upgrade
```

Every change goes on its own branch and needs:

- tests for every new or changed line;
- an entry in [CHANGELOG.md](CHANGELOG.md) under **Unreleased**;
- these five checks passing, with test coverage of at least 90%:

```bash
uv sync --locked && uv run pytest --cov-fail-under=90 && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
```

A database change is a hand-written, numbered migration in `src/riffle/db/migrations/versions/`, with
the tables in `src/riffle/db/models.py` kept in step. A commit message has a title in the imperative
("Add …", "Fix …"), then plain prose wrapped at 72 columns: what was wrong, and what changes.
[DESIGN.md](DESIGN.md) explains how Riffle is put together.
