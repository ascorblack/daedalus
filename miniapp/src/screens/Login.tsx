import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { errorText } from "../ui";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

/** The site outside Telegram: the owner signs in with Telegram's Login Widget and gets a session cookie. */
export function LoginScreen({ onDone }: { onDone: () => void }) {
  const slot = useRef<HTMLDivElement>(null);
  const [bot, setBot] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<{ bot_username: string }>("/api/auth/config")
      .then((c) => setBot(c.bot_username))
      .catch((e) => setError(errorText(e)));
  }, []);

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

  return (
    <div className="login">
      <img className="login-logo" src="/app/icons/icon-192.png" alt="" width={96} height={96} />
      <h1>Daedalus</h1>
      <p className="sub">Your agent, outside Telegram. Sign in with the Telegram account that owns it; the session stays in this browser for a month.</p>
      <div ref={slot} className="login-widget" />
      {busy && <div className="sub">Signing in…</div>}
      {error && <div className="login-error">{error}</div>}
      {bot === null && !error && <div className="sub">Loading…</div>}
      <p className="sub small">Install the site as an app from your browser's menu ("Install Daedalus" / "Add to Home Screen") to open it like any other app.</p>
    </div>
  );
}
