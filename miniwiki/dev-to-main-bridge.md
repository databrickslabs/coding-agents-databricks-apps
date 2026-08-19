# Dev-to-main bridge

These measurements are made with `git rev-list --left-right --count`; the left
number is unique to the first ref and the right number is unique to the second.

| Comparison | Left | Right | Meaning |
|---|---:|---:|---|
| `origin/main...dev` | 0 | 83 | private dev contains 83 commits beyond public main |
| `origin/dev...dev` | 0 | 68 | private dev contains 68 commits beyond the stale public dev line |
| `dev...private/dev` | 2 | 0 | local dev has two unpushed continuity commits beyond the private mirror |

`origin/main` is an ancestor of `dev`. Publication is selective and must not be
implemented as a wholesale merge of `dev`.
