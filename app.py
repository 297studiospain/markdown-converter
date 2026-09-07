import ipaddress
import os
import re
import socket
from collections import Counter, OrderedDict
from io import BytesIO
from statistics import median
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, jsonify, request
from markitdown import MarkItDown
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=20 * 1024 * 1024)

UPLOAD_COOLDOWN_SECONDS = 15
MAX_TRACKED_CLIENTS = 10_000
MAX_URL_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_EXTENSIONS = {
    '.pdf', '.docx', '.xls', '.xlsx', '.csv', '.pptx',
    '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp',
    '.txt', '.text', '.md', '.markdown', '.json', '.jsonl', '.html', '.htm',
    '.xml', '.yaml', '.yml', '.toml', '.ini', '.log', '.tsv', '.rtf',
    '.ipynb', '.msg', '.epub', '.zip', '.mp3', '.wav', '.m4a', '.mp4',
}
CONTENT_TYPE_EXTENSIONS = {
    'text/html': '.html',
    'application/pdf': '.pdf',
    'text/plain': '.txt',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
}

_upload_attempts = OrderedDict()
_upload_lock = Lock()
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
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)
        response = requests.get(current_url, headers={'User-Agent': 'MarkItDown Converter/1.0'}, timeout=(5, 20), allow_redirects=False, stream=True)
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


def filename_parts(filename):
    name, extension = os.path.splitext(secure_filename(filename))
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[-\s]+', '-', name).strip('-') or 'documento'
    return name, extension.lower()


def extension_for_url_content(url, content_type):
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    return suffix if suffix in ALLOWED_EXTENSIONS else CONTENT_TYPE_EXTENSIONS.get(content_type, '.html')


def normalized_ocr_text(text):
    return re.sub(r'\s+', ' ', str(text)).strip()


def uppercase_ratio(text):
    letters = [character for character in text if character.isalpha()]
    return sum(character.isupper() for character in letters) / len(letters) if letters else 0


def cluster_heading_sizes(sizes):
    """Map visually distinct font-size groups to Markdown H1-H6."""
    clusters = []
    for size in sorted(sizes, reverse=True):
        if not clusters or abs(size - clusters[-1][0]) / clusters[-1][0] > 0.12:
            clusters.append([size])
        else:
            clusters[-1].append(size)
    return {
        round(size, 1): min(index + 1, 6)
        for index, cluster in enumerate(clusters[:6])
        for size in cluster
    }


def markdown_from_ocr_output(result, repeated_texts=None):
    boxes = getattr(result, 'boxes', None)
    texts = getattr(result, 'txts', None)
    scores = getattr(result, 'scores', None)
    if boxes is None or texts is None:
        return ''

    repeated_texts = repeated_texts or set()
    items = []
    for index, (box, raw_text) in enumerate(zip(boxes, texts)):
        text = normalized_ocr_text(raw_text)
        score = scores[index] if scores is not None else 1
        if (not text or score < 0.45 or text.casefold() in repeated_texts
                or (len(text) == 1 and not text.isalnum())):
            continue
        x_values = [point[0] for point in box]
        y_values = [point[1] for point in box]
        items.append({
            'text': text,
            'x': min(x_values),
            'y': min(y_values),
            'height': max(y_values) - min(y_values),
            'bottom': max(y_values),
        })
    if not items:
        return ''

    items.sort(key=lambda item: (item['y'], item['x']))
    body_sizes = [item['height'] for item in items
                  if len(item['text']) >= 30 and uppercase_ratio(item['text']) < 0.75]
    body_size = median(body_sizes or [item['height'] for item in items]) or 1
    for item in items:
        item['is_heading'] = (
            3 < len(item['text']) <= 120
            and not re.fullmatch(r'[\d\W]+', item['text'])
            and (uppercase_ratio(item['text']) >= 0.85
                 or (len(item['text']) <= 60 and item['height'] >= body_size * 1.35))
        )
    heading_sizes = {round(item['height'], 1) for item in items if item['is_heading']}
    heading_levels = cluster_heading_sizes(heading_sizes)

    blocks = []
    paragraph = []
    previous = None

    def flush_paragraph():
        if paragraph:
            blocks.append(' '.join(paragraph))
            paragraph.clear()

    for item in items:
        text = item['text']
        level = heading_levels.get(round(item['height'], 1)) if item['is_heading'] else None
        if level:
            flush_paragraph()
            blocks.append(f"{'#' * level} {text}")
        elif re.match(r'^(?:[-*+•‣▪]|\d+[.)])\s+', text):
            flush_paragraph()
            blocks.append(re.sub(r'^(?:[-*+•‣▪])\s*', '- ', text))
        elif text.startswith(('“', '"', '«')) and text.endswith(('”', '"', '»')):
            flush_paragraph()
            blocks.append(f'> {text}')
        else:
            if previous:
                vertical_gap = item['y'] - previous['bottom']
                horizontal_shift = abs(item['x'] - previous['x'])
                if vertical_gap > body_size * 1.8 or horizontal_shift > body_size * 5:
                    flush_paragraph()
            paragraph.append(text)
        previous = item

    flush_paragraph()
    return '\n\n'.join(blocks)


