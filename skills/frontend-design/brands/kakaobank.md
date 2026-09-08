# KakaoBank — brand reference

- Category: fintech
- Site: https://www.kakaobank.com
- Primary colour: #ffe300
- Keywords: friendly, plain-spoken, warm, formal-when-needed, personal, trustworthy

## 1. Visual Theme & Atmosphere

KakaoBank is an internet bank that presents financial services as part of ordinary mobile life. Its public story spans deposits, savings, loans, investment, foreign exchange, cards, business banking, youth products, and newer AI-led service exploration. The corporate site expresses that breadth with unusually little banking ornament: a white canvas, black type, pale-gray sections, very large Korean headings, and only selective use of the protected KakaoBank Yellow. The result feels digitally accessible while retaining the clarity and restraint expected from a regulated financial institution.

That restraint is the distinctive system. The official brand resource defines KakaoBank Yellow as `#FFE300`, alongside black, white, and a narrow neutral palette, and warns against nearby substitute yellows. Yet the current public corporate surface is mostly monochrome. Yellow identifies KakaoBank; it is not proof that every banking CTA, card, success state, or app control uses yellow. The current product page organizes the catalog through large editorial sections and simple service tabs, while the brand-resource page explains the symbol, wordmark, and color system as protected identity assets.

Pretendard Variable carries all visible public UI and editorial roles. It loaded from KakaoBank's own domain and appeared across 645 elements, from the 90px home hero to 14px navigation and 16px body text. Brand character comes from scale, disciplined yellow, and product imagery rather than a proprietary display face.

**Key Characteristics:**
- Official single-yellow identity: `#FFE300`, not neighboring Kakao-family yellows
- White and pale-gray corporate canvas with black-first typography
- Pretendard Variable loaded across every observed text role
- 90px/800 home hero and 42px/700 service-page title
- Flat public components with 0px navigation/tab geometry and 6px black actions
- Explicit separation between public corporate evidence and native banking-product UI

## 2. Color Palette & Roles

- **KakaoBank Yellow** (`#ffe300`): official primary identity color and protected brand specification.
- **Canvas** (`#ffffff`) and **foreground** (`#000000`): dominant current public pairing.
- **Secondary** (`#888888`): repeated supporting navigation and footer text.
- **Body gray** (`#444444`): service and brand-resource explanatory text.
- **Section surface** (`#f7f7f7`): current quiet public section fill.
- **Divider** (`#e6e6e6`): public service and brand-spec boundaries.

Earlier `#E02000`, `#0FBE6C`, `#FF9800`, input, placeholder, and native state roles are not retained. The live public site exposed `#007AFF` on small web controls, but its semantic product role was not clear enough to promote.

## 3. Typography Rules

### Font evidence boundary

| Evidence class | Resolution |
|---|---|
| Official product-use | KakaoBank's current public home, service, and brand-resource pages establish Pretendard Variable. |
| Live surface-use | Pretendard Variable loaded/high across 645 visible elements from KakaoBank's first-party WOFF2. |
| Official distributed asset | The public webfont is first-party delivered; the KakaoBank wordmark remains a protected identity asset rather than a font. |
| Declared-only | `swiper-icons` was declared with zero visible text use. |
| Unresolved | Native iOS/Android banking flows and device-specific typography remain unresolved. |

| Role | Size | Weight | Line height | Tracking |
|---|---:|---:|---:|---:|
| Corporate hero | 90px | 800 | 117px | -0.9px |
| Service display | 42px | 700 | 52.08px | -0.84px |
| Section heading | 32px | 700 | 43.52px | -0.64px |
| Card heading | 24px | 700 | 34.56px | -0.48px |
| Body/list | 16px | 400 | 24px | normal |
| Top navigation | 14px | 600 | 21px | -0.14px |

## 4. Component Stylings

### Current public components

#### Top navigation
- Transparent / black, 0px radius, 62px height, `0 20px`
- Pretendard Variable 14px/600; focus, hover, and pressed captured

#### Service-category tab
- Transparent / black, 1px `#e6e6e6` bottom rule
- 0px radius, 62px height, `16px 0`, 16px/400

#### Corporate black action
- Black / white, 6px radius, 42px height, `9.5px 18px`
- Pretendard Variable 15px/600

#### Brand-resource download
- Black / white, 6px radius, 43px height, `10px 16px 10px 20px`
- Pretendard Variable 16px/400

#### Brand specification row
- Transparent / black, 1px `#e6e6e6` top rule
- `10px 0`, 16px/400/24px

Yellow native CTAs, account cards, transfer inputs, status badges, notifications, bottom sheets, and app tabs are intentionally absent until a current inspectable native-product path establishes them.

