# Naver — brand reference

- Category: consumer-tech
- Site: https://www.naver.com
- Primary colour: #03c75a
- Keywords: pragmatic, neutral, factual, clinical, utility

## 1. Visual Theme & Atmosphere

NAVER is a Korean search and discovery platform whose public identity spans the portal home, search results, and a much broader family of local services. Its familiar green is the stable connective tissue, while each product surface optimizes independently for dense information retrieval, quick navigation, and local task completion. NAVER does not expose one public product design system that governs every service, so this reference deliberately separates three inspected domains: the portal home, search results, and the NAVER Corp brand-resource page. That separation preserves the recognizable company identity without turning a corporate font, a search-only pattern, or a portal measurement into a universal product rule.

The official identity constant is NAVER Green (`#03C75A`). Product chrome is otherwise neutral and information-dense. The portal/search surfaces use a System-first Korean stack, while the corporate brand page loads and visibly uses `InterVariable`. Values from one surface must not be silently generalized to the others.

**Key characteristics:**
- Official brand green `#03C75A`, backed by the NAVER brand guide
- Dense portal/search composition on white with dark gray text
- System-first portal/search typography
- `InterVariable` on the corporate brand page
- Search, filters, tabs, cards, menus, and paging controls grounded in live computed evidence

## 2. Color Palette & Roles

### Official identity
- **NAVER Green** (`#03C75A`): official logo and identity color. The guide specifies RGB 3/199/90, CMYK 72/0/88/0, and Pantone 2270C.

### Portal and search
- **Canvas** (`#FFFFFF`): portal and result surfaces.
- **Portal Ink** (`#2E2E2E`): common portal control and chrome text.
- **Search Ink** (`#1C1C1C`): primary search-result text.
- **Search Link** (`#0C43B7`): current search-result link/chip blue.
- **Search Muted** (`#8C8C8C`): inactive tabs and secondary labels.
- **Hairline** (`#E5E5E5`): filter-chip and light container border.

### Corporate brand page
- **Corporate Ink** (`#1A1D24`): brand-page navigation and section labels.
- **Corporate Muted** (`#717680`): secondary corporate copy.

Do not promote older `#0068C3`, `#6633B9`, or estimated semantic colors as current universal NAVER tokens without surface-specific live evidence.

## 3. Typography Rules

### Font resolution

| Evidence class | Resolution |
|---|---|
| Official product-use | No single official family is published for every NAVER product surface. |
| Live surface-use | Portal/search use a System-first stack; the corporate brand page visibly uses loaded `InterVariable`. |
| Official distributed asset | NAVER distributes Nanum and D2Coding, but distribution alone is not UI usage. |
| Declared-only | NanumSquare, NanumSquareNeo, NanumHuman, and Pretendard were declared without visible use. |
| Unresolved | A minority `나눔고딕` usage had no matching loaded FontFace. |

Specimen availability is evaluated per surface and never substitutes one NAVER-published font for another.
- **Portal and search:** `System`. Computed stacks begin with `-apple-system` and continue through Korean platform fallbacks.
- **Corporate brand page:** `InterVariable`, loaded from `https://www.navercorp.com/font/InterVariable.woff2` and visibly used.
- **Declared only in this capture:** NanumSquare, NanumSquareNeo, NanumHuman, and Pretendard. Declaration is not visible use.
- **Unresolved minority:** `나눔고딕` appeared on four search elements without a matching loaded FontFace.
- **No canonical UI monospace:** D2Coding is a NAVER-published font, not evidence that current portal/search UI uses it.

| Role | Surface | Font | Size | Weight | Line height | Tracking |
|---|---|---|---:|---:|---:|---:|
| Portal Search | Portal | System | 21px | 700 | 24px | -0.4px |
| Portal UI | Portal | System | 14.7px | 500 | 17.85px | -0.4px |
| Search Tab | Search | System | 16px | 600 | 21px | -0.3px |
| Search Title | Search | System | 18px | 600 | 24px | -0.16px |
| Corporate Tab | Brand resource | InterVariable | 20px | 600 | 28px | -0.6px |

