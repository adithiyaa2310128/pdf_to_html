import os
import fitz  # PyMuPDF

MAX_FILE_SIZE_MB = 100
MAX_PAGE_COUNT = 51

def validate_pdf(file_path):
    errors = []

    # File size check
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        errors.append(f"File size exceeds {MAX_FILE_SIZE_MB} MB")

    # Page count check
    try:
        doc = fitz.open(file_path)
        if len(doc) > MAX_PAGE_COUNT:
            errors.append(f"PDF has more than {MAX_PAGE_COUNT} pages")

        # Digital format check: look for any actual text
        is_digital = False
        for page in doc:
            text = page.get_text("text")
            if text.strip():
                is_digital = True
                break
        if not is_digital:
            errors.append("PDF appears to be scanned or image-based (no selectable text found)")

        doc.close()
    except Exception as e:
        errors.append(f"Failed to open PDF: {str(e)}")

    return errors
