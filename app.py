import base64
import json
import os
import re
from datetime import date

import gspread
import streamlit as st
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from serpapi import GoogleSearch

# ── Config ───────────────────────────────────────────────────────────────────

load_dotenv()


def get_secret(key, default=""):
    """Read from Streamlit secrets (cloud) or .env (local)."""
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        return os.getenv(key, default)


SERPAPI_KEY = get_secret("SERPAPI_KEY")
SHEET_ID = get_secret("GOOGLE_SHEET_ID")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── SerpAPI Search ───────────────────────────────────────────────────────────


def search_linkedin(titles, location, industry, seniority, keywords, exclude, max_results=100):
    """Search for multiple job titles via SerpAPI, combine and deduplicate."""
    title_list = [t.strip() for t in titles.strip().splitlines() if t.strip()]
    if not title_list:
        return [], ""

    # Build exclude terms once
    exclude_parts = []
    if exclude:
        for ex in exclude.split(","):
            ex = ex.strip()
            if ex:
                exclude_parts.append(f'-"{ex}"')

    results = []
    seen_urls = set()
    all_queries = []
    per_title_limit = max(10, max_results // len(title_list))

    for title in title_list:
        if len(results) >= max_results:
            break

        parts = ["site:linkedin.com/in", f'"{title}"']
        for term in [location, industry, seniority, keywords]:
            if term:
                parts.append(f'"{term}"')
        parts.extend(exclude_parts)

        query = " ".join(parts)
        all_queries.append(query)

        for start in range(0, per_title_limit, 10):
            if len(results) >= max_results:
                break
            params = {
                "engine": "google",
                "q": query,
                "api_key": SERPAPI_KEY,
                "num": 10,
                "start": start,
            }
            search = GoogleSearch(params)
            data = search.get_dict()

            items = data.get("organic_results", [])
            if not items:
                break

            for item in items:
                parsed = _parse_result(item)
                if parsed and parsed["LinkedIn URL"] not in seen_urls:
                    seen_urls.add(parsed["LinkedIn URL"])
                    results.append(parsed)

    combined_query = " | ".join(title_list)
    return results[:max_results], combined_query


def _parse_result(item):
    """Extract name, title, company, and URL from a search result."""
    url = item.get("link", "")
    if "/in/" not in url:
        return None

    raw_title = item.get("title", "")
    # LinkedIn titles are typically "Name - Title - Company | LinkedIn"
    raw_title = raw_title.replace(" | LinkedIn", "").strip()
    segments = [s.strip() for s in re.split(r"\s[–—-]\s", raw_title)]

    name = segments[0] if segments else ""
    title = segments[1] if len(segments) > 1 else ""
    company = segments[2] if len(segments) > 2 else ""

    return {
        "Name": name,
        "Title": title,
        "Company": company,
        "LinkedIn URL": url,
    }


# ── Google Sheets Export ─────────────────────────────────────────────────────


def _get_gsheet_creds():
    """Load Google credentials from base64 secret (cloud) or file (local)."""
    if "GCP_CREDENTIALS_B64" in st.secrets:
        decoded = base64.b64decode(st.secrets["GCP_CREDENTIALS_B64"]).decode()
        info = json.loads(decoded)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)


def export_to_sheet(rows, query):
    """Append rows to a Google Sheet, skipping duplicates by LinkedIn URL."""
    creds = _get_gsheet_creds()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1

    # Get all data and compact it (remove blank rows)
    all_values = ws.get_all_values()
    header = ["Name", "Title", "Company", "LinkedIn URL", "Search Query", "Date"]

    # Filter out blank rows, keep header + data rows only
    data_rows = []
    existing_urls = set()
    url_col = 3
    for row in all_values:
        if any(cell.strip() for cell in row):
            data_rows.append(row)
            if len(row) > url_col and row[url_col].strip():
                existing_urls.add(row[url_col])

    # If no data at all, start with header
    if not data_rows:
        data_rows = [header]

    # Build new rows to add
    new_rows = []
    today = date.today().isoformat()
    for r in rows:
        if r["LinkedIn URL"] not in existing_urls:
            new_rows.append([
                r["Name"], r["Title"], r["Company"],
                r["LinkedIn URL"], query, today,
            ])

    if new_rows:
        # Rewrite the entire sheet: compact data + new rows, no blanks
        all_data = data_rows + new_rows
        needed_rows = len(all_data)
        if needed_rows > ws.row_count:
            ws.add_rows(needed_rows - ws.row_count)
        ws.clear()
        ws.update("A1", all_data, value_input_option="USER_ENTERED")
        # Trim extra blank rows if sheet has too many
        if ws.row_count > needed_rows + 10:
            ws.resize(rows=needed_rows + 10)

    return len(new_rows)


