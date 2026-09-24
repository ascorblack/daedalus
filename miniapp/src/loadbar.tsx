// The load bar: what running terminals cost the machine now, and what it would carry with the cap
// filled. The Terminals screen shows the short form in its header against the configured cap;
// Settings can show the long form under the cap while the operator edits it, passing the value being
// typed as `cap`.
//
// One track is the machine's memory. From the left: what everything else uses, what the terminals use
// now (their process trees and the terminal daemon that holds their emulators), and — lighter — what
// the terminals up to the cap would add. The colour is the projection's, on the app's own ok/warn/bad
// tokens, and the sentences say what each number is, because a bar alone reads as "a percentage of
// something".

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
      <div className={`loadbar ${compact ? "compact" : ""}`} data-level="unknown">
        <div className="sub">{t("load.unknown")}</div>
      </div>
    );
  }
  const other = Math.max(0, f.machineNow - f.terminalsNow);
  const otherW = share(other, f.total, 0);
  const nowW = share(f.terminalsNow, f.total, otherW);
  const extraW = share(f.machineAtCap - f.machineNow, f.total, otherW + nowW);
  const daemon = load.used.daemon_rss_bytes ?? 0;
  const free = size(Math.max(0, f.total - f.machineNow));
  const track = (
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
  );
  const warning = overEstimate(f) && (
    <div className="loadbar-warning" role="status">
      {f.supported !== null ? plural("load.over", f.supported) : t("load.over.unknown")}
    </div>
  );
  if (compact) {
    // The header's form: what runs now first — the question the screen answers — and the cap's
    // projection under it, both on one short line each so a phone keeps its list in view.
    return (
      <div className="loadbar compact" data-level={f.level} data-cpu-level={f.cpuLevel}>
        {track}
        <div className="loadbar-lines">
          <span className="loadbar-now-text">
            {plural("load.now", f.running, { used: size(f.terminalsNow), free })}
            {daemon > 0 && <span className="faint"> {t("load.daemon", { used: size(daemon) })}</span>}
          </span>
          <span className="loadbar-atcap">
            {plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) })}
            <span className="loadbar-percent"> · {t("load.percent", { percent: pct(f.memPercentAtCap) })}</span>
            <span className="loadbar-cpu"> · {t("load.cpu.short", { atcap: pct(f.cpuAtCap) })}</span>
          </span>
        </div>
        {warning}
      </div>
    );
  }
  return (
    <div className="loadbar" data-level={f.level} data-cpu-level={f.cpuLevel}>
      {track}
      <div className="loadbar-head">
        <b>{plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) })}</b>
        <span className="loadbar-percent">{t("load.percent", { percent: pct(f.memPercentAtCap) })}</span>
      </div>
      <div className="sub">
        {plural("load.now", f.running, { used: size(f.terminalsNow), free })}
        {daemon > 0 && ` ${t("load.daemon", { used: size(daemon) })}`}
      </div>
      <div className="sub">{t("load.machine", { now: pct(f.memPercentNow), atcap: pct(f.memPercentAtCap) })}</div>
      <div className="sub loadbar-cpu">{t("load.cpu", { now: pct(f.cpuNow), atcap: pct(f.cpuAtCap) })}</div>
      <div className="sub faint">{t(`load.basis.${load.likely.basis}`, { each: size(load.likely.rss_bytes) })}</div>
      {warning}
    </div>
  );
}
