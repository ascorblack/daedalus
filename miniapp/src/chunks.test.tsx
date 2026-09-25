// @vitest-environment jsdom
// A chunk fetched ahead renders in the render that asks for it, without its fallback first.

import { Suspense, act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { chunk, retried } from "./chunks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Page({ word }: { word: string }) {
  return <b>{word}</b>;
}

/** What the first commit of a render shows, before any promise has had a tick to answer. */
function firstCommit(Screen: ReturnType<typeof chunk<typeof Page>>): string {
  const box = document.createElement("div");
  const root = createRoot(box);
  act(() => root.render(<Suspense fallback={<i>loading</i>}><Screen word="ready" /></Suspense>));
  const shown = box.textContent ?? "";
  act(() => root.unmount());
  return shown;
}

describe("chunk", () => {
  it("draws the fallback when the click has to fetch the module", () => {
    const Screen = chunk(() => Promise.resolve({ default: Page }));
    expect(firstCommit(Screen)).toBe("loading");
  });

  it("draws the screen at once once the module was fetched ahead", async () => {
    const Screen = chunk(() => Promise.resolve({ default: Page }));
    Screen.prefetch();
    await Promise.resolve();
    await Promise.resolve();
    expect(firstCommit(Screen)).toBe("ready");
  });

  it("fetches again after a prefetch that failed", async () => {
    let calls = 0;
    const Screen = chunk(() => (++calls === 1 ? Promise.reject(new Error("offline")) : Promise.resolve({ default: Page })));
    Screen.prefetch();
    await new Promise((r) => setTimeout(r, 0));
    Screen.prefetch();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toBe(2);
    expect(firstCommit(Screen)).toBe("ready");
  });

  it("retries a loader before giving up", async () => {
    let calls = 0;
    const load = retried(() => (++calls < 2 ? Promise.reject(new Error("flaky")) : Promise.resolve("module")));
    expect(await load()).toBe("module");
    expect(calls).toBe(2);
  });
});
