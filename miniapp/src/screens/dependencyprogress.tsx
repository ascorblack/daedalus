import { useEffect, useState } from "react";
import { t } from "../i18n";

export type Progress = { started_at?: number; updated_at?: number; last_output_at?: number; restart_at?: number; stage?: string; detail_stage?: string; progress?: { at: number; stage: string; detail?: string }[]; log?: string };
function duration(seconds: number) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
export function DependencyProgress({ value, active, installation = false }: { value: Progress; active: boolean; installation?: boolean }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => { if (!active) return; const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer); }, [active]);
  const events = value.progress || [];
  const stage = value.stage === "building" ? value.detail_stage || "building" : value.stage || events.at(-1)?.stage;
  const started = value.started_at || events[0]?.at;
  const elapsed = started ? duration((active ? now : value.updated_at || now) - started) : null;
  const last = Math.max(value.updated_at || 0, value.last_output_at || 0);
  return <div className="deps-progress">
    {stage && <div className="deps-progress-heading" role="status"><b>{t(`deps.stage.${stage}`)}</b>{elapsed && <span>{elapsed}</span>}</div>}
    {active && last > 0 && <p className="sub">{t("deps.lastActivity", { n: duration(now - last) })}</p>}
    {active && installation && <p className="sub">{stage === "restarting" ? t("deps.restartNow") : value.restart_at ? t("deps.restartIn", { n: String(Math.max(0, Math.ceil(value.restart_at - now))) }) : t("deps.estimate")}</p>}
    {events.length > 0 && <details open={active}><summary>{t("deps.activity")}</summary><ol className="deps-events">
      {events.map((event, index) => <li key={index}><time>{duration(event.at - (started || event.at))}</time><span>{t(`deps.stage.${event.stage === "building" && event.detail ? event.detail : event.stage}`)}{event.detail && event.stage !== "building" && <small>{event.detail}</small>}</span></li>)}
    </ol></details>}
    {value.log && <details><summary>{t("deps.installLog")}</summary><pre className="deps-live-log">{value.log}</pre></details>}
  </div>;
}
