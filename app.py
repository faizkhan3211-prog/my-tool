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
from requests.adapters import HTTPAdapter

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

WORKERS = 24
HARD_CAP = 3000
SEARCH_LOCK = threading.Lock()
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
OSM_HEADERS = {"User-Agent": "FaizLeadFinder/1.0"}

SESSION = requests.Session()
SESSION.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=64))
SESSION.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=64))

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
EMAIL_FULL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'’-]+){1,2}$")
ADDRESS_RE = re.compile(
    r"(\d{1,6}\s[A-Za-z0-9 .'#-]{3,60}?),?\s+([A-Za-z][A-Za-z .'-]{1,30}?),?\s+"
    r"([A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?)\.?,?\s+(\d{5})(?:-\d{4})?"
)
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
                 "wiktionary.", "youtube.", "tiktok.", "reddit.", "pinterest.", "nytimes.", "timeout.",
                 "bbb.org", "manta.", "birdeye.", "twitter.", "x.com", "google.", "bing.", "vagaro.",
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
                 "dexknows.", "local.com", "hotfrog.", "brownbook.", "showmelocal.", "bizapedia.",
                 "grokipedia.", "openstreetmap.", "linkedin.", "wix.com", "weebly.com", "godaddysites."]

STATE_LIST = [
    ("California", "CA"), ("Texas", "TX"), ("Florida", "FL"), ("New York", "NY"), ("Pennsylvania", "PA"),
    ("Illinois", "IL"), ("Ohio", "OH"), ("Georgia", "GA"), ("North Carolina", "NC"), ("Michigan", "MI"),
    ("New Jersey", "NJ"), ("Virginia", "VA"), ("Washington", "WA"), ("Arizona", "AZ"), ("Tennessee", "TN"),
    ("Massachusetts", "MA"), ("Indiana", "IN"), ("Maryland", "MD"), ("Missouri", "MO"), ("Colorado", "CO"),
    ("Wisconsin", "WI"), ("Minnesota", "MN"), ("South Carolina", "SC"), ("Alabama", "AL"),
    ("Louisiana", "LA"), ("Kentucky", "KY"), ("Oregon", "OR"), ("Oklahoma", "OK"), ("Connecticut", "CT"),
    ("Utah", "UT"), ("Iowa", "IA"), ("Nevada", "NV"), ("Arkansas", "AR"), ("Mississippi", "MS"),
    ("Kansas", "KS"), ("New Mexico", "NM"), ("Nebraska", "NE"), ("Idaho", "ID"), ("West Virginia", "WV"),
    ("Hawaii", "HI"), ("New Hampshire", "NH"), ("Maine", "ME"), ("Montana", "MT"), ("Rhode Island", "RI"),
    ("Delaware", "DE"), ("South Dakota", "SD"), ("North Dakota", "ND"), ("Alaska", "AK"),
    ("Vermont", "VT"), ("Wyoming", "WY"),
]
STATE_BY_NAME = {n.lower(): c for n, c in STATE_LIST}
CODE2NAME = {c: n for n, c in STATE_LIST}
US_NAMES = ("usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america")
ALIASES = {"nyc": "New York City", "la": "Los Angeles", "sf": "San Francisco"}

