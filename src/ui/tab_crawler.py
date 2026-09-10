import streamlit as st

# A helper list of all 50 states
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", 
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", 
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", 
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", 
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", 
    "New Hampshire", "New Jersey", "New Mexico", "New York", 
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", 
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", 
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", 
    "West Virginia", "Wisconsin", "Wyoming"
]

def render():
    # Initialize the log array in session state so it persists across reruns
    if "crawler_logs" not in st.session_state:
        st.session_state.crawler_logs = []

    st.header("🚀 Candidate Data Crawler")
    st.markdown("Configure the parameters below to initiate the unified data harvest.")

    with st.form("crawler_config_form"):
        # --- NEW INSTRUCTIONS EXPANDER ---
        with st.expander("ℹ️ How to get a free Gemini API Key"):
            st.markdown("""
            1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey) and sign in with your Google account.
            2. Click the **Create API key** button.
            3. Choose an existing project or create a new one, then click **Create API key**.
            4. Copy the generated string and paste it into the field below.
            """)
            
        # --- API KEY INPUT ---
        user_api_key = st.text_input(
            "🔑 Gemini API Key", 
            type="password", 
            label_visibility="collapsed", # Hides the redundant label since the placeholder and expander explain it
            placeholder="Paste your Google Gemini API key here...",
            help="Your key is kept securely in your active browser session and is never saved to the database."
        )
        st.divider()
        
        col1, col2 = st.columns(2)
                
        with col1:
            st.subheader("Target Election")
            state = st.selectbox(
                "State", 
                options=US_STATES, 
                index=None, 
                placeholder="Type or select a state..."
            )
            year = st.text_input("Year", value="2026")
            
        with col2:
            st.subheader("Filters")
            st.write("**Target Parties**")
            p_col1, p_col2, p_col3 = st.columns(3)
            inc_rep = p_col1.checkbox("Republican", value=True)
            inc_dem = p_col2.checkbox("Democratic", value=False)
            inc_ind = p_col3.checkbox("Independent", value=False)
            
            st.write("**Office Levels**")
            o_col1, o_col2, o_col3 = st.columns(3)
            fed_cand = o_col1.checkbox("Federal", value=True)
            state_cand = o_col2.checkbox("State", value=True)
            local_cand = o_col3.checkbox("Local", value=True)

            st.write("**Re-scrape Behavior**")
            force_rescrape = st.checkbox(
                "Re-scrape candidates already saved",
                value=False,
                help=(
                    "Off (default): candidates already saved with good data are skipped, so you can "
                    "click Start Harvest again to resume a batch that ran out of time. Candidates that "
                    "previously failed or came back empty are always retried either way.\n\n"
                    "On: re-scrapes everyone from scratch to refresh their data. Existing QA status "
                    "and auditor notes are preserved."
                )
            )

        submitted = st.form_submit_button("Start Harvest", type="primary", use_container_width=True)

    if submitted:
        # --- NEW VALIDATION ---
        if not user_api_key:
            st.error("Please provide a valid Gemini API key to proceed.")
            return
            
        if not state:
            st.error("Please select a target state before starting the harvest.")
            return
            
        # Clear the logs for a fresh run
        st.session_state.crawler_logs = []

        from src.core.scraper import run_scraper
        from src.core.ai_agent import categorize_candidate
        from src.data.db import init_connection
        
        target_parties = []
        if inc_rep: target_parties.append("Republican")
        if inc_dem: target_parties.append("Democratic")
        if inc_ind: target_parties.append("Independent")
        
        include_tables = []
        if fed_cand: include_tables.append("Federal Candidates")
        if state_cand: include_tables.append("State Candidates")
        if local_cand: include_tables.append("Local Candidates")
        
        if not target_parties:
            st.error("Please select at least one target party.")
            return
        if not include_tables:
            st.error("Please select at least one office level.")
            return

        supabase = init_connection()

        # --- RESUME / RE-SCRAPE SUPPORT ---
        # Pull everything already saved for this state/year in one query so we
        # can decide per candidate whether to skip. A candidate is only skipped
        # if it was saved with genuinely usable content -- records that failed
        # categorization (structured_content contains "ERROR") or came back
        # empty are always retried, so a bad run doesn't strand them forever.
        existing_rows = supabase.table("candidates")\
            .select("name, office, qa_status, qa_notes, structured_content")\
            .eq("state", state)\
            .eq("election_year", int(year)).execute()

        existing_lookup = {}
        for row in (existing_rows.data or []):
            sc = row.get("structured_content") or {}
            existing_lookup[(row["name"], row["office"])] = {
                "qa_status": row.get("qa_status", "Pending"),
                "qa_notes": row.get("qa_notes", ""),
                # Store only the verdict, not the content itself -- the full
                # verbatim text for a whole state would be a lot to hold onto
                # for the entire run when all we need is a boolean.
                "has_usable_content": isinstance(sc, dict) and bool(sc) and "ERROR" not in sc,
            }

        def is_already_processed(cand):
            if force_rescrape:
                return False
            row = existing_lookup.get((cand['name'], cand['office']))
            if not row:
                return False
            return row["has_usable_content"]
        # -----------------------------------

        # Define a custom logging function that updates both the UI and the persistent state
        def ui_logger(msg):
            st.session_state.crawler_logs.append(msg)
            st.write(msg) # Outputs inside the status container during the run

        # Tracks successful saves across the run. A plain int can't be
        # rebound from inside the nested callback below without `nonlocal`.
        success_count = 0

        def on_candidate_scraped(record):
            """
            Called immediately after each candidate is scraped (before the
            next one starts). Categorizes and upserts right away so a
            candidate's full scraped text never has to sit around waiting
            for the rest of the batch to finish -- keeping peak memory to
            roughly "one candidate" instead of "the whole state," and
            ensuring progress is saved even if the run gets cut off later.
            """
            nonlocal success_count
            ui_logger(f"    ↳ Categorizing: {record['metadata']['name']}")

            structured_content = categorize_candidate(
                candidate_data=record,
                log_func=ui_logger,
                api_key=user_api_key
            )

            # --- PRESERVE QA STATE ---
            # This fires whenever a candidate reaches the callback despite
            # already having a row: either a previously failed/empty record
            # being retried, or a force re-scrape. In both cases we must carry
            # forward the auditor's existing work rather than reset it.
            existing = existing_lookup.get((record['metadata']['name'], record['metadata']['office']))
            qa_status = existing.get("qa_status", "Pending") if existing else "Pending"
            qa_notes = existing.get("qa_notes", "") if existing else ""
            # -------------------------

            db_payload = {
                "name": record['metadata']['name'],
                "office": record['metadata']['office'],
                "state": state,
                "election_year": int(year),
                "party": record['metadata'].get('party', 'Unknown'),
                "metadata": record['metadata'],
                "structured_content": structured_content,
                "qa_status": qa_status,  # Uses the preserved state
                "qa_notes": qa_notes     # Uses the preserved notes
            }

            try:
                supabase.table("candidates").upsert(db_payload, on_conflict="name, office, state, election_year").execute()
                success_count += 1
            except Exception as e:
                ui_logger(f"    ❌ Database error for {record['metadata']['name']}: {e}")

        with st.status(f"Executing Scraper Pipeline for {state}...", expanded=True) as status:
            # Surface what this run will actually do -- otherwise a run that
            # legitimately skips most of the roster just looks broken.
            saved_ok = sum(1 for r in existing_lookup.values() if r["has_usable_content"])
            needs_retry = len(existing_lookup) - saved_ok

            if force_rescrape:
                ui_logger(f"🔁 Force re-scrape ON: re-processing all matching candidates. "
                          f"({len(existing_lookup)} already in DB; QA status/notes will be preserved.)")
            else:
                ui_logger(f"↩️ Resume mode: skipping {saved_ok} candidate(s) already saved with good data. "
                          f"{needs_retry} previously failed/empty record(s) will be retried.")

            ui_logger("🤖 Scraping, categorizing with Gemini, and saving each candidate as it completes...")
            run_scraper(
                state=state, 
                year=year, 
                target_parties=target_parties, 
                include_tables=include_tables, 
                log_func=ui_logger,
                on_candidate_scraped=on_candidate_scraped,
                is_already_processed=is_already_processed,
            )

            status.update(label=f"✅ Pipeline Complete! Saved {success_count} candidates this run.", state="complete", expanded=False)
            
        if success_count:
            st.success(f"Saved {success_count} candidate(s) to Supabase! Switch to the QA Dashboard to review.")
        else:
            st.info(
                "No new candidates were saved. Everything matching your filters is already in the "
                "database with good data. Tick **Re-scrape candidates already saved** if you want to "
                "refresh their data."
            )

    # Always render the log container below the form if logs exist
    if st.session_state.crawler_logs:
        st.divider()
        st.subheader("📋 Execution Logs")
        with st.container(height=400): # Creates a scrollable container 400px high
            for log_msg in st.session_state.crawler_logs:
                st.text(log_msg)
