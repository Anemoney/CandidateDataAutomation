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

    def wait_if_needed(self, estimated_tokens, log_func, max_throttle_rounds=10):
        now = time.time()
        time_since_last = now - self.last_request_time
        if time_since_last < self.min_gap_seconds:
            sleep_time = self.min_gap_seconds - time_since_last
            log_func(f"    🚦 Pacing API request. Sleeping {sleep_time:.1f}s...")
            time.sleep(sleep_time)
            now = time.time()

        # A single payload larger than the entire per-minute budget can never
        # fit, no matter how long we wait. Waiting on it would loop forever,
        # so clamp it and let the API be the judge instead of hanging here.
        if estimated_tokens >= self.max_tpm:
            log_func(
                f"    ⚠️ Estimated payload ({estimated_tokens:,} tokens) meets or exceeds the "
                f"{self.max_tpm:,} TPM budget on its own. Proceeding without waiting -- the "
                f"request may be rejected for size."
            )
            estimated_tokens = self.max_tpm - 1

        # Bounded retry rather than recursion: if the window still hasn't
        # cleared after this many rounds, proceed and let the API respond
        # rather than stalling the whole pipeline indefinitely.
        for attempt in range(max_throttle_rounds):
            now = time.time()
            while self.request_timestamps and now - self.request_timestamps[0] > 60:
                self.request_timestamps.popleft()
            while self.token_timestamps and now - self.token_timestamps[0][0] > 60:
                self.token_timestamps.popleft()

            current_tpm = sum(count for _, count in self.token_timestamps)
            if current_tpm + estimated_tokens <= self.max_tpm:
                break

            log_func(
                f"    🚦 TPM limit risk ({current_tpm:,}). Throttling 30s "
                f"[{attempt + 1}/{max_throttle_rounds}]..."
            )
            time.sleep(30)
        else:
            log_func(
                f"    ⚠️ TPM window did not clear after {max_throttle_rounds} rounds. "
                f"Proceeding anyway."
            )

        now = time.time()
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
    general_philosophy: Optional[ContentBlock] = Field(
        default=None, alias="General Philosophy",
        description="Core values, worldview, and guiding principles stated in general terms. "
                    "NOT specific policy proposals and NOT their reason for running."
    )
    personal_and_family: Optional[ContentBlock] = Field(
        default=None, alias="Personal and Family",
        description="Family, marriage, children, upbringing, hometown, and where they currently live."
    )
    professional_experience: Optional[ContentBlock] = Field(
        default=None, alias="Professional Experience",
        description="Paid non-political work: career, jobs, business ownership, professional practice. "
                    "Excludes elected or appointed government roles."
    )
    civic_involvement: Optional[ContentBlock] = Field(
        default=None, alias="Civic Involvement",
        description="Unpaid, non-elected community service: volunteering, nonprofits, church or "
                    "service organizations, coaching, community boards."
    )
    political_experience: Optional[ContentBlock] = Field(
        default=None, alias="Political Experience",
        description="Elected or appointed government office held, prior campaigns, party positions, "
                    "and government service. Use this rather than Professional or Civic for "
                    "anything governmental."
    )
    religious_affiliation: Optional[ContentBlock] = Field(
        default=None, alias="Religious Affiliation",
        description="Stated faith, denomination, or congregation membership."
    )
    accomplishments_and_awards: Optional[ContentBlock] = Field(
        default=None, alias="Accomplishments and Awards",
        description="Honors, recognitions, and specific achievements already completed. "
                    "Past results, not future intentions."
    )
    educational_background: Optional[ContentBlock] = Field(
        default=None, alias="Educational Background",
        description="Schools attended, degrees, certifications, and formal training."
    )
    military_service: Optional[ContentBlock] = Field(
        default=None, alias="Military Service",
        description="Branch, rank, deployments, and veteran status."
    )
    why_running: Optional[ContentBlock] = Field(
        default=None, alias="Why I Am Running for Public Office",
        description="The candidate's stated MOTIVATION for seeking this office -- the cause or "
                    "problem that prompted them to run. Typically phrased as 'I am running "
                    "because...'. NOT what they intend to do once elected."
    )
    goals_if_elected: Optional[ContentBlock] = Field(
        default=None, alias="Goals If Elected",
        description="Specific OUTCOMES or ACTIONS they intend to accomplish in office. "
                    "Typically phrased as 'I will...'. Concrete results, distinct from the broad "
                    "topic areas they plan to focus on."
    )
    areas_to_concentrate: Optional[ContentBlock] = Field(
        default=None, alias="Areas to Concentrate On",
        description="The policy TOPICS or issue domains they plan to prioritize -- e.g. education, "
                    "taxes, public safety, infrastructure. A statement of which issues matter to "
                    "them, as opposed to the specific outcomes they promise (Goals If Elected)."
    )

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

# Hard ceiling on response length. Twelve categories at the ~150-word limit
# in the system instruction lands around 3k tokens, so this is comfortable
# headroom while still bounding a runaway response. Kept out of the TPM
# estimate below on purpose: budgeting the full ceiling for every request
# would trigger constant throttling for output that never actually arrives.
MAX_OUTPUT_TOKENS = 8192

def get_estimated_total_tokens(prompt_payload: str, expected_output_tokens: int = 2500) -> int:
    return int(len(prompt_payload) / 2.5) + expected_output_tokens

