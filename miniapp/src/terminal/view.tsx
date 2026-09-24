// A terminal on screen: a place in the layout for one registry instance, and the strips around it.
//
// The view never creates or resets a terminal. It borrows the instance for as long as it is mounted
// (`acquire`/`release`), lends it a container, and reports whether it is visible; the instance keeps
// its screen, scrollback and connection across views. Everything that decides whether a size may be
// sent lives in `fit.ts` — the view only measures, on a resize of its own box, on a change of
// visibility, and when the window gains focus.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { confirmDialog } from "../dialogs";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { connectionText } from "./status";
import { rememberPaste, TerminalBinding, TerminalInstance, TerminalRequest, TerminalState } from "./instance";
import { TerminalSearch } from "./search";
import { acquireTerminal, terminals } from "./terminals";

export type TerminalViewProps = {
  id: string;
  /** On screen now: false for a background tab or a collapsed dock (the terminal stays connected). */
  visible: boolean;
  readOnly?: boolean;
  /** Which environment it runs in; loopback links are rewritten by that environment's ports. */
  env?: string;
  /** Where file references open: a path relative to `workspace`, and a line. */
  fileOpener?: (path: string, line?: number) => void;
  workspace?: string;
  /** Changing it moves the keyboard focus into the terminal (the person just chose it). */
  focusToken?: number;
  onState?: (id: string, state: TerminalState) => void;
  onRestart?: () => void;
  onRemove?: () => void;
};

function useInstanceState(instance: TerminalInstance | null): TerminalState | null {
  const subscribe = useCallback((listener: () => void) => (instance ? instance.subscribe(listener) : () => undefined), [instance]);
  return useSyncExternalStore(subscribe, () => instance?.state ?? null);
}