## 4. Component Stylings

### Portal Search

**Search Input**
- Background: transparent
- Text: `#000000`
- Radius: 0px
- Padding: 17px 0
- Height: 58px
- Font: 21px / 700 / System
- States: focus and autocomplete listbox expansion observed
- Use: Query field inside the portal's branded search assembly

**Search Submit**
- Background: transparent
- Text: `#2E2E2E`
- Radius: 0px
- Padding: 9px 9px 9px 10px
- Height: 58px
- States: default observed; hover and pressed not retained
- Use: AI/search submission control adjacent to the query field

### Search Results

**Vertical Tab**
- Background: transparent
- Text: `#8C8C8C`
- Radius: 0px
- Padding: 6px 12px 14px
- Font: 16px / 600 / System
- Hover: `#595959`
- Pressed: `#595959`
- Use: Search vertical/category navigation

**Filter Chip**
- Background: `#FFFFFF`
- Text: `#0C43B7`
- Border: 1px solid `#E5E5E5`
- Radius: 18px
- Padding: 4px 12px 4px 4px
- Font: 13px / 400 / System
- Use: Image and result refinement filter

**Result Card**
- Background: `#FFFFFF`
- Text: `#1C1C1C`
- Radius: 12px
- Use: Grouped search-result content surface

### Portal Utilities

**Paging Button**
- Background: `#FFFFFF`
- Text: `#2E2E2E`
- Border: 1px solid rgba(0,0,0,0.15)
- Radius: 9999px
- Height: 36px
- Shadow: 0 1px 2px rgba(0,0,0,0.06)
- States: default observed; hover and pressed not retained
- Use: Carousel previous/next action

**Overflow Menu**
- Background: transparent
- Text: `#2E2E2E`
- Font: 14.7px / 500 / System
- States: expanded listbox and option observed
- Use: Content-header overflow navigation

### Corporate Brand Resource

**Section Tab**
- Background: transparent
- Text: `#1A1D24`
- Radius: 0px
- Padding: 17px 0 18px
- Font: 20px / 600 / InterVariable
- States: selected observed
- Use: Brand guide versus official-photo section switching

## 5. Layout Principles

- Use the observed 4/8/12/16/20px spacing clusters for compact UI composition.
- Portal and SERP density are surface properties, not permission to remove hierarchy.
- Search remains the primary spatial anchor on the portal.
- Cards may use 12px rounding on search surfaces; utility controls range from square to fully circular.
- Corporate pages use more generous rhythm and must not inherit portal density automatically.

## 6. Depth & Elevation

- Most sampled portal/search controls use no shadow.
- The portal paging button uses a restrained `0 1px 2px rgba(0,0,0,0.06)` shadow.
- Prefer border, spacing, and type hierarchy before introducing elevation.
- No universal NAVER shadow scale is claimed.

## 7. Do's and Don'ts

### Do
- Use official NAVER Green exactly as `#03C75A` for identity applications.
- Keep portal/search System typography separate from corporate InterVariable typography.
- Preserve compact Korean text rhythm and explicit interactive states.
- Treat live product evidence and the official brand guide as different authorities.

### Don't
- Do not infer native-app typography from these web surfaces.
- Do not promote declared-only Nanum/Pretendard faces as current UI fonts.
- Do not invent a public NAVER product design system from brand-resource guidance.
- Do not alter the official logo's proportions, color, or style.

## 8. Responsive Behavior

- The inspected evidence is desktop at 1440×900; mobile-native claims are intentionally absent.
- Preserve 44px-or-larger touch targets when adapting dense portal utilities to narrow layouts.
- Allow search-result cards to stack before shrinking readable Korean type.
- Keep the search control visually dominant and avoid horizontal overflow in tab/filter rows.

## 9. Agent Prompt Guide

> Build a NAVER-inspired information surface using a white canvas, System-first Korean typography, compact 4/8/12/16/20px spacing, dark neutral text, and `#03C75A` only where identity or a verified action requires it. Separate portal/search components from NAVER Corp brand-page components. Use 12px result cards, 18px filter chips, and explicit hover/pressed/selected states. Do not claim Nanum, Pretendard, or InterVariable outside the surfaces where they were actually observed.

