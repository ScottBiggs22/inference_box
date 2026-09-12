# vast.ai — booking and session management

**Status:** operational reference, written for agents
**Audience:** whoever (human or agent) rents the next GPU for this project
**Derived from:** the Phase 1 session of 2026-09-10 — one A10, 27 minutes of
wall clock, **$0.117** total. Every trap below cost real time or money in that
session, or came within one command of doing so.
**Related:** `VAST_AI_RUNBOOK.md` is *what to do on the box for Phase 1*. This
document is *how to operate vast.ai at all*, and outlives any one phase.

---

## 0. Division of labour — read this before anything else

**The human sets the API key. The agent never sees it.**

```bash
# HUMAN runs this, once:
vastai set api-key <key from https://cloud.vast.ai/manage-keys/?tab=api-keys>
```

It lands in `~/.config/vastai/vast_api_key`. Thereafter an agent can drive the
entire CLI — search, create, ssh-key, destroy — without the key ever entering a
conversation transcript. This is strictly better than the human pasting the key
to the agent, and it costs one command.

**What an agent must still escalate to the human:**

| Action | Why |
|---|---|
| Creating an instance | It spends money. Confirm the offer and the price first |
| Destroying, if any artefact is unretrieved | Irreversible; the disk goes with it |
| Anything after `verify_box.sh` fails | Destroy-and-rebook is a judgement call |

**What an agent should just do:** searching, provenance checks, SSH key
lifecycle, running the measurement, teardown *after* artefacts are collected.

---

## 1. Install

```bash
curl -fsSL https://vast.ai/install.sh | bash      # -> ~/.local/share/vastai
export PATH="$HOME/.local/bin:$PATH"              # NOT on PATH by default
vastai --version                                  # verified against 1.6.0
```

Every command below was verified against **CLI 1.6.0**. The CLI changes; when
something does not match, `vastai <command> --help` is authoritative and this
document is not.

---

## 2. Searching for offers — three traps

```bash
vastai search offers 'gpu_name=A10 num_gpus=1 rentable=true' -o 'dph_total' --raw
```

**Trap 1 — the default search does not show you the inventory.** A bare
`vastai search offers 'num_gpus=1'` returned ~64 rows, and the A10 it happened
to include differed between two consecutive runs. Filtering by `gpu_name`
returned the true set: **exactly two single-GPU A10s existed on the whole
platform** that day. If you are hunting a specific card, **always filter by
`gpu_name`**, or you will conclude from a truncated list that your card is
unavailable, or pick a worse one than exists.

**Trap 2 — offer IDs are ephemeral.** The same physical machine was offered as
`47574078`, then `47574080`, then `47574081` across three queries minutes apart;
vast.ai re-chunks offers continuously. **`machine_id` is stable; `id` is not.**
So: pick by `machine_id`, and **re-resolve the offer `id` immediately before
`create`**, in the same command if possible. An offer id can only be used once.

**Trap 3 — `gpu_ram` is MiB, and the marketing number is not the addressable
number.** A "24 GB" A10 reports **23028 MiB**, and vLLM sees **22.06 GiB**
addressable. Budget against the addressable figure — PRD §3.3 was 12% optimistic
precisely because it used 24 GB (see `PHASE1_RESULTS.md` §3).

### Fields worth reading, and what they told us

| Field | Why it mattered |
|---|---|
| `machine_id` | the stable identity; `id` is not |
| `gpu_name` | `A10` vs `A10G` vs `L4` — different cards, different numbers |
| `gpu_ram` | MiB. 23028 = full A10; anything near half is a partition |
| **`cuda_max_good`** | **the single most important pre-flight field — see §3** |
| `driver_version` | goes to DevOps as the verified minimum |
| `gpu_max_power` | 150 W = A10 at full TDP. A capped card gives numbers that do not transfer |
| `dph_total` | $/hr for the GPU. **Not** the whole bill — see §7 |
| `storage_cost` | $/GB/month. This is what bills on a *stopped* instance |
| `disk_space` | host's free disk, not your allocation (`--disk` sets that) |
| `inet_down` | you pay hourly while pulling ~16 GB of image + weights |
| `reliability2` | >0.98 is a reasonable bar |
| `pcie_bw`, `disk_bw` | affect weight-load time, i.e. cold start — not decode throughput |
| `geolocation` | irrelevant to on-box measurements; matters for SSH interactivity |

