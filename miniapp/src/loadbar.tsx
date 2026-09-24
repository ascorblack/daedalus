// The load bar: what running terminals cost the machine now, and what it would carry with the cap
// filled. Settings shows it under the cap while the operator edits it; the Terminals screen can show
// the same component in its header with the configured cap.
//
// One track is the machine's memory. From the left: what everything else uses, what the terminals use
// now, and — lighter — what the terminals up to the cap would add. The colour is the projection's, on
// the app's own ok/warn/bad tokens, and the sentences under it say what each number is, because a bar
// alone reads as "a percentage of something".

import type { TerminalLoad } from "./api";
import { plural, t } from "./i18n";
import { loadFigures, overEstimate, roundBytes } from "./machineload";

/** A size as the sentences use it: "14 GB", "820 MB". */
export function size(n: number): string {
  const { value, unit } = roundBytes(n);
  return t(`fmt.bytes.${unit}`, { n: value });
}

const pct = (n: number) => Math.round(n);

/** A share of the track, as a CSS width that never runs past what is left of it. */
function share(part: number, total: number, used: number): number {
  if (!total || part <= 0) return 0;
  return Math.max(0, Math.min(100 - used, (100 * part) / total));
}

export function LoadBar({ load, cap, compact = false }: { load: TerminalLoad; cap?: number; compact?: boolean }) {
  const f = loadFigures(load, cap ?? load.cap);
  if (!f.known) {
    return (
      <div className="loadbar" data-level="unknown">
        <div className="sub">{t("load.unknown")}</div>
      </div>
    );
  }
  const other = Math.max(0, f.machineNow - f.terminalsNow);
  const otherW = share(other, f.total, 0);
  const nowW = share(f.terminalsNow, f.total, otherW);
  const extraW = share(f.machineAtCap - f.machineNow, f.total, otherW + nowW);
  const daemon = load.used.daemon_rss_bytes ?? 0;
  return (
    <div className="loadbar" data-level={f.level}>
      <div
        className="loadbar-track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct(f.memPercentAtCap)}
        aria-label={t("load.aria", { percent: pct(f.memPercentAtCap), n: f.cap })}
      >
        <i className="loadbar-other" style={{ width: `${otherW}%` }} />
        <i className="loadbar-now" style={{ width: `${nowW}%` }} />
        <i className="loadbar-extra" style={{ width: `${extraW}%` }} />
      </div>
      <div className="loadbar-head">
        <b>{plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) })}</b>
        <span className="loadbar-percent">{t("load.percent", { percent: pct(f.memPercentAtCap) })}</span>
      </div>
      <div className="sub">
        {plural("load.now", f.running, { used: size(f.terminalsNow), free: size(Math.max(0, f.total - f.machineNow)) })}
        {daemon > 0 && ` ${t("load.daemon", { used: size(daemon) })}`}
      </div>
      {!compact && <div className="sub">{t("load.machine", { now: pct(f.memPercentNow), atcap: pct(f.memPercentAtCap) })}</div>}
      <div className="sub loadbar-cpu" data-level={f.cpuLevel}>{t("load.cpu", { now: pct(f.cpuNow), atcap: pct(f.cpuAtCap) })}</div>
      {!compact && <div className="sub faint">{t(`load.basis.${load.likely.basis}`, { each: size(load.likely.rss_bytes) })}</div>}
      {overEstimate(f) && (
        <div className="loadbar-warning" role="status">
          {f.level !== "bad" ? t("load.over.cpu") : f.supported !== null ? plural("load.over", f.supported) : t("load.over.memory")}
        </div>
      )}
    </div>
  );
}
