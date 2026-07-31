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
            roughly "one candidate" instead of "the whole state."
            """
            nonlocal success_count
            ui_logger(f"    ↳ Categorizing: {record['metadata']['name']}")

            structured_content = categorize_candidate(
                candidate_data=record,
                log_func=ui_logger,
                api_key=user_api_key
            )

            # --- PRESERVE QA STATE ---
            qa_status = "Pending"
            qa_notes = ""

            # Check if this candidate already exists in Supabase
            existing = supabase.table("candidates").select("qa_status, qa_notes")\
                .eq("name", record['metadata']['name'])\
                .eq("office", record['metadata']['office'])\
                .eq("state", state)\
                .eq("election_year", int(year)).execute()

            # If they exist, carry over their previous QA work
            if existing.data:
                qa_status = existing.data[0].get("qa_status", "Pending")
                qa_notes = existing.data[0].get("qa_notes", "")
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
            st.write("🤖 Scraping, categorizing with Gemini, and saving each candidate as it completes...")
            run_scraper(
                state=state, 
                year=year, 
                target_parties=target_parties, 
                include_tables=include_tables, 
                log_func=ui_logger,
                on_candidate_scraped=on_candidate_scraped
            )

            status.update(label=f"✅ Pipeline Complete! Saved {success_count} candidates.", state="complete", expanded=False)
            
        st.success("Data successfully pushed to Supabase! Switch to the QA Dashboard to review.")

    # Always render the log container below the form if logs exist
    if st.session_state.crawler_logs:
        st.divider()
        st.subheader("📋 Execution Logs")
        with st.container(height=400): # Creates a scrollable container 400px high
            for log_msg in st.session_state.crawler_logs:
                st.text(log_msg)
