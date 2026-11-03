import hashlib
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

try:
    import duckdb
except Exception:
    duckdb = None

st.set_page_config(page_title="Decision-Maker Finder", layout="wide")
st.title("Decision-Maker & Contact Finder")
st.caption("Built by Faiz Khan")

WORKERS = 16
HARD_CAP = 3000
DEFAULT_CAP = 300
ENRICH_CAP = 80
ENRICH_SECONDS = 240
SEARCH_LOCK = threading.Lock()
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OV_PATH = "s3://overturemaps-us-west-2/release/{rel}/theme=places/type=place/*"
OV_STATE = {"rel": None, "cols": None, "err": ""}

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
COUNTRY_CODES = {
    "usa": "US", "us": "US", "united states": "US", "america": "US", "uk": "GB", "united kingdom": "GB",
    "england": "GB", "canada": "CA", "australia": "AU", "germany": "DE", "france": "FR", "pakistan": "PK",
    "india": "IN", "uae": "AE", "united arab emirates": "AE", "dubai": "AE", "saudi arabia": "SA",
    "spain": "ES", "italy": "IT", "netherlands": "NL", "brazil": "BR", "mexico": "MX", "turkey": "TR",
    "singapore": "SG", "ireland": "IE", "new zealand": "NZ", "south africa": "ZA", "egypt": "EG",
    "bangladesh": "BD", "philippines": "PH", "indonesia": "ID", "malaysia": "MY", "nigeria": "NG",
    "kenya": "KE", "poland": "PL", "sweden": "SE", "norway": "NO", "denmark": "DK", "switzerland": "CH",
    "belgium": "BE", "austria": "AT", "japan": "JP", "south korea": "KR", "china": "CN",
}

# regex -> (OpenStreetMap tags, relevance keywords, search phrase, Overture category parts)
TAG_MAP = [
    (r"dent(al|ist)|orthodont", ["amenity=dentist"], ["dental", "dentist", "orthodont"], "dentist",
     ["dentist", "dental", "orthodont"]),
    (r"real estate|realtor|realty|estate agen|property agen", ["office=estate_agent"],
     ["estate", "realtor", "realty", "property", "properties", "homes", "broker"], "real estate agency",
     ["real_estate", "realtor", "property_management"]),
    (r"lawyer|attorney|law firm", ["office=lawyer"], ["law", "attorney", "lawyer"], "law firm",
     ["lawyer", "attorney", "law_firm"]),
    (r"accountant|\bcpa\b|accounting", ["office=accountant"], ["account", "cpa", "tax"], "accountant",
     ["accountant", "accounting", "tax_"]),
    (r"insurance", ["office=insurance"], ["insurance"], "insurance agency", ["insurance"]),
    (r"doctor|physician", ["amenity=doctors"], ["doctor", "physician", "medical"], "doctor",
     ["doctor", "physician"]),
    (r"\bclinic", ["amenity=clinic"], ["clinic", "medical", "health"], "clinic", ["clinic"]),
    (r"veterinar|\bvets?\b", ["amenity=veterinary"], ["veterinar", "animal", "pet"], "veterinarian",
     ["veterinar"]),
    (r"pharmac", ["amenity=pharmacy"], ["pharmacy"], "pharmacy", ["pharmac"]),
    (r"\bgyms?\b|fitness", ["leisure=fitness_centre"], ["gym", "fitness"], "gym", ["gym", "fitness"]),
    (r"restaurant", ["amenity=restaurant"], ["restaurant", "menu"], "restaurant", ["restaurant"]),
    (r"hotel", ["tourism=hotel"], ["hotel", "rooms"], "hotel", ["hotel"]),
    (r"salon|\bhair", ["shop=hairdresser"], ["salon", "hair"], "hair salon", ["hair_salon", "salon"]),
    (r"med ?spa|aesthetic|beauty|\bspa\b", ["shop=beauty"], ["spa", "aesthetic", "beauty", "skin"], "med spa",
     ["med_spa", "medical_spa", "day_spa", "skin_care", "beauty"]),
    (r"auto repair|mechanic", ["shop=car_repair"], ["auto", "repair", "mechanic"], "auto repair",
     ["automotive_repair", "auto_repair"]),
    (r"car dealer", ["shop=car"], ["dealer", "car"], "car dealer", ["car_dealer"]),
    (r"plumb", ["craft=plumber"], ["plumb"], "plumber", ["plumb"]),
    (r"electrician|electrical", ["craft=electrician"], ["electric"], "electrician", ["electrician"]),
    (r"hvac|heating|air conditioning", ["craft=hvac"], ["hvac", "heating", "cooling"], "hvac contractor",
     ["hvac", "heating_and_air"]),
    (r"roof", ["craft=roofer"], ["roof"], "roofing contractor", ["roof"]),
    (r"construction|contractor|builder", ["office=construction_company", "craft=builder"],
     ["construction", "contractor", "builder"], "construction company",
     ["construction", "contractor", "builder"]),
    (r"architect", ["office=architect"], ["architect"], "architect", ["architect"]),
    (r"marketing|advertising", ["office=advertising_agency"], ["marketing", "advertising"],
     "marketing agency", ["marketing", "advertising"]),
    (r"software|it company|it services", ["office=it"], ["software", "technology"], "software company",
     ["software", "it_service"]),
    (r"\bbanks?\b", ["amenity=bank"], ["bank"], "bank", ["bank"]),
    (r"daycare|child care", ["amenity=kindergarten"], ["daycare", "child care"], "daycare",
     ["day_care", "daycare", "child_care"]),
]
GENERIC_WORDS = {"company", "companies", "services", "service", "near", "best", "local", "agency", "agent",
                 "agents", "clinic", "office", "group", "business", "businesses", "target", "industry",
                 "countries", "country", "need", "want", "both", "others", "other", "each"}

