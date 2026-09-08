# Resend — brand reference

- Category: productivity
- Site: https://resend.com
- Primary colour: #00a3ff
- Keywords: cinematic, theatrical, premium, crystalline, precise, dark

## 1. Visual Theme & Atmosphere

Resend is a communication platform for developers, built from the React Email open-source project and launched as an email-sending platform in 2023. Its official story frames the work around making human communication easier while helping developers build better products. The current public marketing expression is dark-first and editorial: black canvas, pale text, a crisp `#00a3ff` action, and a deliberately mixed type system that gives headlines more ceremony than controls. The 2025 rebrand introduced a new identity and expanded the original black-and-white palette with gradients and gray tones inspired by physical materials. This reference is intentionally narrower than a product-system claim: its tokens and components come only from three captured public marketing pages, not an authenticated Resend dashboard, API response, email output, or documentation chrome.

- **Dark-first public marketing:** `#000000`, pale text, and cool translucent hairlines organize the captured pages.
- **Editorial hierarchy:** Domaine creates the hero moment, ABC Favorit carries section-level display copy, and Inter is the loaded product/UI family.
- **Precise, not generic:** the blue CTA is a sharp 4px control; the separate outline action is full-pill. Neither shape is generalized into an unobserved global button rule.

## 2. Color Palette & Roles

### Observed marketing roles

- **Primary action** (`#00a3ff`): home primary CTA at `home::[data-omd-capture="56"]`.
- **Canvas** (`#000000`): opened home menu and the dark public-page base.
- **Foreground** (`#f0f0f0`): outline-pill text, selected tab-panel text, and menu text.
- **Muted navigation** (`#a1a4a5`): home header control text.
- **On-primary** (`#ffffff`): home primary CTA text.
- **Cool hairline** (`rgba(214,235,253,0.19)`): outline-pill and opened-menu border; selected tab-panel fill.

The collector also records local translucent whites and blues. They stay component-scoped; no success, warning, error, dashboard, or email-client semantic palette is inferred.

## 3. Typography Rules

### Evidence classes

- **Official product-use plus live computed use — Inter:** Resend identifies Inter by Rasmus Andersson as its product typeface. It is loaded with high confidence, has 599 visible captured uses, and is corroborated by a Resend-hosted WOFF2 source. It is the UI-family token.
- **Official display-use plus live computed use — ABC Favorit:** Resend names ABC Favorit by Dinamo as display type. It is loaded with high confidence, has 40 visible heading uses, and is corroborated by loaded Book and Medium Resend-hosted WOFF2 sources. Dinamo’s commercial EULA is a licence boundary, not permission to reuse Resend’s served files.
- **Official editorial-use plus live computed use — Domaine Display:** Resend names Domaine Display by Klim Type Foundry as editorial type. It is loaded with high confidence, has four visible heading uses, and is corroborated by three Resend-hosted WOFF2 sources. The 2025 rebrand explains its headline role.
- **System-computed, not a token — Helvetica:** the `#00a3ff` CTA computes to Helvetica but the collector records no matching loaded face/source. The component records that measured value without promoting Helvetica to Resend UI typography.
- **Declared-only, not a token — Commit Mono:** two `@font-face` sources exist, but zero visible captured uses were recorded. It is neither an active UI family nor a code-panel token.
- **System-computed embedded field:** `-apple-system` appears twice in an embedded editable field; it is not a Resend typography token.

### Observed hierarchy

| Role | Family | Size | Weight | Line height | Tracking | Surface boundary |
|---|---|---:|---:|---:|---:|---|
| Marketing hero | Domaine Display | 96px | 400 | 1.00 | -0.96px | public marketing only |
| Marketing hero, compact | Domaine Display | 76.8px | 400 | 1.00 | -0.768px | public marketing only |
| Section heading | ABC Favorit | 56px | 400 | 1.20 | -2.8px | public marketing only |
| Supporting heading | ABC Favorit | 20px | 400 | 1.00 | normal | public marketing only |
| Body/list | Inter | 16px | 400 | 1.50 | normal | public marketing only |
| Control | Inter | 14px | 400–500 | 1.43 | normal | public marketing only |

## 4. Component Stylings

### Buttons and controls

