import streamlit as st
import os
import sys
import subprocess
import time
import base64
import requests
import re # Added for parsing image URLs during deletion
import fitz  # PyMuPDF
from datetime import datetime

# --- SETUP PAGE CONFIG ---
st.set_page_config(
    page_title="PansGPT Content Manager",
    page_icon="💊",
    layout="wide"
)

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
    # If on cloud and missing packages, this catches it, though requirements.txt handles it mostly
    install_package("python-dotenv")
    install_package("groq")
    install_package("supabase")
    from dotenv import load_dotenv
    from groq import Groq
    from supabase import create_client, Client

load_dotenv()

# --- SECRETS MANAGEMENT (HIDDEN CONFIG) ---
# Tries to get keys from Streamlit Secrets (Cloud) first, then local .env
def get_secret(key):
    try:
        # This crashes locally if .streamlit/secrets.toml doesn't exist
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass # Ignore error and fall back to os.getenv (local .env)
        
    return os.getenv(key)

GROQ_API_KEY = get_secret("GROQ_API_KEY")
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = "lecture-images"

# Initialize Clients
if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        st.error(f"Supabase Connection Error: {e}")

# --- DATABASE FUNCTIONS ---

def log_upload_to_db(filename, subject, processed_text):
    """Saves the metadata AND content of the processed file to Supabase DB"""
    if not supabase:
        return
    
    # REQUIRED SQL SETUP IN SUPABASE:
    # CREATE TABLE documents (
    #   id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    #   created_at TIMESTAMPTZ DEFAULT NOW(),
    #   filename TEXT,
    #   subject TEXT,
    #   status TEXT DEFAULT 'processed',
    #   content TEXT  <-- NEW COLUMN REQUIRED
    # );
    
    data = {
        "filename": filename,
        "subject": subject,
        "status": "processed",
        "content": processed_text, # Saving the actual text now
        "created_at": datetime.utcnow().isoformat()
    }
    
    try:
        supabase.table("documents").insert(data).execute()
    except Exception as e:
        st.warning(f"Could not save to history log: {e}")

def delete_document(doc_id):
    """Deletes a document AND its associated images from Storage"""
    if not supabase:
        return
    
    # 1. Fetch content to find linked images before deleting the record
    try:
        response = supabase.table("documents").select("content").eq("id", doc_id).execute()
        if response.data:
            content = response.data[0].get("content", "")
            
            # Find URLs that match our bucket pattern
            # Matches: url=".../lecture-images/filename.png"
            urls = re.findall(r'url="([^"]+)"', content)
            
            files_to_remove = []
            for url in urls:
                # Check if URL belongs to our bucket
                if f"/{SUPABASE_BUCKET}/" in url:
                    # Extract filename: last part of URL after bucket name
                    filename = url.split(f"/{SUPABASE_BUCKET}/")[-1]
                    files_to_remove.append(filename)
            
            # Remove images from Supabase Storage
            if files_to_remove:
                # Supabase storage.remove expects a list of file paths
                supabase.storage.from_(SUPABASE_BUCKET).remove(files_to_remove)
                
    except Exception as e:
        # Just warn, don't stop the DB deletion if image cleanup fails
        print(f"Image cleanup warning: {e}")

    # 2. Delete the database record
    try:
        supabase.table("documents").delete().eq("id", doc_id).execute()
        st.toast("Document and images deleted successfully!", icon="🗑️")
    except Exception as e:
        st.error(f"Could not delete document: {e}")

def get_upload_history():
    """Fetches list of uploaded docs"""
    if not supabase:
        return []
    try:
        # Fetch everything, limit to last 50
        response = supabase.table("documents").select("*").order("created_at", desc=True).limit(50).execute()
        return response.data
    except Exception as e:
        return []

# --- PROCESSING LOGIC ---

