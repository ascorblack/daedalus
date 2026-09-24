// What in a terminal's text can be clicked: file references and local server addresses.
//
// A compiler, a test runner or a stack trace prints `src/cart.ts:42:7`; a click should open that file
// at that line in the app's preview, which reads the session's workspace. So a reference is resolved
// against the terminal's working directory and then made relative to the workspace, the way the rest of
// the app does it — and one that lands outside the workspace is not a link at all, because the preview
// could not open it.
//
// A dev server in a terminal prints `http://localhost:5173`. The browser showing the app is often on
// another machine, where that address means itself; when the port is one the environment publishes,
// the link is rewritten to the address it is published on.

export type FileLink = {
  /** Offsets into the line, end exclusive. */
  start: number;
  end: number;
  path: string;
  line?: number;
  col?: number;
};

// A path: optionally rooted (`/`, `./`, `../`, `~/`), then name segments separated by `/`. A name is
// letters, digits and `._-@+`; a run of `../` is allowed anywhere a segment is.
const PATH = String.raw`(?:~\/|\.{1,2}\/|\/)?(?:[\w.@+-]+\/)*[\w.@+-]+`;
const REFERENCE = new RegExp(String.raw`(${PATH})(?::(\d+)(?::(\d+))?)?`, "g");
const EXTENSION = /\.[A-Za-z0-9]{1,8}$/;
// Characters that, right before a match, mean it is the middle of something else (a URL, a word).
const GLUED = /[\w:/.@+~-]/;

/**
 * File references in one line of terminal text. A reference counts when it has a line number
 * (`app.py:12`) or when it is clearly a path to a file (`src/app.py`, `./run.sh`); a lone word, a
 * version number or a time of day does not.
 */
export function findFileLinks(text: string): FileLink[] {
  const links: FileLink[] = [];
  REFERENCE.lastIndex = 0;
  for (let m = REFERENCE.exec(text); m; m = REFERENCE.exec(text)) {
    const [whole, path, line, col] = m;
    const start = m.index;
    if (start > 0 && GLUED.test(text[start - 1])) continue;
    if (/\w:\/\/\S*$/.test(text.slice(0, start))) continue; // inside a URL
    if (/^\d[\d.]*$/.test(path)) continue; // 1.2.3, 12:30
    const pathLike = path.includes("/") && EXTENSION.test(path);
    const rooted = /^(?:\.{1,2}\/|\/|~\/)/.test(path) && path.length > 2;
    if (!line && !pathLike && !rooted) continue;
    if (line && !EXTENSION.test(path) && !path.includes("/")) continue; // "host:8080", "error:3"
    const link: FileLink = { start, end: start + whole.length, path };
    if (line) link.line = Number(line);
    if (col) link.col = Number(col);
    links.push(link);
  }
  return links;
}

function normalize(path: string): string | null {
  const out: string[] = [];
  for (const part of path.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!out.length) return null;
      out.pop();
    } else out.push(part);
  }
  return "/" + out.join("/");
}

/**
 * A reference as a path inside the workspace, or null when it points elsewhere. Relative references
 * are read against the terminal's working directory; `~` is the terminal's home, which is never the
 * workspace.
 */
export function resolveFileLink(path: string, cwd: string, workspace: string): string | null {
  if (!path || path.startsWith("~")) return null;
  const absolute = normalize(path.startsWith("/") ? path : `${cwd.replace(/\/+$/, "")}/${path}`);
  const root = normalize(workspace);
  if (!absolute || !root || root === "/") return null;
  if (absolute === root) return null;
  if (!absolute.startsWith(root + "/")) return null;
  return absolute.slice(root.length + 1);
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "[::]"]);

/** Parses "lo-hi" into its bounds; null when it is not a range. */
export function parsePortRange(range: string): [number, number] | null {
  const m = /^\s*(\d+)\s*-\s*(\d+)\s*$/.exec(range ?? "");
  if (!m) return null;
  const lo = Number(m[1]);
  const hi = Number(m[2]);
  return lo <= hi ? [lo, hi] : null;
}

/**
 * A loopback URL on a port the environment publishes, pointed at the address it is published on.
 * Anything else — another host, a port outside the range, no public address known — comes back as
 * it was.
 */
export function rewriteLoopbackUrl(url: string, env: { port_range: string; public_host?: string }): string {
  const host = (env.public_host ?? "").trim();
  const range = parsePortRange(env.port_range);
  if (!host || !range) return url;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return url;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return url;
  if (!LOOPBACK.has(parsed.hostname) && !LOOPBACK.has(`[${parsed.hostname}]`)) return url;
  const port = Number(parsed.port);
  if (!port || port < range[0] || port > range[1]) return url;
  parsed.hostname = host;
  return parsed.toString();
}