---

## 3. The pre-flight that saves a whole rental — CUDA vs driver

**Check the image's CUDA build against the offer's `cuda_max_good` *before*
renting.** This is the highest-value check in this document.

On 2026-09-10 exactly two A10 offers existed, at an identical $0.2417/hr:

| machine | location | driver | `cuda_max_good` | reliability |
|---|---|---|---|---|
| `37444` | Netherlands | 570.144 | **12.8** | 1.000 |
| `123534` | Illinois | 595.84 | **13.2** | 0.999 |

The Netherlands box looked *better* on every visible metric. But
`vllm/vllm-openai:v0.28.0` is a **CUDA 13.0** build, and 12.8 < 13.0 — it would
have booted the container, pulled ~16 GB, and failed. Renting it would have cost
a full destroy-and-rebook cycle.

```bash
# Find the image's CUDA build BEFORE booking. For vLLM, the release notes state
# it, and the container confirms it:
python3 -c "import torch; print(torch.version.cuda)"   # 13.0 for v0.28.0
```

**Rule: require `cuda_max_good >= <image CUDA major.minor>`.** A newer driver
runs an older CUDA runtime fine; the reverse is never true.

---

## 4. Creating the instance

```bash
vastai create instance <FRESH_OFFER_ID> \
  --image vllm/vllm-openai:v0.28.0 \
  --disk 60 \
  --ssh --direct \
  --label bkn301-phase1 \
  --cancel-unavail
```

**`--ssh --direct` changes the container's semantics, and that is the point.**
From `create instance --help`:

> *"If you use args/entrypoint launch mode, we create a container from your image
> as is... For ssh/jupyter launch types, use `--onstart-cmd` to pass in startup
> script, instead of `--entrypoint` and `--args`."*

With `--ssh`, vast.ai injects its own SSH setup and **does not run the image's
ENTRYPOINT**. That is what gives you a plain shell on an image like
`vllm/vllm-openai`, whose entrypoint is the API server. You do not need
`--entrypoint` to override it — asking for `--ssh` already has.

**`--cancel-unavail` is not optional.** Without it, a scheduling failure creates
a **stopped instance that still bills for its disk**. With it you get an error.

**`--disk`:** size for image + weights + cache, not just weights. The vLLM image
alone is 9.7 GB, Qwen3-8B-AWQ is ~6 GB. 30 GB is tight; **60 GB** costs about
$0.02/hr and removes a failure mode.

**No published ports.** Reach services through an SSH tunnel (§6). Publishing an
auth surface from a rented host is not worth the convenience.

### ⚠ The create response prints a secret

```json
{"success": true, "new_contract": 50504688,
 "instance_api_key": "f6852aad…"}
```

`instance_api_key` is a credential, and it lands in your terminal scrollback and
in any agent transcript. It is **instance-scoped** — it dies with the contract —
but treat it like any other secret until then: do not write it to a file in the
repo, and do not paste it onward.

### Waiting for `running`

```bash
vastai show instance <ID> --raw | python3 -c "
import json,sys; o=json.load(sys.stdin)
print(o.get('actual_status'), '|', o.get('status_msg'))"
```

`actual_status` goes `None` → `loading` → `running`, typically under a minute.
Poll it; do not assume.

---

## 5. SSH keys — none exist by default

A fresh account has **no SSH keys**, and a fresh Mac may have none locally
either. Generate a **dedicated throwaway**, not a general-purpose key:

```bash
KEY="$HOME/.ssh/vastai_<project>_throwaway"
ssh-keygen -t ed25519 -N '' -f "$KEY" -C "throwaway-$(date +%Y%m%d)"

vastai create ssh-key "$(cat "$KEY.pub")"          # register on the account
vastai attach ssh <INSTANCE_ID> "$(cat "$KEY.pub")" # attach to the instance
```

No passphrase, because the whole point is non-interactive automation against a
host you already treat as untrusted. Delete it at teardown (§8) — both from the
account and from `~/.ssh`.

**Registering a key does not attach it to existing instances.** The CLI says so:
*"Note: You may need to add the new public key to any pre-existing instances."*
Do both steps.

---

## 6. Connecting — use an SSH config file, not inline flags

