import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
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

WANT_ALL = ["Company profile", "People", "Emails", "Phones", "Social profiles"]
PLAT_ALL = ["LinkedIn", "Facebook", "Instagram", "Twitter/X", "YouTube"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
ADDRESS_RE = re.compile(r"\d{1,6}\s[A-Za-z0-9 .,'#-]{5,80}?,?\s[A-Z]{2}\s\d{5}(?:-\d{4})?")
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

INFO_SITES = ["growjo.com", "zoominfo.com", "zippia.com", "dnb.com", "cortera.com",
              "rocketreach.co", "apollo.io", "linkedin.com/company", "craft.co",
              "owler.com", "leadiq.com", "comparably.com"]
BLOCK_DOMAINS = ["yelp.", "facebook.", "instagram.", "linkedin.", "tripadvisor.", "yellowpages.",
                 "mapquest.", "realself.", "groupon.", "healthgrades.", "zocdoc.", "wikipedia.",
                 "youtube.", "tiktok.", "reddit.", "pinterest.", "nytimes.", "timeout.", "bbb.org",
                 "manta.", "birdeye.", "twitter.", "x.com", "google.", "bing.", "vagaro.",
                 "mindbodyonline.", "treatwell.", "thumbtack.", "expertise.", "cylex.", "chamberofcommerce.",
                 "superpages.", "foursquare.", "nextdoor.", "doctor.", "webmd.", "vogue.", "allure.",
                 "forbes.", "buzzfeed.", "medium.", "glassdoor.", "indeed.", "zippia.", "zoominfo.",
                 "growjo.", "dnb.", "apollo.", "rocketreach.", "opencare.", "sharecare."]

UNIT = r"(?:billion|million|thousand|bn|mm|b|m|k)"
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*\.?\d*)\s*(billion|million|thousand|bn|mm|b|m|k)?\b", re.I)
EMP_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:-|–|to)\s*(\d[\d,]*)\+?\s*(?:employees|staff|team members)", re.I)
EMP_ONE_RE = re.compile(r"(\d[\d,]*)\+?\s*(?:employees|staff)", re.I)


# ---------- basic helpers ----------
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
    stem = domain.replace("www.", "").split(".")[0]
    fallback = stem.replace("-", " ").title()
    if soup.title and soup.title.string:
        chunks = [c.strip() for c in re.split(r"\s[|–—-]\s", soup.title.string) if c.strip()]
        best, score = None, 0.0
        for c in chunks:
            s = SequenceMatcher(None, c.lower().replace(" ", ""), stem.lower()).ratio()
            if s > score and len(c) <= 50:
                best, score = c, s
        if best and score >= 0.5:
            return best
    return fallback


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


def extract_profile(html):
    soup = BeautifulSoup(html, "html.parser")
    address, phone, desc = "", "", ""

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            addr = node.get("address")
            if isinstance(addr, dict) and not address:
                parts = [addr.get(k, "") for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
                address = ", ".join(str(p).strip() for p in parts if p)
            elif isinstance(addr, str) and not address:
                address = addr.strip()
            tel = node.get("telephone")
            if isinstance(tel, str) and tel.strip() and not phone:
                phone = tel.strip()
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))

    if not address:
        tag = soup.find("address")
        if tag:
            t = " ".join(tag.get_text(" ").split())
            if 10 <= len(t) <= 200:
                address = t
    if not address:
        m = ADDRESS_RE.search(soup.get_text(" "))
        if m:
            address = " ".join(m.group(0).split())

    md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    if md and md.get("content"):
        desc = md["content"].strip()[:200]
    return address, phone, desc


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


# ---------- size / revenue ----------
def to_usd_m(num, unit):
    try:
        v = float(num.replace(",", ""))
    except Exception:
        return None
    u = (unit or "").lower()
    if u in ("billion", "bn", "b"):
        return v * 1000
    if u in ("million", "mm", "m"):
        return v
    if u in ("thousand", "k"):
        return v / 1000
    if v >= 10000:
        return v / 1_000_000
    return None


def fmt_money(v):
    if v >= 1000:
        return f"${v / 1000:g}B"
    if v >= 1:
        return f"${v:g}M"
    return f"${v * 1000:g}K"


