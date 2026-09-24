import { describe, expect, it } from "vitest";
import { findFileLinks, parsePortRange, resolveFileLink, rewriteLoopbackUrl } from "./links";

const paths = (text: string) => findFileLinks(text).map((l) => [text.slice(l.start, l.end), l.path, l.line, l.col]);

describe("findFileLinks", () => {
  it("finds references with a line and a column, as compilers print them", () => {
    expect(paths("src/cart.ts:42:7 - error TS2322")).toEqual([["src/cart.ts:42:7", "src/cart.ts", 42, 7]]);
    expect(paths("  at render (./components/Cart.tsx:118:12)")).toEqual([["./components/Cart.tsx:118:12", "./components/Cart.tsx", 118, 12]]);
    expect(paths("FAIL ../shared/money.test.ts:9")).toEqual([["../shared/money.test.ts:9", "../shared/money.test.ts", 9, undefined]]);
    expect(paths("/srv/workspaces/shop/app.py:3: warning")).toEqual([["/srv/workspaces/shop/app.py:3", "/srv/workspaces/shop/app.py", 3, undefined]]);
    expect(paths("main.go:10:2: undefined: x")).toEqual([["main.go:10:2", "main.go", 10, 2]]);
  });

  it("finds a path to a file without a line number", () => {
    expect(paths("wrote dist/index.html and ./run.sh")).toEqual([
      ["dist/index.html", "dist/index.html", undefined, undefined],
      ["./run.sh", "./run.sh", undefined, undefined],
    ]);
  });

  it("does not take times, versions, ports, words or URLs for files", () => {
    expect(paths("at 12:30:05 node v20.11.1 listening on host:8080")).toEqual([]);
    expect(paths("error: 3 problems, see README")).toEqual([]);
    expect(paths("open http://localhost:5173/src/main.ts:4 now")).toEqual([]);
    expect(paths("user@example.com")).toEqual([]);
  });
});

describe("resolveFileLink", () => {
  const workspace = "/srv/workspaces/shop";

  it("reads a relative reference against the terminal's directory", () => {
    expect(resolveFileLink("src/cart.ts", "/srv/workspaces/shop", workspace)).toBe("src/cart.ts");
    expect(resolveFileLink("./Cart.tsx", "/srv/workspaces/shop/web/components", workspace)).toBe("web/components/Cart.tsx");
    expect(resolveFileLink("../shared/money.ts", "/srv/workspaces/shop/web/", workspace)).toBe("shared/money.ts");
  });

  it("makes an absolute reference relative to the workspace", () => {
    expect(resolveFileLink("/srv/workspaces/shop/app.py", "/tmp", workspace)).toBe("app.py");
    expect(resolveFileLink("/srv/workspaces/shop/./a/../b.py", "/tmp", workspace + "/")).toBe("b.py");
  });

  it("refuses anything outside the workspace, which the preview could not open", () => {
    expect(resolveFileLink("/etc/passwd", workspace, workspace)).toBeNull();
    expect(resolveFileLink("../../etc/passwd", workspace, workspace)).toBeNull();
    expect(resolveFileLink("/srv/workspaces/shopping/x.py", "/", workspace)).toBeNull();
    expect(resolveFileLink("~/notes.md", workspace, workspace)).toBeNull();
    expect(resolveFileLink("x.py", workspace, "/")).toBeNull();
    expect(resolveFileLink("../../../..", "/a", workspace)).toBeNull();
  });
});

describe("rewriteLoopbackUrl", () => {
  const env = { port_range: "8120-8139", public_host: "shop-server.lan" };

  it("points a loopback address on a published port at the published host", () => {
    expect(rewriteLoopbackUrl("http://localhost:8123/", env)).toBe("http://shop-server.lan:8123/");
    expect(rewriteLoopbackUrl("http://127.0.0.1:8120/api?x=1", env)).toBe("http://shop-server.lan:8120/api?x=1");
    expect(rewriteLoopbackUrl("http://0.0.0.0:8139", env)).toBe("http://shop-server.lan:8139/");
    expect(rewriteLoopbackUrl("http://[::1]:8125/", env)).toBe("http://shop-server.lan:8125/");
  });

  it("leaves every other address as it is", () => {
    expect(rewriteLoopbackUrl("http://localhost:5173/", env)).toBe("http://localhost:5173/");
    expect(rewriteLoopbackUrl("http://localhost/", env)).toBe("http://localhost/");
    expect(rewriteLoopbackUrl("https://example.com:8123/", env)).toBe("https://example.com:8123/");
    expect(rewriteLoopbackUrl("ftp://localhost:8123/", env)).toBe("ftp://localhost:8123/");
    expect(rewriteLoopbackUrl("not a url", env)).toBe("not a url");
    expect(rewriteLoopbackUrl("http://localhost:8123/", { port_range: "8120-8139", public_host: "" })).toBe("http://localhost:8123/");
    expect(rewriteLoopbackUrl("http://localhost:8123/", { port_range: "junk", public_host: "h" })).toBe("http://localhost:8123/");
  });

  it("reads a port range", () => {
    expect(parsePortRange("8120-8139")).toEqual([8120, 8139]);
    expect(parsePortRange(" 1 - 2 ")).toEqual([1, 2]);
    expect(parsePortRange("9-1")).toBeNull();
    expect(parsePortRange("")).toBeNull();
  });
});