```bash
vastai ssh-url <INSTANCE_ID>      # -> ssh://root@138.128.140.140:50194
```

**The direct port is not the proxy port.** `vastai show instance` reports
`ssh_host: ssh2.vast.ai` with `ssh_port: 24688`, while `ssh-url` gave
`138.128.140.140:50194`. Using the proxy port against the direct IP gives
`Connection refused`. The proxy route is the reliable default; the direct route
is lower latency.

**Write a config file rather than passing `-i … -o …` inline:**

```bash
cat > /tmp/vast_ssh_config <<'CFG'
Host vastbox
    HostName ssh2.vast.ai
    Port 24688
    User root
    IdentityFile ~/.ssh/vastai_<project>_throwaway
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    UserKnownHostsFile ~/.ssh/known_hosts_vastai
    ConnectTimeout 20
    ServerAliveInterval 30
    ServerAliveCountMax 6
CFG
chmod 600 /tmp/vast_ssh_config
ssh -F /tmp/vast_ssh_config vastbox 'nvidia-smi'
scp -F /tmp/vast_ssh_config file.tgz vastbox:/root/
```

This is not style. **macOS defaults to zsh, and zsh does not word-split unquoted
parameters.** `SSHOPT="-i key -o Foo=bar"; ssh $SSHOPT host` passes the entire
string as one argument to `-i` and fails with
`Identity file  <the whole string> not accessible`. It cost two failed
connection attempts before the config file replaced it. The same bug bites
`set -- $VAR` — use `cut`/`awk` or an array.

Related zsh trap: a literal `<placeholder>` in a command you hand a human to
paste is a **redirect**, and zsh errors with `no such file or directory`. Either
substitute the value yourself or use a placeholder without angle brackets.

---

## 7. Working on the box — four assumptions that were wrong

**`/workspace` may not exist.** The `vllm/vllm-openai` image's home is `/root`;
`/workspace` is absent. Anything defaulting to it silently misbehaves — in our
case a data-hygiene scan `find`-ed a non-existent directory, found nothing, and
reported **PASS having examined nothing**. Check the layout first:

```bash
ssh -F … vastbox 'pwd; echo $HOME; ls -1 /; df -h /'
```

**`pkill -f "<pattern>"` will kill your own SSH session.** `pkill -f` matches
full command lines, and the command line of the shell sshd spawned for you
contains your whole script — including the pattern. This killed two measurement
runs. The `pkill -f 'vllm[ ]serve'` bracket trick does **not** save you if the
same command line also contains the literal launch text.

**The fix: put long-running launches in a file on the box**, so a kill pattern
and the launch text never share a command line:

```bash
# once
ssh -F … vastbox 'cat > /root/launch.sh <<"EOS"
#!/usr/bin/env bash
setsid nohup <your long-running command> > /root/service.log 2>&1 < /dev/null &
disown
EOS
chmod +x /root/launch.sh'

# thereafter, safely, from a command line that never contains the launch text
ssh -F … vastbox 'pkill -f "<distinctive-process-token>"; sleep 10; bash /root/launch.sh'
```

**`setsid nohup … & disown` with stdin from `/dev/null`** is what makes the
process survive the SSH session closing. Without it the service dies when the
call returns.

**`pgrep -f` self-matches too.** `pgrep -fc "Qwen3-8B"` reported 2 when nothing
was running — it was counting its own shell. Prefer checking the *effect*
(`curl` a health endpoint, read `nvidia-smi`) over checking the process table.

**Host-injected credentials exist.** vast.ai writes `/root/.vast_api_key` into
every instance. Verify by hash that it is **not** your account key:

```bash
shasum -a 256 < ~/.config/vastai/vast_api_key   # laptop
ssh -F … vastbox 'sha256sum < /root/.vast_api_key'
```

Ours differed (65 bytes vs 64), i.e. instance-scoped. A hygiene gate should
report it, not fail on it — a gate that can never pass is one people learn to
skip.

---

## 8. Money — what actually bills

| Component | Rate (our A10) | Bills when |
|---|---|---|
| GPU | $0.2417/hr | instance **running** |
| Storage | $0.24/GB/month | instance **exists** — running *or stopped* |
| All-in as reported | $0.26/hr | |

**60 GB of storage is ~$0.48/day on a stopped instance.** Against a $5 balance,
forgetting to destroy costs real money within a week.

