// Opening and ending a terminal for the operator, wherever it is asked from: the dock, the Terminals
// screen and its full-screen view, the palette. The host answers 409 `over_cap` when the machine already runs as many terminals as the cap
// allows; for an agent that means waiting in line, for a person it is a question — a human choice is
// confirmed, never refused — so the same request goes again with `confirm: true` once they agree.

import { api, ApiError, TerminalCreate, TerminalView } from "../api";
import { confirmDialog } from "../dialogs";
import { t } from "../i18n";
import { errorText } from "../ui";

/** Whether an error is the host's "the cap is reached" rather than a real failure. */
export function overCap(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409 && error.data?.code === "over_cap";
}

/**
 * The new terminal, or null when the operator declined to go past the cap. Any other failure is
 * thrown for the caller to show, as is a failure of the confirmed retry.
 */
export async function createTerminalConfirmed(body: TerminalCreate): Promise<TerminalView | null> {
  try {
    return await api.createTerminal(body);
  } catch (error) {
    if (!overCap(error)) throw error;
    const ok = await confirmDialog({
      title: t("term.cap.title"),
      body: t("term.cap.body", { running: Number(error.data.running ?? 0), cap: Number(error.data.cap ?? 0) }),
      action: t("term.cap.action"),
    });
    if (!ok) return null;
    return await api.createTerminal({ ...body, confirm: true });
  }
}

/**
 * End a terminal's process. Asks first only when something is running in it (a program, not the idle
 * shell); an idle prompt ends at once. A terminal that has already ended is left alone.
 */
export async function endTerminal(id: string, shownTitle: string | undefined, toast: (text: string) => void): Promise<void> {
  let row: TerminalView;
  try {
    row = await api.terminal(id);
  } catch (error) {
    toast(errorText(error));
    return;
  }
  if (row.status !== "running") return;
  const title = shownTitle || row.title || t("term.untitled");
  if (row.live?.busy && !(await confirmDialog({ title: t("term.end.title"), body: t("term.end.confirm", { command: title }), action: t("term.end"), danger: true }))) return;
  try {
    await api.killTerminal(id);
  } catch (error) {
    toast(errorText(error));
  }
}
