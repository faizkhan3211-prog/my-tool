import io
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from ddgs import DDGS
from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================
st.set_page_config(
    page_title="Lead Research & Intelligence",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {max-width: 1500px; padding-top: 1.5rem; padding-bottom: 3rem;}
    [data-testid="stSidebar"] {border-right: 1px solid rgba(128,128,128,.18);}
    .hero {padding: 1.25rem 1.5rem; border: 1px solid rgba(128,128,128,.22);
           border-radius: 16px; margin-bottom: 1.2rem;
           background: linear-gradient(120deg, rgba(40,100,180,.12), rgba(20,170,160,.08));}
    .hero h1 {margin: 0 0 .35rem 0; font-size: 1.9rem;}
    .hero p {margin: 0; opacity: .78;}
    div[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.2);
                                  padding: 12px 14px; border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)

USER_AGENT = "LeadResearchStreamlit/2.0 (public website research)"
REQUEST_TIMEOUT = 18
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_BULK_WEBSITES = 50
MAX_RESEARCH_COMPANIES = 30
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

ROLE_PATTERN = (
    r"owner|founder|co[- ]founder|chief executive officer|ceo|president|"
    r"managing director|general manager|principal|partner|director|"
    r"operations manager|office manager|sales manager|marketing manager"
)
PERSON_NAME_PATTERN = (
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
    r"(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){1,3}"
)

# Search engines, social networks, directories and technical hosts are not treated
# as candidate company websites in AI Company Research.
NON_COMPANY_HOSTS = {
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com",
    "twitter.com", "x.com", "yelp.com", "yellowpages.com", "bbb.org",
    "mapquest.com", "google.com", "bing.com", "duckduckgo.com",
    "crunchbase.com", "zoominfo.com", "apollo.io", "wikipedia.org",
    "reddit.com", "pinterest.com", "tiktok.com", "glassdoor.com",
    "indeed.com", "glassdoor.co.uk", "wixsite.com", "wordpress.com",
}


# ============================================================
# URL / NETWORK SAFETY
# ============================================================
def normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise ValueError("Empty URL")
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid URL: {raw_url}")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not accepted.")
    if not parsed.path:
        parsed = parsed._replace(path="/")
    return urlunsplit(parsed)


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
