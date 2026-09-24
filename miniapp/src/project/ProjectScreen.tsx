// The centre of a project's focus mode: the orchestrator's chat (or the way to switch one on), a
// session of the project, or one of its pages. The route decides which, through `focusView`; this
// file only mounts what that names, with the project's way back instead of the agents list's.

import { lazy, Suspense } from "react";
import { Skeleton } from "../components";
import { t } from "../i18n";
import { navigate, pathFor, projectHome, useRoute } from "../router";
import { useProject } from "./data";
import { focusView } from "./focus";
import { BriefPage, EnableOrchestrator, FoldersPage, JournalPage, TerminalsPage, WakeupsPage } from "./pages";

const SessionScreen = lazy(() => import("../screens/Session").then((m) => ({ default: m.SessionScreen })));
const TeamPage = lazy(() => import("../team/TeamPage").then((m) => ({ default: m.TeamPage })));
const ProjectBoard = lazy(() => import("../board/ProjectBoard").then((m) => ({ default: m.ProjectBoard })));

export function ProjectScreen({ projectId, page, inner, toast, wide }: { projectId: string; page: string | null; inner: string | null; toast: (text: string) => void; wide: boolean }) {
  const route = useRoute();
  const { project, loading } = useProject(projectId);
  const view = focusView(page, inner);
  // On a desktop the sidebar is the way back; a phone has no sidebar, so every page carries one.
  const home = projectHome(projectId);
  const back = wide ? null : home;
  if (!project) return loading ? <Skeleton rows={4} /> : <div className="empty"><b>{t("team.noproject")}</b></div>;

  let body;
  if (view.kind === "orchestrator") {
    const orchestrator = project.settings.orchestrator;
    body = orchestrator?.enabled && orchestrator.session_id ? (
      <SessionScreen key={orchestrator.session_id} id={orchestrator.session_id} focus={{ projectId, kind: "orchestrator" }} onBack={() => navigate(pathFor("agents"))} toast={toast} />
    ) : (
      <EnableOrchestrator project={project} toast={toast} />
    );
  } else if (view.kind === "session") {
    body = <SessionScreen key={view.id} id={view.id} focus={{ projectId, kind: "member" }} onBack={() => navigate(home)} toast={toast} />;
  } else if (view.page === "team") {
    body = <TeamPage projectId={projectId} toast={toast} back={back} />;
  } else if (view.page === "board") {
    body = <ProjectBoard projectId={projectId} toast={toast} selected={route.query.get("task")} back={back} />;
  } else if (view.page === "journal") {
    body = <JournalPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "brief") {
    body = <BriefPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "wakeups") {
    body = <WakeupsPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "folders") {
    body = <FoldersPage projectId={projectId} back={back} toast={toast} />;
  } else {
    body = <TerminalsPage projectId={projectId} selected={route.query.get("t")} back={back} />;
  }
  return <Suspense fallback={<div className="empty">{t("common.loading")}</div>}>{body}</Suspense>;
}
