import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import * as passkeys from "../passkeys";
import { errorText } from "../ui";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

export type AuthConfig = {
  /** The bot whose Login Widget vouches for the operator; null on an installation without Telegram. */
  telegram: { bot_username: string } | null;
  /** How many passkeys are enrolled: one is enough to sign in with nothing else. */
  passkeys: number;
  /** Whether a pairing link the server printed is still open. */
  pairing: boolean;
};

/** The site outside Telegram: a passkey, Telegram's Login Widget, or the link the server printed. */
export function LoginScreen({ onDone }: { onDone: () => void }) {
  const slot = useRef<HTMLDivElement>(null);
  const [conf, setConf] = useState<AuthConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<AuthConfig>("/api/auth/config")
      .then(setConf)
      .catch((e) => setError(errorText(e)));
  }, []);

  const bot = conf?.telegram?.bot_username ?? null;
  useEffect(() => {
    if (!bot || !slot.current) return;
    window.onTelegramAuth = async (user) => {
      setBusy(true);
      setError(null);
      try {
        await api.post("/api/auth/telegram", user);
        onDone();
      } catch (e) {
        setError(errorText(e));
        setBusy(false);
      }
    };
    // The widget script renders its button as an iframe right where the script tag sits.
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", bot);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "12");
    script.setAttribute("data-userpic", "false");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    slot.current.replaceChildren(script);
    return () => {
      window.onTelegramAuth = undefined;
    };
  }, [bot, onDone]);

  async function withPasskey() {
    setBusy(true);
    setError(null);
    try {
      await passkeys.signIn();
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  const hasPasskeys = (conf?.passkeys ?? 0) > 0 && passkeys.supported();
  return (
    <div className="login">
      <img className="login-logo" src="/app/icons/icon-192.png" alt="" width={96} height={96} />
      <h1>Daedalus</h1>
      <p className="sub">Your agent, outside Telegram. Sign in as its owner; the session stays in this browser for a month.</p>
      {hasPasskeys && (
        <button className="btn primary" disabled={busy} onClick={() => void withPasskey()}>
          Sign in with a passkey
        </button>
      )}
      {hasPasskeys && bot && <div className="sub">or</div>}
      {bot && <div ref={slot} className="login-widget" />}
      {conf !== null && !hasPasskeys && !bot && (
        <div className="login-note">
          <b>Open the pairing link the server printed.</b>
          <p className="sub">
            It is in the log as <code>pairing link: …</code> and in <code>pairing-url</code> in the state directory. A fresh one comes from{" "}
            <code>daedalus auth pair</code>. The link opens once and signs this browser in; add a passkey afterwards in Settings → Security and you will not need it again.
          </p>
        </div>
      )}
      {busy && <div className="sub">Signing in…</div>}
      {error && <div className="login-error">{error}</div>}
      {conf === null && !error && <div className="sub">Loading…</div>}
      <p className="sub small">Install the site as an app from your browser's menu ("Install Daedalus" / "Add to Home Screen") to open it like any other app.</p>
    </div>
  );
}
