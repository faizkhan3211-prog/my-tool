import io
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# Optional web search dependency
try:
    from ddgs import DDGS
    DDGS_AVAILABLE = True
    DDGS_IMPORT_ERROR = ""
except Exception as exc:
    DDGS = None
    DDGS_AVAILABLE = False
    DDGS_IMPORT_ERROR = str(exc)


# ============================================================
# APP CONFIG
# ============================================================

st.set_page_config(
    page_title="Lead Research & Intelligence",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {
        max-width: 1450px;
        padding-top: 1.3rem;
        padding-bottom: 3rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid rgba(128,128,128,.18);
    }
    .hero {
        padding: 1.25rem 1.4rem;
        border: 1px solid rgba(128,128,128,.22);
        border-radius: 16px;
        margin-bottom: 1.2rem;
        background: linear-gradient(
            120deg,
            rgba(40,100,180,.12),
            rgba(20,170,160,.08)
        );
    }
    .hero h1 {
        margin: 0 0 .35rem 0;
        font-size: 1.85rem;
    }
    .hero p {
        margin: 0;
        opacity: .78;
    }
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,.2);
        padding: 12px;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; LeadResearchApp/1.0; public-page research)"
)

REQUEST_TIMEOUT = 15
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_BULK_URLS = 50
MAX_RESEARCH_COMPANIES = 30
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

ROLE_PATTERN = (
    r"owner|founder|co[- ]founder|chief executive officer|ceo|president|"
    r"managing director|general manager|principal|partner|director|"
    r"operations manager|office manager|sales manager|marketing manager|"
    r"vice president|\bvp\b|head of sales|head of marketing|"
    r"business development manager|procurement manager"
)

PERSON_NAME_PATTERN = (
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
    r"(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){1,3}"
)

NON_COMPANY_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "yelp.com",
    "yellowpages.com",
    "bbb.org",
    "mapquest.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "crunchbase.com",
    "zoominfo.com",
    "apollo.io",
    "wikipedia.org",
    "reddit.com",
    "pinterest.com",
    "tiktok.com",
    "glassdoor.com",
    "indeed.com",
    "wixsite.com",
    "wordpress.com",
    "alignable.com",
    "manta.com",
    "chamberofcommerce.com",
    "opencorporates.com",
}


# ============================================================
# BASIC HELPERS
# ============================================================

def normalize_url(raw_url: str) -> str:

    value = (raw_url or "").strip()

    if not value:
        raise ValueError("Empty URL")

    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value

    parsed = urlsplit(value)

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid URL: {raw_url}")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not accepted.")

    if not parsed.path:
        parsed = parsed._replace(path="/")

    return urlunsplit(parsed)


def validate_public_url(url: str) -> None:

    parsed = urlsplit(url)
    host = parsed.hostname

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP/HTTPS URLs are allowed.")

    if (
        host.lower() in {"localhost", "localhost.localdomain"}
        or host.lower().endswith(".local")
    ):
        raise ValueError("Local network URLs are blocked.")

    try:
        records = socket.getaddrinfo(
            host,
            parsed.port or (
                443 if parsed.scheme.lower() == "https" else 80
            ),
            type=socket.SOCK_STREAM,
        )

    except (socket.gaierror, ValueError, OSError) as exc:
        raise ValueError(f"Could not resolve website: {exc}") from exc

    addresses = {record[4][0] for record in records}

    if not addresses:
        raise ValueError("Website hostname did not resolve.")

    for address in addresses:

        ip = ipaddress.ip_address(
            address.split("%", 1)[0]
        )

        if not ip.is_global:
            raise ValueError(
                "Private, reserved or local network addresses are blocked."
            )


def fetch_html(url: str):
    """Fetch public HTML and validate each redirect destination."""

    current = url

    redirect_codes = {301, 302, 303, 307, 308}

    for _ in range(6):

        validate_public_url(current)

        response = requests.get(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )

        if response.status_code in redirect_codes:

            location = response.headers.get("Location")

            response.close()

            if not location:
                raise ValueError("Redirect destination is missing.")

            current = urljoin(current, location)
            continue

        try:

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if "html" not in content_type and "xhtml" not in content_type:

                return (
                    None,
                    response.url,
                    response.status_code,
                    content_type,
                )

            body = bytearray()

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if chunk:
                    body.extend(chunk)

                if len(body) > MAX_PAGE_BYTES:
                    raise ValueError(
                        "Page exceeds the 4 MB size limit."
                    )

            html = bytes(body).decode(
                response.encoding or "utf-8",
                errors="replace",
            )

            return (
                html,
                response.url,
                response.status_code,
                content_type,
            )

        finally:
            response.close()

    raise ValueError("Too many redirects.")


# ============================================================
# WEB SEARCH - PYTHON ONLY
# ============================================================

def search_web(
    query: str,
    max_results: int = 6,
    region: str = "us-en",
    backend: str = "auto",
):

    if not DDGS_AVAILABLE:
        return [], f"Search library is unavailable: {DDGS_IMPORT_ERROR}"

    try:

        items = DDGS(timeout=15).text(
            query=query,
            region=region,
            safesearch="moderate",
            max_results=max_results,
            backend=backend,
        )

        results = []

        for item in items:

            results.append({
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            })

        return results, ""

    except Exception as exc:
        return [], str(exc)


