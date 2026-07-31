import asyncio
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- CONFIGURATION CONSTANTS ---
SKIP_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]
BLOCKED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "youtube.com", "linkedin.com", "threads.net", "bluesky.app", "bsky.app",
    "substack.com", "actblue.com", "winred.com", "paypal.com", "venmo.com",
}
BLOCKED_PATH_KEYWORDS = {
    "privacy", "privacy-policy", "terms", "accessibility", "accessibility-statement", 
    "donate", "donation", "jobs", "cart", "login", "sign-in", "sign-up", 
    "authentication", "create-account", "legal", "disclaimer", "terms-and-conditions",
}
BLOCKED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg"}

# Caps the number of internal sub-pages scraped per campaign site. Some sites
# have dozens/hundreds of internal links; without a cap, a single sprawling
# site can spike memory for the whole batch. Tune based on how deep you
# actually need to go for useful campaign content.
MAX_SUBPAGES_PER_SITE = 15

# --- EMAIL/CONTACT RESOLUTION HEURISTICS ---
def resolve_primary_contacts(emails, phones, addresses, candidate_name, website_url=""):
    """Applies heuristics to select the single best contact option and drops the rest."""
    primary_email = ""
    primary_phone = ""
    primary_address = ""

    if emails:
        def score_email(email):
            score = 0
            prefix, domain = email.split('@') if '@' in email else (email, '')
            name_parts = candidate_name.lower().split()
            if any(part in prefix for part in name_parts if len(part) > 2):
                score += 10
            keywords = ["info", "campaign", "contact", "vote", "team", "hello"]
            if any(kw in prefix for kw in keywords):
                score += 5
            if website_url and not any(p in website_url for p in ["blogspot", "wordpress"]):
                site_domain = urlparse(website_url).netloc.lower().replace("www.", "")
                if site_domain and site_domain in domain:
                    score += 15
            return score
        primary_email = max(emails, key=score_email)

    if phones:
        primary_phone = list(phones)[0]

    if addresses:
        primary_address = list(addresses)[0]

    return {
        "email": primary_email,
        "phone": primary_phone,
        "address": primary_address
    }

def extract_contact_details(text, html_content=""):
    """Scans clean text and raw HTML to catch contact hooks, including hidden mailto/tel links."""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = set(e.lower().strip() for e in re.findall(email_pattern, text))

    phone_pattern = r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
    phones = set(re.sub(r'[^\d]', '', p)[-10:] for p in re.findall(phone_pattern, text))

    address_pattern = r'(?:P\.?O\.?\s*Box\s+\d+|(?:\d+\s+[A-Za-z0-9\s\.\,]{2,}(?:St(?:reet)?|Av(?:enue)?|Rd|Road|Blvd|Boulevard|Drive|Dr|Ln|Lane|Way|Ct|Court|Plaza|Pl)))(?:[\s,]+[A-Za-z\s]{2,}){1,2}[\s,]+[A-Z]{2}[\s,]+\d{5}(?:-\d{4})?'
    addresses = set(re.sub(r'\s+', ' ', addr).strip().title() for addr in re.findall(address_pattern, text, re.IGNORECASE))

    if html_content:
        soup = BeautifulSoup(html_content, "lxml")
        for a in soup.find_all("a", href=True):
            href = a['href'].strip().lower()
            if href.startswith("mailto:"):
                clean_email = href.replace("mailto:", "").split("?")[0].strip()
                if re.match(email_pattern, clean_email):
                    emails.add(clean_email)
            elif href.startswith("tel:"):
                clean_phone = href.replace("tel:", "").strip()
                phones.add(re.sub(r'[^\d]', '', clean_phone)[-10:])

    return {
        "emails": list(emails),
        "phones": list(phones),
        "addresses": list(addresses)
    }

