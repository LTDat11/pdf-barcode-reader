import os
import sys
import re
import requests
from io import BytesIO
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode
from PIL import Image
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

# ---------- Cấu hình ----------
TRIM_FROM = 8
DEFAULT_MAX_WORKERS = 6
REQUEST_TIMEOUT = 30


# ---------- Helpers ----------
def get_poppler_path() -> str | None:
    """Trả về None khi chạy trên Streamlit Cloud (đã cài poppler system-wide)."""
    base = os.path.abspath(os.path.dirname(__file__))
    poppler_dir = os.path.join(base, "poppler_bin")
    if os.path.exists(poppler_dir):
        return poppler_dir
    return None


def normalize_drive_url(url: str) -> str:
    """Chuẩn hóa link Google Drive sang link tải trực tiếp (direct download)."""
    # Loại bỏ các đoạn query hoặc tham số thừa
    url = url.strip()

    # --- Dạng: https://drive.google.com/file/d/<id>/view hoặc /edit hoặc không có gì sau id
    match = re.search(r"drive\.google\.com/file/d/([^/?]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # --- Dạng: https://drive.google.com/open?id=<id>
    match = re.search(r"drive\.google\.com/open\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # --- Dạng: https://drive.google.com/uc?id=<id>
    match = re.search(r"drive\.google\.com/uc\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # Không phải link Drive -> giữ nguyên
    return url



def extract_tracking_from_pdf_bytes(pdf_bytes: bytes, poppler_path: str | None) -> List[str]:
    """Chuyển PDF -> ảnh -> decode barcode trên mỗi trang."""
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300, poppler_path=poppler_path)
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
            continue
    return found


def process_single(idx: int, url: str, poppler_path: str | None) -> Dict:
    """Tải PDF từ URL (hỗ trợ link Drive) và đọc barcode."""
    try:
        url = normalize_drive_url(url)
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


# ---------- Streamlit UI ----------
st.set_page_config(page_title="PDF Barcode Batch Reader", layout="wide")
st.title("📦 PDF Barcode Batch Reader — Extract & Trim")
st.markdown("Dán danh sách **URL PDF hoặc link Google Drive** (mỗi link 1 dòng) để trích xuất mã vạch.")

# Khởi tạo session state
if "results" not in st.session_state:
    st.session_state["results"] = []
    st.session_state["total"] = 0
    st.session_state["processed"] = 0
    st.session_state["urls"] = []
    st.session_state["running"] = False

# --- Giao diện chính ---
urls_text = st.text_area(
    "URLs (mỗi link 1 dòng)",
    height=220,
    value="\n".join(st.session_state.get("urls", []))
)

max_workers = st.number_input(
    "Max workers (threads)",
    min_value=1,
    max_value=32,
    value=DEFAULT_MAX_WORKERS,
    step=1
)

col_btn1, col_btn2 = st.columns([1, 1])
with col_btn1:
    start_btn = st.button("🚀 Start processing", disabled=st.session_state["running"])
with col_btn2:
    refresh_btn = st.button("🔄 Refresh / Reset session")

progress_bar = st.progress(0)
status_text = st.empty()
table_area = st.empty()

# --- Reset session ---
if refresh_btn:
    st.session_state["results"] = []
    st.session_state["total"] = 0
    st.session_state["processed"] = 0
    st.session_state["urls"] = []
    st.session_state["running"] = False
    progress_bar.progress(0)
    status_text.text("Idle")
    st.rerun()

# --- Start processing ---
if start_btn:

    # --- Hiển thị popup QR giả lập ---
    st.session_state["show_qr"] = True

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
        status_text.text(f"Started processing {total} URLs...")

        futures = {}
        max_workers_to_use = min(max_workers, DEFAULT_MAX_WORKERS, total) if total > 0 else 1
        with ThreadPoolExecutor(max_workers=max_workers_to_use) as ex:
            for idx, url in enumerate(lines):
                futures[ex.submit(process_single, idx, url, poppler_path)] = idx

            for future in as_completed(futures):
                idx_of = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"index": idx_of, "url": lines[idx_of], "raw": "", "trimmed": "N/A", "error": str(e)}
                st.session_state["results"][idx_of] = result
                st.session_state["processed"] += 1
                progress_val = int((st.session_state["processed"] / st.session_state["total"]) * 100)
                progress_bar.progress(min(progress_val, 100))
                status_text.text(f"Processing {st.session_state['processed']}/{st.session_state['total']}")
                display_rows = [r if r else {"index": "", "url": "", "raw": "", "trimmed": "", "error": ""} for r in st.session_state["results"]]
                table_area.table(display_rows)

        st.session_state["running"] = False
        status_text.text("✅ Completed")


# --- Hiển thị popup QR giả lập ---
if st.session_state.get("show_qr", False):
    popup_html = """
    <style>
        .popup-overlay {
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            background-color: rgba(0,0,0,0.6);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 9999;
        }
        .popup-box {
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
            text-align: center;
            max-width: 350px;
            width: 90%;
            position: relative;
        }
        .popup-box img {
            max-width: 220px;
            border-radius: 8px;
        }
        .popup-close {
            background-color: #ff4b4b;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            margin-top: 10px;
        }
        .popup-close:hover {
            background-color: #e63b3b;
        }
    </style>

    <div class="popup-overlay">
        <div class="popup-box">
            <h3>🍩 Ủng hộ tôi - Donut Time!</h3>
            <p>Nếu công cụ này giúp ích cho bạn,<br>hãy ủng hộ tôi một chiếc donut ☕🍩</p>
            <img src="qrcode/qrcode.jpg" alt="QR Donate">
            <p>Scan để ủng hộ 💗</p>
            <form action="" method="get">
                <button class="popup-close" type="submit">Đóng</button>
            </form>
        </div>
    </div>
    """
    st.markdown(popup_html, unsafe_allow_html=True)
    st.session_state["show_qr"] = False

# --- Hiển thị kết quả ---
if st.session_state.get("results"):
    st.markdown("### 📋 Results")
    display_rows = [r if r else {"index": idx, "url": "", "raw": "", "trimmed": "N/A", "error": "Pending"} for idx, r in enumerate(st.session_state["results"])]
    table_area.table(display_rows)

    trimmed_list = [r.get("trimmed", "N/A") if r else "N/A" for r in st.session_state["results"]]
    trimmed_text = "\n".join(trimmed_list)

    csv_data = "\n".join([",".join(["index", "url", "raw", "trimmed", "error"])] + [
        ",".join([
            str(r.get("index", "")),
            '"' + (r.get("url", "").replace('"', '""')) + '"',
            '"' + (r.get("raw", "").replace('"', '""')) + '"',
            '"' + (r.get("trimmed", "").replace('"', '""')) + '"',
            '"' + (r.get("error", "").replace('"', '""')) + '"'
        ]) for r in st.session_state["results"]
    ])

    st.download_button("💾 Tải CSV kết quả", data=csv_data, file_name="results.csv", mime="text/csv")
    st.text_area("Trimmed list (mỗi dòng tương ứng 1 URL)", value=trimmed_text, height=200)
