# Jianer Memory maintenance policy

This directory is maintained as part of the JianerAI integration and is
published under the notices in [`NOTICE.md`](NOTICE.md).  The implementation
contains code adapted from the upstream A_Memorix project.  The upstream
license and attribution remain applicable to upstream-derived portions; this
file does not relicense them.

## Source record

- Upstream project: <https://github.com/A-Dawn/A_memorix>
- Upstream branch named by the source record: `MaiBot_branch`
- Upstream license text: [`LICENSE`](LICENSE)
- Historical MaiBot-specific grant: [`LICENSE-MAIBOT-GPL.md`](LICENSE-MAIBOT-GPL.md)
- Jianer integration notes: [`JIANER_INTEGRATION.md`](JIANER_INTEGRATION.md)

The source record is kept so that downstream users can identify the origin of
the adapted runtime.  It is not a statement that Jianer is the upstream
project, that Jianer speaks for its authors, or that the upstream grant covers
uses outside its stated scope.

## Local changes

Changes required for Jianer host loading, configuration, adapters, runtime
isolation, or tests may be made in this repository.  Keep those changes small,
document behavior changes in `CHANGELOG.md`, and preserve upstream copyright,
license, and modification notices when editing an upstream-derived file.

When a change is useful independently of Jianer, consider sending it to the
upstream project through its own contribution process.  Do not copy code from
another project into this directory without recording its source and license
in `NOTICE.md` and preserving the required license text.

## Compatibility identifiers

The package import path `plugins.JianerAI.memorix`, the `a_memorix` config key,
and `a-memorix` migration paths are retained for existing installations.  They
are serialized or API compatibility identifiers and should not be renamed
without a migration plan.  New user-facing documentation and UI should use
**Jianer Memory**.

MaiBot names and migration commands may remain where they describe an input
format or optional adapter.  Such references must say what is compatible and
must not suggest endorsement or affiliation.

## Release checklist

Before distributing a modified copy:

1. Keep [`LICENSE`](LICENSE), [`NOTICE.md`](NOTICE.md), and any applicable
   upstream notices with the source and binary distribution.
2. Mark material changes and their dates in the changed file or release notes.
3. Recheck AGPL-3.0 source and network-service obligations for the complete
   distribution.
4. Review dependency licenses listed in `requirements.txt` separately.
5. Verify that public labels say Jianer Memory and that compatibility keys are
   explained as legacy identifiers.
