import streamlit as st
import os
import sys
import subprocess
import time
import io
import base64
import requests
from PIL import Image
import fitz  # PyMuPDF

# --- SETUP PAGE CONFIG ---
st.set_page_config(
    page_title="PansGPT Ingestion Admin",
    page_icon="💊",
    layout="wide"
)

# --- HELPER FUNCTIONS ---
def install_package(package_name):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
        return True
    except subprocess.CalledProcessError:
        return False

# --- IMPORT/INSTALL DEPENDENCIES DYNAMICALLY ---
try:
    from dotenv import load_dotenv
except ImportError:
    install_package("python-dotenv")
    from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:
    install_package("groq")
    from groq import Groq

# Load .env if present
load_dotenv()

# --- SIDEBAR CONFIGURATION ---
st.sidebar.title("⚙️ Configuration")

# API Keys (Pre-filled from .env if available, editable by user)
GROQ_API_KEY = st.sidebar.text_input(
    "Groq API Key", 
    value=os.getenv("GROQ_API_KEY", ""), 
    type="password"
)
SUPABASE_URL = st.sidebar.text_input(
    "Supabase URL", 
    value=os.getenv("SUPABASE_URL", "")
)
SUPABASE_KEY = st.sidebar.text_input(
    "Supabase Service Role Key", 
    value=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""), 
    type="password"
)
SUPABASE_BUCKET = st.sidebar.text_input("Storage Bucket Name", value="lecture-images")

# --- CORE LOGIC (Adapted from your script) ---

def upload_to_supabase(image_bytes, filename):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "https://placeholder.url/credentials_missing.png"

    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "image/png"
    }
    
    try:
        response = requests.post(url, data=image_bytes, headers=headers)
        
        # Check for success or duplicate
        is_success = response.status_code == 200
        is_duplicate = (
            response.status_code == 409 or 
            "Duplicate" in response.text or 
            "duplicate" in response.text
        )

        final_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"

        if is_success:
            return final_url
        elif is_duplicate:
            # st.toast(f"File exists: {filename}", icon="ℹ️") # Optional: Notify user
            return final_url
        else:
            st.error(f"Upload failed: {response.text}")
            return "https://placeholder.url/upload_failed.png"
            
    except Exception as e:
        st.error(f"Supabase Connection Error: {e}")
        return "https://placeholder.url/error.png"

def analyze_image_with_groq(image_bytes, client):
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        prompt = """
        Analyze this image from a pharmacy lecture slide.
        1. If it is a **Table**, transcribe it strictly into a Markdown table.
        2. If it is a **Diagram/Chart**, describe the biological pathway or process in detail.
        3. If it is a **Chemical Structure**, describe the molecule and its key functional groups.
        4. If it is just **scanned text**, transcribe it exactly.
        Return ONLY the content. Do not add conversational filler.
        """

        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct", 
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[Vision Error: {str(e)}]"

def process_file(uploaded_file):
    # Initialize Groq Client
    if not GROQ_API_KEY:
        st.error("Groq API Key is missing!")
        return None

    try:
        client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        st.error(f"Failed to init Groq: {e}")
        return None

    # Read the uploaded file into PyMuPDF
    # PyMuPDF can open from bytes stream directly
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    
    full_content = ""
    image_count = 0
    total_pages = len(doc)
    
    # Progress Bar
    progress_bar = st.progress(0)
    status_text = st.empty()

    for page_num, page in enumerate(doc):
        # Update progress
        progress = (page_num + 1) / total_pages
        progress_bar.progress(progress)
        status_text.text(f"Processing Page {page_num + 1} of {total_pages}...")

        # 1. Sort blocks
        blocks = page.get_text("dict")["blocks"]
        blocks.sort(key=lambda b: b["bbox"][1])

        for block in blocks:
            # TEXT
            if block["type"] == 0:
                block_text = ""
                for line in block["lines"]:
                    for span in line["spans"]:
                        block_text += span["text"] + " "
                if block_text.strip():
                    full_content += block_text.strip() + "\n\n"

            # IMAGE
            elif block["type"] == 1:
                image_count += 1
                img_ext = block["ext"]
                image_bytes = block["image"]
                
                if len(image_bytes) < 2048: continue

                # Unique filename using original filename + timestamp to prevent overwrites
                clean_name = uploaded_file.name.split('.')[0].replace(" ", "_")
                filename = f"doc_{clean_name}_p{page_num}_img{image_count}.{img_ext}"
                
                # Upload
                public_url = upload_to_supabase(image_bytes, filename)
                
                # Analyze
                vision_context = analyze_image_with_groq(image_bytes, client)
                
                # Build Token
                clean_context = vision_context.replace('"', "'")
                token = f"\n<<SLIDE_IMAGE: url=\"{public_url}\" caption=\"Image {image_count} (Page {page_num+1})\" context=\"{clean_context}\">>\n"
                
                full_content += token + "\n"
                
                # Small sleep to prevent rate limits
                time.sleep(1.0)
    
    progress_bar.progress(100)
    status_text.text("Processing Complete!")
    return full_content

# --- MAIN UI LAYOUT ---

st.title("💊 PansGPT Ingestion Tool")
st.markdown("""
Upload a pharmacy lecture PDF to convert it into AI-ready text. 
This process extracts text, identifies images, uses AI to describe diagrams, and saves everything into a format for your RAG database.
""")

uploaded_file = st.file_uploader("Upload Lecture PDF", type=["pdf"])

if uploaded_file is not None:
    st.info(f"Loaded: **{uploaded_file.name}** ({uploaded_file.size / 1024:.1f} KB)")
    
    if st.button("🚀 Start Processing", type="primary"):
        with st.spinner("Analyzing document... do not close this tab."):
            result_text = process_file(uploaded_file)
            
            if result_text:
                st.success("Processing Complete!")
                
                # Create filename
                output_name = uploaded_file.name.replace(".pdf", "_processed.txt")
                
                # Download Button
                st.download_button(
                    label="📥 Download Processed Text",
                    data=result_text,
                    file_name=output_name,
                    mime="text/plain"
                )
                
                # Preview
                with st.expander("Preview Processed Content"):
                    st.text_area("Content", result_text, height=300)