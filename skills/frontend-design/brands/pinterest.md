# Pinterest — brand reference

- Category: consumer-tech
- Site: https://www.pinterest.com
- Primary colour: #e60023
- Keywords: warm, cozy, craft-like, personal, inviting, handcrafted, red

## 1. Visual Theme & Atmosphere

Pinterest is a visual discovery platform for searching, saving, and shopping ideas. Its public Gestalt design-system introduction frames the product as helping people create a life they love; the public business explanation describes Pins as the image, video, and link-bearing units that connect browsing to action. The supplied consumer capture expresses that role with an image-led, low-chrome surface: a warm plum ink, quiet olive-gray supporting text, a single red action, and compact rounded account controls. Official brand guidance similarly fixes the recognizable mark as a white script P in a red circle.

The observed public ecosystem is deliberately not one uniform UI. The signed-out consumer/auth route uses loaded `Pin Sans`, warm `#211922` ink, `#E60023` consumer action, and `#E5E5E0` secondary surface. Pinterest Business is a separate marketing surface with loaded `PinterestSansPro`, `#111111` actions, larger 30px pills, and a selected-tab treatment. These route-local facts are documented side by side without treating business lead-generation chrome as consumer-product UI.

## 2. Layout & Grid

- The supplied consumer capture is a public signed-out/product-auth route at 1440×900; it contains a 48px search control and 48px account actions.
- Pinterest Business is a public marketing route at 1440×900; its 60px action examples and 47px tabs are retained only for that surface.
- Pinterest’s official business guidance describes Pins as visual content and recommends vertical 2:3 advertising canvases, but the supplied capture does not establish a reusable consumer masonry-column count, card ratio, breakpoint, or responsive grid contract.

## 3. Color & Typography

### Color tokens

- `#E60023` — consumer primary action background on `home` and `surface-2`.
- `#211922` — consumer ink on the signed-out/product-auth surfaces.
- `#62625B` — consumer muted-text sample.
- `#E5E5E0` — consumer secondary-action background.
- `#91918C` — consumer auth-input border.
- `#FFFFFF` — observed consumer canvas/action text and business outline surface.
- `#111111` — Pinterest Business marketing ink and filled-action background; it is not promoted as a consumer-product substitute.

### Typography evidence classes

- **Live consumer computed use:** `Pin Sans` is loaded/high confidence with 118 visible uses across body, heading, input, button, text, and toggle roles. The collector matches computed family use to eight Pinterest-hosted `s.pinimg.com/font/Pin-Sans-*` files. It is the consumer UI family token.
- **Live business-marketing computed use:** `PinterestSansPro` is loaded/high confidence with 199 visible uses across body, buttons, headings, list items, tabs, and text. The collector matches it to four Pinterest-hosted `Pinterest-Sans-Pro-*` files. It remains a business-marketing family token rather than a consumer-family replacement.
- **Declared-only assets:** `HaasGrotDisp`, `HaasGrotText`, `PinterestSans`, `PinterestSansLC`, `PinterestSansTPJ`, and `PinterestUI` have captured `@font-face` sources but zero visible use. They remain declared-only; no specimen or UI token is inferred.
- **Unresolved embedded face:** one Google Sign-In control reports `Google Sans` with no matching loaded FontFace or Pinterest source. It is third-party chrome and is not a Pinterest token.
- **Licence boundary:** the official search found Pinterest-hosted font files but no first-party public licence granting downstream reuse of Pin Sans or PinterestSansPro. The family names and observed metadata are recorded; the files are not reusable project assets.

## 4. Components

### Consumer primary action

**Default**
- Background: `#E60023`
- Text: `#000000`
- Border: `2px solid transparent`
- Radius: `16px`
- Padding: `6px 14px`
- Font: `12px / 400 Pin Sans`
- Use: Consumer header action; `home::[data-omd-capture="7"]`, also repeated on `surface-2`.

### Consumer secondary action

**Default**
- Background: `#E5E5E0`
- Text: `#000000`
- Border: `2px solid transparent`
- Radius: `16px`
- Padding: `6px 14px`
- Font: `12px / 400 Pin Sans`
- Use: Consumer header secondary action; `home::[data-omd-capture="8"]`, also repeated on `surface-2`.

### Consumer auth input

**Default**
- Text: `#000000`
- Border: `1px solid #91918C`
- Radius: `16px`
- Padding: `11px 15px`
- Font: `16px / 400 Pin Sans`
- Use: Consumer auth input; `home::[data-omd-capture="19"]`, also repeated on `surface-2`.

### Business marketing action

**Default**
- Background: `#111111`
- Text: `#FFFFFF`
- Radius: `30px`
- Padding: `0px 20px`
- Font: `16px / 400 PinterestSansPro`
- Use: Pinterest Business marketing action; `surface-3::[data-omd-capture="9"]`.

### Business marketing outline action

**Default**
- Background: `#FFFFFF`
- Text: `#111111`
- Radius: `30px`
- Padding: `0px 20px`
- Font: `16px / 400 PinterestSansPro`
- Use: Pinterest Business marketing outline action; `surface-3::[data-omd-capture="10"]`.

### Business marketing tab

