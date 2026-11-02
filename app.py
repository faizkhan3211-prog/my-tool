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

# Optional search library. The app can still start if this import fails.
try:
    from ddgs import DDGS
    DDGS_AVAILABLE = True
    DDGS_IMPORT_ERROR = ""
except Exception as exc:
    DDGS = None
    DDGS_AVAILABLE = False
    DDGS_IMPORT_ERROR = str(exc)


# ============================================================
# CONFIG
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
    .block-container {max-width: 1500px; padding-top: 1.4rem; padding-bottom: 3rem;}
    [data-testid="stSidebar"] {border-right: 1px solid rgba(128,128,128,.18);}
    .hero {
        padding: 1.25rem 1.5rem;
        border: 1px solid rgba(128,128,128,.22);
        border-radius: 16px;
        margin-bottom: 1.2rem;
        background: linear-gradient(120deg, rgba(40,100,180,.12), rgba(20,170,160,.08));
    }
    .hero h1 {margin: 0 0 .35rem 0; font-size: 1.9rem;}
    .hero p {margin: 0; opacity: .78;}
    div[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.2); padding: 12px; border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)

USER_AGENT = "Mozilla/5.0 (compatible; LeadResearchApp/2.0; public website research)"
REQUEST_TIMEOUT = 15
MAX_PAGE_BYTES = 4 * 1024 * 1024
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

SOCIAL_DOMAINS = {
    "LinkedIn": ("linkedin.com",),
    "Facebook": ("facebook.com",),
    "Instagram": ("instagram.com",),
    "X / Twitter": ("x.com", "twitter.com"),
}
OTHER_SOCIAL_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "threads.net"
)

SKIP_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com",
    "youtu.be", "twitter.com", "x.com", "tiktok.com", "pinterest.com",
    "threads.net", "yelp.com", "yellowpages.com", "bbb.org", "mapquest.com",
    "google.com", "bing.com", "duckduckgo.com", "crunchbase.com",
    "zoominfo.com", "apollo.io", "wikipedia.org", "reddit.com",
    "glassdoor.com", "indeed.com", "wixsite.com", "wordpress.com",
    "alignable.com", "manta.com", "chamberofcommerce.com", "opencorporates.com",
}

RELEVANT_PATH_WORDS = (
    "about", "contact", "team", "leadership", "company", "our-story",
    "who-we-are", "management", "imprint", "our-company"
)


# ============================================================
# URL / REQUEST HELPERS
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
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", "", ""))


