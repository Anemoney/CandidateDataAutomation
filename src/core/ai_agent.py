import json
import re
import time
from collections import deque
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from google import genai
from google.genai import types
import streamlit as st

# ── RATE LIMITER METRONOME ──
class LocalRateLimiter:
    """Tracks RPM and TPM, enforcing a steady metronome pace."""
    def __init__(self, max_rpm=14, max_tpm=200000):
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self.request_timestamps = deque()
        self.token_timestamps = deque()
        self.min_gap_seconds = 60.0 / self.max_rpm
        self.last_request_time = 0

    def wait_if_needed(self, estimated_tokens, log_func):
        now = time.time()
        time_since_last = now - self.last_request_time
        if time_since_last < self.min_gap_seconds:
            sleep_time = self.min_gap_seconds - time_since_last
            log_func(f"    🚦 Pacing API request. Sleeping {sleep_time:.1f}s...")
            time.sleep(sleep_time)
            now = time.time()

        while self.request_timestamps and now - self.request_timestamps[0] > 60:
            self.request_timestamps.popleft()
        while self.token_timestamps and now - self.token_timestamps[0][0] > 60:
            self.token_timestamps.popleft()

        current_tpm = sum(count for _, count in self.token_timestamps)
        if current_tpm + estimated_tokens > self.max_tpm:
            log_func(f"    🚦 TPM limit risk ({current_tpm:,}). Throttling 30s...")
            time.sleep(30)
            return self.wait_if_needed(estimated_tokens, log_func)

        self.request_timestamps.append(now)
        self.token_timestamps.append([now, estimated_tokens])
        self.last_request_time = now

# Initialize globally to maintain state across different candidate calls
rate_limiter = LocalRateLimiter(max_rpm=14, max_tpm=200000)

# ── STRUCTURING SCHEMAS ──
class ContentBlock(BaseModel):
    text: str = Field(description="The VERBATIM extracted passage from the source text. Do not paraphrase or summarize.")
    source_url: Optional[str] = Field(default="Unknown Source", description="The exact URL of the specific page this text came from.")

class CandidateCategories(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    general_philosophy: Optional[ContentBlock] = Field(default=None, alias="General Philosophy")
    personal_and_family: Optional[ContentBlock] = Field(default=None, alias="Personal and Family")
    professional_experience: Optional[ContentBlock] = Field(default=None, alias="Professional Experience")
    civic_involvement: Optional[ContentBlock] = Field(default=None, alias="Civic Involvement")
    political_experience: Optional[ContentBlock] = Field(default=None, alias="Political Experience")
    religious_affiliation: Optional[ContentBlock] = Field(default=None, alias="Religious Affiliation")
    accomplishments_and_awards: Optional[ContentBlock] = Field(default=None, alias="Accomplishments and Awards")
    educational_background: Optional[ContentBlock] = Field(default=None, alias="Educational Background")
    military_service: Optional[ContentBlock] = Field(default=None, alias="Military Service")
    why_running: Optional[ContentBlock] = Field(default=None, alias="Why I Am Running for Public Office")
    goals_if_elected: Optional[ContentBlock] = Field(default=None, alias="Goals If Elected")
    areas_to_concentrate: Optional[ContentBlock] = Field(default=None, alias="Areas to Concentrate On")

# ── HELPER UTILITIES ──
def sanitize_text(raw_data):
    if not raw_data: return ""
    if isinstance(raw_data, list): raw_data = " ".join([str(item) for item in raw_data])
    return re.sub(r'\s+', ' ', str(raw_data)).strip()

def extract_array_text(data_array):
    if not data_array: return ""
    if isinstance(data_array, list):
        return " ".join([item.get("text", "") for item in data_array if isinstance(item, dict)])
    return str(data_array)

def get_estimated_total_tokens(prompt_payload: str, expected_output_tokens: int = 1500) -> int:
    return int(len(prompt_payload) / 2.5) + expected_output_tokens

# ── SYSTEM PROMPTS ──
SYSTEM_INSTRUCTION = """
You are a political data classification engineer. Your job is to extract verbatim text passages from the provided candidate sources and classify them into the correct categories.

CRITICAL RULES:
1. Verbatim Extraction Only: Copy exact word-for-word text from the source. Never summarize, paraphrase, or alter the original text.
2. The "Null" Rule: If the provided text does not contain relevant information for a specific category, you MUST leave that category null/empty. Do not force-fit text.
3. Single Source per Category: Identify the single best source page for a category and pull all relevant content from it. Do not stitch together quotes from different URLs into the same category block.
4. Sourcing: Look at the "=== SOURCE: [URL] ===" header immediately above the text you are extracting. You MUST use that exact string as the `source_url`.
5. Mutual Exclusivity: Each passage belongs to exactly one category. Do not place the same quote in multiple categories. Pick the best fit.
"""

# ── RUN PROCESS ──
def categorize_candidate(candidate_data: dict, log_func, api_key: str) -> dict: # <--- Added api_key parameter
    client = genai.Client(api_key=api_key)
    
    bp_url = candidate_data['metadata'].get('ballotpedia_url', 'Unknown Source')
    bp_bio = sanitize_text(extract_array_text(candidate_data['metadata'].get('biography', [])))
    bp_themes = sanitize_text(extract_array_text(candidate_data['metadata'].get('campaign_themes', [])))

    camp_web_blocks = []
    for page in candidate_data.get('campaign_website_text', []):
        if isinstance(page, dict) and 'text' in page and 'url' in page:
            camp_web_blocks.append(f"=== SOURCE: {page['url']} ===\n{sanitize_text(page['text'])}")

    camp_web_formatted = "\n\n".join(camp_web_blocks)

    prompt_payload = f"""
    Candidate Name: {candidate_data['metadata']['name']}
    Office: {candidate_data['metadata']['office']}

    === SOURCE: {bp_url} ===
    [BIOGRAPHY]
    {bp_bio}

    [CAMPAIGN THEMES]
    {bp_themes}

    [CAMPAIGN WEBSITE TEXT]
    {camp_web_formatted}
    """

    token_weight = get_estimated_total_tokens(prompt_payload)
    log_func(f"    ↳ Payload token estimation: ~{token_weight:,} tokens")

    # Call metronome
    rate_limiter.wait_if_needed(token_weight, log_func)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type='application/json',
        response_schema=CandidateCategories,
        temperature=0.0
    )

    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt_payload,
            config=config
        )

        if response.parsed is not None:
            return response.parsed.model_dump(by_alias=True, exclude_none=True)
        else:
            json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if json_match:
                return {k: v for k, v in json.loads(json_match.group(0)).items() if v is not None}
            return {}
    except Exception as e:
        log_func(f"    ❌ Gemini Pipeline Error: {str(e)}")
        return {"ERROR": str(e)}
