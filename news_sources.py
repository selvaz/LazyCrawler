# -*- coding: utf-8 -*-
"""Curated source list for the news-monitor pipeline (financial + geopolitical).

Every feed URL below was checked reachable from this VPS on 2026-07-23 (many
obvious candidates -- Politico, NYT, Britannica, IMF, most Reuters RSS, US
Treasury, BIS, CFR -- return 403/404 here and are excluded).

``mode`` picks the LazyCrawler extraction path per source:

- "ml"    -- no LLM (TextRank summary, YAKE topics, spaCy NER, VADER
             sentiment). Used for every English-language source: zero
             token cost, and the NLP stack itself is English-tuned.
- "smart" -- LLM extraction (DeepSeek). Used for foreign-language local
             sources, where the English-tuned ml pipeline would degrade
             badly; DeepSeek reads the native language directly instead of
             requiring a separate model per language.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsSource:
    name: str
    url: str
    category: str  # "financial" | "central_bank" | "geopolitical"
    region: str     # "global" | "us" | "europe" | "asia" | "africa" | "latam" | "mena"
    lang: str       # ISO 639-1
    mode: str       # "ml" | "smart"


SOURCES: list[NewsSource] = [
    # -- Financial wires (ml, en) ---------------------------------------
    # Tried and dropped (checked live 2026-07-23, full-source validation run):
    # MarketWatch (both feeds) and Folha (both feeds) 100% "Disallowed by
    # robots.txt" -- honored, not overridden. Investing.com and Seeking
    # Alpha's article pages 403 every fetch (feed works, articles don't).
    # Yahoo Finance's article links all redirect to a GDPR consent-wall page
    # (consent.yahoo.com) from this VPS's apparent geo-IP -- zero real content.
    NewsSource("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "financial", "us", "en", "ml"),
    NewsSource("CNBC Economy", "https://www.cnbc.com/id/10000664/device/rss/rss.html", "financial", "us", "en", "ml"),
    NewsSource("ForexLive", "https://www.forexlive.com/feed/", "financial", "global", "en", "ml"),
    NewsSource("Benzinga", "https://www.benzinga.com/feed", "financial", "us", "en", "ml"),

    # -- Central banks (ml, en) -------------------------------------------
    NewsSource("Federal Reserve Press Releases", "https://www.federalreserve.gov/feeds/press_all.xml", "central_bank", "us", "en", "ml"),
    NewsSource("ECB Press Releases", "https://www.ecb.europa.eu/rss/press.xml", "central_bank", "europe", "en", "ml"),

    # -- Geopolitical, major outlets (ml, en) ------------------------------
    NewsSource("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "geopolitical", "global", "en", "ml"),
    NewsSource("Al Jazeera (English)", "https://www.aljazeera.com/xml/rss/all.xml", "geopolitical", "mena", "en", "ml"),
    NewsSource("The Guardian World", "https://www.theguardian.com/world/rss", "geopolitical", "global", "en", "ml"),
    NewsSource("NPR World", "https://feeds.npr.org/1004/rss.xml", "geopolitical", "us", "en", "ml"),
    NewsSource("Deutsche Welle World", "https://rss.dw.com/rdf/rss-en-world", "geopolitical", "europe", "en", "ml"),

    # -- Regional/local, English-language (ml) -----------------------------
    NewsSource("South China Morning Post - China", "https://www.scmp.com/rss/91/feed", "geopolitical", "asia", "en", "ml"),
    NewsSource("South China Morning Post - Asia", "https://www.scmp.com/rss/318208/feed", "geopolitical", "asia", "en", "ml"),
    NewsSource("South China Morning Post - Business", "https://www.scmp.com/rss/92/feed", "financial", "asia", "en", "ml"),
    # Tried and dropped: Xinhua World RSS is dead (items dated 2017-2018),
    # China Daily's RSS serves stale/duplicate items with no pubDate, Caixin
    # Global's RSS host doesn't resolve, Economic Times' RSS returns a valid
    # but item-less feed shell -- all checked live from this VPS on
    # 2026-07-23. SCMP remains the only fresh, working China-adjacent source.
    NewsSource("Times of India - World", "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms", "geopolitical", "asia", "en", "ml"),
    NewsSource("Times of India - Business", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms", "financial", "asia", "en", "ml"),
    NewsSource("LiveMint - Economy", "https://www.livemint.com/rss/economy", "financial", "asia", "en", "ml"),
    NewsSource("Hindustan Times - World", "https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml", "geopolitical", "asia", "en", "ml"),
    NewsSource("AllAfrica Headlines", "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf", "geopolitical", "africa", "en", "ml"),
    NewsSource("Al-Monitor (Middle East)", "https://www.al-monitor.com/rss", "geopolitical", "mena", "en", "ml"),
    NewsSource("Middle East Eye", "https://www.middleeasteye.net/rss", "geopolitical", "mena", "en", "ml"),
    NewsSource("Rappler - Nation (Philippines)", "https://www.rappler.com/nation/feed/", "geopolitical", "asia", "en", "ml"),
    NewsSource("Rappler - World (Philippines)", "https://www.rappler.com/world/feed/", "geopolitical", "asia", "en", "ml"),
    NewsSource("Rappler - Business (Philippines)", "https://www.rappler.com/business/feed/", "financial", "asia", "en", "ml"),
    NewsSource("Daily Maverick (South Africa)", "https://www.dailymaverick.co.za/dmrss/", "geopolitical", "africa", "en", "ml"),
    NewsSource("Buenos Aires Times", "https://www.batimes.com.ar/feed", "geopolitical", "latam", "en", "ml"),

    # -- Local-language sources (smart / DeepSeek) -------------------------
    # Section-scoped feeds only (politics/economy/world), not the sitewide
    # "latest everything" firehose -- the latter mixes in celebrity/gossip/
    # sports content that has no place in a portfolio-manager news feed
    # (e.g. Clarin's old "lo-ultimo" feed once surfaced a porn-actress gossip
    # item alongside the Trump/Saudi and oil headlines).
    NewsSource("Clarin - Politica (Argentina, ES)", "https://www.clarin.com/rss/politica/", "geopolitical", "latam", "es", "smart"),
    NewsSource("Clarin - Economia (Argentina, ES)", "https://www.clarin.com/rss/economia/", "financial", "latam", "es", "smart"),
    NewsSource("Clarin - Mundo (Argentina, ES)", "https://www.clarin.com/rss/mundo/", "geopolitical", "latam", "es", "smart"),
    NewsSource("La Nacion - Economia (Argentina, ES)", "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/economia/", "financial", "latam", "es", "smart"),
    NewsSource("La Nacion - El Mundo (Argentina, ES)", "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/el-mundo/", "geopolitical", "latam", "es", "smart"),
    NewsSource("G1 - Economia (Brazil, PT)", "https://g1.globo.com/rss/g1/economia/", "financial", "latam", "pt", "smart"),
    NewsSource("InfoMoney (Brazil, PT)", "https://www.infomoney.com.br/feed/", "financial", "latam", "pt", "smart"),
    NewsSource("Al Jazeera (Arabic)", "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdff8b8cab9", "geopolitical", "mena", "ar", "smart"),
    NewsSource("NHK News (Japan, JA)", "https://www3.nhk.or.jp/rss/news/cat0.xml", "geopolitical", "asia", "ja", "smart"),
    NewsSource("RFI Afrique (FR)", "https://www.rfi.fr/fr/afrique/rss", "geopolitical", "africa", "fr", "smart"),
    NewsSource("Jeune Afrique (FR)", "https://www.jeuneafrique.com/feed/", "geopolitical", "africa", "fr", "smart"),
]