**Primary CTA — home marketing**
- Background: `#00a3ff`
- Text: `#ffffff`
- Radius: 4px
- Padding: 12px 20px
- Font: 14px / 600 / Helvetica (system-computed)
- States: default capture only
- Use: Home marketing primary CTA at `home::[data-omd-capture="56"]`.

**Outline pill — home marketing**
- Text: `#f0f0f0`
- Border: 1px solid `rgba(214,235,253,0.19)`
- Radius: 9999px
- Padding: 5px 12px
- Font: 14px / 400 / Inter
- States: default capture only
- Use: Home outline control at `home::[data-omd-capture="41"]`.

**Header navigation control — home marketing**
- Text: `#a1a4a5`
- Padding: 4px 12px
- Font: 14px / 500 / Inter
- States: default capture only
- Use: Header control at `home::[data-omd-capture="1"]`.

**Disabled automation action — feature marketing**
- Text: `#4a4a4a`
- Radius: 12px
- Padding: 12px
- Font: 14px / 400 / Inter
- States: disabled capture
- Use: Automations page action at `surface-3::[data-omd-capture="24"]`.

### Tabs and menus

**Selected tab panel — home marketing**
- Background: `rgba(214,235,253,0.19)`
- Text: `#f0f0f0`
- Radius: 16px
- States: selected tab capture
- Use: Home selected panel at `home::[data-omd-capture="65"]`.

**Selected menu option — audiences feature marketing**
- Background: `rgba(221,234,248,0.008)`
- Text: `rgba(233,240,253,0.525)`
- Radius: 12px
- Padding: 8px 24px
- Font: 14px / 400 / Inter
- States: menu-open selected capture
- Use: Audiences option at `surface-2::[data-omd-interaction-capture="menu-0-2"]`.

**Opened menu listbox — home marketing**
- Background: `#000000`
- Text: `#f0f0f0`
- Border: 1px solid `rgba(214,235,253,0.19)`
- Radius: 8px
- Padding: 8px
- Use: Home opened listbox at `home::[data-omd-interaction-capture="menu-0-0"]`.

Only the selector-level public marketing variants above are canonical. No generic card, input, code panel, hover, focus, toast, dialog, or authenticated-product state is asserted without supplied observation.

## 5. Layout Principles

### Spacing system

Observed public-page spacing clusters around 4px, 8px, 12px, 16px, 24px, and 32px. The component evidence specifically confirms 12px/20px primary-action padding, 5px/12px outline-pill padding, and 8px/24px menu-option padding; larger layout spacing is not promoted into a page-grid contract.

### Shape system

The observed control range is intentionally mixed: sharp 4px primary CTA, 8px opened-menu container, 12px feature options/actions, 16px selected panel, and a full-pill outline action. Apply a shape only where its component provenance matches.

## 6. Depth & Elevation

The captured public pages use cool translucent lines and restrained translucent fills to separate controls from the black canvas. The supplied evidence does not justify a general card-shadow or global elevation scale; the opened menu and outlined pill retain their observed hairline only.

## 7. Do's and Don'ts

### Do

- Use the verified dark canvas and pale foreground for public Resend-style marketing surfaces.
- Keep the sharp blue primary CTA distinct from the separate full-pill outline control.
- Use Inter for UI work, ABC Favorit for loaded display headings, and Domaine Display for loaded editorial heroes when the relevant font is actually available.
- Keep selected/menu-open/disabled styling scoped to the observed component and public surface.

### Don't

- Do not substitute a system font and present it as ABC Favorit, Domaine Display, or Commit Mono.
- Do not treat declared-only Commit Mono as a verified code-panel font.
- Do not extend public marketing components into dashboard, API, generated-email, or documentation rules.
- Do not invent hover, focus, form-error, toast, dialog, or loading variants from the supplied captures.

## 8. Responsive Behavior

The collector snapshot is 1440×900. It verifies the compact 76.8px Domaine hero and full 96px hero values on public marketing, but does not supply breakpoint selectors, mobile navigation behavior, or responsive component-state evidence. Preserve the observed type values only; leave unobserved mobile behavior unresolved.

## 9. Agent Prompt Guide

### Quick reference

- Canvas: `#000000`
- Foreground: `#f0f0f0`
- Primary action: `#00a3ff` / `#ffffff` / 4px / 12px 20px
- Hairline: `rgba(214,235,253,0.19)`
- Outline control: full pill / 5px 12px / Inter 14px