# regex in description -> (OpenStreetMap tags, relevance keywords)
TAG_MAP = [
    (r"dent(al|ist)|orthodont", ["amenity=dentist"], ["dental", "dentist", "orthodont"]),
    (r"real estate|realtor|realty|estate agen|property agen", ["office=estate_agent"],
     ["estate", "realtor", "realty", "property", "properties", "homes", "broker"]),
    (r"lawyer|attorney|law firm", ["office=lawyer"], ["law", "attorney", "lawyer"]),
    (r"accountant|\bcpa\b|accounting", ["office=accountant"], ["account", "cpa", "tax"]),
    (r"insurance", ["office=insurance"], ["insurance"]),
    (r"doctor|physician", ["amenity=doctors"], ["doctor", "physician", "medical"]),
    (r"\bclinic", ["amenity=clinic"], ["clinic", "medical", "health"]),
    (r"veterinar|\bvets?\b", ["amenity=veterinary"], ["veterinar", "animal", "pet"]),
    (r"pharmac", ["amenity=pharmacy"], ["pharmacy"]),
    (r"\bgyms?\b|fitness", ["leisure=fitness_centre"], ["gym", "fitness"]),
    (r"restaurant", ["amenity=restaurant"], ["restaurant", "menu"]),
    (r"hotel", ["tourism=hotel"], ["hotel", "rooms"]),
    (r"salon|\bhair", ["shop=hairdresser"], ["salon", "hair"]),
    (r"med ?spa|aesthetic|beauty|\bspa\b", ["shop=beauty"], ["spa", "aesthetic", "beauty", "skin"]),
    (r"auto repair|mechanic", ["shop=car_repair"], ["auto", "repair", "mechanic"]),
    (r"car dealer", ["shop=car"], ["dealer", "car"]),
    (r"plumb", ["craft=plumber"], ["plumb"]),
    (r"electrician|electrical", ["craft=electrician"], ["electric"]),
    (r"hvac|heating|air conditioning", ["craft=hvac"], ["hvac", "heating", "cooling"]),
    (r"roof", ["craft=roofer"], ["roof"]),
    (r"construction|contractor|builder", ["office=construction_company", "craft=builder"],
     ["construction", "contractor", "builder"]),
    (r"architect", ["office=architect"], ["architect"]),
    (r"marketing|advertising", ["office=advertising_agency"], ["marketing", "advertising"]),
    (r"software|it company|it services", ["office=it"], ["software", "technology"]),
    (r"\bbanks?\b", ["amenity=bank"], ["bank"]),
    (r"daycare|child care", ["amenity=kindergarten"], ["daycare", "child care"]),
]
GENERIC_WORDS = {"company", "companies", "services", "service", "near", "best", "local", "agency", "agent",
                 "agents", "clinic", "office", "group", "business", "businesses", "target", "industry",
                 "countries", "country", "need", "want", "both", "others", "other"}

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
        r = SESSION.get(url, headers=HEADERS, timeout=(4, 8), stream=True)
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            r.close()
            return None
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > 1_000_000:
                break
        r.close()
        return buf.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
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
        dns_resolver.resolve(domain, "MX", lifetime=3)
        return True
    except dns_resolver.NXDOMAIN:
        return False
    except dns_resolver.NoAnswer:
        try:
            dns_resolver.resolve(domain, "A", lifetime=3)
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


def norm_state(s):
    s = (s or "").strip().rstrip(".")
    if len(s) == 2:
        return CODE2NAME.get(s.upper(), s)
    return s


def zip5(z):
    m = re.search(r"\d{5}", z or "")
    return m.group(0) if m else ""


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

    text = soup.get_text(" ")[:200000]
    for e in EMAIL_RE.findall(text):
        emails.add(e.lower())
    for p in PHONE_RE.findall(text):
        phones.add(p.strip())
    return emails, phones, socials, pages


def parse_us_address(text):
    m = ADDRESS_RE.search(text or "")
    if not m:
        return None
    return {"street": " ".join(m.group(1).split()), "city": " ".join(m.group(2).split()),
            "state": m.group(3), "zip": m.group(4)}


def extract_profile(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {"street": "", "city": "", "state": "", "zip": "", "phone": "", "desc": ""}

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
            if isinstance(addr, dict) and not (out["street"] or out["city"]):
                out["street"] = str(addr.get("streetAddress", "") or "").strip()
                out["city"] = str(addr.get("addressLocality", "") or "").strip()
                out["state"] = str(addr.get("addressRegion", "") or "").strip()
                out["zip"] = str(addr.get("postalCode", "") or "").strip()
            elif isinstance(addr, str) and not out["street"]:
                pa = parse_us_address(addr)
                if pa:
                    out.update(pa)
            tel = node.get("telephone")
            if isinstance(tel, str) and tel.strip() and not out["phone"]:
                out["phone"] = tel.strip()
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))

    if not out["street"]:
        tag = soup.find("address")
        if tag:
            pa = parse_us_address(" ".join(tag.get_text(" ").split()))
            if pa:
                out.update(pa)
    if not out["street"]:
        pa = parse_us_address(soup.get_text(" ")[:200000])
        if pa:
            out.update(pa)

    md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    if md and md.get("content"):
        out["desc"] = md["content"].strip()[:200]
    return out


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


