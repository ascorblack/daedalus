// The screen you land on: a greeting and the composer, so a chat begins by typing it.
// Naming it, choosing a folder and a loop stay in the new-agent sheet for when they matter.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Preset, Project, Settings } from "../api";
import { fieldHeight } from "../composer";
import { ModelChoice, ModelSelect } from "../modelselect";
import { NewAgentSheet, SessionsScreen } from "./Sessions";
import { Icon } from "../icons";
import { navigate, pathFor, sessionPath, useRoute } from "../router";
import { invalidate, useQuery } from "../store";
import { useMedia } from "../shell";
import { enterSends, errorText } from "../ui";
import { t } from "../i18n";

export function StartScreen({ onOpen, toast, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const route = useRoute();
  const creating = route.query.get("new") === "1";
  const closeNew = () => navigate(pathFor("agents"), { replace: true });
  return (
    <>
      <div className="start">
        <div className="start-hero">
          <h1 className="start-greeting">{t("start.greeting")}</h1>
          <StartComposer phone={phone} project={project} toast={toast} />
        </div>
      </div>
      {phone && (
        <div className="start-list">
          <div className="start-list-head">
            <div className="section-title">{t("start.chats")}</div>
            {onProjects && <button type="button" className="iconbtn" onClick={onProjects} title={t("shell.projects")} aria-label={t("shell.projects")}><Icon name="skill" /></button>}
          </div>
          <SessionsScreen onOpen={onOpen} toast={toast} project={project} projects={projects} bare />
        </div>
      )}
      {creating && <NewAgentSheet onClose={closeNew} onCreated={onOpen} toast={toast} project={project} />}
    </>
  );
}

type Chosen = { preset: string } | { provider: string; model: string } | { model: string };

function StartComposer({ phone, project, toast }: { phone: boolean; project: string; toast: (t: string) => void }) {
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultId = settings.data?.model?.preset ?? "";
  const [draft, setDraft] = useState("");
  const [chosen, setChosen] = useState<Chosen | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const labelOf = (id: string) => {
    const item = presets[id] as Preset | undefined;
    return item ? item.label || `${item.provider}/${item.model}` : id;
  };
  const modelLabel = chosen
    ? "preset" in chosen ? labelOf(chosen.preset) : "provider" in chosen ? `${chosen.provider}/${chosen.model}` : chosen.model
    : defaultId ? labelOf(defaultId) : t("newagent.model.default");

  function choose(choice: ModelChoice) {
    if ("clear" in choice) setChosen(null);
    else if ("preset" in choice) setChosen({ preset: choice.preset });
    else if ("provider" in choice) setChosen({ provider: choice.provider, model: choice.model });
    else setChosen({ model: choice.model });
  }

  // A desktop lands here to type. A phone keeps the keyboard down until the field is touched:
  // opening it over the list of chats hides the thing the reader came to find.
  useEffect(() => {
    if (!phone) field.current?.focus();
  }, [phone]);
  const fit = () => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight);
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    el.style.height = `${fieldHeight(el.scrollHeight, line, pad, 1, phone ? 5 : undefined)}px`;
  };
  useLayoutEffect(fit, [draft, phone]);

  async function send() {
    const text = draft.trim();
    if (busy || (!text && files.length === 0)) return;
    setBusy(true);
    let created: { id: string } | null = null;
    try {
      // The message goes out after the session exists, so a model picked here is the one that
      // reads it. Sending both in the create call would start the run on the default model.
      created = await api.post<{ id: string }>("/api/sessions", {
        autotitle: true,
        title: text || files[0]?.name,
        preset: chosen && "preset" in chosen ? chosen.preset : undefined,
        project_id: project || undefined,
      });
      if (chosen && !("preset" in chosen)) {
        await api.post(`/api/sessions/${created.id}/model`, "provider" in chosen ? { provider: chosen.provider, model: chosen.model } : { model: chosen.model });
      }
      if (files.length) {
        const form = new FormData();
        if (text) form.append("text", text);
        for (const file of files) form.append("files", file);
        const response = await fetch(`/api/sessions/${created.id}/upload`, { method: "POST", headers: api.authHeaders(), body: form });
        if (!response.ok) throw new Error(errorText(await response.json().catch(() => response.statusText)));
      } else {
        await api.post(`/api/sessions/${created.id}/messages`, { text });
      }
      setDraft("");
      setFiles([]);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
    if (created) {
      invalidate("/api/sessions");
      navigate(sessionPath(created.id));
    }
  }

  return (
    <div className="start-composer composer">
      <div className="composer-box">
        <textarea
          ref={field}
          value={draft}
          rows={1}
          placeholder={t("session.composer.idle")}
          aria-label={t("session.composer.idle")}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && enterSends()) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-row">
          <input ref={fileInput} type="file" multiple hidden onChange={(event) => { setFiles((held) => [...held, ...Array.from(event.target.files ?? [])]); event.target.value = ""; }} />
          <button type="button" className="iconbtn flat plus" onClick={() => fileInput.current?.click()} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
          <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet={phone} />
          <div className="composer-tools">
            <button type="button" className="roundbtn primary" onClick={() => void send()} disabled={busy || (!draft.trim() && files.length === 0)} aria-label={t("session.send")}><Icon name="up" /></button>
          </div>
        </div>
      </div>
      {files.length > 0 && <div className="sub start-files">{files.map((file) => file.name).join(", ")}</div>}
    </div>
  );
}
