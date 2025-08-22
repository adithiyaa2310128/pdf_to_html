    from flask import Flask, request, render_template_string, render_template, jsonify
    import fitz  # PyMuPDF
    import pdfplumber
    import os
    import html
    import re
    import time
    import base64
    import threading
    import uuid
    from validation import validate_pdf
    from io import BytesIO
    from fontTools.ttLib import TTFont
    from bs4 import BeautifulSoup
    import json

    app = Flask(__name__)
    UPLOAD_FOLDER = "uploads"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # Store conversion progress
    conversion_progress = {}

    # ---------- HTML → JSON HELPER ----------
    def html_to_json(html_content, json_path):
        soup = BeautifulSoup(html_content, "html.parser")
        pages_data = []

        for page_idx, page_div in enumerate(soup.select(".page-container"), start=1):
            page_obj = {
                "page_number": page_idx,
                "elements": []
            }

            # Extract positioned text
            for text_div in page_div.select(".positioned-text"):
                style = text_div.get("style", "")
                page_obj["elements"].append({
                    "type": "text",
                    "text": text_div.get_text(),
                    "style": style
                })

            # Extract positioned images
            for img_div in page_div.select(".positioned-image"):
                img_tag = img_div.find("img")
                if img_tag:
                    style = img_div.get("style", "")
                    src = img_tag.get("src", "")
                    page_obj["elements"].append({
                        "type": "image",
                        "style": style,
                        "src": src
                    })

            # Extract tables
            for table in page_div.find_all("table"):
                rows = []
                for tr in table.find_all("tr"):
                    row_data = []
                    for td in tr.find_all("td"):
                        cell_data = {
                            "text": td.get_text(strip=True),
                            "rowspan": td.get("rowspan"),
                            "colspan": td.get("colspan")
                        }
                        row_data.append(cell_data)
                    rows.append(row_data)
                page_obj["elements"].append({
                    "type": "table",
                    "rows": rows
                })

            pages_data.append(page_obj)

        json_data = {
            "document": {
                "pages": pages_data
            }
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=4, ensure_ascii=False)

        return json_data

    # ---------- FONT EXTRACTION ----------
    def extract_fonts_as_css(pdf_doc):
        css_rules = []
        seen_fonts = {}
        try:
            for page in pdf_doc:
                try:
                    fonts = page.get_fonts(full=True)
                except Exception as e:
                    print(f"[Font list error on page {page.number}]: {e}")
                    continue

                for font in fonts:
                    xref = font[0]
                    internal_name = font[3]
                    if not internal_name or internal_name in seen_fonts:
                        continue
                    try:
                        ext, font_bytes, font_desc, font_name = pdf_doc.extract_font(xref)
                        display_name = internal_name
                        if isinstance(font_bytes, (bytes, bytearray)) and font_bytes:
                            try:
                                font_file = BytesIO(font_bytes)
                                tt = TTFont(font_file)
                                name_record = tt['name'].getName(4, 3, 1, 1033) or tt['name'].getName(4, 1, 0, 0)
                                if name_record:
                                    display_name = str(name_record)
                            except Exception as e:
                                print(f"[FontTools Error] Could not read full name for {internal_name}: {e}")
                        else:
                            print(f"[Font Extraction Warning] No embedded data for {internal_name}")

                        normalized_display_name = re.sub(
                            r'[, \-](bold|italic|oblique)', '', display_name, flags=re.IGNORECASE
                        ).strip()
                        seen_fonts[internal_name] = normalized_display_name

                        if isinstance(font_bytes, (bytes, bytearray)) and font_bytes:
                            mime_type = "font/woff" if ext.lower() == "woff" else "font/ttf"
                            b64_font = base64.b64encode(font_bytes).decode("utf-8")
                            css_rules.append(f"""
                                @font-face {{
                                    font-family: '{normalized_display_name}';
                                    src: url(data:{mime_type};base64,{b64_font}) format('{ext.lower()}');
                                }}
                            """)
                    except Exception as e:
                        print(f"[Font Extraction Error] {internal_name}: {e}")

        except Exception as e:
            print(f"[extract_fonts_as_css Error]: {e}")
        return "\n".join(css_rules), seen_fonts

    # ---------- PDF CONVERSION ----------
    def convert_pdf_with_progress(filename, job_id):
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

            # Convert entire PDF to base64
            with open(filename, 'rb') as pdf_file:
                pdf_base64 = base64.b64encode(pdf_file.read()).decode('utf-8')
                conversion_progress[job_id]['pdf_base64'] = pdf_base64

            pdf_doc = fitz.open(filename)
            pdf_plumber = pdfplumber.open(filename)
            total_pages = len(pdf_doc)

            font_css, font_name_map = extract_fonts_as_css(pdf_doc)
            conversion_progress[job_id]['progress'] = 5

            for page_num, (page_mupdf, page_plumber) in enumerate(zip(pdf_doc, pdf_plumber.pages)):
                # Update progress
                page_progress = int(5 + (page_num / total_pages) * 85)  # 5-90% for page processing
                conversion_progress[job_id]['progress'] = page_progress
                conversion_progress[job_id]['message'] = f'Processing page {page_num + 1} of {total_pages}...'

                page_width = page_mupdf.rect.width
                page_height = page_mupdf.rect.height
                scale = target_width / page_width
                elements = []

                # Detect tables
                tables = page_plumber.find_tables()
                table_bboxes = [table.bbox for table in tables]

                def is_within_table(x0, y0, x1, y1):
                    for tb in table_bboxes:
                        tx0, ty0, tx1, ty1 = tb
                        if (x0 >= tx0 and x1 <= tx1 and y0 >= ty0 and y1 <= ty1):
                            return True
                    return False

                # Find max text width for padding calc
                blocks = page_mupdf.get_text("dict")["blocks"]
                max_text_width = 0
                for block in blocks:
                    if block["type"] != 0:
                        continue
                    for line in block["lines"]:
                        for span in line["spans"]:
                            text_width = span["bbox"][2] - span["bbox"][0]
                            if text_width > max_text_width:
                                max_text_width = text_width

                # Text extraction with real fonts
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

                            internal_font = span.get("font", "Arial")

                            # Match to extracted font map considering original/raw names
                            font_family = font_name_map.get(internal_font, internal_font)

                            # As a backup, also try the normalized name in map
                            if not font_name_map.get(internal_font, None):
                                norm_name = re.sub(r'[, \-](bold|italic|oblique)', '', internal_font, flags=re.IGNORECASE).strip()
                                font_family = font_name_map.get(norm_name, norm_name)

                            # Now use font_family for CSS


                            # Detect bold/italic from original internal name
                            is_bold = "bold" in internal_font.lower()
                            is_italic = "italic" in internal_font.lower() or "oblique" in internal_font.lower()

                            LEFT_PADDING = 0.8
                            RIGHT_PADDING = max(1.0, (page_width - max_text_width) * scale / 50)
                            left = round(x0 * scale, 1) + LEFT_PADDING
                            right = round((page_width - x1) * scale, 1) + RIGHT_PADDING

                            # Preserve URLs inside text
                            text = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank">\1</a>', text)

                            style = (
                                f"top: {round(y0 * scale, 1)}px; "
                                f"left: {left}px; right: {right}px; "
                                f"font-size: {font_size:.2f}px; "
                                f"color: {font_color}; "
                                f"font-family: '{font_family}', Arial, sans-serif; "
                                f"{'font-weight: bold;' if is_bold else ''}"
                                f"{'font-style: italic;' if is_italic else ''}"
                                f"white-space: pre;"
                            )


                            elements.append(f'<div class="positioned-text" style="{style}">{text}</div>')

                # Images
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


                # Process tables (unchanged)
                for table in page_plumber.find_tables():
                    if not table.cells:
                        continue
                    x0, top, x1, bottom = table.bbox
                    width = (x1 - x0) * scale
                    height = (bottom - top) * scale
                    top_scaled = top * scale
                    left_scaled = x0 * scale

                    table_data = table.extract()
                    if not table_data:
                        continue

                    rows = len(table_data)
                    cols = max(len(row) for row in table_data) if table_data else 0
                    grid = [[None for _ in range(cols)] for _ in range(rows)]
                    occupied = set()

                    for row_idx, row in enumerate(table_data):
                        col_idx = 0
                        for cell_idx, cell in enumerate(row):
                            while (row_idx, col_idx) in occupied:
                                col_idx += 1
                            if col_idx >= cols:
                                break
                            if cell is None:
                                continue

                            rowspan = 1
                            colspan = 1

                            if cell_idx < len(row) - 1 and row[cell_idx + 1] is None:
                                next_col = col_idx + 1
                                while next_col < cols and (row_idx, next_col) not in occupied and (next_col >= len(row) or row[next_col] is None):
                                    colspan += 1
                                    next_col += 1

                            if row_idx < len(table_data) - 1 and len(table_data[row_idx + 1]) > col_idx and table_data[row_idx + 1][col_idx] is None:
                                next_row = row_idx + 1
                                while next_row < rows and (next_row, col_idx) not in occupied and (col_idx >= len(table_data[next_row]) or table_data[next_row][col_idx] is None):
                                    rowspan += 1
                                    next_row += 1

                            for r in range(row_idx, row_idx + rowspan):
                                for c in range(col_idx, col_idx + colspan):
                                    if r < rows and c < cols:
                                        occupied.add((r, c))

                            grid[row_idx][col_idx] = {
                                'content': html.escape(cell or ""),
                                'rowspan': rowspan if rowspan > 1 else None,
                                'colspan': colspan if colspan > 1 else None
                            }
                            col_idx += colspan

                    table_html = "<table>"
                    for r_idx in range(rows):
                        table_html += "<tr>"
                        for c_idx in range(cols):
                            if (r_idx, c_idx) in occupied and grid[r_idx][c_idx] is None:
                                continue
                            cell = grid[r_idx][c_idx]
                            if cell:
                                attributes = ""
                                if cell['rowspan']:
                                    attributes += f' rowspan="{cell["rowspan"]}"'
                                if cell['colspan']:
                                    attributes += f' colspan="{cell["colspan"]}"'
                                table_html += f"<td{attributes}>{cell['content']}</td>"
                            else:
                                table_html += "<td></td>"
                        table_html += "</tr>"
                    table_html += "</table>"

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
            print(font_css)
            # Create comparison view HTML
            final_html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>PDF to HTML Comparison</title>
                <style>
                    {font_css}
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
                </style>
                <script>
                    const t0 = performance.now();
                    window.onload = () => {{
                        const t1 = performance.now();
                        document.getElementById("render-time").innerText = (t1 - t0).toFixed(2) + " ms";
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

            clean_html = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="utf-8">
                        <title>Converted PDF (Clean)</title>
                        <style>
                            {font_css}
                            body {{
                                font-family: Arial, sans-serif;
                                margin: 0;
                                padding: 20px;
                                background: #f5f5f5;
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
                        </style>
                    </head>
                    <body>
                        {full_html}
                    </body>
                    </html>
                    """

            output_dir = "output"
            os.makedirs(output_dir, exist_ok=True)
            output_filename = f"{job_id}.html"
            output_path = os.path.join(output_dir, output_filename)


            with open(output_path, "w", encoding="utf-8") as f:
                f.write(clean_html)
            # Save JSON alongside HTML
            json_path = os.path.join(output_dir, f"{job_id}.json")

            
            html_to_json(clean_html, json_path)
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


    @app.route('/convert', methods=['POST'])
    def convert_pdf():
        uploaded_file = request.files['pdf']
        filename = os.path.join(UPLOAD_FOLDER, uploaded_file.filename)
        uploaded_file.save(filename)
        errors = validate_pdf(filename)
        if errors:
            return jsonify({
                'status': 'error',
                'message': ' | '.join(errors)
            }), 400  # Bad request
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

    @app.route('/edit/<job_id>', methods=['GET', 'POST'])
    def edit_html(job_id):
        output_dir = "output"
        html_file = os.path.join(output_dir, f"{job_id}.html")
        json_file = os.path.join(output_dir, f"{job_id}.json")

        if request.method == 'POST':
            edited_html = request.form['edited_html']
            # Save HTML
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(edited_html)
            # Update JSON
            html_to_json(edited_html, json_file)
            return "Changes saved! <a href='/compare/{}'>Go back</a>".format(job_id)
        
        # GET: Load HTML for editing
        if os.path.exists(html_file):
            with open(html_file, encoding="utf-8") as f:
                html_content = f.read()
        else:
            html_content = ""
        return render_template('editor.html', job_id=job_id, html_content=html_content)


    @app.route('/compare/<job_id>')
    def compare_view(job_id):
        if job_id in conversion_progress:
            progress = conversion_progress[job_id]
            if progress['status'] == 'completed':
                return render_template('compare.html',
                        pdf_base64=progress['pdf_base64'],
                        html_content=progress['result'],
                        job_id=job_id)

            else:
                return f"Conversion not completed. Status: {progress['status']}", 400
        else:
            return "Job not found", 404


    @app.route('/result/<job_id>')
    def get_result(job_id):
        if job_id in conversion_progress:
            progress = conversion_progress[job_id]
            if progress['status'] == 'completed':
                del conversion_progress[job_id]
                return render_template_string(progress['result'])
            else:
                return f"Conversion not completed. Status: {progress['status']}", 400
        else:
            return "Job not found", 404


    if __name__ == '__main__':
        app.run(debug=True)
