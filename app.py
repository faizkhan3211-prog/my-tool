import re
import time
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

MAX_BULK = 25
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


def extract_people(html, src):
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    found = []
    for i, line in enumerate(lines):
        if len(line) < 70 and TITLE_RE.search(line) and not NAME_RE.match(line):
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and NAME_RE.match(lines[j]):
                    found.append({"Name": lines[j], "Title": line, "Source": "Website", "Link": src})
                    break
    return found


def search_people(company, domain, limit=10):
    if DDGS is None:
        return []
    queries = [
        f'site:linkedin.com/in "{company}" (CEO OR Founder OR Owner OR President)',
        f'site:linkedin.com/in "{company}" (Director OR Manager OR Head OR VP)',
        f'"{company}" {domain} leadership team CEO founder',
    ]
    out = []
    for q in queries:
        try:
            results = DDGS().text(q, max_results=limit)
        except Exception:
            continue
        for r in results or []:
            href, title, body = r.get("href", ""), r.get("title", ""), r.get("body", "")
            if "linkedin.com/in" not in href:
                continue
            blob = (title + " " + body).lower()
            if company.lower().split()[0] not in blob:
                continue
            t = title.replace("| LinkedIn", "").strip()
            parts = [x.strip() for x in re.split(r" - | – | \| ", t) if x.strip()]
            if len(parts) >= 2 and NAME_RE.match(parts[0]):
                out.append({"Name": parts[0], "Title": parts[1], "Source": "Web search", "Link": href})
        time.sleep(1)
    return out


def process_site(url, company_hint=""):
    base = normalize(url)
    domain = urlparse(base).netloc
    html = fetch(base)
    if not html:
        return None

    company = company_hint.strip() or company_from_html(html, domain)
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

    people_rows = []
    for x in people:
        row = {"Company": company, "Website": domain}
        row.update(x)
        people_rows.append(row)

    contact_rows, soc = [], set()
    for src, ems, phs, scs in contacts:
        contact_rows += [{"Company": company, "Website": domain, "Type": "Email", "Value": v, "Found on": src} for v in ems]
        contact_rows += [{"Company": company, "Website": domain, "Type": "Phone", "Value": v, "Found on": src} for v in phs]
        soc |= scs
    contact_rows += [{"Company": company, "Website": domain, "Type": "Social", "Value": v, "Found on": base} for v in sorted(soc)]

    return {"people": people_rows, "contacts": contact_rows}


def show_results():
    res = st.session_state.get("res")
    if not res:
        return

    st.markdown("### Decision-makers / People")
    if res["people"]:
        pdf = pd.DataFrame(res["people"]).drop_duplicates(subset=["Website", "Name"])