# ---------- size / revenue (sirf tab jab description mein ho) ----------
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


def tags_from_text(text):
    low = text.lower()
    tags, kws = [], []
    for pat, t, k in TAG_MAP:
        if re.search(pat, low):
            tags += t
            kws += k
    return list(dict.fromkeys(tags)), list(dict.fromkeys(kws))


def get_secret(name, default=""):
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return default


@st.cache_data(show_spinner=False, ttl=3600)
def ai_parse(desc, api_key, models):
    prompt = (
        "Read this client's lead-generation request and reply with ONLY JSON in this shape: "
        '{"niches": ["up to 6 short business-type phrases, e.g. real estate agency, dental clinic"], '
        '"locations": ["cities/states/regions mentioned; empty list if only a whole country"], '
        '"country": "USA or other country name or empty", '
        '"osm_tags": ["up to 6 OpenStreetMap tags as key=value for these business types, e.g. amenity=dentist, office=estate_agent; empty list if unsure"], '
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


# ---------- discovery: OpenStreetMap ----------
@lru_cache(maxsize=256)
def geocode_area(q):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 1}, headers=OSM_HEADERS, timeout=15)
        data = r.json()
        if not data:
            return None
        d = data[0]
        if d.get("osm_type") == "relation":
            return 3600000000 + int(d["osm_id"])
        if d.get("osm_type") == "way":
            return 2400000000 + int(d["osm_id"])
    except Exception:
        return None
    return None


def area_clause(loc, country):
    name = ALIASES.get(loc.strip().lower(), loc.strip())
    code = STATE_BY_NAME.get(name.lower())
    if code:
        return f'area["ISO3166-2"="US-{code}"]->.a;'
    q = name
    if country.strip().lower() in US_NAMES and "usa" not in name.lower():
        q = f"{name}, USA"
    aid = geocode_area(q)
    time.sleep(1)
    if aid:
        return f"area(id:{aid})->.a;"
    return None


def osm_fetch(clause, tag_list):
    parts = []
    for kv in tag_list:
        k, v = kv.split("=", 1)
        for wk in ("website", "contact:website"):
            parts.append(f'nwr["{k}"="{v}"]["{wk}"](area.a);')
    q = "[out:json][timeout:180];" + clause + "(" + "".join(parts) + ");out tags;"
    for ep in OVERPASS:
        try:
            r = requests.post(ep, data={"data": q}, headers=OSM_HEADERS, timeout=200)
            if r.status_code == 200:
                return r.json().get("elements", [])
        except Exception:
            pass
        time.sleep(3)
    return None


def osm_category(tags):
    for k in ("amenity", "office", "shop", "craft", "healthcare", "tourism", "leisure"):
        if tags.get(k):
            return tags[k]
    return ""


def discover_osm(tag_list, locations, country, goal, status, budget):
    found, notes = {}, []
    start = time.time()
    if locations:
        regions = list(locations)
    elif country.strip().lower() in US_NAMES:
        regions = [n for n, _ in STATE_LIST]
    elif country.strip():
        regions = [country]
    else:
        return found, ["Country/location nahi mili, is liye OpenStreetMap skip hui"]
    for loc in regions:
        if time.time() - start > budget:
            notes.append("OpenStreetMap ka time limit khatam hua")
            break
        clause = area_clause(loc, country)
        if not clause:
            notes.append(f"{loc}: OpenStreetMap par area nahi mili")
            continue
        els = osm_fetch(clause, tag_list)
        if els is None:
            notes.append(f"{loc}: OpenStreetMap server ne jawab nahi diya")
            continue
        for el in els:
            tags = el.get("tags", {})
            site = (tags.get("website") or tags.get("contact:website") or "").split(";")[0].strip()
            if not site:
                continue
            host = urlparse(normalize(site)).netloc.lower().replace("www.", "")
            if not host or "." not in host or any(b in host for b in BLOCK_DOMAINS):
                continue
            found.setdefault(host, {"hint": tags.get("name", ""), "tags": tags, "src": "OpenStreetMap"})
        status.write(f"Step 1: {loc} mukammal | ab tak websites mili: {len(found)}")
        if goal and len(found) >= goal:
            break
        time.sleep(1.5)
    return found, notes


