# Ontology, evidence model and entity resolution

Source of truth: `backend/sextant/ontology.py` (types), `model.py` (graph), `resolution/scorer.py` (resolution).
The tables below are generated from the code.

## 1. Entity types

Entities are deduplicated by **type + normalized value** (`email:jane@example.org`, `account:github:octocat`,
`domain:example.co.uk`). There are three families:

* **Selectors** (name, email, phone, username, domain, ip, asn) are values. Two sources reporting the same value
  produce one entity.
* **Objects** (account, website, document, publication, image, public_record, breach, location, organization)
  are things found in sources.
* **Identities** (person) are never keyed by a name. A person is either the investigation's target
  (`person:target`) or the person described by one specific record (`person:<registry>:<record id>`). Two
  "Jane Doe" records therefore create two persons, each linked to the *name string* "jane doe" by `HAS_NAME`.

*Resolvable* types get an identity confidence. *Expandable* types can be the input of transforms.

| Type | Family | Description | Resolvable | Expandable | Key properties |
|---|---|---|---|---|---|
| `person` | identity | A human being. Never deduplicated by name: either the target of the investigation or the person described by one specific record. | yes | yes | `full_name`, `aliases`, `birth_year` (local-only), `birth_date`, `nationality`, `occupation`, `employer`, `location`, `role`… |
| `organization` | object | A company, institution or group. Keyed by registry identifier when known, otherwise by normalized name + jurisdiction. | yes | yes | `name`, `legal_form`, `jurisdiction`, `registry`, `registration_number`, `lei`, `status`, `incorporated`, `dissolved`… |
| `name` | selector | A personal-name string as written. Equal names are not equal people. |  | yes | `variants` |
| `email` | selector | An email address (lower-cased; provider canonical form kept as a property). |  | yes | `provider`, `free_provider`, `provider_canonical`, `mx`, `public_commits` |
| `phone` | selector | A phone number in E.164 form. |  | yes | `international`, `national`, `region`, `region_name`, `carrier`, `number_type`, `valid`, `timezones` |
| `username` | selector | A handle as used on online platforms (case-insensitive key). |  | yes | `username`, `uniqueness`, `derived_from`, `hypothesis` |
| `domain` | selector | A DNS name (registered domain or subdomain). |  | yes | `registrar`, `registered`, `expires`, `nameservers`, `dns`, `registrant`, `site_title`, `technologies`, `subdomain`… |
| `ip` | selector | An IPv4/IPv6 address. |  | yes | `asn`, `network`, `org`, `country`, `ports`, `hostnames`, `cpes`, `tags` |
| `asn` | selector | An autonomous system (network operator). |  | yes | `name`, `country`, `registry` |
| `account` | object | An account on a platform (social network, forum, code host, game…). Existing does not mean it belongs to the target. | yes | yes | `platform`, `platform_title`, `category`, `username`, `user_id`, `display_name`, `bio`, `location`, `website`… |
| `website` | object | A web page or site URL: search results, linked pages, news articles, archived snapshots. | yes | yes | `url`, `title`, `snippet`, `host`, `category`, `published`, `matched`, `queries`, `language` |
| `document` | object | A public document (PDF, office file, filing). Never downloaded automatically. | yes | yes | `url`, `title`, `file_type`, `snippet`, `host`, `published`, `matched` |
| `publication` | object | A scholarly, official or press publication (DOI, ORCID work, filing). | yes | yes | `title`, `doi`, `year`, `venue`, `authors`, `url`, `kind` |
| `image` | object | An image identified by its perceptual hash (a profile photo or a photo supplied by the investigator). |  | yes | `phash`, `dhash`, `thumb`, `example_url`, `flat` |
| `public_record` | object | An entry in an official public source: court case, judgment, sanctions or wanted list, company filing, procurement award, gazette notice, licence. A name on a record is not an identity. | yes | yes | `record_type`, `jurisdiction`, `authority`, `official_url`, `reference`, `date`, `title`, `category`, `matched_name`… |
| `breach` | object | A data breach or info-stealer exposure (metadata only — never credentials). |  | yes | `title`, `breach_date`, `data_classes`, `records`, `verified`, `domain`, `description` |
| `location` | object | A place as written in a source, optionally geocoded. |  | yes | `text`, `city`, `region`, `country`, `country_code`, `lat`, `lon`, `kind` |
| `source` | meta | Where an observation came from (shown in the graph only on request). |  |  | `connector`, `url`, `retrieved_at` |

