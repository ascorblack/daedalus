// A site's icon in a tab chip, the preview's footer and the phone's bar.
//
// Only an icon the host hands over inline (`data:`) is drawn. An icon's own address is on the site the
// agent visited, and fetching it from here would have the operator's device — its address, its
// language, its cookies' absence — call a site that only the agent's walled browser was meant to
// reach. Anything else, or an icon that does not decode, is the first letter of the host.

import { useEffect, useState } from "react";
import { domainOf } from "./model";

export function Favicon({ url, page, size = 14 }: { url: string; page: string; size?: number }) {
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [url]);
  const inline = /^data:image\//i.test(url);
  if (!inline || broken) {
    const letter = (domainOf(page).replace(/^[^a-z0-9а-я]+/i, "")[0] ?? "·").toUpperCase();
    return <span className="bp-favicon letter" style={{ width: size, height: size, fontSize: Math.round(size * 0.62) }} aria-hidden="true">{letter}</span>;
  }
  return <img className="bp-favicon" src={url} width={size} height={size} alt="" onError={() => setBroken(true)} />;
}
