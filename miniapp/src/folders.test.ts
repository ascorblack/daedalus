import { describe, expect, it } from "vitest";
import type { ProjectDir } from "./api";
import { primaryFolder, projectPath, projectReachable } from "./folders";

function dir(id: string, over: Partial<ProjectDir> = {}): ProjectDir {
  return { id, path: `/work/${id}`, label: "", env: "container", is_git: false, readonly: false, position: 0, managed: false, reachable: true, writable: true, ...over };
}

describe("a project's folders", () => {
  it("names the first folder as the primary", () => {
    const project = { folders: [dir("site"), dir("docs", { position: 1 })] };
    expect(primaryFolder(project)?.id).toBe("site");
    expect(projectPath(project)).toBe("/work/site");
  });

  it("is reachable when its primary folder is, whatever the others are", () => {
    expect(projectReachable({ folders: [dir("site", { reachable: false }), dir("docs", { position: 1 })] })).toBe(false);
    expect(projectReachable({ folders: [dir("site"), dir("docs", { position: 1, reachable: false })] })).toBe(true);
  });

  it("answers for a project with no folder rather than throwing", () => {
    expect(primaryFolder({ folders: [] })).toBeUndefined();
    expect(projectPath({ folders: [] })).toBe("");
    expect(projectReachable({ folders: [] })).toBe(false);
  });
});
