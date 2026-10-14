# NOTICE

The Wordle plugin (`plugins/Wordle/`) adapts the game rules and exam
dictionaries from **nonebot-plugin-wordle**.

- Source: https://github.com/noneplugin/nonebot-plugin-wordle
- Author: MeetWq
- License: MIT

The files under `resources/words/*.json` are the upstream dictionaries
(`CET4`, `CET6`, `专四`, `专八`, `考研`, `GRE`, `GMAT`, `TOEFL`, `SAT`,
`IELTS`); each entry maps a word to its Chinese and English gloss.  The board
rendering and the green/yellow/gray scoring rules are reimplemented for the
JianerCore plugin runtime.

The upstream MIT license text follows.

```
MIT License

Copyright (c) 2022 MeetWq

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