**Default**
- Text: `#111111`
- Radius: `12px`
- Padding: `14px 16px`
- Font: `15px / 700 PinterestSansPro`
- Selected: White text observed at `surface-3::[data-omd-capture="15"]`; tab selection is recorded by two `interactions[]` entries.
- Use: Pinterest Business marketing tab; unselected representative `surface-3::[data-omd-capture="16"]`.

No hover, focus, pressed, disabled, menu, dialog, validation, or responsive component variant is claimed beyond that tab-selection interaction provenance.

---

**Verified:** 2026-07-13
**Tier 1 sources:** `https://kr.pinterest.com/` (consumer product/auth capture), `https://business.pinterest.com/ko/` (business marketing capture), `https://gestalt.pinterest.systems/` (official product design-system context), `https://business.pinterest.com/en-in/brand-guidelines/` (official brand guidance), and Pinterest-hosted loaded font sources under `https://s.pinimg.com/font/`
**Tier 2 sources:** `https://getdesign.md/pinterest` (one broad editorial record; no source-backed token/component data used); `https://styles.refero.design/?q=Pinterest` (attempted through built-in web open; safe-open error, no usable record)
**Conflicts unresolved:** none

Legacy global component, font, responsive, motion, and product-state claims were removed because the supplied 2026-07-13 capture establishes only the selector- and surface-scoped observations above.

## 5. Elevation

The retained consumer controls have `box-shadow: none`. Business marketing action samples include a 2px `#111111` outline-like shadow, but the one route-local observation does not establish a shared elevation scale.

## 6. Spacing & Shape

Observed spacing values retained as a conservative source-bound set are `4 / 8 / 12 / 20px`. Consumer account and form controls use 16px radii; the business marketing tab is 12px and its large actions are 30px. These are observations of separate domains, not a universal radius prescription.

## 7. Iconography & Imagery

Pinterest’s official business guidance describes Pins as visual content that can include images or videos and recommends that creative put visuals first. The supplied captures do not establish a reusable icon set, stroke width, media-card ratio, or image treatment, so those details are omitted.

### Do

- Keep the Pinterest badge in its red circle and pair external uses with an account context or call to action, as the official brand guidelines require.
- Keep consumer and business-marketing component examples source-scoped.
- Let visual content carry the discovery story rather than inventing unobserved decorative chrome.

### Don't

- Do not recolor, outline, or add effects to the Pinterest badge.
- Do not represent a Pinterest Business action as an observed consumer-product CTA.
- Do not substitute a system or third-party font for a named Pinterest family.

## 8. Responsive Behavior

The raw bundle is desktop-only (1440×900). Pinterest’s official business creative guidance recommends a vertical 2:3 canvas for mobile advertising, but it does not prove a consumer-product breakpoint or responsive component behavior. No breakpoint table is inferred.

## 9. Agent Prompt Guide

Use the requested source domain explicitly. For a consumer signed-out/auth example, use `Pin Sans`, plum `#211922` ink, `#E60023` primary action, and `#E5E5E0` secondary action with the selector-backed 16px control radius. For a Pinterest Business marketing example, use `PinterestSansPro`, charcoal `#111111`, and the observed 30px action radius. Do not blend the two into a claimed single system.

## 10. Voice & Tone

Pinterest’s first-party product and business materials consistently frame the service around inspiration becoming action: people search, save, shop, and create a life they love. The approved external-language examples in the brand guidelines include “Follow on Pinterest”, “Find more ideas on Pinterest”, and “Get inspired on Pinterest”; they prohibit “Trending on Pinterest”, “Trending Pins”, and using “Pin” as a verb. This is official communications guidance, not a reconstructed in-product copy library.

## 11. Brand Narrative

Pinterest describes itself as a visual discovery platform where people search, save, and shop ideas. Its public Gestalt introduction connects the product system to experiences that inspire people to create a life they love, while its business explanation connects visual browsing to saving, clicking, and buying. Current official campaign coverage continues that position: inspiration is presented as a step toward life away from the screen rather than an end in itself.

The red-badge mark provides the most explicit first-party brand expression in the sources checked here. Public brand guidance defines it as a white script P in a red circle and limits black or white versions to constrained-colour contexts. This narrative evidence explains the documented mark and product purpose; it does not authorize additional UI tokens.

## 12. Principles

1. **Visual discovery should lead to action.** *UI implication:* retain image/video-first content context where it is actually supplied; do not replace it with generic dashboard chrome.
2. **Inspiration is the product promise.** *UI implication:* make consumer action language practical and supportive, using the first-party guidance rather than invented hype.
3. **The badge retains its circle and colour treatment.** *UI implication:* do not recolour, outline, or add effects to the Pinterest mark.
4. **Source domains stay distinct.** *UI implication:* business-marketing typography and 30px actions are not defaults for consumer product/auth controls.

## 13. Personas

Official sources checked for this update describe broad audiences such as people discovering ideas, shoppers, creators, and businesses, but do not provide verified user-persona definitions suitable for this reference. `[FILL IN: user research or customer-segment source supplied by Pinterest]`

## 14. States

The supplied evidence records two business-marketing tab interactions and selected/unselected text treatments. It does not establish consumer loading, empty, error, success, toast, disabled, modal, or validation state contracts. Those unobserved states are intentionally omitted rather than synthesized.

## 15. Motion & Easing

No motion duration, easing curve, transition, or reduced-motion rule is supported by the supplied raw capture or the official sources checked for this update. No motion token is claimed.
