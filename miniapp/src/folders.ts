/** A project's folders, as the screens read them. */

import type { ProjectDir, ProjectRef, SessionFolder } from "./api";

/** The folder an agent of the project works in unless it is given another: the first one. */
export function primaryFolder(project: Pick<ProjectRef, "folders">): ProjectDir | undefined {
  return project.folders[0];
}

/** The primary folder's path, for the line under a project's name. */
export function projectPath(project: Pick<ProjectRef, "folders">): string {
  return primaryFolder(project)?.path ?? "";
}

/** Whether an agent can be started in the project from where the bot runs: its primary folder is there. */
export function projectReachable(project: Pick<ProjectRef, "folders">): boolean {
  return primaryFolder(project)?.reachable ?? false;
}

/** A folder's name in a list: the operator's label, else the last part of its path. */
export function folderName(folder: Pick<SessionFolder, "label" | "path">): string {
  return folder.label.trim() || folder.path.split("/").filter(Boolean).pop() || folder.path;
}

/**
 * Where a session's file pane reads a folder from. The session's own folder is the session's own
 * address; another one is under `/folders/{id}`, so every file address built from the base — the
 * preview, a download, a page preview's links — names the folder without knowing about it.
 */
export function folderBase(sessionBase: string, folder: string, home: string): string {
  return !folder || folder === home ? sessionBase : `${sessionBase}/folders/${encodeURIComponent(folder)}`;
}