UNIT = r"(?:billion|million|thousand|bn|mm|b|m|k)"
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*\.?\d*)\s*(billion|million|thousand|bn|mm|b|m|k)?\b", re.I)
MONEY_UNIT_RE = re.compile(r"\$?\s?(\d[\d,]*\.?\d*)\s*(billion|million|thousand|bn|mm|k|m|b)\b", re.I)
EMP_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:-|–|to)\s*(\d[\d,]*)\+?\s*(?:employees|staff|team members)", re.I)
EMP_ONE_RE = re.compile(r"(\d[\d,]*)\+?\s*(?:employees|staff)", re.I)
REV_WORD_RE = r"rev[ei]n[ue]+\b|sales|turnover"


# ---------- basic helpers ----------
def normalize(url):
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


def domain_of(url):
    try:
        host = urlparse(normalize(url)).netloc.lower().replace("www.", "")
    except Exception:
        return ""
    if not host or "." not in host or " " in host or any(b in host for b in BLOCK_DOMAINS):
        return ""
    return host


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
    if s.upper().startswith("US-"):
        s = s[3:]
    if len(s) == 2:
        return CODE2NAME.get(s.upper(), s)
    return s


def zip5(z):
    m = re.search(r"\d{5}", z or "")
    return m.group(0) if m else str(z or "").strip()


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
    out = {"street": "", "city": "", "state": "", "zip": "", "phone": ""}

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


# ---------- size / revenue (sirf tab jab maango) ----------
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
    for m in re.finditer(REV_WORD_RE, text, re.I):
        window = text[max(0, m.start() - 60): m.end() + 140]
        found = []
        for mm in MONEY_UNIT_RE.finditer(window):
            val = to_usd_m(mm.group(1), mm.group(2))
            if val is not None:
                found.append((val, mm.start(), mm.end()))
        for mm in MONEY_RE.finditer(window):
            if any(f[1] <= mm.start() < f[2] for f in found):
                continue
            val = to_usd_m(mm.group(1), mm.group(2))
            if val is not None:
                found.append((val, mm.start(), mm.end()))
        if not found:
            continue
        found.sort(key=lambda x: x[1])
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


def empty_info():
    return {"emp": None, "rev": None, "emp_src": "", "rev_src": "", "link": "", "confidence": ""}


def enrich(company, city="", state=""):
    info = empty_info()
    if DDGS is None or not company:
        return info
    words = [w for w in company.lower().split() if w not in ("the", "a", "and")]
    token = words[0] if words else company.lower()
    emps, revs = [], []
    q = " ".join(x for x in [company, city, state, "revenue employees"] if x)
    try:
        results = DDGS().text(q, region="us-en", max_results=10)
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
    if emps:
        info["emp"], info["emp_src"], info["link"] = emps[0][0], emps[0][1], emps[0][2]
    if revs:
        info["rev"], info["rev_src"] = revs[0][0], revs[0][1]
        if not info["link"]:
            info["link"] = revs[0][2]
    sources = {x[1] for x in emps + revs}
    info["confidence"] = "Medium" if len(sources) >= 2 else ("Low" if sources else "")
    return info


def enrich_many(rows, status, budget):
    start = time.time()
    infos = {}

    def work(r):
        if time.time() - start > budget:
            return r["_key"], None
        time.sleep(0.3)
        return r["_key"], enrich(r["Company"], r.get("City", ""), r.get("State", ""))

    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(work, r) for r in rows]
        for f in as_completed(futures):
            try:
                k, info = f.result()
            except Exception:
                k, info = None, None
            if k is not None and info is not None:
                infos[k] = info
            done += 1
            status.write(f"Step 3: size/revenue andaza {done}/{len(rows)} (hadd {budget} second)")
    return infos


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


def out_of_range(info, emp_min, emp_max, rev_min, rev_max):
    emp, rev = info["emp"], info["rev"]
    if (emp_min or emp_max) and emp and not overlaps(emp[0], emp[1], emp_min, emp_max):
        return True
    if (rev_min or rev_max) and rev and not overlaps(rev[0], rev[1], rev_min, rev_max):
        return True
    return False


# ---------- description understanding ----------
def clean_description(desc):
    q = EMP_RANGE_RE.sub(" ", desc)
    q = EMP_ONE_RE.sub(" ", q)
    q = re.sub(r"(rev[ei]n[ue]+|sales)[^,.;\n]*", " ", q, flags=re.I)
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


def detect_wants(desc):
    low = desc.lower()
    return {
        "revenue": bool(re.search(REV_WORD_RE, low)),
        "employees": bool(re.search(r"employee|staff size|company size|team size|headcount", low)),
        "email": bool(re.search(r"\be-?mails?\b", low)),
        "phone": bool(re.search(r"\bphones?\b|contact numbers?|mobile", low)),
        "people": bool(re.search(r"decision.?makers?|\bpeople\b|\bowners?\b|founders?|\bceo\b|executives?|contact persons?|job titles?", low)),
    }


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


def ov_fallback(phrase):
    words = [w for w in re.findall(r"[a-z]{4,}", phrase.lower()) if w not in GENERIC_WORDS]
    pats = []
    if len(words) > 1:
        pats.append("_".join(words))
    pats += words
    return list(dict.fromkeys(pats))


def groups_from_text(text):
    low = text.lower()
    groups = []
    for pat, tags, kws, phrase, ov in TAG_MAP:
        if re.search(pat, low):
            groups.append({"label": kws[0], "tags": list(tags), "kws": list(kws), "q": phrase, "ov": list(ov)})
    labels = [g["label"] for g in groups]
    if "dental" in labels:
        groups = [g for g in groups if g["label"] != "clinic"]
    return groups


def get_secret(name, default=""):
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return default


