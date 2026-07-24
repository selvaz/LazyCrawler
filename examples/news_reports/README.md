# Sample news-monitor output

Real output from a 4-article smoke-test run (2 BBC World articles, 2 Clarín
Economía articles — see the news-monitor pipeline section in the main
[README](../../README.md)), renamed for clarity (a real run's filenames are
`news_full_<session_id>_<region>.md`, etc.).

- [`news_digest.md`](news_digest.md) — the DeepSeek executive digest, grouped
  by theme across every region.
- [`news_full_global.md`](news_full_global.md) — the `global` region report:
  English-language sources (BBC World), index + full articles.
- [`news_full_latam.md`](news_full_latam.md) — the `latam` region report:
  Clarín (Spanish, `smart` mode) — same index format, English summaries, full
  articles kept in the original language.
- [`news_cost.md`](news_cost.md) — the per-run cost report (DeepSeek token
  usage + USD cost, by agent).
