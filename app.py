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
    .block-container {
        max-width: 1500px;
        padding-top: 1.5rem;
        padding-bottom: 3rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid rgba(128,128,128,.18);
    }
    .hero {
        padding: 1.25rem 1.5rem;
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

USER_AGENT = "LeadResearchApp/1.1"
REQUEST_TIMEOUT = 18
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_WEBSITES = 30
DEFAULT_MODEL = "gemini-3.8-flash"


# ============================================================
# WEBSITE EXTRACTION
# ============================================================

def normalize_url(raw_url):

    value = (raw_url or "").strip()

    if not value:
        raise ValueError("Empty URL")

    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value

    parsed = urlsplit(value)

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP/HTTPS URLs are supported.")

    if not parsed.hostname:
        raise ValueError("Invalid website URL.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are blocked.")

    if not parsed.path:
        parsed = parsed._replace(path="/")

    return urlunsplit(parsed)


def validate_public_url(url):

    parsed = urlsplit(url)
    host = parsed.hostname

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Invalid public website URL.")

    if (
        host.lower() in {"localhost", "localhost.localdomain"}
        or host.lower().endswith(".local")
    ):
        raise ValueError("Local network URLs are blocked.")

    try:

        resolved = socket.getaddrinfo(
            host,
            parsed.port or (
                443 if parsed.scheme.lower() == "https" else 80
            ),
            type=socket.SOCK_STREAM,
        )

    except (socket.gaierror, ValueError, OSError) as exc:
        raise ValueError(f"Could not resolve website: {exc}") from exc

    addresses = {item[4][0] for item in resolved}

    if not addresses:
        raise ValueError("Website host did not resolve.")

    for address in addresses:

        ip = ipaddress.ip_address(
            address.split("%", 1)[0]
        )

        if not ip.is_global:
            raise ValueError("Private or local URLs are blocked.")


def fetch_html(url):

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
                raise ValueError("Missing redirect destination.")

            current = urljoin(current, location)
            continue

        try:

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if "html" not in content_type and "xhtml" not in content_type:
                return None, response.url, response.status_code, content_type

            body = bytearray()

            for chunk in response.iter_content(chunk_size=65536):

                if not chunk:
                    continue

                body.extend(chunk)

                if len(body) > MAX_PAGE_BYTES:
                    raise ValueError("Page exceeds the 5 MB limit.")

            encoding = response.encoding or "utf-8"

            html = bytes(body).decode(
                encoding,
                errors="replace"
            )

            return html, response.url, response.status_code, content_type

        finally:
            response.close()

    raise ValueError("Too many redirects.")


# ============================================================
# STRUCTURED DATA EXTRACTION
# ============================================================

def walk_jsonld(node, found_people, emails, phones):

    if isinstance(node, dict):

        node_type = node.get("@type", "")

        if isinstance(node_type, list):
            types_found = {str(x).lower() for x in node_type}
        else:
            types_found = {str(node_type).lower()}

        name = node.get("name")

        job = (
            node.get("jobTitle")
            or node.get("jobtitle")
            or ""
        )

        if name and ("person" in types_found or job):

            found_people.append({

                "Full Name": str(name).strip(),

                "Job Title": str(job).strip(),

                "Source URL": str(
                    node.get("url")
                    or node.get("sameAs")
                    or ""
                ).strip(),

                "Evidence": "Person information found in website structured data.",

            })

        for key, value in node.items():

            key_lower = str(key).lower()

            if key_lower in {"email", "emailaddress"} and isinstance(value, str):
                emails.add(value.replace("mailto:", "").strip())

            elif key_lower in {"telephone", "phone"} and isinstance(value, str):
                phones.add(value.strip())

            walk_jsonld(
                value,
                found_people,
                emails,
                phones
            )

    elif isinstance(node, list):

        for child in node:
            walk_jsonld(child, found_people, emails, phones)


# ============================================================
# PAGE DATA EXTRACTION
# ============================================================

def extract_page(url):

    html, final_url, status, content_type = fetch_html(url)

    if html is None:

        return {
            "url": final_url,
            "status": status,
            "title": "",
            "description": f"Non-HTML content: {content_type}",
            "text": "",
            "emails": set(),
            "phones": set(),
            "links": [],
            "structured_people": [],
        }

    soup = BeautifulSoup(html, "html.parser")

    emails = set()
    phones = set()
    structured_people = []

    # JSON-LD data
    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):

        try:

            data = json.loads(
                script.string or script.get_text()
            )

            walk_jsonld(
                data,
                structured_people,
                emails,
                phones
            )

        except (json.JSONDecodeError, TypeError, ValueError):
            pass

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

    # Remove unnecessary HTML
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

    # Emails in visible text
    emails.update(
        re.findall(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            text,
            flags=re.I
        )
    )

    # Mailto
    for anchor in soup.select('a[href^="mailto:"]'):

        email = (
            anchor.get("href", "")
            .replace("mailto:", "")
            .split("?", 1)[0]
            .strip()
        )

        if email:
            emails.add(email)

    # Tel links
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

        digits = re.sub(r"\D", "", phone)

        if 8 <= len(digits) <= 16:
            phones.add(re.sub(r"\s+", " ", phone).strip())

    # Find internal pages
    same_domain = urlsplit(final_url).netloc.lower()

    target_words = (
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
        "our-story",
        "meet-the-team",
        "our-team",
        "owners",
    )

    links = []

    for anchor in soup.find_all("a", href=True):

        href = urljoin(
            final_url,
            anchor["href"].strip()
        ).split("#", 1)[0]

        parsed_href = urlsplit(href)

        label = anchor.get_text(
            " ",
            strip=True
        ).lower()

        path = parsed_href.path.lower()

        if (
            parsed_href.scheme in {"http", "https"}
            and parsed_href.netloc.lower() == same_domain
            and any(
                word in (label + " " + path)
                for word in target_words
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
        "structured_people": structured_people,
    }


# ============================================================
# WEBSITE CRAWLER
# ============================================================

def scrape_website(start_url, max_pages=6):

    visited = set()
    queue = [start_url]
    pages = []

    start_host = urlsplit(start_url).netloc.lower()

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
                "structured_people": [],

            })

        time.sleep(0.35)

    return pages


