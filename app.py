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
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/search"

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
                 "grokipedia.", "openstreetmap.", "wix.com", "weebly.com", "godaddysites."]

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
AMBIGUOUS_CODES = {"IN", "OR", "ME", "OK", "HI"}
US_NAMES = ("usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america")
ALIASES = {"nyc": "New York City", "la": "Los Angeles", "sf": "San Francisco"}
CITY_TO_STATE = {
    "new york city": "New York", "brooklyn": "New York", "manhattan": "New York", "queens": "New York",
    "bronx": "New York", "staten island": "New York", "los angeles": "California",
    "san francisco": "California", "san diego": "California", "chicago": "Illinois", "houston": "Texas",
    "dallas": "Texas", "austin": "Texas", "miami": "Florida", "boston": "Massachusetts",
    "seattle": "Washington", "atlanta": "Georgia", "philadelphia": "Pennsylvania", "phoenix": "Arizona",
    "denver": "Colorado", "las vegas": "Nevada",
}

# regex in description -> (OpenStreetMap tags, relevance keywords, search phrase)
TAG_MAP = [
    (r"dent(al|ist)|orthodont", ["amenity=dentist"], ["dental", "dentist", "orthodont"], "dentist"),
    (r"real estate|realtor|realty|estate agen|property agen", ["office=estate_agent"],
     ["estate", "realtor", "realty", "property", "properties", "homes", "broker"], "real estate agency"),
    (r"lawyer|attorney|law firm", ["office=lawyer"], ["law", "attorney", "lawyer"], "law firm"),
    (r"accountant|\bcpa\b|accounting", ["office=accountant"], ["account", "cpa", "tax"], "accountant"),
    (r"insurance", ["office=insurance"], ["insurance"], "insurance agency"),
    (r"doctor|physician", ["amenity=doctors"], ["doctor", "physician", "medical"], "doctor"),
    (r"\bclinic", ["amenity=clinic"], ["clinic", "medical", "health"], "clinic"),
    (r"veterinar|\bvets?\b", ["amenity=veterinary"], ["veterinar", "animal", "pet"], "veterinarian"),
    (r"pharmac", ["amenity=pharmacy"], ["pharmacy"], "pharmacy"),
    (r"\bgyms?\b|fitness", ["leisure=fitness_centre"], ["gym", "fitness"], "gym"),
    (r"restaurant", ["amenity=restaurant"], ["restaurant", "menu"], "restaurant"),
    (r"hotel", ["tourism=hotel"], ["hotel", "rooms"], "hotel"),
    (r"salon|\bhair", ["shop=hairdresser"], ["salon", "hair"], "hair salon"),
    (r"med ?spa|aesthetic|beauty|\bspa\b", ["shop=beauty"], ["spa", "aesthetic", "beauty", "skin"], "med spa"),
    (r"auto repair|mechanic", ["shop=car_repair"], ["auto", "repair", "mechanic"], "auto repair"),
    (r"car dealer", ["shop=car"], ["dealer", "car"], "car dealer"),
    (r"plumb", ["craft=plumber"], ["plumb"], "plumber"),
    (r"electrician|electrical", ["craft=electrician"], ["electric"], "electrician"),
    (r"hvac|heating|air conditioning", ["craft=hvac"], ["hvac", "heating", "cooling"], "hvac contractor"),
    (r"roof", ["craft=roofer"], ["roof"], "roofing contractor"),
    (r"construction|contractor|builder", ["office=construction_company", "craft=builder"],
     ["construction", "contractor", "builder"], "construction company"),
    (r"architect", ["office=architect"], ["architect"], "architect"),
    (r"marketing|advertising", ["office=advertising_agency"], ["marketing", "advertising"], "marketing agency"),
    (r"software|it company|it services", ["office=it"], ["software", "technology"], "software company"),
    (r"\bbanks?\b", ["amenity=bank"], ["bank"], "bank"),
    (r"daycare|child care", ["amenity=kindergarten"], ["daycare", "child care"], "daycare"),
]
GENERIC_WORDS = {"company", "companies", "services", "service", "near", "best", "local", "agency", "agent",
                 "agents", "clinic", "office", "group", "business", "businesses", "target", "industry",
                 "countries", "country", "need", "want", "both", "others", "other", "each"}