Normalizers (examples): `John@Example.COM → john@example.com` · `+358 40 123 4567 → +358401234567` ·
`https://www.Example.com/a/?utm_source=x#f → https://example.com/a` · `SMITH, John → john smith` ·
`https://doi.org/10.1000/XYZ → doi:10.1000/xyz` · `as15169 → AS15169`. A value that fails validation is
rejected and counted in the run's `rejected` list. It is never stored "as is".

## 2. Relationship types

| Relationship | Meaning | From → to |
|---|---|---|
| `USES` | An identity or account uses an identifier (email, phone, username, domain). | person, account, organization → email, phone, username, domain |
| `OWNS` | Ownership/control: an identity owns an account, domain or asset. Only asserted from explicit evidence or an investigator's confirmation. | person, organization → account, domain, website, phone, email |
| `AUTHORED` | Authorship of a document, publication, post or commit. | person, account, organization → document, publication, website |
| `MENTIONED_IN` | An identifier or name appears in a page or document. | any → website, document, publication |
| `LINKED_TO` | A hyperlink, rel=me link or profile field pointing at another account, page or domain. | account, website, domain → account, website, domain |
| `LOCATED_IN` | Stated or registered location. | any → location |
| `WORKS_FOR` | Employment or professional affiliation as stated in a source. | person, account → organization |
| `MEMBER_OF` | Officer, director, partner, board or group membership (the role is recorded on the relationship). | person → organization |
| `APPEARS_ON` | A name, identifier or image appears on a record, list or page. | name, username, email, image, person → public_record, website, breach |
| `ASSOCIATED_WITH` | A weaker association reported by a source (co-occurrence, related entity). | any → any |
| `REGISTERED_TO` | Registration data: a domain or network registered to an organization, person, email or AS. | domain, ip, asn → organization, person, email, asn |
| `REFERENCED_BY` | Referenced by an archive snapshot, citation or index. | any → any |
| `RESOLVES_TO` | DNS resolution. | domain → ip |
| `HAS_SUBDOMAIN` | DNS hierarchy. | domain → domain |
| `EXPOSED_IN` | An identifier is included in a breach or info-stealer dataset (metadata only). | email, username, domain, phone → breach |
| `HAS_IMAGE` | Profile photo or image shown by an account or page. | account, website, person → image |
| `HAS_NAME` | A person, account or record carries this name string. | person, account, public_record, organization → name |
| `SAME_AS` | Candidate equivalence produced by entity resolution, with confidence and evidence. Never merges automatically. | any → any |
| `ANNOUNCED_IN` | Official notice concerning an organization or person (gazette, procurement award, filing). | organization, person → public_record, publication |

Relationships are directed and deduplicated by (source, type, target). Each carries an **evidence list**: one
item per source that reported it (source id, description, transform run, confidence, origin). A relationship's
confidence is the strongest evidence item. Rejecting a relationship keeps it, marked `rejected`, so the
decision stays auditable and the same transform cannot silently re-add it.

## 3. Evidence model

```
Source        id, connector, kind (api|web_page|dataset|tool|local|user), url, title, retrieved_at,
              request (what was sent), reliability, license
Observation   id, entity, prop, value, source, run, confidence, evidence (text), observed_at
Entity        props = aggregate(observations) → {prop: [{v, k, obs: [observation ids], conf}]}
Relationship  source, target, type, evidence: [RelEvidence(source, description, run, confidence, origin)]
Check         per-platform outcome of a sweep (found / not_found / blocked / skipped / unknown) with the URL
TransformRun  transform, input entity, trigger (auto|user), status, message, timings, requests, counts
```

