"""Booking Prospect Checker: websites ko check karta hai (WhatsApp, booking provider, manual-booking ishaare,
contact form, email, phone, socials, regular booking evidence). Sab kuch website ke text/links se, har cheez ke saath page link.
AI use nahi hota, is liye koi andaza ya hallucination nahi."""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

PROVIDERS = {
    "Bookwhen": ["bookwhen.com"], "Fresha": ["fresha.com"], "Booksy": ["booksy.com"], "Setmore": ["setmore.com"],
    "SimplyBook.me": ["simplybook.me"], "Acuity/Squarespace Scheduling": ["acuityscheduling.com", "squarespacescheduling.com"],
    "Square Appointments": ["squareup.com/appointments", "square.site"], "Mindbody": ["mindbodyonline.com", "mindbody.io"],
    "Glofox": ["glofox.com"], "TeamUp": ["teamup.com", "goteamup.com"], "bsport": ["bsport.io"],
    "Gymcatch": ["gymcatch.com"], "ClassForKids": ["classforkids.io", "classforkids.com"], "Book That In": ["bookthatin.com"],
    "Pembee": ["pembee.co.uk"], "TicketSource": ["ticketsource.co.uk", "ticketsource.com"], "Eventbrite": ["eventbrite."],
    "Ticket Tailor": ["tickettailor.com"], "Skiddle": ["skiddle.com"], "Fatsoma": ["fatsoma.com"], "DICE": ["dice.fm"],
    "TryBooking": ["trybooking.com"], "Ticketleap": ["ticketleap.com"], "Billetto": ["billetto."], "SimpleTix": ["simpletix.com"],
    "FareHarbor": ["fareharbor.com"], "Rezdy": ["rezdy.com"], "Checkfront": ["checkfront.com"], "Beyonk": ["beyonk.com"],
    "BookingLive": ["bookinglive.com"], "DigiTickets": ["digitickets.co.uk"], "OpenTable": ["opentable."],
    "ResDiary": ["resdiary.com"], "Treatwell": ["treatwell."], "Vagaro": ["vagaro.com"], "Calendly": ["calendly.com"],
    "Bookeo": ["bookeo.com"], "Timely": ["gettimely.com", "timely.app"], "Jane App": ["janeapp.com"],
    "Cliniko": ["cliniko.com"], "Momence": ["momence.com"], "Wix Bookings": ["wixbookings", "bookings.wix"],
    "Planyo": ["planyo.com"], "Booking Hound": ["bookinghound"],
}
WA_RE = re.compile(r"(wa\.me/|api\.whatsapp\.com/send|web\.whatsapp\.com/send|wa\.link/|whatsapp://send)", re.I)
MANUAL_RE = re.compile(
    r"((?:message|dm|pm|whatsapp|text|call|ring|email|contact)\s+(?:us\s+)?(?:to|for)\s+book(?:ing)?s?\b"
    r"|book(?:ings?)?\s+(?:via|by|through|on)\s+(?:whatsapp|phone|email|message|dm|text|telephone)"
    r"|bookings?\s+(?:are\s+)?(?:taken|made|handled)\s+(?:via|by|through|on)\s+\w+"
    r"|to\s+book\s+(?:a\s+)?(?:place|slot|session|appointment|class|table|space)[^.]{0,50}(?:whatsapp|call|message|dm|text|email))",
    re.I)
REGULAR_RE = re.compile(
    r"(every\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|day)"
    r"|weekly|timetable|class\s+schedule|term[- ]time|waiting\s+list|sold\s+out|fully\s+booked|upcoming\s+(?:dates|events|sessions|classes)"
    r"|next\s+(?:class|session|event)|per\s+week|\bmon(?:day)?s?\b[^.]{0,30}\d{1,2}[:.]\d{2}|whats?\s*on)", re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_EMAIL = ("example.", "sentry", "wixpress", "domain.com", "yourdomain", "schema.org", "w3.org", ".png", ".jpg", ".webp", ".svg")
PAGE_KEYS = ["book", "timetable", "schedule", "classes", "class", "contact", "about", "events", "prices", "sessions",
             "calendar", "whats-on", "whatson", "appointments", "hire", "enquir"]
BAD_SOCIAL = ("sharer", "share", "intent", "dialog", "login", "plugins", "/p/", "/reel", "/explore", "hashtag")


def snippet_around(text, start, end, pad=70):
    return " ".join(text[max(0, start - pad): end + pad].split())[:220]


def fetch_site(fetch, domain):
    for base in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        h = fetch(base)
        if h:
            return h, base
    return None, ""


def pick_pages(html, base, limit=8):
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base).netloc.replace("www.", "")
    scored, seen = [], {base.rstrip("/")}
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"]).split("#")[0].rstrip("/")
        p = urlparse(full)
        if p.netloc.replace("www.", "") != host or full in seen:
            continue
        low = full.lower()
        for i, k in enumerate(PAGE_KEYS):
            if k in low:
                seen.add(full)
                scored.append((i, full))
                break
    scored.sort()
    return [u for _, u in scored[:limit]]


