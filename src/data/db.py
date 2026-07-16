import os
import streamlit as st
from supabase import create_client, Client

@st.cache_resource
def init_connection() -> Client:
    # Checks Hugging Face environment variables first, falls back to local secrets.toml
    url = os.environ.get("SUPABASE_URL") or st.secrets["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_KEY") or st.secrets["SUPABASE_KEY"]
    
    return create_client(url, key)