## 10. Voice & Tone

The inspected public copy is direct, functional, and navigation-led. Labels such as “검색하기”, “삭제”, “전체 서비스”, and “브랜드 리소스” describe the action or destination without promotional filler.

| Do | Don't |
|---|---|
| Use short Korean action labels | Add decorative slogans to utility controls |
| Explain the next recoverable action | Blame the user |
| Name destinations consistently | Rename familiar portal concepts for novelty |

Verified live samples:
- “검색하기” — corporate-site integrated search. <!-- verified: https://www.navercorp.com/company/brandGuide -->
- “브랜드 리소스” — official brand-resource section. <!-- verified: https://www.navercorp.com/company/brandGuide -->
- “기술과 서비스로 세상의 모든 가능성을 연결합니다” — corporate navigation statement. <!-- verified: https://www.navercorp.com/company/about -->

## 11. Brand Narrative

NAVER's official company page describes its beginning in 1999 and frames the organization as “Navigators” connecting possibilities through technology and services. The same official timeline presents integrated search in 2000, HyperCLOVA in 2021, and the 1784 robot-friendly headquarters in 2022.

The official brand guide describes NAVER Green as carrying trust, challenge, exploration, familiarity, and an eco-friendly image. This reference uses those statements only as sourced brand context; it does not infer unpublished product principles.

In product terms, this produces a useful tension: NAVER must remain instantly recognizable while supporting services with very different densities and jobs. Search favors fast scanning and compact labels; the portal coordinates many destinations; the corporate brand surface explains the shared identity. Green, logo rules, and company statements stay at brand level, while typography, spacing, and components remain attached to the surface where they were observed.

## 12. Principles

1. **Keep identity consistent.** The official logo and `#03C75A` must not be arbitrarily recolored, distorted, outlined, or given effects.
   - *UI implication:* isolate brand identity tokens from transient service colors.
2. **Connect people to destinations quickly.** Public navigation and search copy is literal and compact.
   - *UI implication:* prioritize recognizable labels and scanning speed.
3. **Separate service surfaces.** NAVER operates many products with different local systems.
   - *UI implication:* never treat one service's typography or component geometry as a universal NAVER token.

## 13. Personas

NAVER has not published validated product personas for the inspected portal, search, and brand-resource surfaces. The evidence supports usage contexts instead: people entering a query and comparing results; portal visitors scanning news, shopping, maps, mail, and other destinations; and designers or partners retrieving official identity assets. These are task contexts, not demographic profiles. Implementations should validate the specific NAVER service, language, device, and task before turning them into research personas.

## 14. States

- **Hover / pressed:** captured on search tabs and utility actions.
- **Selected:** captured on portal/search/corporate tabs.
- **Expanded:** captured for the portal listbox/menu.
- **Checked / unchecked:** captured for portal display controls and a search switch.
- **Empty, loading, error, success, disabled:** [FILL IN — no safe representative live evidence captured in this run.]

Do not fill absent states with generic NAVER-looking values.

## 15. Motion & Easing

The collector captured state changes but did not establish a canonical duration or easing scale. Use motion only to clarify menu expansion, tab selection, and focus transitions; respect reduced-motion preferences.

[FILL IN — official product motion tokens were not found in the inspected public sources.]

---

**Verified:** 2026-07-11 (omd:migrate)
**Tier 1 sources:** https://www.naver.com/ · https://search.naver.com/search.naver?query=%EB%94%94%EC%9E%90%EC%9D%B8 · https://www.navercorp.com/company/brandGuide · https://www.navercorp.com/company/about
**Tier 2 sources:** https://getdesign.md/naver (no importable record in available path) · https://styles.refero.design/?q=naver (no importable result in available path)
**Tier 2 status:** unavailable; no Tier 2 value promoted
**Conflicts unresolved:** none
**Migration depth:** Apple-tier evidence graph; visual smoke pending
