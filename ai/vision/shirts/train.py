"""
Trains the shirt-number model on synthetic players (synth.py) and exports it
to ONNX for matchvision/shirts.py. It runs as an Azure Machine Learning job
(job.yml) or anywhere with PyTorch:

    python3 train.py --out outputs/                  # the full run: about 2 hours on 8 CPUs
    python3 train.py --out /tmp/t --epochs 1 --per-epoch 512 --no-pretrained   # a quick check

The samples are drawn as the training goes (no dataset to upload), each data
loader worker with its own seed. The model is a ResNet-18 (ImageNet weights,
so real shirts are less of a jump) with one output per question shirts.py
asks: how many digits, the first, the second.

Each epoch it's measured on two fixed sets the training never sees: players
in the training's fonts, and players in two fonts it never trains on (a new
kit's numbers). On each: of the legible numbers, how many it reads right
(read), reads wrong (misread) or leaves (unread); and of the illegible ones,
how many it reads anyway (invented), the error that matters most. In Azure
ML these go to the run's metrics (MLflow).

The output folder gets shirt_numbers.onnx, with what it was trained on and its
results in its metadata, and metrics.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, IterableDataset, get_worker_info

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))           # matchvision, for prepare() and decode()
sys.path.insert(0, str(HERE))

from matchvision import shirts  # noqa: E402
import synth  # noqa: E402

MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
THRESHOLDS = (0.5, 0.7, 0.8, 0.9)


class Synthetic(IterableDataset):
    """per_epoch samples an epoch, split among the workers, each seeded by (seed, epoch, worker)."""

    def __init__(self, fonts: list[Path], per_epoch: int, seed: int) -> None:
        self.fonts, self.per_epoch, self.seed, self.epoch = fonts, per_epoch, seed, 0

    def __iter__(self):
        info = get_worker_info()
        worker, workers = (info.id, info.num_workers) if info else (0, 1)
        players = synth.Players(self.fonts, seed=hash((self.seed, self.epoch, worker)) % 2**32)
        for _ in range(self.per_epoch // workers):
            s = players.sample()
            yield torch.from_numpy(shirts.prepare(s.image)), torch.tensor(shirts.target(s.number))


def batches_per_epoch(per_epoch: int, batch: int, workers: int) -> int:
    """Each worker draws its share and batches it on its own, so its last batch may be short."""
    workers = max(1, workers)
    return workers * math.ceil((per_epoch // workers) / batch)


def fixed(fonts: list[Path], n: int, seed: int) -> tuple[torch.Tensor, list]:
    players = synth.Players(fonts, seed=seed)
    samples = [players.sample() for _ in range(n)]
    return torch.from_numpy(np.stack([shirts.prepare(s.image) for s in samples])), [s.number for s in samples]


class Reader(nn.Module):
    """RGB 0–1 in, shirts.OUTPUTS logits out; the normalisation inside, so the ONNX file needs nothing else."""

    def __init__(self, pretrained: bool) -> None:
        super().__init__()
        from torchvision.models import ResNet18_Weights, resnet18

        self.net = resnet18(weights=ResNet18_Weights.DEFAULT if pretrained else None)
        self.net.fc = nn.Linear(self.net.fc.in_features, shirts.OUTPUTS)
        self.register_buffer("mean", torch.tensor(MEAN).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor(STD).view(1, 3, 1, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net((x - self.mean) / self.std)


def loss_of(logits: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    L, D = shirts.LENGTHS, shirts.DIGITS
    loss = F.cross_entropy(logits[:, :L], y[:, 0])
    for i, start in ((1, L), (2, L + D)):
        if (y[:, i] >= 0).any():
            loss = loss + F.cross_entropy(logits[:, start:start + D], y[:, i], ignore_index=-1)
    return loss


@torch.no_grad()
def measure(model: nn.Module, images: torch.Tensor, labels: list, batch: int = 256) -> dict:
    model.eval()
    logits = torch.cat([model(images[i:i + batch]) for i in range(0, len(images), batch)]).numpy()
    out = {}
    for t in THRESHOLDS:
        read = misread = unread = invented = 0
        for row, label in zip(logits, labels):
            got, _ = shirts.decode(row, t)
            if label is None:
                invented += got is not None
            elif got is None:
                unread += 1
            elif got == label:
                read += 1
            else:
                misread += 1
        legible = sum(label is not None for label in labels)
        out[t] = {"read": read / max(1, legible), "misread": misread / max(1, legible), "unread": unread / max(1, legible),
                  "invented": invented / max(1, len(labels) - legible), "legible": legible, "illegible": len(labels) - legible}
    return out


def export(model: nn.Module, path: Path, about: dict) -> None:
    import onnx

    model.eval()
    dummy = torch.rand(1, 3, shirts.SIZE, shirts.SIZE)
    torch.onnx.export(model, (dummy,), str(path), input_names=["image"], output_names=["logits"],
                      dynamic_shapes={"x": {0: torch.export.Dim("batch", min=1, max=1024)}}, dynamo=True,
                      external_data=False)
    m = onnx.load(str(path))
    entry = m.metadata_props.add()
    entry.key, entry.value = "shirts", json.dumps(about)
    onnx.save(m, str(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("outputs"))
    parser.add_argument("--epochs", type=int, default=16)
    parser.add_argument("--per-epoch", type=int, default=24000, help="samples an epoch")
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-3)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--val", type=int, default=2000, help="samples in each fixed test set")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fonts", type=Path, default=Path.home() / ".cache" / "shirt-fonts")
    parser.add_argument("--no-pretrained", action="store_true", help="start from random weights (no download)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(args.seed)

    try:
        import mlflow
    except ImportError:
        mlflow = None
    if mlflow and not os.environ.get("MLFLOW_TRACKING_URI"):
        mlflow = None                                   # only in Azure ML (or wherever a tracking server is set)
    if mlflow:
        mlflow.log_params({k: str(v) for k, v in vars(args).items()})

    train_fonts, new_fonts = synth.fonts(args.fonts), synth.fonts(args.fonts, held_out=True)
    print(f"fonts: {len(train_fonts)} to train on, {len(new_fonts)} held out", flush=True)
    data = Synthetic(train_fonts, args.per_epoch, args.seed)
    loader = DataLoader(data, batch_size=args.batch, num_workers=args.workers, persistent_workers=False)
    tests = {"seen_fonts": fixed(train_fonts, args.val, 10**6), "new_fonts": fixed(new_fonts, args.val, 2 * 10**6)}

    model = Reader(not args.no_pretrained)
    steps = args.epochs * batches_per_epoch(args.per_epoch, args.batch, args.workers)
    optimiser = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    schedule = torch.optim.lr_scheduler.OneCycleLR(optimiser, max_lr=args.lr, total_steps=steps, pct_start=0.15)

    results, best, started = {}, -1.0, time.time()
    for epoch in range(args.epochs):
        data.epoch = epoch
        model.train()
        total, n = 0.0, 0
        for x, y in loader:
            optimiser.zero_grad()
            loss = loss_of(model(x), y)
            loss.backward()
            optimiser.step()
            schedule.step()
            total, n = total + loss.item() * len(x), n + len(x)
        results = {name: measure(model, *test) for name, test in tests.items()}
        at = results["new_fonts"][shirts.THRESHOLD]
        line = {"loss": total / max(1, n), **{f"{name}_{k}": v for name, r in results.items()
                                               for k, v in r[shirts.THRESHOLD].items() if k not in ("legible", "illegible")}}
        print(f"epoch {epoch + 1}/{args.epochs} {time.time() - started:.0f}s " +
              " ".join(f"{k}={v:.3f}" for k, v in line.items()), flush=True)
        if mlflow:
            mlflow.log_metrics(line, step=epoch + 1)
        # Kept: the epoch that reads most new-font numbers while inventing the fewest
        score = at["read"] - 5 * at["invented"]
        if score > best:
            best = score
            torch.save(model.state_dict(), args.out / "best.pt")

    model.load_state_dict(torch.load(args.out / "best.pt"))
    results = {name: measure(model, *test) for name, test in tests.items()}
    about = {"size": shirts.SIZE, "band": list(shirts.BAND), "threshold": shirts.THRESHOLD, "outputs": shirts.OUTPUTS,
             "trained": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "epochs": args.epochs,
             "samples": args.epochs * args.per_epoch, "pretrained": not args.no_pretrained,
             "synthetic": {name: r[shirts.THRESHOLD] for name, r in results.items()}}
    export(model, args.out / "shirt_numbers.onnx", about)
    (args.out / "metrics.json").write_text(json.dumps({name: {str(t): v for t, v in r.items()} for name, r in results.items()}, indent=2))
    (args.out / "best.pt").unlink()
    if mlflow:
        mlflow.log_metrics({f"final_{name}_{k}": v for name, r in results.items()
                            for k, v in r[shirts.THRESHOLD].items() if k not in ("legible", "illegible")})
    print(json.dumps(about["synthetic"], indent=1))
    print(f"→ {args.out / 'shirt_numbers.onnx'}")


if __name__ == "__main__":
    main()