def ai_parse(desc, api_key, models):
    cache = st.session_state.setdefault("ai_cache", {})
    ck = desc.strip()
    if ck in cache:
        return cache[ck], ""
    fail = st.session_state.get("ai_fail")
    if fail and time.time() - fail[0] < 60:
        return None, fail[1] + " (60 second baad dobara koshish hogi)"

    prompt = (
        "Read this client's lead-generation request and reply with ONLY JSON in this shape: "
        '{"niches": ["up to 6 short business-type phrases, e.g. real estate agency, dental clinic"], '
        '"locations": ["cities/states/regions mentioned; empty list if only a whole country"], '
        '"country": "country name or empty", "country_code": "ISO 3166-1 alpha-2 code or empty", '
        '"osm_tags": ["up to 6 OpenStreetMap tags as key=value for these business types; empty list if unsure"], '
        '"count": 0, "per_niche": false, '
        '"want": ["only those explicitly asked, from: revenue, employees, email, phone, people"], '
        '"emp_min": 0, "emp_max": 0, "rev_min_m": 0, "rev_max_m": 0}. '
        "count = how many companies the client wants (0 if not stated). "
        "per_niche = true if the count applies to EACH niche separately (for example '100 each'). "
        "Never invent employee or revenue numbers: use 0 unless the client explicitly states them. "
        "Revenue is in millions of USD.\n\nRequest: " + desc
    )
    errors = []
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
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
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(text)
                    cache[ck] = data
                    st.session_state.pop("ai_fail", None)
                    return data, ""
                errors.append(f"{model}: HTTP {r.status_code}")
                if r.status_code in (429, 500, 503) and attempt == 0:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                errors.append(f"{model}: {str(e)[:80]}")
                break
    msg = " | ".join(errors[-4:])
    st.session_state["ai_fail"] = (time.time(), msg)
    return None, msg


# ---------- geography ----------
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


_AREA = {}


def area_info(loc, country):
    ck = (loc, country)
    if ck in _AREA:
        return _AREA[ck]
    name = norm_loc(loc)
    code = STATE_BY_NAME.get(name.lower())
    if code:
        info = geocode_info(f"{name} State, United States")
        time.sleep(1)
        out = (f'area["ISO3166-2"="US-{code}"]->.a;', (info or {}).get("bbox"), code)
    else:
        q = name
        if country.strip().lower() in US_NAMES and "usa" not in name.lower():
            q = f"{name}, USA"
        info = geocode_info(q)
        time.sleep(1)
        clause = f"area(id:{info['id']})->.a;" if info and info.get("id") else None
        out = (clause, (info or {}).get("bbox"), None)
    _AREA[ck] = out
    return out


def make_tiles(bbox, step=1.5):
    if not bbox:
        return [None]
    south, north, west, east = bbox
    if (north - south) <= step * 1.3 and (east - west) <= step * 1.3:
        return [(south, west, north, east)]
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


def get_regions(locations, country):
    if locations:
        return list(dict.fromkeys(norm_loc(l) for l in locations))
    if country.strip().lower() in US_NAMES:
        return [n for n, _ in STATE_LIST]
    if country.strip():
        return [country]
    return []


def mkrec(key, name, domain, src):
    return {"key": key, "name": name or "", "domain": domain, "street": "", "city": "", "state": "",
            "zip": "", "country": "", "phones": [], "emails": [], "socials": {}, "category": "",
            "src": src, "group": "", "kws": None, "states": None}


# ---------- source 1: Overture Maps ----------
def ov_connect():
    con = duckdb.connect()
    for stmt in ("SET memory_limit='500MB'", "SET threads=4", "SET preserve_insertion_order=false",
                 "INSTALL httpfs", "LOAD httpfs", "SET s3_region='us-west-2'"):
        try:
            con.execute(stmt)
        except Exception:
            pass
    return con


def ov_setup():
    if OV_STATE["rel"]:
        return True
    if duckdb is None:
        OV_STATE["err"] = "duckdb install nahi hui (requirements.txt mein duckdb likho)"
        return False
    cands = []
    sec = get_secret("OVERTURE_RELEASE")
    if sec:
        cands.append(sec)
    try:
        j = requests.get("https://stac.overturemaps.org/catalog.json", timeout=10).json()
        if j.get("latest"):
            cands.append(str(j["latest"]))
    except Exception:
        pass
    cands += ["2026-09-23.1", "2026-08-19.0", "2026-04-15.0"]
    last = ""
    for rel in dict.fromkeys(cands):
        try:
            con = ov_connect()
            rows = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{OV_PATH.format(rel=rel)}') LIMIT 0").fetchall()
            con.close()
            cols = {r[0] for r in rows}
            if "bbox" in cols and "names" in cols:
                OV_STATE["rel"], OV_STATE["cols"], OV_STATE["err"] = rel, cols, ""
                return True
            last = f"{rel}: schema alag hai"
        except Exception as e:
            last = f"{rel}: {str(e)[:120]}"
    OV_STATE["err"] = last or "Overture khuli nahi"
    return False


