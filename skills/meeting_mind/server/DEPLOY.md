# Server side of `--backend spark` — install and update

What runs where:

| Piece | Location |
|---|---|
| Environment (pixi workspace, `pixi.toml` here) | `/opt/bio/apps/meetingmind/` |
| Code: a copy of this skill's `src/meetingmind` | `/opt/bio/apps/meetingmind/src/meetingmind/` |
| Entry point (`meetingmind-asr` here) | `/opt/bio/apps/bin/meetingmind-asr` |
| Model Qwen/Qwen3-ASR-1.7B, revision `7278e1e` | `/opt/bio/db/qwen3-asr-1.7b/current` |
| Job directories, one per meeting, mode 0700 | `/scratch/meetingmind/<user>/` (1777; not backed up) |
| Lab settings for clients (cloud address, meetings folder) | `/opt/bio/apps/meetingmind/lab.json` |
| Daily clean-up of abandoned job directories | `/opt/bio/sbin/meetingmind-scratch-clean` + `meetingmind-scratch-clean.{service,timer}` |

Every step below runs as root (`sudo -i`, or `sudo bash` and then these lines)
with `umask 002`. Ownership and modes are set explicitly rather than inherited:
copies keep whatever their source had, and a source in someone's home is often
group-private.

## Install

```bash
umask 002
# A copy of the skill on the server (only `server/` and `src/` are needed),
# e.g. scp'd into your own scratch first. Absolute, because the steps cd around.
SKILL=/path/to/meeting_mind
APP=/opt/bio/apps/meetingmind

# 1. Model, pinned to the revision the local installs use
S=/opt/bio/db/.staging/qwen3-asr-1.7b && mkdir -p "$S" && cd "$S"
R=7278e1e70fe206f11671096ffdd38061171dd6e5
for f in chat_template.json config.json generation_config.json merges.txt \
         model-00001-of-00002.safetensors model-00002-of-00002.safetensors \
         model.safetensors.index.json preprocessor_config.json tokenizer_config.json vocab.json README.md; do
  curl -sSfL --retry 3 -o "$f" "https://huggingface.co/Qwen/Qwen3-ASR-1.7B/resolve/$R/$f"
done
sha256sum model-0000*.safetensors   # must match the LFS oids:
#   a4cd1f1a04d90b757dc7f7dd26254e69a013b19e80efe590a83c6a3bde8608d6  model-00001-of-00002.safetensors
#   6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc  model-00002-of-00002.safetensors
bio-admin db-add qwen3-asr-1.7b 7278e1e "$S"

# 2. Environment (~5.4 GB)
install -d -m 2775 -o root -g bioadm "$APP"
install -m 664 -o root -g bioadm "$SKILL/server/pixi.toml" "$APP/pixi.toml"
(cd "$APP" && PIXI_CACHE_DIR=/opt/bio/cache pixi install)

# 3. Code and entry point
install -d -m 2775 -o root -g bioadm "$APP/src"     # rsync creates only the last level
rsync -rlt --delete --no-owner --no-group --chmod=D2775,F664 \
      --exclude __pycache__ --exclude '*.bak' "$SKILL/src/meetingmind/" "$APP/src/meetingmind/"
chown -R root:bioadm "$APP/src"
install -m 775 -o root -g bioadm "$SKILL/server/meetingmind-asr" /opt/bio/apps/bin/meetingmind-asr

# 4. Job directories
install -d -m 1777 /scratch/meetingmind

# 4b. Lab settings handed to clients by `meetingmind-asr lab` (used by `meetingmind cloud login`).
#     The real values live only here on the server — the skill itself is published.
cat > "$APP/lab.json" <<'JSON'
{"cloud_url": "https://<the lab's Nextcloud>", "cloud_folder": "/<shared folder>/meetings"}
JSON
chown root:bioadm "$APP/lab.json"; chmod 664 "$APP/lab.json"

# 5. Check, as a normal user (not root): "ok": true
/opt/bio/apps/bin/meetingmind-asr doctor
```

## Update

- **Code** (`server.py`, `transcribe.py` changed in a skill release): wait until
  `squeue -n mm-asr` is empty, then repeat step 3. If `PROTOCOL` changed, clients
  on the old skill are refused with a clear message until they update.
- **Environment**: edit `pixi.toml` here first, copy it over, `pixi install`.
- **Model**: a new revision is a new `bio-admin db-add` version; `current` moves.
  Keep local installs on the same revision, or transcripts will differ.

## Job sizing

Measured 2026-10-23 (a 33-minute meeting, 3-minute chunks): 2 min 55 s on the
GPU (about 5 s per minute of audio, model load 10 s), one CPU core busy, host
memory 6.5 GB plus 7.2 GB CUDA reserved, flat per chunk. `server.py` asks for
`-c 2 --mem=20G` and `--time = 10 min + 0.25 × audio minutes` (≥ 15 min), and
accepts only what that budget covers: chunks of at most 5 minutes, at most 8 hours
of audio per file.

## Where meeting audio can be, and for how long

- The job's own directory, while it waits and runs. `run` deletes the audio in a
  `finally`; the job script's EXIT/TERM trap deletes it (and the chunk WAVs in
  the job's `tmp/`, which is its TMPDIR) when Slurm stops the job at its time
  limit or on scancel. Only a SIGKILL without the TERM before it (the kernel
  OOM killer) can skip both.
- Text (partial transcript, result, log) stays until the client fetches it and
  cleans the directory, or until the user's next `new-job` prunes directories
  older than 7 days. A user who never comes back leaves those behind; a weekly
  timer like `nxf-work-clean` would bound that, if wanted.
