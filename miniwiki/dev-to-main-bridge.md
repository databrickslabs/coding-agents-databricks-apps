# Dev-to-main bridge

These measurements are made with `git rev-list --left-right --count`; the left
number is unique to the first ref and the right number is unique to the second.

| Comparison | Left | Right | Meaning |
|---|---:|---:|---|
| `origin/main...dev` | 0 | 81 | private dev contains 81 commits beyond public main |
| `origin/dev...dev` | 0 | 66 | private dev contains 66 commits beyond the stale public dev line |
| `dev...private/dev` | 0 | 0 | local dev and private dev are identical |

`origin/main` is an ancestor of `dev`. Publication is selective and must not be
implemented as a wholesale merge of `dev`.