def ov_fetch(tile, patterns, limit=30000):
    cols = OV_STATE["cols"]
    cat = next((f"{c}.primary" for c in ("categories", "taxonomy") if c in cols), None)
    if not cat:
        return None, "category column nahi mila"
    pats = [re.sub(r"[^a-z_]", "", p) for p in patterns if p]
    pats = [p for p in pats if p]
    if not pats:
        return [], ""
    sel = ["names.primary AS name", f"{cat} AS cat"]
    sel.append("confidence AS conf" if "confidence" in cols else "NULL AS conf")
    for c, alias in (("websites", "web"), ("phones", "phone"), ("emails", "email")):
        sel.append(f"{c}[1] AS {alias}" if c in cols else f"NULL AS {alias}")
    sel.append("socials AS socials" if "socials" in cols else "NULL AS socials")
    if "addresses" in cols:
        sel += ["addresses[1].freeform AS street", "addresses[1].locality AS city",
                "addresses[1].region AS region", "addresses[1].postcode AS zip",
                "addresses[1].country AS country"]
    else:
        sel += ["NULL AS street", "NULL AS city", "NULL AS region", "NULL AS zip", "NULL AS country"]
    south, west, north, east = tile
    cond = " OR ".join(f"{cat} LIKE '%{p}%'" for p in pats)
    where = (f"bbox.xmin >= {west} AND bbox.xmax <= {east} AND bbox.ymin >= {south} "
             f"AND bbox.ymax <= {north} AND ({cond})")
    if "confidence" in cols:
        where += " AND confidence >= 0.5"
    sql = (f"SELECT {', '.join(sel)} FROM read_parquet('{OV_PATH.format(rel=OV_STATE['rel'])}') "
           f"WHERE {where} LIMIT {limit}")
    try:
        con = ov_connect()
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        con.close()
        return rows, ""
    except Exception as e:
        return None, str(e)[:150]


def discover_overture(patterns, locations, country, cc, goal, status, budget, known, label):
    found, notes = {}, []
    if not ov_setup():
        return found, ["Overture nahi khuli: " + OV_STATE["err"] + " (OpenStreetMap se kaam chalega)"]
    regions = get_regions(locations, country)
    if not regions:
        return found, ["Country/location nahi mili, is liye Overture skip hui"]
    start = time.time()
    for loc in regions:
        if time.time() - start > budget:
            notes.append("Overture ka time limit khatam hua")
            break
        _, bbox, scode = area_info(loc, country)
        if not bbox:
            notes.append(f"{loc}: Overture ke liye area ka naqsha nahi mila")
            continue
        tiles = make_tiles(bbox, 6.0)
        failed, last_err = 0, ""
        for ti, tile in enumerate(tiles):
            if time.time() - start > budget:
                notes.append("Overture ka time limit khatam hua")
                break
            if tile is None:
                continue
            status.write(f"Step 1: {label} | Overture | {loc} hissa {ti + 1}/{len(tiles)} check ho raha hai... | ab tak mili: {len(found) + len(known)}")
            rows, err = ov_fetch(tile, patterns)
            if rows is None:
                failed += 1
                last_err = err
                continue
            for r in rows:
                name = (r.get("name") or "").strip()
                if not name:
                    continue
                ctry = (r.get("country") or "").upper()
                if cc and ctry and ctry != cc:
                    continue
                region = str(r.get("region") or "").upper().replace("US-", "")
                if scode and region and region != scode:
                    continue
                domain = domain_of(r.get("web") or "") if r.get("web") else ""
                street = (r.get("street") or "").strip()
                if domain:
                    key = domain
                else:
                    key = "osm-" + hashlib.md5((name.lower() + street.lower()).encode()).hexdigest()[:10]
                if key in known or key in found:
                    continue
                rec = mkrec(key, name, domain, "Overture")
                rec.update(street=street, city=(r.get("city") or "").strip(),
                           state=norm_state(str(r.get("region") or "")), zip=str(r.get("zip") or "").strip(),
                           country=ctry, category=str(r.get("cat") or ""))
                if r.get("phone"):
                    rec["phones"].append(str(r["phone"]))
                if r.get("email"):
                    rec["emails"].append(str(r["email"]).lower())
                for u in (r.get("socials") or []):
                    plat = valid_social(str(u))
                    if plat and plat not in rec["socials"]:
                        rec["socials"][plat] = str(u).rstrip("/")
                if not domain and not (rec["phones"] or rec["emails"] or street):
                    continue
                found[key] = rec
                if goal and len(found) + len(known) >= goal:
                    break
            if goal and len(found) + len(known) >= goal:
                break
        if failed:
            notes.append(f"{loc} ({label}): Overture ke {failed}/{len(tiles)} hisse nahi aaye ({last_err})")
        if goal and len(found) + len(known) >= goal:
            break
    return found, notes


# ---------- source 2: OpenStreetMap ----------
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


def osm_rec(key, domain, tags):
    rec = mkrec(key, tags.get("name", ""), domain, "OpenStreetMap")
    rec["street"] = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x)
    rec["city"] = tags.get("addr:city", "")
    rec["state"] = norm_state(tags.get("addr:state", ""))
    rec["zip"] = tags.get("addr:postcode", "")
    rec["country"] = (tags.get("addr:country") or "").upper()
    rec["category"] = osm_category(tags)
    for k in ("phone", "contact:phone"):
        for part in (tags.get(k) or "").split(";"):
            if part.strip():
                rec["phones"].append(part.strip())
    for k in ("email", "contact:email"):
        for part in (tags.get(k) or "").split(";"):
            if part.strip():
                rec["emails"].append(part.strip().lower())
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
            url = v if v.startswith("http") else (prefix + v.lstrip("@/") if prefix else "")
            if url and valid_social(url) == plat:
                rec["socials"][plat] = url.rstrip("/")
                break
    return rec


def discover_osm(tag_list, locations, country, goal, status, budget, need_website, known, label):
    found, notes = {}, []
    start = time.time()
    regions = get_regions(locations, country)
    if not regions:
        return found, ["Country/location nahi mili, is liye OpenStreetMap skip hui"]
    kind = "website wali" if need_website else "bina website wali"
    for loc in regions:
        if time.time() - start > budget:
            notes.append("OpenStreetMap ka time limit khatam hua")
            break
        clause, bbox, _ = area_info(loc, country)
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
                domain = domain_of(site) if site else ""
                if domain:
                    key = domain
                else:
                    if need_website or not tags.get("name"):
                        continue
                    if not (tags.get("phone") or tags.get("contact:phone") or tags.get("email")
                            or tags.get("contact:email") or tags.get("addr:street")):
                        continue
                    key = f"osm-{el.get('type', 'x')}-{el.get('id', '0')}"
                if key in known or key in found:
                    continue
                found[key] = osm_rec(key, domain, tags)
            status.write(f"Step 1: {label} | OpenStreetMap | {loc} hissa {ti + 1}/{len(tiles)} ({kind}) | mili: {len(found) + len(known)}")
            if goal and len(found) + len(known) >= goal:
                break
            time.sleep(1)
        if failed:
            notes.append(f"{loc} ({label}, {kind}): {failed}/{len(tiles)} hisse OpenStreetMap se nahi aaye ({last_err})")
        if goal and len(found) + len(known) >= goal:
            break
        time.sleep(1)
    return found, notes