UNIT = r"(?:billion|million|thousand|bn|mm|b|m|k)"
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*\.?\d*)\s*(billion|million|thousand|bn|mm|b|m|k)?\b", re.I)
EMP_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:-|–|to)\s*(\d[\d,]*)\+?\s*(?:employees|staff|team members)", re.I)
EMP_ONE_RE = re.compile(r"(\d[\d,]*)\+?\s*(?:employees|staff)", re.I)


# ---------- basic helpers ----------
def normalize(url):
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


def site_key(url):
    if str(url).startswith("osm-"):
        return str(url)
    return urlparse(normalize(url)).netloc.replace("www.", "")


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


def decode_cf(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except Exception:
        return ""


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


def norm_loc(loc):
    name = ALIASES.get(loc.strip().lower(), loc.strip())
    if len(name) == 2 and name.upper() in CODE2NAME:
        return CODE2NAME[name.upper()]
    return name


def wanted_states(locations):
    if not locations:
        return None
    out = set()
    for l in locations:
        n = norm_loc(l).lower()
        if n in STATE_BY_NAME:
            out.add(n)
        elif n in CITY_TO_STATE:
            out.add(CITY_TO_STATE[n].lower())
        else:
            return None
    return out


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

        if "email-protection#" in low:
            d = decode_cf(href.split("#", 1)[1])
            if d:
                emails.add(d.lower())
            continue

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

    for tag in soup.find_all(attrs={"data-cfemail": True}):
        d = decode_cf(tag.get("data-cfemail", ""))
        if d:
            emails.add(d.lower())

    text = soup.get_text(" ")[:200000]
    text = re.sub(r"\s*[\[\(]\s*at\s*[\]\)]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", text, flags=re.I)
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
    each = bool(re.search(r"\beach\b|\bper\b|\bevery\b", desc, re.I))
    m = re.search(r"\b(\d[\d,]*)\s*\+?\s*(?:companies|company|leads|records|results|businesses|contacts|websites)\b", desc, re.I)
    if not m:
        m = re.search(r"\b(?:companies|company|leads|businesses)\s*[:\-]?\s*(\d[\d,]*)\b", desc, re.I)
    if not m:
        m = re.search(r"\b(\d[\d,]*)\s+(?:each|per)\b", desc, re.I)
    if not m:
        m = re.search(r"\b(?:need|want|get|give|find)\s+(?:me\s+)?(\d[\d,]*)\b", desc, re.I)
    if m:
        try:
            return min(int(m.group(1).replace(",", "")), 10000), each
        except Exception:
            return 0, each
    return 0, each


def groups_from_text(text):
    low = text.lower()
    groups = []
    for pat, tags, kws, phrase in TAG_MAP:
        if re.search(pat, low):
            groups.append({"label": kws[0], "tags": list(tags), "kws": list(kws), "q": phrase})
    labels = [g["label"] for g in groups]
    if "dental" in labels:
        groups = [g for g in groups if g["label"] != "clinic"]
    return groups


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
        '"count": 0, "per_niche": false, "emp_min": 0, "emp_max": 0, "rev_min_m": 0, "rev_max_m": 0}. '
        "count = how many companies the client wants (0 if not stated). "
        "per_niche = true if the count applies to EACH niche separately (for example '100 each'). "
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
def geocode_info(q):
    try:
        r = requests.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1},
                         headers=OSM_HEADERS, timeout=15)
        data = r.json()
        if not data:
            return None
        d = data[0]
        bb = d.get("boundingbox")
        bbox = tuple(float(x) for x in bb) if bb else None
        aid = None
        if d.get("osm_type") == "relation":
            aid = 3600000000 + int(d["osm_id"])
        elif d.get("osm_type") == "way":
            aid = 2400000000 + int(d["osm_id"])
        return {"id": aid, "bbox": bbox}
    except Exception:
        return None


def area_info(loc, country):
    name = norm_loc(loc)
    code = STATE_BY_NAME.get(name.lower())
    if code:
        info = geocode_info(f"{name} State, United States")
        time.sleep(1)
        return f'area["ISO3166-2"="US-{code}"]->.a;', (info or {}).get("bbox")
    q = name
    if country.strip().lower() in US_NAMES and "usa" not in name.lower():
        q = f"{name}, USA"
    info = geocode_info(q)
    time.sleep(1)
    if info and info.get("id"):
        return f"area(id:{info['id']})->.a;", info.get("bbox")
    return None, None


