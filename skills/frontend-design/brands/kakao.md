# Kakao — brand reference

- Category: consumer-tech
- Site: https://www.kakaocorp.com/page/
- Primary colour: #fee500
- Keywords: warm, friendly, functional, personal, sunshine, yellow, korean, messaging, super-app

## 1. Visual Theme & Atmosphere

Kakao is a mobile-life platform whose identity is built around making communication and everyday services feel immediately approachable. Yellow, speech-bubble symbolism, friendly proprietary type, and compact Korean language create recognition before a person reads a product name. That familiarity spans many services, but it is not one transferable UI kit: the corporate site uses KakaoBig and KakaoSmall with black, white, and marketing yellow; Kakao Login is a regulated integration component; and Kakao Developers uses Pretendard only for documentation chrome. Correct reuse begins by naming the surface and relationship being designed.

The clean-slate rule is therefore surface identity first: name whether a value belongs to corporate marketing, the official login integration, or documentation chrome before reusing it.

**Key Characteristics:**
- Corporate fonts: KakaoSmall 133 visible uses; KakaoBig 49
- Developers docs font: Pretendard 1,339 visible uses, documentation chrome only
- Login compliance: `#fee500`, black symbol, black 85% label, 12px radius, OS system font at 30Pt
- Corporate marketing yellow `#fae100` remains distinct from login yellow

## 2. Color Palette & Roles

### Kakao Login compliance
- **Container** (`#fee500`): mandatory login-button background.
- **Symbol** (`#000000`): mandatory speech-bubble symbol color.
- **Label** (`rgba(0, 0, 0, 0.85)`): mandatory 85% black label.

The official guide explicitly says colors outside this regulation must not be applied and that another third-party login button must not be emphasized by omitting Kakao's button color.

### Corporate marketing
- **Marketing Yellow** (`#fae100`): current corporate-home marketing accent.
- **Dark Marketing** (`#111111`): current paired dark tag/CTA background.
- **Canvas** (`#ffffff`): dominant public-site background.
- **Foreground** (`#333333`): primary corporate text/control color.
- **On Dark** (`#ffffff`): content on black/dark surfaces.
- **Muted** (`#888888`): milestone metadata.
- **Surface** (`#eeeeee`): current filter/footer-pill fill.
- **Border** (`#dbdbdb`): captured culture-page circular-control border.

## 3. Typography Rules

### Verified families
- **KakaoBig**: loaded/high; 49 visible uses; display, navigation, card, and heading roles.
- **KakaoSmall**: loaded/high; 133 visible uses; body, input, button, badge, and heading roles.
- **System**: required by the Kakao Login design guide for the 30Pt label.
- **Pretendard**: 1,339 visible uses on the Kakao Developers documentation website. It is documentation chrome, not the Kakao Login label font.
- **KakaoDigitLatin**: declared but 0 visible uses; not promoted.

| Role | Family | Size | Weight | Line Height | Tracking |
|---|---|---:|---:|---:|---:|
| Corporate Display | KakaoBig | 53.82px | 700 | 66px | -1px |
| Corporate Heading | KakaoBig | 30px | 700 | 40px | normal |
| Corporate Title | KakaoSmall | 28px | 700 | 42px | normal |
| Navigation | KakaoBig | 17px | 400 | 27px | normal |
| Body | KakaoSmall | 14px | 400 | 21px | normal |
| Readable Body | KakaoSmall | 14px | 400 | 24.92px | -0.2px |
| Caption | KakaoSmall | 12px | 400 | 18px | -0.2px |
| Kakao Login Label | OS system | 30Pt | guide does not specify | proportional | unchanged |

| Evidence class | Kakao status |
|---|---|
| **Official product-use** | The Kakao Login guide mandates OS system type for its label; KakaoBig/KakaoSmall are the official corporate families |
| **Live surface-use** | KakaoBig and KakaoSmall are loaded on corporate surfaces; Pretendard is loaded on developer-documentation chrome |
| **Official distributed asset** | No general redistribution right for KakaoBig/KakaoSmall is asserted by the current official sources |
| **Declared-only** | KakaoDigitLatin is declared but had zero visible first-family uses in the inspected surfaces |
| **Unresolved** | A universal KakaoTalk/native-service family mapping beyond the exact official surfaces documented here |