# ---------- source 3: web search ----------
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


def discover_web(niches, locations, country, kws, goal, status, budget, known, label, states):
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
                host = domain_of(r.get("href", ""))
                if not host or host in known or host in found:
                    continue
                blob = (r.get("title", "") + " " + r.get("body", "") + " " + host).lower()
                if kws and not any(k in blob for k in kws):
                    continue
                rec = mkrec(host, "", host, "Web search")
                rec["kws"] = kws
                rec["states"] = states
                found[host] = rec
            done += 1
            status.write(f"Step 1: {label} | web search {done}/{len(queries)} | extra websites: {len(found)}")
            if goal and len(known) + len(found) >= goal:
                break
            time.sleep(0.4)
    return found


# ---------- building rows ----------
def build_row(rec, deep, emails_on, people_on, use_search):
    domain = rec["domain"]
    base = "https://" + domain if domain else ""
    html = fetch(base) if domain else None
    src = rec["src"]

    if src == "Web search":
        if not html:
            return None
        if rec.get("kws"):
            low = html[:300000].lower()
            if not any(k in low for k in rec["kws"]):
                return None

    prof = {"street": "", "city": "", "state": "", "zip": "", "phone": ""}
    pdata, people = [], []
    if html:
        e, p, s, pages = extract_contacts(html, base)
        pdata.append((e, p, s))
        prof = extract_profile(html)
        if people_on:
            people += extract_people(html, base)
        if deep:
            ordered = sorted(pages, key=lambda u: 0 if "contact" in u.lower() else 1)
            for pg in ordered[:3]:
                h = fetch(pg)
                if h:
                    e2, p2, s2, _ = extract_contacts(h, pg)
                    pdata.append((e2, p2, s2))
                    if people_on:
                        people += extract_people(h, pg)
                    p3 = extract_profile(h)
                    for k in prof:
                        prof[k] = prof[k] or p3[k]

    addr = {k: rec.get(k, "") or prof[k] for k in ("street", "city", "state", "zip")}
    addr["state"] = norm_state(addr["state"])
    addr["zip"] = zip5(addr["zip"])

    want_states = rec.get("states")
    if want_states and src == "Web search" and addr["state"]:
        if addr["state"].lower() not in want_states:
            return None

    if not domain and not (rec["phones"] or rec["emails"] or addr["street"]):
        return None

    socials = dict(rec["socials"])
    cand_phones = list(rec["phones"])
    if prof["phone"]:
        cand_phones.append(prof["phone"])
    cand_emails = list(rec["emails"])
    for e_, p_, s_ in pdata:
        cand_phones += sorted(p_)
        cand_emails += sorted(e_)
        for x in sorted(s_):
            plat = valid_social(x)
            if plat and plat not in socials:
                socials[plat] = x

    phones, seen_p = [], set()
    for x in cand_phones:
        n = norm_phone(x)
        if n and n not in seen_p:
            seen_p.add(n)
            phones.append(n)

    emails = []
    if emails_on:
        stem = domain.split(".")[0]
        seen_e = set()
        for x in cand_emails:
            x = x.strip().lower()
            if x and x not in seen_e and valid_email(x):
                seen_e.add(x)
                emails.append(x)
        emails.sort(key=lambda m: stem not in m.split("@")[-1])

    company = rec["name"] or (company_from_html(html, domain) if html else domain)

    if people_on and use_search:
        people += search_people(company, domain)
    uniq, seen_n = [], set()
    for x in people:
        k = x["Name"].lower()
        if k not in seen_n:
            seen_n.add(k)
            uniq.append(x)
    people = uniq

    row = {
        "Website": domain,
        "Company": company,
        "Niche": rec.get("group", ""),
        "Street": addr["street"],
        "City": addr["city"],
        "State": addr["state"],
        "ZIP": addr["zip"],
        "Country": rec.get("country", ""),
        "Phone": phones[0] if phones else "",
        "Emails": "; ".join(emails[:8]),
        "People (name - title)": "; ".join(f"{x['Name']} - {x['Title']}" for x in people[:10]),
        "Category": rec.get("category", ""),
        "Source": src,
        "_key": rec["key"],
    }
    for col in SOCIAL_COLS:
        row[col] = socials.get(col, "")

    filled = [row["Street"], row["City"], row["Phone"], row["Emails"], row["People (name - title)"]]
    filled += [row[c] for c in SOCIAL_COLS]
    row["_score"] = sum(1 for v in filled if v)
    base_cols = {"Company": company, "Website": domain}
    return {"row": row, "people": [dict(base_cols, **x) for x in people]}


def safe_build(rec, deep, emails_on, people_on, use_search):
    try:
        return build_row(rec, deep, emails_on, people_on, use_search)
    except Exception:
        return None


def run_batch(recs, deep, emails_on, people_on, use_search, status):
    seen, unique = set(), []
    for r in recs:
        if r["key"] not in seen:
            seen.add(r["key"])
            unique.append(r)
    res = {"rows": [], "people": [], "failed": [], "unknown": []}
    if not unique:
        return res
    bar = st.progress(0.0)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(safe_build, r, deep, emails_on, people_on, use_search): r["key"] for r in unique}
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
                status.write(f"Step 2: data tayyar ho raha hai: {done}/{len(unique)} | kaam ke rows: {len(res['rows'])}")
    return res


