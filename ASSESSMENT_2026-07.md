# Assessment di implementazione — LazyCrawler (Luglio 2026)

> Audit indipendente di LazyCrawler **v0.14.0** (commit `27fcb6c` — "Deep audit
> round 3", 2026-07-05). Metodo: lettura integrale dei 21 moduli sorgente del
> package `lazycrawler/` (~6.500 LOC) e della suite di test (~3.000 LOC),
> esecuzione reale della suite in un venv dedicato, verifica di CI/packaging/docs
> e riscontro puntuale dei problemi noti dichiarati in `ANALYSIS.md`.
> Ogni affermazione è supportata da `file:riga` o da output reale.

---

## 1. Panoramica e stato di salute

LazyCrawler è un crawler/web-search generico con tre modalità di estrazione
(`pure` senza LLM, `ml` locale zero-token, `smart` via LazyBridge), persistenza
SQLite con dedup a 3 livelli, motore sincrono (`crawler.py`) e asincrono
(`async_crawler.py`) che condividono la stessa pipeline post-fetch
(`_pipeline.py`), e un layer di tool per agenti LLM (`tools.py`).

**Stato di salute complessivo: molto buono.** Il codice è insolitamente curato
per un progetto pre-1.0: redirect gestiti manualmente con ri-validazione SSRF
per hop (`http.py:634-661`, `async_crawler.py:281-345`), body streamati con cap
di byte (`http.py:663-677`), robots.txt rispettato di default con stato
esplicito `robots_blocked` (`_pipeline.py:156-171`), nessuna eccezione
silenziata (logger + `strict`), risorse chiuse tramite context manager e
`weakref.finalize` (`http.py:587-589`, `db.py:218`). I tre round di audit
precedenti (documentati in `ANALYSIS.md` e nel commit `27fcb6c`) hanno chiuso i
difetti maggiori; **non ho trovato issue CRITICHE nuove**. I residui sono
esposizioni di sicurezza note/documentate, gap di parità sync/async, drift
documentazione/codice e copertura test disomogenea.

### Voti

| Asse | Voto | Motivazione sintetica |
|---|---|---|
| **Correttezza** | **A-** | Nessun bug critico trovato; concorrenza gestita con lock e stato per-run; restano edge case minori (validazione modalità assente nel sync, `render_js` che fabbrica status 200, `max_retries=0` silenzioso). |
| **Sicurezza** | **B+** | Guardia SSRF per-hop, cap di byte, robots, prompt-hardening, no secrets nel repo. Resta come vera esposizione residua solo il TOCTOU/DNS-rebinding sul percorso agente (documentato, A1). Minori: nessuna allowlist esplicita di schemi URL (M3); il fallback PDF via `urllib` è **debito di consistenza non raggiungibile a guardia attiva** (v. B11, declassato in revisione — non è un bypass SSRF sfruttabile). |
| **Test** | **B** | 221 test, tutti verdi in 6.75s, offline e idiomatici; coverage totale 73% ma con buchi: `browser.py` 34%, `llm.py` 35%, `pdf.py` 54%. |
| **Documentazione** | **B** | README/docs estesi e in gran parte allineati; drift residuo in `ml.py`/`config.py` (docstring "later phase" su feature già implementate), `ANALYSIS.md` cita file di test inesistenti e un item ROADMAP mai aggiunto. |
| **Manutenibilità** | **B+** | Architettura pulita, ruff/format al 100%; penalizzano la duplicazione sync/async (robots, rate-limit, client HTTP, traversal) e la tripla copia della versione. |

---

## 2. Stato dell'implementazione

### Architettura (verificata sul codice)

| Modulo | Responsabilità | Stato |
|---|---|---|
| `config.py` (478 r.) | 6 dataclass di configurazione, tutte documentate | Completo |
| `http.py` (961 r.) | Client HTTP, retry/backoff, redirect manuali + SSRF, normalizzazione URL, hashing dedup, robots checker | Completo |
| `_pipeline.py` (916 r.) | Pipeline per-pagina condivisa sync/async (cache, redirect/canonical adoption, estrazione, artifact, persistenza) | Completo |
| `crawler.py` (506 r.) | Orchestratore: DFS sequenziale, BFS parallela, best-first | Completo |
| `async_crawler.py` (940 r.) | Motore aiohttp (pure+ml), riusa la pipeline in executor | Completo (smart escluso by design) |
| `search.py` (653 r.) | Seed da DuckDuckGo / Brave / Tavily / Gemini | Completo (gemini in "answer mode" sintetico) |
| `db.py` (611 r.) | SQLite WAL, dedup 3 livelli, TTL, FTS5 con fallback LIKE, artifacts | Completo |
| `ml.py` (471 r.) | Scoring semantico link (Model2Vec) + estrazione locale (TextRank/YAKE/spaCy/VADER) | Completo (docstring obsolete, v. §3) |
| `llm.py` / `prompts.py` | Wrapper LazyBridge, output strutturato, prompt anti-injection | Completo |
| `pdf.py` (457 r.) | Estrazione PDF (PyMuPDF→pypdf→pdfplumber), artifact PDF | Completo (fallback `urllib`, v. §3) |
| `artifacts.py`, `markdown.py` | Tabelle/immagini/chart/SVG, anchor `[[artifact:<hash>]]`, `render_for_rag` | Completo |
| `tools.py`, `presets.py` | 7 tool LazyBridge, 11 preset | Completo |
| `browser.py`, `ratelimit.py`, `text.py`, `models.py`, `_log.py` | Rendering JS, rate-limit per host, estrazione testo/date/link, output types | Completo |

### Verifica ROADMAP.md vs stato reale

- **Done (v0.2–v0.8)**: tutti gli item dichiarati "Done" risultano effettivamente
  implementati (spot-check su parallel mode `crawler.py:356-390`, rate limiter
  `ratelimit.py:30-65`, PDF single-download `http.py:742-751`, markdown anchors
  `artifacts.py:428-466`, context manager ovunque, presets `presets.py:148-318`).
- **Next #4 (Politeness: autothrottle + proxy rotation)**: **non implementato** —
  nessuna occorrenza di autothrottle/proxy nel package (grep negativo).
- **Next #6 (Smarter link frontier)**: **parzialmente superato** — il best-first
  semantico esiste (`crawler.py:392-439`, `ml.py:104-229`) ma il **seeding da
  `sitemap.xml` non esiste** (grep "sitemap" nel package: 0 risultati) e non c'è
  frontier con priorità globale persistente.
- **Later**: azioni interattive (click/scroll/form) non implementate; grounding
  Gemini con URL sorgente non implementato (dipende da LazyBridge); re-esposizione
  via `lazytools` marcata fatta (esterna a questo repo, non verificabile qui).

### Residui dai problemi noti di ANALYSIS.md (§8 della richiesta)

| Item ANALYSIS.md | Stato a oggi |
|---|---|
| §5.1 doppia emissione redirect condiviso | **Chiuso** — guard presente in `_pipeline.py:277-285`, regression test `tests/test_audit_fixes.py` |
| §5.2 PDF con query string (async) | **Chiuso** — `async_crawler.py:305-311` |
| §5.3 SSRF best-effort (DNS rebinding) | **Aperto per design** — documentato in `http.py:252-259` e README |
| §5.4 fallback PDF via `urllib` | **Aperto** — `pdf.py:118-142` invariato; inoltre ANALYSIS dice "tracked in ROADMAP.md" ma **ROADMAP non contiene l'item** (incoerenza doc) |
| §5.5 cache-enrich non ricorsivo | **Aperto** — `_pipeline.py:512-537` ritorna `[]` senza frontier anche con `recurse_from_cache=True`; nessuna nota in docs |
| §5.5 robots/domain con porta nel netloc | **Aperto** — `http.py:900-903,938-947`, `db.py` domain via `get_base_domain` |
| §5.5 politeness (autothrottle/proxy) | **Aperto** — ROADMAP "Next" |
| §5.6 "193 passed, 5 skipped" | **Stantio** — oggi 221 passed (v. §6); ANALYSIS §3 cita anche file di test che non esistono più (`tests/decoupled_test.py`, `robots_test.py`, `parallel_test.py`, `tools_test.py` → oggi `test_*.py`) |

---

## 3. Issue trovate — per severità

### CRITICA

Nessuna issue critica individuata in questo round.

### ALTA

**A1 — SSRF: TOCTOU / DNS-rebinding sulla guardia (noto, accettato ma residuo)**
`lazycrawler/http.py:239-292` (`is_blocked_address`), `async_crawler.py:113-153`.
La guardia risolve il DNS al momento del check; `requests`/`aiohttp` ri-risolvono
al connect. Un DNS ostile può restituire un IP pubblico al check e uno privato al
connect. Documentato onestamente (docstring + README:96-101), ma resta la
principale esposizione del percorso agente (`CrawlerTools`), dove URL arbitrari
arrivano da un LLM. **Impatto**: accesso a servizi interni/metadata in deployment
senza egress control. **Mitigazione possibile in-code**: risolvere una volta e
connettersi all'IP validato (pin del DNS) o usare un connector custom.

**A2 → declassata a B11 in revisione adversariale.**
Verifica sul codice: il ramo `urllib` (`_pipeline.py:297-303`) è raggiungibile
solo quando `is_pdf=True` **e** `pdf_bytes` è vuoto; ma la fetch sync popola
sempre `content` per qualunque risorsa "PDF-like" (`http.py:742-751`), quindi
l'unico modo per avere `pdf_bytes` vuoto è il percorso `render_js`
(`http.py:719-722` ritorna html+status senza `content`) — e `render_js` è
**mutuamente esclusivo** con `block_private_addresses` per costruzione
(`http.py:538-545`). Ne segue che **a guardia SSRF attiva il ramo `urllib` è
irraggiungibile**: non c'è alcun bypass SSRF sfruttabile. Resta un debito di
consistenza (no retry/backoff, no proxy della sessione, percorso di rete non
uniforme). Impatto reale rivalutato: BASSO. Dettaglio e correzione in **B11**.
La stessa ANALYSIS §5.4 già lo classificava come "residual exposure minimal";
l'assessment lo aveva escalato erroneamente ad ALTA.

### MEDIA

**M1 — `WebCrawler.crawl_many` non valida `content`/`links` (gap di parità con l'async)**
`lazycrawler/crawler.py:229-235`: qualunque stringa diversa da "pure"/"ml" cade
nel ramo smart (`_pipeline.py:397-400`) con `res.llm=None` →
`AttributeError` su `res.llm.extract_content` (`_pipeline.py:611`), loggata e
pagina persa in non-strict, crash in strict. `AsyncWebCrawler.crawl_many` invece
valida esplicitamente (`async_crawler.py:626-631`). **Impatto**: un typo
(`mode="purr"`) produce un crawl vuoto con errori criptici invece di un
`ValueError` chiaro.

**M2 — `render_js` fabbrica `status=200` e può cacheare pagine d'errore come `done`**
`lazycrawler/http.py:719-723`: il path browser ritorna sempre
`FetchResult(html=..., status=200)`; una pagina 404/500 renderizzata (il browser
non espone qui lo status reale) viene emessa `done` e persistita in cache con
TTL (`db.py:263-279`), avvelenando i risultati per 24h di default.

**M3 — Nessuna allowlist esplicita di schemi URL nel fetch**
`lazycrawler/http.py:692-716` e `async_crawler.py:281-345` non verificano che lo
scheme sia http/https (né su seed né su Location dei redirect); di fatto
`requests`/`aiohttp` rifiutano `file://` con `InvalidSchema`, e il browser ha la
sua allowlist (`browser.py:37-49`), ma la difesa è implicita e fragile rispetto
a futuri backend. Il check costerebbe 2 righe per client. (Nota: i link estratti
sono già filtrati http/https in `text.py:159-161`; il buco riguarda seed e
redirect.)

**M4 — Engine `gemini`: `session_id` e `max_results` del chiamante ignorati**
`lazycrawler/search.py:419-420` chiama `_run_gemini(query, topic, content_mode)`
senza `session_id`; `search.py:637` ne genera uno proprio. Ma
`CrawlerTools.web_search` (`tools.py:264-284`) restituisce all'agente il **suo**
`sid` (`search_<uuid>`): con engine gemini quel `session_id` non corrisponde a
nessuna sessione DB, quindi `get_session_pages(session_id)` ritorna vuoto.
Anche `max_results` e `timelimit` non sono propagati (irrilevanti in answer
mode, ma non documentato).

**M5 — Copertura test fortemente disomogenea**
Output reale `pytest --cov` (v. §6): `browser.py` **34%**, `llm.py` **35%**,
`pdf.py` **54%**, `text.py` 68%, `http.py` 69% (totale 73%). I percorsi non
coperti sono proprio quelli con più I/O ed error-handling (parser PDF reali,
envelope LLM, ciclo di vita Playwright). Nessun modulo è a zero, ma la fiducia
sulle regressioni in quei tre moduli è bassa.

**M6 — Emissioni non conteggiate senza cap di memoria**
`lazycrawler/_pipeline.py:804-819` (`_emit`): con `count=False`
(fetch_error/robots_blocked/no_text) il risultato è sempre appeso a
`st.results`, senza limite legato a `max_pages`. Un crawl con molti seed/link
morti o un sito ostile pieno di URL bloccati accumula PageResult senza bound
(il cap ferma solo le pagine `done`). Stesso comportamento nell'async via
`_emit_status` (`async_crawler.py:915-928`).

### BASSA

**B1 — Duplicazione sync/async con divergenze comportamentali**
`_AsyncRobotsChecker` (`async_crawler.py:412-472`) vs `RobotsChecker`
(`http.py:878-961`); `_AsyncRateLimiter` (161-201) vs `HostRateLimiter`
(`ratelimit.py:30-65`); `_AsyncHTTPClient._fetch_once` (281-345) vs
`HTTPClient._request/fetch` (634-778); traversal `_crawl_bfs`/`_crawl_best_first`
(714-767) vs `_crawl_parallel`/`_crawl_ordered` (`crawler.py:356-439`). Divergenza
concreta: il fallback di estrazione testo async (`async_crawler.py:347-361`)
collassa tutti i newline (`re.sub(r"\s+", " ", ...)`) mentre quello sync
(`http.py:481-495`) preserva paragrafi e decodifica le entità HTML → stesso HTML,
`text` diverso (e quindi `content_hash` diverso) tra i due motori quando
trafilatura è assente.

**B2 — Docstring obsolete su ml mode**
`lazycrawler/ml.py:11-14` ("``extract_content`` currently returns clean text
only") e `config.py:362-364` ("summary_sentences, ... Reserved for
``content="ml"`` ... (a later phase)"): l'estrazione locale è invece completa
(`ml.py:456-471`). Fuorviante per chi valuta il modulo dalla docstring.

**B3 — `ANALYSIS.md` non allineato al repo**
§3 elenca `tests/decoupled_test.py`, `robots_test.py`, `parallel_test.py`,
`tools_test.py` (inesistenti: la suite è `tests/test_*.py`); §5.6 riporta "193
passed, 5 skipped" (oggi 221 passed, 0 skipped); §5.4 dichiara l'item PDF
"tracked in ROADMAP.md" ma ROADMAP.md non lo contiene.

**B4 — Cache-enrich non ricorsivo (trade-off non documentato)**
`lazycrawler/_pipeline.py:512-537`: l'arricchimento pure→ml/smart da cache
ritorna sempre `[]`, quindi con `recurse_from_cache=True` una cache calda pota
la traversata che un crawl freddo avrebbe seguito (ANALYSIS §5.5, ancora aperto
e senza nota in docs/guide).

**B5 — Robots: chiave cache per host senza scheme, porta inclusa**
`http.py:938-947`: la cache robots è per `host` (netloc) e ignora lo scheme —
la prima variante fetchata (http o https) vince per entrambe;
`example.com:8080` e `example.com` sono host distinti (noto, ROADMAP
"Acknowledged trade-offs").

**B6 — `max_retries=0` ritorna un `FetchResult` vuoto in silenzio**
`http.py:725-778` (`for attempt in range(1, cfg.max_retries + 1)`) e
`async_crawler.py:266-279`: con `max_retries=0` il loop non esegue mai e la
fetch fallisce senza log. Meritano un `max(1, ...)` o validazione config.

**B7 — Versione triplicata**
`pyproject.toml:7` (0.14.0), `lazycrawler/__init__.py:64` (0.14.0),
`config.py:259` (User-Agent "LazyCrawler/0.14"): tre punti da aggiornare a mano
a ogni release.

**B8 — Esempio non portabile e con TLS disabilitato**
`examples/basic_usage.py:15` hardcoda `D:\serious_tests\ecosystemv0.9.1`
(path Windows) e `:37` usa `verify_ssl=False` come default dell'esempio —
copia-incollabile in produzione con verifica TLS spenta.

**B9 — Race benigna nel caricamento dell'embedder**
`lazycrawler/ml.py:63-82`: il lock è rilasciato tra il check della cache e la
costruzione dell'`_Embedder`; due thread possono caricare il modello (~30MB)
due volte. Solo spreco, nessuna corruzione.

**B10 — Type hints incompleti negli interni**
La pipeline usa `st: Any`, `res: Any`, `fr: Any` (`_pipeline.py:98-106,195-205`)
e `_State.cfg/ml_cfg: Any` (`crawler.py:86-87`) nonostante `py.typed` sia
spedito: i tipi reali (`_State`, `_Res`, `FetchResult`) esistono e sarebbero
annotabili senza refactoring. Nessun TODO/FIXME nel codice (grep negativo).

**B11 — Fallback PDF via `urllib` fuori dal client condiviso (ex-A2, declassata)**
`lazycrawler/pdf.py:118-142` (`fetch_pdf_bytes` con `urlopen`), invocato da
`_pipeline.py:297-303`. Il download avviene fuori da `HTTPClient`, quindi senza
retry/backoff, senza proxy della sessione e con redirect seguiti da `urlopen`
senza ri-validazione. **Non è però un bypass SSRF**: il ramo è raggiungibile
solo con `pdf_bytes` vuoto e `is_pdf=True`, condizione che si verifica unicamente
sul percorso `render_js` (`http.py:719-722`), il quale è mutuamente esclusivo con
la guardia SSRF (`http.py:538-545`, `ValueError` a costruzione). A guardia attiva
il ramo è irraggiungibile; nel percorso `render_js` la guardia è comunque
disattivata by design e il "proxy" è il browser. È il solo percorso di rete non
uniforme del package (motivo del rilievo). Doc: debito citato in ANALYSIS §5.4
ma **assente da ROADMAP.md** (`grep` negativo su pdf/urllib fallback in ROADMAP;
l'item "Single PDF download" è cosa diversa) → l'affermazione "tracked in
ROADMAP.md" di ANALYSIS §5.4 è falsa. **Impatto**: consistenza/manutenibilità,
non sicurezza sfruttabile. La migrazione (Step 1.1) resta utile ma **non è una
priorità di sicurezza**.

### CI/CD e packaging (verifica §6 della richiesta)

- `.github/workflows/ci.yml`: lint (ruff check+format), test offline con
  coverage su Python 3.10–3.13, job build+`twine check`. Solido. Nota: nessun
  gate di coverage minima (`--cov-fail-under`), e l'extra `ml` è escluso
  volutamente (motivato nel commento, righe 33-42).
- `.github/workflows/docs.yml`: build MkDocs anche su PR, deploy gated. OK.
- `pyproject.toml` vs `requirements.txt`: **coerenti by design** —
  requirements.txt è solo `-e .[all]` (puntatore). Attenzione: funziona **solo
  se lanciato dalla root del repo** (verificato: da altra cwd fallisce con
  "file:///… does not appear to be a Python project"). L'extra `all` è
  auto-referenziale e non può driftare. `dev` extra separato (pytest/ruff/build).
  Un dettaglio: i test usano `pytest-timeout`/`pytest-asyncio`? No — la suite
  non li richiede (nessun test async diretto: l'async è testato via
  `asyncio.run` interni), e `dev` non li include: coerente.

---

## 4. Punti di miglioramento

1. **Unificare i percorsi di rete**: migrare `fetch_pdf_bytes` sul client
   condiviso (A2) e aggiungere l'allowlist di schemi (M3) rende ogni byte in
   ingresso soggetto agli stessi controlli (SSRF, cap, retry, proxy).
2. **Ridurre la duplicazione sync/async** (B1): estrarre la logica comune di
   robots-parsing, rate-limit (calcolo delay) e decisione PDF/charset in
   funzioni pure condivise; allineare il fallback di estrazione testo async a
   `html_to_text_basic`.
3. **Hardening del percorso agente**: pin DNS (risolvi-valida-connetti su IP)
   per chiudere il TOCTOU (A1) almeno sul client sync; in alternativa
   documentare un esempio di egress-control pronto (namespace/em firewall).
4. **Coverage mirata**: portare `llm.py` e `pdf.py` ≥75% con fixture di envelope
   fake e PDF binari minimi in-repo; `browser.py` con un fake Playwright.
5. **Igiene release**: derivare lo User-Agent da `__version__` (B7); aggiornare
   ANALYSIS.md o marcarlo come storico (B3); correggere le docstring ml (B2).
6. **UX degli errori**: validazione modalità nel sync (M1), log/valore d'errore
   per `max_retries=0` (B6), nota in docs sul cache-enrich non ricorsivo (B4).
7. **ROADMAP**: aggiungere formalmente gli item aperti (PDF fallback, sitemap
   seeding, autothrottle) così il tracking dichiarato in ANALYSIS torna vero.

---

## 5. Piano di risoluzione dettagliato (guida operativa)

Prerequisito comune a ogni fase: venv con
`pip install -e ".[all]" && pip install pytest pytest-cov ruff` dalla root del
repo; criterio trasversale di completamento: `ruff check . && ruff format
--check . && pytest -q` verdi.

### Fase 1 — Sicurezza residua (priorità massima)

**Step 1.1 — Migrare il fallback PDF sul client condiviso (ex-A2 → B11) — Effort M**
> Revisione: NON è un fix di sicurezza (il ramo è irraggiungibile a guardia SSRF
> attiva, v. B11). Trattarlo come cleanup di consistenza; declassare la priorità
> rispetto agli Step 1.2 (M3) e 1.3 (A1), che restano gli unici veri item di
> sicurezza della Fase 1.
- Cosa: in `lazycrawler/_pipeline.py:295-303`, sostituire la chiamata a
  `_crawler_fn("extract_pdf")(url, ...)` con un download via `res.http`
  (`HTTPClient.fetch_bytes` esiste già, `http.py:780-801`; alzare il cap a
  `max_pdf_bytes` con un parametro `cap=` opzionale) seguito da
  `extract_pdf_bytes(body)`. Mantenere `extract_pdf` come API pubblica
  deprecata (docstring) per compatibilità con i test che la monkeypatchano
  (`crawler.py:66` re-export).
- Test: aggiornare/aggiungere in `tests/test_pdf.py` un caso che verifichi che
  il download PDF di fallback passa da `HTTPClient` (stub contatore come in
  `conftest.py:56-94`) e rispetta `block_private_addresses`.
- Completamento: nessuna occorrenza di `urlopen` nel flusso di crawl
  (`grep -n urlopen lazycrawler/` → solo eventuale codice deprecato);
  suite verde.

**Step 1.2 — Allowlist di schemi URL (M3) — Effort S**
- Cosa: in `http.py` aggiungere in testa a `_request` (riga ~641) e in
  `async_crawler.py:_fetch_once` (riga ~288):
  `if urlparse(current).scheme not in ("http", "https"): log.warning(...); return None`.
  Applicarlo anche al `Location` dei redirect (già coperto perché il loop
  ri-entra dal controllo).
- Test: nuovo test in `tests/test_security.py` con seed `file:///etc/passwd` e
  redirect `Location: ftp://…` → `fetch_error` senza tentativi di connessione.
- Completamento: test verdi; comportamento identico sync/async.

**Step 1.3 — (Opzionale, design) Pin DNS anti-rebinding (A1) — Effort L**
- Cosa: nel client sync, risolvere l'host con `getaddrinfo`, validare gli IP,
  poi connettersi all'IP validato passando `Host` header + SNI (richiede un
  `HTTPAdapter` custom o il passaggio a `urllib3` diretto). Per aiohttp:
  `aiohttp.TCPConnector(resolver=...)` con resolver che valida e cachea.
- Completamento: test con resolver fake che cambia risposta tra check e connect
  → richiesta bloccata. Se l'effort non è giustificato, chiudere lo step
  documentando in README un esempio concreto di egress-control (S).

### Fase 2 — Correttezza e parità sync/async

**Step 2.1 — Validare `content`/`links` nel sync (M1) — Effort S**
- Cosa: in `crawler.py:crawl_many` (dopo riga 231) replicare il blocco di
  validazione dell'async (`async_crawler.py:626-631`) con i valori ammessi
  `("pure", "ml", "smart")`, `raise ValueError` altrimenti.
- Test: `tests/test_crawler.py::test_invalid_mode_raises` (nuovo).
- Completamento: `crawl(url, mode="typo")` → `ValueError` immediato.

**Step 2.2 — Status reale dal browser o marcatura esplicita (M2) — Effort M**
- Cosa: in `browser.py:_render_sync` catturare `response = page.goto(...)` e
  ritornare `(html, response.status if response else None)`; in
  `http.py:719-723` propagare lo status e trattare ≥400 come le fetch normali
  (terminale, niente cache `done`).
- Test: fake renderer in `tests/test_browser.py` che ritorna status 404 →
  `PageResult.status == "fetch_error"` e nessuna riga `done` in DB.
- Completamento: una pagina 404 renderizzata non entra più in cache come `done`.

**Step 2.3 — Propagare `session_id` all'engine gemini (M4) — Effort S**
- Cosa: `search.py:419-420` → `self._run_gemini(query, topic, content_mode,
  session_id)`; in `_run_gemini` (riga ~637) usare
  `sid = session_id or self.crawler._default_session_id(...)`.
- Test: estendere `tests/test_search.py` con Agent fake: il `session_id`
  passato compare in `sessions` e l'edge punta alla pagina sintetica.
- Completamento: `get_session_pages(sid)` non è più vuoto con engine gemini.

**Step 2.4 — Allineare il fallback testo async (B1, parte comportamentale) — Effort S**
- Cosa: in `async_crawler.py:347-361` sostituire il fallback regex locale con
  `from .http import html_to_text_basic` (stessa funzione del sync).
- Test: stesso HTML senza trafilatura → stesso `text` e stesso `content_hash`
  su entrambi i motori (nuovo test in `tests/test_async_ml.py`).
- Completamento: dedup livello 2 coerente cross-engine.

**Step 2.5 — Cap sulle emissioni non conteggiate (M6) — Effort S**
- Cosa: in `_pipeline.py:_emit` (riga ~814) introdurre un limite (es.
  `len(st.results) >= cfg.max_pages * 10` → drop con log) o un contatore
  separato `errors_kept` configurabile.
- Completamento: crawl con 10k URL bloccati non supera il bound; test dedicato.

**Step 2.6 — Edge `max_retries=0` (B6) — Effort S**
- Cosa: in `HTTPClient.fetch` e `_AsyncHTTPClient.fetch` usare
  `attempts = max(1, cfg.max_retries)` (o validare in `HTTPConfig.__post_init__`).
- Completamento: `HTTPConfig(max_retries=0)` esegue 1 tentativo; test unitario.

### Fase 3 — Test e CI

**Step 3.1 — Coverage `llm.py` 35%→≥75% — Effort M**
- Cosa: fixture "envelope fake" (classi con `ok/payload/text()/error`) per
  coprire `extract_content` (rami errore/payload sbagliato, `llm.py:204-228`),
  `summarize_large` (chunking, partial failure, righe 319-390),
  `enrich_artifact` (165-191). Nessuna rete/LazyBridge necessaria: monkeypatch
  di `_Agent`.
- Completamento: `pytest --cov=lazycrawler.llm` ≥75%.

**Step 3.2 — Coverage `pdf.py` 54%→≥75% — Effort M**
- Cosa: generare in-test un PDF minimo (bytes hardcoded o via pymupdf, già
  installato in CI extra `pdf`) per coprire `_extract_with_pymupdf/_pypdf`,
  `extract_pdf_artifacts`, `_normalize_pdf_date` edge.
- Completamento: coverage target raggiunto, suite ancora offline.

**Step 3.3 — Coverage `browser.py` 34%→≥60% — Effort M**
- Cosa: modulo `playwright.sync_api` fake iniettato in `sys.modules` per
  esercitare `_ensure_session`, crash-recovery (`_render_sync` righe 130-152),
  `close()`.
- Completamento: target raggiunto; nessun download Chromium in CI.

**Step 3.4 — Gate di coverage in CI — Effort S**
- Cosa: in `.github/workflows/ci.yml:53` aggiungere `--cov-fail-under=72`
  (baseline attuale 73%), da alzare a fine Fase 3.
- Completamento: CI rossa se la coverage regredisce.

### Fase 4 — Documentazione e igiene

**Step 4.1 — Correggere docstring ml (B2) — Effort S**
- File: `lazycrawler/ml.py:11-14` (rimuovere "currently returns clean text
  only"), `config.py:362-364` (rimuovere "Reserved ... later phase").
- Completamento: docstring descrivono la feature reale.

**Step 4.2 — Aggiornare ANALYSIS.md e ROADMAP.md (B3, A2-tracking) — Effort S**
- Cosa: in ANALYSIS.md §3 sostituire i nomi dei file di test con
  `pytest -m "not integration"` reale (221 passed) o marcare il documento come
  storico con data; in ROADMAP.md aggiungere sotto "Next": migrazione fallback
  PDF (Step 1.1), sitemap seeding, autothrottle/proxy.
- Completamento: ogni claim "tracked in ROADMAP" ha un item corrispondente.

**Step 4.3 — Versione unica (B7) — Effort S**
- Cosa: in `config.py:259` costruire lo UA da `importlib.metadata.version
  ("lazycrawler")` (con fallback statico) oppure importare `__version__`
  evitando cicli (definire la versione in `_version.py`).
- Completamento: bump di versione = 1 solo file.

**Step 4.4 — Esempio portabile (B8) — Effort S**
- Cosa: in `examples/basic_usage.py` sostituire il path Windows con una env var
  (`LAZY_ECOSYSTEM_ROOT`) e usare `HTTPConfig()` di default con commento su
  `ca_bundle` (non `verify_ssl=False`).
- Completamento: esempio eseguibile su qualunque OS senza modifiche.

**Step 4.5 — Documentare il cache-enrich non ricorsivo (B4) — Effort S**
- Cosa: nota in `docs/guides/database.md` (sezione cache/TTL) e nel docstring di
  `CrawlerConfig.recurse_from_cache` (`config.py:85-89`): l'arricchimento
  pure→ml/smart da cache non alimenta la frontier. In alternativa (M):
  implementare il recurse riusando `row.get("links")` come nel ramo
  `_satisfies` (`_pipeline.py:501-509`).
- Completamento: comportamento documentato o allineato, con test.

### Fase 5 — Refactoring strutturale (dopo le fasi 1-4)

**Step 5.1 — De-duplicare sync/async (B1) — Effort L**
- Cosa: estrarre in un modulo `_netcore.py` le parti pure e condivisibili:
  parsing robots + calcolo crawl-delay (oggi in `http.py:938-961` e
  `async_crawler.py:455-472`), calcolo del delay effettivo del rate limiter,
  decisione PDF/charset/decode (oggi duplicata in `http.py:679-690,740-751` e
  `async_crawler.py:304-341`). I due client restano separati ma chiamano le
  stesse funzioni.
- Completamento: le regole di parity hanno una sola implementazione; suite
  verde; diff di comportamento zero (verificare con i test di parity esistenti
  in `tests/test_async_ml.py` e `tests/test_audit_fixes.py`).

**Step 5.2 — Tipizzare gli interni della pipeline (B10) — Effort M**
- Cosa: sostituire `st: Any`/`res: Any`/`fr: Any` con `_State`/`_Res`/
  `FetchResult` (import sotto `TYPE_CHECKING` per evitare cicli); aggiungere
  `mypy --strict lazycrawler/_pipeline.py lazycrawler/crawler.py` in CI (S).
- Completamento: mypy pulito sui moduli target.

---

## 6. Esito dei test eseguiti

Ambiente: venv dedicato in
`/tmp/claude-0/.../scratchpad/venv-lazycrawler`, Python 3.11.15, install
`pip install -e ".[all]"` **dalla root del repo** (nota: `pip install -r
requirements.txt` da un'altra cwd fallisce perché il file contiene `-e .[all]`,
relativo alla cwd — primo tentativo fallito con "file:///home/user does not
appear to be a Python project", riuscito dalla root). Tutti gli extra
installati, incluso `ml` (model2vec scaricato) e `aiohttp`.

Comando: `pytest -x -q --timeout=120` (pytest 9.1.1, pytest-timeout):

```
221 passed, 1 deselected in 6.75s
```

(1 deselected = il marker `integration`, escluso di default da
`pyproject.toml:addopts`; nessuno skip: con tutti gli extra installati i 5 skip
storici di ANALYSIS §5.6 si attivano.)

Run con coverage (`pytest -q --timeout=120 --cov=lazycrawler`):

```
221 passed, 1 deselected in 8.22s — TOTAL 3640 stmts, 993 miss, 73%
browser.py 34% · llm.py 35% · pdf.py 54% · text.py 68% · http.py 69%
markdown.py 69% · ml.py 73% · async_crawler.py 74% · search.py 76%
_pipeline.py 78% · artifacts.py 79% · tools.py 82% · db.py 84% · crawler.py 87%
config/models/presets/prompts/__init__ 100%
```

Lint/format (ruff 0.x da venv): `ruff check .` → **All checks passed!**;
`ruff format --check .` → **42 files already formatted**.

Conteggio test: 222 funzioni `def test_*` distribuite in 18 file `tests/test_*.py`
(19 `.py` totali in `tests/`, incluso `conftest.py` che non contiene test);
nessun TODO/FIXME/XXX nel
codice sorgente o nei test (grep negativo). Working tree git pulito prima e
dopo l'audit (nessun file del progetto modificato oltre a questo documento).

---

## Nota di revisione (verifica adversariale)

Revisione indipendente svolta **contro il codice reale** (nessun test rieseguito;
verificata solo la coerenza interna dei numeri). Ogni issue è stata riaperta al
`file:riga` citato.

### Verificate
- **ALTA A1** (SSRF TOCTOU/DNS-rebinding): confermata. `is_blocked_address`
  (`http.py:239-292`) e `_is_blocked_async` (`async_crawler.py:113-153`) risolvono
  il DNS al check; `requests`/`aiohttp` ri-risolvono al connect. Raggiungibile sul
  percorso agente (`CrawlerTools`, guardia on di default). Citazioni corrette.
- **ALTA A2** (fallback PDF via `urllib`): la falla di codice esiste
  (`pdf.py:118-142` → `_pipeline.py:297-303`) ma la severità era **sbagliata**
  (v. Corrette).
- **MEDIE** verificate tutte e sei sul codice: M1 (`crawler.py:229-235` non valida,
  vs `async_crawler.py:626-631` che fa `ValueError`; degrada a `res.llm.extract_content`
  su ramo smart `_pipeline.py:397-401,611`), M2 (`http.py:719-722` fabbrica
  `status=200`; browser scarta la response, `browser.py:130-139`; cache TTL
  `db.py:263-279`), M3 (nessun check scheme in `http.py:692-716`/`async_crawler.py:281-345`),
  M4 (`search.py:419-420` non passa `session_id`; `search.py:637` ne genera uno
  proprio; `tools.py:264,284` restituisce all'agente il proprio `sid`), M5
  (coerenza numerica coverage: 2647/3640 ≈ 73% ✓), M6 (`_pipeline.py:804-819`
  e `async_crawler.py:915-928`, `count=False` appende senza bound).
- **BASSE** verificate a campione e oltre: B1 (divergenza fallback testo
  `async_crawler.py:347-361` collassa i newline vs `http.py:481-495`), B2
  (docstring obsolete `ml.py:11-14`/`config.py:362-364` vs estrazione completa
  `ml.py:456-471`), B3 (ANALYSIS `§3` cita file test inesistenti, `§5.6` "193
  passed"), B4 (`_pipeline.py:512-537` enrich ritorna `[]`), B5 (`http.py:938-960`
  chiave robots per host), B6 (`http.py:725` loop vuoto con `max_retries=0`),
  B7 (versione in 3 punti), B9 (`ml.py:63-82` lock rilasciato tra check e build).
- **Riferimenti `file:riga`**: verificati a campione ampio, tutti entro ±5 righe
  (per lo più esatti). Nessuna correzione di citazione necessaria.

### Confermate (invariate)
CRITICA 0; ALTA **A1**; MEDIE M1–M6; BASSE B1–B10. Il piano §5 è eseguibile: le
funzioni/righe target (`HTTPClient.fetch_bytes` `http.py:780-801`, fixture
`stub_fetch` `tests/conftest.py:~55-95`, validazione async `626-631`, `_render_sync`,
ecc.) esistono e i passi sono attuabili.

### Corrette
- **A2 declassata da ALTA a BASSA (B11)**. Motivazione dimostrata sul codice: il
  ramo `urllib` richiede `pdf_bytes` vuoto + `is_pdf`, situazione prodotta solo da
  `render_js` (`http.py:719-722`), che è **mutuamente esclusivo** con la guardia
  SSRF (`http.py:538-545`, `ValueError`); con guardia attiva il ramo è
  **irraggiungibile** → nessun bypass SSRF sfruttabile. È debito di consistenza
  (no retry/proxy), non sicurezza. L'assessment aveva contraddetto persino la
  ANALYSIS §5.4 ("residual exposure minimal"). Aggiornati: tabella voti Sicurezza,
  §3, priorità Step 1.1 (declassata da "massima" a cleanup).
- **§6 conteggio file test**: "222 funzioni in 19 file" → 222 in **18** file
  `test_*.py` (il 19° è `conftest.py`, privo di test).

### Eliminate
Nessuna. Tutte le issue elencate corrispondono a comportamento reale del codice;
nessun falso positivo da rimuovere. (A2 non è eliminata: è ridimensionata a B11.)

### Aggiunte (caccia attiva: async / cache / browser / ml)
Nessuna nuova issue CRITICA/ALTA. Riscontri della caccia: (a) il conteggio
`max_pages` in parallelo è corretto — check+incremento sono atomici sotto
`st.lock` in `_emit`/`_add_counted` (`_pipeline.py:797-819`), nessun overrun; (b)
il ramo cache "satisfies" ricorre correttamente con `recurse_from_cache`
(`_pipeline.py:500-509`), quindi B4 è correttamente circoscritto al solo ramo
`enrich`; (c) robots parsing e SSRF per-hop async in parità col sync. Nessun
difetto grave sfuggito individuato.

### Conteggi finali
| Severità | Prima | Dopo |
|---|---|---|
| CRITICA | 0 | 0 |
| ALTA | 2 (A1, A2) | **1** (A1) |
| MEDIA | 6 | 6 |
| BASSA | 10 | **11** (B11 = ex-A2) |
| **Totale** | 18 | 18 |

Voti confermati (Correttezza A-, Sicurezza B+, Test B, Doc B, Manutenibilità B+):
il declassamento di A2 non altera i voti — A1 (TOCTOU) resta il tetto della
Sicurezza; la wording della motivazione Sicurezza è stata corretta per non
contare A2 come esposizione sfruttabile.
