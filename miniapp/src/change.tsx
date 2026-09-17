// The strip that says the agent has changed its own code, and what became of it.
//
// A local installation has no pull request to review: the change is already committed to the
// checkout and the only thing between it and running is a restart. That makes the notice load
// bearing rather than decorative — it is the whole review step — so it sits above the screen
// rather than inside one, and it stays there until the reader has been told how it ended.

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Capabilities } from "./capabilities";
import { changeNotice } from "./capabilities";
import type { Query } from "./store";
import { t } from "./i18n";

const SEEN_KEY = "daedalus.changeSeen";

function seen(): string {
  try {
    return localStorage.getItem(SEEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function ChangeStrip({ caps }: { caps: Query<Capabilities> }) {
  const [dismissed, setDismissed] = useState(seen);
  const [restarting, setRestarting] = useState(false);
  const [problem, setProblem] = useState("");
  const commit = caps.data?.restart_required?.commit ?? "";
  const refresh = useRef(caps.refresh);
  refresh.current = caps.refresh;

  // While the app is restarting the answer that matters arrives in seconds, not in the five minutes
  // the capabilities are otherwise polled at — and for part of that time there is nothing at the
  // other end to answer at all, which is exactly what makes a slow poll read as a hung button.
  useEffect(() => {
    if (!restarting) return;
    const timer = window.setInterval(() => void refresh.current(), 2000);
    return () => window.clearInterval(timer);
  }, [restarting]);

  // The restart is over when the change it was applying is no longer the one waiting.
  useEffect(() => {
    if (restarting && commit === "") setRestarting(false);
  }, [restarting, commit]);

  const notice = changeNotice(caps.data, dismissed);
  if (!notice) return null;

  const dismiss = () => {
    setDismissed(notice.commit);
    try {
      localStorage.setItem(SEEN_KEY, notice.commit);
    } catch {
      /* private mode: the strip comes back, which is the harmless way to be wrong */
    }
  };

  const restart = async () => {
    setProblem("");
    setRestarting(true);
    try {
      await api.post("/api/self/restart");
    } catch (e) {
      setRestarting(false);
      setProblem(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className={`change-strip ${notice.kind}`} role="status">
      <div className="change-text">
        <b>{notice.title}</b>
        <span>{notice.body}</span>
        {problem && <span className="change-problem">{problem}</span>}
      </div>
      <div className="change-actions">
        {notice.action && (
          <button className="btn primary" onClick={() => void restart()} disabled={restarting}>
            {restarting ? t("change.restarting") : t("change.restart")}
          </button>
        )}
        {!notice.action && (
          <button className="btn" onClick={dismiss}>
            {t("change.dismiss")}
          </button>
        )}
      </div>
    </div>
  );
}
