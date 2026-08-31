"use client";

import { ChangeEvent, DragEvent, useRef, useState } from "react";
import packageMetadata from "../package.json";

type Mode = "file" | "url";

const emptyMarkdown = `# Ready when you are\n\nDrop a document or paste a URL to turn it into clean Markdown.\n\n- PDF, Word, Excel and PowerPoint\n- Images supported by MarkItDown\n- Web pages and links`;
const appVersion = `v${packageMetadata.version.split(".")[0]}`;

export default function Home() {
  const [mode, setMode] = useState<Mode>("file");
  const [markdown, setMarkdown] = useState(emptyMarkdown);
  const [status, setStatus] = useState("Ready to convert");
  const [isBusy, setIsBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
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
    setIsBusy(true);
    setStatus("Converting…");
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers,
        body,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Conversion failed");
      const filename = data.download_name || defaultName;
      setMarkdown(data.markdown);
      downloadMarkdown(data.markdown, filename);
      setSelectedFile(null);
      setStatus(`Downloaded ${filename}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Conversion failed");
    } finally {
      setIsBusy(false);
    }
  };

  const convertFile = async (file: File) => {
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
        <div className="sidebar-footer">No history · No accounts<br />Powered by MarkItDown</div>
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
    </main>
  );
}
