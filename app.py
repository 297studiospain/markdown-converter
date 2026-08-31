import os
import re
import warnings
import ipaddress
import socket
from collections import OrderedDict
from io import BytesIO
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlparse

import requests
# Suppress warning from pydub regarding missing ffmpeg/avconv since audio conversion is optional
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Couldn't find ffmpeg or avconv")

from flask import Flask, request, jsonify
from markitdown import MarkItDown
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=20 * 1024 * 1024)

UPLOAD_COOLDOWN_SECONDS = 15
MAX_TRACKED_CLIENTS = 10_000
MAX_URL_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_EXTENSIONS = {
    '.docx', '.pdf', '.rtf', '.txt', '.html', '.htm', '.xlsx', '.xls',
    '.pptx', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp', '.mp3', '.wav',
}
_upload_attempts = OrderedDict()
_upload_lock = Lock()

# Initialize MarkItDown converter
markitdown = MarkItDown()


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({'error': 'El archivo supera el límite de 20 MB.'}), 413


def upload_cooldown_remaining(client_id):
    """Allow one upload per client every 15 seconds without persisting data."""
    now = monotonic()
    with _upload_lock:
        expired = [key for key, value in _upload_attempts.items()
                   if now - value >= UPLOAD_COOLDOWN_SECONDS]
        for key in expired:
            _upload_attempts.pop(key, None)
        last_attempt = _upload_attempts.get(client_id)
        if last_attempt is not None:
            remaining = UPLOAD_COOLDOWN_SECONDS - (now - last_attempt)
            if remaining > 0:
                return max(1, int(remaining + 0.999))
        _upload_attempts[client_id] = now
        _upload_attempts.move_to_end(client_id)
        while len(_upload_attempts) > MAX_TRACKED_CLIENTS:
            _upload_attempts.popitem(last=False)
    return 0


def validate_public_url(raw_url):
    parsed = urlparse(raw_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('La URL debe ser HTTP(S) pública y válida.')
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as error:
        raise ValueError('No se ha podido resolver el dominio.') from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('No se permiten direcciones privadas o locales.')
    return parsed


def fetch_public_url(raw_url):
    """Fetch an external URL while validating every redirect to prevent SSRF."""
    current_url = raw_url
    headers = {'User-Agent': 'MarkItDown Converter/1.0'}
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)
        response = requests.get(current_url, headers=headers, timeout=(5, 20),
                                allow_redirects=False, stream=True)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('La redirección no contiene una URL válida.')
            current_url = urljoin(current_url, location)
            continue
        response.raise_for_status()
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > MAX_URL_BYTES:
                response.close()
                raise ValueError('El contenido de la URL supera el límite de 10 MB.')
            chunks.append(chunk)
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].lower()
        response.close()
        return b''.join(chunks), content_type, current_url
    raise ValueError('La URL supera el límite de redirecciones.')


def extension_for_url_content(url, content_type):
    content_type_extensions = {
        'text/html': '.html', 'application/pdf': '.pdf', 'text/plain': '.txt',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    }
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    return suffix if suffix in ALLOWED_EXTENSIONS else content_type_extensions.get(content_type, '.html')

def sanitize_title(filename):
    # Split filename and extension
    name, ext = os.path.splitext(filename)
    # Remove special characters, keep alphanumeric, spaces, hyphens, underscores
    name = re.sub(r'[^\w\s-]', '', name)
    # Replace multiple spaces/hyphens with a single hyphen
    name = re.sub(r'[-\s]+', '-', name).strip('-')
    return name, ext

MARKDOWN_PREFIX_RE = re.compile(r'^(#{1,6}\s+|>|[-*+]\s+|\d+[.)]\s+|```|\|)')
ORDERED_HEADING_RE = re.compile(r'^((?:\d+[.)])(?:\s*\d+[.)]?){0,5})\s+(.+)$')
LIST_RE = re.compile(r'^(?:[-*+•‣▪])\s+(.+)$')