def apply_info(row, info):
    row["Employees"] = fmt_emp(info["emp"])
    row["Employees source"] = info["emp_src"]
    row["Revenue (Estimate)"] = fmt_rev(info["rev"])
    row["Revenue source"] = info["rev_src"]
    row["Source link"] = info["link"]
    row["Confidence"] = info["confidence"]


def trim_rows(rows, per_group):
    if not per_group:
        return rows
    out, cnt = [], {}
    for r in rows:
        k = r.get("Niche", "")
        if cnt.get(k, 0) < per_group:
            out.append(r)
            cnt[k] = cnt.get(k, 0) + 1
    return out


# ---------- results ----------
def cols_for(wants, d_filters):
    cols = ["Website", "Company", "Niche", "Street", "City", "State", "ZIP", "Country"] + SOCIAL_COLS + ["Phone"]
    if wants.get("email"):
        cols.append("Emails")
    if wants.get("people"):
        cols.append("People (name - title)")
    if wants.get("employees") or d_filters[0] or d_filters[1]:
        cols += ["Employees", "Employees source"]
    if wants.get("revenue") or d_filters[2] or d_filters[3]:
        cols += ["Revenue (Estimate)", "Revenue source"]
    if any(c in cols for c in ("Employees", "Revenue (Estimate)")):
        cols += ["Source link", "Confidence"]
    return cols + ["Category", "Source"]


ALL_COLS = ["Website", "Company", "Niche", "Street", "City", "State", "ZIP", "Country"] + SOCIAL_COLS + [
    "Phone", "Emails", "People (name - title)", "Category", "Source"]


def rows_to_df(rows, cols):
    df = pd.DataFrame(rows)
    if "_score" in df.columns:
        df = df.sort_values("_score", ascending=False).drop(columns=["_score"])
    df["_k"] = df["Website"].where(df["Website"] != "", df["Company"] + "|" + df["Street"])
    df = df.drop_duplicates(subset=["_k"]).drop(columns=["_k"])
    return df[[c for c in cols if c in df.columns]]


def render_results(res, kp):
    cols = res.get("cols", ALL_COLS)
    if res.get("stats"):
        st.success(res["stats"])
    for n in res.get("notes", []):
        st.caption("Note: " + n)
    rows = res["rows"]
    unknown = res.get("unknown") or []
    if rows:
        df = rows_to_df(rows, cols)
        st.dataframe(df, use_container_width=True)
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"), "leads.csv", key=kp + "_dl_master")
        if res["people"]:
            pdf = pd.DataFrame(res["people"]).drop_duplicates(subset=["Website", "Name"])
            st.download_button("Download People CSV (har banda ek row)",
                               pdf.to_csv(index=False).encode("utf-8-sig"), "people.csv", key=kp + "_dl_people")
    elif unknown:
        st.info("Aap ki range mein data-wali koi company match nahi hui. Neeche wo companies hain jin ka size/revenue data nahi mila.")
    else:
        st.warning("Koi data nahi nikla.")
    if unknown:
        st.subheader(f"Size/revenue ka data nahi mila ({len(unknown)} companies, aap khud check kar lo)")
        udf = rows_to_df(unknown, cols)
        st.dataframe(udf, use_container_width=True)
        st.download_button("Download CSV (data nahi mila wali)", udf.to_csv(index=False).encode("utf-8-sig"),
                           "leads_unknown.csv", key=kp + "_dl_unknown")
    if res["failed"]:
        with st.expander(f"{len(res['failed'])} skip hui (website khuli nahi, niche ya location se match nahi)"):
            st.write(", ".join(res["failed"]))


def show_results():
    res = st.session_state.get("res")
    if res:
        render_results(res, "main")


def read_sites_csv(up):
    df0 = pd.read_csv(up, header=None, dtype=str)
    first = str(df0.iloc[0, 0]).strip() if len(df0) else ""
    if re.match(r"^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+(/.*)?$", first):
        df0.columns = [f"Column {i + 1}" for i in range(df0.shape[1])]
        return df0, False
    up.seek(0)
    return pd.read_csv(up, dtype=str), True


def manual_rec(url, hint=""):
    d = urlparse(normalize(url)).netloc.lower().replace("www.", "")
    return mkrec(d, hint, d, "Manual")


