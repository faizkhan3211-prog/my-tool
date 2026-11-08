"""Universal research tab: koi bhi brief + koi bhi columns -> web se rows, har value ke saath source.
Anti-hallucination rule: value tab hi rakhi jati hai jab uska source page (jo tool ne khud fetch kiya) us value ya quote ko
sach mein contain kare. Warna 'Not found'."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

NF = "Not found"
SKIP_FETCH = ("facebook.", "instagram.", "pinterest.", "youtube.", "tiktok.", "linkedin.", "twitter.", "x.com")
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]


def norm(s):
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def parse_cols(txt):
    txt = (txt or "").strip()
    if not txt:
        return []
    parts = re.split(r"[\t\n]+", txt) if ("\t" in txt or "\n" in txt) else txt.split(",")
    out = []
    for p in parts:
        p = p.strip().strip('"').strip()
        if p and p not in out:
            out.append(p)
    return out


def gjson(prompt, key, models, timeout=90):
    last = "koi model nahi chala"
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(
                    url, headers={"x-goog-api-key": key}, timeout=timeout,
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
                if r.status_code == 200:
                    t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    t = re.sub(r"^```(?:json)?|```$", "", t).strip()
                    return json.loads(t), ""
                last = f"{m}: HTTP {r.status_code}"
                if r.status_code in (429, 500, 503) and attempt == 0:
                    time.sleep(4)
                    continue
                break
            except Exception as e:
                last = f"{m}: {str(e)[:80]}"
                break
    return None, last


def ggrounded(prompt, key, models, timeout=90):
    """Gemini + Google Search grounding: sirf SOURCE links (urls) wapas leta hai, jawab ka text use nahi hota."""
    last = ""
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, timeout=timeout,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "tools": [{"google_search": {}}]})
            if r.status_code == 200:
                cand = r.json()["candidates"][0]
                chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
                out = []
                for ch in chunks:
                    w = ch.get("web") or {}
                    if w.get("uri"):
                        out.append((w["uri"], w.get("title", "")))
                return out, ""
            last = f"{m}: HTTP {r.status_code}"
            if r.status_code in (429, 403):
                return [], "QUOTA " + last
        except Exception as e:
            last = f"{m}: {str(e)[:80]}"
    return [], last


def resolve_url(uri):
    try:
        r = requests.get(uri, headers={"User-Agent": "Mozilla/5.0"}, timeout=(4, 8), stream=True, allow_redirects=True)
        u = r.url
        r.close()
        return u
    except Exception:
        return uri


def google_corpus(queries, cfg, fetch, used, max_pages):
    """Google Search grounding se mile source pages ka corpus (free quota khatam ho to khamoshi se band)."""
    lock = cfg["g_lock"]
    found = []
    for q in queries[: cfg["g_per_round"]]:
        with lock:
            if cfg["g_dead"] or cfg["g_left"] <= 0:
                break
            cfg["g_left"] -= 1
        prompt = ("Use Google Search. Find real, specific entities (with their official websites and sources) for this request: "
                  + cfg["task"] + ". Search focus: " + q)
        urls, err = ggrounded(prompt, cfg["api_key"], cfg["models"])
        if err.startswith("QUOTA"):
            cfg["g_dead"] = True
            cfg["g_notes"].append("Google Search ki free quota khatam/block: baaki kaam DuckDuckGo se hua (" + err + ")")
            break
        if err and not urls:
            cfg["g_notes"].append("Google grounding: " + err)
        for uri, title in urls:
            final = resolve_url(uri)
            with lock:
                if final in used:
                    continue
                used.add(final)
            found.append((final, title))
    corpus = {}
    to_fetch = [(u, t) for u, t in found if not any(b in u.lower() for b in SKIP_FETCH)][:max_pages]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, u): (u, t) for u, t in to_fetch}
        for f in as_completed(futs):
            u, t = futs[f]
            try:
                html = f.result()
            except Exception:
                html = None
            if html:
                corpus[u] = (f"{t} {u}\n" + page_text(html))[:6000]
    for u, t in found:
        corpus.setdefault(u, f"{t} {u}")
    return corpus


def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "noscript", "svg"]):
        s.decompose()
    return " ".join(soup.get_text(" ").split())


def search_corpus(queries, DDGS, lock, fetch, max_pages, used):
    """queries chalao, results + pages ka corpus {url: text} banao."""
    results = []
    if DDGS is None:
        return {}
    with lock:
        for q in queries:
            try:
                rs = DDGS().text(q, max_results=10)
            except Exception:
                rs = []
            for r in rs or []:
                h = r.get("href", "")
                if h and h not in used:
                    used.add(h)
                    results.append(r)
            time.sleep(0.8)
    corpus, snippets = {}, {}
    for r in results:
        h = r["href"]
        snippets[h] = f"{r.get('title', '')}. {r.get('body', '')} {h}"
    to_fetch = [h for h in snippets if not any(b in h.lower() for b in SKIP_FETCH)][:max_pages]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, h): h for h in to_fetch}
        for f in as_completed(futs):
            h = futs[f]
            try:
                html = f.result()
            except Exception:
                html = None
            if html:
                corpus[h] = (snippets[h] + "\n" + page_text(html))[:6000]
    for h, s in snippets.items():
        corpus.setdefault(h, s)
    return corpus


def make_batches(corpus, limit=14000):
    batch, size, out = {}, 0, []
    for u, t in corpus.items():
        if batch and size + len(t) > limit:
            out.append(batch)
            batch, size = {}, 0
        batch[u] = t
        size += len(t)
    if batch:
        out.append(batch)
    return out


def sources_block(batch):
    return "\n\n".join(f"SOURCE_URL: {u}\nTEXT: {t}" for u, t in batch.items())


def verify_field(f, corpus):
    """Value tab hi manzoor jab source page mein value ya quote mojood ho."""
    if not isinstance(f, dict):
        return NF, ""
    v = str(f.get("v", "") or "").strip()
    if not v or v.lower() in ("not found", "n/a", "none", "unknown", "null"):
        return NF, ""
    u = str(f.get("u", "") or "").strip()
    q = str(f.get("q", "") or "")
    text = norm(corpus.get(u, ""))
    if not text:
        return NF, ""
    nv, nq = norm(v), norm(q)
    if (nv and nv in text) or (len(nq) >= 8 and nq in text):
        return v, u
    return NF, ""


def row_key(vals, key_cols):
    parts = [norm(vals.get(k, "")) for k in key_cols if vals.get(k, NF) != NF]
    return "|".join(parts) if parts else ""


def merge_row(rows, new, key_cols, cols):
    k = row_key(new["vals"], key_cols)
    if not k:
        return
    cur = rows.get(k)
    if not cur:
        rows[k] = new
        return
    for c in cols:
        if cur["vals"].get(c, NF) == NF and new["vals"].get(c, NF) != NF:
            cur["vals"][c] = new["vals"][c]
            cur["src"][c] = new["src"].get(c, "")


def extract_batch(batch, cfg):
    cols = [c for c in cfg["cols"] if c not in cfg["derived"]]
    prompt = (
        "You are a careful research analyst.\n"
        "TASK: " + cfg["task"] + "\n"
        + ("EXTRA RULES: " + cfg["rules"] + "\n" if cfg["rules"] else "")
        + "COLUMNS: " + json.dumps(cols, ensure_ascii=False) + "\n\n"
        "Below are SOURCES (url + text). Find every distinct entity (one row each) that matches the TASK and is "
        "clearly supported by the sources. For every column return an object {\"v\": value, \"u\": source url, "
        "\"q\": an exact quote copied from that source text, max 15 words}. \"u\" must be exactly one of the given "
        "SOURCE_URLs. If a column is not stated in the sources return {\"v\": \"Not found\", \"u\": \"\", \"q\": \"\"}. "
        "NEVER guess, never use outside knowledge, never invent emails, dates or numbers.\n"
        "Return JSON only: {\"rows\": [ {\"<column>\": {\"v\":..,\"u\":..,\"q\":..}, ...}, ... ]}\n\n"
        + sources_block(batch))
    data, err = gjson(prompt, cfg["api_key"], cfg["models"])
    out = []
    if not data:
        return out, err
    rows = data.get("rows", []) if isinstance(data, dict) else data
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        vals, src = {}, {}
        for c in cols:
            vals[c], src[c] = verify_field(r.get(c), batch)
        if row_key(vals, cfg["key_cols"]):
            out.append({"vals": vals, "src": src})
    return out, ""


def plan_queries(cfg, used_queries, found_names):
    prompt = (
        "TASK: " + cfg["task"] + "\n"
        + ("EXTRA RULES: " + cfg["rules"] + "\n" if cfg["rules"] else "")
        + "Write up to 8 diverse web search queries (short, 3-9 words) that would find real pages listing entities "
        "that match this task (directories, official sites, news, lists). Do not repeat these: "
        + json.dumps(used_queries[:30]) + ". Already found: " + json.dumps(found_names[:30]) + ".\n"
        'Return JSON only: {"queries": ["..."]}')
    data, err = gjson(prompt, cfg["api_key"], cfg["models"])
    if not data:
        return [], err
    qs = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
    return [q for q in qs if q not in used_queries][:8], ""


def enrich_row(row, cfg, DDGS, lock, fetch, start):
    if time.time() - start > cfg["budget"]:
        return row, "time khatam"
    cols = [c for c in cfg["cols"] if c not in cfg["derived"]]
    missing = [c for c in cols if row["vals"].get(c, NF) in (NF, "")]
    if not missing:
        return row, ""
    label = " ".join(row["vals"][k] for k in cfg["key_cols"] if row["vals"].get(k, NF) != NF)
    if not label:
        return row, ""
    queries = [f'"{label}" ' + " ".join(missing[:2]), f'"{label}" official website']
    corpus = search_corpus(queries, DDGS, lock, fetch, 4, set())
    if cfg.get("use_google"):
        corpus.update(google_corpus([queries[0]], cfg, fetch, set(), 3))
    if not corpus:
        return row, "search se kuch nahi mila"
    known = {c: row["vals"][c] for c in cols if row["vals"].get(c, NF) != NF}
    prompt = (
        "ENTITY: " + label + "\nKNOWN: " + json.dumps(known, ensure_ascii=False) + "\n"
        "Fill ONLY these columns: " + json.dumps(missing, ensure_ascii=False) + "\n"
        "Use ONLY the sources below, and only if the source clearly refers to this exact entity (not a namesake). "
        "For each column return {\"v\": value, \"u\": one of the SOURCE_URLs, \"q\": exact quote from that source, "
        "max 15 words}, else {\"v\": \"Not found\", \"u\": \"\", \"q\": \"\"}. Never guess.\n"
        "Return JSON only: {\"<column>\": {\"v\":..,\"u\":..,\"q\":..}}\n\n"
        + sources_block(corpus)[:30000])
    data, err = gjson(prompt, cfg["api_key"], cfg["models"])
    if not data or not isinstance(data, dict):
        return row, err
    for c in missing:
        v, u = verify_field(data.get(c), corpus)
        if v != NF:
            row["vals"][c], row["src"][c] = v, u
    return row, ""


def derive_row(row, cfg):
    if not cfg["derived"]:
        return row, ""
    cols = [c for c in cfg["cols"] if c not in cfg["derived"]]
    known = {c: row["vals"].get(c, NF) for c in cols}
    prompt = (
        "TASK CONTEXT: " + cfg["task"] + "\n"
        "RUBRIC / RULES: " + (cfg["rules"] or "none given - use sensible judgement") + "\n"
        "ROW DATA (verified from sources): " + json.dumps(known, ensure_ascii=False) + "\n"
        "Calculate ONLY these columns: " + json.dumps(cfg["derived"], ensure_ascii=False) + "\n"
        "Base them strictly on ROW DATA. Where data is 'Not found', score conservatively and say so in comments. "
        "Keep comments short and precise.\n"
        "Return JSON only: {\"<column>\": value}")
    data, err = gjson(prompt, cfg["api_key"], cfg["models"])
    if not data or not isinstance(data, dict):
        return row, err
    for c in cfg["derived"]:
        row["vals"][c] = str(data.get(c, NF))
        row["src"][c] = "AI-calculated"
    return row, ""


def run_research(cfg, DDGS, lock, fetch, log):
    start = time.time()
    notes, rows = [], {}
    cols = cfg["cols"]
    cfg.setdefault("use_google", False)
    cfg.setdefault("g_left", 15)
    cfg.setdefault("g_dead", False)
    cfg.setdefault("g_per_round", 3)
    cfg.setdefault("g_notes", [])
    cfg.setdefault("g_lock", threading.Lock())

    if cfg["mode"] == "list":
        for r in cfg["input_rows"]:
            vals = {c: (str(r.get(c)) if str(r.get(c, "")).strip() and str(r.get(c)) != "nan" else NF) for c in cols}
            rows[row_key(vals, cfg["key_cols"]) or str(len(rows))] = {"vals": vals, "src": {c: "Aap ki list" for c in cols if vals[c] != NF}}
    else:
        if DDGS is None:
            return [], ["ddgs library install nahi hui"]
        used_urls, used_q = set(), []
        for rnd in range(3):
            if len(rows) >= cfg["target"] or time.time() - start > cfg["budget"]:
                break
            qs, err = plan_queries(cfg, used_q, [row_key(x["vals"], cfg["key_cols"]) for x in rows.values()])
            if not qs:
                notes.append("Search queries nahi ban sakin: " + err)
                break
            used_q += qs
            log(f"Round {rnd + 1}: {len(qs)} searches chal rahi hain...")
            corpus = search_corpus(qs, DDGS, lock, fetch, cfg["max_pages"], used_urls)
            if cfg["use_google"]:
                log(f"Round {rnd + 1}: Google Search grounding bhi chal rahi hai...")
                corpus.update(google_corpus(qs, cfg, fetch, used_urls, cfg["max_pages"]))
            batches = make_batches(corpus)
            log(f"Round {rnd + 1}: {len(corpus)} pages/results mile, {len(batches)} hisson mein padhe ja rahe hain...")
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = [ex.submit(extract_batch, b, cfg) for b in batches]
                for i, f in enumerate(as_completed(futs), 1):
                    try:
                        got, err = f.result()
                    except Exception as e:
                        got, err = [], str(e)[:80]
                    if err:
                        notes.append(err)
                    for nr in got:
                        merge_row(rows, nr, cfg["key_cols"], [c for c in cols if c not in cfg["derived"]])
                    log(f"Round {rnd + 1}: hissa {i}/{len(batches)} | ab tak rows: {len(rows)}")
        keys = list(rows.keys())[: cfg["target"]]
        rows = {k: rows[k] for k in keys}

    todo = list(rows.values())
    log(f"Missing fields bharne ka kaam: {len(todo)} rows...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(enrich_row, r, cfg, DDGS, lock, fetch, start) for r in todo]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                _, err = f.result()
                if err:
                    notes.append(err)
            except Exception as e:
                notes.append(str(e)[:80])
            log(f"Enrich {i}/{len(todo)}")
    if cfg["derived"]:
        log("Scoring / calculated columns ban rahe hain...")
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(derive_row, r, cfg) for r in todo]
            for f in as_completed(futs):
                try:
                    _, err = f.result()
                    if err:
                        notes.append(err)
                except Exception as e:
                    notes.append(str(e)[:80])
    return todo, list(dict.fromkeys(notes + cfg["g_notes"]))


def to_df(rows, cols):
    recs = []
    for r in rows:
        d = {c: r["vals"].get(c, NF) for c in cols}
        d["Sources (field: url)"] = " | ".join(
            f"{c}: {u}" for c, u in r["src"].items() if u and u.startswith("http"))
        d["Not found count"] = sum(1 for c in cols if d[c] == NF)
        recs.append(d)
    return pd.DataFrame(recs, columns=cols + ["Sources (field: url)", "Not found count"])


def render_universal(fetch, DDGS, lock, get_secret):
    st.write("Koi bhi research kaam: client ki brief likho aur jo columns chahiye wo paste karo (Excel ki header row seedhi paste ho jati hai). "
             "Tool web se rows dhoondta hai, har value ka source link deta hai, aur jo cheez source se sabit na ho wahan **Not found** likhta hai.")
    key = get_secret("GEMINI_API_KEY")
    if not key:
        st.error("Is tab ke liye Secrets mein GEMINI_API_KEY zaroori hai.")
    models = tuple(m for m in dict.fromkeys([get_secret("GEMINI_MODEL")] + DEFAULT_MODELS) if m)

    mode_lbl = st.radio("Kaam ki qism", ["Naye rows dhoondo (brief se)", "Meri list ke rows bharo (CSV upload)"],
                        key="uni_mode", horizontal=True)
    mode = "search" if mode_lbl.startswith("Naye") else "list"
    task = st.text_area("Client ki brief / kaam", height=120, key="uni_task",
                        placeholder="Jaise: Early-stage fintech investors in UK/EU jinhon ne last 12 mahine mein investment ki ho, ticket $250k-$2M")
    cols_txt = st.text_area("Columns (har column nayi line par, ya Excel header row paste karo)", height=100, key="uni_cols")
    cols = parse_cols(cols_txt)
    rules = st.text_area("Rules / scoring rubric / validation instructions (optional)", height=100, key="uni_rules",
                         placeholder="Jaise: Sector Fit /30 = ..., Recent Activity /20 = last 12 mahine ka deal ho to full marks...")
    key_cols = st.multiselect("Unique key columns (kis se pata chale ke row duplicate hai)", cols,
                              default=cols[:1], key="uni_keys")
    derived = st.multiselect("AI-calculated columns (score, comment, priority jaise, jo source se nahi nikalte)", cols,
                             key="uni_derived")
    c1, c2, c3 = st.columns(3)
    target = c1.number_input("Kitne rows chahiye", 1, 100, 15, key="uni_target")
    max_pages = c2.number_input("Har round mein max pages", 5, 60, 25, key="uni_pages")
    budget = c3.number_input("Time limit (minute)", 2, 30, 10, key="uni_budget") * 60

    use_google = st.checkbox("Gemini + Google Search bhi use karo (free API key ki quota tak; khatam ho to khud DuckDuckGo par aa jata hai)",
                             value=True, key="uni_google")
    g_max = st.number_input("Google searches ki hadd (is run mein)", 1, 100, 15, key="uni_gmax") if use_google else 0

    input_rows = []
    if mode == "list":
        up = st.file_uploader("Apni list CSV upload karo", type="csv", key="uni_csv")
        if up is not None:
            df_in = pd.read_csv(up, dtype=str)
            st.caption(f"{len(df_in)} rows mili. Columns: {', '.join(df_in.columns)}")
            input_rows = df_in.to_dict("records")
            if not cols:
                cols = list(df_in.columns)

    if st.button("Research shuru karo", key="uni_btn"):
        if not key:
            st.error("GEMINI_API_KEY nahi hai.")
        elif not task.strip() and mode == "search":
            st.error("Brief likho.")
        elif not cols or not key_cols:
            st.error("Columns aur kam az kam ek unique key column chahiye.")
        elif mode == "list" and not input_rows:
            st.error("CSV upload karo.")
        else:
            cfg = {"task": task.strip() or "Fill the missing columns for each given entity.", "cols": cols,
                   "derived": derived, "key_cols": key_cols, "rules": rules.strip(), "target": int(target),
                   "max_pages": int(max_pages), "budget": int(budget), "mode": mode, "input_rows": input_rows,
                   "api_key": key, "models": models, "use_google": bool(use_google), "g_left": int(g_max)}
            status = st.empty()
            try:
                rows, notes = run_research(cfg, DDGS, lock, fetch, status.write)
                st.session_state["uni_res"] = {"df": to_df(rows, cols), "notes": notes}
                status.write("Mukammal!")
            except Exception as e:
                st.error("Run beech mein ruk gaya:")
                st.exception(e)

    res = st.session_state.get("uni_res")
    if res:
        df = res["df"]
        if df.empty:
            st.warning("Koi row source se sabit nahi hui. Brief ko aur saaf likho ya columns kam karo.")
        else:
            st.success(f"{len(df)} rows. Jin fields ka source nahi mila wahan 'Not found' hai. "
                       "AI-calculated columns ko client ko bhejne se pehle khud dekh lo.")
            try:
                st.dataframe(df, width="stretch")
            except Exception:
                st.dataframe(df)
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "research.csv", key="uni_dl")
        for n in res["notes"][:6]:
            st.caption("Note: " + n)
