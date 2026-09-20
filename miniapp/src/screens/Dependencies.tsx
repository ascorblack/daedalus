import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Sheet } from "../dialogs";
import { t } from "../i18n";
import { errorText } from "../ui";
import "./dependencies.css";

type Recipe = { python: string[]; system: string[] };
type Proposal = { id: string; state: string; request: string; error?: string; explanation?: string; proposal?: { patch: string; recipe: Recipe } };
type View = {
  capability: { mode: string; python: boolean; system: boolean; manager: string; reason: string };
  tools: { name: string; available: boolean; version: string }[];
  packages: [string, string][];
  recipe: Recipe;
  models: { id: string; label: string }[];
  proposal: Proposal | null;
  job: { id: string; state: string; error: string } | null;
};

export function DependenciesTab() {
  const [view, setView] = useState<View | null>(null);
  const [problem, setProblem] = useState("");
  const [offline, setOffline] = useState(false);
  const [request, setRequest] = useState("");
  const [preset, setPreset] = useState("");
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const opened = useRef("");
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const ticket = ++sequence.current;
    try {
      const next = await api.get<View>("/api/dependencies");
      if (ticket !== sequence.current) return;
      setView(next);
      setOffline(false);
      setPreset((current) => next.models.some((model) => model.id === current) ? current : next.models[0]?.id || "");
      if (next.proposal?.state === "ready" && next.proposal.id !== opened.current) {
        opened.current = next.proposal.id;
        setAccepted(false);
        setReview(true);
      }
    } catch {
      if (ticket === sequence.current) setOffline(true);
    }
  }, []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await load();
      if (alive) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { alive = false; ++sequence.current; clearTimeout(timer); };
  }, [load]);

  async function act(path: string, body: object = {}) {
    setBusy(true);
    setProblem("");
    try {
      await api.post(path, body);
      setReview(false);
      setAccepted(false);
      await load();
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  const proposal = view?.proposal;
  const planning = proposal?.state === "planning";
  const installing = view?.job?.state === "installing" || view?.job?.state === "restarting";
  const blocked = busy || planning || installing || offline;
  return <div className="card dependencies">
    <div className="section-title">{t("settings.sec.dependencies")}</div>
    <p className="sub">{t("deps.intro")}</p>
    {offline && <div className="sub attn" role="status">{t("deps.offline")}</div>}
    {problem && <div className="sub attn" role="alert">{problem}</div>}
    {!view && !offline && <p className="sub">{t("common.loading")}</p>}
    {view && <>
      <p className="sub">{t(view.capability.mode === "native" ? "deps.native" : "deps.docker")}</p>
      {!view.capability.python && <p className="sub attn">{view.capability.reason}</p>}
      {!view.capability.system && <p className="sub">{t("deps.noSystem")}</p>}
      <div className="deps-tools">
        {view.tools.map((tool) => <div className="kv" key={tool.name}>
          <code>{tool.name}</code><span className={tool.available ? "sub" : "sub faint"}>{tool.available ? tool.version : t("deps.missing")}</span>
        </div>)}
      </div>
      <details><summary>{t("deps.pythonPackages", { n: String(view.packages.length) })}</summary>
        {view.packages.map(([name, version]) => <div className="kv" key={name}><code>{name}</code><span className="sub">{version}</span></div>)}
      </details>
      <details><summary>{t("deps.recipe")}</summary><pre>{JSON.stringify(view.recipe, null, 2)}</pre></details>
      <label className="deps-label" htmlFor="dependency-request">{t("deps.request")}</label>
      <textarea id="dependency-request" className="field" rows={3} maxLength={2000} value={request} disabled={blocked} onChange={(event) => setRequest(event.target.value)} placeholder={t("deps.placeholder")} />
      <label className="deps-label" htmlFor="dependency-model">{t("deps.model")}</label>
      <select id="dependency-model" className="field" value={preset} disabled={blocked} onChange={(event) => setPreset(event.target.value)}>
        {!view.models.length && <option value="">{t("deps.noModels")}</option>}
        {view.models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
      </select>
      <div className="btnrow">
        <button className="btn primary" disabled={blocked || !view.capability.python || !request.trim() || !preset} onClick={() => void act("/api/dependencies/request", { request, preset })}>{t(planning ? "deps.planning" : "deps.prepare")}</button>
        {proposal && ["planning", "ready"].includes(proposal.state) && <button className="btn" disabled={busy || offline} onClick={() => void act(`/api/dependencies/${proposal.id}/cancel`)}>{t("common.cancel")}</button>}
        {proposal?.state === "ready" && <button className="btn" onClick={() => { setAccepted(false); setReview(true); }}>{t("deps.review")}</button>}
      </div>
      {proposal?.state === "failed" && <p className="sub attn" role="alert">{proposal.error}</p>}
      {view.job && <div className="comp-restart" role="status">
        <b>{t(`deps.job.${view.job.state}`)}</b>
        {installing && <p className="sub">{t("deps.installingHint")}</p>}
        {view.job.error && <p className="sub attn">{view.job.error}</p>}
      </div>}
    </>}
    {review && proposal?.state === "ready" && proposal.proposal && <Sheet title={t("deps.review")} onClose={() => !busy && setReview(false)}>
      <div className="dependencies">
        <p className="deps-explanation">{proposal.explanation}</p>
        <pre className="deps-patch">{proposal.proposal.patch}</pre>
        <p className="sub attn">{t("deps.warning")}</p>
        {view?.capability.mode === "native" && <p className="sub">{t("deps.nativeWarning")}</p>}
        {problem && <p className="sub attn" role="alert">{problem}</p>}
        <label className="deps-consent"><input type="checkbox" checked={accepted} disabled={busy} onChange={(event) => setAccepted(event.target.checked)} />{t("deps.consent")}</label>
        <div className="btnrow">
          <button className="btn primary" disabled={!accepted || busy || offline || installing} onClick={() => void act(`/api/dependencies/${proposal.id}/approve`)}>{t("deps.install")}</button>
          <button className="btn" disabled={busy || offline} onClick={() => void act(`/api/dependencies/${proposal.id}/cancel`)}>{t("common.cancel")}</button>
        </div>
      </div>
    </Sheet>}
  </div>;
}