# ---------- main search pipeline ----------
def run_search(cfg):
    t0 = time.time()
    groups, locations, country, cc = cfg["groups"], cfg["locations"], cfg["country"], cfg["cc"]
    per_group, wants, filt = cfg["per_group"], cfg["wants"], cfg["filt"]
    filters_on = any(filt)
    deep = bool(wants["email"] or wants["phone"] or wants["people"])
    emails_on, people_on = bool(wants["email"]), bool(wants["people"])
    status = st.empty()
    budget = 1200 if per_group > 300 else 600
    want_states = wanted_states(locations)

    ov_ok = ov_setup()
    notes = []
    if ov_ok:
        st.caption(f"Data source check: Overture Maps OK (release {OV_STATE['rel']})")
    else:
        st.caption("Data source check: Overture nahi khuli (" + OV_STATE["err"] + "). OpenStreetMap se kaam chalega.")

    all_found, stat_lines = {}, []
    for g in groups:
        label = g["label"] or g["q"]
        goal_g = math.ceil(per_group * 1.25) if per_group else DEFAULT_CAP
        enough = goal_g if per_group else 30
        f_all = {}
        n_ov = n_osm = n_web = 0
        if ov_ok:
            f_ov, nts = discover_overture(g["ov"], locations, country, cc, goal_g, status, budget,
                                          set(all_found), label)
            notes += nts
            f_all.update(f_ov)
            n_ov = len(f_ov)
        short = len(f_all) < enough
        if g["tags"] and short:
            f_site, nts = discover_osm(g["tags"], locations, country, goal_g, status, budget, True,
                                       set(f_all) | set(all_found), label)
            notes += nts
            f_all.update(f_site)
            n_osm += len(f_site)
            short = len(f_all) < enough
            if short:
                f_ns, nts2 = discover_osm(g["tags"], locations, country, goal_g, status, budget, False,
                                          set(f_all) | set(all_found), label)
                notes += nts2
                f_all.update(f_ns)
                n_osm += len(f_ns)
                short = len(f_all) < enough
        if short:
            f_web = discover_web([g["q"]], locations, country, g["kws"], goal_g, status, 120,
                                 set(f_all) | set(all_found), label, want_states)
            f_all.update(f_web)
            n_web = len(f_web)
        for k, v in f_all.items():
            v["group"] = g["label"] or g["q"]
            if not v["country"] and cc:
                v["country"] = cc
            all_found.setdefault(k, v)
        stat_lines.append(f"{label}: {len(f_all)} mili (Overture {n_ov}, OpenStreetMap {n_osm}, web search {n_web})")

    if not all_found:
        st.error("Koi company nahi mili. " + " | ".join(notes) + " Niche ya location saaf likh kar dobara try karo, ya 2-3 minute baad dobara.")
        return
    st.caption(f"Milestone 1: {len(all_found)} companies mili ({int(time.time() - t0)} second). " + " | ".join(stat_lines))

    recs = list(all_found.values())[:HARD_CAP]
    res = run_batch(recs, deep, emails_on, people_on, people_on and len(recs) <= 25, status)
    res["rows"].sort(key=lambda r: r["_score"], reverse=True)
    st.caption(f"Milestone 2: {len(res['rows'])} companies ka data tayyar ({int(time.time() - t0)} second)")
    res["notes"] = notes
    res["cols"] = cols_for(wants, filt)

    need_enrich = filters_on or wants["revenue"] or wants["employees"]
    if not need_enrich:
        res["rows"] = trim_rows(res["rows"], per_group)
        counts = {}
        for r in res["rows"]:
            counts[r.get("Niche", "")] = counts.get(r.get("Niche", ""), 0) + 1
        res["stats"] = "Search: " + " | ".join(stat_lines) + " || Final CSV: " + (
            ", ".join(f"{k or 'sab'}: {v}" for k, v in counts.items()) or "0")
        st.session_state["res"] = res
        status.write(f"Mukammal! ({int(time.time() - t0)} second)")
        return

    # basic list pehle dikha do, phir size/revenue check
    base_rows = trim_rows(list(res["rows"]), per_group) if not filters_on else list(res["rows"])
    holder = st.empty()
    interim = dict(res)
    interim["rows"] = base_rows[:500]
    interim["unknown"] = []
    interim["stats"] = (f"Abhi size/revenue check ho raha hai. Neeche companies ki basic list hai ({len(base_rows)}). "
                        "Check ke baad ye table update ho jayegi.")
    with holder.container():
        render_results(interim, "tmp")

    targets = base_rows[:ENRICH_CAP]
    infos = enrich_many(targets, status, ENRICH_SECONDS)

    if filters_on:
        match, unknown = [], []
        for row in res["rows"]:
            info = infos.get(row["_key"])
            if info is None:
                apply_info(row, empty_info())
                row["Confidence"] = "Not checked"
                unknown.append(row)
                continue
            apply_info(row, info)
            if out_of_range(info, *filt):
                continue
            if passes(info, *filt):
                match.append(row)
            else:
                unknown.append(row)
        res["rows"] = trim_rows(match, per_group)
        res["unknown"] = trim_rows(unknown, per_group)
        res["stats"] = (f"Search: {' | '.join(stat_lines)} || Range match: {len(res['rows'])} | "
                        f"Data nahi mila / check nahi hua (alag list): {len(res['unknown'])} | "
                        f"jaanch hui: {len(infos)} companies ({int(time.time() - t0)} second)")
    else:
        res["rows"] = base_rows
        for row in res["rows"]:
            info = infos.get(row["_key"])
            apply_info(row, info if info is not None else empty_info())
        res["stats"] = (f"Search: {' | '.join(stat_lines)} || Final CSV: {len(res['rows'])} | "
                        f"size/revenue check hua: {len(infos)} companies ({int(time.time() - t0)} second)")

    holder.empty()
    st.session_state["res"] = res
    status.write("Mukammal!")


# ---------- UI ----------
tab_search, tab_bulk, tab_single = st.tabs(["Client description search", "Bulk websites", "Single website"])

