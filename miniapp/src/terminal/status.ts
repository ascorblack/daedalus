// A terminal connection's state in words, for the strip over the terminal.

import { t } from "../i18n";
import type { ConnectionState } from "./connection";

export function connectionText(state: ConnectionState): string {
  switch (state.kind) {
    case "connecting":
      return t("term.state.connecting");
    case "live":
      return t("term.state.live");
    case "reconnecting":
      return state.delayMs >= 1000 ? t("term.state.reconnectingIn", { n: Math.round(state.delayMs / 1000) }) : t("term.state.reconnecting");
    case "exited":
      return state.code === null && state.signal ? t("term.state.exitedSignal", { signal: state.signal }) : t("term.state.exited", { code: state.code ?? "?" });
    case "proxy-blocked":
      return t("term.state.proxy");
    case "unavailable":
      switch (state.reason) {
        case "gone":
          return t("term.state.gone");
        case "environment":
          return t("term.state.environment");
        case "origin":
          return t("term.state.origin");
        case "auth":
          return t("term.state.auth");
      }
  }
}
