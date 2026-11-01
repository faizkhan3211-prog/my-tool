import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except Exception:
    DDGS = None

try:
    import dns.resolver as dns_resolver
except Exception:
    dns_resolver = None

st.set_page_config(page_title="Decision-Maker Finder", layout="wide")
st.title("Decision-Maker & Contact Finder")
st.caption("Built by Faiz Khan")

WORKERS = 8
HARD_CAP = 3000
DISCOVER_SECONDS = 900
SEARCH_LOCK = threading.Lock()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
EMAIL_FULL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
ADDRESS_RE = re.compile(r"\d{1,6}\s[A-Za-z0-9 .,'#-]{5,80}?,?\s[A-Z]{2}\s\d{5}(?:-\d{4})?")
BAD_END = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff", ".ico")
JUNK_EMAIL_DOMAIN = ("example.", "sentry", "wixpress", "domain.com", "yourdomain", "email.com",
                     "test.com", "godaddy", "squarespace", "wordpress", "schema.org", "w3.org")
JUNK_EMAIL_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply", "you", "name", "user",
                    "yourname", "email", "firstname", "lastname")
SOCIAL_NAMES = {
    "linkedin.com": "LinkedIn",
    "facebook.com": "Facebook",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "instagram.com": "Instagram",
    "youtube.com": "YouTube",
}
SOCIAL_COLS = ["LinkedIn", "Facebook", "Instagram", "Twitter/X", "YouTube"]
BAD_SOCIAL_SEG = {"sharer", "sharer.php", "share", "share.php", "tr", "plugins", "dialog", "login",
                  "intent", "home", "explore", "hashtag", "p", "reel", "reels", "watch", "privacy",
                  "policy", "about", "help", "groups", "events", "photo", "photos",
                  "permalink.php", "story.php", "profile.php"}
SHARE_WORDS = ("share", "sharer", "intent", "dialog", "login", "plugins")
KEYS = ["contact", "about", "team", "leadership", "management", "people", "staff"]
TITLES = ["CEO", "Founder", "Co-Founder", "Owner", "President", "Chairman", "Director",
          "Manager", "VP", "Vice President", "Head", "Chief", "Partner", "Principal",
          "Broker", "Managing", "COO", "CFO", "CTO", "CMO", "Dentist", "Doctor", "Dr"]
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
                 "growjo.", "dnb.", "apollo.", "rocketreach.", "opencare.", "sharecare.",
                 "zillow.", "realtor.com", "redfin.", "trulia.", "apartments.com", "loopnet.", "crexi.",
                 "homes.com", "1800dentist", "ada.org", "dentistry.com", "dentalplans", "dentist.com",
                 "amazon.", "ebay.", "apple.", "microsoft.", "bloomberg.", "linktr.ee", "nerdwallet.",
                 "usnews.", "healthline.", "mayoclinic.", "cdc.gov", "nih.gov", ".gov", ".edu",
                 "angi.", "porch.", "houzz.", "clutch.co", "g2.com", "capterra.", "trustpilot.",
                 "crunchbase.", "owler.", "yellowbook.", "alignable.", "merchantcircle.", "citysearch.",
                 "dexknows.", "local.com", "hotfrog.", "brownbook.", "showmelocal.", "bizapedia."]

US_STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
             "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
             "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
             "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
             "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
             "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
             "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
             "Wisconsin", "Wyoming"]
US_NAMES = ("usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america")

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


def valid_social(url):
    plat = social_platform(url)
    if not plat:
        return None
    segs = [s for s in urlparse(url).path.split("/") if s]
    if not segs:
        return None
    first = segs[0].lower()
    if plat == "LinkedIn":
        return plat if first in ("company", "in", "school") and len(segs) > 1 else None
    if plat == "YouTube":
        ok = (first in ("channel", "c", "user") and len(segs) > 1) or first.startswith("@")
        return plat if ok else None
    return plat if first not in BAD_SOCIAL_SEG else None