### Example component prompts

- “Create a public marketing primary CTA with `#00a3ff` background, white text, 4px radius, and 12px 20px padding. Do not turn it into a pill.”
- “Create a home-style outline control with pale text, a cool translucent hairline, full-pill radius, 5px 12px padding, and loaded Inter if available.”
- “Create an audiences-style selected menu option only for an open-menu state: 12px radius, 8px 24px padding, and the observed translucent pale treatment.”

## 10. Voice & Tone

Resend’s official mission is to make human communication easier and help developers build better products. Its public language is concise, direct, and builder-oriented rather than inflated.

| Context | Direction |
|---|---|
| Product framing | Explain a concrete communication or developer outcome. |
| CTA | Use a short, clear action. |
| Technical support | Prefer specific next steps over abstract reassurance. |

**Voice samples**

- “Email for developers” <!-- verified: Resend public home, supplied 2026-07-13 collector -->
- “To help humans communicate” <!-- verified: resend.com/handbook/company/why-we-exist, 2026-07-13 -->
- “To help developers build better products” <!-- verified: resend.com/handbook/company/why-we-exist, 2026-07-13 -->

## 11. Brand Narrative

Resend began with an open-source project in 2022, when its founders were frustrated by the difficulty of making modern email templates work across email clients. The company launched an email-sending platform in 2023 and joined Y Combinator’s winter batch. Its official timeline identifies React Email, incorporation in January 2023, and later developer tooling as connected milestones rather than separate brands.

The public identity evolved in July 2025 with a new logo, visual identity, wallpapers, expanded color treatment, and a typography system that gives Domaine editorial headline work, ABC Favorit subheading/display work, and Inter product/body work. That narrative supports the marketing atmosphere here; it does not expand the scope of the captured component tokens.

## 12. Principles

1. **Reduce friction.** Resend’s official brand guidelines call the brand simple. *UI implication:* use clear action labels and avoid decorative control complexity.
2. **Be modern without noise.** The official guidelines describe a look ahead of the present. *UI implication:* use the dark-first palette and measured translucent details rather than generic neon decoration.
3. **Earn trust through memorability.** The official guidelines pair memorability with trust. *UI implication:* keep the blue action and cool hairline language consistent where their source component applies.
4. **Make for builders.** Resend’s mission centers developers and makers. *UI implication:* keep instructions and technical decisions concrete and actionable.

## 13. Personas

*These are stakeholder archetypes derived from Resend’s official mission and public product framing, not individual people or user research findings.*

**Developer building a product.** Needs a direct way to send reliable communications and values precise, implementation-ready guidance.

**Product maker coordinating communication.** Needs emails and messaging to feel coherent with the product, without becoming an email-infrastructure specialist.

**Technical team evaluating a platform.** Needs transparent constraints, clear next actions, and source-backed claims rather than ornamental marketing language.

## 14. States

Only the supplied public-page states are asserted.

| State | Treatment | Scope |
|---|---|---|
| Default primary CTA | Blue `#00a3ff`, white text, 4px radius | Home marketing |
| Default outline control | Pale text, cool hairline, full pill | Home marketing |
| Selected tab panel | Cool translucent fill, pale text, 16px radius | Home marketing |
| Menu open | Black listbox with cool hairline | Home marketing |
| Selected menu option | Translucent pale treatment, 12px radius | Audiences marketing |
| Disabled action | `#4a4a4a` text, 12px radius, 12px padding | Automations marketing |

Empty, loading, error, success, skeleton, form validation, toast, dialog, and authenticated-product states were not supplied and are intentionally omitted.

## 15. Motion & Easing

The supplied collector preserves selected, menu-open, and disabled outcomes but not a reliable duration/easing contract. Do not infer a global motion scale from CSS class names or adjacent legacy material. Use reduced motion by default when implementing a new surface, and keep any animation specification outside this reference until a source-backed observation is available.

---

**Verified:** 2026-07-13
**Tier 1 sources:** supplied collector for https://resend.com/, https://resend.com/features/audiences, and https://resend.com/features/automations; official Resend brand guidelines, rebrand article, About, history, and mission pages.
**Tier 2 sources:** getdesign Resend analysis opened; Refero query attempted but unavailable through built-in web open.
**Conflicts unresolved:** none