export function TerminalView({ id, visible, readOnly, env, fileOpener, workspace, focusToken, onState, onRestart, onRemove }: TerminalViewProps) {
  const screen = useRef<HTMLDivElement>(null);
  const [instance, setInstance] = useState<TerminalInstance | null>(null);
  const [searching, setSearching] = useState(false);
  const state = useInstanceState(instance);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const binding = useRef<TerminalBinding>({});
  binding.current.fileOpener = fileOpener;
  binding.current.workspace = workspace;
  binding.current.context = () => ({ visible: visibleRef.current && !!screen.current?.offsetParent, focused: document.hasFocus() });

  // Borrow the instance for the life of this view; the registry keeps it when the view goes.
  useLayoutEffect(() => {
    const taken = acquireTerminal(id, { readOnly, env });
    const lent = binding.current;
    taken.mount(screen.current!, lent);
    setInstance(taken);
    return () => {
      taken.setVisible(false);
      taken.unbind(lent);
      terminals.release(id);
      setInstance(null);
    };
    // A view is for one terminal; a different id is a different view (the dock keys them by id).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (instance && env) instance.envName = env;
  }, [instance, env]);

  // Visibility: the registry decides WebGL by it, the instance stops proposing sizes without it.
  useLayoutEffect(() => {
    if (!instance) return;
    instance.setVisible(visible);
    terminals.setVisible(id, visible);
    if (!visible) return;
    const frame = requestAnimationFrame(() => instance.fit());
    return () => cancelAnimationFrame(frame);
  }, [instance, id, visible]);

  // A box that changes size proposes a new grid on the next frame (one per frame, however many
  // observations arrive), and a window that gains focus may now claim the size it could not before.
  useEffect(() => {
    if (!instance || !screen.current) return;
    let frame = 0;
    const propose = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => instance.fit());
    };
    const observer = new ResizeObserver(propose);
    observer.observe(screen.current);
    window.addEventListener("focus", propose);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("focus", propose);
    };
  }, [instance]);

  useEffect(() => {
    if (!instance || !focusToken || !visible) return;
    terminals.touch(id);
    const frame = requestAnimationFrame(() => instance.focus());
    return () => cancelAnimationFrame(frame);
    // Only a new token moves the focus; becoming visible alone does not steal it from the composer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance, focusToken]);

  useEffect(() => {
    if (instance && state) onState?.(id, state);
  }, [instance, state, id, onState]);

  useEffect(() => {
    if (!instance) return;
    return instance.onRequest((request: TerminalRequest) => {
      if (request.kind === "search") setSearching(true);
      else if (request.kind === "paste") void confirmPaste(instance, request.text, request.lines);
      else if (request.kind === "link") void confirmLink(request.uri);
    });
  }, [instance]);

  const closeSearch = useCallback(() => {
    setSearching(false);
    instance?.focus();
  }, [instance]);

  const connection = state?.connection;
  const exited = state?.exit ?? (connection?.kind === "exited" ? { code: connection.code, signal: connection.signal } : null);
  const problem = state?.kit === "failed" ? t("term.kit.failed") : connection && connection.kind !== "live" && connection.kind !== "exited" ? connectionText(connection) : "";

  return (
    <div className={`term-view ${visible ? "" : "hidden"}`} data-terminal-view={id} data-state={connection?.kind ?? "loading"}>
      {/* The strips float over the terminal instead of taking rows from it: a strip that came and
          went with the connection would change the grid, and every change is a RESIZE the program
          redraws for. */}
      <div className="term-strips">
      {problem && (
        <div className={`term-strip ${connection?.kind === "unavailable" || connection?.kind === "proxy-blocked" || state?.kit === "failed" ? "bad" : ""}`} role="status">{problem}</div>
      )}
      {state?.sizedElsewhere && !exited && (
        <div className="term-strip other" role="status">
          <span>{t("term.sized.other")}</span>
          <button className="term-strip-action" onClick={() => instance?.claimSize()}>{t("term.fit.here")}</button>
        </div>
      )}
      {state?.agentTyping && <div className="term-strip agent" role="status">{t("term.keyboard.agent", { actor: state.agentTyping })}</div>}
      {!state?.agentTyping && state?.keyboard.owner === "agent" && <div className="term-strip agent" role="status">{t("term.keyboard.held")}</div>}
      </div>
      <div className="term-screen" ref={screen} />
      {searching && instance?.search && <TerminalSearch search={instance.search} onClose={closeSearch} />}
      {exited && (
        <div className="term-exit" role="status">
          <span className="term-exit-text">{exited.code === null && exited.signal ? t("term.state.exitedSignal", { signal: exited.signal }) : t("term.exited", { code: exited.code ?? "?" })}</span>
          {onRestart && <button className="btn small" onClick={onRestart}><Icon name="reload" size={14} />{t("term.restart")}</button>}
          {onRemove && <button className="btn small ghost" onClick={onRemove}><Icon name="trash" size={14} />{t("term.remove")}</button>}
        </div>
      )}
    </div>
  );
}

async function confirmPaste(instance: TerminalInstance, text: string, lines: number): Promise<void> {
  let remember = false;
  const ok = await confirmDialog({
    title: plural("term.paste.lines", lines),
    body: (
      <div className="term-paste">
        <p>{t("term.paste.body")}</p>
        <pre className="term-paste-preview">{text.length > 600 ? `${text.slice(0, 600)}…` : text}</pre>
        <label className="term-paste-remember">
          <input type="checkbox" onChange={(e) => (remember = e.target.checked)} />
          {t("term.paste.remember")}
        </label>
      </div>
    ),
    action: t("term.paste.action"),
  });
  if (ok && remember) rememberPaste(instance.id);
  if (ok) instance.paste(text);
  instance.focus();
}

async function confirmLink(uri: string): Promise<void> {
  const ok = await confirmDialog({ title: t("term.link.title"), body: <code className="term-link-uri">{uri}</code>, action: t("term.link.open") });
  if (ok) window.open(uri, "_blank", "noopener,noreferrer");
}
