// The files a turn produced, as cards under the answer: a glyph, the name, what it is and how big,
// and two actions — open it in the panel, or download it. An image the agent handed over shows
// itself; everything else is the row alone. One card per file, whatever the trace above it shows.

import { Icon, IconName } from "./icons";
import { AuthImg, PreviewSource, canPreview, previewKind } from "./preview";
import type { Artifact } from "./turns";
import { t } from "./i18n";

/** The line icon for a file, by what the viewer would make of it. */
export function fileIcon(name: string): IconName {
  switch (previewKind(name)) {
    case "image":
      return "image";
    case "json":
      return "terminal";
    case "diff":
      return "changes";
    case "html":
      return "globe";
    case "csv":
    case "sheet":
      return "board";
    case "audio":
    case "video":
      return "play";
    case "markdown":
    case "text":
    case "pdf":
    case "docx":
      return "file";
    default:
      return "attach";
  }
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toLowerCase() : "";
}

export type ArtifactCardProps = {
  item: Artifact;
  /** Where the file is served from: the workspace for a written file, the call itself for a sent one. */
  src: PreviewSource;
  downloadUrl: string;
  onOpen: (src: PreviewSource) => void;
};

export function ArtifactCard({ item, src, downloadUrl, onOpen }: ArtifactCardProps) {
  const image = previewKind(item.name) === "image";
  const openable = canPreview(item.name);
  const meta = [item.size, extOf(item.name), t(item.how === "wrote" ? "turn.artifact.wrote" : "turn.artifact.sent")].filter(Boolean).join(" · ");
  const open = () => (openable ? onOpen(src) : window.open(downloadUrl, "_blank", "noreferrer"));
  return (
    <div className={`artifact ${image ? "image" : ""}`} data-path={item.path}>
      <button type="button" className="artifact-main" onClick={open} title={openable ? t("turn.artifact.open") : t("preview.download")}>
        <span className="artifact-glyph" aria-hidden><Icon name={fileIcon(item.name)} size={18} /></span>
        <span className="artifact-text">
          <span className="artifact-name truncate">{item.name}</span>
          <span className="artifact-meta truncate">{item.caption ? `${item.caption} · ${meta}` : meta}</span>
        </span>
      </button>
      <span className="artifact-actions">
        <a className="iconbtn small" href={downloadUrl} target="_blank" rel="noreferrer" aria-label={t("preview.download")} title={t("preview.download")} onClick={(e) => e.stopPropagation()}>
          <Icon name="download" size={15} />
        </a>
        {openable && (
          <button type="button" className="iconbtn small" onClick={() => onOpen(src)} aria-label={t("turn.artifact.open")} title={t("turn.artifact.open")}>
            <Icon name="panel" size={15} />
          </button>
        )}
      </span>
      {image && (
        <div className="artifact-thumb">
          <AuthImg src={src} alt={item.name} className="tool-image" onClick={() => onOpen(src)} />
        </div>
      )}
    </div>
  );
}
