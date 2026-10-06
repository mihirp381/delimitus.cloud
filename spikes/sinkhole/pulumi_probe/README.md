# Pulumi probe (SSC-091)

Cloud DNS creates a rule in about 0.2 s and took 660 a minute in `../results-2026-10-06.json`,
yet `pulumi up` made about 18 a minute (3.3 s a rule). This probe finds where Pulumi spends the
time: it creates N rules shaped like the cell's `*.<tld>.` sinkhole rules, with
`pulumi up --parallel P` for several P, and times the `up` and the `destroy` after it.

## What it touches

Only project `ssc-platform-0` (the runner and the program both refuse any other, and
`ristretto-506621`). One policy `ssc-exp091p` with no network, one sinkhole rule
`ssc-exp091p-sink` (each rule depends on it, as the cell's do) and N rules `ssc-exp091p-<i>`.
State is a local file backend in a fresh temporary folder for each P, with an empty passphrase:
nothing goes to Pulumi Cloud or to the repo's stacks. No VPC or cell is affected. The destroy runs
in `finally`. If it fails, the folder is kept and named, and `python3 ../sinkhole.py
--cleanup-only` deletes every `ssc-exp091*` policy and rule.

`program.py` copies the rule's arguments from `Cell.dns_policy` and does not import `ssc_infra`;
a test compares them with the cell's own rule so the copy cannot drift. Not copied: the cell's
rules depend on about 20 API-enabling resources, and its `project` is an output.

## Run

Needs `uv` and `pulumi` on the path and `gcloud` signed in (the token comes from
`GOOGLE_OAUTH_ACCESS_TOKEN` if set, else `gcloud auth print-access-token`). Pulumi's uv toolchain
makes `.venv` in this folder on the first run from `pyproject.toml` and `uv.lock`, which pin the
same pulumi 3.263.0 and pulumi-gcp 9.37.0 as infra; that needs the network once. It is not
`infra/.venv`: the pip toolchain wants pip in the venv (uv's has none), and the uv toolchain may run
`uv sync` on the venv it is given, which would strip infra's (not tested). No other Pulumi or gcloud command
is run.

```
cd spikes/sinkhole/pulumi_probe
python3 run.py --dry-run                  # every command, nothing run
python3 run.py --parallel 1,8,32          # 100 rules each; about 30 minutes in all
python3 run.py --parallel 32 --count 1500 # the cell's size, once the small run is understood
```

Same P is used for the destroy as for the up. `--count` is 1 to 1500. Stop it with Ctrl-C and it
still destroys. It prints the Pulumi CLI, pulumi-gcp and the installed provider plugin versions
first, and writes `results.json` after each P.

### State size and backend

- `--pad-mb N` (0 to 10, default 0) makes the checkpoint about N MB. The padding is the output of
  a component resource created first (hex of `random.Random(91).randbytes`, so it does not
  compress), because a stack output only joins the checkpoint when the program ends and would not
  pad `up`. The program exports only a small `pad_mb` marker.
- `--backend gs` keeps the state in `gs://ssc-platform-0-pulumi/exp091p/<P>-<UTC timestamp>`, a
  fresh prefix per P, and prints it. After a clean destroy it runs `pulumi stack rm --yes --force`
  to remove the files. After a failed destroy it does not, and the prefix stays for you to
  clean up. Any other bucket or prefix is refused. The default is `--backend file`.
- `--skip-checkpoints` sets `PULUMI_SKIP_CHECKPOINTS=true` for up and destroy.
- `results.json` records `backend`, `pad_mb` and `skip_checkpoints` for each run.

## Read it

`results.json`, per P: `up_seconds`, `up_rules_per_minute` (rules include the sinkhole rule),
`destroy_seconds`, `destroy_rules_per_minute`, `error` if any.

1. **About 18 a minute at every P.** Something serialises below the cloud: the provider or the
   engine, not the flag. Read the log for what a create waits on.
2. **Rate rises with P** (8 about eight times 1). Pulumi parallelises fine, and the 78 minutes
   comes from what is different in the cell: its dependency lists (about 20 APIs on each rule),
   its size (1,438 rules; try `--count 1500`), or its project output. The next probe adds those.
3. **Fast everywhere** (hundreds a minute at 32). Same conclusion as 2, for the cell's own shape.
4. **`destroy` much slower or faster than `up`**: tells whether deletes need the same fix.

For P=8 the run keeps `logs/p8.log` (gitignored; `pulumi up --logflow -v=9 --logtostderr`, with
`TF_LOG=DEBUG`, for up and destroy) and prints a summary of it:

- `rule creates`: how many started and ended, how many at once (up to 8 if Pulumi really runs
  eight), and `one create`, the median and maximum seconds from the engine's `Create` to its
  answer. About 3 s here with 0.2 s POSTs means the time is inside the provider, before or after
  the POST.
- The table of the first and last five rules: start, end, seconds, relative to the first start.
- `HTTP calls seen`: the provider's calls by method and path with status. A `GET .../rules/{rule}`
  after each `POST` is the provider's read after create. `rule POSTs` shows the gap between
  neighbouring POSTs: about 3 s apart at 8 at once means they queue before the call.
- `429, retry, sleep, backoff or quota lines`: the first ten. Terraform's logging mentions
  retries in ordinary lines, so read them. A 429 here is the silent retry the provider does
  within its 20-minute create timeout.

The summary is a reading aid built on Pulumi's glog lines and Terraform's HTTP dump. If it finds
no creates, `logs/p8.log` is still the data. The provider is expected to redact the Authorization
header; check before sharing the file, and keep it local.

Tests (no Pulumi, gcloud or network): `cd infra && uv run pytest ../spikes/sinkhole -p no:cacheprovider`.

## Results (2026-10-06)

101 rules each, the same program, in `results/`. The cell's own state is 7.1 MB, so the padded runs carry 7 MB.

| Run | Backend | Padding | Checkpoints | Up | Rules a minute | Destroy |
|---|---|---|---|---|---|---|
| `file-pad0` P=1 / 8 / 32 | local file | none | on | 29.9 / 8.6 / 4.5 s | 203 / 701 / 1,344 | 15.0 / 4.4 / – s |
| `gs-pad7` P=8 | `gs://ssc-platform-0-pulumi/exp091p/…` | 7 MB | on | 685.5 s | **8.8** | 673.8 s |
| `gs-pad7-skip` P=8 | the same bucket | 7 MB | `PULUMI_SKIP_CHECKPOINTS` | 25.5 s | 238 | 10.2 s |
| `file-pad7` P=8 | local file | 7 MB | on | 22.1 s | 274 | 21.4 s |

No 429 in any run, and Cloud DNS answers each create in about 0.3 s. The slowness is Pulumi rewriting the whole checkpoint in the bucket after each step, one write at a time: about 3.4 s per write with a 7 MB state, two writes per resource. The cell's 18 a minute fits a state that grows from nothing to 7 MB during onboarding. A local file backend with the same state is 31 times faster and still writes every checkpoint. Chosen (SSC-091 phase 2b): onboarding keeps its state in a local file backend and moves it to the bucket once, at the end.
