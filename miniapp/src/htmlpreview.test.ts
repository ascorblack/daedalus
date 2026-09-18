import { describe, expect, it } from "vitest";
import { localHtmlPath } from "./htmlpath";

describe("document links", () => {
  it("resolves relative pages and fragments inside the workspace", () => {
    expect(localHtmlPath("next.html", "site/index.html")).toBe("site/next.html");
    expect(localHtmlPath("#section", "site/index.html")).toBe("site/index.html#section");
    expect(localHtmlPath("../other.html", "site/index.html")).toBe("other.html");
  });
  it("does not let a frame ask the app to fetch a URL or escape the root", () => {
    for (const href of ["https://example.com", "//example.com", "javascript:alert(1)", "/api/settings", "../../secret"]) expect(localHtmlPath(href, "site/index.html")).toBeNull();
  });
});
