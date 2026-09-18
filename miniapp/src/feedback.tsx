import { t } from "./i18n";

export function FileSkeleton({ rows = 8 }: { rows?: number }) {
  return <div className="file-skeleton" aria-label={t("common.loading")} aria-busy="true">{Array.from({ length: rows }, (_, i) => <div key={i}><span className="skeleton" style={{ width: `${55 + i % 3 * 15}%` }} /></div>)}</div>;
}
