# Shirt numbers on Azure Machine Learning

Our own shirt-number reader: a small network trained on **synthetic players** on an Azure ML CPU cluster, exported to **ONNX**, and run by the vision pipeline with ONNX Runtime in milliseconds a crop (`matchvision/shirts.py`). It's meant to replace the vision-language model (`gemma4:e2b` on Ollama, `matchvision/numbers.py`), which takes seconds a crop on CPU. It replaces it only if it reads more numbers on real footage and invents none it shouldn't: on the clips we used before, the vision-language model read 24 of the 30 numbers a person could read.

**Why synthetic.** A player's number is the one thing the kit changes completely between clubs and seasons: the colours, the font, the outline, the name above it, stripes or hoops. Synthetic players give the model every kit at once (the league's clubs' colours plus random ones, 16 open-licence block fonts) without labelling a frame. They also give it the hard cases with the right answer: a number turned round the side, a digit behind an arm, the front of the shirt, too few pixels. For those the label is *none*, so it learns to say so rather than guess.

| | |
|---|---|
| [`synth.py`](synth.py) | The synthetic players: `python3 synth.py --show 64 sheet.jpg` draws a contact sheet with each label (red: none, and why) |
| [`train.py`](train.py) | The training: a ResNet-18 (ImageNet weights) with three outputs (how many digits; the first; the second), samples drawn as it trains, measured every epoch on players in the training's fonts and in two fonts it never sees; the best epoch exported to `shirt_numbers.onnx` with its results in the file's metadata |
| [`job.yml`](job.yml), [`conda.yml`](conda.yml) | The Azure ML command job and its environment (CPU PyTorch, ONNX, MLflow for the run's metrics) |
| [`aml.sh`](aml.sh) | Submits the job, streams it, registers the model (`shirt-numbers`), downloads it to `ai/vision/models/` |
| [`real.py`](real.py) | Real players' crops from SoccerTrack v2 (CC BY 4.0), labelled with each player's number from the dataset's game state: the benchmark below |
| [`label.py`](label.py) | A local page to check those crops by eye: is the dataset's number legible there? (`checked.json`) |
| [`../matchvision/shirts.py`](../matchvision/shirts.py) | The reader: what the model sees (`prepare`, the band from the shoulders to the shorts) and how its answer is read (`decode`: a number only when it's 80% sure of it), shared with the training |
| [`../matchvision/numbers_eval.py`](../matchvision/numbers_eval.py) | Both readers on the same labelled crops: read, misread, unread, invented |

## Train it

The workspace and its cluster come from [`deploy/azure`](../../../deploy/azure) (`ml = true`, the default). Then:

```bash
ai/vision/shirts/aml.sh                        # about 2 hours on the cluster's 8 CPUs (16 epochs of 24,000 players)
EPOCHS=2 PER_EPOCH=2000 ai/vision/shirts/aml.sh   # a few minutes, to try the setup
```

Azure ML Studio shows the run's metrics as it trains (the read, misread and invented rates of both test sets, every epoch). The model is registered as `shirt-numbers` with a new version each run, and lands in `ai/vision/models/shirt_numbers.onnx` (git-ignored, like the other models).

Without Azure, the same script trains anywhere with PyTorch: `python3 train.py --out outputs/`.

## Is it better? On footage

The synthetic test sets say whether it learned; only real footage says whether it's better. The test is our own kickabout, filmed with numbered bibs and every player's consent:

```bash
cd ai/vision
python -m matchvision.numbers_eval cut ../../extras/clips/kickabout.mp4 crops/ --every 1   # players' crops, as the readers get them
# label crops/labels.json: each crop's number, or null where none is legible
python -m matchvision.numbers_eval compare crops/ --onnx models/shirt_numbers.onnx \
    --ollama-url http://localhost:11434 --ollama-model gemma4:e2b
```

It prints each reader's reads, misreads, unread and invented numbers, and whether the ONNX model can replace the vision-language model: more numbers read, no more invented, at most one more misread. If it can, analyse clips with `--numbers-onnx models/shirt_numbers.onnx` (or `NUMBERS_ONNX`).

## On real players: SoccerTrack v2

The kickabout isn't filmed yet, so the benchmark is real players from [SoccerTrack v2](https://huggingface.co/datasets/atomscott/soccertrack-v2) (CC BY 4.0: A. Scott, I. Uchida, K. Kuroda, Y. Kim, K. Fujii, arXiv:2508.01802): ten university matches filmed by a fixed panorama, whose game-state files give every player's number, frame by frame. The test is matches M7 and M9 (the dataset's own test split); the crops are cut as the pipeline cuts them, by our detector and `numbers.crop()`, only the views it would read (at least 90 px tall, standing, no one overlapping).

```bash
python3 ai/vision/shirts/real.py frames ai/vision/shirts/real/      # frames and who's in them (HF token; ~4 GB read, crops kept, frames deleted)
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp -e YOLO_CONFIG_DIR=/tmp \
    -v "$PWD/ai/vision:/work/ai/vision:ro" -v "$PWD/ai/vision/shirts/real:/real" \
    --entrypoint python brighton-vision:dev /work/ai/vision/shirts/real.py crops /real
python3 ai/vision/shirts/label.py ai/vision/shirts/real/test        # http://127.0.0.1:8765/: is each number legible?
cd ai/vision && python -m matchvision.numbers_eval compare shirts/real/test --labels checked.json \
    --onnx models/shirt_numbers.onnx --ollama-url http://localhost:11434 --ollama-model gemma4:e2b
```

The dataset's label is who the player is, not whether the number shows: a player side-on still has his. So the test's crops are checked by a person (`label.py`): the number when it's legible, none when it isn't. Only that makes the invented rate count.

**The result** (2026-10-26, 212 crops checked by hand: 124 with a legible number, 14 of them another player's than the dataset says, and 88 with none; CPU only):

| Reader | Read (of 124) | Misread | Left | Invented (of 88) | A crop |
|---|---|---|---|---|---|
| `gemma4:e2b` (vision-language, Ollama) | 89 (72%) | 13 | 22 | 11 | 4.8 s |
| `shirt_numbers.onnx`, synthetic, at 0.8 | 9 (7%) | 2 | 113 | 0 | 0.005 s |
| the same at 0.5 | 10 | 4 | 110 | 0 | 0.005 s |
| `train.py --real` (below) | 0 | 0 | 124 | 0 | 0.005 s |

So the vision-language model stays the reader. Ours never invents a number and is a thousand times faster, but on real players it's too unsure to say much: the synthetic players don't look enough like a panorama's small, blurred, side-lit shirts. Both misread the same way, a digit dropped or swapped for one that looks like it (77 read as 17, 16 as 15).

**Training on them doesn't work (yet).** `train.py --real ai/vision/shirts/real/` trains on the eight other matches' crops instead of synthetic players, keeping the epoch that reads most of the validation matches' (M2, M10). On 1,089 crops (58 numbers, 18 with fewer than 10 crops; about half not showing the number) it learned the players, not the digits: the training loss went to 0.02 while the validation matches' numbers were read 1% of the time and misread 12%, so the epoch it kept reads none. A few hundred players seen again and again, labelled by who they are, teach the model who they are. So the model stays trained on synthetic players (the pictures it learns from are never on screen) and real players are its test.

## Tests

`python3 tests/test_shirts.py`: what the model sees and how its answer is read (every number 1–99, none, unsure, never a leading zero), the synthetic players' labels and that a seed always draws the same player, the ONNX export read back by the reader with PyTorch's own logits, a model trained on another input refused, and the comparison's counts.