def root_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme or "https", parsed.netloc, "/", "", ""))


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP/HTTPS URLs are allowed.")
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        raise ValueError("Local network URLs are blocked.")
    try:
        records = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, ValueError, OSError) as exc:
        raise ValueError(f"Could not resolve website: {exc}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise ValueError("Website host did not resolve.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("Private, reserved or local network addresses are blocked.")


def fetch_html(url: str):
    """Fetch public HTML, validating every redirect destination."""
    current = url
    redirect_codes = {301, 302, 303, 307, 308}
    for _ in range(6):
        validate_public_url(current)
        response = requests.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
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
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type and "xhtml" not in content_type:
                return None, response.url, response.status_code, content_type
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise ValueError("Page exceeds the 4 MB safety limit.")
            html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            return html, response.url, response.status_code, content_type
        finally:
            response.close()
    raise ValueError("Too many redirects.")


# ============================================================
# SEARCH (PYTHON ONLY)
# ============================================================

def search_web(query: str, max_results: int = 20, page: int = 1, region: str = "us-en"):
    if not DDGS_AVAILABLE:
        return [], f"The ddgs package is not available: {DDGS_IMPORT_ERROR}"
    try:
        results = DDGS(timeout=15).text(
            query=query,
            region=region,
            safesearch="moderate",
            max_results=max_results,
            page=page,
            backend="auto",
        )
        rows = []
        for item in results:
            rows.append({
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
                "query": query,
            })
        return rows, ""
    except Exception as exc:
        return [], str(exc)


def region_code(location: str) -> str:
    text = (location or "").lower()
    mapping = {
        "united states": "us-en", "usa": "us-en", " u.s. ": "us-en",
        "united kingdom": "uk-en", "uk": "uk-en", "britain": "uk-en",
        "canada": "ca-en", "australia": "au-en", "new zealand": "nz-en",
        "pakistan": "pk-en", "india": "in-en", "germany": "de-de",
        "france": "fr-fr", "ireland": "ie-en", "singapore": "sg-en",
        "uae": "wt-wt", "united arab emirates": "wt-wt",
    }
    for key, value in mapping.items():
        if key in text:
            return value
    return "us-en"


def direct_search_queries(instructions: str, location: str = ""):
    """Turn direct instructions into search phrases without an AI call."""
    brief = re.sub(r"\s+", " ", instructions).strip()
    loc = (location or "").strip()
    queries = []

    # User-entered separate lines are treated as separate search queries.
    for line in instructions.splitlines():
        line = line.strip(" \t-•")
        if len(line) > 8:
            queries.append(line)

    if loc and loc.lower() not in brief.lower():
        queries.append(f"{brief} {loc}")

    queries.extend([
        f"{brief} companies official website",
        f"{brief} businesses company websites",
        f"{brief} company directory business websites",
    ])

    unique = []
    seen = set()
    for query in queries:
        normalized = query.casefold()
        if query and normalized not in seen:
            seen.add(normalized)
            unique.append(query)
    return unique or [brief]


def collect_company_candidates(queries, requested_count, location=""):
    """Paginate search queries until requested company count is reached or results end."""
    region = region_code(location)
    candidates = []
    seen_domains = set()
    all_search_results = []
    errors = []

    progress = st.progress(0, text="Searching the web for company websites...")
    status = st.empty()

    for query_index, query in enumerate(queries, start=1):
        page = 1
        while len(candidates) < requested_count:
            status.write(f"Search {query_index}/{len(queries)} · page {page}: {query[:100]}")
            results, error = search_web(
                query,
                max_results=20,
                page=page,
                region=region,
            )
            if error:
                errors.append(f"{query}: {error}")
                break
            if not results:
                break
            all_search_results.extend(results)

            for item in results:
                url = item.get("url", "")
                if not is_candidate_company_url(url):
                    continue
                domain = (urlsplit(url).hostname or "").lower().removeprefix("www.")
                if not domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)
                candidates.append({
                    "domain": domain,
                    "website": root_url(url),
                    "search_result": item,
                })
                if len(candidates) >= requested_count:
                    break

            progress.progress(
                min(1.0, len(candidates) / requested_count),
                text=f"Found {len(candidates)} of {requested_count} requested companies...",
            )

            # No more result pages for this query when the backend returns a short page.
            if len(results) < 20:
                break
            page += 1

        if len(candidates) >= requested_count:
            break

    status.empty()
    progress.empty()
    return candidates[:requested_count], all_search_results, errors


def is_candidate_company_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host or "." not in host:
            return False
        if any(host == blocked or host.endswith("." + blocked) for blocked in SKIP_DOMAINS):
            return False
        if host.endswith((".gov", ".edu")):
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        return True
    except Exception:
        return False


# ============================================================
# COMPANY NAME, SOCIALS, REVENUE EXTRACTION
# ============================================================

def extract_org_name_from_jsonld(soup):
    names = []

    def walk(node):
        if isinstance(node, dict):
            raw_type = node.get("@type", "")
            types_found = {str(x).lower() for x in raw_type} if isinstance(raw_type, list) else {str(raw_type).lower()}
            if types_found.intersection({"organization", "localbusiness", "corporation", "professionalservice", "store"}):
                name = node.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return names[0] if names else ""


def clean_company_name(name: str, domain: str) -> str:
    value = re.sub(r"\s+", " ", str(name or "")).strip(" |–—-:")
    if not value or len(value) > 100:
        value = domain.split(".")[0].replace("-", " ").replace("_", " ").title()
    # Strip common page-title suffixes.
    value = re.split(r"\s+[|–—]\s+|\s+-\s+", value)[0].strip()
    if value.lower() in {"home", "homepage", "welcome", "official website"}:
        value = domain.split(".")[0].replace("-", " ").title()
    return value or domain


def classify_social(url: str):
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for label, domains in SOCIAL_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return label
    if any(host == d or host.endswith("." + d) for d in OTHER_SOCIAL_DOMAINS):
        return "Other Socials"
    return ""


