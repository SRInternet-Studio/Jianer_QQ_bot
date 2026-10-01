# Jianer Memory integration

Jianer Memory is the JianerAI long-term memory integration.  It is loaded from
the stable package path `plugins.JianerAI.memorix` and stores runtime data in
`data/jianer_ai_memorix/` by default.

The storage, retrieval, graph, Episode, profile, image-memory, maintenance,
and migration layers include code adapted from the upstream A_Memorix project.
The source URL, AGPL-3.0 terms, and the historical MaiBot-specific grant are
recorded in [`NOTICE.md`](NOTICE.md), [`LICENSE`](LICENSE), and
[`LICENSE-MAIBOT-GPL.md`](LICENSE-MAIBOT-GPL.md).  The Jianer project does not
claim ownership of upstream-derived code and does not represent an affiliation
with or endorsement by A_Dawn, A_Memorix, or MaiBot.

## Host boundary

`core.storage` can be imported without loading the host AI configuration.
Embedding, retrieval runtime, and optional MaiBot compatibility services are
loaded lazily.  JianerAI adapters provide the embedding provider, LLM client,
and conversation-scope filter through the local public interfaces.

The host should call the service and runtime facades in this package.  The
historical `src.*` import paths are not required by Jianer and must not be
reintroduced as new dependencies.

## Compatibility names

Existing installations use `a_memorix` in configuration, serialized payloads,
and some migration source labels.  Those machine-facing keys remain stable to
avoid data loss and are documented as compatibility identifiers.  New UI and
   user-facing text should call the feature **Jianer Memory**.
