import streamlit as st
from src.ui import tab_qa, tab_crawler  # Flipped imports

st.set_page_config(page_title="Candidate Data Platform", layout="wide")

st.title("🗳️ WeVote Candidate Data Platform")

# Create the tab navigation with the QA Dashboard as Tab 1
tab1, tab2 = st.tabs(["🛠️ QA Dashboard", "🚀 Crawler & Categorizer"])

with tab1:
    tab_qa.render() # Renders first by default

with tab2:
    tab_crawler.render() # Renders second