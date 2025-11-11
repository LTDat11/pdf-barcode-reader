import os
import sys
import re
import threading
import queue
import csv
import requests
from tkinter import Tk, Text, Button, Label, filedialog, StringVar, END, DISABLED, NORMAL, messagebox
from tkinter import ttk
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode
from PIL import Image

# ---------- Cấu hình ----------
BUNDLED_POPPLER_DIRNAME = "poppler_bin"
TRIM_FROM = 8
REQUEST_TIMEOUT = 30


# --- Nếu chạy dưới PyInstaller (frozen), thêm thư mục poppler_bin vào PATH ---
if getattr(sys, "frozen", False):
    _bundle_bin = os.path.join(sys._MEIPASS, BUNDLED_POPPLER_DIRNAME)
    os.environ["PATH"] = _bundle_bin + os.pathsep + os.environ.get("PATH", "")


# ---------- Helpers ----------
def get_poppler_path():
    """Lấy đường dẫn đến thư mục poppler_bin."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, BUNDLED_POPPLER_DIRNAME)


def normalize_drive_url(url: str) -> str:
    """Chuẩn hóa link Google Drive sang link tải trực tiếp (direct download)."""
    url = url.strip()

    # --- Dạng: https://drive.google.com/file/d/<id>/view hoặc /edit ---
    match = re.search(r"drive\.google\.com/file/d/([^/?]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # --- Dạng: https://drive.google.com/open?id=<id> ---
    match = re.search(r"drive\.google\.com/open\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # --- Dạng: https://drive.google.com/uc?id=<id> ---
    match = re.search(r"drive\.google\.com/uc\?id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # Không phải link Drive -> giữ nguyên
    return url


def extract_tracking_from_pdf_bytes(pdf_bytes, poppler_path):
    """Chuyển PDF sang ảnh rồi đọc barcode."""
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


# ---------- Worker ----------
def worker_thread(input_queue, output_list, poppler_path, progress_callback):
    """Luồng xử lý từng URL."""
    while True:
        item = input_queue.get()
        if item is None:
            input_queue.task_done()
            break
        idx, url = item
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
        output_list[idx] = result
        input_queue.task_done()
        progress_callback()


# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title("📦 PDF Barcode Batch Reader - Extract & Trim")
        root.geometry("950x620")

        Label(root, text="Dán danh sách URL PDF hoặc link Google Drive (mỗi link 1 dòng):").pack(anchor="w", padx=8, pady=(8,0))
        self.txt = Text(root, height=12)
        self.txt.pack(fill="x", padx=8)

        frame = ttk.Frame(root)
        frame.pack(fill="x", padx=8, pady=6)

        self.btn_start = Button(frame, text="🚀 Start", command=self.start_processing)
        self.btn_start.pack(side="left")

        Button(frame, text="📂 Load from file...", command=self.load_file).pack(side="left", padx=4)
        Button(frame, text="💾 Save results...", command=self.save_results).pack(side="left", padx=4)
        Button(frame, text="📋 Copy trimmed", command=self.copy_trimmed).pack(side="left", padx=4)
        Button(frame, text="🔄 Refresh", command=self.refresh_all).pack(side="left", padx=4)

        self.status_var = StringVar(value="Idle")
        Label(frame, textvariable=self.status_var).pack(side="left", padx=12)

        self.progress = ttk.Progressbar(root, length=400)
        self.progress.pack(fill="x", padx=8, pady=6)

        cols = ("index", "url", "raw", "trimmed", "error")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=12)
        for c in cols:
            self.tree.heading(c, text=c, command=lambda _col=c: self.treeview_sort_column(_col, False))
            self.tree.column(c, width=160 if c=="url" else 120, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)

        self.results = []
        self.input_queue = None
        self.total = 0
        self.processed = 0

    def treeview_sort_column(self, col, reverse):
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
        if col == "index":
            data.sort(key=lambda t: int(t[0]) if t[0].isdigit() else 999999, reverse=reverse)
        else:
            data.sort(key=lambda t: t[0], reverse=reverse)
        for index, (val, k) in enumerate(data):
            self.tree.move(k, '', index)
        self.tree.heading(col, command=lambda: self.treeview_sort_column(col, not reverse))

    def copy_trimmed(self):
        if not self.results:
            messagebox.showwarning("Warning", "Chưa có kết quả để copy!")
            return
        trimmed_list = []
        for r in self.results:
            if not r:
                trimmed_list.append("N/A")
            else:
                val = r.get("trimmed")
                trimmed_list.append(val if val not in ("", None) else "N/A")
        text_to_copy = "\n".join(trimmed_list)
        self.root.clipboard_clear()
        self.root.clipboard_append(text_to_copy)
        self.root.update()
        messagebox.showinfo("Copied", f"Đã copy {len(trimmed_list)} dòng vào clipboard.")

    def refresh_all(self):
        self.txt.delete("1.0", END)
        self.tree.delete(*self.tree.get_children())
        self.progress["value"] = 0
        self.status_var.set("Idle")
        self.results = []
        self.total = 0
        self.processed = 0

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text files","*.txt"),("All files","*.*")])
        if not path: return
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        self.txt.delete("1.0", END)
        self.txt.insert("1.0", data)

    def save_results(self):
        if not self.results:
            messagebox.showwarning("Warning", "Chưa có kết quả để lưu!")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path: return
        with open(path, "w", newline="", encoding="utf-8") as csvf:
            writer = csv.writer(csvf)
            writer.writerow(["index", "url", "raw", "trimmed", "error"])
            for idx, r in enumerate(self.results):
                if not r:
                    writer.writerow([idx, "", "", "N/A", "Pending"])
                else:
                    writer.writerow([r.get("index"), r.get("url"), r.get("raw"), r.get("trimmed"), r.get("error")])
        self.status_var.set(f"Saved {path}")

    def update_progress(self):
        self.processed += 1
        if self.total:
            self.progress["value"] = (self.processed / self.total) * 100
        self.status_var.set(f"Processing {self.processed}/{self.total}")

        for idx, r in enumerate(self.results):
            if not r:
                continue
            iid = f"r{idx}"
            if not self.tree.exists(iid):
                self.tree.insert("", "end", iid, values=(r["index"], r["url"], r["raw"], r["trimmed"], r["error"]))

        if self.processed >= self.total:
            self.btn_start.config(state=NORMAL)
            self.status_var.set("✅ Completed")

    def start_processing(self):
        text = self.txt.get("1.0", END).strip()
        if not text:
            self.status_var.set("Please paste URLs first")
            return
        urls = [line.strip() for line in text.splitlines() if line.strip()]
        self.total = len(urls)
        if self.total == 0:
            self.status_var.set("No URLs")
            return

        self.btn_start.config(state=DISABLED)
        self.tree.delete(*self.tree.get_children())
        self.progress["value"] = 0
        self.processed = 0
        self.results = [None] * self.total

        q = queue.Queue()
        for idx, url in enumerate(urls):
            q.put((idx, url))
        num_workers = min(6, max(2, os.cpu_count() or 2))
        for _ in range(num_workers):
            q.put(None)

        poppler_path = get_poppler_path()
        if not os.path.exists(poppler_path):
            self.status_var.set(f"poppler path not found: {poppler_path}")
            self.btn_start.config(state=NORMAL)
            return

        safe_progress_callback = lambda: self.root.after(0, self.update_progress)

        for _ in range(num_workers):
            t = threading.Thread(target=worker_thread, args=(q, self.results, poppler_path, safe_progress_callback), daemon=True)
            t.start()

        self.input_queue = q
        self.status_var.set("Started processing...")


# ---------- Run ----------
def main():
    root = Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
