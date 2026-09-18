// Syntax colours for the file viewer and the code blocks: a small scanner, one pass, with the few
// distinctions a reader's eye actually uses — comments, strings, numbers, keywords, the name being
// called, a tag and its attributes. No grammar per language, only the pieces each one spells
// differently: how a comment starts, which quotes a string takes, which words are keywords.

export type Lang = "js" | "ts" | "py" | "sh" | "json" | "css" | "html" | "sql" | "yaml" | "toml" | "rust" | "go" | "c" | "java" | "ruby" | "php" | "diff" | "md" | "text";

type Spec = {
  line?: string[];
  block?: [string, string][];
  quotes?: string[];
  triple?: boolean;
  keywords: Set<string>;
  hash?: boolean;
  tags?: boolean;
  css?: boolean;
};

const words = (s: string) => new Set(s.split(" "));

const JS = words("const let var function return if else for while do switch case break continue new delete typeof instanceof in of class extends super this import export from default async await yield try catch finally throw void null undefined true false static get set as interface type enum implements declare readonly namespace abstract private public protected");
const PY = words("def class return if elif else for while in not and or is lambda import from as pass break continue try except finally raise with yield global nonlocal assert del async await True False None self");
const SH = words("if then else elif fi for while do done case esac in function return exit export local readonly set unset shift source true false echo cd");
const SQL = words("select from where and or not in is null as join left right inner outer on group by order having limit offset insert into values update set delete create table drop alter index primary key unique default with distinct union all case when then else end like between exists");
const RUST = words("fn let mut pub struct enum impl trait for in while loop if else match return use mod crate self super as const static ref move async await dyn where type unsafe true false Some None Ok Err");
const GO = words("func package import var const type struct interface map chan go defer return if else for range switch case default break continue select fallthrough nil true false");
const C = words("int char long short float double void unsigned signed const static struct union enum typedef return if else for while do switch case break continue default sizeof extern inline volatile register goto class public private protected virtual template typename namespace using new delete this true false nullptr bool auto override");
const JAVA = words("public private protected static final abstract class interface extends implements new return if else for while do switch case break continue default void int long short byte char boolean float double this super null true false import package try catch finally throw throws instanceof var enum record");
const RUBY = words("def end class module if elsif else unless while until for in do return yield begin rescue ensure raise self nil true false and or not require include attr_accessor lambda proc");
const PHP = words("function return if else elseif for foreach while do switch case break continue class new extends implements public private protected static echo null true false use namespace try catch finally throw as array");
const YAML = words("true false null yes no on off");

const SPECS: Record<Lang, Spec> = {
  js: { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "'", "`"], keywords: JS },
  ts: { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "'", "`"], keywords: JS },
  py: { line: ["#"], quotes: ['"', "'"], triple: true, keywords: PY },
  sh: { line: ["#"], quotes: ['"', "'"], keywords: SH },
  json: { quotes: ['"'], keywords: words("true false null") },
  css: { block: [["/*", "*/"]], quotes: ['"', "'"], keywords: new Set(), css: true },
  html: { block: [["<!--", "-->"]], quotes: ['"', "'"], keywords: new Set(), tags: true },
  sql: { line: ["--"], block: [["/*", "*/"]], quotes: ["'", '"'], keywords: SQL },
  yaml: { line: ["#"], quotes: ['"', "'"], keywords: YAML },
  toml: { line: ["#"], quotes: ['"', "'"], keywords: words("true false") },
  rust: { line: ["//"], block: [["/*", "*/"]], quotes: ['"'], keywords: RUST },
  go: { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "`"], keywords: GO },
  c: { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "'"], keywords: C, hash: true },
  java: { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "'"], keywords: JAVA },
  ruby: { line: ["#"], quotes: ['"', "'"], keywords: RUBY },
  php: { line: ["//", "#"], block: [["/*", "*/"]], quotes: ['"', "'"], keywords: PHP },
  diff: { keywords: new Set() },
  md: { keywords: new Set() },
  text: { keywords: new Set() },
};

