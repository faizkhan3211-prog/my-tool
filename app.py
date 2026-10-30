import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

MAX_BULK = 300
WORKERS = 4
SEARCH_LOCK = threading.Lock()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
BAD_END = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
SOCIAL_NAMES = {
    "linkedin.com": "LinkedIn",
    "facebook.com": "Facebook",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "instagram.com": "Instagram",
    "youtube.com": "YouTube",
}
SHARE_WORDS = ("share", "sharer", "intent", "dialog", "login", "plugins")
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


def social_platform(url):
    host = urlparse(url).netloc.lower().replace("www.", "")
    low = url.lower()
    if any(w in low for w in SHARE_WORDS):
        return None
    for dom, name in SOCIAL_NAMES.items():
        if host == dom or host.endswith("." + dom):
            return name
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

        if social_platform(full):
            socials.add(full.split("?")[0].rstrip("/"))
            continue

        same_site = urlparse(full).netloc == base_host
        has_key = any(k in full.lower() for k in KEYS)
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
    with SEARCH_LOCK:
        for q in queries:
            try:
                results = DDGS().text(q, max_results=limit)
            except Exception:
                results = []
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


def process_site(url, company_hint, use_search):
    base = normalize(url)
    domain = urlparse(base).netloc.replace("www.", "")
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

    if use_search:
        people += search_people(company, domain)

    base_cols = {"Company": company, "Website": domain}
    people_rows = [dict(base_cols, **x) for x in people]
    email_rows, phone_rows, social_rows = [], [], []
    for src, ems, phs, scs in contacts:
        email_rows += [dict(base_cols, Email=v, **{"Found on": src}) for v in sorted(ems)]
        phone_rows += [dict(base_cols, Phone=v, **{"Found on": src}) for v in sorted(phs)]
        social_rows += [dict(base_cols, Platform=social_platform(v), Link=v) for v in sorted(scs)]

    return {"people": people_rows, "emails": email_rows, "phones": phone_rows, "socials": social_rows}


def safe_process(url, comp, use_search):
    try:
        return process_site(url, comp, use_search)
    except Exception:
        return None


def run_batch(items, use_search):
    seen, unique = set(), []
    for url, comp in items:
        key = urlparse(normalize(url)).netloc.replace("www.", "")
        if key and key not in seen:
            seen.add(key)
            unique.append((url, comp))
    unique = unique[:MAX_BULK]

    res = {"people": [], "emails": [], "phones": [], "socials": [], "failed": []}
    bar = st.progress(0.0)
    status = st.empty()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(safe_process, u, c, use_search): u for u, c in unique}
        for f in as_completed(futures):
            out = f.result()
            if out:
                for k in ("people", "emails", "phones", "socials"):
                    res[k] += out[k]
            else:
                res["failed"].append(futures[f])
            done += 1
            bar.progress(done / len(unique))
            status.write(f"{done}/{len(unique)} mukammal")
    status.write(f"Mukammal! {len(unique)} websites check hui.")
    st.session_state["res"] = res


def table(rows, subset, label, fname, key, empty_msg):
    if rows:
        df = pd.DataFrame(rows).drop_duplicates(subset=subset)
        st.write(f"**{len(df)} {label}**")
        st.dataframe(df, use_container_width=True)
        st.download_button(f"Download {label} CSV", df.to_csv(index=False), fname, key=key)
    else:
        st.info(empty_msg)


def show_results():
    res = st.session_state.get("res")
    if not res:
        return
    t1, t2, t3, t4 = st.tabs(["People (naam + title)", "Emails", "Phones", "Social profiles"])
    with t1:
        table(res["people"], ["Website", "Name"], "people", "people.csv", "dl_p",
              "Koi naam + title nahi mila. Company ka naam likh kar dobara try karo.")
    with t2:
        table(res["emails"], ["Website", "Email"], "emails", "emails.csv", "dl_e", "Koi email nahi mili.")
    with t3:
        table(res["phones"], ["Website", "Phone"], "phones", "phones.csv", "dl_ph", "Koi phone nahi mila.")
    with t4:
        table(res["socials"], ["Website", "Link"], "social profiles", "social_profiles.csv", "dl_s",
              "Koi social profile nahi mila.")
    if res["failed"]:
        st.error("Ye websites khul nahi sakin: " + ", ".join(res["failed"]))


tab1, tab2 = st.tabs(["Single website", "Bulk mode"])

with tab1:
    c1, c2 = st.columns(2)
    url_input = c1.text_input("Website ka link", placeholder="example.com")
    company_input = c2.text_input("Company ka naam (optional, accuracy behtar hoti hai)")
    if st.button("Dhoondo", key="single_btn") and url_input:
        with st.spinner("Website aur search check ho rahi hai..."):
            run_batch([(url_input, company_input)], True)

with tab2:
    st.write(f"Har line mein ek website. Company ka naam dena ho to comma lagao: `example.com, Example Corp`. Max {MAX_BULK} websites ek dafa mein.")
    use_search = st.checkbox("Web search se log (naam + title) bhi dhoondo (slow, lekin zyada people milte hain)", value=True)
    text = st.text_area("Websites ki list", height=180, placeholder="example.com\nanother.com, Another Corp")
    up = st.file_uploader("Ya CSV upload karo", type="csv")

    items = []
    for line in text.splitlines():
        if line.strip():
            parts = [x.strip() for x in line.split(",", 1)]
            items.append((parts[0], parts[1] if len(parts) > 1 else ""))

    if up is not None:
        df_in = pd.read_csv(up)
        wcol = st.selectbox("Website wala column", df_in.columns)
        ccol = st.selectbox("Company naam wala column (optional)", ["(none)"] + list(df_in.columns))
        for _, r in df_in.iterrows():
            w = str(r[wcol]).strip()
            if w and w.lower() != "nan":
                comp = "" if ccol == "(none)" else str(r[ccol]).replace("nan", "").strip()
                items.append((w, comp))

    if items:
        st.info(f"{len(items)} websites mili. Is dafa {min(len(items), MAX_BULK)} check hongi.")
    if st.button("Bulk start karo", key="bulk_btn") and items:
        run_batch(items, use_search)

show_results()
