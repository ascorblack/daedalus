# Uber — brand reference

- Category: consumer-tech
- Site: https://www.uber.com
- Primary colour: #000000
- Keywords: bold, confident, minimal, efficient, direct, black

## 1. Visual Theme & Atmosphere

Uber operates a marketplace that connects people who ride, earn, eat, deliver, and sell; its stated mission is to reimagine how the world moves for the better. The current Korean public web capture expresses that broad mobility role through high-contrast black navigation, white type, and compact capsule controls rather than a broad decorative palette. Across the home, About, and Accessibility pages, the same header geometry recurs: black or white 999px controls set in UberMoveText, with an 8px-radius menu panel appearing when a header menu is expanded. Uber's 2018 official “A new look” announcement framed a new logo and mission around clearer recognition for riders, while the current capture confirms the sober black-and-white public chrome is still live. This reference documents that public marketing/corporate surface only; it does not claim the authenticated rider, earner, courier, or merchant product UI uses the same tokens.

## 2. Color Palette & Roles

### Observed public-site foundation

- **Ink** (`#000000`): Header background, dark capsule controls, foreground text, and borders in the 2026-07-13 capture.
- **Paper** (`#ffffff`): Inverse text on dark header controls, light control fill, and expanded menu surface.
- **Soft control** (`#efefef`): Home-only capsule-control fill at `home::[data-omd-capture="9"]`.
- **Interactive light gray** (`#e2e2e2`): Observed on the light header menu control on About and Accessibility capture states.
- **Muted** (`#757575`) and **subtle** (`#afafaf`): Repeated public-site text/border observations; use only where the local component evidence calls for de-emphasis.

The capture also contains `#0000ee` in Accessibility-page link content and a single red pressed-link observation. They are content/link observations, not promoted as Uber brand or semantic-status tokens.

## 3. Typography Rules

### Evidence classes

- **Live computed surface-use — UberMoveText:** Computed on 414 visible public-surface elements and corroborated by the collector's loaded FontFaceSet result. It appears on body, headings, controls, inputs, and menu text.
- **Live computed surface-use — UberMove:** Computed on 31 visible public-surface elements and corroborated by a loaded FontFaceSet result. Observed on public h1/h2/h3 text, including 52px/700 and 36px/700 headings.
- **System use:** `sans-serif` is also computed on 121 public-surface elements, including the expanded menu. It remains system text and is not a substitute rendered as either Uber family.
- **Declared-only:** `Book`, `Medium`, `NarrowBook`, `NarrowMedium`, `NarrowNews`, `NarrowThin`, `News`, and `Thin` have `@font-face` declarations in the capture but no visible use; no relationship to the UberMove families is asserted.
- **Official product-use and license:** The reviewed first-party brand and corporate sources establish Uber's mission and public design/code context, but did not provide a public font-license term or an app/product font-use declaration. The collector contains no font source URL. Do not infer redistribution permission or browser-loadable external specimens from the loaded-family result.

### Observed public-surface hierarchy

| Role | Family | Size | Weight | Line height | Surface boundary |
|---|---|---:|---:|---:|---|
| Hero h1 | UberMove | 52px | 700 | 64px | Public home/About/Accessibility capture |
| Section h2 | UberMove | 36px | 700 | 44px | Public capture |
| Small heading | UberMove | 24px | 700 | 32px | Public capture |
| Body | UberMoveText | 16px | 400 | 24px | Public capture |
| Strong UI text | UberMoveText | 16px | 500 | 20px | Public capture |
| Header control | UberMoveText | 14px | 500 | 16px | Public header |
| Menu/caption | UberMoveText | 14px | 400 | 20px | Public header menu |
| Fine print | UberMoveText | 12px | 400 | 20px | Public capture |

Base Web is an Uber-published React component library under an MIT repository license. That code license is separate from UberMove/UberMoveText licensing and does not prove the current public-site font runtime or product UI implementation.

## 4. Component Stylings

### Public header controls

**Dark capsule control**
- Background: `#000000`
- Text: `#ffffff`
- Radius: `999px`
- Padding: `10px 12px`
- Font: `14px / 500 / UberMoveText`
- Hover: `rgba(255,255,255,0.1) 999px 999px 0px 0px inset`
- Pressed: `rgba(255,255,255,0.2) 999px 999px 0px 0px inset`
- Use: Public-site header control on home, About, and Accessibility; representative selector `home::[data-omd-capture="3"]`.