def fmt_rev(rev):
    if not rev:
        return ""
    return fmt_money(rev[0]) if rev[0] == rev[1] else f"{fmt_money(rev[0])}-{fmt_money(rev[1])}"


def fmt_emp(emp):
    if not emp:
        return ""
    return str(emp[0]) if emp[0] == emp[1] else f"{emp[0]}-{emp[1]}"


def parse_revenue(text):
    for m in re.finditer(r"revenue|sales", text, re.I):
        window = text[max(0, m.start() - 60): m.end() + 140]
        found = []
        for mm in MONEY_RE.finditer(window):
            val = to_usd_m(mm.group(1), mm.group(2))
            if val is not None:
                found.append((val, mm.start(), mm.end()))
        if not found:
            continue
        lo = hi = found[0][0]
        if len(found) > 1:
            gap = window[found[0][2]: found[1][1]]
            if len(gap) <= 8:
                lo, hi = min(found[0][0], found[1][0]), max(found[0][0], found[1][0])
        return (lo, hi)
    return None


def parse_employees(text):
    m = EMP_RANGE_RE.search(text)
    if m:
        a, b = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        return (min(a, b), max(a, b))
    m = EMP_ONE_RE.search(text)
    if m:
        n = int(m.group(1).replace(",", ""))
        return (n, n)
    return None


def enrich(company):
    empty = {"emp": None, "rev": None, "emp_src": "", "rev_src": "", "link": "", "confidence": ""}
    if DDGS is None:
        return empty
    words = [w for w in company.lower().split() if w not in ("the", "a", "and")]
    token = words[0] if words else company.lower()
    emps, revs = [], []
    queries = [f'"{company}" revenue employees', f'"{company}" company size employees']
    with SEARCH_LOCK:
        for q in queries:
            try:
                results = DDGS().text(q, max_results=10)
            except Exception:
                results = []
            for r in results or []:
                href = r.get("href", "")
                site = next((s for s in INFO_SITES if s in href), None)
                if not site:
                    continue
                blob = r.get("title", "") + " . " + r.get("body", "")
                if token not in blob.lower():
                    continue
                rv, em = parse_revenue(blob), parse_employees(blob)
                if rv:
                    revs.append((rv, site, href))
                if em:
                    emps.append((em, site, href))
            time.sleep(1)
            if emps and revs:
                break

    info = dict(empty)
    if emps:
        info["emp"], info["emp_src"], info["link"] = emps[0][0], emps[0][1], emps[0][2]
    if revs:
        info["rev"], info["rev_src"] = revs[0][0], revs[0][1]
        if not info["link"]:
            info["link"] = revs[0][2]
    sources = {x[1] for x in emps + revs}
    info["confidence"] = "Medium" if len(sources) >= 2 else "Low"
    return info


def overlaps(lo, hi, min_v, max_v):
    top = max_v if max_v > 0 else float("inf")
    return hi >= min_v and lo <= top


def passes(info, emp_min, emp_max, rev_min, rev_max, drop_blank):
    emp, rev = info["emp"], info["rev"]
    if drop_blank and not emp and not rev:
        return False
    if emp_min or emp_max:
        if not emp or not overlaps(emp[0], emp[1], emp_min, emp_max):
            return False
    if rev_min or rev_max:
        if not rev or not overlaps(rev[0], rev[1], rev_min, rev_max):
            return False
    return True


# ---------- description search ----------
def clean_description(desc):
    q = EMP_RANGE_RE.sub(" ", desc)
    q = EMP_ONE_RE.sub(" ", q)
    q = re.sub(r"(revenue|sales)[^,.;\n]*", " ", q, flags=re.I)
    money = r"\$\s?\d[\d,.]*\s*" + UNIT + r"?(?:\s*(?:-|–|to)\s*\$?\s*\d[\d,.]*\s*" + UNIT + r"?)?"
    q = re.sub(money, " ", q, flags=re.I)
    q = re.sub(r"\b(with|and|having|between|of|around)\b\s*(?=[,.;]|$)", " ", q, flags=re.I)
    return " ".join(q.replace(",", " ").split())


def detect_filters(desc):
    emp_min = emp_max = 0
    rev_min = rev_max = 0.0
    emp = parse_employees(desc)
    if emp:
        emp_min, emp_max = (emp[0], emp[1]) if emp[0] != emp[1] else (emp[0], 0)
    rev = parse_revenue(desc)
    if rev:
        rev_min, rev_max = (rev[0], rev[1]) if rev[0] != rev[1] else (rev[0], 0.0)
    return emp_min, emp_max, rev_min, rev_max