# ============================================================
# PERSON IDENTIFICATION - PYTHON RULE BASED
# ============================================================

def add_person(
    rows,
    name,
    title,
    source_url,
    evidence,
    confidence="Needs source review",
):

    name = re.sub(
        r"\s+",
        " ",
        str(name or "")
    ).strip(" ,|–—-\t")

    title = re.sub(
        r"\s+",
        " ",
        str(title or "")
    ).strip(" ,|–—-\t")

    if not name or not title or len(name.split()) < 2:
        return

    if len(name) > 75 or len(title) > 100:
        return

    if not re.fullmatch(PERSON_NAME_PATTERN, name):
        return

    if not re.search(ROLE_PATTERN, title, flags=re.I):
        return

    rows.append({
        "Full Name": name,
        "Job Title": title,
        "Source URL": source_url,
        "Evidence": evidence,
        "Confidence": confidence,
    })


def extract_people_from_text(text: str, source_url: str):

    """Extract explicit name/title pairs without using AI."""

    found = []

    if not text:
        return found

    compact = re.sub(r"\s+", " ", text)

    name = PERSON_NAME_PATTERN

    role = rf"(?i:{ROLE_PATTERN})"

    patterns = [

        rf"(?P<name>{name})\s*[,|–—-]\s*(?P<role>{role})\b",

        rf"(?P<role>{role})\s*(?:at|of|:|[-–—])\s*(?P<name>{name})(?=\s|[,.;|]|$)",

        rf"(?i:our|the)\s+(?P<role>{role})\s+(?:is|:|-)\s+(?P<name>{name})(?=\s|[,.;|]|$)",

    ]

    for pattern in patterns:

        for match in re.finditer(pattern, compact):

            add_person(
                found,
                match.group("name"),
                match.group("role"),
                source_url,
                "The public page text contains the name and job title together.",
            )

    return found


def extract_jsonld_people(
    soup: BeautifulSoup,
    source_url: str,
):

    found = []

    def walk(node):

        if isinstance(node, dict):

            raw_type = node.get("@type", "")

            if isinstance(raw_type, list):
                types_found = {str(x).lower() for x in raw_type}
            else:
                types_found = {str(raw_type).lower()}

            if "person" in types_found:

                source = (
                    node.get("url")
                    or node.get("sameAs")
                    or source_url
                )

                if isinstance(source, list):
                    source = source[0] if source else source_url

                add_person(
                    found,
                    node.get("name", ""),
                    node.get("jobTitle", "") or node.get("jobtitle", ""),
                    str(source),
                    "Name and jobTitle found in Schema.org JSON-LD on the page.",
                    "Structured website data",
                )

            for value in node.values():
                walk(value)

        elif isinstance(node, list):

            for value in node:
                walk(value)

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):

        try:

            walk(json.loads(
                script.string or script.get_text()
            ))

        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return found


def extract_people_from_dom(
    soup: BeautifulSoup,
    source_url: str,
):

    found = extract_jsonld_people(
        soup,
        source_url
    )

    for tag in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "strong", "b"]
    ):

        heading = tag.get_text(
            " ",
            strip=True
        )

        if not heading or len(heading) > 85:
            continue

        nearby = []

        sibling = tag.find_next_sibling()

        if sibling:
            nearby.append(
                sibling.get_text(" ", strip=True)
            )

        if tag.parent:
            nearby.append(
                tag.parent.get_text(" ", strip=True)[:350]
            )

        role_match = re.search(
            ROLE_PATTERN,
            " ".join(nearby),
            flags=re.I,
        )

        if role_match:

            add_person(
                found,
                heading,
                role_match.group(0),
                source_url,
                "Name heading and job title appear in the same public page section.",
                "Team-page candidate",
            )

    return found


def dedupe_people(rows, company=""):

    output = []
    seen = set()

    for row in rows:

        name = str(
            row.get("Full Name", "")
        ).strip()

        title = str(
            row.get("Job Title", "")
        ).strip()

        source = str(
            row.get("Source URL", "")
        ).strip()

        row_company = str(
            row.get("Company", company)
        ).strip()

        if not name or not title:
            continue

        key = (
            row_company.casefold(),
            name.casefold(),
            title.casefold(),
            source.rstrip("/").casefold(),
        )

        if key in seen:
            continue

        seen.add(key)

        output.append({

            "Company": row_company,

            "Full Name": name,

            "Job Title": title,

            "Source URL": source,

            "Evidence": str(
                row.get("Evidence", "")
            ),

            "Confidence": str(
                row.get("Confidence", "Needs source review")
            ),

        })

    return output


# ============================================================
# WEBSITE PAGE EXTRACTION
# ============================================================

