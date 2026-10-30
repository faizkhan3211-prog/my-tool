import re
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except Exception:
    DDGS = None

st.set_page_config(page_title="Decision-Maker Finder", layout="wide")
st.title("Decision-Maker & Contact Finder")
st.caption("Built by Faiz Khan")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
BAD_END = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
SOCIALS = ["linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com"]
KEYS = ["contact", "about", "team", "leadership", "management", "people", "staff"]
TITLES = ["CEO", "Founder", "Co-Founder", "Owner", "President", "Chairman", "Director",
          "Manager", "VP", "Vice President", "Head", "Chief", "Partner", "Principal",
          "Broker", "Managing", "COO", "CFO", "CTO", "CMO"]
TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in TITLES) + r")\b", re.I)


def normalize(url):
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            return r.text
    except Exception:
        pass
    return None


def company_from_html(html, domain):
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()
    name = domain.replace("www.", "").split(".")[0]
    return name.replace("-", " ").title()


def extract_contacts(html, base):
    soup = BeautifulSoup(html, "html.parser")
    emails, phones, socials, pages = set(), set(), set(), set()
    base_host = urlparse(base).netloc

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()

        if low.startswith("mailto:"):
            emails.add(href[7:].split("?")[0].strip().lower())
            continue

        if low.startswith("tel:"):
            phones.add(href[4:].strip())
            continue

        full = urljoin(base, href)
        full_low = full.lower()

        if any(s in full_low for s in SOCIALS):
            socials.add(full.split("?")[0])
            continue

        same_site = urlparse(full).netloc == base_host
        has_key = any(k in full_low for k in KEYS)
        if same_site and has_key:
            pages.add(full.split("#")[0])

    text = soup.get_text(" ")
    for e in EMAIL_RE.findall(text):
        emails.add(e.lower())
    for p in PHONE_RE.findall(text):
        if 10 <= len(re.sub(r"\D", "", p)) <= 15:
            phones.add(p.strip())

    emails = {e for e in emails if not e.endswith(BAD_END) and "example" not in e and "sentry" not in e}
    return emails, phones, socials, pages

col1, col2 = st.columns(2)
url_input = col1.text_input("Website ka link", placeholder="example.com")
company_input = col2.text_input("Company ka naam (optional, accuracy behtar hoti hai)")

if st.button("Dhoondo") and url_input:
    base = normalize(url_input)
    domain = urlparse(base).netloc
    with st.spinner("Website aur search check ho rahi hai..."):
        html = fetch(base)
        if not html:
            st.error("Website khul nahi saki ya block hai. Link check karo.")
            st.stop()

        company = company_input.strip() or company_from_html(html, domain)
        e, p, s, pages = extract_contacts(html, base)
        contacts = [(base, e, p, s)]
        people = extract_people(html, base)

        for pg in list(pages)[:6]:
            h = fetch(pg)
            if h:
                e2, p2, s2, _ = extract_contacts(h, pg)
                contacts.append((pg, e2, p2, s2))
                people += extract_people(h, pg)

        people += search_people(company, domain)

    st.subheader(f"Company: {company}")

    st.markdown("### Decision-makers / People")
    if people:
        pdf = pd.DataFrame(people).drop_duplicates(subset=["Name"])
        st.dataframe(pdf, use_container_width=True)
        st.download_button("Download People CSV", pdf.to_csv(index=False), "people.csv")
    else:
        st.warning("Koi naam + title nahi mila. Company ka naam upar likh kar dobara try karo.")

    st.markdown("### Company contacts")
    rows, soc = [], set()
    for src, ems, phs, scs in contacts:
        rows += [{"Type": "Email", "Value": x, "Found on": src} for x in ems]
        rows += [{"Type": "Phone", "Value": x, "Found on": src} for x in phs]
        soc |= scs
    rows += [{"Type": "Social", "Value": x, "Found on": base} for x in sorted(soc)]
    if rows:
        cdf = pd.DataFrame(rows).drop_duplicates(subset=["Type", "Value"])
        st.dataframe(cdf, use_container_width=True)
        st.download_button("Download Contacts CSV", cdf.to_csv(index=False), "contacts.csv")
    else:
        st.info("Website par koi public contact nahi mila.")
