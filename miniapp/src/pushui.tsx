// What a screen shows about push on this device: the Settings card, and the one-line nudge at the
// top of the phone's notification list while push is off.

import { useState } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { usePush } from "./push";
import { pathFor } from "./router";
import { go, useMedia } from "./shell";
import { errorText } from "./ui";

interface TestDevice {
  id: number;
  device: string;
  outcome: string;
}

/** What the test sent to each device, in words; anything the host said that is not "sent" is shown as it said it. */
function testLines(delivered: Record<string, unknown>): string[] {
  const push = delivered.push;
  if (push && typeof push === "object" && Array.isArray((push as { devices?: unknown }).devices)) {
    return ((push as { devices: TestDevice[] }).devices).map((d) => `${d.device}: ${d.outcome === "sent" ? t("push.test.sent") : d.outcome}`);
  }
  return [typeof push === "string" && push.startsWith("skipped: no device") ? t("push.test.nodevice") : String(push ?? "")];
}

export function PushCard({ toast }: { toast: (text: string) => void }) {
  const { state, busy, error, enable, disable } = usePush();
  const [testing, setTesting] = useState(false);
  const [lines, setLines] = useState<string[] | null>(null);

  async function test() {
    setTesting(true);
    try {
      const r = await api.post<{ delivered: Record<string, unknown> }>("/api/notifications/test");
      setLines(testLines(r.delivered));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setTesting(false);
    }
  }

  return (
    <>
      <div className="card push-card" data-push-state={state ?? "loading"}>
        <div className="section-title" style={{ marginTop: 0 }}>{t("push.device")}</div>
        <div className="sub">{state === null ? t("common.loading") : t(`push.state.${state}`)}</div>
        {state === "needs-home-screen" && <ol className="push-steps"><li>{t("push.ios.share")}</li><li>{t("push.ios.add")}</li><li>{t("push.ios.open")}</li></ol>}
        {(state === "off" || state === "on") && (
          <div className="btnrow">
            {state === "off" ? (
              <button className="btn primary small" disabled={busy} onClick={() => void enable()}>{t("push.enable")}</button>
            ) : (
              <button className="btn small" disabled={busy} onClick={() => void disable()}>{t("push.disable")}</button>
            )}
          </div>
        )}
        {error && <div className="sub push-error">{error}</div>}
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("push.test")}</div>
        <div className="sub">{t("push.test.sub")}</div>
        <div className="btnrow">
          <button className="btn small" disabled={testing} onClick={() => void test()}>{t("push.test.send")}</button>
        </div>
        {lines && (
          <ul className="push-test-result">
            {lines.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        )}
      </div>
    </>
  );
}

/** A line above the list on a phone, while this device could have push and does not. */
export function PushNudge() {
  const phone = !useMedia("(min-width: 1024px)");
  const { state } = usePush(phone);
  if (!phone || state !== "off") return null;
  const target = pathFor("settings", "notifications");
  return (
    <a className="push-nudge" href={target} onClick={(e) => go(e, target)}>
      {t("push.nudge")}
    </a>
  );
}