@lru_cache(maxsize=8192)
def mx_ok(domain):
    if dns_resolver is None:
        return True
    try:
        dns_resolver.resolve(domain, "MX", lifetime=4)
        return True
    except dns_resolver.NXDOMAIN:
        return False
    except dns_resolver.NoAnswer:
        try:
            dns_resolver.resolve(domain, "A", lifetime=4)
            return True
        except Exception:
            return False
    except Exception:
        return True


def valid_email(e):
    e = e.strip().lower()
    if not EMAIL_FULL.match(e) or e.endswith(BAD_END):
        return False
    local, dom = e.split("@", 1)
    if any(j in dom for j in JUNK_EMAIL_DOMAIN) or local in JUNK_EMAIL_LOCAL:
        return False
    return mx_ok(dom)


def norm_phone(raw):
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789":
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if raw.startswith("+") and 11 <= len(digits) <= 15:
        return "+" + digits
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
        phones.add(p.strip())
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


def passes(info, emp_min, emp_max, rev_min, rev_max):
    emp, rev = info["emp"], info["rev"]
    if emp_min or emp_max:
        if not emp or not overlaps(emp[0], emp[1], emp_min, emp_max):
            return False
    if rev_min or rev_max:
        if not rev or not overlaps(rev[0], rev[1], rev_min, rev_max):
            return False
    return True


# ---------- description understanding ----------
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


def detect_count(desc):
    m = re.search(r"\b(\d[\d,]*)\s*\+?\s*(?:companies|company|leads|records|results|businesses|contacts|websites)\b", desc, re.I)
    if not m:
        m = re.search(r"\b(?:need|want|get|give|find)\s+(?:me\s+)?(\d[\d,]*)\b", desc, re.I)
    if m:
        try:
            return min(int(m.group(1).replace(",", "")), 10000)
        except Exception:
            return 0
    return 0


def get_secret(name, default=""):
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return default


@st.cache_data(show_spinner=False, ttl=3600)
def ai_parse(desc, api_key, models):
    prompt = (
        "Read this client's lead-generation request and reply with ONLY JSON in this shape: "
        '{"niches": ["up to 6 short business-type phrases for web search, e.g. real estate agency, dental clinic"], '
        '"locations": ["cities/states/regions mentioned; empty list if only a whole country"], '
        '"country": "USA or other country name or empty", '
        '"count": 0, "emp_min": 0, "emp_max": 0, "rev_min_m": 0, "rev_max_m": 0}. '
        "count = how many companies the client wants (0 if not stated). "
        "Never invent employee or revenue numbers: use 0 unless the client explicitly states them. "
        "Revenue is in millions of USD.\n\nRequest: " + desc
    )
    last = "koi model try nahi hua"
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                timeout=30,
            )
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                continue
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text), ""
        except Exception as e:
            last = f"{model}: {e}"
    return None, last


def build_queries(niches, locations, country, target):
    niches = [n for n in niches if n] or [""]
    locs = [l for l in locations if l]
    is_us = country.strip().lower() in US_NAMES
    first_locs = locs or ([country] if country else [""])
    variants = ["{n} {l}", "best {n} {l}", "top {n} {l}", "{n} {l} contact us", "{n} {l} services", "local {n} {l}"]
    queries = []
    for v in variants:
        for l in first_locs:
            for n in niches:
                queries.append(" ".join(v.format(n=n, l=l).split()))
    if target and is_us and not locs:
        for state in US_STATES:
            for n in niches:
                queries.append(f"{n} {state}")
        for state in US_STATES:
            for n in niches:
                queries.append(f"best {n} in {state}")
    return list(dict.fromkeys(queries))


def discover(queries, target, status):
    found = {}
    if DDGS is None:
        return found, 0
    start = time.time()
    done = 0
    goal = int(target * 1.6) if target else 0
    with SEARCH_LOCK:
        for q in queries:
            if time.time() - start > DISCOVER_SECONDS:
                break
            try:
                results = DDGS().text(q, max_results=30)
            except Exception:
                results = []
            for r in results or []:
                host = urlparse(r.get("href", "")).netloc.lower().replace("www.", "")
                if not host or any(b in host for b in BLOCK_DOMAINS):
                    continue
                found.setdefault(host, "")
            done += 1
            status.write(f"Step 1: search {done}/{len(queries)} | companies ki websites mili: {len(found)}")
            if goal and len(found) >= goal:
                break
            time.sleep(0.7)
    return found, done