def extract_page(url: str):

    html, final_url, status, content_type = fetch_html(url)

    if html is None:

        return {
            "url": final_url,
            "status": status,
            "title": "",
            "description": f"Skipped non-HTML content: {content_type}",
            "text": "",
            "emails": set(),
            "phones": set(),
            "links": [],
            "people": [],
            "company_name": "",
        }

    soup = BeautifulSoup(html, "html.parser")

    people = extract_people_from_dom(
        soup,
        final_url
    )

    title = (
        soup.title.get_text(" ", strip=True)
        if soup.title else ""
    )

    desc_tag = soup.find(
        "meta",
        attrs={"name": re.compile("description", re.I)}
    )

    description = (
        desc_tag.get("content", "").strip()
        if desc_tag else ""
    )

    site_tag = soup.find(
        "meta",
        property="og:site_name"
    )

    company_name = (
        site_tag.get("content", "").strip()
        if site_tag else ""
    )

    if not company_name:

        h1 = soup.find("h1")

        company_name = (
            h1.get_text(" ", strip=True)
            if h1 else title
        )

    # Emails and phone numbers

    visible_text = soup.get_text(
        " ",
        strip=True
    )

    emails = set(re.findall(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        visible_text,
        flags=re.I,
    ))

    for anchor in soup.select('a[href^="mailto:"]'):

        email = (
            anchor.get("href", "")
            .replace("mailto:", "")
            .split("?", 1)[0]
            .strip()
        )

        if email:
            emails.add(email)

    phones = set()

    for anchor in soup.select('a[href^="tel:"]'):

        phone = (
            anchor.get("href", "")
            .replace("tel:", "")
            .strip()
        )

        if phone:
            phones.add(phone)

    for phone in re.findall(
        r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)",
        visible_text
    ):

        digits = re.sub(r"\D", "", phone)

        if 8 <= len(digits) <= 16:
            phones.add(
                re.sub(r"\s+", " ", phone).strip()
            )

    # Remove irrelevant HTML

    for tag in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "iframe"
    ]):
        tag.decompose()

    text = re.sub(
        r"\s+",
        " ",
        soup.get_text(" ", strip=True)
    ).strip()

    people.extend(
        extract_people_from_text(text, final_url)
    )

    # Internal pages

    relevant_words = (
        "about",
        "contact",
        "team",
        "leadership",
        "staff",
        "people",
        "management",
        "company",
        "imprint",
        "our-team",
        "meet-the-team",
        "owners",
        "our-story",
        "who-we-are",
    )

    domain = urlsplit(final_url).netloc.lower()

    links = []

    for anchor in soup.find_all("a", href=True):

        href = urljoin(
            final_url,
            anchor["href"].strip()
        ).split("#", 1)[0]

        parsed = urlsplit(href)

        label = anchor.get_text(
            " ",
            strip=True
        ).lower()

        path = parsed.path.lower()

        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc.lower() == domain
            and any(
                word in (label + " " + path)
                for word in relevant_words
            )
        ):

            links.append(href)

    return {

        "url": final_url,

        "status": status,

        "title": title,

        "description": description,

        "text": text,

        "emails": emails,

        "phones": phones,

        "links": list(dict.fromkeys(links)),

        "people": dedupe_people(people),

        "company_name": company_name,

    }


def crawl_website(
    start_url: str,
    max_pages: int = 6
):

    visited = set()
    queue = [start_url]
    pages = []

    start_host = urlsplit(
        start_url
    ).netloc.lower()

    while queue and len(pages) < max_pages:

        current = queue.pop(0)

        if current in visited:
            continue

        visited.add(current)

        try:

            page = extract_page(current)

            pages.append(page)

            for link in page.get("links", []):

                if (
                    urlsplit(link).netloc.lower() == start_host
                    and link not in visited
                ):
                    queue.append(link)

        except Exception as exc:

            pages.append({

                "url": current,
                "status": "Error",
                "title": "",
                "description": str(exc),
                "text": "",
                "emails": set(),
                "phones": set(),
                "links": [],
                "people": [],
                "company_name": "",

            })

        time.sleep(0.2)

    return pages


def people_from_search_result(result: dict):

    title_text = str(
        result.get("title", "")
    )

    snippet = str(
        result.get("snippet", "")
    )

    source_url = str(
        result.get("url", "")
    )

    found = extract_people_from_text(
        f"{title_text}. {snippet}",
        source_url
    )

    cleaned_title = re.split(
        r"\s+[|–—-]\s+",
        title_text
    )[0].strip()

    role_match = re.search(
        ROLE_PATTERN,
        snippet,
        flags=re.I
    )

    if (
        role_match
        and re.fullmatch(PERSON_NAME_PATTERN, cleaned_title)
    ):

        add_person(
            found,
            cleaned_title,
            role_match.group(0),
            source_url,
            "Search result title contains a person's name and snippet states the role.",
            "Search-result candidate; verify original source",
        )

    for person in found:
        person["Confidence"] = (
            "Search-result candidate; verify original source"
        )

    return found


def is_candidate_company_url(url: str) -> bool:

    try:

        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()

        if not host or "." not in host:
            return False

        if any(
            host == bad or host.endswith("." + bad)
            for bad in NON_COMPANY_HOSTS
        ):
            return False

        if host.endswith((".gov", ".edu", ".pdf")):
            return False

        return parsed.scheme in {"http", "https"}

    except Exception:
        return False


def root_url(url: str) -> str:

    parsed = urlsplit(url)

    return urlunsplit((
        parsed.scheme or "https",
        parsed.netloc,
        "/",
        "",
        ""
    ))


# ============================================================
# GEMINI: CLIENT BRIEF BREAKDOWN ONLY
# ============================================================

def get_gemini_client():

    api_key = os.getenv(
        "GEMINI_API_KEY",
        ""
    )

    try:

        api_key = api_key or st.secrets.get(
            "GEMINI_API_KEY",
            ""
        )

    except Exception:
        pass

    if not api_key:
        return None

    # Gemini is imported only when this function is called.
    from google import genai

    return genai.Client(
        api_key=api_key
    )


