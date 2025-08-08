
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import os
import uuid
import fitz  # PyMuPDF
import requests
from PyPDF2 import PdfMerger, PdfReader
import re
from urllib.parse import urlparse, urlunparse
import time

app = Flask(__name__)  # FIXED: Changed _name_ to __name__


CORS(app, origins=[
    "http://localhost:5173",
    "http://localhost:5500",
    "https://your-frontend-domain.com"
], supports_credentials=True)

@app.after_request
def after_request(response):
    origin = request.headers.get("Origin")
    if origin in ["http://localhost:5173", "http://localhost:5500", "https://your-frontend-domain.com"]:
        response.headers.add("Access-Control-Allow-Origin", origin)
    response.headers.add("Access-Control-Allow-Credentials", "true")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response


UPLOAD_FOLDER = 'uploads'
MERGED_FOLDER = 'merged'
DOWNLOADS_FOLDER = 'downloads'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(MERGED_FOLDER, exist_ok=True)
os.makedirs(DOWNLOADS_FOLDER, exist_ok=True)

def normalize_url(url):
    parsed = urlparse(url)
    clean = parsed._replace(query="", fragment="")
    return urlunparse(clean).rstrip('/')

def extract_links_from_pdf(filepath):
    links = set()
    doc = fitz.open(filepath)
    url_pattern = re.compile(r'https?://[^\s<>\)\]]+')

    for page_num, page in enumerate(doc, start=1):
        for link in page.get_links():
            uri = link.get("uri")
            if uri:
                print(f"[Page {page_num}] Clickable link: {uri}")
                links.add(uri)

        text = page.get_text()
        text_urls = url_pattern.findall(text)
        for url in text_urls:
            print(f"[Page {page_num}] Text URL: {url}")
            links.add(url)

    print(f"[INFO] Total links extracted (raw): {len(links)}")
    return list(links)

def download_pdfs(links):
    file_paths = []
    for url in links:
        retries = 3
        backoff = 3
        for attempt in range(retries):
            try:
                print(f"\\n[INFO] Trying to download: {url} (Attempt {attempt + 1})")
                r = requests.get(url, timeout=30)
                content_type = r.headers.get("Content-Type", "")
                first_bytes = r.content[:10]

                if r.status_code == 200 and content_type.startswith("application/pdf") and b"%PDF" in first_bytes:
                    filename = f"{uuid.uuid4()}.pdf"
                    filepath = os.path.join(DOWNLOADS_FOLDER, filename)
                    with open(filepath, "wb") as f:
                        f.write(r.content)
                    file_size = os.path.getsize(filepath)
                    print(f"[SUCCESS] Saved PDF {filename} - {file_size} bytes")
                    file_paths.append(filepath)
                    break
                else:
                    print(f"[SKIPPED] Not a valid PDF: {url}")
                    print(f"Status: {r.status_code}, Content-Type: {content_type}, First bytes: {first_bytes}")
                    break
            except requests.exceptions.RequestException as e:
                print(f"[ERROR] {url} failed (Attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                else:
                    print(f"[FAILURE] All retries failed for {url}")
    return file_paths

def merge_pdfs(pdf_paths, output_path):
    merger = PdfMerger()
    total_pages = 0

    for path in pdf_paths:
        try:
            reader = PdfReader(path)
            pages = len(reader.pages)

            if pages == 0:
                print(f"[SKIP] {os.path.basename(path)} has 0 pages.")
                os.remove(path)
                continue

            merger.append(path)
            print(f"[MERGED] {os.path.basename(path)} with {pages} pages.")
            total_pages += pages

        except Exception as e:
            print(f"[ERROR] Failed to merge {os.path.basename(path)}: {e}")
            try:
                os.remove(path)
                print(f"[REMOVED] Corrupted/Protected file deleted: {os.path.basename(path)}")
            except Exception as delete_error:
                print(f"[ERROR] Couldn't delete {path}: {delete_error}")

    if total_pages == 0:
        print("[WARN] No valid PDFs to merge.")
        raise Exception("All downloaded PDFs are invalid, corrupted, or protected.")

    merger.write(output_path)
    merger.close()
    print(f"[INFO] Merged PDF created at {output_path} with total {total_pages} pages.")

    try:
        reader = PdfReader(output_path)
        print(f"[INFO] Final merged PDF has {len(reader.pages)} pages")
    except Exception as e:
        print(f"[WARN] Couldn't read merged PDF: {e}")

@app.route('/')
def home():
    return "✅ PDF Link Extractor Backend is running!"

@app.route('/upload', methods=['POST'])
def upload_pdf():
    try:
        if 'file' not in request.files:
            raise Exception("No file part in request.")

        file = request.files['file']
        if file.filename == '':
            raise Exception("No selected file.")

        filename = f"{uuid.uuid4()}.pdf"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        print(f"[INFO] File saved to {filepath}")

        raw_links = extract_links_from_pdf(filepath)
        normalized_links_set = set()
        clean_links = []

        for link in raw_links:
            norm = normalize_url(link)
            if norm not in normalized_links_set:
                normalized_links_set.add(norm)
                clean_links.append(link)

        links = clean_links
        print(f"[INFO] Normalized & deduplicated to {len(links)} links")

        if not links:
            return jsonify({"status": "no_links", "message": "No links found in PDF"}), 200

        downloaded = download_pdfs(links)
        print(f"[INFO] Downloaded {len(downloaded)} files")

        if not downloaded:
            return jsonify({
                "status": "download_failed",
                "message": "Links found but couldn't be downloaded.",
                "extractedLinks": links
            }), 200

        merged_filename = f"merged_{uuid.uuid4()}.pdf"
        merged_path = os.path.join(MERGED_FOLDER, merged_filename)
        merge_pdfs(downloaded, merged_path)
        print(f"[INFO] Merged PDF created at {merged_path}")

        return jsonify({
            "status": "success",
            "mergedPdfUrl": f"/download/{merged_filename}",
            "extractedLinks": links
        })

    except Exception as e:
        print(f"[ERROR] {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    try:
        file_path = os.path.join(MERGED_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({"status": "error", "message": "File not found"}), 404
        return send_file(file_path, as_attachment=True)
    except Exception as e:
        print(f"[ERROR] Download failed: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def cleanup_old_files(folder_path, age_minutes=30):
    now = time.time()
    cutoff = now - (age_minutes * 60)

    deleted_files = 0
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            file_modified_time = os.path.getmtime(file_path)
            if file_modified_time < cutoff:
                try:
                    os.remove(file_path)
                    print(f"[CLEANUP] Deleted old file: {file_path}")
                    deleted_files += 1
                except Exception as e:
                    print(f"[ERROR] Couldn't delete {file_path}: {e}")
    print(f"[CLEANUP] Total files deleted from {folder_path}: {deleted_files}")

if __name__ == '__main__':  # FIXED: Changed _name_ to __name__
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
