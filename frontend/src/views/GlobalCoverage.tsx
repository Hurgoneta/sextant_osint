import { useMemo, useState } from "react";
import type { CoverageSummary, CountryRow } from "../types";
import { Badge, ErrorBox, Spinner, navigate, useApi } from "../ui";

interface Payload {
  rows: CountryRow[];
  levels: Record<string, string>;
  groups: Record<string, string>;
  regions: string[];
  summary: CoverageSummary;
  provenance: { countries: { retrieved_at: string; note: string; source_url: string[] };
    datasets: Record<string, { title: string; retrieved_at: string; items: number; license: string }> };
}

export const LEVEL_KIND: Record<string, string> = { connectors: "good", links: "info", discovered: "warn", none: "bad" };

export function CoveragePanel({ s }: { s: CoverageSummary }) {
  const c = s.countries;
  const p = s.platforms;
  const pub = s.public_databases;
  return (
    <div className="grid3 cov-panel">
      <div className="card pad">
        <div className="cov-h">🌍 Countries</div>
        <b className="cov-n">{c.total}</b><span className="muted"> sovereign states</span>
        <div className="cov-list">
          <span><Badge kind="good">{c.with_country_connectors}</Badge> with country connectors</span>
          <span><Badge kind="info">{c.links_only}</Badge> official sources, links only</span>
          <span><Badge kind="warn">{c.discovered_only}</Badge> potential sources only (datasets)</span>
          <span><Badge kind="bad">{c.none}</Badge> no connector currently available</span>
          <span className="faint small">+ {c.territories} territories listed separately</span>
        </div>
      </div>
      <div className="card pad">
        <div className="cov-h">🌐 Platforms</div>
        <b className="cov-n">{p.registered.toLocaleString()}</b><span className="muted"> registered</span>
        <div className="cov-list">
          <span><Badge kind="good">{p.searchable.toLocaleString()}</Badge> searchable on this installation</span>
          {(p.by_status.search_everything_only ?? 0) > 0 && <span><Badge kind="info">{(p.by_status.search_everything_only ?? 0).toLocaleString()}</Badge> more with SEARCH EVERYTHING (Maigret beyond its top sites)</span>}
          {(p.by_status.adapter_off ?? 0) > 0 && <span><Badge kind="info">{(p.by_status.adapter_off ?? 0).toLocaleString()}</Badge> via an installed adapter that is switched off</span>}
          {(p.by_status.adult_off ?? 0) > 0 && <span><Badge>{(p.by_status.adult_off ?? 0).toLocaleString()}</Badge> adult/dating sites (off in Settings)</span>}
          <span><Badge kind="warn">{p.api_only}</Badge> need an API key / subscription</span>
          <span><Badge>{p.unavailable.toLocaleString()}</Badge> not searchable here (manual, no rule, excluded, private)</span>
          <span className="faint small">{p.curated} curated with capability data · {p.imported.toLocaleString()} from community datasets</span>
        </div>
      </div>
      <div className="card pad">
        <div className="cov-h">📚 Data sources</div>
        <b className="cov-n">{s.connectors.total}</b><span className="muted"> connectors · {pub.official_sources} official links</span>
        <div className="cov-list">
          <span><Badge kind="good">{s.connectors.by_status.available ?? 0}</Badge> connectors ready</span>
          <span><Badge kind="warn">{s.connectors.by_status.needs_credentials ?? 0}</Badge> API key required</span>
          <span><Badge kind="violet">{(s.connectors.by_status.needs_subscription ?? 0)}</Badge> subscription required · {s.commercial.configured} commercial configured</span>
          <span><Badge>{pub.discovered.toLocaleString()}</Badge> potential sources from datasets</span>
          <span className="faint small">{pub.automated} official sources automated · {pub.links_only} link-only ·
            {" "}{s.commercial.providers_listed ?? 0} commercial providers listed for {s.commercial.countries_with_providers ?? 0} countries</span>
        </div>
      </div>
    </div>
  );
}