def analyse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    out = {"wa": set(), "emails": set(), "phones": set(), "fb": set(), "ig": set(), "providers": {}, "form": False,
           "manual": [], "regular": [], "title": ""}
    og = soup.find("meta", property="og:site_name")
    out["title"] = (og.get("content") if og and og.get("content") else (soup.title.string.strip() if soup.title and soup.title.string else ""))
    blob_links = []
    for tag in soup.find_all(["a", "iframe", "script", "link", "form"]):
        for attr in ("href", "src", "action", "data-src"):
            v = tag.get(attr)
            if v:
                blob_links.append(urljoin(url, v.strip()))
    for l in blob_links:
        low = l.lower()
        if WA_RE.search(l):
            out["wa"].add(l.split("&")[0] if "text=" not in low else l)
        if low.startswith("mailto:"):
            e = l[7:].split("?")[0].strip().lower()
            if e:
                out["emails"].add(e)
        if low.startswith("tel:"):
            out["phones"].add(l[4:].strip())
        host = urlparse(l).netloc.lower().replace("www.", "")
        if (host == "facebook.com" or host.endswith(".facebook.com")) and not any(b in low for b in BAD_SOCIAL):
            segs = [s for s in urlparse(l).path.split("/") if s]
            if segs:
                out["fb"].add(l.split("?")[0].rstrip("/"))
        if (host == "instagram.com" or host.endswith(".instagram.com")) and not any(b in low for b in BAD_SOCIAL):
            segs = [s for s in urlparse(l).path.split("/") if s]
            if segs:
                out["ig"].add(l.split("?")[0].rstrip("/"))
        for name, keys in PROVIDERS.items():
            if name not in out["providers"] and any(k in low for k in keys):
                out["providers"][name] = l
    for f in soup.find_all("form"):
        fields = f.find_all(["input", "textarea", "select"])
        has_msg = f.find("textarea") is not None
        has_email = any((i.get("type") == "email") or "email" in (i.get("name") or "").lower() for i in f.find_all("input"))
        texts = [i for i in f.find_all("input") if (i.get("type") or "text") in ("text", "email", "tel")]
        if (has_msg or (has_email and len(texts) >= 2)) and len(fields) >= 2:
            out["form"] = True
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    text = " ".join(soup.get_text(" ").split())[:300000]
    for e in EMAIL_RE.findall(text):
        e = e.lower()
        if not any(b in e for b in BAD_EMAIL):
            out["emails"].add(e)
    last = -999
    for m in MANUAL_RE.finditer(text):
        if len(out["manual"]) < 3 and m.start() - last > 200:
            out["manual"].append((snippet_around(text, m.start(), m.end(), 50), url))
            last = m.end()
    last = -999
    for m in REGULAR_RE.finditer(text):
        if len(out["regular"]) < 3 and m.start() - last > 200:
            out["regular"].append((snippet_around(text, m.start(), m.end(), 50), url))
            last = m.end()
    return out


def check_site(domain, fetch):
    html, base = fetch_site(fetch, domain)
    row = {"Website": domain, "Business": "", "Date checked": date.today().isoformat(), "Reachable": "No"}
    if not html:
        row["Status"] = "Website khuli nahi"
        return row
    row["Reachable"] = "Yes"
    pages = [(base, html)]
    for u in pick_pages(html, base):
        h = fetch(u)
        if h:
            pages.append((u, h))
    agg = {"wa": set(), "emails": set(), "phones": set(), "fb": set(), "ig": set(), "providers": {}, "form": [],
           "manual": [], "regular": []}
    for u, h in pages:
        try:
            a = analyse_page(h, u)
        except Exception:
            continue
        if not row["Business"] and a["title"]:
            row["Business"] = a["title"][:80]
        for k in ("wa", "emails", "phones", "fb", "ig"):
            agg[k] |= a[k]
        for n, l in a["providers"].items():
            agg["providers"].setdefault(n, (l, u))
        if a["form"]:
            agg["form"].append(u)
        agg["manual"] += a["manual"]
        agg["regular"] += a["regular"]
    row["Business"] = row["Business"] or domain
    row["Booking provider (detected)"] = "; ".join(agg["providers"]) or "Not found"
    row["Provider evidence link"] = "; ".join(f"{n}: {l}" for n, (l, _) in list(agg["providers"].items())[:3])
    row["Manual booking signals"] = " || ".join(s for s, _ in agg["manual"][:3]) or "Not found"
    row["Manual signal page"] = "; ".join(dict.fromkeys(u for _, u in agg["manual"][:3]))
    row["Regular booking evidence"] = " || ".join(s for s, _ in agg["regular"][:3]) or "Not found"
    row["Regular evidence page"] = "; ".join(dict.fromkeys(u for _, u in agg["regular"][:3]))
    row["WhatsApp link"] = "; ".join(sorted(agg["wa"])[:2]) or "Not found"
    row["Email (public)"] = "; ".join(sorted(agg["emails"])[:4]) or "Not found"
    row["Phone (tel link)"] = "; ".join(sorted(agg["phones"])[:2]) or "Not found"
    row["Contact form page"] = "; ".join(dict.fromkeys(agg["form"][:2])) or "Not found"
    row["Facebook"] = sorted(agg["fb"])[0] if agg["fb"] else "Not found"
    row["Instagram"] = sorted(agg["ig"])[0] if agg["ig"] else "Not found"
    routes = sum(1 for k in ("WhatsApp link", "Email (public)", "Phone (tel link)", "Contact form page", "Facebook", "Instagram")
                 if row[k] != "Not found")
    row["Contact routes found"] = routes
    signal = row["Booking provider (detected)"] != "Not found" or row["Manual booking signals"] != "Not found"
    regular = row["Regular booking evidence"] != "Not found"
    if signal and regular and routes:
        row["Status"] = "Qualified candidate (khud verify karo)"
    elif routes and (signal or regular):
        row["Status"] = "Investigate further"
    else:
        row["Status"] = "Weak / kam saboot"
    missing = ["decision-maker", "current pricing/costs", "contract & switching", "recent complaints (social)"]
    if not regular:
        missing.insert(0, "regular bookings ka saboot")
    if not routes:
        missing.insert(0, "contact route")
    row["Khud verify karna hai"] = ", ".join(missing)
    return row