def repeated_ocr_texts(results):
    counts = Counter()
    for result in results:
        for text in getattr(result, 'txts', None) or []:
            normalized = normalized_ocr_text(text).casefold()
            if 2 < len(normalized) < 80:
                counts[normalized] += 1
    minimum_repetitions = max(3, len(results) // 3)
    return {text for text, count in counts.items() if count >= minimum_repetitions}


def ocr_text_from_file(file_path, extension):
    """Extract structured visual text without retaining the upload."""
    from rapidocr import RapidOCR

    engine = RapidOCR()
    if extension == '.pdf':
        import pymupdf

        document = pymupdf.open(file_path)
        results = []
        try:
            for page in document:
                image = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes('png')
                results.append(engine(image))
        finally:
            document.close()
        repeated_texts = repeated_ocr_texts(results)
        pages = [markdown_from_ocr_output(result, repeated_texts) for result in results]
        return '\n\n---\n\n'.join(page for page in pages if page)

    return markdown_from_ocr_output(engine(file_path))


def should_run_ocr(markdown_text, extension, content):
    if extension in {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'}:
        return len(markdown_text.strip()) < 40
    if extension != '.pdf':
        return False
    try:
        from pypdf import PdfReader

        page_count = len(PdfReader(BytesIO(content)).pages)
    except Exception:
        page_count = 1
    return len(markdown_text.strip()) < max(300, page_count * 40)


def convert_bytes(content, extension, url=None):
    result = markitdown.convert_stream(BytesIO(content), file_extension=extension, url=url)
    markdown_text = result.text_content or ''
    if url is None and should_run_ocr(markdown_text, extension, content):
        with TemporaryDirectory(prefix='markitdown-') as temp_dir:
            file_path = os.path.join(temp_dir, f'upload{extension}')
            with open(file_path, 'wb') as temporary_file:
                temporary_file.write(content)
            ocr_text = ocr_text_from_file(file_path, extension)
        if ocr_text:
            markdown_text = ocr_text
    if not markdown_text.strip():
        raise ValueError('No se ha podido extraer texto de este archivo. Comprueba que no esté protegido con contraseña o dañado.')
    return result, markdown_text


@app.route('/api/convert', methods=['POST'])
def convert_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No se proporcionó ningún archivo'}), 400
    uploaded_file = request.files['file']
    if not uploaded_file.filename:
        return jsonify({'error': 'Nombre de archivo vacío'}), 400

    title, extension = filename_parts(uploaded_file.filename)
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({'error': 'Formato no compatible. Usa PDF, DOCX, XLS/XLSX, CSV, PPTX, PNG, JPG, WEBP, BMP o TIFF.'}), 400
    remaining = upload_cooldown_remaining(request.remote_addr or 'unknown')
    if remaining:
        response = jsonify({'error': f'Espera {remaining} segundos antes de subir otro archivo.', 'retry_after_seconds': remaining})
        response.headers['Retry-After'] = str(remaining)
        return response, 429

    content = uploaded_file.read()
    if not content:
        return jsonify({'error': 'El archivo está vacío.'}), 400
    try:
        _result, markdown_text = convert_bytes(content, extension)
        return jsonify({'success': True, 'download_name': f'{title}.md', 'markdown': markdown_text})
    except ValueError as error:
        return jsonify({'error': str(error)}), 422
    except Exception as error:
        app.logger.exception('File conversion failed: %s', error)
        return jsonify({'error': 'No se ha podido convertir el archivo.'}), 500


@app.route('/api/convert-url', methods=['POST'])
def convert_url():
    try:
        url = (request.get_json() or {}).get('url')
        if not url:
            return jsonify({'error': 'No se proporcionó ninguna URL'}), 400
        parsed_url = validate_public_url(url)
        content, content_type, final_url = fetch_public_url(url)
        result, markdown_text = convert_bytes(content, extension_for_url_content(final_url, content_type), url=final_url)
        title, _ = filename_parts(result.title or parsed_url.netloc.replace('.', '_'))
        return jsonify({'success': True, 'download_name': f'{title}.md', 'markdown': markdown_text})
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except requests.RequestException:
        return jsonify({'error': 'No se ha podido recuperar la URL solicitada.'}), 502
    except Exception as error:
        app.logger.exception('URL conversion failed: %s', error)
        return jsonify({'error': 'No se ha podido convertir la URL.'}), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
