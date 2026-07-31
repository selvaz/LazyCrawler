# Operations catalog integration

LazyCrawler keeps its authoritative `news.db` and per-session cost databases.
The scheduled pipeline additionally publishes to the shared LazyTools
operations catalog:

* `run_news_crawl.py` registers the crawl session, metadata and summary.
* `make_news_report.py` registers region reports, digest and cost reports.

Initialize the catalog once from the LazyTools checkout:

```powershell
powershell -ExecutionPolicy Bypass -File ..\LazyTools\setup_operations.ps1
```

The Telegram scheduler wrapper imports the persisted catalog paths before
starting Python. If `lazytoolkit` is not installed, the crawl continues and
reports the skipped central registration instead of failing the scheduled job.
