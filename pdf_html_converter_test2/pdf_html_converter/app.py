#contains errors




from flask import Flask, request, render_template_string, render_template, jsonify
import fitz  # PyMuPDF
import pdfplumber
import os
import html
import re
import time
import base64
from io import BytesIO
import threading
from validation import validate_pdf
import uuid
from collections import defaultdict

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Store conversion progress
conversion_progress = {}

# Map common PDF font names to CSS-friendly web font stacks
FONT_MAP = {
    # Serif Fonts
    'timesnewroman': '"Times New Roman", serif',
    'times': '"Times New Roman", serif',
    'georgia': 'Georgia, serif',
    'cambria': 'Cambria, serif',
    'garamond': 'Garamond, serif',

    # Sans-serif Fonts
    'arial': 'Arial, sans-serif',
    'arialmt': '"Arial MT", Arial, Helvetica, sans-serif',
    'calibri': 'Calibri, sans-serif',
    'helvetica': 'Helvetica, sans-serif',
    'tahoma': 'Tahoma, sans-serif',
    'verdana': 'Verdana, sans-serif',
    'segoeui': '"Segoe UI", sans-serif',
    'sourcesans': '"Source Sans Pro", Arial, sans-serif',
    'opensans': '"Open Sans", sans-serif',
    'roboto': 'Roboto, sans-serif',

    # Monospace Fonts
    'courier': 'Courier, monospace',
    'couriernew': '"Courier New", monospace',
    'consolas': 'Consolas, monospace',
    'monaco': 'Monaco, monospace',

    # Decorative / Others
    'impact': 'Impact, sans-serif',
    'comic': '"Comic Sans MS", cursive, sans-serif',
    'lucida': '"Lucida Console", monospace',

    # Fallback
    'default': 'Arial, sans-serif'
}


def get_cell_background_color(page_mupdf, cell_bbox):
    """Extract background color from a cell area"""
    try:
        # Get drawings and vector graphics in the cell area
        drawings = page_mupdf.get_drawings()
        for drawing in drawings:
            rect = drawing.get('rect', fitz.Rect())
            if rect and cell_bbox:
                # Check if the drawing overlaps with the cell
                cell_rect = fitz.Rect(cell_bbox)
                if rect.intersects(cell_rect):
                    # Check for fill color
                    fill = drawing.get('fill')
                    if fill:
                        # Convert color to hex
                        if isinstance(fill, (list, tuple)) and len(fill) >= 3:
                            r, g, b = [int(c * 255) for c in fill[:3]]
                            return f"#{r:02x}{g:02x}{b:02x}"
    except:
        pass
    return None


def process_table_with_spans(table, page_mupdf, scale):
    """Process table with proper rowspan and colspan handling"""
    if not table.cells:
        return ""

    try:
        # Get the table data first
        table_data = table.extract()
        if not table_data:
            return ""

        # Get table bbox for color extraction
        table_bbox = table.bbox

        # For more advanced span detection, we need to analyze the table structure
        # This is a simplified version that handles basic cases

        # Check if we can access cell information properly
        cells_info = []
        if hasattr(table, 'cells') and table.cells:
            for cell_bbox in table.cells:
                if isinstance(cell_bbox, (tuple, list)) and len(cell_bbox) >= 4:
                    cells_info.append(cell_bbox)

        # Build HTML table
        table_html = '<table style="width: 100%; height: 100%; border-collapse: collapse;">'

        for row_idx, row in enumerate(table_data):
            table_html += '<tr>'
            for col_idx, cell_content in enumerate(row):
                content = html.escape(cell_content or "")

                # Try to get cell background color
                bg_color = None
                if cells_info and (row_idx * len(row) + col_idx) < len(cells_info):
                    try:
                        cell_bbox = cells_info[row_idx * len(row) + col_idx]
                        bg_color = get_cell_background_color(page_mupdf, cell_bbox)
                    except:
                        pass

                bg_style = f"background-color: {bg_color};" if bg_color else ""

                # For now, we'll detect basic merged cells by looking for empty cells
                # and checking if the previous cell should span
                rowspan = 1
                colspan = 1

                # Simple merge detection: if next cells in row are empty, extend colspan
                if col_idx < len(row) - 1:
                    next_cells_empty = 0
                    for next_col in range(col_idx + 1, len(row)):
                        if not row[next_col] or row[next_col].strip() == "":
                            next_cells_empty += 1
                        else:
                            break

                    # If current cell has content and next cells are empty, span them
                    if content.strip() and next_cells_empty > 0:
                        colspan = 1 + next_cells_empty

                # Build cell attributes
                cell_attrs = []
                if rowspan > 1:
                    cell_attrs.append(f'rowspan="{rowspan}"')
                if colspan > 1:
                    cell_attrs.append(f'colspan="{colspan}"')

                attrs_str = ' ' + ' '.join(cell_attrs) if cell_attrs else ''

                # Skip rendering if this cell is part of a previous cell's span
                if col_idx > 0:
                    # Check if previous cell in this row has content and current is empty
                    prev_content = row[col_idx - 1] if col_idx - 1 >= 0 else ""
                    if not content.strip() and prev_content and prev_content.strip():
                        # This cell is likely part of the previous cell's span
                        continue

                table_html += f'<td{attrs_str} style="border: 1px solid #000; padding: 4px; vertical-align: top; font-size: 12px; {bg_style}">{content}</td>'

            table_html += '</tr>'

        table_html += '</table>'
        return table_html

    except Exception as e:
        # Fallback to simple table if advanced processing fails
        table_data = table.extract()
        table_html = '<table style="width: 100%; height: 100%; border-collapse: collapse;">'

        for row in table_data:
            table_html += '<tr>'
            for cell in row:
                content = html.escape(cell or "")
                table_html += f'<td style="border: 1px solid #000; padding: 4px; vertical-align: top; font-size: 12px;">{content}</td>'
            table_html += '</tr>'

        table_html += '</table>'
        return table_html


