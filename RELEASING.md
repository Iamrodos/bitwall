# Releasing bitwall

## Versions

bitwall has one version, `MAJOR.MINOR.PATCH`, written in one place:
`__version__` in `bitwall/__init__.py`. The Python package reads it from
there (`pyproject.toml`), and every Rust crate carries the same number in
its `Cargo.toml`.

**MAJOR is the protocol's version**, the `v1` a wall says in `hello`
(`VERSION` in `bitwall/protocol.py` and `bitwall/rust/src/protocol.rs`).
A sender pinned to 1.x speaks the protocol every v1 wall speaks.
`tests/bitwall-tests` fails if the package's major, the protocol's
version and the crates' versions disagree, and `cargo test` in
`bitwall/rust` checks the crate's own.

- MINOR: something new a sender or receiver may use, and nothing an
  existing one relied on taken away.
- PATCH: a fix.

A receiver also says a code hash, the first twelve hex digits of the
SHA-256 of the files it is built from (`bitwall/receivers/__init__.py`),
in its `receiver` line. That says which build a wall is running; the
release says which protocol it speaks.

## Branches and tags

- `main` is where work happens.
- Each release is a tag, `vX.Y.Z`, on `main`.
- `v1` is a branch that points at the latest 1.x release. Senders pin to
  it, so they take fixes and additions and never a change of protocol.
  It only ever moves forward, by fast-forward.
- A change that breaks the protocol is a new MAJOR: the protocol's
  `VERSION` and the package's major go up together, its first release is
  `v2.0.0`, and a `v2` branch starts there. `v1` stays where it is.

## Making a release

1. Run the full tests on a Mac with uv, node and a C compiler:
   `tests/bitwall-tests` (which runs `tests/bitwall-contracts` and
   `tests/paint/run` among its groups), and `cargo test` in each crate:
   `bitwall/rust`, `bitwall/receivers/core-rs`, `bitwall/receivers/term`,
   `bitwall/receivers/eink`, `senders/games/cells/rust`. Build the crates
   one at a time.
2. Set the version in `bitwall/__init__.py` and in each of those
   `Cargo.toml` files, and run `cargo test` in each again so its
   `Cargo.lock` follows.
3. Check the package builds: `uv build`, then import it from outside the
   repo, `uv run --no-project --with dist/bitwall-X.Y.Z-py3-none-any.whl
   python -c "import bitwall, bitwall.letters; print(bitwall.__version__)"`.
4. Commit, tag and push:
   `git tag -a vX.Y.Z -m vX.Y.Z && git push origin main vX.Y.Z`.
5. Write the release notes as a GitHub release on the tag: what a sender
   or a receiver can now do, and anything it must change.
6. Move the major branch: `git push origin vX.Y.Z^{commit}:refs/heads/v1`
   (a fast-forward; if the push is refused, `v1` has something the
   release does not, and that is looked at before anything is forced).

## Depending on bitwall

A Python sender written as a script names it in its PEP 723 header:

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["bitwall @ git+https://github.com/iamrodos/bitwall@v1"]
# ///
```

or, for a project, `uv add "bitwall @ git+https://github.com/iamrodos/bitwall@v1"`
or `pip install "bitwall[all] @ git+https://github.com/iamrodos/bitwall@v1"`.
The extras are `arrays` (numpy), `letters` (Pillow, for TILE and the
outline fonts) and `all`. TmuxSource and CommandSource need the terminal
emulator built from a clone, `bitwall/vt/build`.

A Rust sender takes the library crate from the same branch. Cargo finds a
crate in a subdirectory of a git repository by its name, and the library's
name, `bitwall`, is the only one of that name in the repo:

```toml
[dependencies]
bitwall = { git = "https://github.com/iamrodos/bitwall", branch = "v1" }
```

The crates are edition 2024 where they say so, which needs Rust 1.85 or
later.
