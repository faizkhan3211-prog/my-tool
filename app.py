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
from ddgs import DDGS
from google import genai
from google.genai import types


# =========================================================
# APP CONFIGURATION
# =========================================================

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
          padding-top: 1.5rem;
          padding-bottom: 3rem;
          max-width: 1500px;
      }
      [data-testid="stSidebar"] {
          border-right: 1px solid rgba(128,128,128,.18);
      }
      .hero {
          padding: 1.3rem 1.5rem;
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
          font-size: 1.9rem;
      }
      .hero p {
          margin: 0;
          opacity: .78;
      }
      div[data-testid="stMetric"] {
          border: 1px solid rgba(128,128,128,.2);
          padding: 12px 14px;
          border-radius: 12px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

APP_USER_AGENT = (
    "LeadResearchStreamlit/1.0 "
    "(public-page research; respectful request rate)"
)

REQUEST_TIMEOUT = 18
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_WEBSITES_PER_RUN = 50


# =========================================================
# COMMON FUNCTIONS
# =========================================================

def normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()

    if not value:
        raise ValueError("Empty URL")

    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value

    parsed = urlsplit(value)

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS are supported.")

    if not parsed.hostname:
        raise ValueError(f"Invalid website URL: {raw_url}")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not accepted.")

    if not parsed.path:
        parsed = parsed._replace(path="/")

    return urlunsplit(parsed)


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP/HTTPS destinations are allowed.")

    if (
        host.lower() in {"localhost", "localhost.localdomain"}
        or host.lower().endswith(".local")
    ):
        raise ValueError("Local network addresses are blocked.")

    try:
        resolved = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except (socket.gaierror, ValueError) as exc:
        raise ValueError(f"Could not resolve website: {exc}") from exc

    addresses = {item[4][0] for item in resolved}

    if not addresses:
        raise ValueError("Website hostname did not resolve.")

    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])

        if not ip.is_global:
            raise ValueError(
                "Private, reserved, or local network addresses are blocked."
            )


def fetch_public_page(url: str):
    current_url = url
    redirect_codes = {301, 302, 303, 307, 308}

    for _ in range(6):

        validate_public_url(current_url)

        response = requests.get(
            current_url,
            headers={
                "User-Agent": APP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml"
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

            current_url = urljoin(current_url, location)
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
                    content_type
                )

            body = bytearray()

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if not chunk:
                    continue

                body.extend(chunk)

                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        "Page exceeds the 5 MB safety limit."
                    )

            encoding = response.encoding or "utf-8"

            html = bytes(body).decode(
                encoding,
                errors="replace"
            )

            return (
                html,
                response.url,
                response.status_code,
                content_type
            )

        finally:
            response.close()

    raise ValueError("Too many redirects.")


def extract_json_ld_contacts(soup):

    emails = set()
    phones = set()

    def walk(node):

        if isinstance(node, dict):

            for key, value in node.items():

                key_l = str(key).lower()

                if key_l in {"email", "emailaddress"}:

                    if isinstance(value, str):
                        emails.add(
                            value.replace("mailto:", "").strip()
                        )

                elif key_l in {"telephone", "phone"}:

                    if isinstance(value, str):
                        phones.add(value.strip())

                walk(value)

        elif isinstance(node, list):

            for child in node:
                walk(child)

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):

        try:

            data = json.loads(
                script.string or script.get_text()
            )

            walk(data)

        except (json.JSONDecodeError, TypeError):
            continue

    return emails, phones


