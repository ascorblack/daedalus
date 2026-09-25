// What the Questions tab reads: the operator's list, one project's or every orchestrated project's,
// kept current by the event stream. A question the orchestrator adds is an `ask.pending`; one
// answered anywhere, or taken back, is an `ask.answered` or a `permission.resolved` — each says
// "read the list again", and the list is the truth.

import type { WaitingQuestion } from "../api";
import { useEvent, useStreamUp } from "../events";
import { invalidate, useQuery } from "../store";

/** Whose list a tab shows: one project's in its focus mode, or all of them in the main chat. */
export type QuestionScope = { projectId: string } | "all";

export const QUESTIONS_KEY = "/api/questions";

export function questionsKey(scope: QuestionScope): string {
  return scope === "all" ? QUESTIONS_KEY : `${QUESTIONS_KEY}?project=${encodeURIComponent(scope.projectId)}`;
}

/** Where a batch of answers from this list is sent. */
export function answerPath(scope: QuestionScope): string {
  return scope === "all" ? "/api/asks/answer" : `/api/projects/${encodeURIComponent(scope.projectId)}/asks/answer`;
}

/** The list, or null before the first answer from the host. */
export function useQuestions(scope: QuestionScope | null): { questions: WaitingQuestion[] | null; error: string | null } {
  const live = useStreamUp();
  const key = scope ? questionsKey(scope) : null;
  const query = useQuery<{ questions: WaitingQuestion[] }>(key, { pollMs: live ? 60000 : 10000, staleMs: 2000 });
  const projectId = scope && scope !== "all" ? scope.projectId : null;
  useEvent(["ask.", "permission.", "project.changed"], (event) => {
    if (projectId && event.project_id && event.project_id !== projectId) return;
    invalidate(QUESTIONS_KEY);
  }, [projectId]);
  // An answer of the wrong shape (an older host) draws an empty list rather than breaking the panel.
  const questions = query.data && Array.isArray(query.data.questions) ? query.data.questions : null;
  return { questions, error: query.error };
}