# ============================================================
# STRUCTURED PEOPLE FALLBACK
# ============================================================

def structured_people_fallback(company_packets):

    rows = []
    seen = set()

    for packet in company_packets:

        for person in packet.get("structured_people", []):

            name = str(
                person.get("Full Name", "")
            ).strip()

            title = str(
                person.get("Job Title", "")
            ).strip()

            if not name or not title:
                continue

            key = (
                packet["website"].casefold(),
                name.casefold(),
                title.casefold()
            )

            if key in seen:
                continue

            seen.add(key)

            rows.append({

                "Company": urlsplit(packet["website"]).netloc,

                "Full Name": name,

                "Job Title": title,

                "Source URL": str(
                    person.get("Source URL", "")
                ).strip() or packet["website"],

                "Evidence": str(
                    person.get(
                        "Evidence",
                        "Person and job title appear in website structured data."
                    )
                ),

                "Confidence": "Structured website data; manually verify",

            })

    return rows


# ============================================================
# GOOGLE / WEB SEARCH
# ============================================================

def web_search(query, max_results=5, backend="auto"):

    try:

        results = DDGS(timeout=12).text(
            query=query,
            region="us-en",
            safesearch="moderate",
            max_results=max_results,
            backend=backend,
        )

        return [

            {
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            }

            for item in results

        ]

    except Exception as exc:

        return [{
            "title": "Search unavailable",
            "url": "",
            "snippet": str(exc),
        }]


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

def get_gemini_client():

    api_key = os.getenv("GEMINI_API_KEY", "")

    try:

        api_key = api_key or st.secrets.get(
            "GEMINI_API_KEY",
            ""
        )

    except Exception:
        pass

    return genai.Client(api_key=api_key) if api_key else None


# ============================================================
# GEMINI ERROR HANDLING
# ============================================================