def make_tiles(bbox, step=1.5):
    if not bbox:
        return [None]
    south, north, west, east = bbox
    if (north - south) <= step * 1.3 and (east - west) <= step * 1.3:
        return [None]
    while math.ceil((north - south) / step) * math.ceil((east - west) / step) > 60:
        step *= 1.4
    tiles = []
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            tiles.append((round(lat, 4), round(lon, 4), round(min(lat + step, north), 4), round(min(lon + step, east), 4)))
            lon += step
        lat += step
    return tiles


def osm_fetch(clause, tag_list, need_website=True, tile=None):
    bb = f"({tile[0]},{tile[1]},{tile[2]},{tile[3]})" if tile else ""
    parts = []
    for kv in tag_list:
        k, v = kv.split("=", 1)
        if need_website:
            for wk in ("website", "contact:website"):
                parts.append(f'nwr["{k}"="{v}"]["{wk}"](area.a){bb};')
        else:
            parts.append(f'nwr["{k}"="{v}"]["name"](area.a){bb};')
    q = "[out:json][timeout:90];" + clause + "(" + "".join(parts) + ");out tags;"
    last = "koi jawab nahi"
    for ep in OVERPASS:
        try:
            r = requests.post(ep, data={"data": q}, headers=OSM_HEADERS, timeout=120)
            if r.status_code == 200:
                return r.json().get("elements", []), ""
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(2)
    return None, last


def osm_category(tags):
    for k in ("amenity", "office", "shop", "craft", "healthcare", "tourism", "leisure"):
        if tags.get(k):
            return tags[k]
    return ""


def discover_osm(tag_list, locations, country, goal, status, budget, need_website=True, known=None, label=""):
    known = known or set()
    found, notes = {}, []
    start = time.time()
    if locations:
        regions = list(dict.fromkeys(norm_loc(l) for l in locations))
    elif country.strip().lower() in US_NAMES:
        regions = [n for n, _ in STATE_LIST]
    elif country.strip():
        regions = [country]
    else:
        return found, ["Country/location nahi mili, is liye OpenStreetMap skip hui"]
    kind = "website wali" if need_website else "bina website wali"
    for loc in regions:
        if time.time() - start > budget:
            notes.append("OpenStreetMap ka time limit khatam hua")
            break
        clause, bbox = area_info(loc, country)
        if not clause:
            notes.append(f"{loc}: OpenStreetMap par area nahi mili")
            continue
        tiles = make_tiles(bbox)
        failed, last_err = 0, ""
        for ti, tile in enumerate(tiles):
            if time.time() - start > budget:
                notes.append("OpenStreetMap ka time limit khatam hua")
                break
            els, err = osm_fetch(clause, tag_list, need_website, tile)
            if els is None:
                failed += 1
                last_err = err
                continue
            for el in els:
                tags = el.get("tags", {})
                site = (tags.get("website") or tags.get("contact:website") or "").split(";")[0].strip()
                if site:
                    host = urlparse(normalize(site)).netloc.lower().replace("www.", "")
                    if not host or "." not in host or any(b in host for b in BLOCK_DOMAINS):
                        continue
                    key = host
                else:
                    if need_website or not tags.get("name"):
                        continue
                    has_contact = (tags.get("phone") or tags.get("contact:phone") or tags.get("email")
                                   or tags.get("contact:email") or tags.get("addr:street"))
                    if not has_contact:
                        continue
                    key = f"osm-{el.get('type', 'x')}-{el.get('id', '0')}"
                if key in known or key in found:
                    continue
                found[key] = {"hint": tags.get("name", ""), "tags": tags, "src": "OpenStreetMap"}
            status.write(f"Step 1: {label} | {loc} hissa {ti + 1}/{len(tiles)} ({kind}) | mili: {len(found) + len(known)}")
            if goal and len(found) + len(known) >= goal:
                break
            time.sleep(1)
        if failed:
            notes.append(f"{loc} ({label}, {kind}): {failed}/{len(tiles)} hisse OpenStreetMap se nahi aaye ({last_err})")
        if goal and len(found) + len(known) >= goal:
            break
        time.sleep(1)
    return found, notes