const BY_EXT: Record<string, Lang> = {
  js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "ts", tsx: "ts", mts: "ts",
  py: "py", pyi: "py", sh: "sh", bash: "sh", zsh: "sh", json: "json", jsonl: "json", css: "css", scss: "css", less: "css",
  html: "html", htm: "html", xml: "html", svg: "html", vue: "html", sql: "sql", yml: "yaml", yaml: "yaml", toml: "toml", ini: "toml", cfg: "toml", conf: "toml", env: "sh",
  rs: "rust", go: "go", c: "c", h: "c", cpp: "c", hpp: "c", cc: "c", cs: "java", java: "java", kt: "java", swift: "java", rb: "ruby", php: "php",
  diff: "diff", patch: "diff", md: "md", markdown: "md",
};

const BY_NAME: Record<string, Lang> = { dockerfile: "sh", makefile: "sh", justfile: "sh" };

/** Which colours a file takes, by its name; "text" for anything unknown. */
export function langOf(name: string): Lang {
  const base = name.split("/").pop()?.toLowerCase() ?? "";
  if (BY_NAME[base]) return BY_NAME[base];
  const ext = base.includes(".") ? base.split(".").pop()! : "";
  return BY_EXT[ext] ?? "text";
}

/** What a fenced block's label means, for the markdown code blocks. */
export function langOfLabel(label: string): Lang {
  const l = label.toLowerCase();
  const alias: Record<string, Lang> = { javascript: "js", typescript: "ts", python: "py", shell: "sh", bash: "sh", zsh: "sh", console: "sh", jsonc: "json", rust: "rust", golang: "go", "c++": "c", cpp: "c", csharp: "java", kotlin: "java", yaml: "yaml", yml: "yaml", html: "html", xml: "html", css: "css", sql: "sql", diff: "diff", patch: "diff", markdown: "md", md: "md", ruby: "ruby", php: "php" };
  return alias[l] ?? (l in SPECS ? (l as Lang) : "text");
}

export type Token = { cls: string; text: string };

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** Above this many lines the viewer shows the file plain: colouring it would cost more than it says. */
export const HIGHLIGHT_MAX_LINES = 5000;

function startsAt(s: string, i: number, what: string): boolean {
  return s.startsWith(what, i);
}

