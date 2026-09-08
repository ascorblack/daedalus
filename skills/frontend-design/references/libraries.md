# Front-end libraries by task

Pick by what the page has to *do*, not by habit. One library per concern; a landing page rarely
needs more than two or three. Every entry: what it is for, how to load it, and the trap.

Loading. Vite or a bundle (`web-artifacts-builder`) is the default for an app. A single static
page can load from a CDN: `https://cdn.jsdelivr.net/npm/<pkg>@<version>/<file>` or
`https://unpkg.com/<pkg>@<version>/<file>`; always pin the version. When the host forbids
external scripts (a strict content-security policy, as on Get Posting Board), vendor the file
next to the page or bundle it. Import maps (`<script type="importmap">`) let plain ES modules
resolve bare specifiers without a build.

## Charts and data

| library | use it for | load | trap |
|---|---|---|---|
| Chart.js | the everyday chart: line, bar, doughnut, radar; responsive by default | `chart.js` (UMD `chart.umd.js`) | set a fixed-height container or it grows forever |
| Apache ECharts | dashboards, dense series, maps, big data, themes, animation between states | `echarts` (`dist/echarts.min.js`) | 1 MB; use the modular build or the `echarts/core` tree-shaken import |
| Observable Plot | quick exploratory and editorial charts from arrays of objects, grammar-of-graphics style | `@observablehq/plot` (needs `d3`) | not interactive by default; add tips with `tip: true` |
| D3 | anything bespoke: custom scales, force layouts, hand-drawn axes, animated transitions | `d3` (`dist/d3.min.js`) | it is a toolkit, not a chart library: budget the time |
| uPlot | tens of thousands of points on a time axis, live streaming, tiny footprint | `uplot` (+ `uPlot.min.css`) | series must share one x array |
| Plotly.js | scientific and 3D plots, contour, statistical, out-of-the-box zoom and export | `plotly.js-dist-min` | 3 MB; only when the science needs it |
| Recharts / visx | charts inside React (declarative / low-level primitives) | npm | Recharts hides layout maths, visx exposes it |
| Tabulator, TanStack Table | tabular data with sort, filter, virtualised rows | `tabulator-tables` / npm | TanStack is headless: bring the markup |
| TanStack Virtual, virtua | long lists that must scroll at 60 fps | npm | measure row heights or expect jumps |
| Leaflet, MapLibre GL | maps: tile maps and markers / vector tiles and 3D terrain | `leaflet` (+ css) / `maplibre-gl` (+ css) | tile providers need attribution and often a key |
| deck.gl | large geo point clouds, arcs, hexbins on top of MapLibre | `deck.gl` | WebGL2 required |
| Mermaid | flowcharts, sequence and Gantt diagrams from text | `mermaid` | render after fonts load or text overflows |

## Motion and animation

| library | use it for | load | trap |
|---|---|---|---|
| Motion (`motion`, ex-Framer Motion) | UI transitions, layout animation, gestures, springs; vanilla and React | `motion` (`motion.js`) / npm | keep transforms on `transform` and `opacity` only |
| GSAP + ScrollTrigger | choreographed sequences, scroll-scrubbed timelines, pinning (see `Skill("gsap")`) | `gsap` (+ `ScrollTrigger.min.js`) | one timeline per section; kill triggers on unmount |
| Lenis | smooth scrolling that stays native-feeling; pairs with GSAP | `lenis` | one instance per page; respect `prefers-reduced-motion` |
| anime.js | lightweight tweening with staggering and SVG line drawing | `animejs` | v4 API differs from the v3 tutorials |
| Lottie (`lottie-web`), dotLottie | vector animations exported from After Effects / LottieFiles | `lottie-web` | huge JSON slows load; prefer `.lottie` |
| Rive | interactive state-machine animations (buttons, mascots, loaders) | `@rive-app/canvas` | needs a `.riv` file made in Rive |
| AutoAnimate | zero-config add/remove/move transitions for lists | `@formkit/auto-animate` | no control over easing |
| View Transitions API | page and state transitions without a library (`document.startViewTransition`) | native | Chromium-first; feature-detect |
| Theatre.js | keyframe editor for complex sequences and 3D | npm | tooling-heavy; only for real productions |
| Splitting / SplitType | split text into chars and words for reveals | `splitting` / `split-type` | re-split on resize |
| CSS scroll-driven animations | scroll-linked progress bars, parallax, reveals with zero JS (`animation-timeline: scroll()`) | native | Safari lag; keep a static fallback |

