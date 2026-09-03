import streamlit as st
import streamlit.components.v1 as components
from src.ui import tab_qa, tab_crawler  # Flipped imports

st.set_page_config(page_title="Candidate Data Platform", layout="wide")

components.html("""
<script>
(function() {
    const RELOAD_FLAG = "streamlit_chunk_reload_attempted";

    function handleChunkFailure(message) {
        if (message && message.includes("Failed to fetch dynamically imported module")) {
            // Avoid an infinite reload loop if something is genuinely broken server-side
            if (!sessionStorage.getItem(RELOAD_FLAG)) {
                sessionStorage.setItem(RELOAD_FLAG, "1");
                window.top.location.reload();
            }
        }
    }

    window.addEventListener("error", (e) => handleChunkFailure(e.message));
    window.addEventListener("unhandledrejection", (e) => handleChunkFailure(String(e.reason)));
})();
</script>
""", height=0, width=0)

st.title("🗳️ WeVote Candidate Data Platform")

# Create the tab navigation with the QA Dashboard as Tab 1
tab1, tab2 = st.tabs(["🛠️ QA Dashboard", "🚀 Crawler & Categorizer"])

with tab1:
    tab_qa.render() # Renders first by default

with tab2:
    tab_crawler.render() # Renders second