def name_for_domain(domain):
    html = fetch("https://" + domain)
    if not html:
        return None
    return company_from_html(html, domain)


# ---------- per-site processing ----------
def pick_email(emails, domain):
    stem = domain.split(".")[0]
    own = [e for e in emails if stem in e.split("@")[-1]]
    pool = own or emails
    for pref in ("info", "contact", "hello", "office", "admin", "support", "sales"):
        for e in pool:
            if e.split("@")[0] == pref:
                return e
    return pool[0] if pool else ""


def process_site(url, hint, use_search):
    base = normalize(url)
    domain = urlparse(base).netloc.replace("www.", "")
    html = fetch(base)
    if not html:
        return None

    company = hint.strip() or company_from_html(html, domain)
    e, p, s, pages = extract_contacts(html, base)
    pdata = [(e, p, s)]
    people = extract_people(html, base)
    address, tel, desc = extract_profile(html)

    for pg in list(pages)[:6]:
        h = fetch(pg)
        if h:
            e2, p2, s2, _ = extract_contacts(h, pg)
            pdata.append((e2, p2, s2))
            people += extract_people(h, pg)
            a2, t2, d2 = extract_profile(h)
            address, tel, desc = address or a2, tel or t2, desc or d2

    stem = domain.split(".")[0]
    emails, phones, socials = [], [], {}
    seen_e, seen_p = set(), set()
    cand_phones = [tel] if tel else []
    for e_, p_, s_ in pdata:
        for x in sorted(e_):
            if x not in seen_e and valid_email(x):
                seen_e.add(x)
                emails.append(x)
        cand_phones += sorted(p_)
        for x in sorted(s_):
            plat = valid_social(x)
            if plat and plat not in socials:
                socials[plat] = x
    for x in cand_phones:
        n = norm_phone(x)
        if n and n not in seen_p:
            seen_p.add(n)
            phones.append(n)
    emails.sort(key=lambda m: stem not in m.split("@")[-1])

    if use_search:
        people += search_people(company, domain)
    uniq, seen_n = [], set()
    for x in people:
        k = x["Name"].lower()
        if k not in seen_n:
            seen_n.add(k)
            uniq.append(x)
    people = uniq

    row = {
        "Company": company,
        "Website": domain,
        "Address": address,
        "Phone": phones[0] if phones else "",
        "All phones": "; ".join(phones[:5]),
        "General email": pick_email(emails, domain),
        "All emails": "; ".join(emails[:8]),
        "People (name - title)": "; ".join(f"{x['Name']} - {x['Title']}" for x in people[:10]),
    }
    for col in SOCIAL_COLS:
        row[col] = socials.get(col, "")
    row["Description"] = desc

    filled = [row["Address"], row["Phone"], row["General email"], row["People (name - title)"]]
    filled += [row[c] for c in SOCIAL_COLS]
    row["_score"] = sum(1 for v in filled if v)

    base_cols = {"Company": company, "Website": domain}
    return {"row": row, "people": [dict(base_cols, **x) for x in people]}


def safe_process(url, hint, use_search):
    try:
        return process_site(url, hint, use_search)
    except Exception:
        return None


def run_batch(items, use_search, status):
    seen, unique = set(), []
    for url, hint in items:
        key = urlparse(normalize(url)).netloc.replace("www.", "")
        if key and key not in seen:
            seen.add(key)
            unique.append((url, hint))
    res = {"rows": [], "people": [], "failed": [], "stats": ""}
    if not unique:
        return res
    bar = st.progress(0.0)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(safe_process, u, c, use_search): u for u, c in unique}
        for f in as_completed(futures):
            out = f.result()
            if out:
                res["rows"].append(out["row"])
                res["people"] += out["people"]
            else:
                res["failed"].append(futures[f])
            done += 1
            bar.progress(done / len(unique))
            status.write(f"Step 3: websites se data nikal raha hun: {done}/{len(unique)} (kaam ki: {len(res['rows'])})")
    return res


