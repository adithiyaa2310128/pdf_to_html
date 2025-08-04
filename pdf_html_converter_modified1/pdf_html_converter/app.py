import time
from flask import Flask, request, render_template_string
import os
import pdfplumber
import html
import base64
from io import BytesIO
import re

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/')
def home():
    return '''
    <h2>Upload PDF</h2>
    <form method="POST" action="/convert" enctype="multipart/form-data">
        <input type="file" name="pdf">
        <button type="submit">Convert</button>
    </form>
    '''

@app.route('/convert', methods=['POST'])
def convert_pdf():
    start_time = time.time() 
    pdf_file = request.files['pdf']
    filepath = os.path.join(UPLOAD_FOLDER, pdf_file.filename)
    pdf_file.save(filepath)

    full_html = ""
    target_width = 960

    with pdfplumber.open(filepath) as pdf:
        for page_count, page in enumerate(pdf.pages, 1):
            elements = []

            pdf_width = page.width
            pdf_height = page.height
            scale_factor = target_width / pdf_width

            # Table bounding boxes
            table_bboxes = [table.bbox for table in page.find_tables()]

            def is_inside_table(x0, top, x1, bottom):
                for tb_x0, tb_top, tb_x1, tb_bottom in table_bboxes:
                    if (x0 >= tb_x0 and x1 <= tb_x1 and
                        top >= tb_top and bottom <= tb_bottom):
                        return True
                return False

            # Text
            for word in page.extract_words(keep_blank_chars=True, use_text_flow=True):
                x0 = word['x0']
                x1 = word['x1']
                top = word['top']
                bottom = word['bottom']

                if is_inside_table(x0, top, x1, bottom):
                    continue

                raw_text = word['text']
                escaped_text = html.escape(raw_text)

                # Wrap URLs
                url_pattern = r'(https?://[^\s]+)'
                linked_text = re.sub(url_pattern, r'<a href="\1" target="_blank">\1</a>', escaped_text)

                height = bottom - top
                font_size = round(height * scale_factor, 1)
                top_scaled = round(top * scale_factor, 1)
                left_scaled = round(x0 * scale_factor, 1)
                style = f"top: {top_scaled}px; left: {left_scaled}px; font-size: {font_size}px;"
                elements.append(f'<div class="positioned-text" style="{style}">{linked_text}</div>')

            # Tables
            for table in page.find_tables():
                if not table.cells:
                    continue
                x0, top, x1, bottom = table.bbox
                width = (x1 - x0) * scale_factor
                height = (bottom - top) * scale_factor
                top_scaled = top * scale_factor
                left_scaled = x0 * scale_factor

                table_html = "<table style='border-collapse: collapse; width: 100%; height: 100%;'>"
                for row in table.extract():
                    table_html += "<tr>"
                    for cell in row:
                        content = html.escape(cell or "")
                        table_html += f"<td style='border: 1px solid #000; padding: 2px;'>{content}</td>"
                    table_html += "</tr>"
                table_html += "</table>"

                elements.append(f"""
                <div style="position: absolute; top: {top_scaled}px; left: {left_scaled}px;
                            width: {width}px; height: {height}px;">
                    {table_html}
                </div>
                """)

            # Images
            for idx, img in enumerate(page.images):
                x0 = max(0, img['x0'])
                top = max(0, img['top'])
                x1 = min(pdf_width, img['x1'])
                bottom = min(pdf_height, img['bottom'])

                bbox = (x0, top, x1, bottom)
                try:
                    cropped = page.crop(bbox).to_image(resolution=360)
                    buffer = BytesIO()
                    cropped.save(buffer, format='PNG')
                    buffer.seek(0)
                    img_base64 = base64.b64encode(buffer.read()).decode('utf-8')

                    style = (
                        f"top: {round(top * scale_factor, 1)}px; "
                        f"left: {round(x0 * scale_factor, 1)}px; "
                        f"width: {round((x1 - x0) * scale_factor, 1)}px; "
                        f"height: {round((bottom - top) * scale_factor, 1)}px;"
                    )
                    elements.append(
                        f'<img src="data:image/png;base64,{img_base64}" class="positioned-image" style="{style}">'
                    )
                except Exception as e:
                    print(f"Image on page {page_count} skipped due to error: {e}")

            html_body = "\n".join(elements)

            page_html = f"""
            <div class="page-container" style="width: {target_width}px; height: {int(pdf_height * scale_factor)}px;">
                {html_body}
            </div>
            """
            full_html += page_html + "\n"
    end_time = time.time()
    conversion_time = round(end_time - start_time, 2)

    final_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PDF Converted</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 0;
                background: #eee;
            }}
            .page-container {{
                position: relative;
                margin: 30px auto;
                background: white;
                border: 1px solid #ccc;
                box-shadow: 0 0 10px rgba(0,0,0,0.1);
            }}
            .positioned-text {{
                position: absolute;
                white-space: pre;
                color: #000;
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
            table td {{
                vertical-align: top;
                font-size: 12px;
            }}
        </style>
        <script>
            const t0 = performance.now();
            window.onload = () => {{
                const t1 = performance.now();
                const renderTime = (t1 - t0).toFixed(2);
                document.getElementById("render-time").innerText = renderTime + " ms";
            }};
        </script>
    </head>
    <body>
        <div style="padding:10px; font-family:Arial;">
            <b>Server conversion time:</b> {conversion_time} seconds<br>
            <b>Browser render time:</b> <span id="render-time">...</span>
        </div>
        {full_html}
    </body>
    </html>
    """
    return render_template_string(final_html)

if __name__ == '__main__':
    app.run(debug=True)