# ---------- discovery: web search (support) ----------
def build_queries(niches, locations, country):
    niches = [n for n in niches if n] or [""]
    locs = [norm_loc(l) for l in locations if l] or ([country] if country else [""])
    variants = ["{n} in {l}", "best {n} in {l}", "{n} {l} contact"]
    queries = []
    for v in variants:
        for l in locs:
            for n in niches:
                queries.append(" ".join(v.format(n=n, l=l).split()))
    return list(dict.fromkeys(queries))


def discover_web(niches, locations, country, kws, goal, status, budget, known, label=""):
    found = {}
    if DDGS is None:
        return found
    queries = build_queries(niches, locations, country)
    start = time.time()
    done = 0
    with SEARCH_LOCK:
        for q in queries:
            if time.time() - start > budget:
                break
            try:
                results = DDGS().text(q, region="us-en", max_results=30)
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
            status.write(f"Step 1b: {label} web search {done}/{len(queries)} | extra websites: {len(found)}")
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
    key = item["url"]
    tags = item.get("tags") or {}
    osm = osm_contact(tags) if tags else None
    if str(key).startswith("osm-"):
        base, domain, html = "", "", None
    else:
        base = normalize(key)
        domain = urlparse(base).netloc.replace("www.", "")
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

    want_states = item.get("states")
    if want_states and item.get("src") == "Web search" and addr["state"]:
        if addr["state"].lower() not in want_states:
            return None

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

    if html:
        wstatus = "OK"
    elif not domain:
        wstatus = "No website listed"
    else:
        wstatus = "Not reachable"

    row = {
        "Company": company,
        "Niche": item.get("group", ""),
        "Website": domain,
        "Website status": wstatus,
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
        key = site_key(it["url"])
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
                status.write(f"Step 2: data nikal raha hun: {done}/{len(unique)} | kaam ke rows: {len(res['rows'])}")
    return res


# ---------- results ----------
COLS = ["Company", "Niche", "Website", "Website status", "Category", "Street", "City", "State", "ZIP", "Phone",
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
        df["_k"] = df["Website"].where(df["Website"] != "", df["Company"] + "|" + df["Street"])
        df = df.drop_duplicates(subset=["_k"]).drop(columns=["_k"])
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
        with st.expander(f"{len(res['failed'])} websites skip hui (khuli nahi, niche ya location se match nahi)"):
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
    return {"url": url, "hint": hint, "tags": None, "src": "Manual", "kws": None, "group": ""}


# ---------- UI ----------
tab_search, tab_bulk, tab_single = st.tabs(["Client description search", "Bulk websites", "Single website"])

with tab_search:
    st.write("Client ki need likho: niche, country/state/city, kitni companies chahiye (jaise `100 each`), aur chaho to employees/revenue. Baqi sab tool khud karega aur ek CSV dega.")
    desc = st.text_area("Client description", height=130,
                        placeholder="Target: NY, USA. Industry: real estate and dental. Companies 100 each")

    niches, locations, country = [], [], ""
    target, each = 0, False
    groups = []
    per_group = 0
    d_emp_min = d_emp_max = 0
    d_rev_min = d_rev_max = 0.0

    if desc.strip():
        d_emp_min, d_emp_max, d_rev_min, d_rev_max = detect_filters(desc)
        q_clean = clean_description(desc)
        niches = [q_clean]
        target, each = detect_count(desc)
        groups = groups_from_text(desc)

        locations = [n for n, _ in STATE_LIST if re.search(r"\b" + re.escape(n) + r"\b", desc, re.I)]
        for m in re.finditer(r"\b([A-Z]{2})\b", desc):
            c = m.group(1)
            if c in CODE2NAME and c not in AMBIGUOUS_CODES and CODE2NAME[c] not in locations:
                locations.append(CODE2NAME[c])
        if re.search(r"\bnyc\b|new york city", desc, re.I):
            locations = [l for l in locations if l != "New York"] + ["New York City"]
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
                if ai_locs:
                    locations = list(dict.fromkeys([norm_loc(x) for x in ai_locs] + locations))
                country = str(ai.get("country", "") or "") or country
                if int(ai.get("count", 0) or 0):
                    target = int(ai.get("count", 0))
                each = each or bool(ai.get("per_niche"))
                known_tags = {t for g in groups for t in g["tags"]}
                extra = [str(t) for t in ai.get("osm_tags", [])
                         if re.fullmatch(r"[a-z_:]+=[a-z_]+", str(t)) and str(t) not in known_tags]
                if extra:
                    groups.append({"label": "other", "tags": extra, "kws": [], "q": niches[0]})
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

        general_kws = []
        for n in niches:
            for w in re.findall(r"[a-z]{5,}", n.lower()):
                if w not in GENERIC_WORDS:
                    general_kws.append(w)
        general_kws = list(dict.fromkeys(general_kws))
        if not groups and niches:
            groups = [{"label": "", "tags": [], "kws": general_kws, "q": niches[0]}]
        for g in groups:
            if not g["kws"]:
                g["kws"] = general_kws

        if not re.search(r"\d", desc):
            d_emp_min = d_emp_max = 0
            d_rev_min = d_rev_max = 0.0
        target = min(target, 10000)
        if target:
            per_group = target if (each or len(groups) <= 1) else math.ceil(target / len(groups))

        st.caption(
            f"Locations: {', '.join(locations) or country or 'koi nahi'} | "
            f"Niches: {', '.join(g['label'] or g['q'] for g in groups) or '-'} | "
            f"Har niche ka target: {per_group or 'jitni mil jayen sab'} | "
            f"Employees: {d_emp_min or '-'} se {d_emp_max or 'upar'} | Revenue ($M): {d_rev_min or '-'} se {d_rev_max or 'upar'}"
        )

    if st.button("Dhoondo", key="search_btn") and desc.strip():
        if not groups:
            st.error("Niche samajh nahi aaya. Description mein business type saaf likho (jaise dental, real estate).")
            st.stop()
        if not any(g["tags"] for g in groups) and DDGS is None:
            st.error("Search library (ddgs) install nahi hui. requirements.txt mein `ddgs` likha hona chahiye, phir app Reboot karo.")
            st.stop()

        filters_on = bool(d_emp_min or d_emp_max or d_rev_min or d_rev_max)
        status = st.empty()
        budget = 1200 if per_group > 300 else 600
        want_states = wanted_states(locations)

        all_found, notes, stat_lines = {}, [], []
        for g in groups:
            label = g["label"] or g["q"]
            goal_g = math.ceil(per_group * 1.25) if per_group else 0
            f_all, n_site, n_nosite, n_web = {}, 0, 0, 0
            if g["tags"]:
                f_site, nts = discover_osm(g["tags"], locations, country, goal_g, status, budget, True, None, label)
                notes += nts
                f_all.update(f_site)
                n_site = len(f_site)
                if (goal_g and len(f_all) < goal_g) or (not goal_g and len(f_all) < 30):
                    f_ns, nts2 = discover_osm(g["tags"], locations, country, goal_g, status, budget, False,
                                              set(f_all) | set(all_found), label)
                    notes += nts2
                    f_all.update(f_ns)
                    n_nosite = len(f_ns)
            if (not g["tags"]) or (goal_g and len(f_all) < goal_g) or len(f_all) < 30:
                f_web = discover_web([g["q"]], locations, country, g["kws"], goal_g, status, 120,
                                     set(f_all) | set(all_found), label)
                f_all.update(f_web)
                n_web = len(f_web)
            for k, v in f_all.items():
                v["group"] = g["label"] or g["q"]
                if v["src"] == "Web search":
                    v["states"] = want_states
                all_found.setdefault(k, v)
            stat_lines.append(f"{label}: {len(f_all)} mili (website wali {n_site}, bina website {n_nosite}, web search {n_web})")

        if not all_found:
            st.error("Koi company nahi mili. " + " | ".join(notes) + " Niche ya location saaf likh kar dobara try karo, ya 2-3 minute baad dobara.")
            st.stop()

        items = [dict(url=k, **v) for k, v in all_found.items()][:HARD_CAP]

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
            if per_group:
                kept_rows, cnt = [], {}
                for r in res["rows"]:
                    k = r.get("Niche", "")
                    if cnt.get(k, 0) < per_group:
                        kept_rows.append(r)
                        cnt[k] = cnt.get(k, 0) + 1
                res["rows"] = kept_rows
            res["notes"] = notes
            final_counts = {}
            for r in res["rows"]:
                final_counts[r.get("Niche", "")] = final_counts.get(r.get("Niche", ""), 0) + 1
            res["stats"] = ("Search: " + " | ".join(stat_lines) + " || Final CSV: "
                            + ", ".join(f"{k or 'sab'}: {v}" for k, v in final_counts.items()))
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
