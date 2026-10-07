import { api } from "./api";
import { OntologyProvider, useApi, useRoute } from "./ui";
import Home from "./views/Home";
import History from "./views/History";
import Workspace from "./views/Workspace";
import Sources from "./views/Sources";
import Matrix from "./views/Matrix";
import Platforms from "./views/Platforms";
import Jurisdictions from "./views/Jurisdictions";
import GlobalCoverage from "./views/GlobalCoverage";
import CountryDetail from "./views/CountryDetail";
import Settings from "./views/Settings";

interface Meta {
  version: string;
  data_dir: string;
  encryption: string;
  privacy: string;
  connectors: { total: number; available: number };
  platforms: number;
  platforms_curated: number;
  countries: number;
  jurisdictions: number;
}

const NAV: [string, string, string][] = [
  ["", "✦", "New investigation"],
  ["investigations", "☰", "Investigations"],
];
const CATALOG: [string, string, string][] = [
  ["coverage", "🌍", "Global coverage"],
  ["sources", "⛁", "Data sources"],
  ["matrix", "▦", "Coverage matrix"],
  ["platforms", "◎", "Platforms"],
  ["jurisdictions", "⚖", "Jurisdictions"],
  ["settings", "⚙", "Settings"],
];

export default function App() {
  const route = useRoute();
  const meta = useApi<Meta>(api.hasToken() ? "/api/meta" : null);
  if (!api.hasToken()) {
    return (
      <div className="page">
        <h1>Session key missing</h1>
        <p className="sub">
          Open Sextant with the link printed in the terminal (it contains <span className="mono">#token=…</span>). The key
          protects the local engine from other websites.
        </p>
      </div>
    );
  }
  const top = route[0] ?? "";
  const view = (() => {
    switch (top) {
      case "investigations":
        return <History />;
      case "inv":
        return <Workspace key={route[1]} id={route[1]} tab={route[2] ?? "assistant"} entity={route[3]} />;
      case "sources":
        return <Sources focus={route[1]} />;
      case "matrix":
        return <Matrix />;
      case "platforms":
        return <Platforms />;
      case "coverage":
        return route[1] ? <CountryDetail key={route[1]} code={route[1]} /> : <GlobalCoverage />;
      case "jurisdictions":
        return <Jurisdictions />;
      case "settings":
        return <Settings />;
      default:
        return <Home />;
    }
  })();
  const link = ([path, ico, label]: [string, string, string]) => (
    <a key={path} href={`#/${path}`} className={top === path || (path === "investigations" && top === "inv") ? "active" : ""}>
      <span className="ico">{ico}</span>
      {label}
    </a>
  );
  return (
    <OntologyProvider>
      <div className="shell">
        <nav className="nav">
          <div className="brand">
            <div className="brand-mark">
              <span />
            </div>
            <div>
              <b>SEXTANT</b>
              <small>OSINT investigation platform</small>
            </div>
          </div>
          {NAV.map(link)}
          <div className="section">Catalog</div>
          {CATALOG.map(link)}
          <div className="foot">
            {meta.data && (
              <>
                v{meta.data.version} · {meta.data.connectors.available}/{meta.data.connectors.total} sources ready
                <br />
                {meta.data.countries} countries · {meta.data.platforms.toLocaleString()} platforms registered
                <br />
                Local-first · data encrypted ({meta.data.encryption})
              </>
            )}
          </div>
        </nav>
        <main className="main">{view}</main>
      </div>
    </OntologyProvider>
  );
}
