                client = get_gemini_client()
                if client is None:
                    st.error("GEMINI_API_KEY is missing in Streamlit Cloud → App settings → Secrets.")
                    return
                with st.spinner("Gemini is understanding the client instructions..."):
                    plan = analyze_client_brief(
                        client,
                        model.strip() or DEFAULT_GEMINI_MODEL,
                        instructions,
                        location,
                    )
                queries = [str(q).strip() for q in plan.get("search_queries", []) if str(q).strip()]
                if location and not any(location.lower() in q.lower() for q in queries):
                    queries = [f"{q} {location}" for q in queries]
                if not queries:
                    queries = direct_search_queries(instructions, location)
                st.session_state["last_brief_plan"] = plan
            except Exception as exc:
                show_gemini_error(exc)
                return
        else:
            queries = direct_search_queries(instructions, location)
            st.session_state["last_brief_plan"] = None

        st.write(f"**Search mode:** {mode}")
        if plan:
            with st.expander("Gemini's brief breakdown", expanded=True):
                st.write(plan.get("brief_summary", ""))
                st.write("**Industry / business type:**", plan.get("industry_or_business_type", []))
                st.write("**Location:**", plan.get("location", []))
                st.write("**Company criteria:**", plan.get("company_criteria", []))
                st.write("**Person / company signals:**", plan.get("decision_maker_or_company_signals", []))
                st.write("**Search queries:**", queries)

        if not DDGS_AVAILABLE:
            st.error(f"Python web search package is not available: {DDGS_IMPORT_ERROR}. Install ddgs in requirements.txt and redeploy.")
            return

        candidates, search_results, errors = collect_company_candidates(
            queries,
            count,
            location,
        )

        if not candidates:
            st.error("No company websites were returned by web search. Try adding a clearer industry, location or business type to the instructions.")
            if errors:
                with st.expander("Search errors"):
                    st.write(errors)
            return

        company_rows = []
        progress = st.progress(0, text="Opening company websites and collecting details...")
        status = st.empty()

        for index, candidate in enumerate(candidates, start=1):
            status.write(f"Researching company {index} of {len(candidates)}: {candidate['domain']}")
            progress.progress((index - 1) / len(candidates), text=f"Researching {candidate['domain']}")
            try:
                company_rows.append(research_one_company(candidate))
            except Exception as exc:
                company_rows.append({
                    "Company Name": candidate["domain"],
                    "Website": candidate["website"],
                    "Revenue": "Not publicly found",
                    "LinkedIn": "", "Facebook": "", "Instagram": "",
                    "X / Twitter": "", "Other Socials": "",
                    "Source Notes": candidate["website"],
                    "Search Evidence": candidate["search_result"].get("url", ""),
                    "Errors": str(exc),
                })
            time.sleep(0.15)

        progress.progress(1.0, text="Company research completed")
        status.empty()

        st.session_state["research_company_rows"] = company_rows
        st.session_state["research_search_errors"] = errors
        st.session_state["research_search_sources"] = search_results
        st.session_state["research_count_requested"] = count
        st.session_state["research_mode_used"] = mode

        if len(company_rows) < count:
            st.warning(
                f"Search found {len(company_rows)} suitable company websites out of {count} requested. "
                "The tool does not invent companies to fill the target. Try broader search wording if you need more."
            )

    rows = st.session_state.get("research_company_rows", [])
    if rows:
        st.subheader("Company research results — one company per row")
        display_company_results(rows, "company_research")
        errors = st.session_state.get("research_search_errors", [])
        if errors:
            with st.expander("Search warnings"):
                st.write(errors)


# ============================================================
# SIDEBAR AND APP ROUTER
# ============================================================

with st.sidebar:
    st.markdown("## 🔎 Research Suite")
    selected_tool = st.radio(
        "Choose a tool",
        ["Bulk Website Extractor", "AI Company Research"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("Bulk Website Extractor: Python only, no Gemini.")
    st.caption("AI Company Research: optional Gemini brief breakdown, Python extraction.")

if selected_tool == "Bulk Website Extractor":
    bulk_extractor_page()
else:
    ai_company_research_page()

st.divider()
st.caption(
    "Revenue is shown only when a public source provides a detectable figure; otherwise it is marked Not publicly found. "
    "Social links are collected from official website links or matching search results. "
    "This app accesses public pages only and does not bypass logins, CAPTCHAs or paywalls. "
    "Verify revenue and social profiles against their source before client delivery."
)