def discover(queries, per_query=20):
    found = {}
    if DDGS is None:
        return found
    with SEARCH_LOCK:
        for q in queries:
            try:
                results = DDGS().text(q, max_results=per_query)
            except Exception:
                results = []
            for r in results or []:
                host = urlparse(r.get("href", "")).netloc.lower().replace("www.", "")
                if not host or any(b in host for b in BLOCK_DOMAINS):
                    continue
                found.setdefault(host, "")
            time.sleep(1)
    return found


def name_for_domain(domain):
    html = fetch("https://" + domain)
    if not html:
        return None
    return company_from_html(html, domain)


# ---------- per-site processing ----------
def pick_email(emails, domain):
    emails = sorted(emails)
    stem = domain.split(".")[0]
    own = [e for e in emails if stem in e.split("@")[-1]]
    pool = own or emails
    for pref in ("info", "contact", "hello", "office", "admin", "support", "sales"):
        for e in pool:
            if e.split("@")[0] == pref:
                return e
    return pool[0] if pool else ""


def process_site(url, company_hint, use_search, opts):
    want, plats = opts["want"], opts["plats"]
    base = normalize(url)
    domain = urlparse(base).netloc.replace("www.", "")
    html = fetch(base)
    if not html:
        return None

    company = company_hint.strip() or company_from_html(html, domain)
    e, p, s, pages = extract_contacts(html, base)
    contacts = [(base, e, p, s)]
    people = extract_people(html, base) if "People" in want else []
    address, phone, desc = extract_profile(html)

    for pg in list(pages)[:6]:
        h = fetch(pg)
        if h:
            e2, p2, s2, _ = extract_contacts(h, pg)
            contacts.append((pg, e2, p2, s2))
            if "People" in want:
                people += extract_people(h, pg)
            a2, ph2, d2 = extract_profile(h)
            address, phone, desc = address or a2, phone or ph2, desc or d2

    if not phone:
        for _, _, phs, _ in contacts:
            if phs:
                phone = sorted(phs)[0]
                break

    if use_search and "People" in want:
        people += search_people(company, domain)

    base_cols = {"Company": company, "Website": domain}
    all_emails, all_socials = set(), {}
    email_rows, phone_rows, social_rows = [], [], []
    for src, ems, phs, scs in contacts:
        scs = {v for v in scs if social_platform(v) in plats}
        all_emails |= ems
        for v in sorted(scs):
            all_socials.setdefault(social_platform(v), v)
        email_rows += [dict(base_cols, Email=v, **{"Found on": src}) for v in sorted(ems)]
        phone_rows += [dict(base_cols, Phone=v, **{"Found on": src}) for v in sorted(phs)]
        social_rows += [dict(base_cols, Platform=social_platform(v), Link=v) for v in sorted(scs)]

    profile = {"Company": company, "Website": domain, "Address": address, "Phone": phone,
               "General email": pick_email(all_emails, domain), "Description": desc}
    for pl in PLAT_ALL:
        if pl in plats:
            profile[pl] = all_socials.get(pl, "")

    return {
        "companies": [profile] if "Company profile" in want else [],
        "people": [dict(base_cols, **x) for x in people] if "People" in want else [],
        "emails": email_rows if "Emails" in want else [],
        "phones": phone_rows if "Phones" in want else [],
        "socials": social_rows if "Social profiles" in want else [],
    }


def safe_process(url, comp, use_search, opts):
    try:
        return process_site(url, comp, use_search, opts)
    except Exception:
        return None


def run_batch(items, use_search, opts):
    seen, unique = set(), []
    for url, comp in items:
        key = urlparse(normalize(url)).netloc.replace("www.", "")
        if key and key not in seen:
            seen.add(key)
            unique.append((url, comp))
    unique = unique[:MAX_BULK]

    res = {"companies": [], "people": [], "emails": [], "phones": [], "socials": [],
           "failed": [], "want": list(opts["want"])}
    if not unique:
        return res
    bar = st.progress(0.0)
    status = st.empty()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(safe_process, u, c, use_search, opts): u for u, c in unique}
        for f in as_completed(futures):
            out = f.result()
            if out:
                for k in ("companies", "people", "emails", "phones", "socials"):
                    res[k] += out[k]
            else:
                res["failed"].append(futures[f])
            done += 1
            bar.progress(done / len(unique))
            status.write(f"Data nikal raha hun: {done}/{len(unique)} mukammal")
    return res


