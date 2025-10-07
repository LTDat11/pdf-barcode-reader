# app.py
import os
import sys
import csv
import requests
from io import BytesIO
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode
from PIL import Image
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

# ---------- Cấu hình ----------
BUNDLED_POPPLER_DIRNAME = "poppler_bin"  # nếu bạn ship poppler kèm
TRIM_FROM = 8
DEFAULT_MAX_WORKERS = 6
REQUEST_TIMEOUT = 30

# Nếu chạy PyInstaller (không thường gặp với Streamlit) -- giữ cho tương thích
if getattr(sys, "frozen", False):
    _bundle_bin = os.path.join(sys._MEIPASS, BUNDLED_POPPLER_DIRNAME)
    os.environ["PATH"] = _bundle_bin + os.pathsep + os.environ.get("PATH", "")

# ---------- Helpers ----------
def get_poppler_path() -> str:
    """
    Trả về đường dẫn thư mục poppler nếu bạn ship poppler_bin bên cạnh file này.
    Nếu không dùng poppler_bin kèm, hãy cài poppler system-wide và để trống đường dẫn này.
    """
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, BUNDLED_POPPLER_DIRNAME)

def extract_tracking_from_pdf_bytes(pdf_bytes: bytes, poppler_path: str) -> List[str]:
    """
    Chuyển PDF -> ảnh -> decode barcode trên mỗi trang.
    Trả về list các chuỗi đọc được (theo thứ tự top->down).
    """
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300, poppler_path=poppler_path if os.path.exists(poppler_path) else None)
    except Exception as e:
        raise RuntimeError(f"convert_from_bytes error: {e}")

    found = []
    for img in images:
        try:
            codes = decode(img)
            codes_sorted = sorted(codes, key=lambda c: c.rect.top)
            for c in codes_sorted:
                try:
                    s = c.data.decode("utf-8")
                except:
                    s = c.data.decode(errors="ignore")
                found.append(s)
        except Exception:
            # ignore page-level decode errors
            continue
    return found

