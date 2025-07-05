from flask import Flask, request, redirect, url_for, send_file
import os
import pdfplumber
import html
from PIL import Image
import re

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

@app.route('/')
def home():
    return '''
        <h2>Upload PDF to Convert</h2>
        <form action="/convert" method="post" enctype="multipart/form-data">
            <input type="file" name="pdf">
            <button type="submit">Upload</button>
        </form>
    '''

@app.route('/convert', methods=['POST'])
def convert_pdf():
    pdf_file = request.files['pdf']
    filepath = os.path.join(UPLOAD_FOLDER, pdf_file.filename)
    pdf_file.save(filepath)

    for file in os.listdir(OUTPUT_FOLDER):
        os.remove(os.path.join(OUTPUT_FOLDER, file))

    page_count = 0

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_count += 1
            elements = []

            pdf_width = page.width
            pdf_height = page.height
            target_width = 960  # Target display width (full window)
            scale_factor = target_width / pdf_width

            # Extract text with estimated font size
            for word in page.extract_words(keep_blank_chars=True, use_text_flow=True):
                text = html.escape(word['text'])
                height = word['bottom'] - word['top']
                font_size = round(height * scale_factor, 1)
                top = round(word['top'] * scale_factor, 1)
                left = round(word['x0'] * scale_factor, 1)
                style = f"top: {top}px; left: {left}px; font-size: {font_size}px;"
                elements.append(f'<div class="positioned-text" style="{style}">{text}</div>')

            # Extract and scale images
            for idx, img in enumerate(page.images):
                bbox = (img['x0'], img['top'], img['x1'], img['bottom'])
                cropped = page.crop(bbox).to_image(resolution=150)
                image_filename = f'page_{page_count}_img_{idx+1}.png'
                image_path = os.path.join(OUTPUT_FOLDER, image_filename)
                cropped.save(image_path, format='PNG')

                style = (
                    f"top: {round(img['top'] * scale_factor, 1)}px; "
                    f"left: {round(img['x0'] * scale_factor, 1)}px; "
                    f"width: {round((img['x1'] - img['x0']) * scale_factor, 1)}px; "
                    f"height: {round((img['bottom'] - img['top']) * scale_factor, 1)}px;"
                )
                elements.append(f'<img src="/outputs/{image_filename}" class="positioned-image" style="{style}">')

            html_body = "\n".join(elements)

            html_template = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Page {page_count}</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        margin: 0;
                        padding: 0;
                        overflow: auto;
                    }}
                    .page-container {{
                        position: relative;
                        width: {target_width}px;
                        height: {int(pdf_height * scale_factor)}px;
                        margin: auto;
                        background: #fff;
                    }}
                    .positioned-text {{
                        position: absolute;
                        white-space: pre;
                        color: #000;
                    }}
                    .positioned-image {{
                        position: absolute;
                        object-fit: contain;
                    }}
                </style>
            </head>
            <body>
                <div class="page-container">
                    {html_body}
                </div>
            </body>
            </html>
            """

            with open(os.path.join(OUTPUT_FOLDER, f'page_{page_count}.html'), 'w', encoding='utf-8') as f:
                f.write(html_template)

    return redirect(url_for('show_pages'))

@app.route('/pages')
def show_pages():
    query = request.args.get('q', '').strip().lower()

    def extract_page_number(filename):
        match = re.search(r'page_(\d+)\.html', filename)
        return int(match.group(1)) if match else float('inf')

    all_pages = sorted(
        [f for f in os.listdir(OUTPUT_FOLDER) if f.endswith('.html')],
        key=extract_page_number
    )

    matching_pages = []

    if query:
        for page in all_pages:
            filepath = os.path.join(OUTPUT_FOLDER, page)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().lower()
                if query in content:
                    matching_pages.append(page)
    else:
        matching_pages = all_pages

    if not matching_pages:
        results_html = f"<p>No results found for <b>{query}</b></p>"
    else:
        results_html = ''.join(
            f'<li><a href="/outputs/{page}">{page}</a></li>' for page in matching_pages
        )

    search_form = f'''
        <form method="get" action="/pages">
            <input type="text" name="q" placeholder="Search text..." value="{query}">
            <button type="submit">Search</button>
        </form>
        <br>
    '''

    return f"""
        <h2>{'Search Results' if query else 'Converted Pages'} ({len(matching_pages)} pages)</h2>
        {search_form}
        <ul>{results_html}</ul>
        <br><a href="/pages">View All</a>
    """

@app.route('/outputs/<filename>')
def show_output(filename):
    filepath = os.path.join(OUTPUT_FOLDER, filename)
    if os.path.exists(filepath):
        if filename.endswith('.html'):
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            return send_file(filepath, mimetype='image/png')
    return "File not found", 404

@app.route('/download/<filename>')
def file_download(filename):
    download=os.path.join(OUTPUT_FOLDER, filename)
    return f"<link>"

if __name__ == '__main__':
    app.run(debug=True)
