import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import * as passkeys from "../passkeys";
import { Icon } from "../icons";
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

  const canPasskey = passkeys.supported();
  const hasPasskeys = (conf?.passkeys ?? 0) > 0 && canPasskey;
  const [pairing, setPairing] = useState("");
  function withPairing(e: FormEvent) {
    e.preventDefault();
    // A whole link or just its code: both end up at the same address, which signs this browser in and comes back here.
    const raw = pairing.trim();
    const code = (raw.includes("code=") ? new URL(raw, window.location.origin).searchParams.get("code") : raw) ?? "";
    if (!code) return;
    window.location.assign(`/api/auth/pair?code=${encodeURIComponent(code)}`);
  }
  const methods = (hasPasskeys ? 1 : 0) + (bot ? 1 : 0);
  return (
    <div className="gate">
      <div className="login" role="main" aria-labelledby="login-title">
        <img className="login-logo" src="/app/icons/icon-192.png" alt="" width={72} height={72} />
        <h1 id="login-title">Daedalus</h1>
        <p className="sub">Your agent, in this browser. Sign in as its owner; the session stays for a month.</p>
        {conf === null && !error && <div className="sub">Loading…</div>}
        {conf !== null && (
          <div className="login-methods">
            {hasPasskeys && (
              <button className="btn primary big" disabled={busy} onClick={() => void withPasskey()}>
                <Icon name="key" size={16} /> Sign in with a passkey
              </button>
            )}
            {bot && (
              <>
                {hasPasskeys && <div className="login-or">or</div>}
                <div ref={slot} className="login-widget" />
              </>
            )}
            {methods > 0 && <div className="login-or">or</div>}
            <form className="login-pair" onSubmit={withPairing}>
              <label className="field" htmlFor="pairing">Pairing link or code</label>
              <div className="share-field">
                <input id="pairing" className="field mono" value={pairing} onChange={(e) => setPairing(e.target.value)} placeholder="https://…/api/auth/pair?code=… or the code" autoComplete="off" spellCheck={false} />
                <button className="btn" type="submit" disabled={!pairing.trim() || busy}>Continue</button>
              </div>
              <p className="sub small">
                The server prints one at every start (<code>pairing link: …</code> in its log, <code>pairing-url</code> in the state directory); the desktop app opens it by itself; <code>daedalus auth pair</code> makes a fresh one. It works once, for 30 minutes.
              </p>
            </form>
            {canPasskey && !hasPasskeys && (
              <p className="sub small">No passkey yet: after you sign in, add one in Settings → Security, and the next time Face ID, Touch ID or a security key is enough.</p>
            )}
          </div>
        )}
        {busy && <div className="sub">Signing in…</div>}
        {error && <div className="login-error">{error}</div>}
        <p className="sub small login-foot">Install the site as an app from your browser's menu ("Install Daedalus" / "Add to Home Screen") to open it like any other app.</p>
      </div>
    </div>
  );
}