## 5. Layout Principles

- Use large type and generous vertical rhythm to explain complex financial categories.
- Keep public navigation and tabs geometrically flat; hierarchy comes from placement and weight.
- Use `#f7f7f7` for section-level grouping instead of elevated cards.
- Apply KakaoBank Yellow at verified identity moments, not as ambient chrome.
- Keep official brand-resource layouts separate from banking-task layouts.

## 6. Depth & Elevation

The promoted public system is shadow-free. Separation comes from white/pale-gray surfaces, `#e6e6e6` rules, and typographic scale. No sheet, card, or focus shadow is promoted for native banking UI.

## 7. Do's and Don'ts

### Do
- Use exact `#FFE300` when the role is an official KakaoBank identity role.
- Preserve Pretendard Variable's observed Korean-first metrics and tracking.
- Keep the corporate web surface restrained and largely monochrome.
- Distinguish official identity, public service marketing, and native banking evidence.

### Don't
- Do not substitute `#FEE500`, `#FAE100`, or another Kakao-family yellow.
- Do not infer a Yellow CTA or transfer component from a brand swatch.
- Do not fabricate app error, success, account, badge, or sheet components.
- Do not turn the public corporate site into a Kakao Friends-heavy app mockup.

## 8. Responsive Behavior

The public routes preserve their typographic hierarchy and section grouping while content reflows. Exact mobile-app navigation, banking-task breakpoints, device safe areas, and native keyboard behavior remain unresolved.

## 9. Agent Prompt Guide

> Build a restrained Korean financial information surface with a white canvas, black-first type, pale `#f7f7f7` section fills, Pretendard Variable, very large bold headings, flat navigation, and 6px black public actions. Use `#FFE300` only for verified KakaoBank identity roles and omit unverified native banking components.

## 10. Voice & Tone

KakaoBank's public voice makes finance feel ordinary and useful. It favors direct Korean nouns and verbs, names the service category clearly, and explains benefits without institutional ceremony. Service descriptions should state who a product is for, what financial task it supports, and what the visitor can learn next. Corporate and brand material may be more declarative, while compliance or Help content must stay precise and unambiguous. Avoid playful copy where money movement, identity, or risk is involved. Do not invent rates, user counts, or performance claims.

## 11. Brand Narrative

KakaoBank positions banking as a digital product embedded in everyday routines. The official symbol explains a bank centered on the individual, while the service catalog shows continuous expansion from basic accounts into youth, business, investment, global, and AI-related offerings. The visual identity balances Kakao-family recognition with the restraint expected of a bank: one unmistakable yellow, then a large field of neutral structure. This system allows the company to speak both as a consumer product and as a financial institution. Large Korean headings make service categories approachable; monochrome navigation and pale-gray sections keep attention on information rather than decoration. The protected wordmark and exact yellow establish ownership, but they do not dictate the geometry of native transfers, account cards, or compliance flows. Those product layers require their own evidence.

## 12. Principles

1. **Put the individual at the center.** Organize information around the user's task, not the bank's internal structure.
2. **Use yellow with discipline.** Exact brand recognition is stronger than broad decorative use.
3. **Make finance readable.** Strong Korean typography and generous rhythm should reduce cognitive load.
4. **Do not confuse brand with product evidence.** A CI guide cannot authorize native transfer controls.

## 13. Personas

First-party material establishes task contexts only:
- An individual comparing everyday banking services.
- A young user or guardian exploring KakaoBank mini.
- A business owner reviewing business banking options.
- A visitor seeking official company, ESG, investor, or brand information.

Project-specific names, ages, balances, credit profiles, income, risk tolerance, and success metrics are intentionally unspecified and must come from the product brief.

## 14. States

Top navigation exposed focus, hover, and pressed states. One current service control exposed a disabled state, but its semantic role was insufficient for promotion. Native loading, empty, transfer success, transfer failure, identity verification, and destructive states remain absent.

## 15. Motion & Easing

No reusable duration or easing curve is promoted. Public interaction capture establishes state presence, not native banking motion behavior.

---

**Verified:** 2026-07-12 (omd:migrate)
**Tier 1 sources:** https://www.kakaobank.com/ ; https://www.kakaobank.com/view/service ; https://www.kakaobank.com/view/about/brand/resource ; https://www.kakaobank.com/static/etc/logo/KakaoBank_BrandIdentityGuidelines_V2.0.pdf
**Tier 2 attempts:** getdesign.md/kakaobank returned no design record; Refero had no reliable KakaoBank match
**Conflicts unresolved:** none
