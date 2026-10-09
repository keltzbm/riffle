# Security

Riffle runs on your own machine, against your own files and a database you run. It
fetches public data from the sources named in the README and sends nothing anywhere
else. Even so, a bug in how it reads a file or a page from one of those sources could
be made to do harm, and I want to hear about it privately first.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: on the repository's **Security** tab,
choose **Report a vulnerability**. It opens a private advisory only you and I can see.
Please don't open a public issue for a security problem.

Say what you found, how to reproduce it, and what it lets someone do. I'll answer
within a week, say whether I agree it's a vulnerability, and tell you when a fix
ships. Credit in the fix's changelog entry is yours if you want it.

## What counts

- Reading a crafted file (a ManaBox export, a deck note, a downloaded price list or
  page) that makes Riffle write outside its own folders, run a command, or leak a
  secret.
- A dependency with a known vulnerability that Riffle's use of it exposes.
- Riffle sending data anywhere other than the sources the README names.

Out of scope: problems in the sources themselves, and attacks that need access to
your machine or your database password.

## Supported versions

Only `main` and the newest tagged release get fixes. Dependabot watches the lock file
and opens a pull request when a dependency has an advisory.