# ---------- results ----------
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
    order = [w for w in WANT_ALL if w in res.get("want", WANT_ALL)]
    tabs = st.tabs(order)
    for name, tab in zip(order, tabs):
        with tab:
            if name == "Company profile":
                st.caption("Employees/Revenue (agar hon) sirf andaza (Estimate) hain. Source link khol kar verify karo.")
                table(res["companies"], ["Website"], "companies", "companies.csv", "dl_c", "Koi company nahi mili.")
            elif name == "People":
                table(res["people"], ["Website", "Name"], "people", "people.csv", "dl_p", "Koi naam + title nahi mila.")
            elif name == "Emails":
                table(res["emails"], ["Website", "Email"], "emails", "emails.csv", "dl_e", "Koi email nahi mili.")
            elif name == "Phones":
                table(res["phones"], ["Website", "Phone"], "phones", "phones.csv", "dl_ph", "Koi phone nahi mila.")
            else:
                table(res["socials"], ["Website", "Link"], "social profiles", "social_profiles.csv", "dl_s",
                      "Koi social profile nahi mila.")
    if res["failed"]:
        st.error("Ye websites khul nahi sakin: " + ", ".join(res["failed"]))


def read_sites_csv(up):
    df0 = pd.read_csv(up, header=None, dtype=str)
    first = str(df0.iloc[0, 0]).strip() if len(df0) else ""
    if re.match(r"^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+(/.*)?$", first):
        df0.columns = [f"Column {i + 1}" for i in range(df0.shape[1])]
        return df0, False
    up.seek(0)
    return pd.read_csv(up, dtype=str), True


# ---------- UI ----------
tab_single, tab_bulk, tab_search = st.tabs(["Single website", "Bulk mode", "Client description search"])

with tab_single:
    s1, s2 = st.columns(2)
    url_input = s1.text_input("Website ka link", placeholder="example.com")
    company_input = s2.text_input("Company ka naam (optional)")
    if st.button("Dhoondo", key="single_btn") and url_input:
        opts = {"want": WANT_ALL, "plats": PLAT_ALL}
        st.session_state["res"] = run_batch([(url_input, company_input)], True, opts)

with tab_bulk:
    st.write(f"Har line mein ek website. Company ka naam dena ho to comma lagao: `example.com, Example Corp`. Max {MAX_BULK} websites.")
    want_b = st.multiselect("Kya nikalna hai", WANT_ALL, default=WANT_ALL, key="want_b")
    plats_b = st.multiselect("Social platforms (kaun se chahiye)", PLAT_ALL, default=PLAT_ALL, key="plats_b")
    search_b = st.checkbox("People ke liye web search bhi (slow, lekin zyada log milte hain)", value=True, key="search_b")
    text = st.text_area("Websites ki list", height=180, placeholder="example.com\nanother.com, Another Corp")
    up = st.file_uploader("Ya CSV upload karo", type="csv")

    items = []
    for line in text.splitlines():
        if line.strip():
            parts = [x.strip() for x in line.split(",", 1)]
            items.append((parts[0], parts[1] if len(parts) > 1 else ""))

    if up is not None:
        df_in, had_header = read_sites_csv(up)
        if not had_header:
            st.info("CSV mein header nahi mila, is liye pehli row bhi website maani gayi.")
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
        if not want_b:
            st.error("Kam az kam ek cheez 'Kya nikalna hai' mein chuno.")
        else:
            opts = {"want": want_b, "plats": plats_b or PLAT_ALL}
            st.session_state["res"] = run_batch(items, search_b, opts)

