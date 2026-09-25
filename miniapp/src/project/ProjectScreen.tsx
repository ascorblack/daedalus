// The centre of a project's focus mode: the orchestrator's chat (or the way to switch one on), a
// session of the project, or one of its pages. The route decides which, through `focusView`; this
// file only mounts what that names, with the project's way back instead of the agents list's.

import { lazy, Suspense } from "react";
import { Skeleton } from "../components";
import { t } from "../i18n";
import { back as goBack, navigate, pathFor, projectHome, projectPagePath, useRoute } from "../router";
import { useProject } from "./data";
import { focusView } from "./focus";
import { BriefPage, EnableOrchestrator, FoldersPage, JournalPage, TerminalsPage, WakeupsPage } from "./pages";
import { SetupLine } from "../main/cards";
import { NeedsYouBanner, PhoneBoard, PhoneTeam, PhoneTerminals } from "./phone";

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
      <SessionScreen
        key={orchestrator.session_id}
        id={orchestrator.session_id}
        focus={{ projectId, kind: "orchestrator" }}
        onBack={() => navigate(pathFor("agents"))}
        toast={toast}
        // On a phone the request waiting longest sits under the chat's header: the orchestrator's
        // chat is the tab a project opens on, and a question there should not wait for a scroll. A
        // project the main orchestrator is setting up says so, with the button that ends the setup.
        banner={
          <>
            {project.setup_by === "dispatcher" && <SetupLine projectId={projectId} name={project.name} toast={toast} />}
            {!wide && <NeedsYouBanner projectId={projectId} toast={toast} />}
          </>
        }
      />
    ) : (
      <EnableOrchestrator project={project} toast={toast} />
    );
  } else if (view.kind === "session") {
    // A member's conversation is reached from the team on a phone, and goes back there.
    body = <SessionScreen key={view.id} id={view.id} focus={{ projectId, kind: "member" }} onBack={() => (wide ? navigate(home) : goBack(projectPagePath(projectId, "team")))} toast={toast} />;
  } else if (!wide && view.page === "team") {
    body = <PhoneTeam projectId={projectId} toast={toast} />;
  } else if (!wide && view.page === "board") {
    body = <PhoneBoard projectId={projectId} toast={toast} board={<ProjectBoard projectId={projectId} toast={toast} embedded selected={route.query.get("task")} />} />;
  } else if (!wide && view.page === "terminals") {
    body = <PhoneTerminals projectId={projectId} toast={toast} />;
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