def show_gemini_error(exc):

    message = str(exc)
    lowered = message.lower()

    if (
        "429" in lowered
        or "resource_exhausted" in lowered
        or "quota" in lowered
    ):

        if any(
            term in lowered
            for term in (
                "quota_exceeded",
                "current quota",
                "exceeded your quota",
                "billing",
            )
        ):

            st.error(
                "Gemini quota/billing limit reached. "
                "The API key is being read, but Google is not "
                "allowing another generation request right now."
            )

            st.warning(
                "Code cannot remove a Google-side quota limit. "
                "Check your Google AI Studio project, model limits "
                "and billing. Website extraction and contact "
                "details have still been saved."
            )

        else:

            st.error(
                "Gemini rate limit reached (429). Wait for the "
                "limit window to reset, then run the AI step again."
            )

        st.markdown(
            "[Check API usage](https://aistudio.google.com/usage) | "
            "[Check rate limits](https://aistudio.google.com/rate-limit) | "
            "[Gemini API error guide](https://ai.google.dev/gemini-api/docs/api-errors)"
        )

    elif "403" in lowered or "permission_denied" in lowered:

        st.error(
            "Gemini API permission/billing access was denied. "
            "Check the API key project and Gemini API access."
        )

    elif "404" in lowered or "not_found" in lowered:

        st.error(
            "The selected Gemini model was not found or is unavailable. "
            "Try gemini-3.8-flash or gemini-2.5-flash."
        )

    else:

        st.error(f"Gemini request failed: {message}")


# ============================================================
# JSON PARSING
# ============================================================

def parse_json_response(text):

    raw = (text or "").strip()

    raw = re.sub(
        r"^```(?:json)?\s*",
        "",
        raw,
        flags=re.I
    )

    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)

    except json.JSONDecodeError:
        pass

    for opening, closing in (("{", "}"), ("[", "]")):

        start = raw.find(opening)
        end = raw.rfind(closing)

        if start >= 0 and end > start:

            try:
                return json.loads(raw[start:end + 1])

            except json.JSONDecodeError:
                continue

    return None


# ============================================================
# REMOVE DUPLICATES
# ============================================================

def dedupe_people(rows):

    clean = []
    seen = set()

    for row in rows:

        if not isinstance(row, dict):
            continue

        name = str(
            row.get("Full Name", "")
        ).strip()

        title = str(
            row.get("Job Title", "")
        ).strip()

        if not name or not title:
            continue

        key = (
            name.casefold(),
            title.casefold(),
            str(row.get("Company", "")).casefold()
        )

        if key in seen:
            continue

        seen.add(key)

        clean.append({

            "Company": str(row.get("Company", "")).strip(),

            "Full Name": name,

            "Job Title": title,

            "Source URL": str(
                row.get("Source URL", "")
            ).strip(),

            "Evidence": str(
                row.get("Evidence", row.get("Fit Notes", ""))
            ).strip(),

            "Confidence": str(
                row.get("Confidence", "Needs manual verification")
            ).strip(),

        })

    return clean


# ============================================================
# GEMINI PEOPLE RESEARCH
# ONE REQUEST FOR ALL WEBSITES
# ============================================================

