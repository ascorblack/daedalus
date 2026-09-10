// One icon set for the whole app: 24-unit stroked paths, drawn with the current text colour.

export type IconName =
  | "back" | "more" | "plus" | "up" | "stop" | "model" | "terminal" | "file" | "pen" | "search" | "globe" | "attach" | "image"
  | "question" | "skill" | "spawn" | "bulb" | "wrench" | "clock" | "plug" | "dot" | "compact"
  | "folder" | "settings" | "bots" | "inbox" | "board" | "changes" | "chart" | "loop" | "pause" | "play" | "trash" | "check" | "close" | "send"
  | "download" | "share" | "split" | "key" | "link" | "down" | "copy" | "columns" | "eye" | "mic";

const PATHS: Record<IconName, string> = {
  back: "M15 18l-6-6 6-6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  plus: "M12 5v14M5 12h14",
  up: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  model: "M4 12l8-8 8 8-8 8-8-8z",
  terminal: "M4 5h16v14H4zM7 9l3 3-3 3M12 15h5",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  pen: "M4 20l4-1 11-11-3-3L5 16zM13 6l3 3",
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
  attach: "M21 12l-8 8a5 5 0 0 1-7-7l9-9a3 3 0 0 1 4 4l-9 9a1 1 0 0 1-2-2l8-8",
  image: "M4 5h16v14H4zM8 13l3-3 4 4 2-2 3 3",
  question: "M9 9a3 3 0 1 1 4 3c-1 .5-1 1-1 2M12 17h.01",
  skill: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  spawn: "M12 3v6M12 15v6M3 12h6M15 12h6",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3 11v1h6v-1a6 6 0 0 0-3-11z",
  wrench: "M14 4a5 5 0 0 0 6 6l-9 9-3-3 9-9a5 5 0 0 0-3-3z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  plug: "M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3",
  dot: "M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  compact: "M4 7h16M4 12h10M4 17h6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  bots: "M5 8h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zM12 4v4M9 13h.01M15 13h.01M9 17h6",
  inbox: "M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6z",
  board: "M4 5h16M4 12h16M4 19h10",
  changes: "M6 3v12M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9c0 4-4 4-8 6",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  loop: "M4 12a8 8 0 0 1 14-5.3L20 8M20 4v4h-4M20 12a8 8 0 0 1-14 5.3L4 16M4 20v-4h4",
  pause: "M8 6h3v12H8zM13 6h3v12h-3z",
  play: "M7 5l12 7-12 7z",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  check: "M5 12l5 5 9-11",
  close: "M6 6l12 12M18 6L6 18",
  send: "M4 12l16-8-6 16-2-6z",
  download: "M12 4v12M6 10l6 6 6-6M4 20h16",
  share: "M4 12v8h16v-8M12 3v12M7 8l5-5 5 5",
  split: "M4 5h16v14H4zM12 5v14",
  key: "M15 3a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM10 14l-7 7M6 18l2 2M9 15l2 2",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  down: "M12 5v14M5 12l7 7 7-7",
  copy: "M8 8h12v12H8zM16 8V4H4v12h4",
  columns: "M3 5h18v14H3zM12 5v14",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM6 11a6 6 0 0 0 12 0M12 17v4M9 21h6",
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg className={`ic ic-${name}`} viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