# ---------- results ----------
def show_results():
    res = st.session_state.get("res")
    if not res:
        return
    if res.get("stats"):
        st.success(res["stats"])
    rows = res["rows"]
    if rows:
        df = pd.DataFrame(rows)
        if "_score" in df.columns:
            df = df.sort_values("_score", ascending=False).drop(columns=["_score"])
        df = df.drop_duplicates(subset=["Website"])
        st.dataframe(df, use_container_width=True)
        st.download_button("Download CSV (sab data)", df.to_csv(index=False).encode("utf-8-sig"),
                           "leads.csv", key="dl_master")
        if res["people"]:
            pdf = pd.DataFrame(res["people"]).drop_duplicates(subset=["Website", "Name"])
            st.download_button("Download People CSV (alag, har banda ek row)", pdf.to_csv(index=False).encode("utf-8-sig"),
                               "people.csv", key="dl_people")
    else:
        st.warning("Koi data nahi nikla.")
    if res["failed"]:
        with st.expander(f"{len(res['failed'])} websites khul nahi sakin"):
            st.write(", ".join(res["failed"]))


def read_sites_csv(up):
    df0 = pd.read_csv(up, header=None, dtype=str)
    first = str(df0.iloc[0, 0]).strip() if len(df0) else ""
    if re.match(r"^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+(/.*)?$", first):
        df0.columns = [f"Column {i + 1}" for i in range(df0.shape[1])]
        return df0, False
    up.seek(0)
    return pd.read_csv(up, dtype=str), True


# ---------- UI ----------
tab_search, tab_bulk, tab_single = st.tabs(["Client description search", "Bulk websites", "Single website"])

