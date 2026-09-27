#!/usr/bin/env python3
"""Fine-tune a Laya checkpoint on THIS project's tool-family decision.

WHY THIS FILE EXISTS.

`ci/laya_bakeoff.py` measured the zero-shot model and said no: top-1 62% on the
authored set, `none` swallowing imperatives, and a confidence that saturates on
exactly the cases it gets wrong (this pass found one mechanical reason — the
checkpoint calibrates by OPTION COUNT, and this task's 12-option question lands
in its widest bucket, `choice:11+`, at a temperature of 0.10). Laya's own README
says the value is in fine-tuning, and its runtime ships the pieces for it:
`common.build_sequence`, `DecisionModel.forward(detach_encoder=…)` and a strictly
proper scoring rule (`proper_reward`). So this is the fine-tune the harness was
built to grade, using the library's own code path rather than a reimplementation
— the sequence format, the option rendering and the head are exactly what the
runtime will use, which is the only way the grade means anything.

WHAT IT TRAINS, AND HOW THE CLAIM IS KEPT HONEST.

The task is one `choice` question over the harness's own family table, worded by
the harness's own `questions()` — imported, not restated — and the label of a row
is the family the app's real model reached for (authored rows are labelled by
hand; mined rows by the tool the real turn called). Three stages, because they
cost different things and overfit differently:

  * `head`   — the encoder is frozen and detached; only the decision head learns.
               With a corpus this small this is the honest first try.
  * `last:N` — the head plus the encoder's last N layers, at a lower LR.
  * `full`   — everything. Probably overfitting on 70 rows; measured anyway.

Every number is reported by STRATIFIED CROSS-VALIDATION, and the zero-shot model
is scored on the SAME folds, so the headline is a paired delta rather than two
unrelated accuracies. All metrics are taken on the RAW logits (temperature 1):
ranking is unaffected by a positive scale, so leaving the checkpoint's table out
of the comparison keeps the calibration finding separate from the accuracy one.
A temperature is then fitted on the training split of each fold and reported —
against the checkpoint's own value for the bucket — because "when may the
assistant act on this" is the question the table is supposed to answer.

`--save` writes a COMPLETE, loadable checkpoint (`laya.load(dir)`) beside the
app's state directory: the base config and tokenizer, the fine-tuned weights, and
a record naming the corpus hash it was trained on, so a later run can tell
whether the corpus moved under it. Nothing is written into this checkout.

Usage (the interpreter that has laya):
    ~/.local/share/pipx/venvs/laya/bin/python ci/laya_finetune.py --folds 5
    ... --stage head --checkout . --real ~/.config/handsoff --grow
    ... --stage head --save --out /tmp/finetune.json
Then grade it end to end with the harness:
    ~/.local/share/pipx/venvs/laya/bin/python ci/laya_bakeoff.py \
        --checkpoint ~/.local/state/handsoff/laya-finetune/<run>
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import pathlib
import random
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import laya_bakeoff as bakeoff  # noqa: E402
import laya_corpus as corpus  # noqa: E402

#: where a saved checkpoint goes: the app's state dir, never the checkout
DEFAULT_OUT_DIR = pathlib.Path.home() / ".local" / "state" / "handsoff" / "laya-finetune"
BASE_REPO = "convaiinnovations/laya"
BASE_SUBFOLDER = "typed-decisions"
#: refuse to train on the card below this much free VRAM unless told otherwise
MIN_FREE_VRAM_MIB = 2500
#: the action the head should reach for: index 0 is "answer directly", the
#: index-1 action costs 0.5 (cfg act_costs ["escalate"]) and routing is not a
#: reason to escalate, so the auxiliary action loss points at 0.
ACT_TARGET = 0
ACT_WEIGHT = 0.2


# --- corpus -> model items ---------------------------------------------------

def family_keys() -> list[str]:
    """The option order the runtime will use: the harness's own key order.

    `render_options` renders a choice's criteria in dict order, and
    `system_one` reads the answer back with `list(crit.keys())`, so the training
    target's index must be THIS order and nothing else.
    """
    return list(corpus.families().keys())


def question_internal():
    """The harness's question, in the runtime's internal form.

    Converted with the runtime's own `Agent._to_internal`, so the wording the
    model is trained on and the wording it is graded on cannot drift apart.
    """
    import laya

    return laya.Agent._to_internal(bakeoff.questions(strict=False)["family"])


def make_items(agent, rows: list[dict]) -> list[dict]:
    """One training item per row: same sequence builder the runtime uses."""
    keys = family_keys()
    q = question_internal()
    max_len = int(agent.cfg.get("max_len", 1024))
    head_max_len = int(agent.cfg.get("head_max_len", 256))
    items = []
    for row in rows:
        family = row["family"]
        if family not in keys:
            continue
        ids, markers = bakeoff_sequence(agent, row["text"], q, max_len, head_max_len)
        if len(markers) != len(keys):
            raise ValueError(f"option markers ({len(markers)}) != families ({len(keys)})")
        target = [0.0] * len(keys)
        target[keys.index(family)] = 1.0
        items.append({"ids": ids, "markers": markers,
                      "qtype": 0,                 # QTYPES["choice"]
                      "target": target, "text": row["text"], "family": family})
    return items


def bakeoff_sequence(agent, text: str, q: dict, max_len: int, head_max_len: int):
    """build_sequence, from the library, with the state an utterance makes."""
    from laya.common import build_sequence

    return build_sequence(agent.tok, {"utterance": text}, q,
                          max_len=max_len, head_max_len=head_max_len)


# --- batches, forward, metrics ----------------------------------------------

def batch_of(items: list[dict], agent, device):
    from laya.common import collate_items

    b = collate_items([[it] for it in items], agent.tok.pad_token_id)
    return {k: (v.to(device) if hasattr(v, "to") else v) for k, v in b.items()}


def forward(model, b, device, dtype, detach_encoder: bool):
    import torch

    with torch.autocast(device_type=device.type, dtype=dtype,
                        enabled=device.type == "cuda"):
        logits, act = model(b["input_ids"], b["attention_mask"], b["marker_pos"],
                            b["marker_mask"], b["qtype"],
                            detach_encoder=detach_encoder)
    return logits, act


def evaluate(model, items: list[dict], agent, device, dtype, batch_size: int = 8,
             temperature: float = 1.0) -> dict:
    """Top-1, recall@3, log-score and calibration at a given temperature.

    `temperature` scales the logits before the softmax, which is what the
    runtime's own table does. A positive scale cannot move the ranking, so
    top-1 and recall@k are the same at every temperature — that is the point of
    reporting both: the DECISION is temperature-free, the CONFIDENCE is not.
    Passing the shipped value reproduces what a turn would actually see.
    """
    import numpy as np
    import torch

    from laya.common import confidence_from_probs, ece_score, proper_reward

    keys = family_keys()
    k = len(keys)
    model.eval()
    picks, confs, correct, log_scores, ranks, act0 = [], [], [], [], [], []
    with torch.no_grad():
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            b = batch_of(chunk, agent, device)
            logits, act = forward(model, b, device, dtype, detach_encoder=True)
            p = torch.softmax(logits.float() / max(1e-3, temperature), -1).cpu().numpy()
            rew = proper_reward(torch.tensor(p), b["target"].cpu(),
                                b["qtype"].cpu(), b["marker_mask"].cpu())
            log_scores.extend([float(v) for v in rew])
            act0.extend([float(v) for v in torch.softmax(act.float(), -1).cpu().numpy()[:, 0]])
            for row, probs in zip(chunk, p, strict=True):
                order = list(np.argsort(-probs))
                want = keys.index(row["family"])
                picks.append(keys[int(order[0])])
                ranks.append(int(order.index(want)) + 1)
                correct.append(int(order[0]) == want)
                confs.append(confidence_from_probs(probs, k))
    np_conf = np.asarray(confs, dtype=float)
    np_cor = np.asarray(correct, dtype=float)
    return {
        "n": len(items),
        "top1": sum(correct),
        "top1_pct": (sum(correct) / len(items)) if items else 0.0,
        "recall3": sum(1 for r in ranks if r <= 3),
        "recall3_pct": (sum(1 for r in ranks if r <= 3) / len(items)) if items else 0.0,
        "log_score": float(sum(log_scores) / len(log_scores)) if log_scores else 0.0,
        "conf_p50": float(sorted(np_conf)[len(np_conf) // 2]) if len(np_conf) else 0.0,
        "ece": float(ece_score(np_conf, np_cor)),
        "act0_mean": float(sum(act0) / len(act0)) if act0 else 0.0,
        "temperature": temperature,
        "picks": picks, "ranks": ranks, "probs_conf": [float(c) for c in confs],
    }


def fit_temperature(model, items, agent, device, dtype, batch_size: int = 8) -> float:
    """The scale that makes this task's probabilities mean what they say.

    Fitted by maximising the proper scoring rule's log-score on the data it is
    given (a fold's TRAINING split — never the fold it is reported on), over a
    log-spaced grid. The checkpoint ships a table keyed by option count; the
    fitted value is what THIS task's 12-option question actually wants, which is
    the difference between a confident answer and a calibrated one.
    """
    import numpy as np
    import torch

    from laya.common import proper_reward

    model.eval()
    logits_all, targets, masks, qtypes = [], [], [], []
    with torch.no_grad():
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            b = batch_of(chunk, agent, device)
            lg, _ = forward(model, b, device, dtype, detach_encoder=True)
            logits_all.append(lg.float().cpu())
            targets.append(b["target"].cpu())
            masks.append(b["marker_mask"].cpu())
            qtypes.append(b["qtype"].cpu())
    if not logits_all:
        return 1.0
    logits = torch.cat(logits_all)
    target = torch.cat(targets)
    mask = torch.cat(masks)
    qtype = torch.cat(qtypes)
    best, best_score = 1.0, -1e9
    for temp in np.exp(np.linspace(np.log(0.03), np.log(8.0), 60)):
        p = torch.softmax(logits / float(temp), -1)
        score = float(proper_reward(p, target, qtype, mask).mean())
        if score > best_score:
            best, best_score = float(temp), score
    return round(best, 4)


# --- training ----------------------------------------------------------------

def train(model, items, agent, device, dtype, cfg: dict) -> dict:
    """One training run. Returns the history (mean reward per epoch)."""
    import torch

    from laya.common import proper_reward

    trainable = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(trainable, lr=cfg["lr"],
                            weight_decay=cfg.get("weight_decay", 0.01))
    rng = random.Random(cfg.get("seed", 0))
    history = []
    for epoch in range(cfg["epochs"]):
        order = list(range(len(items)))
        rng.shuffle(order)
        model.train()
        epoch_rewards, epoch_act = [], []
        for start in range(0, len(order), cfg["batch_size"]):
            chunk = [items[i] for i in order[start:start + cfg["batch_size"]]]
            if not chunk:
                continue
            b = batch_of(chunk, agent, device)
            logits, act = forward(model, b, device, dtype,
                                  detach_encoder=cfg["stage"] == "head")
            p = torch.softmax(logits.float(), -1)
            reward = proper_reward(p, b["target"].float(), b["qtype"], b["marker_mask"])
            act_loss = torch.nn.functional.cross_entropy(
                act.float(), torch.full((act.size(0),), ACT_TARGET,
                                        dtype=torch.long, device=device))
            loss = -reward.mean() + ACT_WEIGHT * act_loss
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            opt.step()
            epoch_rewards.append(float(reward.mean()))
            epoch_act.append(float(act_loss))
        history.append({"epoch": epoch + 1,
                        "reward": sum(epoch_rewards) / len(epoch_rewards),
                        "act_loss": sum(epoch_act) / len(epoch_act)})
    return {"history": history, "params": sum(p.numel() for p in trainable)}


def prepare_model(pristine, stage: str, unfreeze_last: int):
    """Freeze what the stage says to freeze, on a COPY of the pristine model."""
    model = copy.deepcopy(pristine)
    for p in model.parameters():
        p.requires_grad_(True)
    if stage == "head":
        for p in model.encoder.parameters():
            p.requires_grad_(False)
    elif stage == "last":
        for p in model.encoder.parameters():
            p.requires_grad_(False)
        layers = getattr(model.encoder, "layers", None)
        if layers is None:
            raise SystemExit("this encoder exposes no .layers to unfreeze")
        chosen = list(layers)[-max(1, unfreeze_last):]
        for layer in chosen:
            for p in layer.parameters():
                p.requires_grad_(True)
        for attr in ("final_norm", "norm"):
            mod = getattr(model.encoder, attr, None)
            if mod is not None:
                for p in mod.parameters():
                    p.requires_grad_(True)
    return model


def stratified_folds(rows: list[dict], k: int, seed: int) -> list[list[int]]:
    """Indices per fold, round-robin within each family: every fold sees every
    label, which matters when some families have four rows and some have one."""
    by_family: dict = {}
    for i, row in enumerate(rows):
        by_family.setdefault(row["family"], []).append(i)
    for group in by_family.values():
        group.sort()
    folds: list[list[int]] = [[] for _ in range(k)]
    rng = random.Random(seed)
    for family in sorted(by_family):
        group = list(by_family[family])
        rng.shuffle(group)
        for j, idx in enumerate(group):
            folds[j % k].append(idx)
    return folds


# --- saving ------------------------------------------------------------------

def base_checkpoint_dir() -> pathlib.Path:
    """The cached base checkpoint directory (offline; it is already here)."""
    from huggingface_hub import snapshot_download

    root = snapshot_download(BASE_REPO, allow_patterns=[f"{BASE_SUBFOLDER}/*"])
    return pathlib.Path(root) / BASE_SUBFOLDER


def save_checkpoint(model, agent, out_dir: pathlib.Path, record: dict,
                    fitted_temp: float, bucket: str) -> pathlib.Path:
    """A complete, loadable checkpoint — `laya.load(that_dir)` must just work."""
    from safetensors.torch import save_file

    src = base_checkpoint_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("encoder", "tokenizer"):
        target = out_dir / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src / name, target)
    cfg = json.loads((src / "rl_agent_config.json").read_text(encoding="utf-8"))
    cfg["fine_tuned"] = True
    cfg.setdefault("temperature_by_options", {})[bucket] = fitted_temp
    cfg["handsoff"] = record
    (out_dir / "rl_agent_config.json").write_text(
        json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    state = {k: v.detach().to("cpu").contiguous()
             for k, v in model.state_dict().items()}
    save_file(state, str(out_dir / "model.safetensors"))
    return out_dir


# --- main --------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--stage", default="head", choices=("head", "last", "full"),
                        help="what may learn: the head, the head + last N encoder "
                             "layers, or everything")
    parser.add_argument("--unfreeze-last", type=int, default=4,
                        help="encoder layers to unfreeze when --stage last")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=None,
                        help="defaults: 3e-4 for the head, 5e-5 when the encoder moves")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--checkout", type=pathlib.Path, default=HERE.parent)
    parser.add_argument("--real", type=pathlib.Path, default=None,
                        help="directory of history.json* files to mine (private)")
    parser.add_argument("--store", type=pathlib.Path, default=corpus.DEFAULT_STORE)
    parser.add_argument("--grow", action="store_true",
                        help="mine the history into the corpus store first")
    parser.add_argument("--save", action="store_true",
                        help="train on the whole corpus and write a checkpoint dir")
    parser.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="write the run record (JSON) here")
    parser.add_argument("--allow-low-vram", action="store_true")
    parser.add_argument("--save-holdout", type=int, default=0,
                        help="with --save: train on every fold EXCEPT this one and "
                             "save that model, so the harness can grade rows it "
                             "never saw (write them with --out-rows)")
    parser.add_argument("--out-rows", type=pathlib.Path, default=None,
                        help="write the held-out fold's rows here (JSONL)")
    args = parser.parse_args(argv[1:])

    try:
        import numpy as np  # noqa: F401
        import torch

        import laya
    except ImportError as exc:
        print(f"laya/torch is not importable here ({exc}).")
        print("  ~/.local/share/pipx/venvs/laya/bin/python ci/laya_finetune.py …")
        return 2

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    if args.device == "cuda" and device.type != "cuda":
        print("cuda was asked for and is not available; using cpu")
    if device.type == "cuda":
        free = torch.cuda.mem_get_info()[0] // 2**20
        used = torch.cuda.memory_allocated() // 2**20
        print(f"vram: {free} MiB free, {used} MiB held by this process")
        if free < MIN_FREE_VRAM_MIB and not args.allow_low_vram:
            print(f"REFUSED: under {MIN_FREE_VRAM_MIB} MiB free — the running bubble "
                  f"holds models on this card and a training run that OOMs it is a "
                  f"worse trade than waiting. Use --allow-low-vram to override.")
            return 3

    built = corpus.build(real_dir=args.real, store_path=args.store, grow=args.grow)
    rows = built["rows"]
    print(f"corpus: {built['counts']['total']} rows {built['counts']['by_source']}, "
          f"hash {built['hash'][:16]}…")
    check = corpus.label_check(args.checkout.resolve())
    if check.get("unmapped"):
        print(f"  belt tools in NO family: {', '.join(check['unmapped'])}")
    if len(rows) < 20:
        print(f"REFUSED: {len(rows)} rows is not a corpus")
        return 1

    t0 = time.perf_counter()
    agent = laya.load(BASE_REPO, subfolder=BASE_SUBFOLDER, device=str(device))
    print(f"base checkpoint loaded in {time.perf_counter() - t0:.1f}s "
          f"({sum(p.numel() for p in agent.model.parameters()) / 1e6:.0f}M params)")
    dtype = getattr(agent, "dtype", None) or (
        torch.bfloat16 if str(agent.cfg.get("amp_dtype")) == "bf16" else torch.float16)
    pristine = copy.deepcopy(agent.model)
    items = make_items(agent, rows)
    keys = family_keys()
    bucket = f"choice:{'2' if len(keys) <= 2 else '3-5' if len(keys) <= 5 else '6-10' if len(keys) <= 10 else '11+'}"
    shipped_temp = float((agent.cfg.get("temperature_by_options") or {}).get(bucket, 1.0))
    print(f"options: {len(keys)} -> temperature bucket {bucket} "
          f"(checkpoint says {shipped_temp:.4g})")

    lr = args.lr if args.lr is not None else (3e-4 if args.stage == "head" else 5e-5)
    # Seeded, because an unseeded run of this file produced 57% and then 66% on
    # the same folds: with a corpus this size the fine-tune's effect has to be
    # compared against its own seed spread, and that needs runs to repeat.
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    folds = stratified_folds(rows, max(2, args.folds), args.seed)
    base_metrics, tuned_metrics, fitted = [], [], []
    base_shipped, base_fitted_m, tuned_fitted_m = [], [], []
    record_folds = []
    print(f"\n=== {args.folds}-fold stratified CV (paired: same folds both ways) ===")
    for i, fold_idx in enumerate(folds):
        val = [items[j] for j in fold_idx]
        train_idx = [j for j in range(len(items)) if j not in set(fold_idx)]
        train_items = [items[j] for j in train_idx]
        b_base = evaluate(pristine, val, agent, device, dtype)
        base_metrics.append(b_base)
        # the same zero-shot model, seen the way a turn would see it (the
        # checkpoint's own table) and the way our own corpus says it should be
        b_shipped = evaluate(pristine, val, agent, device, dtype,
                             temperature=shipped_temp)
        base_shipped.append(b_shipped)
        temp_base = fit_temperature(pristine, train_items, agent, device, dtype)
        base_fitted_m.append(evaluate(pristine, val, agent, device, dtype,
                                      temperature=temp_base))
        torch.manual_seed(args.seed + i)
        torch.cuda.manual_seed_all(args.seed + i)
        model = prepare_model(pristine, args.stage, args.unfreeze_last).to(device)
        info = train(model, train_items, agent, device, dtype,
                     {"stage": args.stage, "lr": lr, "epochs": args.epochs,
                      "batch_size": args.batch_size, "seed": args.seed + i})
        t_eval = evaluate(model, val, agent, device, dtype)
        temp = fit_temperature(model, train_items, agent, device, dtype)
        t_fitted = evaluate(model, val, agent, device, dtype, temperature=temp)
        fitted.append(temp)
        tuned_metrics.append(t_eval)
        tuned_fitted_m.append(t_fitted)
        assert t_eval["top1"] == t_fitted["top1"], (
            "temperature moved the ranking — it can only scale probabilities")
        record_folds.append({
            "fold": i + 1, "train": len(train_items), "val": len(val),
            "baseline_top1": b_base["top1"], "tuned_top1": t_eval["top1"],
            "baseline_log_score": round(b_base["log_score"], 4),
            "tuned_log_score": round(t_eval["log_score"], 4),
            "shipped_temp_log_score": round(b_shipped["log_score"], 4),
            "baseline_fitted_temp": temp_base,
            "baseline_fitted_log_score": round(base_fitted_m[-1]["log_score"], 4),
            "fitted_temperature": temp, "trainable_params": info["params"],
        })
        print(f"  fold {i + 1}: n={len(val):>2}  zero-shot {b_base['top1']}/{len(val)}"
              f"  fine-tuned {t_eval['top1']}/{len(val)}"
              f"  log-score {b_base['log_score']:+.3f} -> {t_eval['log_score']:+.3f}"
              f"  T*={temp:g}")
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()

    counts = collections_counter(rows)
    majority_family, majority_n = counts.most_common(1)[0]

    def summarise(label, per_fold, key):
        """Per-fold mean, min and max. With a corpus this small the SPREAD is
        the honest part of the number: a mean alone reads as a fact."""
        vals = [m[key] / m["n"] for m in per_fold]
        return (f"  {label:<14} {sum(vals) / len(vals):>4.0%} mean, "
                f"min {min(vals):>4.0%}, max {max(vals):>4.0%}")

    print(f"\n=== accuracy over {len(folds)} folds (paired: same folds, both ways) ===")
    print(summarise("zero-shot", base_metrics, "top1"))
    print(summarise(f"fine-tuned ({args.stage})", tuned_metrics, "top1"))
    print(f"  {'majority':<14} {majority_n / len(rows):>4.0%}     "
          f"(always answering {majority_family})")
    print(summarise("zero-shot r@3", base_metrics, "recall3"))
    print(summarise("fine-tuned r@3", tuned_metrics, "recall3"))
    mean = lambda rows_, key: sum(m[key] for m in rows_) / len(rows_)  # noqa: E731
    print(f"  log-score    zero-shot {mean(base_metrics, 'log_score'):+.3f}"
          f"  fine-tuned {mean(tuned_metrics, 'log_score'):+.3f}"
          f"   (raw logits, temperature 1)")
    print("\n=== calibration: what the temperature table does to the SAME decisions ===")
    print(f"  ECE  zero-shot raw (T=1)      {mean(base_metrics, 'ece'):.3f}")
    print(f"  ECE  zero-shot as SHIPPED     {mean(base_shipped, 'ece'):.3f}"
          f"   (table says {shipped_temp:.4g} for {bucket})")
    print(f"  ECE  zero-shot with T* fitted {mean(base_fitted_m, 'ece'):.3f}")
    print(f"  ECE  fine-tuned raw (T=1)     {mean(tuned_metrics, 'ece'):.3f}")
    print(f"  ECE  fine-tuned with T*       {mean(tuned_fitted_m, 'ece'):.3f}")
    print(f"  log-score as SHIPPED {mean(base_shipped, 'log_score'):+.3f}"
          f"  vs T*=1 {mean(base_metrics, 'log_score'):+.3f}"
          f"  vs fitted {mean(base_fitted_m, 'log_score'):+.3f}")
    print(f"\n=== fitted temperature for {bucket} ===")
    print(f"  zero-shot folds: {', '.join(f'{t:g}' for t in [f['baseline_fitted_temp'] for f in record_folds])}")
    print(f"  fine-tuned folds: {', '.join(f'{t:g}' for t in fitted)}"
          f"   checkpoint's own table: {shipped_temp:.4g}")
    fitted_final = sorted(fitted)[len(fitted) // 2] if fitted else 1.0

    record = {
        "when": datetime.datetime.now().isoformat(timespec="seconds"),
        "base": f"{BASE_REPO}/{BASE_SUBFOLDER}",
        "stage": args.stage, "unfreeze_last": args.unfreeze_last if args.stage == "last" else 0,
        "lr": lr, "epochs": args.epochs, "batch_size": args.batch_size,
        "device": str(device), "dtype": str(dtype).replace("torch.", ""),
        "corpus": {"hash": built["hash"], "counts": built["counts"]},
        "label_check": {k: v for k, v in check.items() if k != "family_gates"},
        "folds": record_folds,
        "summary": {
            "baseline_top1": sum(m["top1"] for m in base_metrics) / sum(m["n"] for m in base_metrics),
            "tuned_top1": sum(m["top1"] for m in tuned_metrics) / sum(m["n"] for m in tuned_metrics),
            "baseline_recall3": sum(m["recall3"] for m in base_metrics) / sum(m["n"] for m in base_metrics),
            "tuned_recall3": sum(m["recall3"] for m in tuned_metrics) / sum(m["n"] for m in tuned_metrics),
            "baseline_log_score": sum(m["log_score"] for m in base_metrics) / len(base_metrics),
            "tuned_log_score": sum(m["log_score"] for m in tuned_metrics) / len(tuned_metrics),
            "baseline_ece": sum(m["ece"] for m in base_metrics) / len(base_metrics),
            "tuned_ece": sum(m["ece"] for m in tuned_metrics) / len(tuned_metrics),
            "fitted_temperature_median": fitted_final,
            "baseline_fitted_temperature_median": sorted(
                f["baseline_fitted_temp"] for f in record_folds)[len(record_folds) // 2],
            "baseline_ece_as_shipped": mean(base_shipped, "ece"),
            "baseline_ece_fitted": mean(base_fitted_m, "ece"),
            "tuned_ece_fitted": mean(tuned_fitted_m, "ece"),
            "temperature_moves_ranking": False,
            "bucket": bucket,
            "checkpoint_temperature": shipped_temp,
            "majority_family": collections_counter(rows).most_common(1)[0][0],
            "majority_pct": max(collections_counter(rows).values()) / len(rows),
        },
    }

    if args.save:
        hold = args.save_holdout
        if hold:
            if not 1 <= hold <= len(folds):
                print(f"REFUSED: --save-holdout must be 1..{len(folds)}")
                return 1
            keep = set(folds[hold - 1])
            train_set = [items[j] for j in range(len(items)) if j not in keep]
            if args.out_rows:
                held_out = [rows[j] for j in sorted(keep)]
                args.out_rows.write_text(
                    "".join(json.dumps({"text": r["text"], "family": r["family"],
                                        "source": r.get("source") or "holdout"},
                                       ensure_ascii=False) + "\n"
                            for r in held_out), encoding="utf-8")
                print(f"held-out fold {hold}: {len(held_out)} rows -> {args.out_rows}"
                      f" (graded by the harness, never trained on)")
        else:
            train_set = items
        torch.manual_seed(args.seed + 100)
        torch.cuda.manual_seed_all(args.seed + 100)
        model = prepare_model(pristine, args.stage, args.unfreeze_last).to(device)
        info = train(model, train_set, agent, device, dtype,
                     {"stage": args.stage, "lr": lr, "epochs": args.epochs,
                      "batch_size": args.batch_size, "seed": args.seed})
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = args.out_dir / (f"{stamp}-{args.stage}"
                                 + (f"-holdout{hold}" if hold else ""))
        record["trained_on"] = len(train_set)
        record["holdout_fold"] = hold
        # What this checkpoint was BUILT from, so a directory of saved
        # fine-tunes is traceable back to a base model instead of only to a
        # timestamp. It sat here half-written for a while: a `rev = ""` that
        # nothing ever read, and an import of `scan_cache_dir` marked unused
        # because the thing it was imported FOR was never written down. Read
        # from the local hub cache, and treat a cache that cannot be read as
        # absent rather than as a failure — the training has already happened
        # by this line, and provenance is a nicety, not a gate.
        try:
            from huggingface_hub import scan_cache_dir
            revs = sorted(f"{r.repo_id}@{r.revision}"
                          for r in scan_cache_dir().repos)
        except Exception:  # noqa: BLE001 - see above: never fails a save
            revs = []
        if revs:
            record["hub_revisions"] = revs
        record["trainable_params"] = info["params"]
        record["final_history"] = info["history"]
        record["saved_to"] = str(out_dir)
        save_checkpoint(model, agent, out_dir, record, fitted_final, bucket)
        print(f"\nsaved a loadable checkpoint: {out_dir}")
        print(f"  grade it: python ci/laya_bakeoff.py --checkpoint {out_dir} "
              f"--checkout ." + (f" --grown {args.out_rows}" if args.out_rows else ""))

    if args.out:
        args.out.write_text(json.dumps(record, indent=1), encoding="utf-8")
        print(f"run record: {args.out}")
    return 0


def collections_counter(rows: list[dict]):
    import collections

    return collections.Counter(row["family"] for row in rows)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
