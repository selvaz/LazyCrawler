# Sample news-monitor output

Real output from a 4-article smoke-test run (2 BBC World articles, 2 Clarín
Economía articles — see the news-monitor pipeline section in the main
[README](../../README.md)), renamed for clarity (a real run's filenames are
`news_full_<session_id>_<region>.md`, etc.).

- [`news_digest.md`](news_digest.md) — an executive digest grouped by theme
  across every region. Theme is one of three shapes the digest can take;
  which one a run produces depends on its cycle and engine, and the other
  two group by geography and by asset class instead. See
  `make_news_report._select_digest_format`.
- [`news_full_global.md`](news_full_global.md) — the `global` region report:
  English-language sources (BBC World), index + full articles.
- [`news_full_south_america.md`](news_full_south_america.md) — the
  `south_america` region report: Clarín (Spanish, `smart` mode) — same index
  format, English summaries, full articles kept in the original language.
- [`news_cost.md`](news_cost.md) — the per-run cost report (DeepSeek token
  usage + USD cost, by agent).