**Light capsule menu control**
- Background: `#ffffff`
- Text: `#000000`
- Radius: `999px`
- Padding: `10px 12px`
- Font: `14px / 500 / UberMoveText`
- Focus: `#fbfbfb` on the home capture; `#fbfbfb`/`#fdfdfd` on captured About/Accessibility focus states.
- Hover: `rgba(255,255,255,0.1) 999px 999px 0px 0px inset`
- Pressed: `rgba(255,255,255,0.2) 999px 999px 0px 0px inset`
- Use: Public-site header menu trigger; representative selector `home::[data-omd-capture="7"]`.

### Home control

**Soft capsule control**
- Background: `#efefef`
- Text: `#000000`
- Radius: `999px`
- Padding: `14px 16px`
- Font: `16px / 500 / UberMoveText`
- Hover: `rgba(0,0,0,0.04) 999px 999px 0px 0px inset`
- Pressed: `rgba(0,0,0,0.08) 999px 999px 0px 0px inset`
- Use: Home public-surface control; representative selector `home::[data-omd-capture="9"]`.

### Expanded header menu

**Menu panel**
- Background: `#ffffff`
- Text: `#000000`
- Radius: `8px`
- Padding: `8px 0px`
- Shadow: `rgba(0,0,0,0.16) 0px 4px 16px 0px`
- Font: `16px / 400 / sans-serif`
- Use: Expanded header menu on home, About, and Accessibility; representative selector `surface-2::[data-omd-interaction-capture="menu-0-0"]`.

No authenticated ride-request form, trip card, map control, checkout, or error-state component was present in the supplied public capture, so none is specified here.

## 5. Layout Principles

- The captured desktop public header is 64px tall with `12px 0px` padding and a black background at `home::nav`.
- The capture was taken at 1440×900; it provides no canonical container width, breakpoint, or mobile-grid value.
- The observed pattern is compact horizontal public navigation: 14px labels inside 36–38px capsule controls, with an expanded 8px-radius menu panel.

## 6. Depth & Elevation

| Level | Treatment | Observed use |
|---|---|---|
| Flat | No shadow | Header controls and soft capsule control |
| Menu | `rgba(0,0,0,0.16) 0px 4px 16px 0px` | Expanded header menu on About and Accessibility |

No broader card-elevation scale is claimed from this capture.

## 7. Do's and Don'ts

### Do

- Use the observed ink/paper polarity for the public-header controls.
- Preserve the measured 999px radius and 10px × 12px geometry for the documented header capsules.
- Keep the loaded UberMove display role and UberMoveText UI role separate when those fonts are available from an authorized source.
- Treat Base Web's MIT code license independently from the UberMove font license boundary.
- Use sentence case and direct, comprehensible CTA language when working within the scope of Uber's published advertising guidance.

### Don't

- Don't present a system font or Inter as UberMove or UberMoveText.
- Don't extend the public-web component observations into authenticated rider, earner, courier, merchant, or checkout UI.
- Don't promote the Accessibility-page default-link colors into a general brand or status palette.
- Don't add an unobserved card, input, toast, modal, map, or form-error variant to this reference.
- Don't infer a public redistribution license for UberMove or UberMoveText.

## 8. Responsive Behavior

The supplied evidence records a 1440×900 public-web capture only. It confirms an expandable header menu on all three captured URLs, but it does not provide measured breakpoint, touch-target, stacking, or mobile-layout rules. Those values remain outside this reference.

## 9. Agent Prompt Guide

### Quick Color Reference

- Dark public-header control: `#000000` background with `#ffffff` text, 999px radius, `10px 12px` padding.
- Light public-header menu control: `#ffffff` background with `#000000` text, 999px radius, `10px 12px` padding.
- Home soft capsule control: `#efefef` background with `#000000` text, 999px radius, `14px 16px` padding.
- Expanded menu: `#ffffff` surface, 8px radius, `8px 0px` padding, `rgba(0,0,0,0.16) 0px 4px 16px 0px` shadow.

### Example Component Prompts

- “Create a public-site header action using the observed dark capsule: `#000000` background, `#ffffff` label, 999px radius, 10px 12px padding, UberMoveText 14px/500. On hover use the measured white 0.1 inset overlay.”
- “Create an expanded public navigation menu with a white 8px-radius panel, 8px 0px padding, and the observed `rgba(0,0,0,0.16) 0px 4px 16px` shadow. Do not represent it as an in-app ride menu.”

## 10. Voice & Tone