/** The whole text as tokens; a token may span lines (a block comment, a template string). */
export function tokenize(code: string, lang: Lang): Token[] {
  const spec = SPECS[lang] ?? SPECS.text;
  if (lang === "diff") return tokenizeDiff(code);
  if (lang === "md" || lang === "text") return [{ cls: "", text: code }];
  const out: Token[] = [];
  let i = 0;
  let plain = "";
  const flush = () => {
    if (plain) out.push({ cls: "", text: plain });
    plain = "";
  };
  const push = (cls: string, text: string) => {
    flush();
    out.push({ cls, text });
  };
  const n = code.length;
  let inTag = false;
  while (i < n) {
    const c = code[i];
    // comments
    let matched = false;
    for (const open of spec.line ?? []) {
      if (startsAt(code, i, open) && !(open === "#" && spec.hash && i === 0)) {
        let j = code.indexOf("\n", i);
        if (j < 0) j = n;
        push("c", code.slice(i, j));
        i = j;
        matched = true;
        break;
      }
    }
    if (matched) continue;
    for (const [open, close] of spec.block ?? []) {
      if (startsAt(code, i, open)) {
        let j = code.indexOf(close, i + open.length);
        j = j < 0 ? n : j + close.length;
        push("c", code.slice(i, j));
        i = j;
        matched = true;
        break;
      }
    }
    if (matched) continue;
    // a preprocessor line
    if (spec.hash && c === "#" && (i === 0 || code[i - 1] === "\n")) {
      let j = code.indexOf("\n", i);
      if (j < 0) j = n;
      push("k", code.slice(i, j));
      i = j;
      continue;
    }
    // strings — in markup, only inside a tag: an apostrophe in running text is not a quote
    if (spec.quotes?.includes(c) && (!spec.tags || inTag)) {
      const triple = spec.triple && startsAt(code, i, c + c + c);
      const q = triple ? c + c + c : c;
      let j = i + q.length;
      while (j < n) {
        if (code[j] === "\\") j += 2;
        else if (startsAt(code, j, q)) {
          j += q.length;
          break;
        } else if (!triple && c !== "`" && code[j] === "\n") break;
        else j++;
      }
      push("s", code.slice(i, Math.min(j, n)));
      i = Math.min(j, n);
      continue;
    }
    // markup
    if (spec.tags) {
      if (c === "<" && /[A-Za-z/!?]/.test(code[i + 1] ?? "")) {
        let j = i + 1;
        while (j < n && /[A-Za-z0-9:_/!?-]/.test(code[j])) j++;
        push("t", code.slice(i, j));
        i = j;
        inTag = true;
        continue;
      }
      if (inTag && (c === ">" || startsAt(code, i, "/>"))) {
        const len = c === ">" ? 1 : 2;
        push("t", code.slice(i, i + len));
        i += len;
        inTag = false;
        continue;
      }
      if (inTag && /[A-Za-z_:@-]/.test(c)) {
        let j = i;
        while (j < n && /[A-Za-z0-9_:.@-]/.test(code[j])) j++;
        push("a", code.slice(i, j));
        i = j;
        continue;
      }
      if (!inTag) {
        // text between tags stays plain, up to the next tag or comment
        let j = i + 1;
        while (j < n && code[j] !== "<") j++;
        plain += code.slice(i, j);
        i = j;
        continue;
      }
    }
    // stylesheets: a property before a colon inside a block, a selector outside
    if (spec.css) {
      if (/[A-Za-z_-]/.test(c)) {
        let j = i;
        while (j < n && /[A-Za-z0-9_-]/.test(code[j])) j++;
        const word = code.slice(i, j);
        const rest = code.slice(j, j + 40);
        if (/^\s*:/.test(rest) && !/^\s*:[A-Za-z]/.test(rest)) push("a", word);
        else if (c === "-" && word.startsWith("--")) push("a", word);
        else plain += word;
        i = j;
        continue;
      }
      if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(code[i + 1] ?? ""))) {
        let j = i;
        while (j < n && /[0-9.%a-z]/i.test(code[j])) j++;
        push("n", code.slice(i, j));
        i = j;
        continue;
      }
      if (c === "#" && /[0-9a-f]{3,8}\b/i.test(code.slice(i + 1, i + 10))) {
        const m = /^#[0-9a-f]{3,8}/i.exec(code.slice(i))!;
        push("n", m[0]);
        i += m[0].length;
        continue;
      }
      plain += c;
      i++;
      continue;
    }
    // numbers
    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(code[i + 1] ?? ""))) {
      let j = i;
      while (j < n && /[0-9a-fA-FxXoObB._]/.test(code[j])) j++;
      push("n", code.slice(i, j));
      i = j;
      continue;
    }
    // words: a keyword, a name being called, or nothing
    if (/[A-Za-z_$]/.test(c)) {
      let j = i;
      while (j < n && /[A-Za-z0-9_$]/.test(code[j])) j++;
      const word = code.slice(i, j);
      if (spec.keywords.has(lang === "sql" ? word.toLowerCase() : word)) push("k", word);
      else if (code[j] === "(") push("f", word);
      else if (lang === "yaml" && /^\s*:/.test(code.slice(j, j + 2)) && (i === 0 || /[\n\s-]/.test(code[i - 1]))) push("a", word);
      else if (lang === "toml" && /^\s*=/.test(code.slice(j, j + 3))) push("a", word);
      else plain += word;
      i = j;
      continue;
    }
    plain += c;
    i++;
  }
  flush();
  return out;
}

function tokenizeDiff(code: string): Token[] {
  return code.split("\n").flatMap((line, i, all) => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "t" : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("diff ") || line.startsWith("index ") ? "c" : "";
    const toks: Token[] = [{ cls, text: line }];
    if (i < all.length - 1) toks.push({ cls: "", text: "\n" });
    return toks;
  });
}

/** The code as HTML, one string per line, every span closed at the line's end and reopened after. */
export function highlightLines(code: string, lang: Lang): string[] {
  const lines: string[] = [];
  let cur = "";
  for (const tok of tokenize(code, lang)) {
    const parts = tok.text.split("\n");
    parts.forEach((part, i) => {
      if (i > 0) {
        lines.push(cur);
        cur = "";
      }
      if (!part) return;
      cur += tok.cls ? `<span class="tk-${tok.cls}">${esc(part)}</span>` : esc(part);
    });
  }
  lines.push(cur);
  return lines;
}

/** The code as one HTML string, for a code block in an answer. */
export function highlight(code: string, lang: Lang): string {
  return highlightLines(code, lang).join("\n");
}