def upload_image_to_storage(image_bytes, filename):
    """Uploads image to Supabase Bucket"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "https://placeholder.url/credentials_missing.png"

    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "image/png"
    }
    
    try:
        response = requests.post(url, data=image_bytes, headers=headers)
        # Check success (200) or duplicate (409)
        final_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
        if response.status_code == 200 or response.status_code == 409 or "Duplicate" in response.text:
            return final_url
        return "https://placeholder.url/upload_failed.png"
    except:
        return "https://placeholder.url/error.png"

def analyze_image_groq(image_bytes):
    """Vision Pass using Groq"""
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        prompt = "Analyze this pharmacy slide image. Transcribe tables to markdown. Describe diagrams/pathways in detail. Transcribe text exactly. Return ONLY content."
        
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct", 
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                ],
            }],
            max_tokens=1024,
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[Vision Error: {str(e)}]"

def process_pdf_file(uploaded_file):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    image_count = 0
    
    prog_bar = st.progress(0)
    status_txt = st.empty()
    
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        prog_bar.progress((i + 1) / total_pages)
        status_txt.text(f"Processing Page {i+1}/{total_pages}...")
        
        blocks = page.get_text("dict")["blocks"]
        blocks.sort(key=lambda b: b["bbox"][1])

        for block in blocks:
            if block["type"] == 0: # Text
                text = " ".join([span["text"] for line in block["lines"] for span in line["spans"]])
                if text.strip(): full_content += text.strip() + "\n\n"
            
            elif block["type"] == 1: # Image
                image_count += 1
                img_bytes = block["image"]
                if len(img_bytes) < 2048: continue
                
                clean_name = uploaded_file.name.split('.')[0].replace(" ", "_")
                fname = f"doc_{clean_name}_p{i}_img{image_count}.{block['ext']}"
                
                pub_url = upload_image_to_storage(img_bytes, fname)
                desc = analyze_image_groq(img_bytes)
                
                # Clean up description
                clean_desc = desc.replace('"', "'")
                token = f"\n<<SLIDE_IMAGE: url=\"{pub_url}\" caption=\"Img {image_count} (Page {i+1})\" context=\"{clean_desc}\">>\n"
                
                full_content += token + "\n"
                time.sleep(1.0) # Rate limit safety

    prog_bar.progress(100)
    status_txt.text("Done!")
    return full_content

# --- UI LAYOUT ---

st.title("💊 PansGPT Manager")
st.markdown("---")

# Split Screen Layout: Left (Upload) | Right (History)
col1, col2 = st.columns([1, 1.2], gap="large") 

# --- LEFT COLUMN: UPLOAD ---
with col1:
    with st.container(border=True):
        st.subheader("📤 Upload Material")
        st.info("Upload PDF lectures here. The AI will extract text and analyze diagrams.")
        
        subject_tag = st.selectbox(
            "Subject Category", 
            [
                "Pharmacology(PCL)", 
                "Pharmaceutical Medicinal Chemistry(PCH)", 
                "Pharmaceutics(PCT)", 
                "Clinical Pharmacy(PCP/CLP)", 
                "Pharmaceutical Microbiology(PMB)", 
                "Pharmaceutical Technology(PTE)", 
                "Anatomy(ANA)", 
                "Physiology(PHY)", 
                "Biochemistry(BIO)", 
                "Other"
            ]
        )
        
        uploaded_file = st.file_uploader("Drop PDF here", type=["pdf"])

        if uploaded_file and st.button("Start Processing", type="primary"):
            if not GROQ_API_KEY:
                st.error("Missing Groq API Key in Secrets.")
            else:
                with st.spinner("Processing... this may take a minute."):
                    processed_text = process_pdf_file(uploaded_file)
                    
                    if processed_text:
                        st.success("Processing Complete!")
                        
                        # Log to DB (Now saving content!)
                        log_upload_to_db(uploaded_file.name, subject_tag, processed_text)
                        
                        out_name = uploaded_file.name.replace(".pdf", "_processed.txt")
                        st.download_button("📥 Download Result", processed_text, file_name=out_name)
                        
                        time.sleep(1) # Give db a moment
                        st.rerun()

# --- RIGHT COLUMN: HISTORY (Custom UI) ---
with col2:
    with st.container(border=True):
        # Header with Refresh Button
        h1, h2 = st.columns([4, 1])
        h1.subheader("📚 Library History")
        if h2.button("🔄"):
            st.rerun()

        history_data = get_upload_history()
        
        if history_data:
            for doc in history_data:
                # Create a card-like container for each file
                with st.container(border=True):
                    # Layout: Info (Left) | Download (Right) | Delete (Far Right)
                    c_info, c_down, c_del = st.columns([4, 1, 0.5])
                    
                    with c_info:
                        st.markdown(f"**{doc.get('filename', 'Unknown File')}**")
                        # Format date nicely
                        raw_date = doc.get('created_at', '')
                        display_date = raw_date[:10] if raw_date else "Unknown Date"
                        st.caption(f"🏷️ {doc.get('subject', 'General')} • 📅 {display_date}")
                    
                    with c_down:
                        # Only show download button if content exists
                        content = doc.get('content', '')
                        if content:
                            dl_name = doc.get('filename', 'doc.pdf').replace(".pdf", ".txt")
                            st.download_button(
                                "📥", 
                                data=content, 
                                file_name=dl_name,
                                key=f"dl_{doc['id']}",
                                help="Download processed text"
                            )
                        else:
                            st.caption("No content")

                    with c_del:
                        if st.button("🗑️", key=f"del_{doc['id']}", help="Delete permanently"):
                            delete_document(doc['id'])
                            time.sleep(0.5)
                            st.rerun()
        else:
            st.info("No documents found in database.")