def convert_pdf_with_progress(filename, job_id):
    """Convert PDF with progress tracking"""
    try:
        conversion_progress[job_id] = {
            'status': 'starting',
            'progress': 0,
            'message': 'Initializing conversion...',
            'result': None,
            'error': None,
            'pdf_base64': None
        }

        start_time = time.time()
        target_width = 960
        full_html = ""

        # Convert entire PDF to base64 for display
        conversion_progress[job_id]['message'] = 'Preparing PDF for display...'
        with open(filename, 'rb') as pdf_file:
            pdf_base64 = base64.b64encode(pdf_file.read()).decode('utf-8')
            conversion_progress[job_id]['pdf_base64'] = pdf_base64

        # Open PDF documents
        conversion_progress[job_id]['message'] = 'Opening PDF document...'
        pdf_doc = fitz.open(filename)
        pdf_plumber = pdfplumber.open(filename)
        total_pages = len(pdf_doc)

        conversion_progress[job_id]['progress'] = 5
        conversion_progress[job_id]['message'] = f'Processing {total_pages} pages...'

        for page_num, (page_mupdf, page_plumber) in enumerate(zip(pdf_doc, pdf_plumber.pages)):
            # Update progress
            page_progress = int(5 + (page_num / total_pages) * 85)  # 5-90% for page processing
            conversion_progress[job_id]['progress'] = page_progress
            conversion_progress[job_id]['message'] = f'Processing page {page_num + 1} of {total_pages}...'

            page_width = page_mupdf.rect.width
            page_height = page_mupdf.rect.height
            scale = target_width / page_width
            elements = []

            # Get tables with enhanced processing
            tables = page_plumber.find_tables()
            table_bboxes = [table.bbox for table in tables]

            def is_within_table(x0, y0, x1, y1):
                for tb in table_bboxes:
                    tx0, ty0, tx1, ty1 = tb
                    if (x0 >= tx0 and x1 <= tx1 and y0 >= ty0 and y1 <= ty1):
                        return True
                return False

            # Process text blocks
            blocks = page_mupdf.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        x0, y0, x1, y1 = span["bbox"]
                        if is_within_table(x0, y0, x1, y1):
                            continue
                        text = html.escape(span["text"])
                        if not text.strip():
                            continue
                        font_size = round(span["size"] * scale, 1)
                        font_color = "#{:06x}".format(span["color"])
                        font_name = span.get("font", "").lower()
                        is_bold = "bold" in font_name
                        is_italic = "italic" in font_name or "oblique" in font_name

                        # Extract base font name
                        font_base = re.sub(r'[^a-zA-Z]', '', font_name.split(',')[0]).lower()

                        if font_base:
                            font_base = font_base[0]
                        else:
                            font_base = 'default'
                        font_family = FONT_MAP.get(font_base, FONT_MAP['default'])

                        left = round(x0 * scale, 1)
                        top = round(y0 * scale, 1)

                        text = re.sub(r'(https?://[^"]+)', r'<a href="\1" target="_blank">\1</a>', text)

                        style = (
                            f"top: {top}px; left: {left}px; font-size: {font_size}px; "
                            f"color: {font_color}; font-family: {font_family}; "
                            f"{'font-weight: bold;' if is_bold else ''}"
                            f"{'font-style: italic;' if is_italic else ''}"
                        )

                        elements.append(f'<div class="positioned-text" style="{style}">{text}</div>')

            # Process images
            for img_index, img in enumerate(page_mupdf.get_images(full=True)):
                xref = img[0]
                pix = fitz.Pixmap(pdf_doc, xref)
                if pix.n >= 5:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
                img_base64 = base64.b64encode(img_bytes).decode('utf-8')

                rects = page_mupdf.get_image_rects(xref)
                for rect in rects:
                    left = round(rect.x0 * scale, 1)
                    top = round(rect.y0 * scale, 1)
                    width = round((rect.x1 - rect.x0) * scale, 1)
                    height = round((rect.y1 - rect.y0) * scale, 1)

                    elements.append(f'''
                        <div class="positioned-image" style="top: {top}px; left: {left}px; width: {width}px; height: {height}px;">
                            <img src="data:image/png;base64,{img_base64}" style="width: 100%; height: 100%; object-fit: contain;">
                        </div>
                    ''')

            # Process tables with enhanced span support
            for table in tables:
                if not table.cells:
                    continue

                x0, top, x1, bottom = table.bbox
                width = (x1 - x0) * scale
                height = (bottom - top) * scale
                top_scaled = top * scale
                left_scaled = x0 * scale

                # Use enhanced table processing
                table_html = process_table_with_spans(table, page_mupdf, scale)

                elements.append(f"""
                <div style="position: absolute; top: {top_scaled}px; left: {left_scaled}px;
                            width: {width}px; height: {height}px;">
                    {table_html}
                </div>
                """)

            full_html += f'''
            <div class="page-container" style="height: {int(page_height * scale)}px;">
                {"".join(elements)}
            </div>
            '''

        # Finalizing
        conversion_progress[job_id]['progress'] = 95
        conversion_progress[job_id]['message'] = 'Finalizing HTML output...'

        pdf_plumber.close()
        conversion_time = round(time.time() - start_time, 2)

        # Create comparison view HTML
        final_html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>PDF to HTML Comparison</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 0;
                    background: #f5f5f5;
                }}
                .header {{
                    padding: 15px;
                    background-color: #2c3e50;
                    color: white;
                    text-align: center;
                    font-size: 18px;
                    font-weight: bold;
                }}
                .info-bar {{
                    padding: 12px;
                    font-size: 14px;
                    background-color: #ecf0f1;
                    border-bottom: 1px solid #bdc3c7;
                    font-family: monospace;
                    text-align: center;
                }}
                .comparison-container {{
                    display: flex;
                    height: calc(100vh - 120px);
                }}
                .pdf-panel, .html-panel {{
                    width: 50%;
                    border: 2px solid #34495e;
                    overflow: auto;
                }}
                .panel-header {{
                    background-color: #34495e;
                    color: white;
                    padding: 10px;
                    text-align: center;
                    font-weight: bold;
                    position: sticky;
                    top: 0;
                    z-index: 100;
                }}
                .pdf-content {{
                    padding: 20px;
                    text-align: center;
                    background: white;
                }}
                .pdf-embed {{
                    width: 100%;
                    height: 800px;
                    border: none;
                }}
                .html-content {{
                    background: #eee;
                    min-height: 100%;
                }}
                .page-container {{
                    position: relative;
                    margin: 30px auto;
                    background: white;
                    border: 1px solid #ccc;
                    box-shadow: 0 0 10px rgba(0,0,0,0.1);
                    width: {target_width}px;
                }}
                .positioned-text {{
                    position: absolute;
                    white-space: pre;
                    text-decoration: none;
                }}
                .positioned-text a {{
                    color: blue;
                    text-decoration: underline;
                }}
                .positioned-image {{
                    position: absolute;
                    object-fit: contain;
                }}
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    height: 100%;
                }}
                table td {{
                    border: 1px solid #000;
                    padding: 4px;
                    vertical-align: top;
                    font-size: 12px;
                }}
                .controls {{
                    position: fixed;
                    top: 50%;
                    left: 50%;
                    transform: translateX(-50%);
                    background: rgba(52, 73, 94, 0.9);
                    color: white;
                    padding: 10px 20px;
                    border-radius: 5px;
                    z-index: 1000;
                    font-size: 12px;
                }}
            </style>
            <script>
                const t0 = performance.now();
                window.onload = () => {{
                    const t1 = performance.now();
                    document.getElementById("render-time").innerText = (t1 - t0).toFixed(2) + " ms";

                    // Sync scrolling between panels
                    const pdfPanel = document.querySelector('.pdf-panel');
                    const htmlPanel = document.querySelector('.html-panel');

                    let isScrollingPdf = false;
                    let isScrollingHtml = false;

                    pdfPanel.addEventListener('scroll', () => {{
                        if (isScrollingHtml) return;
                        isScrollingPdf = true;
                        const ratio = pdfPanel.scrollTop / (pdfPanel.scrollHeight - pdfPanel.clientHeight);
                        htmlPanel.scrollTop = ratio * (htmlPanel.scrollHeight - htmlPanel.clientHeight);
                        setTimeout(() => isScrollingPdf = false, 50);
                    }});

                    htmlPanel.addEventListener('scroll', () => {{
                        if (isScrollingPdf) return;
                        isScrollingHtml = true;
                        const ratio = htmlPanel.scrollTop / (htmlPanel.scrollHeight - htmlPanel.clientHeight);
                        pdfPanel.scrollTop = ratio * (pdfPanel.scrollHeight - pdfPanel.clientHeight);
                        setTimeout(() => isScrollingHtml = false, 50);
                    }});
                }};
            </script>
        </head>
        <body>
            <div class="header">PDF to HTML Conversion Comparison</div>
            <div class="info-bar">
                <b>Conversion time:</b> {conversion_time} seconds |
                <b>Render time:</b> <span id="render-time">...</span> |
                <b>Pages:</b> {total_pages}
            </div>

            <div class="comparison-container">
                <div class="pdf-panel">
                    <div class="panel-header">Original PDF</div>
                    <div class="pdf-content">
                        <embed class="pdf-embed" src="data:application/pdf;base64,{pdf_base64}" type="application/pdf" />
                    </div>
                </div>

                <div class="html-panel">
                    <div class="panel-header">Converted HTML</div>
                    <div class="html-content">
                        {full_html}
                    </div>
                </div>
            </div>


        </body>
        </html>
        '''

        conversion_progress[job_id]['progress'] = 100
        conversion_progress[job_id]['status'] = 'completed'
        conversion_progress[job_id]['message'] = f'Conversion completed in {conversion_time} seconds'
        conversion_progress[job_id]['result'] = final_html

    except Exception as e:
        conversion_progress[job_id]['status'] = 'error'
        conversion_progress[job_id]['error'] = str(e)
        conversion_progress[job_id]['message'] = f'Error: {str(e)}'


@app.route('/')
def upload_form():
    return render_template('index.html')


from flask import jsonify

@app.route('/convert', methods=['POST'])
def convert_pdf():
    uploaded_file = request.files['pdf']
    filename = os.path.join(UPLOAD_FOLDER, uploaded_file.filename)
    uploaded_file.save(filename)

    # Run validation
    errors = validate_pdf(filename)
    if errors:
        return jsonify({
            'status': 'error',
            'message': ' | '.join(errors)
        }), 400  # Bad request

    # Proceed if valid
    job_id = str(uuid.uuid4())
    thread = threading.Thread(target=convert_pdf_with_progress, args=(filename, job_id))
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'ok', 'job_id': job_id})




@app.route('/progress/<job_id>')
def get_progress(job_id):
    if job_id in conversion_progress:
        return jsonify(conversion_progress[job_id])
    else:
        return jsonify({'status': 'not_found', 'error': 'Job not found'}), 404


@app.route('/compare/<job_id>')
def compare_view(job_id):
    """Render the comparison view using template"""
    if job_id in conversion_progress:
        progress = conversion_progress[job_id]
        if progress['status'] == 'completed':
            return render_template('compare.html',
                                 pdf_base64=progress['pdf_base64'],
                                 html_content=progress['result'])
        else:
            return f"Conversion not completed. Status: {progress['status']}", 400
    else:
        return "Job not found", 404


@app.route('/result/<job_id>')
def get_result(job_id):
    if job_id in conversion_progress:
        progress = conversion_progress[job_id]
        if progress['status'] == 'completed':
            # Clean up the progress data
            del conversion_progress[job_id]
            return render_template_string(progress['result'])
        else:
            return f"Conversion not completed. Status: {progress['status']}", 400
    else:
        return "Job not found", 404


if __name__ == '__main__':
    app.run(debug=True)