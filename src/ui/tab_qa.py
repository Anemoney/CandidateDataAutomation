import streamlit as st
from src.data.db import init_connection

def render():
    st.header("🛠️ QA Dashboard")
    st.markdown("Review and refine extracted candidate data before finalizing.")

    # Initialize persistent auditor name in session state
    if "auditor_name" not in st.session_state:
        st.session_state.auditor_name = ""

    # Persistent name input at the very top
    col_name, _ = st.columns([1, 2])
    with col_name:
        st.session_state.auditor_name = st.text_input(
            "👤 Your Name (Auditor)", 
            value=st.session_state.auditor_name,
            placeholder="Enter your name here.",
            help="Your name persists during this active session so you only have to enter it once."
        ).strip()

    supabase = init_connection()

    # 1. Fetch data with a brief cache
    @st.cache_data(ttl=10) 
    def get_candidates():
        response = supabase.table("candidates").select("*").order("qa_status", desc=True).execute()
        return response.data

    all_candidates = get_candidates()

    if not all_candidates:
        st.info("No candidates found in the database. Run the crawler first!")
        return

    # 2. Dynamic Filters
    st.markdown("### 🔍 Filters")
    f_col1, f_col2 = st.columns(2)
    
    unique_states = sorted(list(set(c.get("state", "Unknown") for c in all_candidates)))
    unique_parties = sorted(list(set(c.get("party", "Unknown") for c in all_candidates)))
    
    with f_col1:
        selected_state = st.selectbox("State Filter", ["All States"] + unique_states)
    with f_col2:
        selected_party = st.selectbox("Party Filter", ["All Parties"] + unique_parties)

    # 3. Apply Filters In-Memory
    filtered_candidates = [
        c for c in all_candidates 
        if (selected_state == "All States" or c.get("state") == selected_state)
        and (selected_party == "All Parties" or c.get("party") == selected_party)
    ]

    if not filtered_candidates:
        st.warning("No candidates match your current filter criteria.")
        return

    st.divider()

    # 4. Build the Selection Dropdown
    options = [
        f"{'✅' if c['qa_status'] == 'Reviewed' else '❌' if c['qa_status'] == 'Flagged' else '⏳'} "
        f"{c['name']} - {c['office']} ({c.get('party', 'Unknown')} - {c['state']})" 
        for c in filtered_candidates
    ]
    
    selected_option = st.selectbox("Select a candidate to review:", options)
    selected_index = options.index(selected_option)
    candidate = filtered_candidates[selected_index]
    
    meta = candidate.get("metadata", {})
    content = candidate.get("structured_content", {})
    contacts = meta.get("extracted_contacts", {})
    socials = meta.get("socials", {})

    # 5. Build the Editable UI Interface
    with st.container(border=True):
        st.subheader(f"Editing: {candidate['name']}")
        st.write(f"**Office:** {candidate['office']} | **Party:** {candidate.get('party', 'Unknown')}")
        
        # Display previous auditor if they exist
        previous_auditor = candidate.get("qa_auditor", "")
        if previous_auditor:
            st.info(f"Last QA completed by: **{previous_auditor}**")
        
        with st.form("qa_editor_form"):
            col1, col2 = st.columns([2, 1])
            
            with col1:
                st.markdown("### 📞 Contact & Socials")
                edit_email = st.text_input("Email", value=contacts.get("email", ""))
                edit_phone = st.text_input("Phone", value=contacts.get("phone", ""))
                edit_address = st.text_input("Address", value=contacts.get("address", ""))
                
                edit_bp = st.text_input("Ballotpedia URL", value=meta.get("ballotpedia_url", ""))
                edit_website = st.text_input("Campaign Website", value=socials.get("campaign_website", ""))
                edit_fb = st.text_input("Facebook", value=socials.get("facebook", ""))
                edit_x = st.text_input("X (Twitter)", value=socials.get("x", ""))
                
                st.markdown("<h2 style='color: #1e3a8a; border-bottom: 2px solid #e2e8f0; padding-bottom: 4px; margin-top: 15px; margin-bottom: 10px; font-size: 1.5rem;'>📝 Structured Content</h2>", unsafe_allow_html=True)
                
                schema_categories = [
                    "General Philosophy", "Personal and Family", "Professional Experience",
                    "Civic Involvement", "Political Experience", "Religious Affiliation",
                    "Accomplishments and Awards", "Educational Background", "Military Service",
                    "Why I Am Running for Public Office", "Goals If Elected", "Areas to Concentrate On"
                ]
                
                edit_content = {}
                for cat in schema_categories:
                    cat_data = content.get(cat, {})
                    if cat_data:
                        st.markdown(f"<h3 style='color: #2563eb; margin: 5px 0px; font-size: 1.1rem;'>🔍 {cat}</h3>", unsafe_allow_html=True)
                        
                        edit_text = st.text_area(f"Verbatim Extraction ({cat})", label_visibility="collapsed", value=cat_data.get("text", ""), height=100, key=f"text_{cat}")
                        edit_url = st.text_input(f"Source URL ({cat})", label_visibility="collapsed", value=cat_data.get("source_url", ""), key=f"url_{cat}")
                        
                        edit_content[cat] = {"text": edit_text, "source_url": edit_url}
                        st.markdown("<hr style='margin: 10px 0; border: 0; border-top: 1px dashed #cbd5e1;'>", unsafe_allow_html=True)

            with col2:
                st.markdown("### ⚙️ QA Status")
                qa_notes = st.text_area("Auditor Notes", value=candidate.get("qa_notes", ""), height=150)
                
                st.write("**Save Actions**")
                
                # Check if they have put their name in before letting them submit
                has_name = bool(st.session_state.auditor_name)
                
                mark_reviewed = st.form_submit_button("✅ Save & Mark Reviewed", use_container_width=True, disabled=not has_name)
                flag_issue = st.form_submit_button("❌ Flag Issue", use_container_width=True, disabled=not has_name)
                save_draft = st.form_submit_button("💾 Save Draft (Pending)", use_container_width=True, disabled=not has_name)
                
                if not has_name:
                    st.warning("⚠️ Please enter your name at the top of the dashboard to unlock save actions.")
                
        # 6. Handle Database Updates
        if mark_reviewed or flag_issue or save_draft:
            new_status = "Reviewed" if mark_reviewed else "Flagged" if flag_issue else "Pending"
            
            meta["extracted_contacts"] = {"email": edit_email, "phone": edit_phone, "address": edit_address}
            meta["ballotpedia_url"] = edit_bp
            meta["socials"] = {"campaign_website": edit_website, "facebook": edit_fb, "x": edit_x} 
            
            for cat, data in edit_content.items():
                content[cat] = data
                
            try:
                supabase.table("candidates").update({
                    "metadata": meta,
                    "structured_content": content,
                    "qa_status": new_status,
                    "qa_notes": qa_notes,
                    "qa_auditor": st.session_state.auditor_name # <--- NEW: Pushes the persistent session name
                }).eq("id", candidate["id"]).execute()
                
                st.success(f"Successfully updated {candidate['name']} to '{new_status}'!")
                get_candidates.clear() 
                st.rerun() 
            except Exception as e:
                st.error(f"Error updating database: {e}")