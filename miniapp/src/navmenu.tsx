// The menu: every destination, its count, the language and Settings, in a panel that opens upward
// from the bottom-left corner over whatever is on the screen. It replaces the column of
// destinations that used to stand beside the sessions and cost the conversation its width.

import { useEffect, useRef } from "react";
import { LangPicker } from "./components";
import { Overlay, useLayer } from "./dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { menuSections, moveIndex } from "./navigation";
import { Screen, pathFor } from "./router";
import { SelfDevMode, screenTag } from "./capabilities";
import { Counts, ICONS, countFor, go, screenTitle } from "./shell";

const BETA: Screen[] = ["voice"];

export function NavMenu({ screen, counts, selfdev, onClose, opener }: { screen: Screen; counts: Counts; selfdev: SelfDevMode; onClose: () => void; opener?: HTMLElement | null }) {
  const panel = useRef<HTMLDivElement>(null);
  useLayer(onClose);
  // The first item takes focus on open; when the menu closes focus goes back to the button that
  // opened it, or to wherever it was for a keyboard shortcut. The callback's identity changes on
  // every render of the shell and must not re-run this.
  useEffect(() => {
    const from = opener ?? (document.activeElement as HTMLElement | null);
    panel.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    return () => {
      from?.focus?.();
    };
  }, []);
  const items = () => Array.from(panel.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ?? []);
  const focusable = () => Array.from(panel.current?.querySelectorAll<HTMLElement>("[role='menuitem'], .lang button") ?? []);
  const onKey = (e: React.KeyboardEvent) => {
    const list = items();
    const next = moveIndex(list.indexOf(document.activeElement as HTMLElement), e.key, list.length);
    if (next !== null) {
      list[next]?.focus();
      e.preventDefault();
      return;
    }
    // Tab stays inside: the menu is the only thing that can take the keyboard while it is open.
    if (e.key === "Tab") {
      const all = focusable();
      const i = all.indexOf(document.activeElement as HTMLElement);
      const to = e.shiftKey ? all[(i - 1 + all.length) % all.length] : all[(i + 1) % all.length];
      to?.focus();
      e.preventDefault();
    }
  };
  const item = (s: Screen, kbd?: string) => {
    const n = countFor(s, counts);
    const tag = screenTag(s, selfdev, BETA);
    return (
      <a key={s} role="menuitem" href={pathFor(s)} className={`navmenu-item ${screen === s ? "active" : ""}`} aria-current={screen === s ? "page" : undefined} onClick={(e) => { go(e, pathFor(s)); if (!e.defaultPrevented) return; onClose(); }}>
        <Icon name={ICONS[s]} size={18} />
        <span className="truncate">{screenTitle(s)}</span>
        {tag && <span className="beta-tag">{t(tag)}</span>}
        {n > 0 && <span className={`count ${s === "services" ? "ok" : s === "changes" ? "attn" : ""}`}>{n}</span>}
        {kbd && n === 0 && <kbd>{kbd}</kbd>}
      </a>
    );
  };
  return (
    <Overlay>
      <div className="navmenu-backdrop" onClick={onClose} />
      <div ref={panel} className="navmenu" role="menu" aria-label={t("nav.menu")} onKeyDown={onKey}>
        {menuSections(selfdev).map((g) => (
          <div key={g.key} className="navmenu-section" role="group" aria-label={t(`nav.group.${g.key}`)}>
            <div className="navmenu-label">{t(`nav.group.${g.key}`)}</div>
            {g.items.map((s) => item(s))}
          </div>
        ))}
        <div className="navmenu-section">
          <div className="navmenu-lang">
            <span><Icon name="globe" size={18} /> {t("lang.menu")}</span>
            <LangPicker />
          </div>
          {item("settings", "⌘,")}
          <a role="menuitem" href={pathFor("settings", "about")} className="navmenu-item" onClick={(e) => { go(e, pathFor("settings", "about")); if (e.defaultPrevented) onClose(); }}>
            <Icon name="question" size={18} />
            <span className="truncate">{t("settings.sec.about")}</span>
          </a>
        </div>
      </div>
    </Overlay>
  );
}