def numbered_heading_level(text):
    """Return a heading level for outline numbering (1, 1.2, 1.2.3…), if present."""
    match = ORDERED_HEADING_RE.match(text)
    if not match:
        return None
    marker = match.group(1)
    # The number of outline components maps directly to H1–H6.
    return min(6, len(re.findall(r'\d+', marker)))


def looks_like_heading(text):
    """Conservative fallback for OCR text that has no size metadata."""
    if len(text) > 72 or text.endswith(('.', ',', ';', ':', '?', '!')):
        return False
    if MARKDOWN_PREFIX_RE.match(text):
        return False
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(char.isupper() for char in letters) / len(letters)
    # OCR often turns display headings into all caps; title case is intentionally
    # not promoted to avoid converting ordinary prose into an H3.
    return upper_ratio >= 0.85 and len(text.split()) <= 12


def markdown_from_lines(lines, heading_levels=None):
    """Build Markdown blocks without flattening lists, quotes, tables or headings."""
    blocks, paragraph = [], []
    heading_levels = heading_levels or {}

    def flush_paragraph():
        if paragraph:
            blocks.append(' '.join(paragraph))
            paragraph.clear()

    for index, raw_line in enumerate(lines):
        text = raw_line.strip()
        if not text:
            flush_paragraph()
            continue

        bullet = LIST_RE.match(text)
        if bullet:
            flush_paragraph()
            blocks.append(f"- {bullet.group(1).strip()}")
            continue

        heading_level = heading_levels.get(index) or numbered_heading_level(text)
        # A single number can also be an ordered-list marker. Promote it only
        # when it looks like a section label; nested outline numbers are always
        # treated as structural headings.
        if heading_level == 1:
            label = re.sub(r'^\d+[.)]\s*', '', text)
            if len(label) > 72 or label.endswith(('.', ',', ';', ':')):
                heading_level = None
        if not heading_level and looks_like_heading(text):
            heading_level = 2
        if heading_level:
            flush_paragraph()
            blocks.append(f"{'#' * heading_level} {text}")
        elif MARKDOWN_PREFIX_RE.match(text):
            flush_paragraph()
            blocks.append(text)
        else:
            paragraph.append(text)

    flush_paragraph()
    return '\n\n'.join(blocks)


def format_text_to_markdown(raw_text):
    if not raw_text:
        return ""
    return markdown_from_lines(raw_text.splitlines())


