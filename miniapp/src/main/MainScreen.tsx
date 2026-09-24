// The main chat: the conversation with the main orchestrator, with the questions and the dispatches
// above it. Opening it the first time makes its session; after that it is the same session every
// time, from any device, until the operator replaces it.

import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { navigate, pathFor } from "../router";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { MainBoard } from "./cards";
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
        onBack={() => navigate(pathFor("agents"))}
        toast={toast}
        banner={<MainBoard view={data} toast={toast} />}
        placeholder={t("main.composer")}
      />
    </Suspense>
  );
}