def interpret_brief(
    client,
    model: str,
    brief: str,
    location_override: str = "",
):

    prompt = f"""
You are a B2B lead-research analyst.

Your ONLY task is to understand the client's work description
and convert it into a practical research plan for a Python app.

You are NOT doing web research.
Do not invent company names or people.

CLIENT WORK DESCRIPTION:
{brief}

LOCATION OVERRIDE:
{location_override or 'No override; use the client brief.'}

Return valid JSON with these keys:

{{
  "plain_english_breakdown": "Explain in simple language exactly what the client needs",
  "campaign_goal": "What the research is intended to achieve",
  "target_industries": ["industry"],
  "target_locations": ["country, state or region"],
  "company_types": ["business type"],
  "company_size": "employee count or unspecified",
  "decision_maker_titles": ["Owner", "Founder"],
  "keywords_and_signals": ["search terms or buying signals"],
  "exclusions": ["companies or records to exclude"],
  "data_to_collect": ["Company", "Website", "Full Name", "Job Title", "Email", "Phone", "Source URL"],
  "search_queries": ["4 to 7 practical search engine queries"]
}}

RULES:

- Keep the interpretation faithful to the client brief.
- Do not add unsupported targeting requirements.
- Mark missing requirements as unspecified.
- If job titles are not specified, recommend relevant titles and say they are suggestions.
- Create search queries that Python can submit to a normal web search engine.
- Gemini must only analyze the brief.
- Python will perform the actual search and extraction.
- Return JSON only, without Markdown fences.
"""

    from google.genai import types

    response = client.models.generate_content(

        model=model,

        contents=prompt,

        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            max_output_tokens=1800,
        ),

    )

    data = json.loads(
        response.text or "{}"
    )

    if not isinstance(data, dict):
        raise ValueError(
            "Gemini did not return a valid research plan."
        )

    list_keys = [
        "target_industries",
        "target_locations",
        "company_types",
        "decision_maker_titles",
        "keywords_and_signals",
        "exclusions",
        "data_to_collect",
        "search_queries",
    ]

    for key in list_keys:

        if not isinstance(data.get(key), list):
            data[key] = []

    return data


def show_gemini_error(exc):

    message = str(exc)
    low = message.lower()

    if (
        "429" in low
        or "resource_exhausted" in low
        or "quota" in low
    ):

        st.error(
            "Gemini quota/rate limit reached. "
            "This affects only client-brief analysis in AI Company Research."
        )

        st.info(
            "Bulk Website Extractor and the Python search/extraction engine "
            "do not use Gemini."
        )

        st.markdown(
            "[Check Google AI Studio usage](https://aistudio.google.com/usage) · "
            "[Check model limits](https://aistudio.google.com/rate-limit)"
        )

    elif "403" in low or "permission_denied" in low:

        st.error(
            "Gemini API access was denied. Check the API key and billing/project access."
        )

    elif "404" in low or "not_found" in low:

        st.error(
            "Gemini model was not found. Check the model name in the AI Research settings."
        )

    else:

        st.error(
            f"Gemini brief analysis failed: {message}"
        )


# ============================================================
# RESULTS / EXPORT HELPERS
# ============================================================

def make_excel_download(sheets: dict) -> bytes:

    buffer = io.BytesIO()

    with pd.ExcelWriter(
        buffer,
        engine="openpyxl"
    ) as writer:

        for sheet_name, dataframe in sheets.items():

            dataframe.to_excel(
                writer,
                index=False,
                sheet_name=sheet_name[:31]
            )

    return buffer.getvalue()


