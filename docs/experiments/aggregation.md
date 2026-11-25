# Aggregate measurements and plot attempts

`experiments/aggregate.py` reads saved results locally. It performs no SSH,
cluster setup, or measurements, and never edits the input artifacts. Pass an
attempt directory, its `measurement/` directory, or an individual diagnostic
phase directory. Pass several inputs to compare attempts without pooling them.

```bash
# Summarize one attempt, including every completed diagnostic phase.
./experiments/aggregate.py \
  results/collection-TIMESTAMP/01-vxlan-attempt-1

# Compare two attempts in one report and set a new output directory.
./experiments/aggregate.py \
  results/collection-TIMESTAMP/01-vxlan-attempt-1 \
  results/collection-TIMESTAMP/02-vm-vxlan-1450-attempt-1 \
  --output results/vxlan-comparison

# Summarize one phase.
./experiments/aggregate.py \
  results/collection-TIMESTAMP/01-vxlan-attempt-1/measurement/phases/sample-01-pod-single

# Explicitly take a snapshot of completed phases from a running/failed attempt.
./experiments/aggregate.py results/collection-TIMESTAMP/02-vm-vxlan-1450-attempt-1 \
  --allow-partial
```

Default output is a new `results/aggregate-UTC_TIMESTAMP/` directory. Existing
output directories are rejected. Input measurements must pass unless
`--allow-partial` is supplied; that option excludes unfinished/failed diagnostic
phases and labels the result as partial. It does not make partial combined
non-diagnostic runs valid. Snapshot reports do not update automatically; rerun
into a new directory to reflect later completed phases. Concurrent writes to
an active input may require retrying the snapshot. No collection or cluster
lock is needed because the utility only reads saved measurements.

## Plotting dependency

Tables and statistics use the Python standard library. Graphs use Matplotlib.
In a Python virtual environment:

```bash
python3 -m venv /tmp/cs740-analysis
/tmp/cs740-analysis/bin/python -m pip install -r experiments/requirements-analysis.txt
/tmp/cs740-analysis/bin/python experiments/aggregate.py PATH_TO_ATTEMPT
```

On NixOS, a Nix-provided Python environment avoids shared-library issues from
ordinary binary wheels:

```bash
nix-shell -p 'python3.withPackages (ps: [ ps.matplotlib ])' \
  --run 'python3 experiments/aggregate.py PATH_TO_ATTEMPT'
```

Use `--no-plots` for dependency-free Markdown/CSV/JSON output. A missing or
unloadable plotting dependency fails with an explanation before output is
created; graphs are never silently omitted.

## Output

| File | Contents |
| --- | --- |
| `summary.md` | Performance, CPU, counter tables, embedded figures, coverage notes |
| `observations.csv` | Individual observations with attempt, phase, sample, scope, units, source file |
| `aggregate.csv` | Count, mean, sample SD, median, minimum and maximum for each group |
| `summary.json` | Input metadata and phase status, all aggregate statistics and warnings |
| `figures/*.svg`, `figures/*.png` | Exportable throughput, latency, transaction-rate, CPU and per-core heatmap figures |

Performance charts show individual samples with the mean and one sample
standard deviation. Throughput plots convert Mb/s to Gb/s for readability;
CSV/JSON values retain original units. CPU charts distinguish machine-wide
average utilization from the busiest individual core in each phase. Per-target
heatmaps show mean utilization of each core across samples, separated by test
and attempt. Missing heatmap cells are gray. Figures are generated only for
available metric families; absent CPU diagnostics do not produce CPU figures.

Diagnostic phase reports are the canonical input. Their copies under the
measurement root's `reports/` are ignored, avoiding duplicate observations.
Cilium JSON provides throughput, transaction rates and latency percentiles;
single-sample console tables supplement minimum, mean and maximum latency.
Combined non-diagnostic runs can retain only the final trial in JSON, so their
summary explicitly warns that observation count is not the requested sample
count. Earlier console trials are not reconstructed in that case.

## Interpretation

Groups preserve attempt, mode, test, network, node placement, units, target,
core/interface, and measurement scope. Multiple attempts are compared but never
pooled. Ambiguous duplicate attempt names and duplicate performance metrics
within one phase are rejected. Different endpoint/workload/profile settings
produce a comparison warning. Averages of per-sample latency percentiles are
not pooled transaction percentiles. Sample SD is unavailable for one observation
and is not a confidence interval. Within-attempt samples are not independent
cluster rebuilds; the tool makes no significance or causal claims.

CPU, TCP and backlog summaries use the approximate observed workload window.
NIC/queue deltas cover the entire invocation, including setup and cleanup, and
keep driver-specific counter names rather than assuming every value is packets.
Host-wide TCP/backlog deltas are loaded once per target and phase, avoiding the
repetition present in each core row of the original diagnostic CSV. Negative
NIC deltas are omitted with a warning as possible counter resets. Values from
different interfaces and guest/host layers can count the same traffic; they
must not be added as independent events. Full capability warnings remain in
`summary.json`; the Markdown report displays the first 20.
