
## Experiment `exp-20261010-124011-8eba66` (gemini)

_2026-10-10T08:12:40+00:00_ -- severity: **critical**


A 'kill' fault on the 'things-search' service resulted in a total search outage (100% error rate) and an experiment abort. Automated recovery failed because the container was recreated with a different ID, preventing the rollback mechanism from verifying the state.

- **[critical] Complete Search Service Outage** (supported_by_measurements) -- The 'during_search_error_rate' reached 1.0 and 'during_search_p95_ms' became null, triggering an abort after 15.42 seconds.
  - Impact: All search-related functionality for 'things' was unavailable to users.
  - Fix: Implement a multi-replica deployment for 'things-search' to ensure high availability during individual node failures.

- **[high] Rollback Mechanism Failure** (supported_by_measurements) -- The 'rollback_verified' field is false and 'rollback_error' indicates a container ID mismatch (1c951d5ba6fb).
  - Impact: The system was left in an unverified state, requiring manual SRE intervention to confirm service health.
  - Fix: Update chaos engineering scripts to identify containers by stable labels or names rather than volatile internal IDs.

## Experiment `exp-20261010-124738-86f5bb` (gemini)

_2026-10-10T08:12:49+00:00_ -- severity: **critical**


A kill fault on the things-search service resulted in a total service outage with a 100% error rate, ultimately failing to recover within the allotted time and triggering a recovery timeout.

- **[critical] Total Search Service Outage** (supported_by_measurements) -- The during_search_error_rate increased from 0.0 to 1.0, and the experiment aborted because the search endpoint failed on all of the last 3 ticks.
  - Impact: Users were completely unable to perform search operations during the fault period.
  - Fix: Review the liveness and readiness probe configurations for things-search to ensure the orchestrator can correctly identify and recover failed instances.

- **[critical] Recovery Timeout** (supported_by_measurements) -- The experiment state reached RECOVERY_TIMEOUT and recovery_outcome was 'timeout'.
  - Impact: The system did not return to a healthy state automatically within the expected timeframe, requiring manual intervention or rollback.
  - Fix: Investigate the startup latency and dependency initialization (mongodb, things) for the things-search service to identify bottlenecks in the recovery path.

## Experiment `exp-20261010-124011-8eba66` (gemini)

_2026-10-10T08:37:39+00:00_ -- severity: **critical**


A kill fault on the things-search service caused a total search endpoint outage (100% error rate) and triggered an automated abort, but the recovery failed due to a container ID mismatch during rollback.

- **[critical] Total Search Service Failure** (supported_by_measurements) -- during_search_error_rate: 1.0, abort_reason: 'search endpoint failed on all of the last 3 ticks'
  - Impact: Users are unable to perform search operations on things.
  - Fix: Implement high availability for things-search to prevent single-point-of-failure outages.

- **[high] Automated Rollback Failure due to Container ID Conflict** (supported_by_measurements) -- rollback_error: 'container 'docker-things-search-1' now has id 1c951d5ba6fb, not the original 1c951d5ba6fb'
  - Impact: Manual intervention required to restore the environment after a chaos experiment.
  - Fix: Update the chaos tool to handle container recreation or use labels instead of strict ID matching for rollback.

## Experiment `exp-20261010-143332-3d4bf9` (gemini)

_2026-10-10T09:05:00+00:00_ -- severity: **low**


A 'kill' fault targeting the 'things' service resulted in a significant reduction in p95 latency for both direct and search operations, with zero errors and no data loss recorded.

- **[low] Significant Latency Improvement During Service Outage** (supported_by_measurements) -- baseline_direct_p95_ms: 91.5, during_direct_p95_ms: 50.9; baseline_search_p95_ms: 137.0, during_search_p95_ms: 54.7
  - Impact: The system performed significantly better with one fewer instance, suggesting the baseline state included an underperforming node.
  - Fix: Investigate automated health checks and outlier detection to proactively remove or restart degraded service instances that negatively impact tail latency.

- **[low] High Availability and Fault Tolerance** (supported_by_measurements) -- during_direct_error_rate: 0.0, during_search_error_rate: 0.0, during_lost_acked_max: 0
  - Impact: The 'things' service demonstrated perfect availability and data integrity during a node failure.
  - Fix: None required for availability; current redundancy levels are effective.

## Experiment `exp-20261010-145742-7cf6db` (deterministic_fallback)

_2026-10-10T09:29:00+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-145742-7cf6db (kill fault on 'connectivity').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-150132-ce02ba` (deterministic_fallback)

_2026-10-10T09:32:50+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-150132-ce02ba (kill fault on 'policies').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-140527-9af8a1` (deterministic_fallback)

_2026-10-10T09:37:42+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-140527-9af8a1 (delay fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-150756-de09ec` (deterministic_fallback)

_2026-10-10T09:38:07+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-150756-de09ec (kill fault on 'gateway').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-150841-aaa4df` (deterministic_fallback)

_2026-10-10T09:38:51+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-150841-aaa4df (kill fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-150904-be08c8` (deterministic_fallback)

_2026-10-10T09:39:14+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-150904-be08c8 (delay fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-151103-e254bd` (deterministic_fallback)

_2026-10-10T09:41:13+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-151103-e254bd (kill fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-151232-3290b5` (deterministic_fallback)

_2026-10-10T09:42:43+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-151232-3290b5 (delay fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-151247-ca4ff1` (deterministic_fallback)

_2026-10-10T09:42:58+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-151247-ca4ff1 (kill fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-151403-46f28b` (deterministic_fallback)

_2026-10-10T09:44:14+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-151403-46f28b (kill fault on 'things-search').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-152045-8a0677` (deterministic_fallback)

_2026-10-10T09:52:02+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-152045-8a0677 (delay fault on 'mongodb').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-152503-da3f33` (deterministic_fallback)

_2026-10-10T09:56:19+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-152503-da3f33 (kill fault on 'things').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-154539-224fa8` (deterministic_fallback)

_2026-10-10T10:16:56+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-154539-224fa8 (kill fault on 'connectivity').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.

## Experiment `exp-20261010-155949-d9be8e` (deterministic_fallback)

_2026-10-10T10:31:05+00:00_ -- severity: **low**


Deterministic comparison of baseline vs. during-fault measurements for experiment exp-20261010-155949-d9be8e (kill fault on 'policies').

- **[low] No significant degradation detected in the saved measurements** (supported_by_measurements) -- all before/during deltas are within noise
  - Impact: No evidence of a resilience weakness from this experiment.
  - Fix: Consider a longer or larger fault parameter to get a more informative signal next time.
