import streamlit as st
import os
import sys
import subprocess
import time
import base64
import requests
import json
import fitz  # PyMuPDF
from datetime import datetime

# --- SETUP PAGE CONFIG ---
st.set_page_config(page_title="PansGPT Manager", page_icon="💊", layout="wide")

# --- HELPER: INSTALL PACKAGES ---
def install_package(package_name):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
    except subprocess.CalledProcessError:
        pass

# --- IMPORTS ---
try:
    from dotenv import load_dotenv
    from groq import Groq
    from supabase import create_client, Client
except ImportError:
    install_package("python-dotenv")
    install_package("groq")
    install_package("supabase")
    from dotenv import load_dotenv
    from groq import Groq
    from supabase import create_client, Client

load_dotenv()

# --- SECRETS ---
def get_secret(key):
    try:
        if key in st.secrets: return st.secrets[key]
    except: pass
    return os.getenv(key)

GROQ_API_KEY = get_secret("GROQ_API_KEY")
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = "lecture-images"

if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try: supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e: st.error(f"Supabase Error: {e}")

# --- DB FUNCTIONS ---
def log_upload_to_db(filename, subject, pages_data):
    """Saves the structured PAGE DATA (JSON) to Supabase DB"""
    if not supabase: return
    
    # We serialize the list of pages to a JSON string
    json_content = json.dumps(pages_data)
    
    data = {
        "filename": filename,
        "subject": subject,
        "status": "processed",
        "content": json_content, # Saving JSON structure now, not raw text
        "created_at": datetime.utcnow().isoformat()
    }
    try: supabase.table("documents").insert(data).execute()
    except Exception as e: st.warning(f"Save error: {e}")

def get_upload_history():
    if not supabase: return []
    try:
        return supabase.table("documents").select("*").order("created_at", desc=True).limit(20).execute().data
    except: return []

def delete_document(doc_id):
    if not supabase: return
    try: supabase.table("documents").delete().eq("id", doc_id).execute()
    except: pass

# --- PROCESSING ---
def upload_image_to_storage(image_bytes, filename):
    if not SUPABASE_URL or not SUPABASE_KEY: return None
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    headers = {"Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "image/png"}
    try:
        response = requests.post(url, data=image_bytes, headers=headers)
        if response.status_code in [200, 409] or "Duplicate" in response.text:
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    except: pass
    return None

def analyze_image_groq(image_bytes):
    try:
        b64 = base64.b64encode(image_bytes).decode('utf-8')
        prompt = "Describe this image in detail for a pharmacy student. If it's a chemical structure, describe the rings and groups. If it's a diagram, describe the flow."
        resp = groq_client.chat.completions.create(
            model="meta-llama/llama-3.2-90b-vision-preview", # Using a solid vision model
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt},{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            max_tokens=500
        )
        return resp.choices[0].message.content.strip()
    except: return "No description available."

def process_pdf_file(uploaded_file):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    pages_list = []
    
    prog_bar = st.progress(0)
    status = st.empty()
    
    for i, page in enumerate(doc):
        prog_bar.progress((i + 1) / len(doc))
        status.text(f"Processing Page {i+1}...")
        
        # 1. Extract Text (This shows on the slide)
        text_content = page.get_text().strip()
        
        # 2. Extract Image (If any)
        image_data = None
        images = page.get_images(full=True)
        
        if images:
            # Get the largest image on the page (avoid icons)
            xref = images[0][0]
            base_img = doc.extract_image(xref)
            img_bytes = base_img["image"]
            
            if len(img_bytes) > 5000: # Ignore tiny images
                clean_name = uploaded_file.name.split('.')[0].replace(" ", "_")
                fname = f"doc_{clean_name}_p{i}_img.png"
                
                pub_url = upload_image_to_storage(img_bytes, fname)
                ai_desc = analyze_image_groq(img_bytes) # This is for the BRAIN, not the slide text
                
                image_data = {
                    "url": pub_url,
                    "description": ai_desc
                }

        # 3. Create Page Object
        page_obj = {
            "page_number": i + 1,
            "text": text_content if text_content else "No text on this slide.",
            "image": image_data
        }
        pages_list.append(page_obj)

    return pages_list

# --- UI ---
st.title("💊 PansGPT Manager (Page-Based)")
col1, col2 = st.columns([1, 1.2], gap="large")

with col1:
    with st.container(border=True):
        st.subheader("📤 Upload Course")
        subj = st.selectbox("Subject", ["Pharmacology", "Medicinal Chemistry", "Pharmaceutics", "Clinical Pharmacy", "Other"])
        f = st.file_uploader("PDF", type=["pdf"])
        if f and st.button("Process", type="primary"):
            if not GROQ_API_KEY: st.error("No API Key")
            else:
                with st.spinner("Processing pages..."):
                    data = process_pdf_file(f)
                    log_upload_to_db(f.name, subj, data)
                    st.success(f"Processed {len(data)} slides!")
                    time.sleep(1)
                    st.rerun()

with col2:
    with st.container(border=True):
        st.subheader("📚 Library")
        if st.button("🔄"): st.rerun()
        for d in get_upload_history():
            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"**{d.get('filename')}**\n\nExisting Slides: {len(json.loads(d['content'])) if d['content'] else 0}")
                if c2.button("🗑️", key=d['id']):
                    delete_document(d['id'])
                    st.rerun()