## 3D, canvas and visual effects

| library | use it for | load | trap |
|---|---|---|---|
| Three.js | real 3D scenes, hero objects, shaders (see `Skill("threejs")`) | `three` (ES modules, use an import map) | dispose geometries and renderers on unmount |
| React Three Fiber + drei | Three.js inside React with helpers (cameras, controls, text) | npm | Suspense boundaries for loaders |
| p5.js | generative and algorithmic art, sketches, seeded randomness | `p5` | one global sketch; use instance mode inside apps |
| PixiJS | 2D WebGL sprites, particles, games, thousands of moving things | `pixi.js` | not for text-heavy UI |
| tsParticles | configurable particle backgrounds and confetti | `tsparticles` | cap particle counts on mobile |
| canvas-confetti | one-line celebratory confetti | `canvas-confetti` | fire on success only, once |
| Vanta.js | animated WebGL backgrounds (waves, fog, net, birds) | `vanta` (needs `three`) | heavy on battery; pause off-screen |
| cobe, globe.gl | small spinning globe / data globe with arcs and points | `cobe` / `globe.gl` | cobe is 5 KB, globe.gl brings Three |
| Shaders, OGL | WebGL/WebGPU shader effects with a tiny runtime | `@paper-design/shaders` / `ogl` | provide a static fallback image |
| html2canvas, dom-to-image-more | rasterise DOM to PNG for share cards | `html2canvas` | cross-origin images taint the canvas |
| SVG filters (`feTurbulence`, `feDisplacementMap`, `feGaussianBlur`) | gooey blobs, grain, distortion without any library | native | expensive on large areas; keep filtered regions small |

## Interaction and UX components

| library | use it for | load | trap |
|---|---|---|---|
| Floating UI | positioning tooltips, popovers, dropdowns that stay in the viewport | `@floating-ui/dom` | positioning only; bring the markup and a11y |
| Tippy.js | ready-made tooltips and popovers on top of Popper | `tippy.js` (+ css) | one theme per site |
| Radix Primitives, Headless UI, Ark UI | accessible unstyled components (dialog, menu, tabs, combobox) | npm | React (Radix), React/Vue (Headless), any (Ark) |
| shadcn/ui | Radix + Tailwind components you copy into the project (see `web-artifacts-builder`) | CLI | restyle the tokens or every site looks like shadcn |
| cmdk | command palette / ⌘K menu | npm (React) | give it real actions, not decoration |
| Sonner | toast notifications | npm (React) | one toaster per app |
| Vaul | bottom-sheet drawers on mobile | npm (React) | iOS scroll locking needs testing |
| Swiper, Embla | carousels and sliders (full-featured / minimal) | `swiper` (+ css) / `embla-carousel` | never autoplay without pause control |
| SortableJS, dnd-kit, Pragmatic drag and drop | drag-and-drop lists, kanban boards | `sortablejs` / npm | provide a keyboard path too |
| Tiptap, Lexical, Quill | rich text editors | npm | sanitise the HTML you store |
| CodeMirror 6, Monaco | code editors (light / full VS Code) | npm | Monaco is 5 MB |
| Shiki, Prism | syntax highlighting of static code (accurate / small) | `shiki` / `prismjs` | Shiki highlights at build or on a worker |
| Fuse.js, MiniSearch, FlexSearch | client-side fuzzy / full-text search | `fuse.js` / `minisearch` | build the index once |
| date-fns, Day.js, Temporal | dates, durations, formatting | `date-fns` / `dayjs` | always format in the viewer's zone |
| Intl (`NumberFormat`, `RelativeTimeFormat`, `ListFormat`) | numbers, currencies, relative time, lists, plurals | native | pass the locale |
| Zod, Valibot | form and payload validation with typed schemas | npm | show messages next to the field |
| htmx, Alpine.js | server-driven interactivity / small reactive sprinkles without a framework | `htmx.org` / `alpinejs` | do not mix with a big SPA framework |
| Web Components (Lit) | reusable widgets that survive any framework | `lit` | style with `::part` and CSS variables |
| Hotkeys (`hotkeys-js`, `tinykeys`) | keyboard shortcuts | npm | list them in a help sheet |
| Driver.js, Shepherd | product tours and onboarding highlights | `driver.js` | one short tour, skippable |
| Vaul, Radix Dialog + `inert` | focus trapping in modals | npm / native | restore focus on close |