# ---------- discovery: web search (support) ----------
def build_queries(niches, locations, country, goal):
    niches = [n for n in niches if n] or [""]
    locs = [l for l in locations if l] or ([country] if country else [""])
    variants = ["{n} {l}", "best {n} {l}", "{n} {l} contact"]
    queries = []
    for v in variants:
        for l in locs:
            for n in niches:
                queries.append(" ".join(v.format(n=n, l=l).split()))
    return list(dict.fromkeys(queries))


def discover_web(niches, locations, country, kws, goal, status, budget, known):
    found = {}
    if DDGS is None:
        return found
    queries = build_queries(niches, locations, country, goal)
    start = time.time()
    done = 0
    with SEARCH_LOCK:
        for q in queries:
            if time.time() - start > budget:
                break
            try:
                results = DDGS().text(q, max_results=30)
            except Exception:
                results = []
            for r in results or []:
                host = urlparse(r.get("href", "")).netloc.lower().replace("www.", "")
                if not host or host in known or any(b in host for b in BLOCK_DOMAINS):
                    continue
                blob = (r.get("title", "") + " " + r.get("body", "") + " " + host).lower()
                if kws and not any(k in blob for k in kws):
                    continue
                found.setdefault(host, {"hint": "", "tags": None, "src": "Web search", "kws": kws})
            done += 1
            status.write(f"Step 1b: web search {done}/{len(queries)} | extra websites: {len(found)}")
            if goal and len(known) + len(found) >= goal:
                break
            time.sleep(0.4)
    return found


def name_for_domain(domain):
    html = fetch("https://" + domain)
    if not html:
        return None
    return company_from_html(html, domain)


# ---------- per-site processing ----------
def osm_contact(tags):
    street = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x)
    phones, emails = [], []
    for key in ("phone", "contact:phone"):
        for part in (tags.get(key) or "").split(";"):
            if part.strip():
                phones.append(part.strip())
    for key in ("email", "contact:email"):
        for part in (tags.get(key) or "").split(";"):
            if part.strip():
                emails.append(part.strip().lower())
    socials = {}
    specs = (
        ("Facebook", ("contact:facebook", "facebook"), "https://facebook.com/"),
        ("Instagram", ("contact:instagram", "instagram"), "https://instagram.com/"),
        ("Twitter/X", ("contact:twitter", "twitter"), "https://twitter.com/"),
        ("LinkedIn", ("contact:linkedin", "linkedin"), ""),
        ("YouTube", ("contact:youtube", "youtube"), ""),
    )
    for plat, keys, prefix in specs:
        for k in keys:
            v = (tags.get(k) or "").strip()
            if not v:
                continue
            if v.startswith("http"):
                url = v
            elif prefix:
                url = prefix + v.lstrip("@/")
            else:
                url = ""
            if url and valid_social(url) == plat:
                socials[plat] = url.rstrip("/")
                break
    return {"street": street, "city": tags.get("addr:city", ""), "state": tags.get("addr:state", ""),
            "zip": tags.get("addr:postcode", ""), "phones": phones, "emails": emails, "socials": socials}


