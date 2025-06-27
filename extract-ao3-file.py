import re
import requests
from io import BytesIO
from PyPDF2 import PdfReader
from bs4 import BeautifulSoup
import time
import getpass
import urllib.parse
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json

input_file = "urls.txt"
output_file = "results.html"
processed_pdfs_file = "processed_pdfs.txt"
results_json_file = "results.json"

pattern = re.compile(r"http://archiveofourown\.org/works/(\d+)")
headers = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}

lock = threading.Lock()
session = requests.Session()

# Load existing results and track existing work URLs to avoid duplicates
all_results = []
existing_work_urls = set()
if os.path.exists(results_json_file):
    with open(results_json_file, "r", encoding="utf-8") as f:
        all_results = json.load(f)
    for work in all_results:
        existing_work_urls.add(work["url"])

# Ask user if they want to reprocess all PDFs
reprocess_all = input("Do you want to reprocess ALL PDFs? (y/N): ").strip().lower() == "y"

processed_pdfs = set()
if not reprocess_all and os.path.exists(processed_pdfs_file):
    with open(processed_pdfs_file, "r") as f:
        for line in f:
            url = line.strip()
            if url:
                processed_pdfs.add(url)
else:
    # Clear old files if reprocessing all
    if os.path.exists(output_file):
        os.remove(output_file)
    if os.path.exists(processed_pdfs_file):
        os.remove(processed_pdfs_file)
    if os.path.exists(results_json_file):
        os.remove(results_json_file)
    processed_pdfs = set()
    all_results = []
    existing_work_urls = set()

def login_to_ao3(session, username, password):
    login_url = "https://archiveofourown.org/users/login"
    try:
        login_page = session.get(login_url, headers=headers)
        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.find("input", {"name": "authenticity_token"})
        if not token_input:
            print("❌ Could not find authenticity_token.")
            return False
        auth_token = token_input["value"]
        payload = {
            "user[login]": username,
            "user[password]": password,
            "authenticity_token": auth_token,
            "commit": "Log in"
        }
        response = session.post(login_url, headers=headers, data=payload)
        if "Log Out" in response.text or "My Dashboard" in response.text:
            print("✅ Successfully logged into AO3.")
            return True
        else:
            print("❌ Login failed. Check your credentials.")
            return False
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return False

def scrape_ao3_stats(session, work_url):
    work_url = work_url.replace("http://", "https://")
    print(f"  Scraping stats from {work_url} ...")
    for attempt in range(3):
        try:
            response = session.get(work_url, headers=headers, timeout=15)
            if response.status_code == 404:
                return "404"
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            title_tag = soup.find("h2", class_="title heading")
            title = title_tag.text.strip() if title_tag else "No title found"

            summary_div = soup.find("div", class_="summary module")
            summary = summary_div.find("blockquote").text.strip() if summary_div else "No summary found"

            stats = {}
            stats_section = soup.find("dl", class_="work meta group")
            if stats_section:
                for dt, dd in zip(stats_section.find_all("dt"), stats_section.find_all("dd")):
                    label = dt.text.strip().lower()
                    value = dd.text.strip()
                    stats[label] = value

            return {"title": title, "summary": summary, "stats": stats}
        except requests.exceptions.RequestException as e:
            print(f"  Attempt {attempt + 1} failed: {e}")
        time.sleep(2 ** attempt)
    return None

def extract_title_summary_from_pdf(reader):
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    print("DEBUG lines[0]:", repr(lines[0]))  # for debug
    
    title = lines[0] if lines else "Unknown Title"
    
    metadata_keys = [
        "Rating:", "Archive Warning:", "Category:", "Fandom:",
        "Character:", "Additional Tags:", "Stats:"
    ]
    
    metadata = {}
    current_key = None
    summary_lines = []
    in_summary = False
    
    for line in lines[1:]:
        # Stop collecting metadata if we hit Summary or Notes section
        if line.lower() == "summary":
            in_summary = True
            continue
        if line.lower() == "notes":
            # skip notes section entirely
            in_summary = False
            break
        
        if in_summary:
            # Collect summary lines until blank line or next header
            if line == "" or any(line.startswith(k) for k in metadata_keys + ["Summary", "Notes"]):
                break
            summary_lines.append(line)
        else:
            # Parsing metadata
            for key in metadata_keys:
                if line.startswith(key):
                    current_key = key.rstrip(':')
                    val = line[len(key):].strip()
                    metadata[current_key] = val
                    break
            else:
                # Line does not start with a key
                # Append to previous key's value if any
                if current_key:
                    metadata[current_key] += " " + line

    summary_text = " ".join(summary_lines).strip() if summary_lines else "No summary found"
    
    return title.strip(), summary_text, metadata 

def fallback_title_from_filename(url):
    filename = os.path.basename(urllib.parse.urlparse(url).path)
    return urllib.parse.unquote(filename.replace(".pdf", "").replace("_", " ")).strip()

def extract_summary_from_text(text):
    # Match Summary section up to the first occurrence of a known keyword like "Notes" or end of text
    pattern = re.compile(
        r"Summary\s*(.+?)(?=\s*Notes\b|\s*Rating\b|\s*Archive Warning\b|$)",
        re.DOTALL | re.IGNORECASE
    )
    match = pattern.search(text)
    if match:
        summary_text = match.group(1).strip()
        # Normalize whitespace
        summary_text = re.sub(r'\s+', ' ', summary_text)
        return summary_text
    return None