## 4. Component Stylings

### Kakao Login Button
- Background: `#fee500`
- Symbol: `#000000`, mandatory Kakao speech bubble; shape, proportion, and color cannot change
- Label: `rgba(0, 0, 0, 0.85)`
- Radius: 12px
- Font: 30Pt OS default system font
- Labels: `카카오 로그인` / `Login with Kakao`, or shortened `로그인` / `Login`
- States: full or shortened label; hover, pressed, and disabled visuals are not specified by this guide
- Use: login integration only; preserve symbol/label area when resizing

### Corporate Navigation Menu
- Background: `#ffffff` in light mode; `#000000` on captured dark subpages
- Text: `#000000` in light mode; `#ffffff` in dark mode
- Radius: 999px
- Padding: 4px 16px 6px
- Height: 37px
- Font: 17px / 400 / KakaoBig
- States: light/dark themes, focus, hover

### Corporate Search Control
- Background: transparent
- Text/Icon: `#333333`
- Radius: 18px
- Height: 36px
- Font context: 14px / 400 / KakaoSmall
- States: default and hover observed

### Dark Marketing Tag
- Background: `#111111`
- Text: `#ffffff`
- Border: 2px solid `#ffffff`
- Radius: 16px
- Padding: 7px 8px 8px
- Height: 32px
- Font: 13px / 700 / KakaoSmall

### Milestone Filter
- Background: `#eeeeee`
- Text: `#000000`
- Radius: 30px
- Padding: 8px 20px 12px
- Height: 44px
- Font: 16px / 700 / KakaoBig
- States: selected filter captured

### Footer Related-Site Pill
- Background: `#eeeeee`
- Text: `#000000`
- Radius: 24px
- Padding: 10px 20px 12px
- Height: 40px
- Font: 12px / 400 / KakaoSmall
- States: default captured; hover not retained

---

**Verified:** 2026-07-11 (verification v2, current corporate surfaces + rendered official Login guide)
**Tier 1 sources:** https://www.kakaocorp.com/page/ https://www.kakaocorp.com/page/about/culture https://www.kakaocorp.com/page/about/milestones https://developers.kakao.com/docs/ko/kakaologin/design-guide
**Tier 2 sources:** https://getdesign.md/kakao and https://styles.refero.design/?q=Kakao did not provide importable current Kakao records in this run.
**Surface split:** corporate marketing uses KakaoBig/KakaoSmall; the Login component requires OS system type; Developers docs chrome uses Pretendard.
**Conflicts unresolved:** none

## 5. Layout Principles

- Corporate content uses a compact 4/6/8/16/20 spacing rhythm around controls and text.
- The Login guide requires the symbol and label zones to remain intact when widening the container; expand left and right equally.
- When scaling the whole Login button, preserve symbol/label proportions and keep the label height at no more than one third of the container height.

## 6. Depth & Elevation

No canonical shadow token is promoted. Captured corporate controls are largely flat; the Developers documentation site contains its own hover shadows, but those belong to documentation chrome rather than the Kakao Login component.

## 7. Do's and Don'ts

### Do
- Use the exact Kakao Login compliance colors, symbol, radius, and system font.
- Keep corporate KakaoBig/KakaoSmall values separate from Developers-site Pretendard.
- Name marketing yellow `#fae100` separately from login yellow `#fee500`.
- Use the shortened Login label instead of distorting a standard button below its intended size.
- Preserve observed theme and interaction states on corporate navigation.

### Don't
- Don't change the Kakao Login symbol shape, proportion, or color.
- Don't replace the speech-bubble symbol with Kakao CI or a functional icon.
- Don't remove the symbol from the Login button.
- Don't use Developers-site Pretendard as evidence that the Login button uses Pretendard.
- Don't present unverified KakaoTalk chat bubbles, mobile inputs, tabs, badges, or semantic colors as current canonical Kakao tokens.

## 8. Responsive Behavior

