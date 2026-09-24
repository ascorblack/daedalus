// What focus mode reads about a project, under the same keys the team page and the board use, so a
// member hired in the sidebar shows on the team page and a task moved on the board moves in the panel
// without a second request. One subscription to the event stream keeps all of it current.

import type { Ask, Project, SessionList, TerminalList, Wakeup, WatchList } from "../api";
import type { ProjectBoardData } from "../board/board";
import { useMemo } from "react";
import { useEvent, useStreamUp } from "../events";
import { useProjects } from "../projects";
import { invalidate, useQuery } from "../store";
import type { Team } from "../team/team";
import { ProjectUsage, usageKey } from "./usage";

const enc = encodeURIComponent;

export const staffKey = (projectId: string) => `/api/projects/${enc(projectId)}/staff?archived=0`;
export const boardKey = (projectId: string) => `/api/projects/${enc(projectId)}/board?include_done=0`;
export const terminalsKey = (projectId: string) => `/api/terminals?project_id=${enc(projectId)}`;
export const asksKey = (projectId: string) => `/api/asks?project=${enc(projectId)}&open=0`;
export const briefKey = (projectId: string) => `/api/projects/${enc(projectId)}/brief`;
export const journalKey = (projectId: string) => `/api/projects/${enc(projectId)}/journal`;
export const wakeupsKey = (projectId: string) => `/api/projects/${enc(projectId)}/wakeups`;
export const watchesKey = (projectId: string) => `/api/projects/${enc(projectId)}/watches`;

type Board = ProjectBoardData & { project: { id: string; name: string } };

/** The project itself, from the list the shell already holds. */
export function useProject(projectId: string): { project: Project | null; loading: boolean } {
  const projects = useProjects();
  return { project: projects.data?.find((p) => p.id === projectId) ?? null, loading: !projects.data && !projects.error };
}

/** Everything the sidebar draws: the team with its queue, the board's counts, the terminals, and the
 *  orchestrator's row in the agents listing. A request that fails leaves its part out rather than the
 *  sidebar: a project with no terminals service simply has no Terminals section. */
export function useFocus(projectId: string) {
  // Polls are the net under the stream: slow while it is up, as the other lists do.
  const live = useStreamUp();
  const team = useQuery<Team>(staffKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 5000 });
  const board = useQuery<Board>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const terminals = useQuery<TerminalList>(terminalsKey(projectId), { pollMs: live ? 60000 : 10000, staleMs: 5000 });
  const sessions = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const wakeups = useQuery<{ wakeups: Wakeup[] }>(wakeupsKey(projectId), { pollMs: live ? 120000 : 30000, staleMs: 15000 });
  const watches = useQuery<WatchList>(watchesKey(projectId), { pollMs: live ? 120000 : 30000, staleMs: 15000 });
  useProjectEvents(projectId);
  // An answer of the wrong shape (a proxy's page, an older host) is no answer: the column draws
  // without that part rather than taking the shell down with it.
  return {
    team: team.data && Array.isArray(team.data.staff) ? team.data : null,
    board: board.data && Array.isArray(board.data.tasks) ? board.data : null,
    terminals: terminals.data && Array.isArray(terminals.data.terminals) ? terminals.data : null,
    sessions: sessions.data ?? null,
    wakeups: wakeups.data && Array.isArray(wakeups.data.wakeups) ? wakeups.data.wakeups : [],
    watches: watches.data && Array.isArray(watches.data.watches) ? watches.data.watches : [],
  };
}

/** Read the project's parts again when its events say they changed. */
export function useProjectEvents(projectId: string): void {
  useEvent(["staff.", "task.", "ask.", "permission.", "project.changed", "terminal.", "schedule.fired", "watch.fired"], (event) => {
    if (event.project_id && event.project_id !== projectId) return;
    if (event.type === "schedule.fired" || event.type === "watch.fired") {
      // A one-off that fired is done, a recurring one moved on, and a watch counted a fire.
      invalidate(wakeupsKey(projectId));
      invalidate(watchesKey(projectId));
      return;
    }
    if (event.type.startsWith("terminal.")) {
      invalidate(terminalsKey(projectId));
      return;
    }
    invalidate(`/api/projects/${enc(projectId)}/staff`);
    invalidate(`/api/projects/${enc(projectId)}/board`);
    if (event.type.startsWith("ask.") || event.type.startsWith("permission.")) invalidate(`/api/asks?project=${enc(projectId)}`);
    if (event.type === "project.changed") {
      invalidate("/api/projects");
      invalidate(`/api/projects/${enc(projectId)}/brief`);
      invalidate(`/api/projects/${enc(projectId)}/journal`);
      invalidate(wakeupsKey(projectId));
      invalidate(watchesKey(projectId));
    }
  }, [projectId]);
}

/** The project's requests, by the short id a tool result or an event line names. The newest request
 *  with an id wins: a short id is unique among open requests, and a closed one may give it up. */
export function useAsks(projectId: string | null): Map<string, Ask> {
  const live = useStreamUp();
  const { data } = useQuery<{ asks: Ask[] }>(projectId ? asksKey(projectId) : null, { pollMs: live ? 60000 : 10000, staleMs: 3000 });
  // Built once per answer from the host, so the chat's turns see the same map until something changed.
  return useMemo(() => {
    const byShort = new Map<string, Ask>();
    for (const ask of data?.asks ?? []) if (!byShort.has(ask.short_id)) byShort.set(ask.short_id, ask);
    return byShort;
  }, [data]);
}

/** What the project spends. Spend moves with every model call, so it is read again when a run of the
 *  project ends and otherwise once a minute. */
export function useUsage(projectId: string) {
  const { data } = useQuery<ProjectUsage>(usageKey(projectId), { pollMs: 60000, staleMs: 10000 });
  useEvent(["run.finished", "staff.status"], (event) => {
    if (event.project_id === projectId) invalidate(usageKey(projectId));
  }, [projectId]);
  return data && data.total ? data : null;
}