def research_people_batch(
    client,
    model,
    company_packets,
    max_people_per_company=3
):

    evidence_blocks = []

    for packet in company_packets:

        website = packet["website"]

        page_parts = []

        for page in packet.get("pages", [])[:4]:

            if page.get("text"):

                page_parts.append(

                    "PAGE: " + page.get("title", "") + "\n"
                    "URL: " + page.get("url", "") + "\n"
                    "TEXT: " + page.get("text", "")[:1800]

                )

        search_parts = []

        for result in packet.get("search", [])[:5]:

            search_parts.append(

                "TITLE: " + result.get("title", "") + "\n"
                "URL: " + result.get("url", "") + "\n"
                "SNIPPET: " + result.get("snippet", "")[:500]

            )

        structured = packet.get("structured_people", [])[:8]

        page_block = "\n\n".join(page_parts)
        search_block = "\n\n".join(search_parts)

        structured_block = json.dumps(
            structured,
            ensure_ascii=False
        )

        evidence_blocks.append(

            f"\n### COMPANY: {website}\n"
            f"Website page evidence:\n{page_block}\n"
            f"Public web search evidence:\n{search_block}\n"
            f"Structured website people data:\n{structured_block}\n"

        )

    prompt = f"""
You are a cautious B2B decision-maker researcher.
Analyze each company separately.

Use ONLY the company page text, structured data and
public search snippets supplied below.

Do not claim that you performed additional searches.
Do not invent or infer identities.

FOR EACH COMPANY:

- Identify up to {max_people_per_company} real people.
- Prefer Owner, Founder, Co-Founder, CEO, President,
  Managing Director, Partner or General Manager.
- Use a current job title only when evidence supports it.
- Do not use an email username to identify a person.
- Do not confuse similarly named businesses.
- Only return a person when full name AND job title
  are supported by the supplied evidence.
- Return the exact source URL from the supplied evidence.
- Explain briefly what the source says.
- If no verified person exists, return no row.
- Never fabricate names, titles or URLs.

Return JSON only:

{{
  "people": [
    {{
      "Company": "company domain or company name",
      "Full Name": "publicly supported full name",
      "Job Title": "publicly supported title",
      "Source URL": "exact source URL supplied above",
      "Evidence": "short evidence explanation",
      "Confidence": "High / Medium"
    }}
  ],
  "notes": "Brief research notes"
}}

COMPANY EVIDENCE:

{''.join(evidence_blocks)}
"""

    response = client.models.generate_content(

        model=model,

        contents=prompt,

        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),

    )

    parsed = parse_json_response(
        response.text or ""
    )

    if isinstance(parsed, list):

        raw_people = parsed
        notes = ""

    elif isinstance(parsed, dict):

        raw_people = parsed.get("people", [])
        notes = str(parsed.get("notes", ""))

    else:

        return [], "Gemini returned invalid JSON."

    # Validate source URLs against actual collected sources.
    allowed_sources = set()

    for packet in company_packets:

        for page in packet.get("pages", []):

            if page.get("url"):

                allowed_sources.add(
                    str(page["url"]).strip().rstrip("/")
                )

        for result in packet.get("search", []):

            if result.get("url"):

                allowed_sources.add(
                    str(result["url"]).strip().rstrip("/")
                )

        for person in packet.get("structured_people", []):

            if person.get("Source URL"):

                allowed_sources.add(
                    str(person["Source URL"]).strip().rstrip("/")
                )

    validated_people = []

    for row in raw_people if isinstance(raw_people, list) else []:

        if not isinstance(row, dict):
            continue

        source_url = str(
            row.get("Source URL", "")
        ).strip().rstrip("/")

        if source_url and source_url in allowed_sources:
            validated_people.append(row)

    return dedupe_people(validated_people), notes


# ============================================================
# GEMINI GROUNDING SOURCES
# ============================================================

def extract_grounding_sources(response):

    sources = []
    seen = set()

    try:

        candidates = response.candidates or []

        if not candidates:
            return sources

        metadata = candidates[0].grounding_metadata

        for chunk in (
            metadata.grounding_chunks if metadata else []
        ) or []:

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


# ============================================================
# SIDEBAR
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
        "Two separate tools. Their inputs and results stay separate."
    )

    st.caption(
        "Streamlit • Python • Gemini • Web search"
    )


# ============================================================
# TOOL 1: BULK WEBSITE EXTRACTOR + PERSON FINDER
# ============================================================

