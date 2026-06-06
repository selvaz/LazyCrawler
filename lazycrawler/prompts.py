# -*- coding: utf-8 -*-
"""
lazycrawler.prompts
===================
LLM prompts for SMART mode. Used only when ``mode="smart"``.

These are GENERIC, domain-agnostic prompts: the crawler works on any kind of
web content, not just news or finance.

Structured-output note:
LazyBridge enforces the output shape via ``output=<PydanticModel>``, so these
prompts do NOT describe a JSON schema — they focus on the *task* (what to keep,
what to remove). The shape is guaranteed by the Pydantic model.
"""

from __future__ import annotations

# =============================================================================
# 1. CONTENT EXTRACTION — web page  (-> PageExtract)
# =============================================================================
# Used by: CrawlerLLM.extract_content()
# Output:  PageExtract(title, summary, clean_text, entities, topics)

CONTENT_EXTRACTION_SYSTEM = (
    "You are a content extractor for web pages.\n"
    "Given the raw text of a page, produce a clean, structured version of its\n"
    "main content.\n"
    "\n"
    "REMOVE entirely:\n"
    "- navigation menus, breadcrumbs, page headers and footers\n"
    "- sidebars, widgets, ad banners and sponsored content\n"
    "- 'related articles' / 'you may also like' blocks\n"
    "- cookie notices, newsletter signup forms, user comments\n"
    "- any non-content boilerplate\n"
    "\n"
    "KEEP: headings, main body text, facts, figures, dates, quotes, lists.\n"
    "\n"
    "Fields to produce:\n"
    "- title: the page or article title\n"
    "- summary: a concise summary (1-3 sentences)\n"
    "- clean_text: the full cleaned main content (no markdown). For an index or\n"
    "  listing page with multiple items, concatenate the available titles and\n"
    "  blurbs.\n"
    "- entities: key named entities (people, organizations, places, products...)\n"
    "- topics: the main topics or themes of the page\n"
    "- sentiment: the overall tone of the content - exactly one of\n"
    "  'negative', 'neutral', or 'positive' (use 'neutral' for factual/mixed).\n"
    "- notes: leave EMPTY by default. Reserved for research tags/annotations the\n"
    "  caller may request; only fill it if the task explicitly asks for it.\n"
    "\n"
    "Do not invent information that is not present in the text. If the page has no\n"
    "real content, leave clean_text empty."
)


# =============================================================================
# 1b. CUSTOM-SCHEMA EXTRACTION  (-> user-provided Pydantic model)
# =============================================================================
# Used by: CrawlerLLM.extract_content(..., schema=MyModel)
# The field descriptions of the user's model guide what to extract; this prompt
# only sets the general task and cleaning rules.

CUSTOM_EXTRACTION_SYSTEM = (
    "You extract structured information from a web page.\n"
    "Fill every field of the requested schema using only information present in\n"
    "the page text. Ignore navigation, ads, cookie notices, and other boilerplate.\n"
    "Do not invent data: if a field is not present, leave it empty / null."
)


# =============================================================================
# 2. LINK SELECTION — web page  (-> LinkSelection)
# =============================================================================
# Used by: CrawlerLLM.build_link_selector() / select_links()
# Output:  LinkSelection(indices=[...])  1-based indices of relevant links


def build_link_selection_system(topic: str, max_links: int) -> str:
    """
    System prompt for selecting links relevant to a topic.

    Parameters
    ----------
    topic : str
        Crawl target topic (e.g. "renewable energy storage", or any subject).
    max_links : int
        Maximum number of links to select.
    """
    return (
        "You are a link-relevance classifier for a web crawler.\n"
        "Given a numbered list of links found on a page, select the ones that\n"
        f'are likely to lead to content related to: "{topic}".\n'
        "\n"
        "Prefer links to specific articles, pages, reports, analyses, or data\n"
        "sources connected to the topic. If the topic mentions a time period,\n"
        "prefer links consistent with that period.\n"
        "\n"
        "Exclude: navigation, login/register, social sharing, pagination, author\n"
        "profiles, tag/category pages, ads, unrelated topics.\n"
        "\n"
        f"Return at most {max_links} indices (1-based). When in doubt, include\n"
        "rather than exclude. Return an empty list only if truly no link is\n"
        "relevant."
    )


# =============================================================================
# 3. TOPIC EXPANSION — web search  (-> str)
# =============================================================================
# Used by: WebSearch (smart) to expand the query before crawling

TOPIC_EXPANSION_SYSTEM = (
    "You are a topic expander for a web crawler.\n"
    "Given a search query, produce a concise topic description (max 40 words)\n"
    "that will be used to judge whether crawled pages are relevant.\n"
    "Include key synonyms, related terms, and relevant entities.\n"
    "Output plain text only: no JSON, no markdown, no bullet points."
)


# =============================================================================
# 4. LARGE DOC SUMMARIZATION — map-reduce  (-> str)
# =============================================================================
# Used by: CrawlerLLM.summarize_large() for documents above the threshold

LARGE_DOC_SUMMARY_SYSTEM = (
    "You compress large documents, preserving ONLY high-information content.\n"
    "\n"
    "Extract and preserve, when present:\n"
    "- the main subject and purpose of the document\n"
    "- key facts, findings, conclusions, and decisions\n"
    "- names, dates, numbers, quantities, percentages\n"
    "- definitions, steps, requirements, specifications\n"
    "- important entities, events, and relationships\n"
    "- any materially relevant data or tables\n"
    "\n"
    "Rules: do not invent anything; ignore legal boilerplate and navigation;\n"
    "keep numbers and dates; use short bullets and compact sections; be\n"
    "information-dense, not verbose."
)
