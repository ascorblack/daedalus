// Reasoning effort as a chip that opens a slider upward from the composer. The composer sits at
// the bottom of the window, so the menu is a Popover (bottom-anchored), never a sheet that would
// grow the page or clip under the fold.

import { useEffect, useRef, useState } from "react";
import { Popover } from "./dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { REASONING_EFFORTS, effortIndex } from "./models";

export type EffortSelectProps = {
  effort?: string;
  thinking?: boolean;
  model: string;
  onChoose: (effort: string) => void;
};

export function EffortSelect({ effort, thinking, model, onChoose }: EffortSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const index = effortIndex(effort);
  const current = REASONING_EFFORTS[index];
  const label = thinking ? t(`add.effort.${current}`) : t("composer.effort.select");

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={`effort-select ${open ? "on" : ""}`}
        onClick={() => setOpen((o) => !o)}
        title={t("composer.effort")}
        aria-label={t("composer.effort")}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <span className="truncate">{label}</span>
        <Icon name="chevron" size={12} />
      </button>
      {open && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="effort-menu" align="right" label={t("composer.effort")}>
          <div className="effort-menu-head">
            <Icon name="bolt" size={16} />
            <span className="effort-menu-name">{t(`add.effort.${current}`)}</span>
            <span className="effort-menu-pad" aria-hidden />
          </div>
          {model ? <div className="effort-menu-model truncate" title={model}>{model}</div> : null}
          <EffortSlider
            value={index}
            onChange={(i) => onChoose(REASONING_EFFORTS[i])}
          />
        </Popover>
      )}
    </>
  );
}

function EffortSlider({ value, onChange }: { value: number; onChange: (i: number) => void }) {
  const track = useRef<HTMLDivElement>(null);
  const live = useRef(value);
  const dragging = useRef(false);
  const [shown, setShown] = useState(value);
  const n = REASONING_EFFORTS.length;
  useEffect(() => {
    live.current = value;
    setShown(value);
  }, [value]);

  function at(clientX: number): number {
    const r = track.current?.getBoundingClientRect();
    if (!r || r.width <= 0) return live.current;
    const t = (clientX - r.left) / r.width;
    return Math.max(0, Math.min(n - 1, Math.round(t * (n - 1))));
  }
  function show(i: number) {
    live.current = i;
    setShown(i);
  }
  function commit() {
    onChange(live.current);
  }

  return (
    <div
      ref={track}
      className="effort-slider"
      role="slider"
      tabIndex={0}
      aria-valuemin={0}
      aria-valuemax={n - 1}
      aria-valuenow={shown}
      aria-valuetext={t(`add.effort.${REASONING_EFFORTS[shown]}`)}
      aria-label={t("composer.effort")}
      onPointerDown={(e) => {
        dragging.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        show(at(e.clientX));
      }}
      onPointerMove={(e) => {
        if (!dragging.current) return;
        show(at(e.clientX));
      }}
      onPointerUp={() => {
        if (!dragging.current) return;
        dragging.current = false;
        commit();
      }}
      onPointerCancel={() => {
        dragging.current = false;
      }}
      onKeyDown={(e) => {
        const next =
          e.key === "ArrowRight" || e.key === "ArrowUp" ? Math.min(n - 1, shown + 1)
          : e.key === "ArrowLeft" || e.key === "ArrowDown" ? Math.max(0, shown - 1)
          : e.key === "Home" ? 0
          : e.key === "End" ? n - 1
          : null;
        if (next === null) return;
        e.preventDefault();
        show(next);
        onChange(next);
      }}
    >
      <div className="effort-slider-track">
        <div className="effort-slider-fill" style={{ width: `${(shown / (n - 1)) * 100}%` }} />
        {REASONING_EFFORTS.map((name, i) => (
          <span key={name} className={`effort-slider-dot ${i <= shown ? "on" : ""}`} style={{ left: `${(i / (n - 1)) * 100}%` }} />
        ))}
        <span className="effort-slider-knob" style={{ left: `${(shown / (n - 1)) * 100}%` }} />
      </div>
    </div>
  );
}