# ── SYSTEM PROMPTS ──
SYSTEM_INSTRUCTION = """
You are a political data classification engineer. Your job is to extract verbatim text passages from the provided candidate sources and classify them into the correct categories.

CRITICAL RULES:
1. Verbatim Extraction Only: Copy exact word-for-word text from the source. Never summarize, paraphrase, or alter the original text.
2. The "Null" Rule: If the provided text does not contain relevant information for a specific category, you MUST leave that category null/empty. Do not force-fit text. Most candidates will legitimately have several empty categories; that is the expected outcome, not a failure.
3. Single Source per Category: Identify the single best source page for a category and pull the relevant content from it. Do not stitch together quotes from different URLs into the same category block.
4. Sourcing: Each "=== SOURCE: [URL] ===" header applies to all text beneath it until the next "=== SOURCE:" header or "=== END OF SOURCES ===". Determine which block your extracted text came from and use that block's exact URL as the `source_url`. Never use a URL from a different block.
5. Mutual Exclusivity: Each passage belongs to exactly one category. Do not place the same quote in multiple categories. Pick the best fit using the category descriptions in the schema.
6. Subject Must Be The Candidate: Extract only text describing the candidate named in the prompt. Campaign sites frequently contain text about other people -- endorsers, opponents, running mates, staff, family members with their own biographies, and quoted supporters. Do not attribute any of that to the candidate.
7. Ignore Boilerplate: Skip navigation labels, fundraising and donation appeals, newsletter signup text, volunteer forms, event listings, merchandise, legal disclaimers, and "Paid for by" committee notices. None of it belongs in any category.
8. Length Discipline: Extract the most relevant passage for each category, up to roughly 150 words. If a category's relevant material is longer, choose the single most representative passage rather than reproducing the entire page. Staying within this limit matters -- an over-long response gets truncated and the entire result is discarded.
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

    # --- PRE-FLIGHT: nothing to categorize ---
    # A candidate with no Ballotpedia bio, no themes, and no reachable website
    # gives the model nothing to work with. Calling the API anyway burns a
    # request against the daily quota and the RPM pacing budget for a
    # guaranteed-empty result.
    if not (bp_bio or bp_themes or camp_web_blocks):
        log_func(
            f"    ⚠️ No biography, campaign themes, or website text found for "
            f"{candidate_data['metadata']['name']} -- skipping AI call (nothing to categorize)."
        )
        # A distinct sentinel rather than {}. An empty dict reads as "failed,
        # retry me" to the crawler's resume logic, which would re-scrape this
        # candidate on every future run forever. This says "we looked, there
        # was genuinely nothing," which is a completed state.
        return {
            "NO_CONTENT": "No biography, campaign themes, or reachable campaign website text "
                          "was found for this candidate, so no categorization was attempted."
        }

    # Sources are explicitly delimited and closed. The Ballotpedia block is
    # only opened when it actually has content, so its header can never end up
    # implicitly scoping the campaign website text below it.
    source_sections = []
    if bp_bio or bp_themes:
        bp_section = f"=== SOURCE: {bp_url} ==="
        if bp_bio:
            bp_section += f"\n[BIOGRAPHY]\n{bp_bio}"
        if bp_themes:
            bp_section += f"\n[CAMPAIGN THEMES]\n{bp_themes}"
        source_sections.append(bp_section)

    if camp_web_formatted:
        source_sections.append(camp_web_formatted)

    prompt_payload = f"""CANDIDATE: {candidate_data['metadata']['name']}
OFFICE: {candidate_data['metadata']['office']}

Extract and classify text about {candidate_data['metadata']['name']} only.
Each source block below begins with its own "=== SOURCE: <url> ===" header and
ends where the next header or "=== END OF SOURCES ===" begins.

{chr(10).join(f"{section}{chr(10)}" for section in source_sections)}
=== END OF SOURCES ===
"""

    token_weight = get_estimated_total_tokens(prompt_payload)
    log_func(f"    ↳ Payload token estimation: ~{token_weight:,} tokens")

    # Call metronome
    rate_limiter.wait_if_needed(token_weight, log_func)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type='application/json',
        response_schema=CandidateCategories,
        temperature=0.0,
        max_output_tokens=MAX_OUTPUT_TOKENS
    )

    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt_payload,
            config=config
        )

        # Truncated output is malformed JSON, and the regex fallback below
        # can't repair unbalanced braces -- so without this check the
        # candidate silently ends up with {} and no explanation.
        try:
            finish_reason = str(response.candidates[0].finish_reason)
        except (AttributeError, IndexError, TypeError):
            finish_reason = ""
        if "MAX_TOKENS" in finish_reason:
            log_func(
                f"    ⚠️ Response hit the {MAX_OUTPUT_TOKENS:,} output token ceiling and was "
                f"truncated. Result may be incomplete or unparseable."
            )

        if response.parsed is not None:
            return response.parsed.model_dump(by_alias=True, exclude_none=True)
        else:
            json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if json_match:
                return {k: v for k, v in json.loads(json_match.group(0)).items() if v is not None}
            log_func("    ⚠️ Could not parse a JSON object from the model response.")
            return {}
    except Exception as e:
        log_func(f"    ❌ Gemini Pipeline Error: {str(e)}")
        return {"ERROR": str(e)}