def extract_revenue(text: str) -> str:
    if not text:
        return "Not publicly found"

    # Keep revenue extraction conservative; never estimate a company's revenue.
    money = r"(?:(?:US\$|USD|EUR|GBP|\$|€|£)\s*)?\d[\d,.]*(?:\s*(?:billion|million|thousand|bn|mn|m|k))?"
    patterns = [
        rf"(?:annual\s+)?(?:revenue|turnover|sales)\s*(?:of|is|was|:|=|approximately|around)?\s*({money})",
        rf"({money})\s*(?:in\s+)?(?:annual\s+)?(?:revenue|turnover|sales)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
            if value and not value.endswith("%"):
                return value
    return "Not publicly found"


def extract_page_data(url: str):
    html, final_url, status, content_type = fetch_html(url)
    if html is None:
        return {
            "url": final_url, "status": status, "title": "",
            "text": "", "company_name": "", "socials": {},
            "internal_links": [], "description": "",
        }

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    desc_tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    description = desc_tag.get("content", "").strip() if desc_tag else ""

    org_name = extract_org_name_from_jsonld(soup)
    if not org_name:
        og = soup.find("meta", property="og:site_name")
        if og:
            org_name = og.get("content", "").strip()
    if not org_name:
        app_name = soup.find("meta", attrs={"name": "application-name"})
        if app_name:
            org_name = app_name.get("content", "").strip()

    socials = {}
    for label in ["LinkedIn", "Facebook", "Instagram", "X / Twitter", "Other Socials"]:
        socials[label] = ""

    for anchor in soup.find_all("a", href=True):
        href = urljoin(final_url, anchor["href"].strip()).split("#", 1)[0]
        social_type = classify_social(href)
        if social_type and not socials[social_type]:
            socials[social_type] = href

    current_domain = (urlsplit(final_url).hostname or "").lower()
    internal_links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(final_url, anchor["href"].strip()).split("#", 1)[0]
        parsed = urlsplit(href)
        label = anchor.get_text(" ", strip=True).lower()
        path = parsed.path.lower()
        if (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").lower() == current_domain
            and any(word in (label + " " + path) for word in RELEVANT_PATH_WORDS)
        ):
            if href not in internal_links:
                internal_links.append(href)

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

    return {
        "url": final_url,
        "status": status,
        "title": title,
        "text": text,
        "company_name": org_name,
        "socials": socials,
        "internal_links": internal_links,
        "description": description,
    }


def research_one_company(candidate: dict):
    website = candidate["website"]
    domain = candidate["domain"]
    pages = []
    errors = []

    # No user-facing pages-per-company control. Automatically check the homepage
    # and a few relevant internal pages when available.
    urls_to_check = [website]
    try:
        first = extract_page_data(website)
        pages.append(first)
        for link in first.get("internal_links", []):
            if link not in urls_to_check:
                urls_to_check.append(link)
        for link in urls_to_check[1:4]:
            try:
                pages.append(extract_page_data(link))
                time.sleep(0.15)
            except Exception as exc:
                errors.append(str(exc))
    except Exception as exc:
        errors.append(str(exc))

    company_name = ""
    for page in pages:
        if page.get("company_name"):
            company_name = page["company_name"]
            break
    if not company_name:
        for page in pages:
            if page.get("title"):
                company_name = page["title"]
                break
    company_name = clean_company_name(company_name, domain)

    socials = {label: "" for label in ["LinkedIn", "Facebook", "Instagram", "X / Twitter", "Other Socials"]}
    all_text = []
    for page in pages:
        all_text.append(page.get("text", ""))
        for label, url in page.get("socials", {}).items():
            if url and not socials.get(label):
                socials[label] = url

    # One Python search pass per company to look for publicly reported revenue
    # and official social-profile links. No Gemini request is made here.
    extra_results, search_error = search_web(
        f'"{company_name}" "{domain}" (revenue OR turnover OR annual sales OR LinkedIn OR Facebook OR Instagram)',
        max_results=8,
        page=1,
        region="us-en",
    )
    if search_error:
        errors.append(search_error)

    revenue_text = " ".join(all_text)
    revenue_candidates = [revenue_text]
    for result in extra_results:
        revenue_candidates.append(result.get("snippet", ""))
        revenue_candidates.append(result.get("title", ""))

        result_url = result.get("url", "")
        social_type = classify_social(result_url)
        if social_type and not socials.get(social_type):
            title_and_snippet = (
                result.get("title", "") + " " + result.get("snippet", "")
            ).lower()
            if company_name.lower() in title_and_snippet or domain.lower() in title_and_snippet:
                socials[social_type] = result_url

    revenue = "Not publicly found"
    for evidence in revenue_candidates:
        found = extract_revenue(evidence)
        if found != "Not publicly found":
            revenue = found
            break

    return {
        "Company Name": company_name,
        "Website": website,
        "Revenue": revenue,
        "LinkedIn": socials["LinkedIn"],
        "Facebook": socials["Facebook"],
        "Instagram": socials["Instagram"],
        "X / Twitter": socials["X / Twitter"],
        "Other Socials": socials["Other Socials"],
        "Source Notes": " | ".join(
            [page.get("url", "") for page in pages if page.get("url")]
        ),
        "Search Evidence": " | ".join(
            [r.get("url", "") for r in extra_results if r.get("url")]
        ),
        "Errors": " | ".join(errors),
    }


# ============================================================
# GEMINI: OPTIONAL CLIENT-BRIEF BREAKDOWN ONLY
# ============================================================

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY", "")
    try:
        api_key = api_key or st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        pass
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def analyze_client_brief(client, model: str, brief: str, location: str):
    from google.genai import types

    prompt = f"""
You are a B2B prospect-research planner. Your ONLY job is to translate the
client's instructions into practical web search queries and targeting criteria.
Do not find companies or contacts. Python will perform all web research.

CLIENT INSTRUCTIONS:
{brief}

LOCATION OVERRIDE:
{location or 'Use the location stated in the client instructions.'}

Return valid JSON only with:
{{
  "brief_summary": "Plain-language summary of what the client wants",
  "industry_or_business_type": ["..."],
  "location": ["..."],
  "company_criteria": ["..."],
  "decision_maker_or_company_signals": ["..."],
  "search_queries": ["five to eight useful search-engine queries"]
}}

Keep every requirement faithful to the client's brief. Do not add unsupported
company-size, industry, location, revenue, or job-title requirements.
The output is only a plan for Python. Do not invent any company or person.
"""

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            max_output_tokens=1600,
        ),
    )

    data = json.loads(response.text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Gemini did not return a valid research plan.")
    if not isinstance(data.get("search_queries"), list):
        data["search_queries"] = []
    return data


def show_gemini_error(exc):
    message = str(exc)
    low = message.lower()
    if "429" in low or "resource_exhausted" in low or "quota" in low:
        st.error(
            "Gemini quota/rate limit reached. This only affects AI-assisted mode. "
            "Switch to Direct Extraction to search without Gemini."
        )
        st.markdown(
            "[Check API usage](https://aistudio.google.com/usage) · "
            "[Check rate limits](https://aistudio.google.com/rate-limit)"
        )
    elif "403" in low or "permission_denied" in low:
        st.error("Gemini API access denied. Check the API key and project access.")
    elif "404" in low or "not_found" in low:
        st.error("Gemini model not found. Check the model name in app settings.")
    else:
        st.error(f"Gemini brief analysis failed: {message}")


# ============================================================
# RESULTS TABLE: ONE COMPANY PER ROW
# ============================================================

def display_company_results(rows, prefix):
    output_columns = [
        "Company Name", "Website", "Revenue", "LinkedIn", "Facebook",
        "Instagram", "X / Twitter", "Other Socials",
    ]
    evidence_columns = ["Source Notes", "Search Evidence", "Errors"]

    df = pd.DataFrame(rows)
    for column in output_columns + evidence_columns:
        if column not in df.columns:
            df[column] = ""

    # Exactly one row per company/domain; output columns are spreadsheet-friendly.
    if "Website" in df.columns:
        df = df.drop_duplicates(subset=["Website"], keep="first")

    display_df = df[output_columns].fillna("")

    st.success(f"Research complete: {len(display_df)} company row(s) found.")
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Website": st.column_config.LinkColumn("Website"),
            "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
            "Facebook": st.column_config.LinkColumn("Facebook"),
            "Instagram": st.column_config.LinkColumn("Instagram"),
            "X / Twitter": st.column_config.LinkColumn("X / Twitter"),
            "Other Socials": st.column_config.LinkColumn("Other Socials"),
        },
    )

    csv_data = display_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download Excel-style CSV",
        csv_data,
        f"{prefix}_companies.csv",
        "text/csv",
        use_container_width=True,
    )

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        display_df.to_excel(writer, index=False, sheet_name="Companies")
    st.download_button(
        "Download Excel file",
        excel_buffer.getvalue(),
        f"{prefix}_companies.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    with st.expander("View source evidence (optional)"):
        st.dataframe(
            df[["Company Name", "Website"] + evidence_columns],
            use_container_width=True,
            hide_index=True,
        )


# ============================================================
# TOOL 1: BULK WEBSITE EXTRACTOR
# ============================================================

def bulk_extractor_page():
    st.markdown(
        '<div class="hero"><h1>Bulk Website Extractor</h1>'
        '<p>Paste website URLs. Python extracts company name, website, revenue mentions and social links. No AI is used.</p></div>',
        unsafe_allow_html=True,
    )

    urls_text = st.text_area(
        "Website URLs (one per line)",
        height=180,
        placeholder="https://company1.com\nhttps://company2.com\ncompany3.com",
        key="bulk_urls",
    )
    uploaded = st.file_uploader(
        "Or upload a TXT/CSV URL list",
        type=["txt", "csv"],
        key="bulk_upload",
    )
    if uploaded:
        urls_text = (urls_text + "\n" + uploaded.getvalue().decode("utf-8", errors="ignore")).strip()

    if not DDGS_AVAILABLE:
        st.warning("Web search is unavailable. Website name/social extraction still works. Check requirements.txt for ddgs.")

    if st.button("Extract company details", type="primary", use_container_width=True, key="bulk_run"):
        urls = []
        invalid = []
        for line in urls_text.splitlines():
            candidate = line.strip().split(",", 1)[0].strip().strip('"').strip("'")
            if not candidate or candidate.lower() in {"url", "website", "domain"}:
                continue
            try:
                normalized = normalize_url(candidate)
                if normalized not in urls:
                    urls.append(normalized)
            except ValueError:
                invalid.append(candidate)

        if not urls:
            st.error("Enter at least one valid website URL.")
            return

        rows = []
        progress = st.progress(0, text="Extracting company details...")
        for index, url in enumerate(urls, start=1):
            progress.progress((index - 1) / len(urls), text=f"Checking {url}")
            domain = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            try:
                row = research_one_company({"website": root_url(url), "domain": domain})
                rows.append(row)
            except Exception as exc:
                rows.append({
                    "Company Name": domain,
                    "Website": root_url(url),
                    "Revenue": "Not publicly found",
                    "LinkedIn": "", "Facebook": "", "Instagram": "",
                    "X / Twitter": "", "Other Socials": "",
                    "Source Notes": root_url(url),
                    "Search Evidence": "",
                    "Errors": str(exc),
                })
        progress.progress(1.0, text="Done")
        st.session_state["bulk_company_rows"] = rows
        st.session_state["bulk_invalid_urls"] = invalid

    rows = st.session_state.get("bulk_company_rows", [])
    invalid = st.session_state.get("bulk_invalid_urls", [])
    if invalid:
        st.warning(f"Skipped {len(invalid)} invalid URL(s).")
    if rows:
        display_company_results(rows, "bulk_website_data")
    else:
        st.info("Add website URLs and click Extract company details.")


# ============================================================
# TOOL 2: AI COMPANY RESEARCH
# TWO MODES, ONE START BUTTON
# ============================================================

def ai_company_research_page():
    st.markdown(
        '<div class="hero"><h1>AI Company Research</h1>'
        '<p>Choose AI-assisted brief understanding or direct extraction. Python always performs the actual company research.</p></div>',
        unsafe_allow_html=True,
    )

    instructions = st.text_area(
        "Client instructions / job description",
        height=190,
        placeholder=(
            "Example: Find 80 US-based electrical contractors in Texas and Florida. "
            "I need company name, official website, annual revenue and LinkedIn/Facebook/Instagram links."
        ),
        key="research_instructions",
    )

    mode = st.radio(
        "How should the tool process this brief?",
        [
            "AI check + then extract",
            "Direct extract (no AI)",
        ],
        horizontal=True,
        index=1,
        key="research_mode",
        help="AI mode uses Gemini once to understand the instructions. Direct mode sends your instructions straight to the Python search engine.",
    )

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        requested_count = st.number_input(
            "How many companies do you need?",
            min_value=1,
            max_value=None,
            value=50,
            step=1,
            key="requested_company_count",
            help="No fixed app limit. Enter the number of companies you want to research.",
        )
    with col2:
        location = st.text_input(
            "Location override (optional)",
            placeholder="United States",
            key="research_location",
        )
    with col3:
        if mode == "AI check + then extract":
            model = st.text_input(
                "Gemini model",
                value=DEFAULT_GEMINI_MODEL,
                key="research_gemini_model",
            )
        else:
            st.caption("Direct mode: Gemini is not called. Your instructions go directly to Python web search.")
            model = DEFAULT_GEMINI_MODEL

    if st.button(
        "Start research",
        type="primary",
        use_container_width=True,
        key="research_start",
    ):
        if not instructions.strip():
            st.error("Paste the client's instructions first.")
            return

        count = int(requested_count)
        queries = []
        plan = None

        if mode == "AI check + then extract":
            try:
                client = get_gemini_client()
                if client is None:
                    st.error("GEMINI_API_KEY is missing in Streamlit Cloud → App settings → Secrets.")
                    return
                with st.spinner("Gemini is understanding the client instructions..."):
                    plan = analyze_client_brief(
                        client,
                        model.strip() or DEFAULT_GEMINI_MODEL,
                        instructions,
                        location,
                    )
                queries = [str(q).strip() for q in plan.get("search_queries", []) if str(q).strip()]
                if location and not any(location.lower() in q.lower() for q in queries):
                    queries = [f"{q} {location}" for q in queries]
                if not queries:
                    queries = direct_search_queries(instructions, location)
                st.session_state["last_brief_plan"] = plan
            except Exception as exc:
                show_gemini_error(exc)
                return
        else:
            queries = direct_search_queries(instructions, location)
            st.session_state["last_brief_plan"] = None

        st.write(f"**Search mode:** {mode}")
        if plan:
            with st.expander("Gemini's brief breakdown", expanded=True):
                st.write(plan.get("brief_summary", ""))
                st.write("**Industry / business type:**", plan.get("industry_or_business_type", []))
                st.write("**Location:**", plan.get("location", []))
                st.write("**Company criteria:**", plan.get("company_criteria", []))
                st.write("**Person / company signals:**", plan.get("decision_maker_or_company_signals", []))
                st.write("**Search queries:**", queries)

        if not DDGS_AVAILABLE:
            st.error(f"Python web search package is not available: {DDGS_IMPORT_ERROR}. Install ddgs in requirements.txt and redeploy.")
            return

        candidates, search_results, errors = collect_company_candidates(
            queries,
            count,
            location,
        )

        if not candidates:
            st.error("No company websites were returned by web search. Try adding a clearer industry, location or business type to the instructions.")
            if errors:
                with st.expander("Search errors"):
                    st.write(errors)
            return

        company_rows = []
        progress = st.progress(0, text="Opening company websites and collecting details...")
        status = st.empty()

        for index, candidate in enumerate(candidates, start=1):
            status.write(f"Researching company {index} of {len(candidates)}: {candidate['domain']}")
            progress.progress((index - 1) / len(candidates), text=f"Researching {candidate['domain']}")
            try:
                company_rows.append(research_one_company(candidate))
            except Exception as exc:
                company_rows.append({
                    "Company Name": candidate["domain"],
                    "Website": candidate["website"],
                    "Revenue": "Not publicly found",
                    "LinkedIn": "", "Facebook": "", "Instagram": "",
                    "X / Twitter": "", "Other Socials": "",
                    "Source Notes": candidate["website"],
                    "Search Evidence": candidate["search_result"].get("url", ""),
                    "Errors": str(exc),
                })
            time.sleep(0.15)

        progress.progress(1.0, text="Company research completed")
        status.empty()

        st.session_state["research_company_rows"] = company_rows
        st.session_state["research_search_errors"] = errors
        st.session_state["research_search_sources"] = search_results
        st.session_state["research_count_requested"] = count
        st.session_state["research_mode_used"] = mode

        if len(company_rows) < count:
            st.warning(
                f"Search found {len(company_rows)} suitable company websites out of {count} requested. "
                "The tool does not invent companies to fill the target. Try broader search wording if you need more."
            )

    rows = st.session_state.get("research_company_rows", [])
    if rows:
        st.subheader("Company research results — one company per row")
        display_company_results(rows, "company_research")
        errors = st.session_state.get("research_search_errors", [])
        if errors:
            with st.expander("Search warnings"):
                st.write(errors)


# ============================================================
# SIDEBAR AND APP ROUTER
# ============================================================

with st.sidebar:
    st.markdown("## 🔎 Research Suite")
    selected_tool = st.radio(
        "Choose a tool",
        ["Bulk Website Extractor", "AI Company Research"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("Bulk Website Extractor: Python only, no Gemini.")
    st.caption("AI Company Research: optional Gemini brief breakdown, Python extraction.")

if selected_tool == "Bulk Website Extractor":
    bulk_extractor_page()
else:
    ai_company_research_page()

st.divider()
st.caption(
    "Revenue is shown only when a public source provides a detectable figure; otherwise it is marked Not publicly found. "
    "Social links are collected from official website links or matching search results. "
    "This app accesses public pages only and does not bypass logins, CAPTCHAs or paywalls. "
    "Verify revenue and social profiles against their source before client delivery."
)
