# Phase 9C-A: Authoritative Lease Takeover, Heartbeat Gating, and Non-Regressing Catalog Projection

> [!IMPORTANT]
> **Audit Baseline:** Commit `f5768eb4ef73c01f7443d80dcd211cf6ecd2ddb4` on branch `phase6b-incremental-crawling`.
> This specification codifies two critical distributed invariants for CorpusAI Phase 9C-A:
> 1. Lease takeover vs. publication races & heartbeat failure abort gates.
> 2. Idempotent catalog projection and monotonic non-regression guarantees.

---

## 1. Lease Takeover Races & Heartbeat Abort Gate

### Principle 1: Expiry Enables Takeover, But Does Not Revoke
In distributed object stores without wall-clock revocation hooks:
- Reaching or exceeding `expires_at` renders a lease record **eligible for takeover**.
- Expiry alone **does not automatically revoke** the current owner in storage until a competing runner successfully executes an atomic Compare-And-Swap (CAS) update to `state/{portal_id}/portal_control.json`.

### Principle 2: The Atomic Publication vs. Takeover Race
If Owner A (whose lease has nominally passed `expires_at` due to a delay) attempts to commit its baseline at the exact moment Runner B attempts a lease takeover:
- Both runners issue an HTTP conditional write (`If-Match: "<downloaded_etag>"`) against the **same object**: `state/{portal_id}/portal_control.json`.
- **The object store serializes these two requests:**
  - **Outcome A (Owner A wins):** Owner A's baseline commit updates `portal_control.json`, advances the active baseline pointer to Owner A's run, bumps the control record version, and generates a new ETag. Runner B's takeover then receives HTTP 412 (`Precondition Failed`). Runner B must re-read the control record, observe that Owner A successfully committed its baseline, and schedule its crawl based on the new baseline.
  - **Outcome B (Runner B wins):** Runner B's takeover succeeds first, setting `lease_owner_id = Runner B`, incrementing `fencing_generation`, and generating a new ETag. Owner A's publication write then receives HTTP 412. Owner A's stale ETag is permanently invalidated. Owner A **cannot publish its baseline**, and its package remains an unindexed orphan for operator review.

### Principle 3: Mandatory Heartbeat Abort Gate
- If a runner's periodic lease renewal heartbeat fails (due to network disruption, transient 5xx, or lost lease):
- The runner **must immediately abort its crawl and packaging lifecycle**.
- It is strictly forbidden from attempting baseline publication if its lease heartbeat is unhealthy.

---

## 2. Sole Authority of `portal_control.json` & Non-Regressing Catalog Projection

### Principle 1: Sole Authority
- `state/{portal_id}/portal_control.json` is the **single, unchallengeable source of truth** for which run constitutes the active incremental baseline (`baseline.latest_successful_run_id`).
- `state/{portal_id}/catalog.json` is an auxiliary, projected history ledger.

### Principle 2: Idempotent Projection
- Baseline publication commits `portal_control.json` first.
- Projection into `catalog.json` is a subsequent idempotent step that registers the verified `CatalogRunEntry` and updates the catalog pointer to match `portal_control.json.baseline.latest_successful_run_id`.

### Principle 3: Strict Monotonic Non-Regression Guarantee
- If a crash occurs after `portal_control.json` was committed with Run 100, but before `catalog.json` was updated:
- A subsequent recovery or run reconciles `catalog.json` by projecting the baseline from `portal_control.json`.
- **Non-Regression Invariant:** A delayed catalog recovery or out-of-order execution **must never move `catalog.latest_successful_run_id` backward**.
- The reconciler and hydration adapters enforce:
  `new_baseline_generation >= current_baseline_generation`
  `new_baseline_timestamp >= current_baseline_timestamp`
- Even if a stale process attempts to write an older `catalog.json` snapshot, projection checks the current `portal_control.json` baseline and refuses to regress the active pointer.
