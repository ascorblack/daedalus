// Named choices keep a discrete setting understandable without dragging an unlabeled track.
import { useId, useRef, useState } from "react";
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

export function EffortOptions({ effort, thinking, onChoose }: Omit<EffortSelectProps, "model">) {
  const name = useId();
  const current = REASONING_EFFORTS[effortIndex(effort)];
  return <fieldset className="effort-options">
    <legend>{t("composer.effort")}</legend>
    {REASONING_EFFORTS.map((value) => <label key={value} className="effort-option">
      <input type="radio" name={name} value={value} checked={!!thinking && value === current} onChange={() => onChoose(value)} />
      <span><b>{t(`add.effort.${value}`)}</b><span className="sub">{t(`composer.effort.${value}.hint`)}</span></span>
    </label>)}
  </fieldset>;
}

export function EffortSelect({ effort, thinking, model, onChoose }: EffortSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const current = REASONING_EFFORTS[effortIndex(effort)];
  return <>
    <button ref={trigger} type="button" className={`effort-select ${open ? "on" : ""}`} onClick={() => setOpen(!open)}
      title={t("composer.effort")} aria-label={t("composer.effort")} aria-haspopup="dialog" aria-expanded={open}>
      <span className="truncate">{thinking ? t(`add.effort.${current}`) : t("composer.effort.select")}</span><Icon name="chevron" size={12} />
    </button>
    {open && <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="effort-menu" align="right" label={t("composer.effort")}>
      <div className="sub truncate">{model}</div>
      <EffortOptions effort={effort} thinking={thinking} onChoose={(value) => { onChoose(value); setOpen(false); }} />
    </Popover>}
  </>;
}
