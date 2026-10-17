# Third-Party Notices

RollPig Plus bundles a small set of third-party assets/dependencies to keep image
rendering stable in offline or Docker environments.

## Google Noto Emoji PNG assets

- Source: [googlefonts/noto-emoji](https://github.com/googlefonts/noto-emoji)
- Bundled path: `nonebot_plugin_rollpig_plus/resource/emoji/google-emoji.zip`
- Usage: offline color Emoji rendering for Pillow cards via `pilmoji`
- Change note: assets are repackaged into a ZIP archive for distribution; image
  content is not intentionally modified by this project.
- License: Depending on the specific resource and Google's repository revisions, these assets are licensed under either the **SIL Open Font License, Version 1.1** or the **Apache License, Version 2.0**. Copies of both are provided at `LICENSES/OFL-1.1.txt` and `LICENSES/Apache-2.0.txt`.

## Source Han Sans SC Medium

- Source: [adobe-fonts/source-han-sans](https://github.com/adobe-fonts/source-han-sans)
- Bundled path: `nonebot_plugin_rollpig_plus/resource/fonts/SourceHanSansSC-Medium.otf`
- Usage: default CJK text font for Pillow-rendered pig cards, catalogs, and
  yesterday recap card body text.
- Change note: the font file is redistributed as-is and is not intentionally
  modified by this project.
- License: **SIL Open Font License, Version 1.1**. A copy is provided at
  `LICENSES/OFL-1.1.txt`.

## RollPig Daily Serif subsets

- Source: [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk)
- Bundled path: `nonebot_plugin_rollpig_plus/resource/fonts/RollPigDailySerif-*.subset.otf`
- Usage: fixed masthead, section labels, and ranking titles on the pigpen daily
  report card; user names and other dynamic text continue to use Source Han Sans.
- Change note: the Regular, Bold, and Black Noto Serif SC sources are subsetted
  and renamed to `RollPig Daily Serif`; the retained character list is shipped
  alongside the fonts so future fixed labels can rebuild the same subsets.
- License: **SIL Open Font License, Version 1.1**. A copy is provided at
  `LICENSES/OFL-1.1.txt`.

## ZCOOL KuaiLe Regular

- Source: [google/fonts](https://github.com/google/fonts/tree/main/ofl/zcoolkuaile)
- Bundled path: `nonebot_plugin_rollpig_plus/resource/fonts/ZCOOLKuaiLe-Regular.ttf`
- Usage: default display font for the Pillow-rendered yesterday recap card.
- Change note: the font file is redistributed as-is and is not intentionally
  modified by this project.
- License: **SIL Open Font License, Version 1.1**. A copy is provided at
  `LICENSES/OFL-1.1.txt`.

## pilmoji

- Source: [jay3332/pilmoji](https://github.com/jay3332/pilmoji)
- Usage: text drawing helper that composes Emoji image assets into Pillow output.
- License: see the upstream package metadata distributed with `pilmoji`.