Uber's current public corporate source describes movement across going, getting, and earning, while its advertising guidance asks for sentence case, friendly language, simple comprehensible promotions, and clear destination-aware CTAs. The latter is advertising guidance, not a universal product-content specification.

| Context | Supported direction | Boundary |
|---|---|---|
| Corporate mission | Movement, access, and marketplace participants | From the 2024 proxy statement |
| Advertising copy | Sentence case, friendly, simple, comprehensible | Applies to Uber advertising guidance |
| Public navigation | Concise Korean labels in the supplied capture | Does not establish app microcopy |

## 11. Brand Narrative

Uber's 2024 proxy statement describes the company's mission as reimagining how the world moves for the better and names a marketplace of earners, riders, eaters, couriers, and merchants. Its 2018 official “A new look” post introduced a new mission and logo, emphasizing easier recognition for riders and a more dependable partnership with drivers. Together, these official sources frame the public site as a movement marketplace with multiple participants, not merely a rider-facing transport page.

The July 2026 public capture carries that framing through a restrained header system: dark and light capsule controls, compact labels, and a menu that opens into a neutral white panel. This is evidence of public marketing/corporate chrome. It is not evidence for a private trip workflow or operational product screen.

## 12. Principles

1. **See every marketplace side.** Uber's corporate source names earners, riders, eaters, couriers, and merchants. *UI implication:* identify the audience and product surface before applying a public-web pattern.
2. **Keep movement legible.** The official mission centers movement, going, getting, and earning. *UI implication:* favor direct labels and clear action outcomes over decorative phrasing.
3. **Use public chrome precisely.** The capture repeats ink/paper capsule controls and a neutral menu panel. *UI implication:* retain measured geometry only on the public-site header patterns documented here.
4. **Respect source domains.** Advertising guidelines, public marketing pages, and authenticated product interfaces are distinct evidence domains. *UI implication:* do not transfer advertising-copy or public-header rules into a product flow without direct evidence.

## 13. Personas

*These are official stakeholder groups, not invented personas; the source does not provide individual biographies or task narratives.*

- **Riders and eaters:** People seeking movement or goods through the marketplace.
- **Earners, couriers, and merchants:** Participants who provide services, deliveries, or commerce through the marketplace.

The supplied public capture does not establish distinct interface needs, journeys, or success criteria for these groups.

## 14. States

| State | Observed treatment | Provenance |
|---|---|---|
| Header menu expanded | White 8px-radius panel with menu items | `home`, `surface-2`, `surface-3` menu interaction capture |
| Dark capsule hover | White 0.1 inset overlay | `home::[data-omd-capture="3"]::state-hover` |
| Dark capsule pressed | White 0.2 inset overlay | `home::[data-omd-capture="3"]::state-pressed` |
| Light capsule focus | `#fbfbfb` or `#fdfdfd` fill | Captured home/About/Accessibility focus states |
| Light capsule pressed | White 0.2 inset overlay | `home::[data-omd-capture="7"]::state-pressed` |
| Soft capsule hover | Black 0.04 inset overlay | `home::[data-omd-capture="9"]::state-hover` |
| Soft capsule pressed | Black 0.08 inset overlay | `home::[data-omd-capture="9"]::state-pressed` |

Loading, validation, payment, booking, and map states were not observed and are intentionally omitted.

## 15. Motion & Easing

The supplied evidence records menu expansion and hover/focus/pressed visual results, but no transition duration, easing curve, animation, or reduced-motion rule. No motion token is claimed.

---

**Verified:** 2026-07-13
**Tier 1 sources:** Raw computed-style and FontFaceSet evidence from `https://www.uber.com/kr/ko/`, `https://www.uber.com/kr/ko/about/`, and `https://www.uber.com/kr/ko/about/accessibility/`; official context/design sources: `https://www.uber.com/us/en/blog/a-new-look/`, `https://tb-static.uber.com/prod/uber-static/uber-sites/_pdf/22789bea-a413-4014-99e4-31144c54ef56.pdf`, `https://www.uber.com/us/en/advertising/specs/guidelines/`, and `https://github.com/uber/baseweb`.
**Tier 2 sources:** `https://getdesign.md/uber`, `https://getdesign.md/design-md/uber/preview`, and `https://styles.refero.design/style/caf8d2ef-4173-4431-9d26-05be0272e9f8`.
**Evidence boundary:** No public rider/earner/courier/merchant product-surface capture or first-party public UberMove/UberMoveText license term was located. Tier 2's unobserved `#9dcdd6` and substitute-font advice are not canonicalized.
**Conflicts unresolved:** none
