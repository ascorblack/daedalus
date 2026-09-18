// A document link resolves within its file root; a URL never becomes an authenticated fetch.

export function localHtmlPath(href: string, path: string): string | null {
  if (/^[a-z][a-z0-9+.-]*:|^\/\/|^\//i.test(href)) return null;
  const [file, hash] = href.split("#", 2);
  const parts = file ? path.split("/").slice(0, -1) : path.split("/");
  for (const part of file.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  return parts.join("/") + (hash !== undefined ? `#${hash}` : "");
}