# --- TEXT PARSING & LINK DEDUPLICATION ---
def clean_text(html):
    """Parse HTML, strip noise tags, return clean text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(SKIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def get_internal_links(html, base_url):
    """Filter down valid candidate site subpages to crawl."""
    soup = BeautifulSoup(html, "lxml")
    
    # FIX: Normalize the base domain by stripping 'www.'
    base_domain = urlparse(base_url).netloc.lower().replace("www.", "") 
    links = set()
    
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")): 
            continue
            
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        
        # Safely handle domains to find the apex
        domain_parts = parsed.netloc.split(".")
        apex = ".".join(domain_parts[-2:]) if len(domain_parts) >= 2 else parsed.netloc

        if apex in BLOCKED_DOMAINS: 
            continue
            
        # FIX: Normalize the parsed link's domain by stripping 'www.'
        parsed_domain = parsed.netloc.lower().replace("www.", "")
        
        if parsed.scheme in ("http", "https") and parsed_domain == base_domain:
            path = parsed.path.lower()
            if any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS): 
                continue
            if set(path.strip("/").split("/")) & BLOCKED_PATH_KEYWORDS: 
                continue
            links.add(parsed._replace(query="", fragment="").geturl())
            
    return links

def deduplicate_pages(pages):
    """Remove repeating cross-page text structures safely in-memory."""
    seen_sentences = set()
    cleaned_pages = []
    for page in pages:
        sentences = re.split(r'(?<=[.!?])\s+', page["text"])
        unique = []
        for sentence in sentences:
            normalized = sentence.strip()
            if not normalized:
                continue
            if normalized not in seen_sentences:
                seen_sentences.add(normalized)
                unique.append(normalized)
        if unique:
            cleaned_pages.append({"url": page["url"], "text": " ".join(unique)})
    return cleaned_pages

def extract_section_by_id(soup, section_id):
    """Find a heading tracking section_id and gather clean text segments underneath."""
    heading_tags = ["h1", "h2", "h3", "h4"]
    anchor = soup.find(id=section_id)
    if not anchor:
        return None

    heading = anchor
    while heading and heading.name not in heading_tags:
        heading = heading.parent
    if not heading:
        return None

    level = int(heading.name[1])
    chunks = []
    for sibling in heading.find_next_siblings():
        if sibling.name in heading_tags and int(sibling.name[1]) <= level:
            break
        if sibling.name in ("style", "script"):
            continue
        for noise in sibling.find_all(["style", "script"]):
            noise.decompose()
        text = sibling.get_text(separator=" ", strip=True)
        if text:
            chunks.append(text)

    return re.sub(r"\s+", " ", " ".join(chunks)).strip() if chunks else None

def clean_campaign_themes(text):
    """Strip boilerplate structural headers and footers from survey arrays."""
    if not text:
        return text
    marker = "Expand all | Collapse all "
    idx = text.find(marker)
    if idx != -1:
        text = text[idx + len(marker):]
    note_idx = text.find(" Note: Ballotpedia reserves")
    if note_idx != -1:
        text = text[:note_idx]
    return text.strip()

def extract_links_and_text(html_content):
    """Scrapes social maps, websites, biography and text blocks simultaneously."""
    soup = BeautifulSoup(html_content, 'html.parser')
    data = {
        'contacts': {'campaign_website': '', 'website_type': '', 'facebook': '', 'x': '', 'instagram': '', 'youtube': '', 'tiktok': '', 'linkedin': ''},
        'biography': '',
        'campaign_themes': ''
    }

    bio = extract_section_by_id(soup, "Biography")
    if bio: data['biography'] = bio

    raw_themes = extract_section_by_id(soup, "2026_2")
    themes = clean_campaign_themes(raw_themes)
    if themes: data['campaign_themes'] = themes

    contact_text = soup.find(string="Contact")
    if contact_text:
        parent_tr = contact_text.find_parent('tr')
        parent_div = contact_text.find_parent('div')
        container = parent_tr or (parent_div.parent if parent_div else None)
        if container:
            raw_map = {link.get_text(strip=True).lower(): link['href'] for link in container.find_all('a', href=True)}

            if 'campaign website' in raw_map:
                data['contacts']['campaign_website'] = raw_map['campaign website']
                data['contacts']['website_type'] = 'campaign website'
            elif 'official website' in raw_map:
                data['contacts']['campaign_website'] = raw_map['official website']
                data['contacts']['website_type'] = 'official website'
            elif 'personal website' in raw_map:
                data['contacts']['campaign_website'] = raw_map['personal website']
                data['contacts']['website_type'] = 'personal website'

            platforms = {
                'facebook': ['campaign facebook', 'official facebook', 'personal facebook', 'facebook'],
                'x': ['campaign x', 'official x', 'personal x', 'campaign twitter', 'twitter', 'x'],
                'instagram': ['campaign instagram', 'official instagram', 'instagram'],
                'youtube': ['campaign youtube', 'youtube'],
                'tiktok': ['campaign tiktok', 'tiktok'],
                'linkedin': ['campaign linkedin', 'linkedin']
            }
            for key, labels in platforms.items():
                for label in labels:
                    if label in raw_map:
                        data['contacts'][key] = raw_map[label]
                        break
    return data

# --- NETWORKING ENGINE ---
async def fetch_page_html(page, url, log_func, custom_timeout=45000):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=custom_timeout)
        content = await page.content()

        waf_keywords = ["checking your browser", "just a moment...", "security challenge"]
        if any(keyword in content.lower() for keyword in waf_keywords):
            log_func(f"    [WAF Alert] Security challenge detected. Polling for resolution...")
            elapsed = 0
            poll_interval = 2000
            while elapsed < 20000:
                await page.wait_for_timeout(poll_interval)
                elapsed += poll_interval
                content = await page.content()
                if not any(keyword in content.lower() for keyword in waf_keywords):
                    log_func("    [WAF Alert] ✅ Challenge bypassed successfully!")
                    await page.wait_for_timeout(2000)
                    content = await page.content()
                    break
            else:
                log_func("    [WAF Alert] ⚠️ Polling timeout reached (Page may have resolved).")

        return content, page.url
    except Exception as e:
        log_func(f"    ⚠️ Fetch exception caught: {str(e)}")
        try:
            return await page.content(), page.url
        except:
            return None, str(e)

# --- MASTER HARVEST FLOW ---
async def async_harvest_pipeline(state: str, year: str, target_parties: list, include_tables: list, log_func, on_candidate_scraped=None):
    """
    Runs the scrape. Rather than accumulating every candidate's full record
    in memory and returning the whole batch at the end, this calls
    on_candidate_scraped(record) as soon as each candidate is done, so the
    caller can categorize/persist and let it be garbage collected immediately.
    Returns the number of candidates processed.
    """
    STATE_URL = state.replace(' ', '_')
    START_URL = f"https://ballotpedia.org/{STATE_URL}_elections,_{year}"
    processed_count = 0

    async with async_playwright() as p:
        log_func("Booting headless Chromium...")
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        log_func(f"Navigating to index page: {START_URL}...")
        main_html, _ = await fetch_page_html(page, START_URL, log_func)
        if not main_html:
            log_func("❌ Critical Failure: Main page blocked. Check config fields.")
            await browser.close()
            return processed_count

        await page.wait_for_timeout(3000)
        main_html = await page.content()
        soup = BeautifulSoup(main_html, 'html.parser')
        
        # Grab Candidate Tables
        candidate_tables = [t for t in soup.find_all('table') if t.find('tr') and {'candidate', 'office', 'party'}.issubset({th.get_text(strip=True).lower() for th in t.find('tr').find_all('th')})]
        
        TABLE_LABELS = ["Federal Candidates", "State Candidates", "Local Candidates"]
        harvested_roster = []
        
        for idx, table in enumerate(candidate_tables):
            # Resolve if this table matches "Federal", "State", or "Local"
            label = TABLE_LABELS[idx] if idx < len(TABLE_LABELS) else f"Table {idx+1}"
            if label not in include_tables: 
                continue

            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 4: 
                    continue

                party_text = cells[2].get_text(strip=True)
                keep_candidate = False
                if party_text == "Republican" and "Republican" in target_parties:
                    keep_candidate = True
                elif party_text == "Democratic" and "Democratic" in target_parties:
                    keep_candidate = True
                elif party_text not in ["Republican", "Democratic"] and "Independent" in target_parties:
                    keep_candidate = True

                if not keep_candidate: 
                    continue

                office_name = cells[1].get_text(strip=True)
                link_tag = cells[0].find('a', href=True)
                raw_href = link_tag['href'] if link_tag else ""
                bp_url = "https://ballotpedia.org" + raw_href if raw_href.startswith('/') else raw_href
                name_clean = cells[0].get_text(strip=True).split('(')[0].split('[')[0].replace("Candidate Connection", "").replace("Incumbent", "").strip()
                
                if name_clean and bp_url:
                    harvested_roster.append({'name': name_clean, 'office': office_name, 'bp_url': bp_url, 'party': party_text})

        log_func(f"✅ Roster compiled. Found {len(harvested_roster)} targeted candidates.")

        # Deep Extraction Loop
        for count, cand in enumerate(harvested_roster, start=1):
            log_func(f"\n[{count}/{len(harvested_roster)}] Processing Profile: {cand['name']}")
            
            bp_html, _ = await fetch_page_html(page, cand['bp_url'], log_func)
            if not bp_html:
                log_func(f"  ⚠️ Skipping profile; connection timed out.")
                continue

            bp_dataset = extract_links_and_text(bp_html)
            
            bio_array = [{"url": cand['bp_url'], "text": bp_dataset['biography']}] if bp_dataset['biography'] else []
            themes_array = []
            themes_text = bp_dataset['campaign_themes']
            if themes_text and "did not complete" not in themes_text and "has not yet completed" not in themes_text:
                themes_array.append({"url": cand['bp_url'], "text": themes_text})

            running_emails = set()
            running_phones = set()
            running_addresses = set()

            candidate_record = {
                "metadata": {
                    "name": cand['name'],
                    "office": cand['office'],
                    "party": cand['party'], # <--- NEW: Grab the party from the roster
                    "ballotpedia_url": cand['bp_url'],
                    "biography": bio_array,
                    "campaign_themes": themes_array,
                    "socials": bp_dataset['contacts'],
                    "extracted_contacts": {}
                },
                "campaign_website_text": []
            }

            site_url = bp_dataset['contacts']['campaign_website']
            is_gov_site = urlparse(site_url).netloc.endswith(('.gov', '.us')) or '.state.' in site_url

            if site_url:
                log_func(f"  🌐 Crawling campaign site: {site_url}")
                site_html, active_url = await fetch_page_html(page, site_url, log_func, custom_timeout=45000 if is_gov_site else 30000)

                if site_html:
                    home_text = clean_text(site_html)
                    parsed_home = extract_contact_details(home_text, html_content=site_html)
                    running_emails.update(parsed_home["emails"])
                    running_phones.update(parsed_home["phones"])
                    running_addresses.update(parsed_home["addresses"])

                    site_pages = [{"url": active_url, "text": home_text}]

                    if not is_gov_site:
                        internal_links = sorted(get_internal_links(site_html, active_url))
                        if len(internal_links) > MAX_SUBPAGES_PER_SITE:
                            log_func(f"    -> Found {len(internal_links)} sub-links. Capping at {MAX_SUBPAGES_PER_SITE} to control memory.")
                            internal_links = internal_links[:MAX_SUBPAGES_PER_SITE]
                        else:
                            log_func(f"    -> Found {len(internal_links)} sub-links. Scraping all...")

                        for sub_link in internal_links:
                            sub_html, final_sub_url = await fetch_page_html(page, sub_link, log_func)
                            final_domain = urlparse(final_sub_url).netloc
                            if any(blocked in final_domain for blocked in BLOCKED_DOMAINS):
                                continue
                            if sub_html:
                                sub_text = clean_text(sub_html)
                                site_pages.append({"url": final_sub_url, "text": sub_text})

                                parsed_sub = extract_contact_details(sub_text, html_content=sub_html)
                                running_emails.update(parsed_sub["emails"])
                                running_phones.update(parsed_sub["phones"])
                                running_addresses.update(parsed_sub["addresses"])

                            await page.wait_for_timeout(500)

                    candidate_record["campaign_website_text"] = deduplicate_pages(site_pages)
                else:
                    log_func(f"    ⚠️ Skipping link structure; server dropped network packet.")

            resolved = resolve_primary_contacts(
                emails=running_emails,
                phones=running_phones,
                addresses=running_addresses,
                candidate_name=cand['name'],
                website_url=site_url
            )
            candidate_record["metadata"]["extracted_contacts"] = resolved

            if on_candidate_scraped:
                on_candidate_scraped(candidate_record)
            processed_count += 1
            # candidate_record (and its full site text) falls out of scope
            # here and can be garbage collected, instead of living on in a
            # growing batch list for the rest of the run.

            await page.wait_for_timeout(2000)

        await browser.close()
        return processed_count

def run_scraper(state: str, year: str, target_parties: list, include_tables: list, log_func, on_candidate_scraped=None):
    """
    Synchronous wrapper to call from Streamlit.

    on_candidate_scraped, if provided, is called synchronously with each
    candidate_record as soon as it's scraped, letting the caller categorize
    and persist it right away instead of waiting for the whole batch.
    Returns the number of candidates processed.
    """
    return asyncio.run(async_harvest_pipeline(
        state, year, target_parties, include_tables, log_func,
        on_candidate_scraped=on_candidate_scraped
    ))