def extract_page_data(url: str):

    html, final_url, status, content_type = fetch_public_page(url)

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
        }

    soup = BeautifulSoup(html, "html.parser")

    # Extract structured data before removing scripts.
    jsonld_emails, jsonld_phones = extract_json_ld_contacts(soup)

    for tag in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "iframe"
    ]):
        tag.decompose()

    title = (
        soup.title.get_text(" ", strip=True)
        if soup.title else ""
    )

    description_tag = soup.find(
        "meta",
        attrs={"name": re.compile("description", re.I)}
    )

    description = (
        description_tag.get("content", "").strip()
        if description_tag else ""
    )

    text = re.sub(
        r"\s+",
        " ",
        soup.get_text(" ", strip=True)
    ).strip()

    emails = set(
        re.findall(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            text,
            flags=re.I
        )
    )

    phones = set()

    # Mailto links
    for anchor in soup.select('a[href^="mailto:"]'):

        email = (
            anchor.get("href", "")
            .replace("mailto:", "")
            .split("?")[0]
            .strip()
        )

        if email:
            emails.add(email)

    # Telephone links
    for anchor in soup.select('a[href^="tel:"]'):

        phone = (
            anchor.get("href", "")
            .replace("tel:", "")
            .strip()
        )

        if phone:
            phones.add(phone)

    # Visible phone numbers
    for phone in re.findall(
        r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)",
        text
    ):

        normalized = re.sub(r"\s+", " ", phone).strip()

        digits = re.sub(r"\D", "", normalized)

        if 8 <= len(digits) <= 16:
            phones.add(normalized)

    emails.update(jsonld_emails)
    phones.update(jsonld_phones)

    # Find internal company pages.
    current_domain = urlsplit(final_url).netloc.lower()

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
        "who-we-are",
    )

    links = []

    for anchor in soup.find_all("a", href=True):

        href = urljoin(
            final_url,
            anchor["href"].strip()
        ).split("#", 1)[0]

        parsed_link = urlsplit(href)

        label = anchor.get_text(
            " ",
            strip=True
        ).lower()

        path = parsed_link.path.lower()

        if (
            parsed_link.scheme in {"http", "https"}
            and parsed_link.netloc.lower() == current_domain
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
    }


def scrape_website(start_url: str, max_pages: int = 6):

    visited = set()
    queue = [start_url]
    pages = []

    start_domain = urlsplit(start_url).netloc.lower()

    while queue and len(pages) < max_pages:

        current_url = queue.pop(0)

        if current_url in visited:
            continue

        visited.add(current_url)

        try:

            page_data = extract_page_data(current_url)
            pages.append(page_data)

            for link in page_data.get("links", []):

                if (
                    urlsplit(link).netloc.lower() == start_domain
                    and link not in visited
                ):
                    queue.append(link)

        except Exception as exc:

            pages.append({
                "url": current_url,
                "status": "Error",
                "title": "",
                "description": str(exc),
                "text": "",
                "emails": set(),
                "phones": set(),
                "links": [],
            })

        time.sleep(0.6)

    return pages


# =========================================================
# WEB SEARCH
# =========================================================

def search_web(
    query: str,
    max_results: int = 5,
    backend: str = "auto"
):

    results = []

    try:

        raw_results = DDGS().text(
            query,
            max_results=max_results,
            backend=backend
        )

        for item in raw_results:

            results.append({
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            })

    except Exception as exc:

        results.append({
            "title": "Search error",
            "url": "",
            "snippet": str(exc),
        })

    return results


# =========================================================
# GEMINI API
# =========================================================

def get_gemini_client():

    api_key = os.getenv("GEMINI_API_KEY", "")

    try:
        api_key = api_key or st.secrets.get(
            "GEMINI_API_KEY",
            ""
        )
    except Exception:
        pass

    if not api_key:
        return None

    return genai.Client(api_key=api_key)


def parse_model_json(text: str):

    content = (text or "").strip()

    content = re.sub(
        r"^```(?:json)?\s*",
        "",
        content,
        flags=re.I
    )

    content = re.sub(
        r"\s*```$",
        "",
        content
    )

    try:
        return json.loads(content)

    except json.JSONDecodeError:
        pass

    # Try JSON object
    start = content.find("{")
    end = content.rfind("}")

    if start >= 0 and end > start:

        try:
            return json.loads(
                content[start:end + 1]
            )
        except json.JSONDecodeError:
            pass

    # Try JSON array
    start = content.find("[")
    end = content.rfind("]")

    if start >= 0 and end > start:

        try:
            return json.loads(
                content[start:end + 1]
            )
        except json.JSONDecodeError:
            pass

    return None


def extract_grounding_sources(response):

    sources = []
    seen = set()

    try:

        candidates = response.candidates or []

        if not candidates:
            return sources

        metadata = candidates[0].grounding_metadata

        chunks = (
            metadata.grounding_chunks
            if metadata else []
        )

        for chunk in chunks or []:

            web_source = getattr(
                chunk,
                "web",
                None
            )

            if not web_source:
                continue

            uri = getattr(
                web_source,
                "uri",
                ""
            ) or ""

            title = getattr(
                web_source,
                "title",
                ""
            ) or uri

            if uri and uri not in seen:

                seen.add(uri)

                sources.append({
                    "Source title": title,
                    "Source URL": uri
                })

    except Exception:
        pass

    return sources


