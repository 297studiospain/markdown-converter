import ipaddress
import os
import re
import socket
from collections import OrderedDict
from io import BytesIO
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


def convert_bytes(content, extension, url=None):
    result = markitdown.convert_stream(BytesIO(content), file_extension=extension, url=url)
    if not result.text_content or not result.text_content.strip():
        raise ValueError('No se ha podido extraer texto de este archivo. Comprueba que no esté protegido con contraseña o dañado.')
    return result


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
        result = convert_bytes(content, extension)
        return jsonify({'success': True, 'download_name': f'{title}.md', 'markdown': result.text_content})
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
        result = convert_bytes(content, extension_for_url_content(final_url, content_type), url=final_url)
        title, _ = filename_parts(result.title or parsed_url.netloc.replace('.', '_'))
        return jsonify({'success': True, 'download_name': f'{title}.md', 'markdown': result.text_content})
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except requests.RequestException:
        return jsonify({'error': 'No se ha podido recuperar la URL solicitada.'}), 502
    except Exception as error:
        app.logger.exception('URL conversion failed: %s', error)
        return jsonify({'error': 'No se ha podido convertir la URL.'}), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