def show_extraction_results(
    prefix: str,
    pages: list,
    people: list,
    search_results: list,
):

    emails = sorted({
        email
        for page in pages
        for email in page.get("emails", set())
    })

    phones = sorted({
        phone
        for page in pages
        for phone in page.get("phones", set())
    })

    websites = {
        page.get(
            "Input Website",
            page.get("Website", "")
        )
        for page in pages
    }

    m1, m2, m3, m4 = st.columns(4)

    m1.metric("Websites", len(websites))
    m2.metric("Pages processed", len(pages))
    m3.metric("Emails found", len(emails))
    m4.metric("People candidates", len(people))

    st.subheader("People / decision-maker candidates")

    people_df = pd.DataFrame(
        people,
        columns=[
            "Company",
            "Full Name",
            "Job Title",
            "Source URL",
            "Evidence",
            "Confidence",
        ]
    )

    if not people_df.empty:

        st.dataframe(
            people_df[
                [
                    "Company",
                    "Full Name",
                    "Job Title"
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("People source evidence"):

            st.dataframe(
                people_df,
                use_container_width=True,
                hide_index=True,
            )

    else:

        st.info(
            "No clear name-and-title match was found in accessible "
            "pages/search snippets. The tool will not guess names."
        )

    page_rows = []
    contact_rows = []

    for page in pages:

        website = page.get(
            "Input Website",
            page.get("Website", "")
        )

        page_emails = page.get(
            "emails",
            set()
        )

        page_phones = page.get(
            "phones",
            set()
        )

        page_rows.append({

            "Website": website,

            "Page URL": page.get("url", ""),

            "Page Title": page.get("title", ""),

            "Status": page.get("status", ""),

            "Description": page.get("description", ""),

            "Emails": (
                "; ".join(sorted(page_emails))
                if isinstance(page_emails, set)
                else str(page_emails)
            ),

            "Phones": (
                "; ".join(sorted(page_phones))
                if isinstance(page_phones, set)
                else str(page_phones)
            ),

            "Extracted Text": page.get("text", ""),

        })

        for email in page_emails:

            contact_rows.append({

                "Website": website,

                "Email": email,

                "Phone": (
                    "; ".join(sorted(page_phones))
                    if isinstance(page_phones, set)
                    else str(page_phones)
                ),

                "Source Page": page.get("url", ""),

            })

    pages_df = pd.DataFrame(page_rows)

    contacts_df = pd.DataFrame(
        contact_rows,
        columns=[
            "Website",
            "Email",
            "Phone",
            "Source Page",
        ]
    )

    search_df = pd.DataFrame(search_results)

    st.subheader("Website pages")

    if not pages_df.empty:

        st.dataframe(
            pages_df[
                [
                    "Website",
                    "Page URL",
                    "Page Title",
                    "Status",
                    "Description",
                    "Emails",
                    "Phones",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Public contact information")

    if contacts_df.empty:

        st.info(
            "No public email addresses were found on accessible pages."
        )

    else:

        st.dataframe(
            contacts_df,
            use_container_width=True,
            hide_index=True,
        )

    if not search_df.empty:

        with st.expander("Search results / source URLs"):

            st.dataframe(
                search_df,
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("Download results")

    x1, x2, x3 = st.columns(3)

    with x1:

        st.download_button(
            "Pages CSV",
            pages_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            f"{prefix}_pages.csv",
            "text/csv",
            use_container_width=True,
        )

    with x2:

        st.download_button(
            "Contacts CSV",
            contacts_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            f"{prefix}_contacts.csv",
            "text/csv",
            use_container_width=True,
        )

    with x3:

        st.download_button(
            "People CSV",
            people_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            f"{prefix}_people.csv",
            "text/csv",
            use_container_width=True,
            disabled=people_df.empty,
        )

    excel = make_excel_download({

        "Pages": pages_df,

        "Contacts": contacts_df,

        "People": people_df,

        "Search": search_df,

    })

    st.download_button(
        "Download all results as Excel",
        excel,
        f"{prefix}_research.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    with st.expander("View full extracted page text"):

        if pages:

            selected = st.selectbox(
                "Choose a page",
                range(len(pages)),
                format_func=lambda i: (
                    f"{pages[i].get('Input Website', '')} | "
                    f"{pages[i].get('title', '') or pages[i].get('url', '')}"
                ),
                key=f"{prefix}_page_select",
            )

            st.text_area(
                "Extracted text",
                pages[selected].get("text", ""),
                height=260,
                disabled=True,
                key=f"{prefix}_page_text",
            )


# ============================================================
# TOOL 1: BULK WEBSITE EXTRACTOR - PYTHON ONLY
# ============================================================

def bulk_extractor_page():

    st.markdown(
        '<div class="hero">'
        '<h1>Bulk Website Extractor</h1>'
        '<p>Python crawls public websites and extracts available '
        'emails, phones, names and job titles. Gemini is not used here.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([2, 1])

    with left:

        urls_text = st.text_area(
            "Website URLs (one per line)",
            height=175,
            placeholder=(
                "https://company1.com\n"
                "https://company2.com\n"
                "company3.com"
            ),
            key="bulk_urls_text",
        )

    with right:

        upload = st.file_uploader(
            "Upload TXT or CSV URL list",
            type=["txt", "csv"],
            key="bulk_urls_file"
        )

        max_pages = st.slider(
            "Maximum pages per website",
            1,
            15,
            6,
            key="bulk_pages_limit"
        )

        do_search = st.checkbox(
            "Also search the web for owner / founder / CEO mentions",
            value=True,
            key="bulk_do_search"
        )

        search_limit = st.slider(
            "Search results per website",
            2,
            10,
            5,
            disabled=not do_search,
            key="bulk_search_limit"
        )

    if upload:

        uploaded_text = upload.getvalue().decode(
            "utf-8",
            errors="ignore"
        )

        urls_text = (
            urls_text + "\n" + uploaded_text
        ).strip()

    if not DDGS_AVAILABLE and do_search:

        st.warning(
            "Web search package is unavailable. Website crawling "
            "will still work. Check requirements.txt for ddgs."
        )

    if st.button(
        "🚀 Extract websites",
        type="primary",
        use_container_width=True
    ):

        urls = []
        invalid = []

        for line in [
            x.strip()
            for x in urls_text.splitlines()
            if x.strip()
        ]:

            if line.lower() in {
                "url",
                "website",
                "websites",
                "domain",
            }:
                continue

            candidate = (
                line.split(",", 1)[0]
                .strip()
                .strip('"')
                .strip("'")
            )

            try:

                normalized = normalize_url(candidate)

                if normalized not in urls:
                    urls.append(normalized)

            except ValueError:
                invalid.append(line)

        if not urls:

            st.error(
                "Paste at least one valid website URL."
            )
            return

        if len(urls) > MAX_BULK_URLS:

            st.error(
                f"Maximum {MAX_BULK_URLS} websites per run. "
                "Split your list into batches."
            )
            return

        pages_all = []
        people_all = []
        search_all = []

        progress = st.progress(
            0,
            text="Starting website extraction..."
        )

        for index, website in enumerate(urls, start=1):

            progress.progress(
                (index - 1) / len(urls),
                text=f"Crawling {website}"
            )

            pages = crawl_website(
                website,
                max_pages=max_pages
            )

            for page in pages:

                page["Input Website"] = website
                pages_all.append(page)

                for person in page.get("people", []):

                    people_all.append({

                        "Company": (
                            page.get("company_name")
                            or urlsplit(website).netloc
                        ),

                        **person,

                    })

            if do_search and DDGS_AVAILABLE:

                domain = (
                    urlsplit(website)
                    .hostname or ""
                ).lower().removeprefix("www.")

                query = (
                    f'site:{domain} '
                    '(owner OR founder OR CEO OR president '
                    'OR "managing director")'
                )

                search_results, search_error = search_web(
                    query,
                    max_results=search_limit
                )

                for result in search_results:

                    result["Input Website"] = website
                    search_all.append(result)

                    for person in people_from_search_result(result):

                        people_all.append({
                            "Company": domain,
                            **person,
                        })

                if search_error:

                    search_all.append({

                        "title": "Search warning",
                        "url": "",
                        "snippet": search_error,
                        "Input Website": website,

                    })

            time.sleep(0.2)

        st.session_state["bulk_pages_results"] = pages_all

        st.session_state["bulk_people_results"] = dedupe_people(
            people_all
        )

        st.session_state["bulk_search_results"] = search_all

        st.session_state["bulk_invalid_urls"] = invalid

        st.success(
            f"Finished: {len(urls)} websites and "
            f"{len(pages_all)} page results processed."
        )

    pages = st.session_state.get(
        "bulk_pages_results",
        []
    )

    if pages:

        invalid = st.session_state.get(
            "bulk_invalid_urls",
            []
        )

        if invalid:

            with st.expander("Skipped invalid URL lines"):
                st.write(invalid)

        show_extraction_results(
            "bulk",
            pages,
            st.session_state.get("bulk_people_results", []),
            st.session_state.get("bulk_search_results", []),
        )

    else:

        st.info(
            "Paste multiple website URLs and click Extract websites. "
            "This tool works without Gemini."
        )


# ============================================================
# TOOL 2: AI COMPANY RESEARCH
# GEMINI BRIEF BREAKDOWN + PYTHON WEB RESEARCH
# ============================================================

def ai_company_research_page():

    st.markdown(
        '<div class="hero">'
        '<h1>AI Company Research</h1>'
        '<p>Gemini only understands the client brief. Python performs '
        'the actual web search, company research and data extraction.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    brief = st.text_area(
        "Client work description / job post / research brief",
        height=190,
        placeholder=(
            "Paste the client's full requirements. Example: Find US electrical "
            "contractors with 5–50 employees and identify owners or operations "
            "managers. Collect company website, public business email, phone "
            "and source URLs."
        ),
        key="ai_brief_input",
    )

    c1, c2, c3 = st.columns(3)

    with c1:

        location_override = st.text_input(
            "Location override (optional)",
            placeholder="United States",
            key="ai_location_override"
        )

    with c2:

        max_companies = st.number_input(
            "Maximum companies",
            min_value=1,
            max_value=MAX_RESEARCH_COMPANIES,
            value=10,
            key="ai_max_companies"
        )

    with c3:

        max_pages = st.number_input(
            "Pages per company",
            min_value=1,
            max_value=12,
            value=5,
            key="ai_pages_per_company"
        )

    model = st.text_input(
        "Gemini model",
        value=DEFAULT_GEMINI_MODEL,
        key="ai_model_name"
    )

    if not DDGS_AVAILABLE:

        st.warning(
            "Python web search is unavailable because ddgs did not load. "
            "Website extraction still works; verify requirements.txt."
        )

    # STEP 1: GEMINI ONLY ANALYZES THE CLIENT BRIEF

    if st.button(
        "1. Understand client brief with Gemini",
        type="primary",
        use_container_width=True
    ):

        if not brief.strip():

            st.error(
                "Paste the client's work description first."
            )

        else:

            try:

                client = get_gemini_client()

                if client is None:

                    st.error(
                        "GEMINI_API_KEY is missing from Streamlit Secrets."
                    )

                else:

                    with st.spinner(
                        "Gemini is only breaking down the client brief..."
                    ):

                        plan = interpret_brief(
                            client,
                            model.strip() or DEFAULT_GEMINI_MODEL,
                            brief,
                            location_override,
                        )

                    st.session_state["ai_plan"] = plan
                    st.session_state["ai_brief_saved"] = brief
                    st.session_state["ai_research_results"] = None

                    st.success(
                        "Brief breakdown is ready. Gemini has not searched "
                        "for companies or people."
                    )

            except Exception as exc:

                show_gemini_error(exc)

    plan = st.session_state.get("ai_plan")

    if not plan:

        st.info(
            "Step 1: paste a client brief and ask Gemini to identify "
            "the ICP, locations, company size, titles and fields required."
        )

        return

    # Display Gemini interpretation

    st.subheader(
        "Gemini's breakdown of the client requirement"
    )

    st.write(
        plan.get("plain_english_breakdown", "")
    )

    left, right = st.columns(2)

    with left:

        st.markdown("**Campaign goal**")
        st.write(plan.get("campaign_goal", ""))

        st.markdown("**Target industries**")
        st.write(plan.get("target_industries", []))

        st.markdown("**Target locations**")
        st.write(plan.get("target_locations", []))

        st.markdown("**Company types**")
        st.write(plan.get("company_types", []))

        st.markdown("**Company size**")
        st.write(plan.get("company_size", "Unspecified"))

    with right:

        st.markdown("**Decision-maker titles**")
        st.write(plan.get("decision_maker_titles", []))

        st.markdown("**Keywords / signals**")
        st.write(plan.get("keywords_and_signals", []))

        st.markdown("**Exclusions**")
        st.write(plan.get("exclusions", []))

        st.markdown("**Data to collect**")
        st.write(plan.get("data_to_collect", []))

    st.markdown(
        "#### Search queries Python will run"
    )

    queries_text = st.text_area(
        "Edit search queries (one per line)",
        value="\n".join(plan.get("search_queries", [])),
        height=150,
        key="ai_search_queries",
    )

    st.caption(
        "The next step uses Python and the web search library. "
        "Gemini will not be called again for company extraction."
    )

    # STEP 2: PYTHON DOES ACTUAL RESEARCH

    if st.button(
        "2. Search companies and extract data with Python",
        type="primary",
        use_container_width=True
    ):

        if not DDGS_AVAILABLE:

            st.error(
                f"DDGS search package could not be loaded: {DDGS_IMPORT_ERROR}"
            )

            return

        queries = [
            q.strip()
            for q in queries_text.splitlines()
            if q.strip()
        ]

        if not queries:

            st.error(
                "Add at least one search query."
            )

            return

        search_results_all = []
        search_errors = []

        search_progress = st.progress(
            0,
            text="Searching the web..."
        )

        queries_to_run = queries[:7]

        for index, query in enumerate(
            queries_to_run,
            start=1
        ):

            search_progress.progress(
                (index - 1) / len(queries_to_run),
                text=f"Searching: {query[:85]}"
            )

            found, error = search_web(
                query,
                max_results=8,
                region="us-en",
                backend="auto",
            )

            for item in found:

                item["Search Query"] = query
                search_results_all.append(item)

            if error:
                search_errors.append(f"{query}: {error}")

            time.sleep(0.25)

        search_progress.progress(
            1.0,
            text="Preparing company list..."
        )

        # Select unique company domains

        candidates = []
        seen_hosts = set()

        for item in search_results_all:

            url = item.get("url", "")

            if not is_candidate_company_url(url):
                continue

            host = (
                urlsplit(url).hostname or ""
            ).lower().removeprefix("www.")

            if host in seen_hosts:
                continue

            seen_hosts.add(host)

            candidates.append({

                "domain": host,

                "url": root_url(url),

                "search_item": item,

            })

            if len(candidates) >= int(max_companies):
                break

        if not candidates:

            st.warning(
                "No suitable company websites came back from search. "
                "Edit the queries to make them more specific and try again."
            )

            if search_errors:

                with st.expander("Search errors"):
                    st.write(search_errors)

            return

        companies = []
        people = []
        pages_all = []
        contacts = []

        crawl_progress = st.progress(
            0,
            text="Opening company websites..."
        )

        for idx, candidate in enumerate(
            candidates,
            start=1
        ):

            crawl_progress.progress(
                (idx - 1) / len(candidates),
                text=f"Researching {candidate['domain']}"
            )

            website = candidate["url"]

            pages = crawl_website(
                website,
                max_pages=int(max_pages)
            )

            site_name = next(
                (
                    p.get("company_name")
                    for p in pages
                    if p.get("company_name")
                ),
                ""
            )

            if not site_name:

                site_name = next(
                    (
                        p.get("title")
                        for p in pages
                        if p.get("title")
                    ),
                    candidate["domain"]
                )

            site_emails = sorted({
                email
                for p in pages
                for email in p.get("emails", set())
            })

            site_phones = sorted({
                phone
                for p in pages
                for phone in p.get("phones", set())
            })

            site_people = [
                person
                for p in pages
                for person in p.get("people", [])
            ]

            # Python searches for people at the company.
            # No Gemini call here.

            role_terms = plan.get(
                "decision_maker_titles",
                []
            )[:5]

            role_query = (
                " OR ".join(
                    f'"{role}"'
                    for role in role_terms
                )
                if role_terms
                else "owner OR founder OR CEO OR president"
            )

            person_search, person_search_error = search_web(
                f'"{candidate["domain"]}" ({role_query})',
                max_results=5,
                region="us-en",
                backend="auto",
            )

            for result in person_search:

                site_people.extend(
                    people_from_search_result(result)
                )

                result["Research Company"] = site_name
                search_results_all.append(result)

            site_people = dedupe_people(
                site_people,
                company=site_name
            )

            companies.append({

                "Company": site_name,

                "Website": website,

                "Domain": candidate["domain"],

                "Description": next(
                    (
                        p.get("description")
                        for p in pages
                        if p.get("description")
                    ),
                    ""
                ),

                "Public Emails": "; ".join(site_emails),

                "Public Phones": "; ".join(site_phones),

                "Decision Makers Found": len(site_people),

                "Pages Checked": len(pages),

                "Search Source URL": candidate["search_item"].get(
                    "url",
                    ""
                ),

            })

            for person in site_people:

                people.append({

                    "Company": site_name,

                    "Website": website,

                    **person,

                })

            for page in pages:

                pages_all.append({

                    "Company": site_name,

                    "Website": website,

                    "Page URL": page.get("url", ""),

                    "Page Title": page.get("title", ""),

                    "Status": page.get("status", ""),

                    "Description": page.get("description", ""),

                    "Emails": "; ".join(
                        sorted(page.get("emails", set()))
                    ),

                    "Phones": "; ".join(
                        sorted(page.get("phones", set()))
                    ),

                    "Extracted Text": page.get("text", ""),

                })

            for email in site_emails:

                contacts.append({

                    "Company": site_name,

                    "Website": website,

                    "Email": email,

                    "Phone": "; ".join(site_phones),

                    "Source Page": next(
                        (
                            p.get("url", "")
                            for p in pages
                            if email in p.get("emails", set())
                        ),
                        website
                    ),

                })

            if person_search_error:

                search_errors.append(
                    f"Person search for {candidate['domain']}: "
                    f"{person_search_error}"
                )

            time.sleep(0.2)

        crawl_progress.progress(
            1.0,
            text="Company research completed."
        )

        st.session_state["ai_research_results"] = {

            "companies": companies,

            "people": people,

            "pages": pages_all,

            "contacts": contacts,

            "search": search_results_all,

            "errors": search_errors,

        }

        st.success(
            f"Python research complete: {len(companies)} companies, "
            f"{len(people)} person candidates and "
            f"{len(contacts)} public email records."
        )

    # DISPLAY FINAL RESEARCH

    results = st.session_state.get(
        "ai_research_results"
    )

    if results:

        companies_df = pd.DataFrame(
            results["companies"]
        )

        people_df = pd.DataFrame(
            results["people"],
            columns=[
                "Company",
                "Website",
                "Full Name",
                "Job Title",
                "Source URL",
                "Evidence",
                "Confidence",
            ]
        )

        contacts_df = pd.DataFrame(
            results["contacts"],
            columns=[
                "Company",
                "Website",
                "Email",
                "Phone",
                "Source Page",
            ]
        )

        pages_df = pd.DataFrame(
            results["pages"]
        )

        search_df = pd.DataFrame(
            results["search"]
        )

        st.subheader("Research results")

        a, b, c, d = st.columns(4)

        a.metric(
            "Companies",
            len(companies_df)
        )

        b.metric(
            "People candidates",
            len(people_df)
        )

        c.metric(
            "Public emails",
            contacts_df["Email"].nunique()
            if not contacts_df.empty else 0
        )

        d.metric(
            "Pages checked",
            len(pages_df)
        )

        st.markdown("#### Companies identified")

        st.dataframe(
            companies_df,
            use_container_width=True,
            hide_index=True
        )

        st.markdown(
            "#### People / decision-maker candidates"
        )

        if people_df.empty:

            st.info(
                "No explicit person-and-title evidence was found. "
                "The app will not invent people."
            )

        else:

            st.dataframe(
                people_df,
                use_container_width=True,
                hide_index=True
            )

        st.markdown(
            "#### Public contact information"
        )

        if contacts_df.empty:

            st.info(
                "No public emails were found on the pages that could be accessed."
            )

        else:

            st.dataframe(
                contacts_df,
                use_container_width=True,
                hide_index=True
            )

        with st.expander(
            "Website pages and extracted content"
        ):

            if not pages_df.empty:

                visible = [
                    x
                    for x in [
                        "Company",
                        "Website",
                        "Page URL",
                        "Page Title",
                        "Status",
                        "Description",
                    ]
                    if x in pages_df.columns
                ]

                st.dataframe(
                    pages_df[visible],
                    use_container_width=True,
                    hide_index=True
                )

        with st.expander("Search sources"):

            if not search_df.empty:

                st.dataframe(
                    search_df,
                    use_container_width=True,
                    hide_index=True
                )

        if results.get("errors"):

            with st.expander("Search warnings"):
                st.write(results["errors"])

        st.subheader("Download research")

        x1, x2 = st.columns(2)

        with x1:

            excel = make_excel_download({

                "Companies": companies_df,

                "People": people_df,

                "Contacts": contacts_df,

                "Pages": pages_df,

                "Search Sources": search_df,

            })

            st.download_button(
                "Download full research Excel",
                excel,
                "client_research_results.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with x2:

            st.download_button(
                "Download companies CSV",
                companies_df.to_csv(
                    index=False
                ).encode("utf-8-sig"),
                "researched_companies.csv",
                "text/csv",
                use_container_width=True,
            )


# ============================================================
# APP NAVIGATION
# ============================================================

with st.sidebar:

    st.markdown("## 🔎 Research Suite")

    selected_tool = st.radio(
        "Choose a tool",
        [
            "Bulk Website Extractor",
            "AI Company Research"
        ],
        label_visibility="collapsed",
    )

    st.divider()

    st.caption(
        "Bulk Extractor = Python only. No Gemini."
    )

    st.caption(
        "AI Company Research = Gemini brief breakdown + Python research."
    )


if selected_tool == "Bulk Website Extractor":

    bulk_extractor_page()

else:

    ai_company_research_page()


st.divider()

st.caption(
    "This app extracts information from accessible public pages and search results. "
    "It does not bypass logins, CAPTCHAs or paywalls. Search results and extracted "
    "person/title candidates should be checked against original sources before delivery. "
    "JavaScript-rendered pages may require a browser-based crawler and may not be readable here."
)