def bulk_extractor_page():

    st.markdown(
        '<div class="hero">'
        '<h1>Bulk Website Extractor</h1>'
        '<p>Extract website content, contacts and publicly supported decision-makers.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([2, 1])

    with left:

        urls_text = st.text_area(
            "Website URLs (one per line)",
            height=170,
            placeholder=(
                "https://company1.com\n"
                "https://company2.com\n"
                "company3.com"
            ),
        )

    with right:

        uploaded = st.file_uploader(
            "Upload URL list",
            type=["txt", "csv"],
            key="bulk_urls"
        )

        max_pages = st.slider(
            "Maximum pages per website",
            1,
            12,
            6
        )

        search_people = st.checkbox(
            "Search web for company owners / decision-makers",
            value=True
        )

        find_people = st.checkbox(
            "Use Gemini to identify people",
            value=True
        )

        people_per_company = st.slider(
            "Maximum people per company",
            1,
            5,
            3,
            disabled=not find_people
        )

        gemini_model = st.text_input(
            "Gemini model",
            value=DEFAULT_MODEL,
            key="bulk_gemini_model"
        )

    if uploaded:

        file_text = uploaded.getvalue().decode(
            "utf-8",
            errors="ignore"
        )

        urls_text = (
            urls_text + "\n" + file_text
        ).strip()

    st.caption(
        "Person results require public evidence. "
        "Unknown names and titles are never guessed."
    )

    if st.button(
        "🚀 Extract websites and find people",
        type="primary",
        use_container_width=True
    ):

        raw_lines = [
            x.strip()
            for x in urls_text.splitlines()
            if x.strip()
        ]

        raw_lines = [
            x for x in raw_lines
            if x.lower() not in {
                "url", "website", "websites", "domain"
            }
        ]

        urls = []
        invalid = []

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
                invalid.append(line)

        if not urls:

            st.error("Enter at least one valid website URL.")
            return

        if len(urls) > MAX_WEBSITES:

            st.error(
                f"Maximum {MAX_WEBSITES} websites per run. "
                "Split your list into smaller batches."
            )
            return

        client = None

        if find_people:

            try:
                client = get_gemini_client()

            except Exception as exc:
                st.error(f"Gemini initialization failed: {exc}")

        all_pages = []
        all_search = []
        packets = []

        progress = st.progress(
            0,
            text="Starting website extraction..."
        )

        status_box = st.empty()

        for index, website in enumerate(urls, start=1):

            progress.progress(
                (index - 1) / len(urls),
                text=f"Extracting {website}"
            )

            status_box.write(
                f"Processing website {index} of {len(urls)}: {website}"
            )

            pages = scrape_website(
                website,
                max_pages=max_pages
            )

            for page in pages:

                page["Input Website"] = website

                page["emails"] = sorted(
                    page.get("emails", [])
                )

                page["phones"] = sorted(
                    page.get("phones", [])
                )

                all_pages.append(page)

            search_results = []

            if search_people:

                host = (
                    urlsplit(website)
                    .netloc.lower()
                    .replace("www.", "")
                )

                company_guess = next(
                    (
                        p.get("title", "")
                        for p in pages
                        if p.get("title")
                    ),
                    ""
                )

                query = (
                    f'"{host}" '
                    '(owner OR founder OR CEO OR president OR '
                    '"our team" OR leadership)'
                )

                if company_guess:

                    query = (
                        f'"{company_guess}" "{host}" '
                        '(owner OR founder OR CEO OR president OR team)'
                    )

                search_results = web_search(
                    query,
                    max_results=5,
                    backend="google, bing, brave"
                )

                for item in search_results:

                    item["Input Website"] = website

                    all_search.append(item)

            structured_people = []

            for page in pages:

                for person in page.get("structured_people", []):

                    person = dict(person)

                    person["Company"] = (
                        host if search_people
                        else urlsplit(website).netloc
                    )

                    structured_people.append(person)

            packets.append({

                "website": website,

                "pages": pages,

                "search": search_results,

                "structured_people": structured_people,

            })

            time.sleep(0.25)

        progress.progress(
            1.0,
            text="Website extraction finished."
        )

        status_box.empty()

        # Save extracted data BEFORE Gemini.
        # A quota error must not delete website results.

        st.session_state["bulk_pages"] = all_pages

        st.session_state["bulk_search_results"] = all_search

        st.session_state["bulk_invalid"] = invalid

        st.session_state["bulk_packets"] = packets

        st.session_state["bulk_people_per_company"] = int(
            people_per_company
        )

        st.session_state["bulk_find_people_enabled"] = bool(
            find_people
        )

        st.session_state["bulk_people"] = []

        st.session_state["bulk_people_note"] = ""

        st.session_state["bulk_ai_status"] = "Not requested"

        if find_people:

            if not client:

                st.session_state["bulk_ai_status"] = (
                    "Gemini key not configured"
                )

                st.session_state["bulk_people_note"] = (
                    "Add GEMINI_API_KEY in Streamlit Secrets."
                )

            else:

                try:

                    with st.spinner(
                        "Gemini is checking the collected evidence..."
                    ):

                        # One Gemini request for the entire bulk run.
                        people, notes = research_people_batch(

                            client,

                            gemini_model.strip() or DEFAULT_MODEL,

                            packets,

                            max_people_per_company=people_per_company,

                        )

                    st.session_state["bulk_people"] = people

                    st.session_state["bulk_people_note"] = notes

                    st.session_state["bulk_ai_status"] = "Completed"

                except Exception as exc:

                    st.session_state["bulk_ai_status"] = (
                        "Gemini unavailable"
                    )

                    st.session_state["bulk_people_note"] = str(exc)

                    # Keep any explicitly identified JSON-LD people.
                    st.session_state["bulk_people"] = (
                        structured_people_fallback(packets)
                    )

                    # Website and contact results are preserved.

        st.success(
            f"Processed {len(urls)} websites and "
            f"{len(all_pages)} page results."
        )

        if invalid:
            st.warning(f"Skipped {len(invalid)} invalid URL lines.")


    # --------------------------------------------------------
    # DISPLAY RESULTS
    # --------------------------------------------------------

    pages = st.session_state.get("bulk_pages", [])

    search_results = st.session_state.get(
        "bulk_search_results",
        []
    )

    people = st.session_state.get(
        "bulk_people",
        []
    )

    ai_status = st.session_state.get(
        "bulk_ai_status",
        "Not run"
    )

    ai_note = st.session_state.get(
        "bulk_people_note",
        ""
    )

    if not pages:

        st.info(
            "Paste website URLs above and click the extraction button."
        )

        return

    # Retry AI using stored search data.
    # No need to scrape websites again.

    saved_packets = st.session_state.get(
        "bulk_packets",
        []
    )

    retry_allowed = st.session_state.get(
        "bulk_find_people_enabled",
        False
    )

    if saved_packets and retry_allowed and ai_status != "Completed":

        if st.button(
            "↻ Retry person identification using saved evidence",
            use_container_width=True
        ):

            try:

                retry_client = get_gemini_client()

                if not retry_client:

                    st.error(
                        "Gemini API key is missing. "
                        "Add it in Streamlit Secrets."
                    )

                else:

                    with st.spinner(
                        "Retrying one Gemini request..."
                    ):

                        retry_people, retry_notes = research_people_batch(

                            retry_client,

                            st.session_state.get(
                                "bulk_gemini_model",
                                DEFAULT_MODEL
                            ),

                            saved_packets,

                            max_people_per_company=st.session_state.get(
                                "bulk_people_per_company",
                                3
                            ),

                        )

                    st.session_state["bulk_people"] = retry_people

                    st.session_state["bulk_people_note"] = retry_notes

                    st.session_state["bulk_ai_status"] = "Completed"

                    st.rerun()

            except Exception as exc:

                st.session_state["bulk_ai_status"] = "Gemini unavailable"

                st.session_state["bulk_people_note"] = str(exc)

                st.session_state["bulk_people"] = (
                    structured_people_fallback(saved_packets)
                )

                st.error(
                    "Person research could not run. "
                    "Website and contact extraction are still available."
                )

                show_gemini_error(exc)

    # Metrics

    email_list = sorted({
        email
        for page in pages
        for email in page.get("emails", [])
    })

    phone_list = sorted({
        phone
        for page in pages
        for phone in page.get("phones", [])
    })

    website_count = len({
        page.get("Input Website")
        for page in pages
    })

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Websites", website_count)

    c2.metric("Pages processed", len(pages))

    c3.metric("Emails found", len(email_list))

    c4.metric("People identified", len(people))

    # People output

    st.subheader("Decision-makers / people found")

    st.caption(f"AI status: {ai_status}")

    if ai_status == "Gemini unavailable":

        show_gemini_error(Exception(ai_note))

    elif ai_status == "Gemini key not configured":

        st.warning(ai_note)

    elif ai_note:

        st.info(ai_note)

    if people:

        people_df = pd.DataFrame(people)

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

        with st.expander("Person evidence and source URLs"):

            st.dataframe(
                people_df[
                    [
                        "Company",
                        "Full Name",
                        "Job Title",
                        "Source URL",
                        "Evidence",
                        "Confidence"
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

    else:

        st.info(
            "No people could be verified from the collected public evidence. "
            "This does not mean the company has no owner. "
            "Their names or titles may not be publicly available."
        )

    # Website results

    page_rows = []

    for page in pages:

        page_rows.append({

            "Input Website": page.get("Input Website", ""),

            "Page URL": page.get("url", ""),

            "Page Title": page.get("title", ""),

            "Status": page.get("status", ""),

            "Description": page.get("description", ""),

            "Emails": ", ".join(
                page.get("emails", [])
            ),

            "Phones": ", ".join(
                page.get("phones", [])
            ),

            "Extracted Text": page.get("text", ""),

        })

    pages_df = pd.DataFrame(page_rows)

    st.subheader("Extracted website pages")

    st.dataframe(
        pages_df[
            [
                "Input Website",
                "Page URL",
                "Page Title",
                "Status",
                "Description",
                "Emails",
                "Phones"
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    # Contact information

    contact_rows = []

    for page in pages:

        for email in page.get("emails", []):

            contact_rows.append({

                "Website": page.get("Input Website", ""),

                "Email": email,

                "Phone": ", ".join(
                    page.get("phones", [])
                ),

                "Source Page": page.get("url", ""),

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

    if contacts_df.empty:

        st.info(
            "No emails were found in the accessible pages."
        )

    else:

        st.dataframe(
            contacts_df,
            use_container_width=True,
            hide_index=True
        )

    if search_results:

        with st.expander("Public web search evidence"):

            st.dataframe(
                pd.DataFrame(search_results),
                use_container_width=True,
                hide_index=True
            )

    # Downloads

    st.subheader("Download results")

    d1, d2, d3 = st.columns(3)

    with d1:

        st.download_button(
            "Download pages CSV",
            pages_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            "website_pages.csv",
            "text/csv",
            use_container_width=True,
        )

    with d2:

        st.download_button(
            "Download contacts CSV",
            contacts_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            "website_contacts.csv",
            "text/csv",
            use_container_width=True,
        )

    with d3:

        people_df = pd.DataFrame(
            people,
            columns=[
                "Company",
                "Full Name",
                "Job Title",
                "Source URL",
                "Evidence",
                "Confidence"
            ]
        )

        st.download_button(
            "Download people CSV",
            people_df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            "company_people.csv",
            "text/csv",
            use_container_width=True,
        )

    with st.expander("View full extracted website text"):

        chosen = st.selectbox(
            "Select page",
            range(len(pages)),
            format_func=lambda i: (
                f'{pages[i].get("Input Website", "")} | '
                f'{pages[i].get("title", "") or pages[i].get("url", "")}'
            ),
        )

        st.text_area(
            "Page content",
            pages[chosen].get("text", ""),
            height=280,
            disabled=True,
        )


# ============================================================
# TOOL 2: AI COMPANY RESEARCH
# ============================================================

def ai_research_page():

    st.markdown(
        '<div class="hero">'
        '<h1>AI Company Research</h1>'
        '<p>Research companies and decision-makers using Gemini and Google Search.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    brief = st.text_area(
        "Client brief / Research instructions",
        height=180,
        placeholder=(
            "Example: Find US-based B2B SaaS companies in healthcare, "
            "11–200 employees, and identify relevant decision-makers."
        ),
    )

    col1, col2, col3 = st.columns(3)

    with col1:

        region = st.text_input(
            "Target country / region",
            placeholder="United States"
        )

    with col2:

        industry = st.text_input(
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

    model = st.text_input(
        "Gemini model",
        value=DEFAULT_MODEL,
        key="research_model"
    )

    st.caption(
        "Final contact list contains Full Name and Job Title. "
        "Email finding is not included."
    )

    if st.button(
        "🧠 Start AI Research",
        type="primary",
        use_container_width=True
    ):

        if not brief.strip():

            st.error("Enter the client brief first.")
            return

        try:

            client = get_gemini_client()

        except Exception as exc:

            st.error(f"Gemini initialization failed: {exc}")
            return

        if not client:

            st.error(
                "Gemini API key is missing. "
                "Add GEMINI_API_KEY in Streamlit Secrets."
            )

            return

        # Clear old results

        st.session_state["research_contacts"] = []
        st.session_state["research_summary"] = ""
        st.session_state["research_sources"] = []
        st.session_state["research_raw"] = ""

        prompt = f"""
You are a careful B2B company and decision-maker research assistant.

Use Google Search grounding for current public evidence.
Never invent facts.

CLIENT BRIEF:
{brief}

TARGET COUNTRY / REGION:
{region or 'Use the brief'}

TARGET INDUSTRY:
{industry or 'Use the brief'}

MAXIMUM CONTACTS:
{int(max_contacts)}

RULES:

- Identify companies matching the brief.
- Include real people only when their full name, current job title
  and company connection are publicly supported.
- Prioritize relevant founders, owners, CEOs, presidents,
  managing directors and requested job functions.
- Do not guess identities or titles.
- Do not generate or find email addresses.
- Return fewer contacts rather than inventing records.
- Avoid duplicates.
- Include a source URL for every person.

Return JSON only:

{{
  "summary": "Short interpretation of the ICP and research approach",
  "contacts": [
    {{
      "Full Name": "Person's full name",
      "Job Title": "Current public job title",
      "Company": "Company name",
      "Company Website": "https://company.example",
      "Source URL": "https://public-source.example/profile",
      "Fit Notes": "Brief evidence-based match explanation"
    }}
  ]
}}
"""

        try:

            with st.spinner(
                "Gemini is researching public web sources..."
            ):

                response = client.models.generate_content(

                    model=model.strip() or DEFAULT_MODEL,

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

            parsed = parse_json_response(
                response.text or ""
            )

            if isinstance(parsed, list):

                parsed = {
                    "summary": "",
                    "contacts": parsed
                }

            if not isinstance(parsed, dict):

                raise ValueError(
                    "Gemini returned invalid JSON. Please run again."
                )

            contacts = []
            seen = set()

            for row in parsed.get("contacts", []):

                if not isinstance(row, dict):
                    continue

                name = str(
                    row.get("Full Name", "")
                ).strip()

                title = str(
                    row.get("Job Title", "")
                ).strip()

                if not name or not title:
                    continue

                key = (
                    name.casefold(),
                    title.casefold()
                )

                if key in seen:
                    continue

                seen.add(key)

                contacts.append({

                    "Full Name": name,

                    "Job Title": title,

                    "Company": str(
                        row.get("Company", "")
                    ).strip(),

                    "Company Website": str(
                        row.get("Company Website", "")
                    ).strip(),

                    "Source URL": str(
                        row.get("Source URL", "")
                    ).strip(),

                    "Fit Notes": str(
                        row.get("Fit Notes", "")
                    ).strip(),

                })

                if len(contacts) >= int(max_contacts):
                    break

            st.session_state["research_contacts"] = contacts

            st.session_state["research_summary"] = str(
                parsed.get("summary", "")
            )

            st.session_state["research_sources"] = (
                extract_grounding_sources(response)
            )

            st.session_state["research_raw"] = (
                response.text or ""
            )

            st.success(
                f"Research completed: {len(contacts)} contact(s)."
            )

        except Exception as exc:

            show_gemini_error(exc)

    # Display research

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

    raw = st.session_state.get(
        "research_raw",
        ""
    )

    if summary:

        st.subheader("Research interpretation")

        st.write(summary)

    if contacts:

        df = pd.DataFrame(contacts)

        m1, m2 = st.columns(2)

        m1.metric("Contacts found", len(df))

        m2.metric(
            "Companies represented",
            df["Company"].replace("", pd.NA).nunique()
        )

        st.subheader("Contact results")

        st.dataframe(
            df[
                [
                    "Full Name",
                    "Job Title"
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Company evidence and source URLs"):

            st.dataframe(
                df[
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
            df[
                [
                    "Full Name",
                    "Job Title"
                ]
            ].to_csv(
                index=False
            ).encode("utf-8-sig"),
            "researched_contacts.csv",
            "text/csv",
            use_container_width=True,
        )

        st.download_button(
            "Download full research CSV",
            df.to_csv(
                index=False
            ).encode("utf-8-sig"),
            "full_company_research.csv",
            "text/csv",
            use_container_width=True,
        )

    if sources:

        with st.expander("Google Search grounding sources"):

            st.dataframe(
                pd.DataFrame(sources),
                use_container_width=True,
                hide_index=True,
            )

    if raw and not contacts:

        with st.expander("Last Gemini response"):

            st.text(raw)


# ============================================================
# APP ROUTER
# ============================================================

if selected_tool == "Bulk Website Extractor":

    bulk_extractor_page()

else:

    ai_research_page()


st.divider()

st.caption(
    "Use only public pages you are permitted to access. "
    "Respect website terms, robots policies and request limits. "
    "This app does not log in, bypass CAPTCHAs or access private/paywalled content. "
    "Verify people and job titles against original sources before delivery."
)
