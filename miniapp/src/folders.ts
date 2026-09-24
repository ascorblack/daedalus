/** A project's folders, as the screens read them. */

import type { ProjectDir, ProjectRef } from "./api";

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
