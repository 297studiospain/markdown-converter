"use client";

import { ChangeEvent, DragEvent, useRef, useState } from "react";
import packageMetadata from "../package.json";

type Mode = "file" | "url";
type ErrorNotice = { id: string; message: string };

const emptyMarkdown = `# Ready when you are\n\nDrop a document or paste a URL to turn it into clean Markdown.\n\n- PDF, Word, Excel and PowerPoint\n- Images and visual PDFs with OCR\n- Web pages and links`;
const appVersion = `v${packageMetadata.version.split(".")[0]}`;
const VERCEL_SAFE_PDF_BYTES = 3_400_000;
const LARGE_PDF_BYTES = 4 * 1024 * 1024;

export default function Home() {
  const [mode, setMode] = useState<Mode>("file");
  const [markdown, setMarkdown] = useState(emptyMarkdown);
  const [status, setStatus] = useState("Ready to convert");
  const [isBusy, setIsBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [errorNotice, setErrorNotice] = useState<ErrorNotice | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const downloadMarkdown = (content: string, filename: string) => {
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl; anchor.download = filename;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    URL.revokeObjectURL(objectUrl);
  };

  const handleConversion = async (endpoint: string, body: FormData | string, headers?: HeadersInit, defaultName = "document.md") => {
    setErrorNotice(null);
    setIsBusy(true);
    setStatus("Converting…");
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers,
        body,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(data?.error || `El servidor no ha podido convertir el archivo (${response.status}).`);
      }
      if (!data || typeof data.markdown !== "string") {
        throw new Error("La conversión no ha devuelto un archivo Markdown válido.");
      }
      const filename = data.download_name || defaultName;
      setMarkdown(data.markdown);
      downloadMarkdown(data.markdown, filename);
      setSelectedFile(null);
      setStatus(`Downloaded ${filename}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "No se ha podido convertir el archivo.";
      setStatus(message);
      setErrorNotice({
        id: `ERR-${Date.now().toString(36).toUpperCase()}`,
        message,
      });
    } finally {
      setIsBusy(false);
    }
  };

  const canvasBlob = (canvas: HTMLCanvasElement, quality: number) => new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("No se ha podido preparar una página del PDF.")), "image/jpeg", quality);
  });

  const renderPdfPages = async (file: File) => {
    const pdfjs = await import("pdfjs-dist");
    pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
    const pdfDocument = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
    if (pdfDocument.numPages > 50) {
      throw new Error("El PDF supera el máximo de 50 páginas para conversión web.");
    }

    const pageBudget = Math.max(45_000, Math.floor(VERCEL_SAFE_PDF_BYTES / pdfDocument.numPages));
    const pageImages: File[] = [];
    const presets = [[1.2, 0.78], [1, 0.7], [0.85, 0.62], [0.7, 0.52]] as const;
    try {
      for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
        setStatus(`Preparing page ${pageNumber} of ${pdfDocument.numPages}…`);
        const page = await pdfDocument.getPage(pageNumber);
        let image: Blob | null = null;
        for (const [scale, quality] of presets) {
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.width = Math.ceil(viewport.width);
          canvas.height = Math.ceil(viewport.height);
          if (!canvas.getContext("2d", { alpha: false })) throw new Error("Tu navegador no puede preparar el PDF.");
          await page.render({ canvas, viewport }).promise;
          const candidate = await canvasBlob(canvas, quality);
          if (candidate.size <= pageBudget || scale === presets[presets.length - 1][0]) {
            image = candidate;
            break;
          }
        }
        if (!image) throw new Error("No se ha podido preparar una página del PDF.");
        pageImages.push(new File([image], `page-${String(pageNumber).padStart(3, "0")}.jpg`, { type: "image/jpeg" }));
      }
    } finally {
      await pdfDocument.destroy();
    }
    const totalBytes = pageImages.reduce((total, page) => total + page.size, 0);
    if (totalBytes > VERCEL_SAFE_PDF_BYTES) {
      throw new Error("Este PDF no se puede comprimir lo suficiente para la conversión web. Divídelo en archivos más pequeños.");
    }
    return pageImages;
  };

  const convertLargePdf = async (file: File) => {
    setErrorNotice(null);
    setIsBusy(true);
    try {
      const pageImages = await renderPdfPages(file);
      const form = new FormData();
      form.append("pdf_pages", "1");
      form.append("source_name", file.name);
      pageImages.forEach((page) => form.append("file", page));
      await handleConversion("/api/convert", form, undefined, dataFilename(file.name));
    } catch (error) {
      const message = error instanceof Error ? error.message : "No se ha podido preparar el PDF.";
      setStatus(message);
      setErrorNotice({ id: `ERR-${Date.now().toString(36).toUpperCase()}`, message });
      setIsBusy(false);
    }
  };

  const convertFile = async (file: File) => {
    if (file.type === "application/pdf" && file.size > LARGE_PDF_BYTES) {
      await convertLargePdf(file);
      return;
    }
    setStatus(`Converting ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    await handleConversion("/api/convert", form, undefined, dataFilename(file.name));
  };

  const dataFilename = (name: string) => name.replace(/\.[^/.]+$/, "") + ".md";

  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      setSelectedFile(file);
      void convertFile(file);
    }
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) {
      setSelectedFile(file);
      void convertFile(file);
    }
  };

  const convertUrl = async () => {
    if (!url.trim()) return;
    setStatus("Extracting page…");
    await handleConversion(
      "/api/convert-url",
      JSON.stringify({ url: url.trim() }),
      { "Content-Type": "application/json" },
      "page.md"
    );
  };

  const requestDownload = async () => {
    if (mode === "file") {
      if (!selectedFile) {
        setStatus("Choose a file before downloading");
        return;
      }
      await convertFile(selectedFile);
    } else {
      await convertUrl();
    }
  };

  return (
    <main className="shell">
      <aside className="sidebar">
        <a className="logo" href="#top" aria-label={`MarkItDown ${appVersion} home`}>
          <span>MI</span>MarkItDown{" "}
          <span style={{ background: "transparent", width: "auto", height: "auto", color: "#9c9992", fontSize: "10px", letterSpacing: ".08em", textTransform: "uppercase", marginLeft: "-3px" }}>
            {appVersion}
          </span>
        </a>
        <div className="sidebar-label">Private conversion</div>
        <p className="empty-history">Files are processed only to create your download. Nothing is kept on the server.</p>
        <div className="sidebar-footer">No history · No accounts<br />Powered by MarkItDown + OCR</div>
      </aside>

      <section className="workspace" id="top">
        <header className="topbar">
          <div><span className="live-dot" /> {status}</div>
          <button
            onClick={requestDownload}
            className="copy-button"
            aria-label="Convert and download Markdown"
            disabled={isBusy || (mode === "file" ? !selectedFile : !url.trim())}
          >
            {isBusy ? "Converting…" : "Download Markdown"} <b>↓</b>
          </button>
        </header>

        <div className="intro">
          <p className="eyebrow">Document conversion, without the friction.</p>
          <h1>Make your files<br />useful again.</h1>
        </div>

        <div className="tabs" role="tablist" aria-label="Conversion type">
          <button className={mode === "file" ? "active" : ""} onClick={() => setMode("file")} role="tab" aria-selected={mode === "file"}>
            Upload a file
          </button>
          <button className={mode === "url" ? "active" : ""} onClick={() => setMode("url")} role="tab" aria-selected={mode === "url"}>
            Convert a URL
          </button>
        </div>

        {mode === "file" ? (
          <button
            className="dropzone"
            onDrop={handleDrop}
            onDragOver={(event) => event.preventDefault()}
            onClick={() => inputRef.current?.click()}
            disabled={isBusy}
          >
            <input ref={inputRef} type="file" accept=".pdf,.docx,.xls,.xlsx,.csv,.pptx,.png,.jpg,.jpeg,.bmp,.tiff,.webp" onChange={handleFile} hidden />
            <span className="upload-mark">↓</span>
            <strong>{isBusy ? "Converting your file…" : selectedFile ? selectedFile.name : "Drop a file here"}</strong>
            <span>{selectedFile ? "ready to convert and download" : "or click to browse"}</span>
            <small>PDF · DOCX · XLS/XLSX · CSV · PPTX · images</small>
          </button>
        ) : (
          <div className="url-box">
            <label htmlFor="url">Web page address</label>
            <div>
              <input id="url" type="url" placeholder="https://example.com/article" value={url} onChange={(event) => setUrl(event.target.value)} />
              <button onClick={requestDownload} disabled={isBusy || !url.trim()}>
                Download <span>↓</span>
              </button>
            </div>
            <p>It will be converted only when you download it.</p>
          </div>
        )}

        <section className="output">
          <div className="output-head">
            <span>Markdown output</span>
            <span>{markdown.length.toLocaleString()} characters</span>
          </div>
          <pre className="code"><code>{markdown}</code></pre>
        </section>
      </section>

      {errorNotice && (
        <div className="error-backdrop" role="presentation">
          <section className="error-dialog" role="alertdialog" aria-modal="true" aria-labelledby="error-title" aria-describedby="error-description">
            <p className="error-label">Conversion error</p>
            <h2 id="error-title">No se ha podido convertir el archivo</h2>
            <p id="error-description">{errorNotice.message}</p>
            <div className="error-reference">
              <span>Referencia</span>
              <code>{errorNotice.id}</code>
            </div>
            <p className="error-help">Haz una captura de esta ventana y envíasela al administrador, incluyendo la referencia del error.</p>
            <button className="error-close" type="button" onClick={() => setErrorNotice(null)}>Cerrar</button>
          </section>
        </div>
      )}
    </main>
  );
}
