# Custom Output Schema

By default, smart mode fills the standard `PageResult` fields (`title`, `summary`, `entities`, `topics`, `sentiment`). You can replace this with your own **Pydantic model** to extract exactly the structured data you need.

The extracted object is stored in `PageResult.data`.

---

## When to use

- Extract product catalogs (name, price, SKU, description)
- Parse job listings (title, company, salary, requirements)
- Collect news articles (headline, author, date, body)
- Pull event data (name, date, location, price)
- Any domain-specific structured extraction

---

## Defining a custom model

```python
from pydantic import BaseModel, Field

class Article(BaseModel):
    headline: str = Field(description="Article headline or title")
    author: str = Field(description="Author full name, or empty string if not found")
    published_date: str = Field(description="Publication date in ISO format, or empty string")
    body_summary: str = Field(description="2–3 sentence summary of the article body")
    category: str = Field(description="Article category (Tech, Business, Science, etc.)")
```

!!! tip
    **`Field(description=...)` is critical.** The description is what guides the LLM — be specific and explicit about what you want.

---

## Passing the schema to crawl

```python
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig

llm_cfg = LLMConfig(model="gpt-4o-mini")
crawler = WebCrawler(llm_cfg=llm_cfg)

results = crawler.crawl(
    "https://techcrunch.com",
    content="smart",   # must be smart
    schema=Article,    # your Pydantic model
)
crawler.close()
```

---

## Accessing extracted data

`PageResult.data` is a `dict` with the field values:

```python
for r in results:
    if r.data:
        article = Article(**r.data)
        print(f"{article.headline}")
        print(f"  By: {article.author} ({article.published_date})")
        print(f"  Category: {article.category}")
        print(f"  {article.body_summary}")
```

If LLM extraction failed, `r.data` is `None` and `r.status` is `"llm_error"`.

---

## Example 1: product catalog

```python
from pydantic import BaseModel, Field
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig, CrawlerConfig

class Product(BaseModel):
    name: str = Field(description="Product name or title")
    price: str = Field(description="Price with currency symbol (e.g. '$29.99'), or empty if not found")
    sku: str = Field(description="Product SKU or model number, or empty string")
    description: str = Field(description="Short product description, 1–2 sentences")
    in_stock: bool = Field(description="True if the product appears to be in stock")
    rating: str = Field(description="Customer rating (e.g. '4.5/5'), or empty string")

llm_cfg = LLMConfig(model="gpt-4o-mini")
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=2, max_pages=50),
    llm_cfg=llm_cfg,
)
results = crawler.crawl("https://shop.example.com/products/", content="smart", schema=Product)
crawler.close()

products = []
for r in results:
    if r.data and r.status == "done":
        products.append(Product(**r.data))

# Export to CSV
import csv
with open("products.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["name", "price", "sku", "in_stock", "rating"])
    for p in products:
        w.writerow([p.name, p.price, p.sku, p.in_stock, p.rating])
```

---

## Example 2: job listings

```python
from pydantic import BaseModel, Field
from typing import List

class JobListing(BaseModel):
    title: str = Field(description="Job title")
    company: str = Field(description="Company name")
    location: str = Field(description="Job location (city, country, or 'Remote')")
    salary: str = Field(description="Salary range or compensation info, or empty string")
    employment_type: str = Field(description="Full-time, Part-time, Contract, or Internship")
    experience_level: str = Field(description="Junior, Mid-level, Senior, or Lead")
    skills: List[str] = Field(description="Required technical skills (list of strings)")
    summary: str = Field(description="2-sentence job summary")

crawler = WebCrawler(llm_cfg=LLMConfig(model="gpt-4o-mini"))
results = crawler.crawl("https://jobs.example.com/", content="smart", schema=JobListing)
crawler.close()

for r in results:
    if r.data:
        job = JobListing(**r.data)
        print(f"{job.title} @ {job.company} ({job.location})")
        print(f"  {job.employment_type} | {job.experience_level} | {job.salary}")
        print(f"  Skills: {', '.join(job.skills[:5])}")
```

---

## Example 3: news articles with date

```python
from pydantic import BaseModel, Field
from typing import List

class NewsArticle(BaseModel):
    headline: str = Field(description="News article headline")
    subheadline: str = Field(description="Subtitle or lead paragraph, or empty string")
    author: str = Field(description="Author name(s), or 'Staff' if unnamed")
    published_date: str = Field(description="Publication date in ISO format YYYY-MM-DD, or empty")
    section: str = Field(description="News section: Politics, Economy, Science, Tech, Sport, World, etc.")
    key_facts: List[str] = Field(description="3–5 key factual claims from the article")
    sentiment: str = Field(description="'positive', 'neutral', or 'negative' tone of the article")

crawler = WebCrawler(llm_cfg=LLMConfig(model="claude-haiku-4-5"))
results = crawler.crawl("https://bbc.com/news", content="smart", schema=NewsArticle)
crawler.close()

for r in results:
    if r.data:
        a = NewsArticle(**r.data)
        print(f"[{a.section}] {a.headline} ({a.published_date})")
        for fact in a.key_facts:
            print(f"  - {fact}")
```

---

## Custom schema with DB

Custom-extracted data is stored in `extract_json` column of the `pages` table:

```python
db = CrawlerDB(DBConfig(db_path="products.db"))
crawler = WebCrawler(llm_cfg=llm_cfg, db=db)
results = crawler.crawl("https://shop.example.com", content="smart", schema=Product)

# Retrieve later
import json, sqlite3
with sqlite3.connect("products.db") as con:
    rows = con.execute("SELECT url, extract_json FROM pages WHERE extract_json IS NOT NULL").fetchall()
    for url, json_str in rows:
        data = json.loads(json_str)
        print(url, data.get("name"), data.get("price"))
```

---

## Tips

- **Optional fields**: use `str` with `Field(default="")` instead of `Optional[str]` — LLMs are more reliable filling empty strings than returning `null`
- **Lists**: always provide `Field(description=...)` explaining what elements to include
- **Booleans**: add examples in the description: `"True if in stock, False if 'sold out' or 'out of stock' appears"`
- **Numbers as strings**: use `str` for prices, ratings — LLMs handle formatting inconsistencies better