def process_site(item, use_search):
    base = normalize(item["url"])
    domain = urlparse(base).netloc.replace("www.", "")
    tags = item.get("tags") or {}
    osm = osm_contact(tags) if tags else None
    html = fetch(base)

    kws = item.get("kws")
    if html and kws and item.get("src") == "Web search":
        low = html[:300000].lower()
        if not any(k in low for k in kws):
            return None
    if not html and not osm:
        return None

    hint = (item.get("hint") or "").strip()
    company = hint or (company_from_html(html, domain) if html else domain)

    pdata, people = [], []
    prof = {"street": "", "city": "", "state": "", "zip": "", "phone": "", "desc": ""}
    if html:
        e, p, s, pages = extract_contacts(html, base)
        pdata.append((e, p, s))
        people += extract_people(html, base)
        prof = extract_profile(html)
        ordered = sorted(pages, key=lambda u: 0 if "contact" in u.lower() else 1)
        for pg in ordered[:3]:
            h = fetch(pg)
            if h:
                e2, p2, s2, _ = extract_contacts(h, pg)
                pdata.append((e2, p2, s2))
                people += extract_people(h, pg)
                p3 = extract_profile(h)
                for k in prof:
                    prof[k] = prof[k] or p3[k]

    addr = {}
    for k in ("street", "city", "state", "zip"):
        addr[k] = (osm[k] if osm else "") or prof[k]
    addr["state"] = norm_state(addr["state"])
    addr["zip"] = zip5(addr["zip"])

    stem = domain.split(".")[0]
    cand_emails = list(osm["emails"]) if osm else []
    cand_phones = list(osm["phones"]) if osm else []
    if prof["phone"]:
        cand_phones.append(prof["phone"])
    socials = dict(osm["socials"]) if osm else {}
    for e_, p_, s_ in pdata:
        cand_emails += sorted(e_)
        cand_phones += sorted(p_)
        for x in sorted(s_):
            plat = valid_social(x)
            if plat and plat not in socials:
                socials[plat] = x

    emails, seen_e = [], set()
    for x in cand_emails:
        x = x.strip().lower()
        if x and x not in seen_e and valid_email(x):
            seen_e.add(x)
            emails.append(x)
    emails.sort(key=lambda m: stem not in m.split("@")[-1])
    phones, seen_p = [], set()
    for x in cand_phones:
        n = norm_phone(x)
        if n and n not in seen_p:
            seen_p.add(n)
            phones.append(n)

    if not html and not (phones or emails or addr["street"]):
        return None

    if use_search:
        people += search_people(company, domain)
    uniq, seen_n = [], set()
    for x in people:
        k = x["Name"].lower()
        if k not in seen_n:
            seen_n.add(k)
            uniq.append(x)
    people = uniq

    general = ""
    for pref in ("info", "contact", "hello", "office", "admin", "support", "sales"):
        general = next((m for m in emails if m.split("@")[0] == pref), "")
        if general:
            break
    general = general or (emails[0] if emails else "")

    row = {
        "Company": company,
        "Website": domain,
        "Website status": "OK" if html else "Not reachable",
        "Category": osm_category(tags) if tags else "",
        "Street": addr["street"],
        "City": addr["city"],
        "State": addr["state"],
        "ZIP": addr["zip"],
        "Phone": phones[0] if phones else "",
        "All phones": "; ".join(phones[:5]),
        "General email": general,
        "All emails": "; ".join(emails[:8]),
        "People (name - title)": "; ".join(f"{x['Name']} - {x['Title']}" for x in people[:10]),
    }
    for col in SOCIAL_COLS:
        row[col] = socials.get(col, "")
    row["Source"] = item.get("src", "")
    row["Description"] = prof["desc"]

    filled = [row["Street"], row["City"], row["Phone"], row["General email"], row["People (name - title)"]]
    filled += [row[c] for c in SOCIAL_COLS]
    row["_score"] = sum(1 for v in filled if v)

    base_cols = {"Company": company, "Website": domain}
    return {"row": row, "people": [dict(base_cols, **x) for x in people]}


def safe_process(item, use_search):
    try:
        return process_site(item, use_search)
    except Exception:
        return None


def run_batch(items, use_search, status):
    seen, unique = set(), []
    for it in items:
        key = urlparse(normalize(it["url"])).netloc.replace("www.", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    res = {"rows": [], "people": [], "failed": [], "stats": ""}
    if not unique:
        return res
    bar = st.progress(0.0)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(safe_process, it, use_search): it["url"] for it in unique}
        for f in as_completed(futures):
            out = f.result()
            if out:
                res["rows"].append(out["row"])
                res["people"] += out["people"]
            else:
                res["failed"].append(futures[f])
            done += 1
            if done % 5 == 0 or done == len(unique):
                bar.progress(done / len(unique))
                status.write(f"Step 2: websites se data nikal raha hun: {done}/{len(unique)} | kaam ke rows: {len(res['rows'])}")
    return res


# ---------- results ----------
COLS = ["Company", "Website", "Website status", "Category", "Street", "City", "State", "ZIP", "Phone",
        "All phones", "General email", "All emails", "People (name - title)"] + SOCIAL_COLS + ["Source", "Description"]


