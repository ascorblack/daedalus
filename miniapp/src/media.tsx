import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import type { MediaItem, MediaPresentation } from "./api";
import { api } from "./api";
import { Sheet } from "./dialogs";
import { t } from "./i18n";
export { mediaCopyText, splitMediaAnswer } from "./mediaformat";

function source(sessionId: string, presentationId: string, itemId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/media/${encodeURIComponent(presentationId)}/${encodeURIComponent(itemId)}/content`;
}

let accessRequest: Promise<void> | null = null;
function requestMediaAccess(): Promise<void> {
  if (!accessRequest) {
    accessRequest = api.post("/api/media/access").then(() => undefined).catch((error) => {
      accessRequest = null;
      throw error;
    });
  }
  return accessRequest;
}

function MediaElement({ sessionId, presentation, item, onOpen }: { sessionId: string; presentation: MediaPresentation; item: MediaItem; onOpen: () => void }) {
  const src = source(sessionId, presentation.id, item.id);
  if (item.kind === "image" || item.kind === "animation") {
    return <button type="button" className="inline-media-image" onClick={onOpen} aria-label={t("media.open", { name: item.alt || item.filename })}>
      <img src={src} alt={item.alt || item.filename} loading="lazy" width={item.width || undefined} height={item.height || undefined} />
      {item.caption && <span>{item.caption}</span>}
    </button>;
  }
  if (item.kind === "video") return <figure className="inline-media-player"><video controls playsInline preload="metadata" src={src} />{item.caption && <figcaption>{item.caption}</figcaption>}</figure>;
  return <figure className="inline-media-player audio"><audio controls preload="metadata" src={src} />{item.caption && <figcaption>{item.caption}</figcaption>}</figure>;
}

function Viewer({ sessionId, presentation, start, onClose }: { sessionId: string; presentation: MediaPresentation; start: number; onClose: () => void }) {
  const [index, setIndex] = useState(start);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef<{ x: number; y: number; ox: number; oy: number; distance?: number } | null>(null);
  const item = presentation.items[index];
  useEffect(() => { setZoom(1); setOffset({ x: 0, y: 0 }); }, [index]);
  const changeZoom = (next: number) => {
    const bounded = Math.max(1, Math.min(4, next));
    setZoom(bounded);
    if (bounded === 1) setOffset({ x: 0, y: 0 });
  };
  const down = (event: ReactPointerEvent) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    drag.current = { x: event.clientX, y: event.clientY, ox: offset.x, oy: offset.y };
  };
  const move = (event: ReactPointerEvent) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...pointers.current.values()];
    if (points.length === 2) {
      const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      if (drag.current?.distance) changeZoom(zoom * distance / drag.current.distance);
      if (drag.current) drag.current.distance = distance;
    } else if (drag.current && zoom > 1) {
      setOffset({ x: drag.current.ox + event.clientX - drag.current.x, y: drag.current.oy + event.clientY - drag.current.y });
    }
  };
  const up = (event: ReactPointerEvent) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size || !drag.current) return;
    const distance = event.clientX - drag.current.x;
    if (zoom === 1 && Math.abs(distance) > 60) {
      if (distance < 0 && index < presentation.items.length - 1) setIndex((value) => value + 1);
      if (distance > 0 && index > 0) setIndex((value) => value - 1);
    }
    drag.current = null;
  };
  const wheel = (event: ReactWheelEvent) => { event.preventDefault(); changeZoom(zoom * (event.deltaY < 0 ? 1.15 : 0.87)); };
  return <Sheet title={item.alt || item.filename} onClose={onClose} size="full" className="media-viewer" head={<span className="media-count">{index + 1}/{presentation.items.length}</span>}>
    <div className="media-viewer-stage" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onWheel={wheel} onDoubleClick={() => changeZoom(zoom === 1 ? 2 : 1)}>
      <img src={source(sessionId, presentation.id, item.id)} alt={item.alt || item.filename} draggable={false} style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }} />
    </div>
    <div className="media-viewer-tools">
      <button className="btn ghost" type="button" disabled={index === 0} onClick={() => setIndex((v) => v - 1)}>{t("media.previous")}</button>
      <button className="btn ghost" type="button" onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }); }}>{t("preview.fit")}</button>
      <span>{Math.round(zoom * 100)}%</span>
      <a className="btn ghost" href={source(sessionId, presentation.id, item.id)} download={item.filename}>{t("common.download")}</a>
      <button className="btn ghost" type="button" disabled={index === presentation.items.length - 1} onClick={() => setIndex((v) => v + 1)}>{t("media.next")}</button>
    </div>
  </Sheet>;
}

export function InlineMedia({ sessionId, presentation }: { sessionId: string; presentation: MediaPresentation }) {
  const [open, setOpen] = useState<number | null>(null);
  const [access, setAccess] = useState<"loading" | "ready" | "error">("loading");
  const load = () => {
    setAccess("loading");
    requestMediaAccess().then(() => setAccess("ready")).catch(() => setAccess("error"));
  };
  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps -- one credential upgrade per mount
  if (access === "loading") return <div className="inline-media-loading" aria-label={t("media.loading")} />;
  if (access === "error") return <button type="button" className="inline-media-error" onClick={load}>{t("media.retry")}</button>;
  return <>
    <div className={`inline-media ${presentation.layout}`}>
      {presentation.items.map((item, index) => <MediaElement key={item.id} sessionId={sessionId} presentation={presentation} item={item} onOpen={() => item.kind === "image" || item.kind === "animation" ? setOpen(index) : undefined} />)}
    </div>
    {open !== null && <Viewer sessionId={sessionId} presentation={presentation} start={open} onClose={() => setOpen(null)} />}
  </>;
}
