#!/usr/bin/env python3
"""
MyJobMag Nigeria - FULL-field CSV scraper (pages 9501 -> 10000).

Every job is appended to ng9501-10000.csv with a running "No".
Resumable: progress saved after every page in ng_state.txt.
ng_pages.csv gives a per-page count (URLs found / rows added).
"""
import os
import re
import csv
import time
import hashlib
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
BASE_URL   = "https://www.myjobmag.com"
START_PAGE = int(os.environ.get("START_PAGE", "9501"))
END_PAGE   = int(os.environ.get("END_PAGE", "10000"))

PAGES_PER_RUN   = int(os.environ.get("PAGES_PER_RUN", "15"))
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "1080"))  # stop cleanly before job timeout

OUTPUT_CSV = f"ng{START_PAGE}-{END_PAGE}.csv"
PAGES_CSV  = "ng_pages.csv"
STATE_FILE = "ng_state.txt"                      # next page to scrape

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36"
    ),
    "Accept-Charset": "utf-8",
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_TIMEOUT = 20

JOB_TYPE_MAPPING = {
    "full-time": "full-time", "full time": "full-time", "fulltime": "full-time",
    "part-time": "part-time", "part time": "part-time", "parttime": "part-time",
    "contract": "contract", "contractor": "contract", "contracting": "contract",
    "temporary": "temporary", "temp": "temporary",
    "freelance": "freelance",
    "internship": "internship", "intern": "internship",
    "volunteer": "volunteer",
}

CSV_COLUMNS = [
    "No", "Page", "Position On Page",
    "Job Title", "Job Description", "Job Type", "Job Type (Normalised)",
    "Location", "Job Field", "Qualification", "Experience", "Salary",
    "Date Posted", "Deadline", "Estimated Deadline", "Application",
    "Company Name", "Company URL", "Company Logo", "Company Industry",
    "Company Founded", "Company Type", "Company Website", "Company Address",
    "Company Details",
    "Job URL", "Job ID", "Scraped At",
]

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
_fh = logging.FileHandler("debug.log")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_ch)

# Company pages are cached per run so the same company isn't fetched 40 times
_COMPANY_CACHE = {}