def show_results():
    res = st.session_state.get("res")
    if not res:
        return
    if res.get("stats"):
        st.success(res["stats"])
    for n in res.get("notes", []):
        st.caption("Note: " + n)
    rows = res["rows"]
    if rows:
        df = pd.DataFrame(rows)
        if "_score" in df.columns:
            df = df.sort_values("_score", ascending=False).drop(columns=["_score"])
        df = df.drop_duplicates(subset=["Website"])
        cols = [c for c in COLS if c in df.columns] + [c for c in df.columns if c not in COLS]
        df = df[cols]
        st.dataframe(df, use_container_width=True)
        st.download_button("Download CSV (sab data)", df.to_csv(index=False).encode("utf-8-sig"),
                           "leads.csv", key="dl_master")
        if res["people"]:
            pdf = pd.DataFrame(res["people"]).drop_duplicates(subset=["Website", "Name"])
            st.download_button("Download People CSV (alag, har banda ek row)",
                               pdf.to_csv(index=False).encode("utf-8-sig"), "people.csv", key="dl_people")
    else:
        st.warning("Koi data nahi nikla.")
    if res["failed"]:
        with st.expander(f"{len(res['failed'])} websites skip hui (khuli nahi ya niche se match nahi)"):
            st.write(", ".join(res["failed"]))


def read_sites_csv(up):
    df0 = pd.read_csv(up, header=None, dtype=str)
    first = str(df0.iloc[0, 0]).strip() if len(df0) else ""
    if re.match(r"^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+(/.*)?$", first):
        df0.columns = [f"Column {i + 1}" for i in range(df0.shape[1])]
        return df0, False
    up.seek(0)
    return pd.read_csv(up, dtype=str), True


def manual_item(url, hint=""):
    return {"url": url, "hint": hint, "tags": None, "src": "Manual", "kws": None}


# ---------- UI ----------
tab_search, tab_bulk, tab_single = st.tabs(["Client description search", "Bulk websites", "Single website"])