def reconstruct_markdown_from_ocr(boxes, txts, scores):
    if not txts:
        return ""

    items = []
    heights = []
    for i, (box, text, score) in enumerate(zip(boxes, txts, scores)):
        x_coords = [p[0] for p in box]
        y_coords = [p[1] for p in box]
        xmin, xmax = min(x_coords), max(x_coords)
        ymin, ymax = min(y_coords), max(y_coords)
        height = ymax - ymin
        width = xmax - xmin
        cx = xmin + width / 2.0
        cy = ymin + height / 2.0

        items.append({
            'index': i,
            'text': text.strip(),
            'xmin': xmin,
            'xmax': xmax,
            'ymin': ymin,
            'ymax': ymax,
            'height': height,
            'width': width,
            'cx': cx,
            'cy': cy,
            'score': score
        })
        heights.append(height)

    # Calculate median height
    sorted_heights = sorted(heights)
    n_heights = len(sorted_heights)
    if n_heights > 0:
        if n_heights % 2 == 1:
            avg_height = sorted_heights[n_heights // 2]
        else:
            avg_height = (sorted_heights[n_heights // 2 - 1] + sorted_heights[n_heights // 2]) / 2.0
    else:
        avg_height = 12.0

    # Sort items by vertical center (cy)
    items.sort(key=lambda x: x['cy'])

    # Group items into lines
    lines = []
    current_line = []
    for item in items:
        if not current_line:
            current_line.append(item)
        else:
            prev_item = current_line[-1]
            vertical_diff = abs(item['cy'] - prev_item['cy'])
            line_height_limit = max(prev_item['height'], item['height']) * 0.75

            if vertical_diff < line_height_limit:
                current_line.append(item)
            else:
                lines.append(current_line)
                current_line = [item]
    if current_line:
        lines.append(current_line)

    # Process each visual line while preserving its typographic information.
    visual_lines = []
    for line in lines:
        # Sort items in the line from left to right
        line.sort(key=lambda x: x['xmin'])

        if len(line) > 1:
            # Calculate horizontal gaps
            gaps = []
            for i in range(len(line) - 1):
                gaps.append(line[i+1]['xmin'] - line[i]['xmax'])

            max_gap = max(gaps) if gaps else 0

            # If columns are far apart and all texts are short (< 30 chars), treat as table
            is_table_like = all(len(item['text']) < 30 for item in line) and max_gap > avg_height * 2.0

            if is_table_like:
                visual_lines.append({
                    'text': "| " + " | ".join(item['text'] for item in line) + " |",
                    'height': max(item['height'] for item in line),
                    'table': True,
                })
            else:
                visual_lines.append({
                    'text': " ".join(item['text'] for item in line),
                    'height': max(item['height'] for item in line),
                    'table': False,
                })
        else:
            item = line[0]
            visual_lines.append({'text': item['text'], 'height': item['height'], 'table': False})

    output, paragraph, table_rows = [], [], []
    # Heading levels in scanned material are encoded primarily by font size.
    # Rank the distinct sizes above body text instead of assigning every large
    # line the same H2/H3 level; this preserves H1–H6 hierarchy when present.
    heading_sizes = sorted({round(line['height'], 1) for line in visual_lines
                            if not line['table'] and line['height'] >= avg_height * 1.05}, reverse=True)
    size_levels = {size: min(6, index + 1) for index, size in enumerate(heading_sizes[:6])}

    def flush_paragraph():
        if paragraph:
            output.append(markdown_from_lines(paragraph))
            paragraph.clear()

    def flush_table():
        if table_rows:
            first_row = table_rows[0]
            columns = len(first_row.split('|')) - 2
            output.append(first_row)
            output.append('|' + ' --- |' * columns)
            output.extend(table_rows[1:])
            table_rows.clear()

    for line in visual_lines:
        text = line['text'].strip()
        if not text:
            continue
        if line['table']:
            flush_paragraph()
            table_rows.append(text)
            continue
        flush_table()

        explicit_level = numbered_heading_level(text)
        if explicit_level:
            flush_paragraph()
            output.append(f"{'#' * explicit_level} {text}")
            continue

        # OCR bounding-box height gives a much stronger heading signal than case.
        level = size_levels.get(round(line['height'], 1))
        if not level and looks_like_heading(text):
            level = 3

        if level and not MARKDOWN_PREFIX_RE.match(text):
            flush_paragraph()
            output.append(f"{'#' * level} {text}")
        elif MARKDOWN_PREFIX_RE.match(text) or LIST_RE.match(text):
            flush_paragraph()
            output.append(markdown_from_lines([text]))
        else:
            paragraph.append(text)

    flush_paragraph()
    flush_table()
    return '\n\n'.join(part for part in output if part)


def run_ocr_fallback(file_path, ext):
    ext_lower = ext.lower()
    if ext_lower == '.pdf':
        try:
            from rapidocr_pdf import RapidOCRPDF
            pdf_extracter = RapidOCRPDF()
            pages = pdf_extracter(file_path)
            if pages:
                ocr_pages = []
                for page in pages:
                    if isinstance(page, list) and len(page) >= 2:
                        ocr_pages.append(format_text_to_markdown(page[1]))
                return "\n\n---\n\n".join(ocr_pages)
        except Exception as e:
            print(f"Error in PDF OCR: {e}")
    elif ext_lower in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp']:
        try:
            from rapidocr import RapidOCR
            engine = RapidOCR()
            result = engine(file_path)
            if result:
                if hasattr(result, 'boxes') and hasattr(result, 'txts') and hasattr(result, 'scores'):
                    return reconstruct_markdown_from_ocr(result.boxes, result.txts, result.scores)
                elif isinstance(result, list):
                    boxes = [line[0] for line in result if isinstance(line, list) and len(line) >= 3]
                    txts = [line[1] for line in result if isinstance(line, list) and len(line) >= 3]
                    scores = [line[2] for line in result if isinstance(line, list) and len(line) >= 3]
                    if boxes and txts:
                        return reconstruct_markdown_from_ocr(boxes, txts, scores)
                    else:
                        lines = [line[1] for line in result if isinstance(line, list) and len(line) >= 2]
                        return format_text_to_markdown("\n".join(lines))
                elif hasattr(result, 'txts') and result.txts:
                    return format_text_to_markdown("\n".join(result.txts))
        except Exception as e:
            print(f"Error in Image OCR: {e}")
    return None


@app.route('/api/convert', methods=['POST'])
def convert_file():
    client_id = request.remote_addr or 'unknown'
    remaining = upload_cooldown_remaining(client_id)
    if remaining:
        response = jsonify({'error': f'Espera {remaining} segundos antes de subir otro archivo.',
                            'retry_after_seconds': remaining})
        response.headers['Retry-After'] = str(remaining)
        return response, 429

    if 'file' not in request.files:
        return jsonify({'error': 'No se proporcionó ningún archivo'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Nombre de archivo vacío'}), 400

    try:
        # The upload is written only into an OS temporary directory while
        # MarkItDown processes it. TemporaryDirectory removes it immediately.
        original_name = secure_filename(file.filename)
        title, ext = sanitize_title(original_name)
        ext = ext.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': 'Formato de archivo no permitido.'}), 400
        if not title:
            title = 'documento'
        with TemporaryDirectory(prefix='markitdown-') as temp_dir:
            file_path = os.path.join(temp_dir, original_name or f'documento{ext}')
            file.save(file_path)
            try:
                result = markitdown.convert(file_path)
                markdown_text = result.text_content
            except Exception as conversion_error:
                markdown_text = run_ocr_fallback(file_path, ext)
                if not markdown_text:
                    raise conversion_error

            if not markdown_text or not markdown_text.strip():
                ocr_text = run_ocr_fallback(file_path, ext)
                if ocr_text:
                    markdown_text = ocr_text
                elif ext.lower() == '.pdf':
                    markdown_text = "⚠️ **Aviso de conversión:**\n\nEl archivo PDF no contiene ninguna capa de texto digital legible y la extracción por OCR falló."
                else:
                    markdown_text = f"⚠️ **Aviso de conversión:**\n\nEl archivo '{original_name}' no contiene texto digital legible."

        return jsonify({
            'success': True,
            'download_name': f"{title}.md",
            'markdown': markdown_text,
        })

    except Exception as error:
        app.logger.exception('File conversion failed: %s', error)
        return jsonify({'error': 'No se ha podido convertir el archivo.'}), 500

@app.route('/api/convert-url', methods=['POST'])
def convert_url():
    try:
        data = request.get_json() or {}
        url = data.get('url')
        if not url:
            return jsonify({'error': 'No se proporcionó ninguna URL'}), 400

        parsed_url = validate_public_url(url)
        content, content_type, final_url = fetch_public_url(url)
        extension = extension_for_url_content(final_url, content_type)
        result = markitdown.convert_stream(BytesIO(content), file_extension=extension, url=final_url)
        markdown_text = result.text_content

        if not markdown_text or not markdown_text.strip():
            return jsonify({'error': 'No se pudo extraer contenido de la URL proporcionada'}), 400

        # Generate a descriptive filename
        domain = parsed_url.netloc.replace('.', '_')
        title = result.title or domain or "url"

        # Sanitize title
        title = re.sub(r'[^\w\s-]', '', title)
        title = re.sub(r'[-\s]+', '-', title).strip('-')
        if not title:
            title = 'enlace'

        return jsonify({
            'success': True,
            'download_name': f"{title}.md",
            'markdown': markdown_text,
        })
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except requests.RequestException:
        return jsonify({'error': 'No se ha podido recuperar la URL solicitada.'}), 502
    except Exception as error:
        app.logger.exception('URL conversion failed: %s', error)
        return jsonify({'error': 'No se ha podido convertir la URL.'}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