with tab_search:
    st.write("Client ki description likho. Tool khud companies dhoondega, aur description mein employees ya revenue likha ho to usay filter bana lega.")
    desc = st.text_area("Client description", height=110,
                        placeholder="Med spas in NYC and Brooklyn, 10-50 employees, revenue $1M-$5M")
    want_s = st.multiselect("Kya nikalna hai", WANT_ALL, default=["Company profile", "Emails", "Phones", "Social profiles"], key="want_s")
    plats_s = st.multiselect("Social platforms (kaun se chahiye)", PLAT_ALL, default=PLAT_ALL, key="plats_s")
    max_cand = st.slider("Kitni companies check karni hain", 10, 150, 40)
    enrich_on = st.checkbox("Employees / revenue ka andaza bhi nikalo (slow)", value=True)
    drop_blank = st.checkbox("Jin ka employees/revenue data na mile unhein hata do", value=True)
    people_s = st.checkbox("People ke liye web search bhi (bohat slow)", value=False)

    with st.expander("Manual filters (optional, 0 = description se lo)"):
        m1, m2, m3, m4 = st.columns(4)
        man_emp_min = m1.number_input("Min employees", min_value=0, value=0, step=1)
        man_emp_max = m2.number_input("Max employees", min_value=0, value=0, step=1)
        man_rev_min = m3.number_input("Min revenue, $M", min_value=0.0, value=0.0, step=0.5)
        man_rev_max = m4.number_input("Max revenue, $M", min_value=0.0, value=0.0, step=0.5)

    if desc.strip():
        d_emp_min, d_emp_max, d_rev_min, d_rev_max = detect_filters(desc)
        if man_emp_min or man_emp_max:
            d_emp_min, d_emp_max = man_emp_min, man_emp_max
        if man_rev_min or man_rev_max:
            d_rev_min, d_rev_max = man_rev_min, man_rev_max
        q_clean = clean_description(desc)
        st.caption(f"Search query: **{q_clean}** | Employees: {d_emp_min or '-'} se {d_emp_max or 'upar'} | Revenue ($M): {d_rev_min or '-'} se {d_rev_max or 'upar'}")

    if st.button("Dhoondo", key="search_btn") and desc.strip():
        filters_on = bool(d_emp_min or d_emp_max or d_rev_min or d_rev_max)
        need_enrich = enrich_on or filters_on
        want = list(want_s)
        if need_enrich and "Company profile" not in want:
            want = ["Company profile"] + want
        opts = {"want": want, "plats": plats_s or PLAT_ALL}

        status = st.empty()
        bar = st.progress(0.0)
        status.write("Step 1: companies ki list ban rahi hai...")
        base_q = q_clean
        queries = []
        for q in (base_q, f"best {base_q}", f"top {base_q}", f"{base_q} contact us", f"{base_q} services"):
            if q not in queries:
                queries.append(q)
        found = discover(queries)
        domains = list(found.keys())[:max_cand]
        if not domains:
            st.error("Koi website nahi mili. Description badal kar try karo, ya thori der baad dobara.")
            st.stop()

        status.write(f"Step 2: {len(domains)} websites ke naam check ho rahe hain...")
        names = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(name_for_domain, d): d for d in domains}
            for f in as_completed(futs):
                nm = f.result()
                if nm:
                    names[futs[f]] = nm

        kept, infos = [], {}
        if need_enrich:
            total = len(names)
            for n, (domain, name) in enumerate(names.items()):
                status.write(f"Step 3: size/revenue check {n + 1}/{total}: {name}")
                bar.progress((n + 1) / max(total, 1))
                info = enrich(name)
                if passes(info, d_emp_min, d_emp_max, d_rev_min, d_rev_max, drop_blank):
                    kept.append((domain, name))
                    infos[domain] = info
        else:
            kept = list(names.items())

        if not kept:
            st.session_state["res"] = None
            st.warning(f"{len(names)} companies check hui, lekin kisi ka data aap ki range mein nahi mila. Range wider karo ya filter hata do.")
        else:
            status.write(f"Step 4: {len(kept)} companies ka data nikal raha hun...")
            res = run_batch(kept, people_s, opts)
            for row in res["companies"]:
                info = infos.get(row["Website"])
                if info:
                    row["Employees"] = fmt_emp(info["emp"])
                    row["Revenue (Estimate)"] = fmt_rev(info["rev"])
                    row["Employees source"] = info["emp_src"]
                    row["Revenue source"] = info["rev_src"]
                    row["Source link"] = info["link"]
                    row["Confidence"] = info["confidence"]
            st.session_state["res"] = res
            status.write(f"Mukammal! {len(names)} mein se {len(kept)} companies match hui.")

show_results()
