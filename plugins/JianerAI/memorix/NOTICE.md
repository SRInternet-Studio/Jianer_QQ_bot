# Jianer Memory notices

`plugins/JianerAI/memorix` is the Jianer integration of the long-term memory
runtime.  The public name used by Jianer is **Jianer Memory**.  The Python
package path and several serialized identifiers still contain `memorix` or
`a_memorix` for compatibility with existing installations and data.  Those
identifiers are implementation compatibility keys, not a claim of ownership
of an upstream name.

## Upstream source and license

The memory runtime in this directory contains code adapted from **A_Memorix**,
whose upstream source is attributed to A_Dawn:

- Source repository: <https://github.com/A-Dawn/A_memorix>
- Upstream integration branch referenced by the source notices: `MaiBot_branch`
- Applicable upstream license: GNU Affero General Public License, version 3

The imported snapshot does not currently record an upstream commit or release
tag.  Treat the repository URL and the retained source notices as the available
provenance record, and resolve the exact upstream revision before publishing a
new redistribution or making a licensing statement about individual files.

The complete AGPL-3.0 text is retained in [`LICENSE`](LICENSE).  The upstream
copyright and license terms apply to upstream-derived portions of this
directory.  Jianer-specific integration and adapter changes do not remove or
replace those terms.  When distributing a modified or network-served copy,
review the AGPL source and corresponding-source requirements before release.
The repository-level Apache-2.0 license applies to other Jianer files only
where their own notices permit it; it is not a blanket replacement for the
AGPL terms in this directory.
Because the current snapshot does not carry file-by-file provenance headers,
the conservative distribution rule is to treat this directory as covered by
the retained AGPL-3.0 notice unless a separate, documented grant says
otherwise.

`LICENSE-MAIBOT-GPL.md` is retained as an upstream grant notice.  It records a
grant addressed to the MaiBot project; it is not a new Jianer grant, and it
does not change the license for other uses.

## Compatibility references

The code keeps a small number of historical references so existing data and
integrations continue to work:

- `a_memorix` configuration keys and plugin IDs;
- `a-memorix`/`A_memorix` paths in migration and compatibility documentation;
- `A_Memorix.*` request-type labels used by host model adapters;
- legacy environment variables such as `A_MEMORIX_NATIVE_MATCHER_MIN_PATTERNS`;
- historical Python aliases such as `AMemorixPlugin` and
  `A_MEMORIX_TEXT_TASK_PRIORITY`;
- MaiBot migration commands and `maibot_*` source labels.

These references identify wire formats, migration inputs, or optional host
adapters.  They do not imply that Jianer is produced, sponsored, or endorsed
by MaiBot or by the A_Memorix upstream project.

## Names and trademarks

“A_Memorix”, “MaiBot”, and related names belong to their respective owners.
Jianer Memory is an independent integration maintained for JianerAI.  This
repository makes no affiliation, endorsement, or trademark license claim for
those names.  Use the names only to identify the compatible source, format, or
migration path described above.

## Dependency licenses

Runtime dependencies are listed in [`requirements.txt`](requirements.txt).
They are separate works with their own license terms; downstream distributors
must preserve the notices required by those dependencies.

The LPMM/OpenIE code paths implement import and conversion of an external data
format.  No LPMM dataset is bundled here; any imported dataset remains subject
to the license and attribution requirements supplied with that dataset.
