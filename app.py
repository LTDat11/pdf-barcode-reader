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
    url = url.strip()
    match = re.search(r"drive\.google\.com/file/d/([^/?]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    match = re.search(r"drive\.google\.com/open\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    match = re.search(r"drive\.google\.com/uc\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
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
st.set_page_config(page_title="PDF Barcode Batch Reader", layout="wide", initial_sidebar_state="expanded")
st.title("📦 PDF Barcode Batch Reader — Extract & Trim")
st.markdown("### Hướng dẫn sử dụng")
st.markdown("""
- Dán danh sách **URL PDF hoặc link Google Drive** (mỗi link 1 dòng) vào ô bên dưới.
- Chọn số lượng worker (threads) để xử lý song song (mặc định: 6).
- Nhấn **🚀 Start processing** để bắt đầu.
- Kết quả sẽ hiển thị dưới dạng bảng, và bạn có thể tải về CSV hoặc copy danh sách trimmed.
""")

# Khởi tạo session state
if "results" not in st.session_state:
    st.session_state["results"] = []
    st.session_state["total"] = 0
    st.session_state["processed"] = 0
    st.session_state["urls"] = []
    st.session_state["running"] = False
    st.session_state["show_donut"] = False

# --- Sidebar cho cấu hình ---
with st.sidebar:
    st.header("⚙️ Cấu hình")
    max_workers = st.number_input(
        "Max workers (threads)",
        min_value=1,
        max_value=32,
        value=DEFAULT_MAX_WORKERS,
        step=1,
        help="Số lượng luồng song song để xử lý nhanh hơn (tùy thuộc vào tài nguyên máy)."
    )
    st.markdown("---")
    st.header("ℹ️ Thông tin")
    st.markdown("Công cụ này hỗ trợ trích xuất mã vạch từ PDF vận đơn (ví dụ: mã tracking).")
    st.markdown("Nếu hữu ích, hãy ủng hộ developer một chiếc donut! 🍩")

# --- Giao diện chính ---
urls_text = st.text_area(
    "Dán URLs PDF hoặc Google Drive (mỗi link 1 dòng)",
    height=220,
    value="\n".join(st.session_state.get("urls", [])),
    help="Ví dụ: https://drive.google.com/file/d/ABC123/view"
)

col_btn1, col_btn2 = st.columns([1, 1])
with col_btn1:
    start_btn = st.button("🚀 Start processing", disabled=st.session_state["running"], type="primary")
with col_btn2:
    refresh_btn = st.button("🔄 Reset session")

progress_bar = st.progress(0)
status_text = st.empty()

# --- Reset session ---
if refresh_btn:
    st.session_state["results"] = []
    st.session_state["total"] = 0
    st.session_state["processed"] = 0
    st.session_state["urls"] = []
    st.session_state["running"] = False
    st.session_state["show_donut"] = False
    progress_bar.progress(0)
    status_text.text("Đã reset. Sẵn sàng sử dụng lại.")
    st.rerun()

# --- Start processing ---
if start_btn:
    st.session_state["show_donut"] = True  # Hiển thị thông báo donut mỗi khi bắt đầu sử dụng
    st.rerun()  # Rerun để hiển thị popup ngay lập tức

# --- Hiển thị popup donut (sử dụng expander để giả lập modal) ---
if st.session_state.get("show_donut", False):
    with st.expander("🍩 Ủng hộ tôi - Donut Time! (Mỗi lần sử dụng, hãy cân nhắc ủng hộ 💗)", expanded=True):
        st.markdown("""
        Nếu công cụ này giúp ích cho bạn, hãy ủng hộ tôi một chiếc donut ☕🍩 để duy trì và phát triển!
        """)
        # Giả sử QR code được lưu tại 'qrcode/qrcode.jpg' - bạn có thể thay bằng URL hoặc upload
        st.image("qrcode/qrcode.jpg", caption="Scan QR để ủng hộ", width=250)
        if st.button("Đóng và tiếp tục xử lý"):
            st.session_state["show_donut"] = False
            st.rerun()

# Chỉ xử lý nếu popup đã đóng (không show_donut nữa) và start_btn đã được nhấn trước đó
if start_btn and not st.session_state["show_donut"]:
    lines = [line.strip() for line in urls_text.splitlines() if line.strip()]
    st.session_state["urls"] = lines
    total = len(lines)
    if total == 0:
        status_text.text("Vui lòng dán URLs trước khi bắt đầu.")
    else:
        st.session_state["total"] = total
        st.session_state["processed"] = 0
        st.session_state["results"] = [None] * total
        st.session_state["running"] = True

        poppler_path = get_poppler_path()
        status_text.text(f"Đang xử lý {total} URLs...")

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
                progress_val = st.session_state["processed"] / st.session_state["total"]
                progress_bar.progress(progress_val)
                status_text.text(f"Đang xử lý {st.session_state['processed']}/{st.session_state['total']}")

        st.session_state["running"] = False
        status_text.text("✅ Hoàn thành xử lý!")

# --- Hiển thị kết quả ---
if st.session_state.get("results"):
    st.markdown("### 📋 Kết quả xử lý")
    display_rows = [r if r else {"index": idx, "url": "", "raw": "", "trimmed": "N/A", "error": "Đang chờ"} for idx, r in enumerate(st.session_state["results"])]
    st.dataframe(display_rows, use_container_width=True)

    trimmed_list = [r.get("trimmed", "N/A") if r else "N/A" for r in st.session_state["results"]]
    trimmed_text = "\n".join(trimmed_list)

    csv_data = "\n".join([",".join(["index", "url", "raw", "trimmed", "error"])] + [
        ",".join([
            str(r.get("index", "")),
            '"' + (r.get("url", "").replace('"', '""')) + '"',
            '"' + (r.get("raw", "").replace('"', '""')) + '"',
            '"' + (r.get("trimmed", "").replace('"', '""')) + '"',
            '"' + (r.get("error", "").replace('"', '""')) + '"'
        ]) for r in st.session_state["results"] if r
    ])

    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button("💾 Tải CSV kết quả", data=csv_data, file_name="results.csv", mime="text/csv")
    with col_dl2:
        st.text_area("Danh sách trimmed (copy-paste)", value=trimmed_text, height=200)