## Typography, icons and imagery

| resource | use it for | load | trap |
|---|---|---|---|
| Google Fonts, Fontsource | web fonts (hosted / self-hosted npm packages) | `<link>` / `@fontsource/<family>` | subset and `font-display: swap`; self-host under a strict CSP |
| Variable fonts (Inter, Roboto Flex, Fraunces, Bricolage Grotesque, Instrument Serif) | one file, every weight and optical size | Google Fonts `wght,opsz` axes | animate `font-variation-settings` sparingly |
| Bundled canvas fonts | 30 open typefaces in `skills/canvas-design/canvas-fonts/` for local rendering | file | licences in the same directory |
| Lucide, Phosphor, Tabler, Heroicons, Remix Icon | icon sets (clean / expressive weights / large / Tailwind-native / broad) | `lucide` / `@phosphor-icons/web` / npm | one set per product, one stroke width |
| Iconify | any icon set through one API, including Simple Icons brand logos | `@iconify/iconify` or inline SVG | inline the SVG for offline and CSP |
| Simple Icons | brand and company logos as SVG | `simple-icons` | respect brand colours from the metadata |
| unDraw, Humaaans, Open Peeps | illustrations that can be recoloured to the palette | SVG | recolour to the palette or they look pasted in |
| Unsplash, Pexels (see `apps`/brand references for art direction) | photography | URL with size params | credit where required; do not hotlink at full size |
| Squoosh, sharp | image compression and format conversion (AVIF/WebP) | CLI / npm | serve `srcset` with 2× variants |
| Satori, `@vercel/og` | generate OG share images from JSX/HTML | npm | fonts must be loaded explicitly |

## Layout, styling and effects

| library | use it for | load | trap |
|---|---|---|---|
| Tailwind CSS (see `Skill("tailwindcss")`) | utility styling, design tokens in `@theme`, responsive variants | CLI / Vite plugin / play CDN for prototypes | do not ship the play CDN in production |
| Open Props | ready CSS custom-property scales (sizes, easings, shadows, gradients) | `open-props` | pick a subset |
| CSS layers, container queries, `:has()`, subgrid, `color-mix()`, `oklch()` | modern CSS that removes JS and media queries | native | check the baseline for the audience |
| Masonry (`masonry-layout`), CSS `grid-template-rows: masonry` | Pinterest-style walls | `masonry-layout` | reserve aspect ratios to avoid jumps |
| Progressive blur, gradient borders, dithering | see `Skill("progressive-blur")` and the `styles/` and `systems/` entries that use them | CSS | backdrop-filter costs GPU; limit the area |
| Panda CSS, vanilla-extract, Stitches | typed styling in TS projects | npm | one system per project |
| normalize / modern-normalize | consistent baseline | `modern-normalize` | Tailwind preflight already includes one |

## Feedback, state and quality

| library | use it for | load | trap |
|---|---|---|---|
| NProgress, Pace | thin top progress bar during navigation | `nprogress` (+ css) | finish it on error too |
| Skeleton screens (CSS), `content-visibility` | perceived performance | native | match skeleton to the final layout |
| web-vitals | measure LCP, INP, CLS in the field (see `Skill("core-web-vitals")`) | `web-vitals` | send beacons, do not log to console in production |
| axe-core, Lighthouse | accessibility and quality audits (see `Skill("accessibility")`) | `npx lighthouse`, `axe-core` | automated checks catch a third of the issues |
| Playwright | screenshots at 360 and 1440 for review (see `Skill("webapp-testing")`) | installed here | look at the screenshot with `ImageView` |