export default function GlobalCoverage() {
  const { data, error } = useApi<Payload>("/api/global-coverage");
  const [q, setQ] = useState("");
  const [region, setRegion] = useState("");
  const [level, setLevel] = useState("");
  const [territories, setTerritories] = useState(false);
  const [sort, setSort] = useState<"name" | "sources" | "connectors" | "platforms">("name");
  const rows = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const out = (data?.rows ?? []).filter((r) => (territories || r.sovereign) && (!region || r.region === region) &&
      (!level || r.level === level) && (!ql || r.name.toLowerCase().includes(ql) || r.code.toLowerCase() === ql));
    const key: Record<string, (r: CountryRow) => number> = { sources: (r) => -r.sources, connectors: (r) => -r.connectors,
      platforms: (r) => -r.regional_platforms };
    return sort === "name" ? out : [...out].sort((a, b) => key[sort](a) - key[sort](b) || a.name.localeCompare(b.name));
  }, [data, q, region, level, territories, sort]);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const s = data.summary;
  return (
    <div className="page wide">
      <h1>Global coverage</h1>
      <p className="sub">
        Every one of the {s.countries.total} sovereign states (193 UN members, the Holy See and the State of Palestine) is evaluated
        individually. These counts come from the registries and this installation's configuration. They are never estimates:
        a country with no known source is listed as such, with the reason.
      </p>
      <CoveragePanel s={s} />
      <div className="card" style={{ marginTop: 14 }}>
        <table className="data">
          <thead><tr><th>Region</th><th className="c">Countries</th><th className="c">Country connectors</th><th className="c">Links only</th>
            <th className="c">Potential only</th><th className="c">None</th></tr></thead>
          <tbody>
            {s.countries.by_region.map((g) => (
              <tr key={g.region} className="clickable" onClick={() => setRegion(region === g.region ? "" : g.region)}>
                <td><b>{g.region}</b></td><td className="c">{g.countries}</td><td className="c">{g.connectors}</td>
                <td className="c">{g.links}</td><td className="c">{g.discovered}</td><td className="c">{g.none}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row wrap" style={{ margin: "16px 0 10px" }}>
        <input type="search" placeholder="Country or ISO code…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 220 }} />
        <select value={region} onChange={(e) => setRegion(e.target.value)} style={{ maxWidth: 230 }}>
          <option value="">All regions</option>
          {data.regions.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select value={level} onChange={(e) => setLevel(e.target.value)} style={{ maxWidth: 280 }}>
          <option value="">All coverage levels</option>
          {Object.entries(data.levels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)} style={{ maxWidth: 200 }}>
          <option value="name">Sort by name</option><option value="sources">Most sources</option>
          <option value="connectors">Most connectors</option><option value="platforms">Most regional platforms</option>
        </select>
        <label className="check small"><input type="checkbox" checked={territories} onChange={(e) => setTerritories(e.target.checked)} />Include territories</label>
        <span className="small muted">{rows.length} shown</span>
      </div>
      <div className="card">
        <table className="data">
          <thead>
            <tr>
              <th>Country</th><th>Coverage</th><th className="c" title="Connectors targeting this country">Connectors</th>
              <th className="c" title="Curated official sources (links)">Official</th>
              <th className="c" title="Potential sources from maintained datasets (not yet reviewed)">Potential</th>
              <th className="c" title="Corporate, judicial, regulatory, procurement and other record sources">Records</th>
              <th className="c" title="Platforms specific to this country (global platforms apply everywhere)">Regional platforms</th>
              <th className="c" title="Global connectors with data filtered to this country">Global w/ country data</th>
              <th>Domains / IP</th><th>Last checked</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code} className="clickable" onClick={() => navigate(`/coverage/${r.code}`)}>
                <td><b>{r.name}</b> <span className="faint small mono">{r.code}</span>
                  <div className="small faint">{r.region}{r.subregion ? ` · ${r.subregion}` : ""}{!r.sovereign ? " · territory" : ""}</div></td>
                <td><Badge kind={LEVEL_KIND[r.level]}>{r.level_title}</Badge>{r.reason && <div className="small faint">{r.reason}</div>}</td>
                <td className="c">{r.connectors ? <>{r.connectors_available}<span className="faint">/{r.connectors}</span></> : <span className="faint">0</span>}</td>
                <td className="c" title={`${r.regional_sources} regional portal(s) also include this country`}>
                  {r.curated_sources + r.custom_sources || <span className="faint">0</span>}{r.regional_sources > 0 && <span className="faint small"> +{r.regional_sources}r</span>}</td>
                <td className="c">{r.discovered_sources || <span className="faint">0</span>}</td>
                <td className="c">{r.records || <span className="faint">0</span>}</td>
                <td className="c">{r.regional_platforms ? <>{r.regional_platforms_searchable}<span className="faint">/{r.regional_platforms}</span></> : <span className="faint">0</span>}</td>
                <td className="c">{r.global_capabilities}</td>
                <td className="small">{r.dns?.tld || "—"} {r.dns?.rdap ? <Badge kind="good">RDAP</Badge> : <span className="faint">no RDAP</span>}
                  <div className="faint">{r.rir || ""}</div></td>
                <td className="small muted">{r.last_checked ? r.last_checked.slice(0, 10) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small faint" style={{ marginTop: 12 }}>
        Connectors column: ready / total country connectors. Regional platforms: searchable / registered. Country list:
        {" "}{data.provenance.countries.note} Datasets:{" "}
        {Object.values(data.provenance.datasets).map((d) => `${d.title} (${d.items.toLocaleString()} items, ${String(d.retrieved_at).slice(0, 10)})`).join(" · ")}.
        {" "}{s.disclaimer}
      </p>
    </div>
  );
}