def discover(DDGS, lock, domain_of, industry, location, n):
    if DDGS is None:
        return []
    qs = [f"{industry} {location} book online", f"{industry} {location} message to book",
          f"{industry} {location} WhatsApp to book", f"{industry} {location} timetable classes"]
    found = []
    with lock:
        for q in qs:
            try:
                rs = DDGS().text(q, max_results=20)
            except Exception:
                rs = []
            for r in rs or []:
                d = domain_of(r.get("href", ""))
                if d and d not in found:
                    found.append(d)
            time.sleep(0.8)
            if len(found) >= n * 2:
                break
    return found[: n * 2]


def render_booking(fetch, DDGS, lock, domain_of):
    st.write("Booking-system wale prospects ke liye: websites ki list daalo (ya niche + location se dhoondo). Tool har website ke "
             "booking page, contact page, timetable wagera padhta hai aur nikalta hai: **WhatsApp link, booking provider, "
             "'message to book' jaise ishaare, contact form, email, phone, Facebook/Instagram, aur regular booking ka saboot**. "
             "Har cheez ke saath page link hota hai. AI use nahi hota, is liye koi andaza nahi. Complaints, pricing aur contract "
             "tool nahi dekh sakta, wo aap ko khud check karna hai.")
    mode = st.radio("Websites kahan se", ["Meri list", "Niche + location se dhoondo"], horizontal=True, key="bk_mode")
    domains = []
    if mode == "Meri list":
        txt = st.text_area("Har line mein ek website", height=140, key="bk_txt", placeholder="example.com\nanother.co.uk")
        for line in txt.splitlines():
            d = urlparse(line.strip() if line.strip().startswith("http") else "https://" + line.strip()).netloc.lower().replace("www.", "")
            if d and "." in d and d not in domains:
                domains.append(d)
    else:
        c1, c2, c3 = st.columns(3)
        industry = c1.text_input("Industry", placeholder="yoga studio", key="bk_ind")
        location = c2.text_input("Location", placeholder="Manchester UK", key="bk_loc")
        n = int(c3.number_input("Kitni websites check karni hain", 3, 60, 15, key="bk_n"))
    if st.button("Check karo", key="bk_btn"):
        if mode != "Meri list":
            if not industry.strip() or not location.strip():
                st.error("Industry aur location dono likho.")
                return
            with st.spinner("Websites dhoondi ja rahi hain..."):
                domains = discover(DDGS, lock, domain_of, industry.strip(), location.strip(), n)
            st.caption(f"{len(domains)} websites mili (directories/social sites hata di gayi).")
        if not domains:
            st.error("Koi website nahi mili.")
        else:
            status, rows = st.empty(), []
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(check_site, d, fetch): d for d in domains[:120]}
                for i, f in enumerate(as_completed(futs), 1):
                    try:
                        rows.append(f.result())
                    except Exception:
                        rows.append({"Website": futs[f], "Status": "Error"})
                    status.write(f"{i}/{len(futs)} websites check ho gayin")
            order = {"Qualified candidate (khud verify karo)": 0, "Investigate further": 1, "Weak / kam saboot": 2}
            rows.sort(key=lambda r: order.get(r.get("Status"), 3))
            st.session_state["bk_res"] = pd.DataFrame(rows)
            status.write("Mukammal!")
    df = st.session_state.get("bk_res")
    if df is not None and not df.empty:
        st.success("'Qualified candidate' ka matlab sirf ye hai ke provider/manual ishaara, regular booking ka lafz aur contact route "
                   "teeno mile. Final faisla aap ka hai: website khud kholkar dekho.")
        try:
            st.dataframe(df, width="stretch")
        except Exception:
            st.dataframe(df)
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"), "booking_prospects.csv", key="bk_dl")