**Destroy. Do not stop.** "Stop" keeps the disk and keeps billing for it.

Check spend before teardown:

```bash
vastai show instances --raw | python3 -c "
import json,sys
for i in json.load(sys.stdin):
    print(f\"id={i['id']} {i['actual_status']} \${i.get('dph_total',0):.4f}/hr \"
          f\"up={i.get('duration',0)/3600:.2f}h \"
          f\"~\${i.get('dph_total',0)*i.get('duration',0)/3600:.3f}\")"
```

---

## 9. Teardown — the checklist, in order

**Collect artefacts first. The disk is not recoverable after destroy.**

```bash
# 1. Pull everything you need OFF the box
scp -F /tmp/vast_ssh_config vastbox:'/root/…/*.jsonl' ./results/
scp -F /tmp/vast_ssh_config vastbox:/root/service.log ./results/

# 2. Scan what came back — a rented box can hand you things too
bash scripts/verify_box.sh --scan-only ./results

# 3. Destroy. -y IS REQUIRED in any non-interactive context.
vastai destroy instance <ID> -y

# 4. Verify it is actually gone
vastai show instances --raw    # must be []

# 5. Volumes bill independently of instances
vastai show volumes --raw      # must be []

# 6. Remove the throwaway key from the account AND locally
vastai show ssh-keys           # find the id
vastai delete ssh-key <KEY_ID>
rm -f ~/.ssh/vastai_*_throwaway* ~/.ssh/known_hosts_vastai

# 7. Confirm the balance moved by roughly what you expected
vastai show user --raw | python3 -c "
import json,sys; print(f\"credit: \${json.load(sys.stdin).get('credit',0):.3f}\")"
```

**`vastai destroy instance` prompts interactively and *aborts* if it gets no
answer.** In our session it printed
`Are you sure…? [y/N]` → `Aborted.` and **the instance kept running and kept
billing** while we believed it was gone. Only `-y` makes it non-interactive.
Always run step 4.

Order note: we deleted the SSH key before destroying, which worked but is
backwards — destroy first, then keys, so you cannot lock yourself out of a box
you still need.

---

## 10. Worked sequence — the whole session

For reference, the Phase 1 session in order. ~27 minutes, $0.117.

```bash
export PATH="$HOME/.local/bin:$PATH"

# 1. (human) vastai set api-key <KEY>

# 2. Find candidates, filtered by card
vastai search offers 'gpu_name=A10 num_gpus=1 rentable=true' -o 'dph_total' --raw

# 3. Pre-flight: cuda_max_good >= image CUDA, gpu_ram ~23028, gpu_max_power 150

# 4. Re-resolve a fresh offer id for the chosen machine_id, then create
vastai create instance <FRESH_ID> --image vllm/vllm-openai:v0.28.0 \
  --disk 60 --ssh --direct --label <label> --cancel-unavail

# 5. Poll actual_status -> running

# 6. Generate, register and attach a throwaway SSH key; write an ssh config

# 7. Transfer code with an audited packer; extract on the box

# 8. GATE: verification BEFORE pulling weights. Pass WORKSPACE explicitly.
ssh -F … vastbox 'cd /root/<repo> && WORKSPACE=/root bash scripts/verify_box.sh'

# 9. Launch the service from a FILE on the box; measure

# 10. Collect artefacts, scan them, destroy -y, verify empty, delete keys
```

---

## 11. What this document does not cover

Stated so nobody reads absence as endorsement. None of the following was
exercised on 2026-09-10, so none of it is verified here:

- **Interruptible / spot instances** (`--bid_price`). Cheaper, and they can be
  reclaimed mid-run — which would silently truncate a benchmark.
- **Volumes** (`--create-volume`, `--link-volume`). They bill independently and
  survive instance destruction, which is a footgun worth understanding before
  use. Step 5 of §9 checks for strays regardless.
- **Templates** (`--template_hash`). They pin a configuration outside our
  control, which works against reproducing a run on another provider.
- **Multi-GPU.** Out of scope by design — PRD §4.5 requires one whole GPU per
  pod and nothing else on the card.
- **Whether any of this generalises to another GPU provider.** The vast.ai
  specifics (offer-id churn, injected `.vast_api_key`, `--ssh` suppressing the
  entrypoint) certainly do not.
