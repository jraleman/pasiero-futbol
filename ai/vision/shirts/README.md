# Shirt numbers on Azure Machine Learning

Our own shirt-number reader: a small network trained on **synthetic players** on an Azure ML CPU cluster, exported to **ONNX**, and run by the vision pipeline with ONNX Runtime in milliseconds a crop (`matchvision/shirts.py`). It's meant to replace the vision-language model (`gemma4:e2b` on Ollama, `matchvision/numbers.py`), which takes seconds a crop on CPU. It replaces it only if it reads more numbers on real footage and invents none it shouldn't: on the clips we used before, the vision-language model read 24 of the 30 numbers a person could read.

**Why synthetic.** A player's number is the one thing the kit changes completely between clubs and seasons: the colours, the font, the outline, the name above it, stripes or hoops. Synthetic players give the model every kit at once (the league's clubs' colours plus random ones, 16 open-licence block fonts) without labelling a frame. They also give it the hard cases with the right answer: a number turned round the side, a digit behind an arm, the front of the shirt, too few pixels. For those the label is *none*, so it learns to say so rather than guess.

| | |
|---|---|
| [`synth.py`](synth.py) | The synthetic players: `python3 synth.py --show 64 sheet.jpg` draws a contact sheet with each label (red: none, and why) |
| [`train.py`](train.py) | The training: a ResNet-18 (ImageNet weights) with three outputs (how many digits; the first; the second), samples drawn as it trains, measured every epoch on players in the training's fonts and in two fonts it never sees; the best epoch exported to `shirt_numbers.onnx` with its results in the file's metadata |
| [`job.yml`](job.yml), [`conda.yml`](conda.yml) | The Azure ML command job and its environment (CPU PyTorch, ONNX, MLflow for the run's metrics) |
| [`aml.sh`](aml.sh) | Submits the job, streams it, registers the model (`shirt-numbers`), downloads it to `ai/vision/models/` |
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

## Tests

`python3 tests/test_shirts.py`: what the model sees and how its answer is read (every number 1–99, none, unsure, never a leading zero), the synthetic players' labels and that a seed always draws the same player, the ONNX export read back by the reader with PyTorch's own logits, a model trained on another input refused, and the comparison's counts.