def process_single(idx: int, url: str, poppler_path: str) -> Dict:
    """
    Tải PDF từ url và trả về dict result tương tự code Tkinter cũ.
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        pdf_bytes = resp.content
        codes = extract_tracking_from_pdf_bytes(pdf_bytes, poppler_path)
        if codes:
            raw = codes[0]
            if raw.startswith("9631"):
                trimmed = raw[-12:]
            else:
                trimmed = raw[TRIM_FROM:] if len(raw) > TRIM_FROM else raw
            result = {"index": idx, "url": url, "raw": raw, "trimmed": trimmed, "error": ""}
        else:
            result = {"index": idx, "url": url, "raw": "", "trimmed": "N/A", "error": "Not found"}
    except Exception as e:
        result = {"index": idx, "url": url, "raw": "", "trimmed": "N/A", "error": str(e)}
    return result

# ---------- Streamlit app ----------
st.set_page_config(page_title="PDF Barcode Batch Reader", layout="wide")
st.title("PDF Barcode Batch Reader — Extract & Trim")
st.markdown("Dán danh sách URL PDF (mỗi link 1 dòng). App chạy song song và giữ session riêng cho từng user.")

# Initialize session state
if "results" not in st.session_state:
    st.session_state["results"] = []        # list of result dicts (or None)
if "total" not in st.session_state:
    st.session_state["total"] = 0
if "processed" not in st.session_state:
    st.session_state["processed"] = 0
if "urls" not in st.session_state:
    st.session_state["urls"] = []
if "running" not in st.session_state:
    st.session_state["running"] = False

col1, col2 = st.columns([2, 1])

with col1:
    urls_text = st.text_area("URLs (mỗi link 1 dòng)", height=220, value="\n".join(st.session_state.get("urls", [])))
    max_workers = st.number_input("Max workers (threads)", min_value=1, max_value=32, value=DEFAULT_MAX_WORKERS, step=1)
    start_btn = st.button("Start processing", disabled=st.session_state["running"])
    refresh_btn = st.button("Refresh / Reset session")
    st.write("Lưu ý: nếu bạn không ship poppler, cần cài `poppler` system-wide (apt/brew/choco).")

with col2:
    st.subheader("Actions")
    st.download_button("Tải file ví dụ (template .txt)", data="https://example.com", file_name="template.txt")  # placeholder
    st.write("Kết quả:")  # placeholder for UI alignment

progress_bar = st.progress(0)
status_text = st.empty()
table_area = st.empty()

# Reset
if refresh_btn:
    st.session_state["results"] = []
    st.session_state["total"] = 0
    st.session_state["processed"] = 0
    st.session_state["urls"] = []
    st.session_state["running"] = False
    progress_bar.progress(0)
    status_text.text("Idle")
    st.experimental_rerun()

# Start processing
if start_btn:
    lines = [line.strip() for line in urls_text.splitlines() if line.strip()]
    st.session_state["urls"] = lines
    total = len(lines)
    if total == 0:
        status_text.text("Please paste URLs first")
    else:
        st.session_state["total"] = total
        st.session_state["processed"] = 0
        st.session_state["results"] = [None] * total
        st.session_state["running"] = True

        poppler_path = get_poppler_path()
        # If poppler folder exists, provide path; otherwise let pdf2image use system poppler (None)
        if os.path.exists(poppler_path):
            used_poppler_path = poppler_path
        else:
            used_poppler_path = ""  # convert_from_bytes will use system poppler if None/empty

        status_text.text(f"Started processing {total} URLs...")

        # Use ThreadPoolExecutor and as_completed to update progress live
        futures = {}
        max_workers_to_use = min(max_workers, DEFAULT_MAX_WORKERS, total) if total > 0 else 1
        with ThreadPoolExecutor(max_workers=max_workers_to_use) as ex:
            for idx, url in enumerate(lines):
                futures[ex.submit(process_single, idx, url, used_poppler_path)] = idx

            for future in as_completed(futures):
                idx_of = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"index": idx_of, "url": lines[idx_of], "raw": "", "trimmed": "N/A", "error": str(e)}
                st.session_state["results"][idx_of] = result
                st.session_state["processed"] += 1
                # update UI
                progress_val = int((st.session_state["processed"] / st.session_state["total"]) * 100)
                progress_bar.progress(min(progress_val, 100))
                status_text.text(f"Processing {st.session_state['processed']}/{st.session_state['total']}")
                # refresh table every N items or every item (small lists ok)
                # build a table-friendly list:
                display_rows = []
                for r in st.session_state["results"]:
                    if not r:
                        display_rows.append({"index": "", "url": "", "raw": "", "trimmed": "", "error": ""})
                    else:
                        display_rows.append(r)
                table_area.table(display_rows)

        st.session_state["running"] = False
        status_text.text("Completed")

# If results present show actions and download
if st.session_state.get("results"):
    st.markdown("### Results")
    # Show table (DataFrame-like)
    display_rows = []
    for idx, r in enumerate(st.session_state["results"]):
        if not r:
            display_rows.append({"index": idx, "url": "", "raw": "", "trimmed": "N/A", "error": "Pending"})
        else:
            display_rows.append(r)
    table_area.table(display_rows)

    # Trimmed list as text for easy copy
    trimmed_list = []
    for r in st.session_state["results"]:
        if not r:
            trimmed_list.append("N/A")
        else:
            val = r.get("trimmed")
            trimmed_list.append(val if val not in ("", None) else "N/A")
    trimmed_text = "\n".join(trimmed_list)

    st.download_button("Tải CSV kết quả", data="\n".join([
        ",".join(["index", "url", "raw", "trimmed", "error"])
    ] + [
        ",".join([
            str(r.get("index", "")),
            '"' + (r.get("url", "").replace('"', '""')) + '"',
            '"' + (r.get("raw", "").replace('"', '""')) + '"',
            '"' + (r.get("trimmed", "").replace('"', '""')) + '"',
            '"' + (r.get("error", "").replace('"', '""')) + '"'
        ]) for r in st.session_state["results"]
    ]), file_name="results.csv", mime="text/csv")

    # Provide trimmed text area for copy/paste
    st.text_area("Trimmed list (mỗi dòng tương ứng 1 URL) — copy manually", value=trimmed_text, height=200)

st.markdown("---")
st.caption("Ghi chú: mỗi session Streamlit được tách biệt — không dùng file cục bộ chung hoặc biến global để tránh 'đụng dữ liệu'.")