# ── Streamlit UI ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="LinkedIn Profile Finder", layout="wide")
st.title("LinkedIn Profile Finder")

# Session state for saved searches and results
if "saved_searches" not in st.session_state:
    st.session_state.saved_searches = {}
if "results" not in st.session_state:
    st.session_state.results = []
if "last_query" not in st.session_state:
    st.session_state.last_query = ""

# ── Sidebar: Saved Searches ─────────────────────────────────────────────────

with st.sidebar:
    st.header("Saved Searches")
    names = list(st.session_state.saved_searches.keys())
    if names:
        selected = st.selectbox("Load a saved search", [""] + names)
        if st.button("Load") and selected:
            st.session_state.update(st.session_state.saved_searches[selected])
            st.rerun()
    else:
        st.info("No saved searches yet.")

# ── Main: Search Form ───────────────────────────────────────────────────────

col1, col2 = st.columns(2)
with col1:
    job_title = st.text_area("Job Titles (one per line)", value=st.session_state.get("job_title", ""),
                             height=120, placeholder="Head of Revenue Operations\nDirector of Revenue Operations\nVP Revenue Operations")
    location = st.text_input("Location", value=st.session_state.get("location", ""))
    industry = st.text_input("Industry", value=st.session_state.get("industry", ""))
with col2:
    seniority = st.selectbox(
        "Seniority",
        ["", "Intern", "Junior", "Mid-level", "Senior", "Lead", "Director", "VP", "C-level"],
        index=0,
    )
    keywords = st.text_input("Additional Keywords", value=st.session_state.get("keywords", ""))
    exclude = st.text_input("Exclude (comma-separated)", value=st.session_state.get("exclude", ""))
    max_results = st.slider("Max results", min_value=10, max_value=100, value=50, step=10,
                            help="Each 10 results uses 1 SerpAPI credit")

# ── Search ───────────────────────────────────────────────────────────────────

if st.button("Search", type="primary"):
    if not SERPAPI_KEY:
        st.error("Set SERPAPI_KEY in your .env file. Get a free key at serpapi.com")
    elif not job_title:
        st.warning("Please enter at least a Job Title.")
    else:
        with st.spinner("Searching…"):
            try:
                results, query = search_linkedin(
                    job_title, location, industry, seniority, keywords, exclude,
                    max_results=max_results,
                )
                st.session_state.results = results
                st.session_state.last_query = query
            except Exception as e:
                st.error(f"Search failed: {e}")

# ── Results ──────────────────────────────────────────────────────────────────

if st.session_state.results:
    st.subheader(f"Results ({len(st.session_state.results)})")
    st.dataframe(
        st.session_state.results,
        column_config={"LinkedIn URL": st.column_config.LinkColumn()},
        use_container_width=True,
    )

    # Export
    if st.button("Export to Google Sheet"):
        if not SHEET_ID:
            st.error("Set GOOGLE_SHEET_ID in your .env file or Streamlit secrets.")
        else:
            with st.spinner("Exporting…"):
                try:
                    added = export_to_sheet(
                        st.session_state.results, st.session_state.last_query
                    )
                    st.success(f"Exported {added} new profile(s) to Google Sheets.")
                except Exception as e:
                    st.error(f"Export failed: {e}")

    # Save search
    with st.expander("Save This Search"):
        save_name = st.text_input("Search name")
        if st.button("Save") and save_name:
            st.session_state.saved_searches[save_name] = {
                "job_title": job_title,
                "location": location,
                "industry": industry,
                "keywords": keywords,
                "exclude": exclude,
            }
            st.success(f"Saved search '{save_name}'.")
elif st.session_state.last_query:
    st.info("No results found. Try broader search terms.")