with tab_search:
    st.write("Client ki need likho: niche, country/city, kitni companies chahiye (jaise 100, 500), aur chaho to employees/revenue. Baqi sab tool khud karega aur ek CSV dega.")
    desc = st.text_area("Client description", height=130,
                        placeholder="Target: USA. Industry: real estate and dental. Need 200 companies")

    niches, locations, country, target = [], [], "", 0
    d_emp_min = d_emp_max = 0
    d_rev_min = d_rev_max = 0.0

    if desc.strip():
        d_emp_min, d_emp_max, d_rev_min, d_rev_max = detect_filters(desc)
        q_clean = clean_description(desc)
        niches = [q_clean]
        target = detect_count(desc)

        api_key = get_secret("GEMINI_API_KEY")
        if api_key:
            wanted = [get_secret("GEMINI_MODEL"), "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]
            models = tuple(m for m in dict.fromkeys(wanted) if m)
            ai, err = ai_parse(desc, api_key, models)
            if ai:
                niches = [str(x) for x in ai.get("niches", [])][:6] or niches
                locations = [str(x) for x in ai.get("locations", [])][:20]
                country = str(ai.get("country", "") or "")
                target = int(ai.get("count", 0) or 0) or target
                d_emp_min = int(ai.get("emp_min", 0) or 0)
                d_emp_max = int(ai.get("emp_max", 0) or 0)
                d_rev_min = float(ai.get("rev_min_m", 0) or 0)
                d_rev_max = float(ai.get("rev_max_m", 0) or 0)
                st.success("AI ne description samjhi. Niches: " + " | ".join(niches)
                           + (" | Locations: " + ", ".join(locations) if locations else "")
                           + (f" | Country: {country}" if country else ""))
            else:
                st.warning("AI call fail hui, simple rules use ho rahe hain. Wajah: " + err)
        else:
            st.info("AI off hai (Secrets mein GEMINI_API_KEY nahi mili). Simple rules use ho rahe hain.")

        if not re.search(r"\d", desc):
            d_emp_min = d_emp_max = 0
            d_rev_min = d_rev_max = 0.0
        target = min(target, 10000)

        st.caption(f"Target: {target or 'jitni mil jayen sab'} companies | Employees: {d_emp_min or '-'} se {d_emp_max or 'upar'} "
                   f"| Revenue ($M): {d_rev_min or '-'} se {d_rev_max or 'upar'}")

    if st.button("Dhoondo", key="search_btn") and desc.strip():
        if DDGS is None:
            st.error("Search library (ddgs) install nahi hui. requirements.txt mein `ddgs` likha hona chahiye, phir app Reboot karo.")
            st.stop()

        filters_on = bool(d_emp_min or d_emp_max or d_rev_min or d_rev_max)
        status = st.empty()
        queries = build_queries(niches, locations, country, target)
        found, searches_done = discover(queries, target, status)
        domains = list(found.keys())
        if not domains:
            st.error("Search se koi website nahi mili. Ho sakta hai free search ne thori der ke liye rok lagayi ho. 2-3 minute baad dobara try karo, ya description mein niche aur location saaf likho.")
            st.stop()

        limit = math.ceil(target * 1.4) if target else HARD_CAP
        domains = domains[:limit]
        stats = [f"{searches_done} searches se {len(found)} websites mili"]

        infos = {}
        kept = [(d, "") for d in domains]
        if filters_on:
            status.write(f"Step 2: {len(domains)} websites ka size/revenue check ho raha hai (slow)...")
            names = {}
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(name_for_domain, d): d for d in domains[:300]}
                for f in as_completed(futs):
                    nm = f.result()
                    if nm:
                        names[futs[f]] = nm
            kept = []
            bar2 = st.progress(0.0)
            for n, (domain, name) in enumerate(names.items()):
                status.write(f"Step 2: size/revenue {n + 1}/{len(names)}: {name}")
                bar2.progress((n + 1) / max(len(names), 1))
                info = enrich(name)
                if passes(info, d_emp_min, d_emp_max, d_rev_min, d_rev_max):
                    kept.append((domain, name))
                    infos[domain] = info
            stats.append(f"size/revenue filter ke baad {len(kept)} bachin")

        if not kept:
            st.session_state["res"] = None
            st.warning("Aap ki range mein kisi ka size/revenue data nahi mila. Range wider karo ya description se size/revenue hata do.")
        else:
            res = run_batch(kept, len(kept) <= 40, status)
            for row in res["rows"]:
                info = infos.get(row["Website"])
                if info:
                    row["Employees"] = fmt_emp(info["emp"])
                    row["Revenue (Estimate)"] = fmt_rev(info["rev"])
                    row["Source link"] = info["link"]
                    row["Confidence"] = info["confidence"]
            res["rows"].sort(key=lambda r: r["_score"], reverse=True)
            if target:
                res["rows"] = res["rows"][:target]
            stats.append(f"{len(res['rows'])} companies ka data tayyar")
            res["stats"] = " | ".join(stats)
            st.session_state["res"] = res
            status.write("Mukammal!")

with tab_bulk:
    st.write("Har line mein ek website. Company ka naam dena ho to comma lagao: `example.com, Example Corp`.")
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
        st.info(f"{len(items)} websites mili.")
    if st.button("Bulk start karo", key="bulk_btn") and items:
        status_b = st.empty()
        res = run_batch(items[:HARD_CAP], len(items) <= 40, status_b)
        res["rows"].sort(key=lambda r: r["_score"], reverse=True)
        res["stats"] = f"{len(res['rows'])} websites ka data tayyar"
        st.session_state["res"] = res
        status_b.write("Mukammal!")

with tab_single:
    s1, s2 = st.columns(2)
    url_input = s1.text_input("Website ka link", placeholder="example.com")
    company_input = s2.text_input("Company ka naam (optional)")
    if st.button("Dhoondo", key="single_btn") and url_input:
        status_s = st.empty()
        res = run_batch([(url_input, company_input)], True, status_s)
        res["stats"] = f"{len(res['rows'])} website ka data tayyar"
        st.session_state["res"] = res
        status_s.write("Mukammal!")

show_results()