# ════════════════════════════════════════════════════════════════════════════
# SANITIZATION / HELPERS
# ════════════════════════════════════════════════════════════════════════════
_MOJIBAKE = [
    ("Â", ""), ("â€™", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€", '"'),
    ("â€¢", "•"), ("Ã©", "é"), ("Ã ", "à"), ("Ã¨", "è"), ("Ã¯", "ï"),
    ("\u00a0", " "), ("\u200b", ""), ("\ufeff", ""),
]


def sanitize(value: str) -> str:
    if not isinstance(value, str):
        return value
    text = value
    for pattern, repl in _MOJIBAKE:
        text = text.replace(pattern, repl)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = re.sub(r"^N/A$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bN/A\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalise_job_type(raw: str) -> str:
    return JOB_TYPE_MAPPING.get((raw or "").lower().strip(), "full-time")


def add_three_months(posted_date: datetime) -> str:
    month = posted_date.month - 1 + 3
    year = posted_date.year + month // 12
    month = month % 12 + 1
    day = min(posted_date.day, 28)
    return datetime(year, month, day).strftime("%Y-%m-%d")


def make_job_id(job_url: str) -> str:
    return hashlib.md5(job_url.encode()).hexdigest()[:16]


def absolute(href: str) -> str:
    return BASE_URL + href if href.startswith("/") else href


# ════════════════════════════════════════════════════════════════════════════
# CSV / STATE
# ════════════════════════════════════════════════════════════════════════════
def _init_csv(path: str, columns: list):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(columns)


def _archive(path: str):
    """Rename an incompatible old file out of the way instead of deleting it."""
    if not os.path.exists(path):
        return
    base, ext = os.path.splitext(path)
    target = f"{base}.old{ext}"
    if os.path.exists(target):
        target = f"{base}.old-{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    os.replace(path, target)
    logger.warning(f"📦 Archived old file {path} → {target}")


def load_existing():
    """Returns (set of job IDs already saved, last 'No' used, migrated_flag)."""
    migrated = False
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        if header != CSV_COLUMNS:
            _archive(OUTPUT_CSV)
            _archive(PAGES_CSV)
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
            migrated = True

    _init_csv(OUTPUT_CSV, CSV_COLUMNS)
    ids, last_no = set(), 0
    with open(OUTPUT_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ids.add(row["Job ID"])
            try:
                last_no = max(last_no, int(row["No"]))
            except (ValueError, KeyError):
                pass
    return ids, last_no, migrated


def append_row(row: list):
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(row)


def append_page_summary(page: int, urls_found: int, rows_added: int):
    _init_csv(PAGES_CSV, ["Page", "Job URLs Found", "Rows Added", "Timestamp"])
    with open(PAGES_CSV, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow([page, urls_found, rows_added, datetime.now().isoformat(timespec="seconds")])


def load_next_page() -> int:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return int(f.read().strip())
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read {STATE_FILE} ({e}) — starting at {START_PAGE}.")
    return START_PAGE


def save_next_page(page: int):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(page))


# ════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ════════════════════════════════════════════════════════════════════════════
def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.encoding = "utf-8"
    logger.debug(f"GET {url} -> {resp.status_code} ({len(resp.text)} chars)")
    return BeautifulSoup(resp.text, "html.parser")


def text_of(soup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""


# ════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ════════════════════════════════════════════════════════════════════════════
def scrape_job_list_page(page_num: int):
    """Returns list of job URLs, or None if the page could not be fetched
    (so the caller does NOT advance past it)."""
    url = f"{BASE_URL}/page/{page_num}"
    logger.info(f"Fetching page {page_num}: {url}")
    soup = None
    for attempt in range(3):
        try:
            soup = get_soup(url)
            break
        except Exception as e:
            logger.error(f"Page {page_num} attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)
    if soup is None:
        return None

    urls = []
    for a in soup.select("li.mag-b > h2 > a"):
        href = a.get("href")
        if href:
            urls.append(absolute(href))
    logger.info(f"Found {len(urls)} job URLs on page {page_num}")
    return urls


def scrape_company_details(company_url: str) -> dict:
    company = {
        "name": "", "logo": "", "industry": "", "founded": "",
        "type": "", "website": "", "address": "", "details": "",
    }
    if not company_url:
        return company
    if company_url in _COMPANY_CACHE:
        return dict(_COMPANY_CACHE[company_url])
    try:
        soup = get_soup(company_url)
        company["name"] = text_of(soup, "#wrap-comp-jobs > div.company-jobs > h1").replace("Recruitment", "").strip()
        logo_el = soup.select_one("#wrap-comp-jobs > div.company-jobs > div.company-logo > img")
        if logo_el and logo_el.get("src"):
            company["logo"] = absolute(logo_el["src"])
        base = "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type({}) > span.comp-info-desc"
        company["industry"] = text_of(soup, base.format(1))
        company["founded"]  = text_of(soup, base.format(2))
        company["type"]     = text_of(soup, base.format(3))
        company["website"]  = text_of(soup, base.format(4))
        company["address"]  = text_of(soup, base.format(5))
        company["details"]  = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.mag-b.fl-r.ts-13.tc-b6.bm-b-35")
        logger.info(f"Company scraped: {company['name']}")
    except Exception as e:
        logger.error(f"Company fetch failed for {company_url}: {e}")

    if not company["details"] and company["name"]:
        company["details"] = search_company_details_fallback(company["name"])

    _COMPANY_CACHE[company_url] = dict(company)
    return company


def _extract_date_pair(soup) -> tuple:
    posted_str, deadline_str = "", ""
    date_lis = soup.select("div.read-date-sec div.read-date-sec-li")
    if len(date_lis) >= 1:
        li = date_lis[0]
        b = li.find("b")
        if b:
            b.extract()
        posted_str = li.get_text(strip=True)
    if len(date_lis) >= 2:
        li = date_lis[1]
        b = li.find("b")
        if b:
            b.extract()
        deadline_str = li.get_text(strip=True)
    return posted_str, deadline_str


def _extract_company_url(soup) -> str:
    a = soup.select_one("li.job-industry a[href^='/jobs-at/']")
    if a and a.get("href"):
        return absolute(a["href"])
    for a in soup.select("#printable > a"):
        href = a.get("href")
        if href:
            return absolute(href)
    return ""


def _extract_application(soup) -> str:
    app_block = soup.select_one("#printable > div.mag-b.bm-b-30")
    if not app_block:
        h2 = soup.select_one("#application-method")
        app_block = h2.find_next_sibling("div") if h2 else None
    if not app_block:
        return ""

    text = app_block.get_text(" ", strip=True)
    email_match = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}", text)
    if email_match:
        return email_match.group(0)

    link = app_block.select_one("a")
    if link and link.get("href"):
        return absolute(link["href"])
    return ""


def scrape_job_details(job_url: str) -> list:
    """Returns a LIST of job dicts (old /readjob/ pages can hold several)."""
    soup = get_soup(job_url)

    job_headings = soup.select("#printable > h2.mag-b")
    if not job_headings:
        h2 = soup.select_one("h2.mag-b")
        if h2:
            job_headings = [h2]

    subjob_blocks = []
    for h2 in job_headings:
        link_el = h2.select_one("a")
        if link_el and link_el.get("href"):
            title = link_el.get_text(strip=True)
            subjob_url = absolute(link_el["href"])
        else:
            span_el = h2.select_one("span.subjob-title")
            title = span_el.get_text(strip=True) if span_el else h2.get_text(strip=True)
            subjob_url = job_url
        title = title.replace("Method of Application", "").strip()
        if not title:
            continue

        key_info_ul = h2.find_next_sibling("ul", class_="job-key-info")
        details_div = h2.find_next_sibling("div", class_="job-details")

        def kv(label, _ul=key_info_ul):
            if not _ul:
                return ""
            for li in _ul.select("li"):
                t = li.select_one("span.jkey-title")
                if t and t.get_text(strip=True) == label:
                    info = li.select_one("span.jkey-info")
                    return info.get_text(strip=True) if info else ""
            return ""

        subjob_blocks.append({
            "title": title,
            "url": subjob_url,
            "job_type": kv("Job Type"),
            "qualifications": kv("Qualification"),
            "experience": kv("Experience"),
            "location": kv("Location"),
            "field": kv("Job Field"),
            "salary": kv("Salary Range"),
            "description": details_div.get_text("\n", strip=True) if details_div else "",
        })

    if not subjob_blocks:
        logger.warning(f"No job blocks found on page — skipping: {job_url}")
        return []

    date_posted_str, deadline_raw = _extract_date_pair(soup)
    estimated_deadline = ""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            estimated_deadline = add_three_months(datetime.strptime(date_posted_str, fmt))
            break
        except ValueError:
            continue
    if not estimated_deadline:
        logger.warning(f"Unparseable date '{date_posted_str}' — keeping job, no estimated deadline: {job_url}")

    deadline = deadline_raw.strip()
    if not deadline or deadline.lower() == "not specified":
        deadline = estimated_deadline

    application = _extract_application(soup)
    company_url = _extract_company_url(soup)
    company = scrape_company_details(company_url)
    if not application:
        application = company["website"]

    jobs = []
    for blk in subjob_blocks:
        jobs.append({
            "job_title": sanitize(blk["title"]),
            "job_description": sanitize(blk["description"]),
            "job_type": sanitize(blk["job_type"]),
            "job_qualifications": sanitize(blk["qualifications"]),
            "job_experience": sanitize(blk["experience"]),
            "job_location": sanitize(blk["location"]),
            "job_field": sanitize(blk["field"]),
            "salary_range": sanitize(blk["salary"]),
            "date_posted": sanitize(date_posted_str),
            "deadline": sanitize(deadline),
            "estimated_deadline": sanitize(estimated_deadline),
            "application": sanitize(application),
            "company_url": sanitize(company_url),
            "company_name": sanitize(company["name"]),
            "company_logo": sanitize(company["logo"]),
            "company_industry": sanitize(company["industry"]),
            "company_founded": sanitize(company["founded"]),
            "company_type": sanitize(company["type"]),
            "company_website": sanitize(company["website"]),
            "company_address": sanitize(company["address"]),
            "company_details": sanitize(company["details"]),
            "job_url": sanitize(blk["url"]),
        })
    return jobs


def search_company_details_fallback(company_name: str) -> str:
    try:
        url = "https://www.google.com/search?q=" + requests.utils.quote(company_name + " company about")
        soup = get_soup(url)
        snippet = (soup.select_one("div.BNeawe") or
                   soup.select_one("span.aCOpRe") or
                   soup.select_one("div.VwiC3b"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
    except Exception as e:
        logger.error(f"Google fallback failed for {company_name}: {e}")
    try:
        slug = re.sub(r"[^a-z0-9-]", "-", company_name.lower())
        url = f"https://www.linkedin.com/company/{slug}"
        soup = get_soup(url)
        snippet = (soup.select_one("p.core-section-container__info") or
                   soup.select_one("section.summary p"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
        meta = soup.find("meta", {"name": "description"})
        if meta and meta.get("content") and len(meta["content"]) > 20:
            return meta["content"]
    except Exception as e:
        logger.error(f"LinkedIn fallback failed for {company_name}: {e}")
    return ""


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def run():
    saved_ids, job_no, migrated = load_existing()
    start_page = START_PAGE if migrated else load_next_page()
    logger.info(f"📋 {len(saved_ids)} jobs already in {OUTPUT_CSV} (last No = {job_no}).")
    if migrated:
        logger.info("♻️  Old-format CSV archived — restarting from page "
                    f"{START_PAGE} to capture all fields.")

    if start_page > END_PAGE:
        logger.info(f"🏁 All pages {START_PAGE}→{END_PAGE} already scraped. Nothing to do.")
        return

    stop_page = min(start_page + PAGES_PER_RUN - 1, END_PAGE)
    logger.info(f"📄 This run: pages {start_page} → {stop_page} (full range {START_PAGE}→{END_PAGE}).")

    added_total = skipped = failed = 0
    run_started = time.monotonic()
    last_done = start_page - 1

    for page_num in range(start_page, stop_page + 1):
        if time.monotonic() - run_started > MAX_RUN_SECONDS:
            logger.info("⏱ Time budget reached — stopping cleanly.")
            break

        job_urls = scrape_job_list_page(page_num)
        if job_urls is None:
            logger.error(f"Page {page_num} unreachable — stopping run; will retry next run.")
            break

        rows_added = 0
        for pos, job_url in enumerate(job_urls, start=1):
            logger.info(f"── Page {page_num} | Job {pos}/{len(job_urls)}: {job_url}")

            # Cheap pre-check: skip the network fetch if already saved
            if make_job_id(job_url) in saved_ids:
                skipped += 1
                continue

            try:
                subjobs = scrape_job_details(job_url)
            except Exception as e:
                logger.error(f"Error scraping {job_url}: {e}")
                failed += 1
                continue

            for job in subjobs:
                job_id = make_job_id(job["job_url"])
                if job_id in saved_ids:
                    skipped += 1
                    continue
                job_no += 1
                append_row([
                    job_no, page_num, pos,
                    job["job_title"], job["job_description"], job["job_type"],
                    normalise_job_type(job["job_type"]),
                    job["job_location"], job["job_field"], job["job_qualifications"],
                    job["job_experience"], job["salary_range"],
                    job["date_posted"], job["deadline"], job["estimated_deadline"],
                    job["application"],
                    job["company_name"], job["company_url"], job["company_logo"],
                    job["company_industry"], job["company_founded"], job["company_type"],
                    job["company_website"], job["company_address"], job["company_details"],
                    job["job_url"], job_id, datetime.now().isoformat(timespec="seconds"),
                ])
                saved_ids.add(job_id)
                rows_added += 1
                added_total += 1
                logger.info(f"✅ #{job_no} saved: {job['job_title']}")

            time.sleep(1)  # be polite

        append_page_summary(page_num, len(job_urls), rows_added)
        last_done = page_num
        save_next_page(page_num + 1)   # progress saved after EVERY page

    logger.info(f"\n{'#' * 60}")
    logger.info(f" RUN COMPLETE ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    logger.info(f" 📄 Pages this run : {start_page} → {last_done}")
    logger.info(f" ▶️  Resumes next at: {last_done + 1}" + ("  (ALL DONE)" if last_done >= END_PAGE else ""))
    logger.info(f" ✅ Rows added : {added_total}   (total No. so far: {job_no})")
    logger.info(f" ⏭ Skipped    : {skipped}")
    logger.info(f" ❌ Failed     : {failed}")
    logger.info(f"{'#' * 60}")


if __name__ == "__main__":
    logger.info("🚀 MyJobMag Nigeria full-field CSV scraper starting…")
    run()
    logger.info("✅ Done.")