with tab_search:
    st.write("Client ki need likho: niche, country/city/state, kitni companies chahiye (jaise 100, 500), aur chaho to employees/revenue. Baqi sab tool khud karega aur ek CSV dega.")
    desc = st.text_area("Client description", height=130,
                        placeholder="Target: USA. Industry: real estate and dental. Need 200 companies")

    niches, locations, country, target = [], [], "", 0
    osm_tags, kws = [], []
    d_emp_min = d_emp_max = 0
    d_rev_min = d_rev_max = 0.0

    if desc.strip():
        d_emp_min, d_emp_max, d_rev_min, d_rev_max = detect_filters(desc)
        q_clean = clean_description(desc)
        niches = [q_clean]
        target = detect_count(desc)
        osm_tags, kws = tags_from_text(desc)
        locations = [n for n, _ in STATE_LIST if re.search(r"\b" + re.escape(n) + r"\b", desc, re.I)]
        if re.search(r"\b(usa|u\.s\.a?\.?|united states|america)\b", desc, re.I):
            country = "USA"

        api_key = get_secret("GEMINI_API_KEY")
        if api_key:
            wanted = [get_secret("GEMINI_MODEL"), "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]
            models = tuple(m for m in dict.fromkeys(wanted) if m)
            ai, err = ai_parse(desc, api_key, models)
            if ai:
                niches = [str(x) for x in ai.get("niches", [])][:6] or niches
                ai_locs = [str(x) for x in ai.get("locations", []) if str(x).strip()][:20]
                locations = ai_locs or locations
                country = str(ai.get("country", "") or "") or country
                target = int(ai.get("count", 0) or 0) or target
                good = [str(t) for t in ai.get("osm_tags", []) if re.fullmatch(r"[a-z_:]+=[a-z_]+", str(t))]
                osm_tags = list(dict.fromkeys(osm_tags + good))
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

        if not kws:
            for n in niches:
                for w in re.findall(r"[a-z]{5,}", n.lower()):
                    if w not in GENERIC_WORDS:
                        kws.append(w)
            kws = list(dict.fromkeys(kws))
        if not re.search(r"\d", desc):
            d_emp_min = d_emp_max = 0
            d_rev_min = d_rev_max = 0.0
        target = min(target, 10000)

        st.caption(f"Target: {target or 'jitni mil jayen sab'} companies | OpenStreetMap tags: {', '.join(osm_tags) or 'koi nahi (sirf web search)'} "
                   f"| Employees: {d_emp_min or '-'} se {d_emp_max or 'upar'} | Revenue ($M): {d_rev_min or '-'} se {d_rev_max or 'upar'}")

    if st.button("Dhoondo", key="search_btn") and desc.strip():
        if not osm_tags and DDGS is None:
            st.error("Search library (ddgs) install nahi hui. requirements.txt mein `ddgs` likha hona chahiye, phir app Reboot karo.")
            st.stop()

        filters_on = bool(d_emp_min or d_emp_max or d_rev_min or d_rev_max)
        status = st.empty()
        goal = math.ceil(target * 1.25) if target else 0
        budget = 1200 if target > 500 else 600

        found, notes, n_osm, n_web = {}, [], 0, 0
        if osm_tags:
            osm_found, notes = discover_osm(osm_tags, locations, country, goal, status, budget)
            found.update(osm_found)
            n_osm = len(found)
        if (not osm_tags) or (goal and len(found) < goal) or len(found) < 30:
            web_found = discover_web(niches, locations, country, kws, goal, status, 180, found)
            found.update(web_found)
            n_web = len(web_found)

        if not found:
            st.error("Koi website nahi mili. " + " | ".join(notes) + " Niche ya location saaf likh kar dobara try karo, ya 2-3 minute baad dobara.")
            st.stop()

        items = [dict(url=d, **v) for d, v in found.items()]
        items = items[:(goal or HARD_CAP)]

        infos = {}
        if filters_on:
            status.write("Step 1c: size/revenue check ho raha hai (slow)...")
            cand = items[:300]
            need = [it for it in cand if not it["hint"]]
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(name_for_domain, it["url"]): it for it in need}
                for f in as_completed(futs):
                    futs[f]["hint"] = f.result() or ""
            kept = []
            bar2 = st.progress(0.0)
            cand = [it for it in cand if it["hint"]]
            for n, it in enumerate(cand):
                status.write(f"Step 1c: size/revenue {n + 1}/{len(cand)}: {it['hint']}")
                bar2.progress((n + 1) / max(len(cand), 1))
                info = enrich(it["hint"])
                if passes(info, d_emp_min, d_emp_max, d_rev_min, d_rev_max):
                    kept.append(it)
                    infos[it["url"]] = info
            items = kept

        if not items:
            st.session_state["res"] = None
            st.warning("Aap ki size/revenue range mein kisi ka data nahi mila. Range wider karo ya description se size/revenue hata do.")
        else:
            res = run_batch(items, len(items) <= 25, status)
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
            res["notes"] = notes
            res["stats"] = (f"OpenStreetMap se {n_osm} + web search se {n_web} websites mili | "
                            f"{len(res['rows'])} companies ka data tayyar")
            st.session_state["res"] = res
            status.write("Mukammal!")

with tab_bulk:
    st.write("Har line mein ek website. Company ka naam dena ho to comma lagao: `example.com, Example Corp`.")
    text = st.text_area("Websites ki list", height=180, placeholder="example.com\nanother.com, Another Corp")
    up = st.file_uploader("Ya CSV upload karo", type="csv")

    b_items = []
    for line in text.splitlines():
        if line.strip():
            parts = [x.strip() for x in line.split(",", 1)]
            b_items.append(manual_item(parts[0], parts[1] if len(parts) > 1 else ""))

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
                b_items.append(manual_item(w, comp))

    if b_items:
        st.info(f"{len(b_items)} websites mili.")
    if st.button("Bulk start karo", key="bulk_btn") and b_items:
        status_b = st.empty()
        res = run_batch(b_items[:HARD_CAP], len(b_items) <= 25, status_b)
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
        res = run_batch([manual_item(url_input, company_input)], True, status_s)
        res["stats"] = f"{len(res['rows'])} website ka data tayyar"
        st.session_state["res"] = res
        status_s.write("Mukammal!")

show_results()