with tab_search:
    st.write("Client ki need likho: niche, country/state/city, kitni companies (jaise `100 each`). "
             "Default mein Website, Company, Address, Socials aur Phone milta hai. Email, people, revenue, "
             "employees tab aate hain jab aap description mein likho (jaise `with email`, `with revenue`, `employees 10-50`). "
             "Count na likho to har niche ki lagbhag 300 companies tak ruk jata hai.")
    desc = st.text_area("Client description", height=130,
                        placeholder="Dentists in Austin, Texas. 100 companies. With employee size")

    niches, locations, country, cc = [], [], "", ""
    target, each = 0, False
    groups = []
    per_group = 0
    wants = {"revenue": False, "employees": False, "email": False, "phone": False, "people": False}
    d_emp_min = d_emp_max = 0
    d_rev_min = d_rev_max = 0.0

    if desc.strip():
        d_emp_min, d_emp_max, d_rev_min, d_rev_max = detect_filters(desc)
        q_clean = clean_description(desc)
        niches = [q_clean]
        target, each = detect_count(desc)
        wants = detect_wants(desc)
        groups = groups_from_text(desc)

        locations = [n for n, _ in STATE_LIST if re.search(r"\b" + re.escape(n) + r"\b", desc, re.I)]
        for m in re.finditer(r"\b([A-Za-z]{2})\b", desc):
            c = m.group(1)
            if c.isupper() or c.istitle():
                c = c.upper()
                if c in CODE2NAME and c not in AMBIGUOUS_CODES and CODE2NAME[c] not in locations:
                    locations.append(CODE2NAME[c])
        if re.search(r"\bnyc\b|new york city", desc, re.I):
            locations = [l for l in locations if l != "New York"] + ["New York City"]
        if re.search(r"\b(usa|u\.s\.a?\.?|united states|america)\b", desc, re.I):
            country = "USA"
        for cname, ccode in COUNTRY_CODES.items():
            if re.search(r"\b" + re.escape(cname) + r"\b", desc, re.I):
                country, cc = cname.title() if cname not in ("usa", "uk", "uae") else cname.upper(), ccode
                break

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
                cc = str(ai.get("country_code", "") or "").upper() or cc
                if int(ai.get("count", 0) or 0):
                    target = int(ai.get("count", 0))
                each = each or bool(ai.get("per_niche"))
                for w in ai.get("want", []) or []:
                    if str(w) in wants:
                        wants[str(w)] = True
                known_tags = {t for g in groups for t in g["tags"]}
                extra = [str(t) for t in ai.get("osm_tags", [])
                         if re.fullmatch(r"[a-z_:]+=[a-z_]+", str(t)) and str(t) not in known_tags]
                if extra:
                    groups.append({"label": "other", "tags": extra, "kws": [], "q": niches[0],
                                   "ov": ov_fallback(niches[0])})
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

        if not cc and country:
            cc = COUNTRY_CODES.get(country.lower(), "")
        if not cc and locations and any(norm_loc(l).lower() in STATE_BY_NAME or norm_loc(l).lower() in CITY_TO_STATE
                                        for l in locations):
            cc = "US"

        general_kws = []
        for n in niches:
            for w in re.findall(r"[a-z]{5,}", n.lower()):
                if w not in GENERIC_WORDS:
                    general_kws.append(w)
        general_kws = list(dict.fromkeys(general_kws))
        if not groups and niches:
            groups = [{"label": "", "tags": [], "kws": general_kws, "q": niches[0], "ov": ov_fallback(niches[0])}]
        for g in groups:
            if not g["kws"]:
                g["kws"] = general_kws

        if not re.search(r"\d", desc):
            d_emp_min = d_emp_max = 0
            d_rev_min = d_rev_max = 0.0
        target = min(target, 10000)
        if target:
            per_group = target if (each or len(groups) <= 1) else math.ceil(target / len(groups))
        if d_emp_min or d_emp_max:
            wants["employees"] = True
        if d_rev_min or d_rev_max:
            wants["revenue"] = True

        extras = [k for k, v in wants.items() if v]
        st.caption(
            f"Locations: {', '.join(locations) or country or 'koi nahi'} | Country code: {cc or '-'} | "
            f"Niches: {', '.join(g['label'] or g['q'] for g in groups) or '-'} | "
            f"Har niche ka target: {per_group or f'lagbhag {DEFAULT_CAP}'} | "
            f"Extra data: {', '.join(extras) or 'koi nahi (sirf default)'} | "
            f"Employees filter: {d_emp_min or '-'} se {d_emp_max or 'upar'} | Revenue filter ($M): {d_rev_min or '-'} se {d_rev_max or 'upar'}"
        )

    if st.button("Dhoondo", key="search_btn") and desc.strip():
        if not groups:
            st.error("Niche samajh nahi aaya. Description mein business type saaf likho (jaise dental, real estate).")
        else:
            cfg = {"groups": groups, "locations": locations, "country": country, "cc": cc,
                   "per_group": per_group, "wants": wants,
                   "filt": (d_emp_min, d_emp_max, d_rev_min, d_rev_max)}
            try:
                run_search(cfg)
            except Exception as e:
                st.error("Run beech mein ruk gaya. Ye error screenshot mein bhej do:")
                st.exception(e)

with tab_bulk:
    st.write("Har line mein ek website. Company ka naam dena ho to comma lagao: `example.com, Example Corp`. Is tab mein email, phone, people sab nikalte hain.")
    text = st.text_area("Websites ki list", height=180, placeholder="example.com\nanother.com, Another Corp")
    up = st.file_uploader("Ya CSV upload karo", type="csv")

    b_recs = []
    for line in text.splitlines():
        if line.strip():
            parts = [x.strip() for x in line.split(",", 1)]
            b_recs.append(manual_rec(parts[0], parts[1] if len(parts) > 1 else ""))

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
                b_recs.append(manual_rec(w, comp))

    if b_recs:
        st.info(f"{len(b_recs)} websites mili.")
    if st.button("Bulk start karo", key="bulk_btn") and b_recs:
        status_b = st.empty()
        res = run_batch(b_recs[:HARD_CAP], True, True, True, len(b_recs) <= 25, status_b)
        res["rows"].sort(key=lambda r: r["_score"], reverse=True)
        res["cols"] = ALL_COLS
        res["stats"] = f"{len(res['rows'])} websites ka data tayyar"
        st.session_state["res"] = res
        status_b.write("Mukammal!")

with tab_single:
    s1, s2 = st.columns(2)
    url_input = s1.text_input("Website ka link", placeholder="example.com")
    company_input = s2.text_input("Company ka naam (optional)")
    if st.button("Dhoondo", key="single_btn") and url_input:
        status_s = st.empty()
        res = run_batch([manual_rec(url_input, company_input)], True, True, True, True, status_s)
        res["cols"] = ALL_COLS
        res["stats"] = f"{len(res['rows'])} website ka data tayyar"
        st.session_state["res"] = res
        status_s.write("Mukammal!")

show_results()