def process_pdf(pdf_url):
    if not reprocess_all and pdf_url in processed_pdfs:
        print(f"Skipping already processed PDF: {pdf_url}")
        return

    print(f"Downloading PDF: {pdf_url}")
    try:
        response = session.get(pdf_url, headers=headers, timeout=20)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(response.content)
            tmp_pdf_path = tmp_file.name

        with open(tmp_pdf_path, "rb") as f:
            reader = PdfReader(f)
            text = "".join(page.extract_text() or "" for page in reader.pages)
            found_links = pattern.findall(text)
            print(f"  Found {len(found_links)} AO3 links in PDF.")

            for work_id in found_links:
                work_url = f"https://archiveofourown.org/works/{work_id}"

                with lock:
                    if work_url in existing_work_urls:
                        print(f"  Skipping duplicate work: {work_url}")
                        continue
        # Mark immediately as processed to avoid duplicates within this PDF too
                    existing_work_urls.add(work_url)


                stats = scrape_ao3_stats(session, work_url)

                if stats == "404":
                    print("=== PDF extracted text preview (first 1000 chars) ===")
                    print(text[:1000])
                    title, summary, meta = extract_title_summary_from_pdf(reader)
                    summary = extract_summary_from_text(text) or ""
                    result = {
                        "url": work_url,
                        "title": title or fallback_title_from_filename(pdf_url),
                        "summary": summary,
                        "stats": meta,
                        "not_found": True,
                        "pdf_url": pdf_url
                    }
                elif stats is None:
                    title, summary, meta = extract_title_summary_from_pdf(reader)
                    result = {
                        "url": work_url,
                        "pdf_url": pdf_url,
                        "title": title,
                        "summary": summary,
                        "stats": None,
                        "not_found": False
                    }
                else:
                    result = {
                        "url": work_url,
                        "pdf_url": pdf_url,
                        "title": stats["title"],
                        "summary": stats["summary"],
                        "stats": stats["stats"],
                        "not_found": False
                    }

                with lock:
                    all_results.append(result)

                time.sleep(1.5)

        os.remove(tmp_pdf_path)

        with lock:
            processed_pdfs.add(pdf_url)
            with open(processed_pdfs_file, "a") as f:
                f.write(pdf_url + "\n")

    except Exception as e:
        print(f"  ❌ ERROR processing {pdf_url}: {e}")

# Begin script
print("🔐 AO3 Login (optional for restricted/private works):")
username = input("AO3 Username: ")
password = getpass.getpass("AO3 Password: ")
if not login_to_ao3(session, username, password):
    print("⚠️ Exiting due to failed login.")
    exit()

with open(input_file, "r") as f:
    urls = [line.strip() for line in f if line.strip()]
print(f"\n📄 Found {len(urls)} PDF URLs to process.\n")

with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(process_pdf, url) for url in urls]
    for future in as_completed(futures):
        pass

# Save results.json
with open(results_json_file, "w", encoding="utf-8") as f:
    json.dump(all_results, f, ensure_ascii=False, indent=2)

# Count of works extracted
work_count = len(all_results)

# Generate HTML output from all_results
all_entries = []
for work in all_results:
    entry = '<div class="work">\n'
    entry += f'<div class="title">{work["title"]}</div>\n'
    if work.get("not_found"):
        entry += '<div class="notfound">Work page not found; showing extracted metadata.</div>\n'
        entry += f'<div class="url"><a href="{work["pdf_url"]}" target="_blank">{work["pdf_url"]}</a></div>\n'
    else:
        entry += f'<div class="url"><a href="{work["url"]}" target="_blank">{work["url"]}</a></div>\n'

    entry += f'<div class="summary">{work["summary"]}</div>\n'
    if work["stats"]:
        entry += '<div class="stats">\n'
        for k, v in work["stats"].items():
            entry += f'<div><strong>{k.title()}:</strong> {v}</div>\n'
        entry += '</div>\n'
    entry += '</div>\n'
    all_entries.append(entry)

html_template = f"""
<html>
<head>
<title>AO3 Works Extraction Results ({work_count} works)</title>
<style>
body {{ font-family: Arial, sans-serif; background:#f5f5f5; color:#222; padding:20px; }}
.work {{ border:1px solid #ddd; padding:15px; margin-bottom:15px; background:#fff; border-radius:8px; }}
.title {{ font-weight:bold; font-size:1.2em; margin-bottom:5px; }}
.summary {{ font-style: italic; margin-bottom:10px; }}
.url a {{ color:#0066cc; text-decoration:none; }}
.stats div {{ margin: 2px 0; }}
.notfound {{ color:#a00; font-weight:bold; margin-bottom:10px; }}
</style>
</head>
<body>
<h1>AO3 Works Extraction Results ({work_count} works)</h1>
{''.join(all_entries)}
</body>
</html>
"""

with open(output_file, "w", encoding="utf-8") as f:
    f.write(html_template)

print(f"\n✅ Done! Results saved to {output_file} ({work_count} unique works).")
print(f"🔹 Results JSON: {results_json_file}")
print(f"🔹 Processed PDFs log: {processed_pdfs_file}")



