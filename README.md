# MarkItDown Converter

A privacy-first web interface for converting documents and public web pages into Markdown. Files are processed only when the download is requested: neither the original upload nor the generated Markdown is stored on the server.

Built with a Next.js interface, a small Flask API and [MarkItDown](https://github.com/microsoft/markitdown). OCR support is included for scanned PDFs and images.

## What it can convert

- Documents: PDF, DOCX, RTF, TXT, HTML and HTM
- Spreadsheets and presentations: XLS, XLSX and PPTX
- Images and scans: PNG, JPG, JPEG, BMP, TIFF and WEBP (via OCR)
- Audio: MP3 and WAV (when the local conversion dependencies are available)
- Public HTTP(S) URLs

The output is shown in the browser and immediately downloaded as a `.md` file. The converter tries to preserve document structure, including headings (`H1` to `H6`), paragraphs, lists, quotes and tables where the source material provides that information.

## How it works

```text
Browser (Next.js, port 3000)
        │  /api/* proxy
        ▼
Flask API (port 5000) ──► MarkItDown / OCR ──► Markdown response ──► browser download
```

Uploaded documents are placed in an operating-system temporary directory only for conversion, then removed automatically. Generated Markdown is kept in browser memory until it is downloaded.

## Run locally

### Prerequisites

- Python 3.10 or later
- Node.js 20 or later
- npm

### 1. Start the conversion API

```bash
git clone git@github.com:297studiospain/markdown-converter.git
cd markdown-converter

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The API listens only on `127.0.0.1:5000`.

### 2. Start the web interface

In a second terminal, from the repository root:

```bash
cd nextjs
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Production build

Create an optimized Next.js build with:

```bash
cd nextjs
npm run build
npm run start
```

The Next.js app proxies `/api/*` to `http://127.0.0.1:5000`. For deployment, run the Flask API on the same private host/network or update the proxy destination in [`nextjs/next.config.ts`](nextjs/next.config.ts). Put the public site behind HTTPS and a reverse proxy.

## API

### `POST /api/convert`

Accepts `multipart/form-data` with a `file` field. A successful response returns JSON with `markdown` and `download_name`.

### `POST /api/convert-url`

Accepts JSON in the form:

```json
{ "url": "https://example.com/article" }
```

It returns the same JSON response shape as file conversion.

## Privacy and security

- No uploaded files, generated Markdown or conversion history are persisted.
- File uploads are limited to 20 MB.
- One file upload is allowed per client every 15 seconds. A blocked request receives `429 Too Many Requests` and `Retry-After`.
- Only the formats listed above are accepted.
- URL conversion permits public HTTP(S) destinations only; private, local and loopback addresses are rejected.
- Redirect destinations are revalidated to help prevent server-side request forgery (SSRF).
- URL downloads have a 10 MB limit, a three-redirect maximum and network timeouts.
- Both services add restrictive security headers; the Flask API is bound to localhost by default.

For an internet-facing installation, deploy behind a reverse proxy with TLS and add infrastructure-level rate limits appropriate for your traffic.

## Project structure

```text
.
├── app.py                 # Flask conversion API and security controls
├── requirements.txt       # Python dependencies
└── nextjs/                # Next.js user interface
    ├── app/               # Page and styles
    └── next.config.ts     # API proxy and browser security headers
```

## Contributing

Issues and pull requests are welcome. Please keep changes focused, avoid committing user documents or generated Markdown, and run the checks below before opening a pull request:

```bash
cd nextjs && npm run build
python3 -m py_compile app.py
```

## License

No license has been selected yet. A public repository lets people inspect the code, but reuse and redistribution require an explicit license. Add a license (for example, MIT) before treating this as an open-source project.
