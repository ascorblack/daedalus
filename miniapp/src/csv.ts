/** CSV/TSV with quoted fields; the delimiter is guessed from the first line. */
export function parseCsv(text: string, max = 2000, separator?: string): string[][] {
  const counts = new Map([[",", 0], [";", 0], ["\t", 0], ["|", 0]]);
  let inQuote = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') { if (inQuote && text[i + 1] === '"') i++; else inQuote = !inQuote; }
    else if (!inQuote && (c === "\n" || c === "\r")) break;
    else if (!inQuote && counts.has(c)) counts.set(c, counts.get(c)! + 1);
  }
  const delimiter = separator ?? [...counts].sort((a, b) => b[1] - a[1])[0][0];
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cell += c;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === delimiter) {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      if (rows.length >= max) return rows;
    } else cell += c;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}