Rules:

1. **Every fact is an observation with a source.** `TransformResult.entity()/prop()/link()` require a `source`
   created by `out.source(url, ...)`. The result builder raises an error for a fact without a source or with
   an unknown source id, so a connector cannot emit an unsourced fact.
2. **Every entity has a sighting** (`prop "@"`): "where was this seen?" is answerable for every node.
3. **Properties are derived.** The same value from two sources is one property value with two observations
   (and so two citations). Conflicting values are all kept and shown, never overwritten.
4. **Merges are reversible.** `Graph.merge(a, b)` re-points observations and relationships to `a` and stores a
   snapshot in `merge_log`. `Graph.separate(b)` restores `b`, its observations and its relationships exactly.
5. **Local-only properties** (the target's birth year) are stripped from what transforms see (`EntityView`)
   and from every export. They are used only for local comparison.

## 4. Entity resolution

For each scored entity (accounts, websites, documents, publications, public records, persons, organizations):

```
confidence = sigmoid(prior[type] + Σ weight(reason))
```

The target profile is built from the input plus **anchors**: confirmed entities, and accounts at ≥ 85% with a
high tier backed by an *independent* strong feature. Mutual corroboration alone cannot create an anchor.

| Evidence (examples) | Weight |
|---|---|
| Same email / phone on the profile | +4.0 / +3.5 |
| Official registration id matches | +5.0 |
| Verified link to an anchor (rel=me, cryptographic proof) / plain link | +3.0 / +2.5 |
| Same profile photo as one the investigator supplied / as an anchor | +3.0 / +2.0 |
| Same birth year (local comparison) / **different** birth year | +2.0 / **−3.0** |
| Same username | +0.3 + 1.7 × estimated uniqueness ("john" ≈ 0 → `username_common`) |
| Full name / contained / fuzzy / surname only / first name only | +1.5 / +1.2 / +0.8 / +0.5 / +0.2 |
| **Different name** shown on the profile | **−1.2** |
| Same city / country / **different** location | +0.8 / +0.3 / **−0.6** |
| Cross-linked account with the same handle (verified) | +1.2 (+2.0) |
| Public record: exact name / name contained | +0.8 / +0.4 |
| Priors | account, website, document −1.5 · publication, public_record, person −2.0 · organization −1.0 |

Worked example (the namesake rule):

| Public record "JANE DOE" on a sanctions list | Calculation | Confidence | Label |
|---|---|---|---|
| Name only | σ(−2.0 + 0.8) | **23%** | Possible name match |
| Name + same birth year | σ(−2.0 + 0.8 + 2.0) | 69% | Possible name match |
| Name + different birth year | σ(−2.0 + 0.8 − 3.0) | 1.5% | Possible name match (contradiction shown) |
| Investigator confirms | — | 100% | Confirmed identity match |

Records **never** link to the target person. They link to name strings (`HAS_NAME`), and the name string links
to the target. Only an investigator's CONFIRM creates an identity link. Record labels are *Public record
found / Possible name match / Confirmed identity match / Unable to verify*.

**Decisions.** CONFIRM pins the entity at 100% (status `confirmed`), adds an `OWNS`/`USES` edge from the target
with the user as source, and makes it an anchor, so its identifiers and links re-score the rest of the graph.
SEPARATE pins it at 0% and rejects that edge. IGNORE hides it. RESET returns it to scoring. Every decision is
logged with time and note.

**Same-owner candidates.** `link_same_owner` adds `SAME_AS` edges between two accounts only when at least two
independent signals agree (e.g. the same unique handle and the same display name). They are suggestions, never
automatic merges.

**Investigative value** (what to expand next) is separate from identity confidence. It is described in
ARCHITECTURE.md §2.
