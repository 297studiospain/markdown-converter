"use client";

import { ChangeEvent, DragEvent, useRef, useState } from "react";
import { marked } from "marked";

type Mode = "file" | "url";

const emptyMarkdown = `# Ready when you are\n\nDrop a document or paste a URL to turn it into clean Markdown.\n\n- PDF, Word, Excel and PowerPoint\n- Images and scanned documents with OCR\n- Web pages and links`;

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

  const convertFile = async (file: File) => {
    setIsBusy(true); setStatus(`Converting ${file.name}…`);
    const form = new FormData(); form.append("file", file);
    try {
      const response = await fetch("/api/convert", { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Conversion failed");
      setMarkdown(data.markdown); downloadMarkdown(data.markdown, data.download_name || "document.md");
      setSelectedFile(null); setStatus(`Downloaded ${data.download_name || "Markdown"}`);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Conversion failed"); }
    finally { setIsBusy(false); }
  };

  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) { setSelectedFile(file); setStatus(`${file.name} ready to download`); }
    event.target.value = "";
  };
  const handleDrop = (event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault(); const file = event.dataTransfer.files[0];
    if (file) { setSelectedFile(file); setStatus(`${file.name} ready to download`); }
  };
  const convertUrl = async () => {
    if (!url.trim()) return;
    setIsBusy(true); setStatus("Extracting page…");
    try {
      const response = await fetch("/api/convert-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not extract this URL");
      setMarkdown(data.markdown); downloadMarkdown(data.markdown, data.download_name || "page.md");
      setStatus(`Downloaded ${data.download_name || "Markdown"}`);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Conversion failed"); }
    finally { setIsBusy(false); }
  };
  const requestDownload = async () => {
    if (mode === "file") {
      if (!selectedFile) { setStatus("Choose a file before downloading"); return; }
      await convertFile(selectedFile);
    } else {
      await convertUrl();
    }
  };

  return <main className="shell">
    <aside className="sidebar">
      <a className="logo" href="#top" aria-label="MarkItDown home"><span>MI</span>MarkItDown</a>
      <div className="sidebar-label">Private conversion</div>
      <p className="empty-history">Files are processed only to create your download. Nothing is kept on the server.</p>
      <div className="sidebar-footer">No history · No accounts<br />Powered by MarkItDown + OCR</div>
    </aside>
    <section className="workspace" id="top">
      <header className="topbar"><div><span className="live-dot" /> {status}</div><button onClick={requestDownload} className="copy-button" aria-label="Convert and download Markdown" disabled={isBusy || (mode === "file" ? !selectedFile : !url.trim())}>{isBusy ? "Converting…" : "Download Markdown"} <b>↓</b></button></header>
      <div className="intro"><p className="eyebrow">Document conversion, without the friction.</p><h1>Make your files<br />useful again.</h1></div>
      <div className="tabs" role="tablist" aria-label="Conversion type"><button className={mode === "file" ? "active" : ""} onClick={() => setMode("file")} role="tab" aria-selected={mode === "file"}>Upload a file</button><button className={mode === "url" ? "active" : ""} onClick={() => setMode("url")} role="tab" aria-selected={mode === "url"}>Convert a URL</button></div>
      {mode === "file" ? <button className="dropzone" onDrop={handleDrop} onDragOver={(event) => event.preventDefault()} onClick={() => inputRef.current?.click()} disabled={isBusy}><input ref={inputRef} type="file" onChange={handleFile} hidden /><span className="upload-mark">↓</span><strong>{isBusy ? "Converting your file…" : selectedFile ? selectedFile.name : "Drop a file here"}</strong><span>{selectedFile ? "ready to convert and download" : "or click to browse"}</span><small>PDF · DOCX · XLSX · PPTX · images · audio</small></button> : <div className="url-box"><label htmlFor="url">Web page address</label><div><input id="url" type="url" placeholder="https://example.com/article" value={url} onChange={(event) => setUrl(event.target.value)} /><button onClick={requestDownload} disabled={isBusy || !url.trim()}>Download <span>↓</span></button></div><p>It will be converted only when you download it.</p></div>}
      <section className="output"><div className="output-head"><span>Markdown output</span><span>{markdown.length.toLocaleString()} characters</span></div><div className="split"><pre className="code"><code>{markdown}</code></pre><iframe className="preview" title="Markdown preview" sandbox="" srcDoc={`<!doctype html><html><head><style>body{font-family:Arial,sans-serif;color:#151515;line-height:1.55;padding:24px}h1,h2,h3{line-height:1.08}pre{background:#f4f2ed;padding:14px;overflow:auto}code{font-family:ui-monospace,monospace}a{color:#e14926}</style></head><body>${marked.parse(markdown, { async: false })}</body></html>`} /></div></section>
    </section>
  </main>;
}
