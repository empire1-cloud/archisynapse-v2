# Archisynapse Agent Service Contracts and FABLE-5 Receipts

## Status

This is the enforceable SLA layer above the existing Agent Commerce Rail.

The rail already controls scoped spend, invoice binding, settlement evidence,
signed payment receipts, crash recovery, AI-token spend, and evidence-backed
reputation. Service contracts now bind each production agent call to the work
that was purchased and the evidence required to accept it.

## Product boundary

- **SLA113** manufactures and runs reusable engines.
- **Archisynapse** owns agent-service contracts, authorized spend, settlement,
  disputes, financial receipts, and verified commercial reputation.
- **FABLE-5 receipts** preserve the authority and evidence chain.
- Empire products consume accepted results without owning the shared engines.

Lyrica 3 is a tenant. It does not own the Engines Factory or the agent economy.

## Contracted execution chain

```text
service contract
  -> FABLE service-contract receipt
  -> scoped Archisynapse spend authorization
  -> FABLE execution-authorization receipt
  -> L402 invoice and payment
  -> signed Archisynapse payment receipt
  -> SLA delivery evaluation
  -> FABLE delivery-verdict receipt
  -> independent validator outcome
  -> validated SLA decision
  -> verified reputation update
  -> FABLE final-SLA receipt
```

The contract, payment, delivery verdict, and validator outcome are bound before
reputation is allowed to change. A provider cannot independently validate its
own delivery unless a contract explicitly permits that weaker boundary.

The legacy `POST /v1/agent-calls` endpoint remains available for compatibility.
It explicitly reports `service_contract_enforced: false`.

New production integrations use:

```text
POST /v1/contracted-agent-calls
```

## First service templates

### Empire Search Intelligence Agent

Service ID: `seo.search-intelligence`

Required output:

- search opportunities;
- search-intent map;
- page-gap analysis;
- prioritized actions;
- source URLs;
- analysis timestamp;
- protected tenant scope.

### Empire Reliability Agent

Service ID: `reliability.workflow-verification`

Required output:

- health verdict;
- failed contracts;
- business impact;
- recommended action;
- irreversible-action status;
- probe evidence;
- checked time;
- deployed version.

### Social Media Analysis Agent

Service ID: `social.performance-analysis`

Required output:

- performance summary;
- winning and failed patterns;
- conversion findings;
- next actions;
- source accounts;
- data window;
- metric definitions.

Templates do not invent a provider, endpoint, price, or tenant. An operator must
create a concrete service contract with the actual SLA113 engine identity.

## SLA parameters

A service contract binds:

- tenant;
- service and version;
- provider agent identity;
- specialty;
- exact endpoint;
- settlement policy;
- maximum price;
- response and delivery deadlines;
- availability target;
- minimum independently verified quality;
- maximum response size;
- retry allowance;
- required deliverables;
- required evidence;
- validator requirement;
- self-verification boundary;
- failed-delivery refund policy;
- expiration and version.

The current L402 execution adapter enforces these supported runtime limits:

- initial challenge timeout: 10 seconds;
- paid-delivery timeout: 90 seconds;
- maximum captured response: 1,000,000 bytes.

A contract cannot claim a deadline below the adapter's current timeout floor or
a response ceiling above what the adapter can safely capture. This prevents the
stored SLA from promising behavior the runtime does not enforce.

`prepaid_l402` is the currently executable paid adapter. `no_spend` and
`after_verified_delivery` are valid contract policies but fail closed on the
paid-call endpoint until their dedicated execution adapters exist. The system
never silently changes the client's settlement choice.

## Endpoint boundary

Service-contract endpoints use the same Archisynapse identity validation as
signed agent profiles:

- the endpoint must be an absolute HTTP or HTTPS URL;
- loopback, private, link-local, reserved, and metadata endpoints are blocked;
- embedded credentials are blocked;
- production requires HTTPS.

The exact normalized endpoint is then bound to the contract, authorization,
payment receipt, and FABLE delivery verdict.

## Acceptance behavior

Technical delivery is rejected when:

- the provider, specialty, endpoint, tenant, or price differs from the contract;
- delivery fails;
- the deadline or response-size ceiling is exceeded;
- the response is not valid UTF-8 JSON;
- required deliverables are missing;
- required evidence is missing.

A technically complete delivery remains
`pending_independent_validation` when the contract requires a separate
validator. The provider cannot validate its own work unless the contract
explicitly permits it.

Before reputation changes, Archisynapse verifies that:

- the payment receipt tenant matches the contract tenant;
- the paid provider, specialty, and endpoint match the contract;
- the FABLE delivery receipt names the same service contract;
- the FABLE delivery receipt binds the same payment receipt and receipt hash;
- the independent validator meets the contract's identity and quality rules.

A final SLA verdict compares the validator's success decision and quality score
against the contract. Rejected prepaid deliveries recommend the governed
refund/dispute path; they do not pretend the original payment never happened.

## API

```text
GET  /v1/service-templates
GET  /v1/service-templates/{template_id}
POST /v1/service-contracts
GET  /v1/service-contracts?tenant_id=...
GET  /v1/service-contracts/{contract_id}

POST /v1/contracted-agent-calls

GET  /v1/fable-receipts/{receipt_id}
POST /v1/receipts/{receipt_id}/outcomes
```

Supplying `service_contract_id` when recording an independent outcome produces
the final FABLE SLA receipt in addition to the existing verified reputation
update.

## Safety rules

- No service contract creates authority to spend by itself.
- No paid call can exceed the separate Archisynapse authorization.
- Contract and authorization tenant scopes must match.
- Exact provider identity and endpoint are enforced.
- Queries and context are hashed in the FABLE authority receipt rather than
  copied into it.
- Payment receipts and FABLE receipts are separate, signed records.
- Rejected delivery does not erase real settlement evidence.
- Validation happens before reputation is changed.
- Reputation changes only after an independently evidenced outcome.
- Every contract and FABLE receipt transition enters the append-only audit chain.