The corporate pages are responsive, but this run does not promote universal breakpoints. For the Login button, follow the official proportional resizing rule: extend both sides equally when widening, preserve symbol/label zones, and use the shortened label if a smaller format is required.

## 9. Agent Prompt Guide

- “Build a compliant Kakao Login button with `#fee500`, black speech-bubble symbol, 85% black label, 12px radius, and OS system font at 30Pt.”
- “Create the current corporate navigation pill with KakaoBig 17px/400, 37px height, 999px radius, and explicit light/dark themes.”
- “Use KakaoSmall for corporate body/control text; do not substitute Pretendard unless reproducing Developers documentation chrome.”
- “Treat any KakaoTalk in-app component as an unverified extension unless native product evidence is supplied.”

## 10. Voice & Tone

Kakao’s corporate mission centers on bringing a needed future closer through technology that understands people. In user-facing copy, that translates into familiar Korean, short labels, and service explanations grounded in an everyday action—talking, finding a place, sending a gift, following a channel—rather than abstract technology claims.

The tone changes by surface. Corporate and culture pages can be optimistic and connective; product/service descriptions should be concrete about the benefit; developer and compliance guidance must be prescriptive and exact. Kakao Login wording, symbol, color, and label rules are not a place for playful rewriting. Avoid turning Kakao’s friendliness into childishness or using yellow as a substitute for clear instructions.

## 11. Brand Narrative

Kakao describes itself as a mobile-life platform that uses technology, content, and high mobile reach to make everyday services more convenient. Its current mission—bringing a needed future closer through technology that understands people—places human context before technical novelty.

KakaoTalk’s communication model became the organizing metaphor for a broader ecosystem: channels connect people with brands and stores, messages carry transactional information, and services extend into commerce, maps, content, mobility, and social impact. The corporate yellow and speech-bubble identity therefore signal connection, but each service still needs its own product rules.

This is why surface separation is essential. Kakao’s corporate type and marketing yellow express the parent identity; Kakao Login uses a precisely regulated `#fee500` component so third-party integrations remain recognizable and trustworthy; developer documentation uses its own Pretendard chrome. A coherent Kakao reference preserves the relationship between these systems without flattening them into one fictional theme.

## 12. Principles

These are evidence-derived implementation principles:

1. **Compliance outranks approximation.** Login symbols, labels, colors, type, and relative emphasis follow the official integration guide even when another treatment looks more fashionable.
2. **Surface-specific fonts are part of correctness.** KakaoBig/KakaoSmall, OS system labels, and Pretendard documentation chrome describe different jobs, not a fallback chain.
3. **Preserve the dual-yellow distinction.** Corporate marketing `#fae100` and regulated Login `#fee500` remain separately named and sourced.
4. **Connection should stay legible.** Friendly language and familiar symbols can reduce distance, but transactional information, consent, and support states must remain explicit.
5. **Use first-party evidence over remembered KakaoTalk conventions.** Do not fabricate chat bubbles, navigation, or native-service tokens from cultural familiarity with the brand.

## 13. Personas

These are official service and stakeholder contexts, not invented demographic personas.

- **A person communicating in daily life:** expects familiar language, fast recognition, and continuity between conversation and adjacent services.
- **A customer receiving information or benefits from a channel:** needs promotional, transactional, and support messages to remain clearly distinguishable.
- **A business or creator operating a Kakao channel:** needs approachable customer communication without losing operational clarity or consent boundaries.
- **A developer integrating Kakao Login:** needs exact compliance rules for symbol, color, type, label, and relative emphasis so the integration remains trustworthy.

## 14. States

| Component | Verified state evidence |
|---|---|
| Kakao Login | full/short label and resizing constraints; interaction-state styling not specified |
| Corporate nav | light/dark themes, focus, hover |
| Search control | default, hover |
| Milestone filter | selected |
| Marketing/footer labels | default only |

## 15. Motion & Easing

No exact motion duration or easing token is promoted. Preserve focus and hover clarity on the corporate site and platform-standard interaction feedback for the Login button; label custom timing as an extension until first-party evidence is available.