# =========================================================
# SIDEBAR NAVIGATION
# =========================================================

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
        "Two separate tools. Their inputs and results remain independent."
    )

    st.caption(
        "Streamlit • Python • Gemini • Public web search"
    )


# =========================================================
# TOOL 1: BULK WEBSITE EXTRACTOR
# =========================================================

def bulk_extractor_page():

    st.markdown(
        '<div class="hero">'
        '<h1>Bulk Website Extractor</h1>'
        '<p>Extract public website text, available contact details, '
        'and relevant company pages.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([2, 1])

    with left:

        urls_input = st.text_area(
            "Website URLs (one URL per line)",
            height=180,
            placeholder=(
                "https://company1.com\n"
                "https://company2.com\n"
                "company3.com"
            ),
            help="You can paste URLs or upload a TXT/CSV file.",
        )

    with right:

        uploaded_file = st.file_uploader(
            "Upload URL list",
            type=["txt", "csv"],
            key="bulk_url_upload"
        )

        max_pages = st.slider(
            "Maximum pages per website",
            1,
            15,
            6
        )

        do_web_search = st.checkbox(
            "Also search the web for each company",
            value=True
        )

        search_backend_label = st.selectbox(
            "Search provider",
            ["Auto", "Google", "Bing"],
            disabled=not do_web_search,
        )

        search_limit = st.slider(
            "Search results per website",
            1,
            8,
            4,
            disabled=not do_web_search
        )

    if uploaded_file:

        uploaded_text = uploaded_file.getvalue().decode(
            "utf-8",
            errors="ignore"
        )

        urls_input = (
            urls_input + "\n" + uploaded_text
        ).strip()

    if st.button(
        "🚀 Extract Website Data",
        type="primary",
        use_container_width=True
    ):

        raw_lines = [
            line.strip()
            for line in urls_input.splitlines()
            if line.strip()
        ]

        raw_lines = [
            line for line in raw_lines
            if line.lower() not in {
                "url",
                "website",
                "websites",
                "domain"
            }
        ]

        urls = []
        invalid_lines = []

        for line in raw_lines:

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
                invalid_lines.append(line)

        if not urls:

            st.error(
                "Enter at least one valid website URL."
            )
            return

        if len(urls) > MAX_WEBSITES_PER_RUN:

            st.error(
                f"Maximum {MAX_WEBSITES_PER_RUN} websites "
                "are allowed per run. Split your list into batches."
            )
            return

        search_backend = {
            "Auto": "auto",
            "Google": "google",
            "Bing": "bing",
        }[search_backend_label]

        all_pages = []
        all_search_results = []

        progress = st.progress(
            0,
            text="Preparing extraction..."
        )

        for index, website_url in enumerate(urls, start=1):

            progress.progress(
                (index - 1) / len(urls),
                text=f"Processing {website_url}"
            )

            website_pages = scrape_website(
                website_url,
                max_pages=max_pages
            )

            for page_data in website_pages:

                page_data["input_website"] = website_url

                page_data["emails"] = sorted(
                    page_data.get("emails", [])
                )

                page_data["phones"] = sorted(
                    page_data.get("phones", [])
                )

                all_pages.append(page_data)

            if do_web_search:

                domain = (
                    urlsplit(website_url)
                    .netloc.lower()
                    .replace("www.", "")
                )

                company_guess = (
                    domain.split(":")[0]
                    .split(".")[0]
                    .replace("-", " ")
                )

                web_results = search_web(
                    f'"{company_guess}" {domain} '
                    "contact about team leadership",
                    max_results=search_limit,
                    backend=search_backend,
                )

                for result in web_results:

                    result["input_website"] = website_url

                    all_search_results.append(result)

        progress.progress(
            1.0,
            text="Extraction finished."
        )

        st.session_state["bulk_pages"] = all_pages

        st.session_state["bulk_search_results"] = (
            all_search_results
        )

        st.session_state["bulk_invalid_lines"] = (
            invalid_lines
        )

        st.success(
            f"Completed {len(urls)} website(s), "
            f"with {len(all_pages)} page result(s)."
        )

    pages = st.session_state.get(
        "bulk_pages",
        []
    )

    search_results = st.session_state.get(
        "bulk_search_results",
        []
    )

    invalid_lines = st.session_state.get(
        "bulk_invalid_lines",
        []
    )

    if invalid_lines:

        with st.expander("Lines that were skipped"):
            st.write(invalid_lines)

    if not pages and not search_results:

        st.info(
            "Paste multiple public website URLs above "
            "and select Extract Website Data."
        )
        return

    all_emails = sorted({
        email
        for page in pages
        for email in page.get("emails", [])
    })

    all_phones = sorted({
        phone
        for page in pages
        for phone in page.get("phones", [])
    })

    unique_sites = {
        page.get("input_website")
        for page in pages
    }

    metric1, metric2, metric3, metric4 = st.columns(4)

    metric1.metric(
        "Pages processed",
        len(pages)
    )

    metric2.metric(
        "Websites",
        len(unique_sites)
    )

    metric3.metric(
        "Emails found",
        len(all_emails)
    )

    metric4.metric(
        "Phone numbers",
        len(all_phones)
    )

    page_rows = []

    for page in pages:

        page_rows.append({

            "Input Website": page.get(
                "input_website",
                ""
            ),

            "Page URL": page.get(
                "url",
                ""
            ),

            "Page Title": page.get(
                "title",
                ""
            ),

            "Status": page.get(
                "status",
                ""
            ),

            "Description": page.get(
                "description",
                ""
            ),

            "Emails": ", ".join(
                page.get("emails", [])
            ),

            "Phones": ", ".join(
                page.get("phones", [])
            ),

            "Extracted Text": page.get(
                "text",
                ""
            ),
        })

    pages_df = pd.DataFrame(page_rows)

    st.subheader("Extracted website pages")

    if not pages_df.empty:

        st.dataframe(
            pages_df[
                [
                    "Input Website",
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

    contact_rows = []

    for page in pages:

        for email in page.get("emails", []):

            contact_rows.append({

                "Website": page.get(
                    "input_website",
                    ""
                ),

                "Email": email,

                "Phone": ", ".join(
                    page.get("phones", [])
                ),

                "Source Page": page.get(
                    "url",
                    ""
                ),

            })

    contacts_df = pd.DataFrame(
        contact_rows,
        columns=[
            "Website",
            "Email",
            "Phone",
            "Source Page"
        ]
    )

    st.subheader("Contact information found")

    if not contacts_df.empty:

        st.dataframe(
            contacts_df,
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "No email addresses were found "
            "on the pages that could be accessed."
        )

    if search_results:

        st.subheader("Public web search results")

        st.dataframe(
            pd.DataFrame(search_results),
            use_container_width=True,
            hide_index=True
        )

    st.subheader("Download extracted data")

    download1, download2, download3 = st.columns(3)

    with download1:

        st.download_button(
            "Download pages CSV",
            pages_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            file_name="website_pages.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with download2:

        st.download_button(
            "Download contacts CSV",
            contacts_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            file_name="website_contacts.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with download3:

        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(
            excel_buffer,
            engine="openpyxl"
        ) as writer:

            pages_df.to_excel(
                writer,
                index=False,
                sheet_name="Website Pages"
            )

            contacts_df.to_excel(
                writer,
                index=False,
                sheet_name="Contacts"
            )

            pd.DataFrame(
                search_results
            ).to_excel(
                writer,
                index=False,
                sheet_name="Web Search"
            )

        st.download_button(
            "Download all as Excel",
            excel_buffer.getvalue(),
            file_name="website_research.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=True,
        )

    with st.expander("View full extracted page text"):

        if pages:

            selected_page_index = st.selectbox(
                "Choose a page",
                range(len(pages)),
                format_func=lambda i: (
                    f'{pages[i].get("input_website", "")} | '
                    f'{pages[i].get("title", "") or pages[i].get("url", "")}'
                ),
            )

            st.text_area(
                "Extracted text",
                value=pages[selected_page_index].get(
                    "text",
                    ""
                ),
                height=300,
                disabled=True,
            )


# =========================================================
# TOOL 2: GEMINI AI COMPANY RESEARCH
# =========================================================

def ai_research_page():

    st.markdown(
        '<div class="hero">'
        '<h1>AI Company Research</h1>'
        '<p>Use Gemini with Google Search grounding to research '
        'matching companies and public decision-maker details.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    client = None

    try:

        client = get_gemini_client()

    except Exception as exc:

        st.error(
            f"Gemini client initialization failed: {exc}"
        )

    if not client:

        st.warning(
            "Gemini API key is missing. Add GEMINI_API_KEY "
            "in Streamlit Cloud Secrets or set it as an "
            "environment variable. Do not put the key in app.py."
        )

    brief = st.text_area(
        "Client brief / Research instructions",
        height=190,
        placeholder=(
            "Example: Find US-based B2B SaaS companies in "
            "the healthcare industry, 11–200 employees. "
            "Identify the relevant sales or marketing decision-makers."
        ),
    )

    col1, col2, col3 = st.columns(3)

    with col1:

        target_region = st.text_input(
            "Target country / region",
            placeholder="United States"
        )

    with col2:

        target_industry = st.text_input(
            "Industry (optional)",
            placeholder="Healthcare SaaS"
        )

    with col3:

        max_contacts = st.number_input(
            "Maximum contacts",
            min_value=1,
            max_value=100,
            value=25,
            step=1
        )

    model_name = st.text_input(
        "Gemini model",
        value="gemini-3.8-flash"
    )

    st.caption(
        "Final contact list shows Full Name and Job Title. "
        "Company and source evidence are available separately. "
        "Email finding is not included."
    )

    if st.button(
        "🧠 Start AI Research",
        type="primary",
        use_container_width=True
    ):

        if not brief.strip():

            st.error(
                "Enter the client brief before starting research."
            )
            return

        if not client:

            st.error(
                "Gemini API key is not configured."
            )
            return

        if not model_name.strip():

            st.error(
                "Enter a Gemini model name."
            )
            return

        prompt = f"""
You are a careful B2B company and decision-maker research assistant.
Use Google Search grounding to find current, publicly available evidence.
Do not rely only on your built-in knowledge.

CLIENT BRIEF:
{brief}

TARGET COUNTRY / REGION: {target_region or "Use the client brief"}
TARGET INDUSTRY: {target_industry or "Use the client brief"}
MAXIMUM CONTACTS: {int(max_contacts)}

RESEARCH RULES:
1. Interpret the ICP carefully and identify companies that genuinely match it.
2. Find real people at those companies only when their full name and current public job title
   are supported by a public web source.
3. Do not guess names, titles, employers, current employment, company size, or facts.
4. Do not generate, infer, or search for email addresses or phone numbers.
5. Return no more than {int(max_contacts)} distinct contacts.
6. Avoid duplicate people and exclude anyone whose name or current title is not supported.
7. Include the actual public source URL for every contact whenever available.
8. Choose job functions relevant to the client brief. If unclear, select appropriate decision-makers
   and briefly explain why.
9. If reliable matches are fewer than requested, return fewer. Never fill the list with guesses.
10. Output valid JSON only. Do not wrap it in Markdown code fences.

Return this JSON structure:
{{
  "summary": "Short explanation of the interpreted ICP and research approach",
  "contacts": [
    {{
      "Full Name": "Person's full name",
      "Job Title": "Current public job title",
      "Company": "Company name",
      "Company Website": "https://company.example",
      "Source URL": "https://public-source.example/profile-or-team-page",
      "Fit Notes": "Short evidence-based reason this person/company matches"
    }}
  ]
}}
"""

        try:

            with st.spinner(
                "Gemini is searching public web sources and checking the brief..."
            ):

                response = client.models.generate_content(

                    model=model_name.strip(),

                    contents=prompt,

                    config=types.GenerateContentConfig(

                        tools=[
                            types.Tool(
                                google_search=types.GoogleSearch()
                            )
                        ],

                        temperature=0.2,
                    ),
                )

            raw_response = response.text or ""

            parsed = parse_model_json(raw_response)

            if isinstance(parsed, list):

                parsed = {
                    "summary": "",
                    "contacts": parsed
                }

            if not isinstance(parsed, dict):

                st.error(
                    "Gemini returned an answer that could not be read as JSON. "
                    "See the raw response below."
                )

                st.session_state["research_contacts"] = []
                st.session_state["research_summary"] = ""
                st.session_state["research_raw"] = raw_response

                st.session_state["research_sources"] = (
                    extract_grounding_sources(response)
                )

            else:

                raw_contacts = parsed.get(
                    "contacts",
                    []
                )

                cleaned_contacts = []
                seen_people = set()

                if isinstance(raw_contacts, list):

                    for item in raw_contacts:

                        if not isinstance(item, dict):
                            continue

                        full_name = str(
                            item.get("Full Name", "")
                        ).strip()

                        job_title = str(
                            item.get("Job Title", "")
                        ).strip()

                        if not full_name or not job_title:
                            continue

                        duplicate_key = (
                            full_name.casefold(),
                            job_title.casefold()
                        )

                        if duplicate_key in seen_people:
                            continue

                        seen_people.add(duplicate_key)

                        cleaned_contacts.append({

                            "Full Name": full_name,

                            "Job Title": job_title,

                            "Company": str(
                                item.get("Company", "")
                            ).strip(),

                            "Company Website": str(
                                item.get("Company Website", "")
                            ).strip(),

                            "Source URL": str(
                                item.get("Source URL", "")
                            ).strip(),

                            "Fit Notes": str(
                                item.get("Fit Notes", "")
                            ).strip(),

                        })

                        if len(cleaned_contacts) >= int(max_contacts):
                            break

                st.session_state["research_contacts"] = (
                    cleaned_contacts
                )

                st.session_state["research_summary"] = str(
                    parsed.get("summary", "")
                )

                st.session_state["research_raw"] = (
                    raw_response
                )

                st.session_state["research_sources"] = (
                    extract_grounding_sources(response)
                )

            st.success(
                "Research request completed."
            )

        except Exception as exc:

            st.error(
                f"Gemini research failed: {exc}"
            )

            st.info(
                "Check the API key, model availability, Gemini API quota, "
                "and whether Google Search grounding is enabled "
                "for your Gemini API project."
            )

    contacts = st.session_state.get(
        "research_contacts",
        []
    )

    summary = st.session_state.get(
        "research_summary",
        ""
    )

    sources = st.session_state.get(
        "research_sources",
        []
    )

    raw_response = st.session_state.get(
        "research_raw",
        ""
    )

    if summary:

        st.subheader(
            "Research interpretation"
        )

        st.write(summary)

    if contacts:

        contacts_df = pd.DataFrame(contacts)

        metric_a, metric_b = st.columns(2)

        metric_a.metric(
            "Contacts returned",
            len(contacts_df)
        )

        metric_b.metric(
            "Companies represented",
            contacts_df["Company"].replace(
                "",
                pd.NA
            ).nunique()
        )

        st.subheader(
            "Contact results"
        )

        st.dataframe(
            contacts_df[
                [
                    "Full Name",
                    "Job Title"
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander(
            "Company research and individual source URLs"
        ):

            st.dataframe(
                contacts_df[
                    [
                        "Company",
                        "Company Website",
                        "Source URL",
                        "Fit Notes"
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Download contact list CSV",

            contacts_df[
                [
                    "Full Name",
                    "Job Title"
                ]
            ].to_csv(
                index=False
            ).encode("utf-8-sig"),

            file_name="researched_contacts.csv",

            mime="text/csv",

            use_container_width=True,
        )

        st.download_button(
            "Download full research CSV",

            contacts_df.to_csv(
                index=False
            ).encode("utf-8-sig"),

            file_name="full_company_research.csv",

            mime="text/csv",

            use_container_width=True,
        )

    if sources:

        with st.expander(
            "Google Search grounding sources"
        ):

            st.dataframe(
                pd.DataFrame(sources),
                use_container_width=True,
                hide_index=True
            )

    if raw_response and not contacts:

        with st.expander(
            "View Gemini raw response"
        ):

            st.text(raw_response)


# =========================================================
# MAIN APP ROUTER
# =========================================================

if selected_tool == "Bulk Website Extractor":

    bulk_extractor_page()

else:

    ai_research_page()


st.divider()

st.caption(
    "Responsible-use note: extract only public pages you are permitted to access. "
    "Respect each website's terms, robots.txt, request limits, and applicable laws. "
    "This app does not log in, bypass CAPTCHAs, or access paywalled/private content. "
    "Extracted and AI-generated information must be checked against the original "
    "source before client delivery."
)
