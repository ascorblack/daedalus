// The main chat: the conversation with the main orchestrator, with the dispatches under way in its
// flow, after the latest turn, and every project's questions in its panel's Questions tab. Opening it
// the first time makes its session; after that it is the same session every time, from any device,
// until the operator replaces it.

import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { ORCHESTRATION_LIST, navigate } from "../router";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { MainFlow } from "./cards";
import { MAIN_KEY, useMain } from "./data";

const SessionScreen = lazy(() => import("../screens/Session").then((m) => ({ default: m.SessionScreen })));

export function MainScreen({ toast }: { toast: (text: string) => void }) {
  const { data, error } = useMain();
  const [opening, setOpening] = useState<string | null>(null);
  const sessionId = data?.session_id || opening || "";
  useEffect(() => {
    if (!data || data.session_id || opening !== null) return;
    setOpening("");
    api
      .post<{ session_id: string }>(MAIN_KEY, {})
      .then((made) => {
        setOpening(made.session_id);
        invalidate(MAIN_KEY);
      })
      .catch((e) => {
        toast(errorText(e));
        setOpening(null);
      });
  }, [data, opening, toast]);
  if (error && !data) return <div className="empty"><b>{t("main.unavailable")}</b><div>{error}</div></div>;
  if (!sessionId || !data) return <div className="empty">{t("common.loading")}</div>;
  return (
    <Suspense fallback={<div className="empty">{t("common.loading")}</div>}>
      <SessionScreen
        key={sessionId}
        id={sessionId}
        // On a phone the main chat is a detail of orchestration's list, Main being that list's first row.
        onBack={() => navigate(ORCHESTRATION_LIST)}
        toast={toast}
        flow={<MainFlow view={data} toast={toast} />}
        placeholder={t("main.composer")}
        main
      />
    </Suspense>
  );
}
