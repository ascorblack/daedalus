// Everything a live terminal needs, in one module that only `load.ts` imports, and only dynamically:
// xterm.js and its addons are several hundred kilobytes that a page without a terminal never loads.
// `bundle.test.ts` holds that line.

import "@xterm/xterm/css/xterm.css";

export { Terminal } from "@xterm/xterm";
export { ClipboardAddon } from "@xterm/addon-clipboard";
export { FitAddon } from "@xterm/addon-fit";
export { ProgressAddon } from "@xterm/addon-progress";
export { SearchAddon } from "@xterm/addon-search";
export { WebLinksAddon } from "@xterm/addon-web-links";
export { WebglAddon } from "@xterm/addon-webgl";
export { GhosttyUnicodeAddon } from "./unicode";
export { swallowQueries } from "./queries";
